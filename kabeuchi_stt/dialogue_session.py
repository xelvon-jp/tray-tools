# -*- coding: utf-8 -*-
"""AI対話モード: セッション（録りっぱなし）。GUI/Qt/COM 非依存。

マイク → 無音で区切る → small 認識 → バッファ（下書き）→ 確定 → 配達依頼（コールバック）。

- 録音は既存の session.Session に任せる（WAV も残る）。区切り器は Session.start(extra_parts=...) で
  録音の AudioTap に載せる。AudioTap は区切り器の例外を握りつぶすので、録音スレッドは止まらない。
  このクラスが録音スレッドで行うのは「区切り器に渡す」「条件が揃ったら作業スレッドのキューへ積む」だけ。
- 認識は作業スレッド（低優先度）、配達は別の配達スレッド。配達が遅くても認識は止まらない。
  認識は区切りごとの清書（small など）だけ。仮認識（逐次の暫定の文字）は廃止した。
- recognizer・deliver_callback・Session・時計はすべて注入できる（実モデル・実マイク無しでテストできる）。
- 確定操作 commit() は、いま途中の発話も区切って含める。認識待ちの発話が全部バッファに入ってから確定する
  （作業スレッドのキューの順序で保証）。
- セッションを終えるとき、清書が終わっていない発話は、清書が済んでから下書きへ入れる（stop の wait 秒まで待つ）。
"""
from __future__ import annotations

import datetime as dt
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import numpy as np

from dialogue_buffer import DeliveryRequest, DialogueBuffer
from dialogue_segmenter import UtteranceSegmenter, Utterance
from dialogue_text import collapse_repeats, is_noise_text, normalize_for_dup
from recorder import SAMPLE_RATE
from settings import dialogue_config

_END = "end"
DUP_WINDOW_SEC = 30.0       # 直前と同じ文を「重複」とみなす時間
DUP_MIN_CHARS = 3          # これ未満（「はい」「うん」など）は、同じでも重複として捨てない


def _lower_priority() -> None:
    try:
        from transcriber import _lower_thread_priority
        _lower_thread_priority()
    except Exception:
        pass


STYLE_PROMPT = "文字起こしの清書と逐次認識で、句読点が入るかを確認します。"      # 句読点の文体の見本（認識結果には出ない）。dialogue_text の幻覚除外が、この文を知っている


def make_whisper_recognizer(settings: dict, model_cache=None, model_loader=None) -> Callable[[np.ndarray, str], str]:
    """既定の認識器 recognizer(audio_int16, context) -> text。区切りごとに small 品質（beam=1）で認識する。

    直前の発話（context）を initial_prompt に足し、hotwords を渡す。置換辞書は DialogueSession 側で適用する。
    model_cache があればそれ経由（常駐キャッシュ）、無ければ初回に load_model して保持する。"""
    cfg = dialogue_config(settings)
    name = str(cfg.get("model") or "small")
    threads = int(settings.get("final_cpu_threads", 0))
    holder: dict[str, Any] = {}
    lock = threading.Lock()

    from models_catalog import supports_hotwords
    _hot_ok = supports_hotwords(name)       # kotoba-whisper は、hotwords を渡すと何も出なくなる

    def run(model, audio_i16: np.ndarray, context: str) -> str:
        audio = audio_i16.astype(np.float32) / 32768.0
        # 句読点の見本を、いつも末尾（音声のすぐ前）に置く。長い発話は、見本が無いと句読点なしで返ってくる。
        # 直前の発話（context）も句読点なしで返っていたら、それを見本にすると次も句読点なしになる（悪循環）ので、
        # 句読点を含むときだけ使う
        ctx = context.strip()[-80:] if any(ch in context for ch in "。、？！") else ""      # 前置きが長いと、出力できる長さが減る
        prompt = " ".join(p for p in ((settings.get("initial_prompt") or "").strip(), ctx, STYLE_PROMPT) if p) or None
        base = dict(language=settings.get("language", "ja"), beam_size=1, vad_filter=False,
                    condition_on_previous_text=False, initial_prompt=prompt,
                    hotwords=(settings.get("hotwords") or None) if _hot_ok else None,
                    # 時刻を出さない方式は速いが、長い発話の途中で認識を打ち切ることがある（実測: 30秒の発話で15字しか出ず、残りが消えた）。
                    # 清書は時刻を出す方式（約5割遅い）で、取りこぼしを避ける
                    without_timestamps=False)
        # 短い・雑音だけの音声で、認識が50〜60秒も回り続けて、後ろの発話が全部待たされることがあった（実測: 1秒の音に47秒）。
        # 温度の切り替えによる再試行をやめ（temperature=0 の1回だけ）、出力の長さにも上限（音声1秒あたり約8トークン＋余白）を付ける。
        # 認識は遅延評価（結果を取り出すときに処理が走る）なので、list にして中で例外を受ける。失敗したら、制限なしの設定で1回やり直す
        # Whisper は、前置き（prompt・hotwords）と出力の合計が 448 トークンまで。前置きが長いのに出力の上限を大きく取ると
        # 「合計が長すぎる」エラーになる（実測: 前置き240＋出力230）。一方、前置きの見積もりが大きすぎると、出力の上限が小さくなって
        # 長い発話が途中で切れる（実測: 20秒・130字の発話が70字で切れた）。**トークナイザで実際の長さを数えて**、残りを上限にする
        cap = None
        try:
            tok = model.hf_tokenizer
            hot = settings.get("hotwords") or ""
            used = len(tok.encode(" " + prompt, add_special_tokens=False).ids) if prompt else 0
            used += len(tok.encode(" " + hot, add_special_tokens=False).ids) if hot else 0
            cap = max(128, min(int(len(audio) / 16000.0 * 8) + 24, 440 - used))
        except Exception:
            cap = None          # 数えられなければ上限は付けない（temperature=0 の1回だけで、再試行の長引きは防げる）
        # 温度は 0 → 0.3 の2段だけ。同じ言葉の暴走（圧縮率が高い結果）のときだけ、温度を上げてやり直す（実測: 「途中で言葉が抜けたり、」が
        # 6回続き、出力の上限（128）で「途中で言」と切れた）。普通の発話は1回で終わるので、遅くならない
        bounded = dict(base, temperature=[0.0, 0.3], compression_ratio_threshold=2.2)
        if cap is not None:
            bounded["max_new_tokens"] = cap
        try:
            segs = list(model.transcribe(audio, **bounded)[0])
        except Exception as e:
            try:
                from dialogue_log import dlog
                dlog("recognize_retry", error=f"{type(e).__name__}: {str(e)[:160]}", audio_s=round(len(audio) / 16000.0, 1))
            except Exception:
                pass
            segs = list(model.transcribe(audio, **base)[0])
        # 無音の幻覚を捨てる条件は Whisper 本来の規則（無音確率が高く、かつ自信も低いときだけ）にする。
        # 無音確率だけで捨てると、声が小さい（-36dBFS 前後）ときの本物の発話が丸ごと消える（実測: 0.75 で捨てられた）
        def _is_silence(x) -> bool:
            return (getattr(x, "no_speech_prob", 0) >= 0.6 and getattr(x, "avg_logprob", 0.0) < -1.0)
        return "".join(x.text for x in segs if not _is_silence(x)).strip()

    def recognize(audio_i16: np.ndarray, context: str = "") -> str:
        if model_cache is not None:
            with model_cache.use(name, threads) as model:
                return run(model, audio_i16, context)
        with lock:
            if "m" not in holder:
                if model_loader is not None:
                    holder["m"] = model_loader(name, threads)
                else:
                    from transcriber import load_model
                    holder["m"] = load_model(name, settings, cpu_threads=threads)
        return run(holder["m"], audio_i16, context)

    return recognize


class DialogueSession:
    def __init__(self, settings: dict, session, recognizer: Callable[[np.ndarray, str], str],
                 deliver_callback: Callable[[str], Any], replacer=None,
                 buffer: DialogueBuffer | None = None,
                 on_event: Callable[[str, dict], None] | None = None,
                 on_status: Callable[[str], None] = lambda m: None,
                 now: Callable[[], dt.datetime] = dt.datetime.now):
        """session: session.Session（start(wav_path, live_enabled, extra_parts) / stop() / is_recording を持つもの）。
        recognizer(audio_int16, context) -> text。deliver_callback(text) -> 結果（dict の status が "delivered" で成功）。
        on_event(name, info) は作業スレッド・配達スレッドから呼ばれる（Qt へはシグナル経由で渡すこと）。"""
        self.s = settings
        self.cfg = dialogue_config(settings)
        self._session = session
        self._recognizer = recognizer
        self._deliver = deliver_callback
        self._replacer = replacer
        self._pend_utts: dict[float, Utterance] = {}  # 清書（small）待ちの発話（ID は区切りの開始秒）
        self._commit_lock = threading.Lock()
        self._commit_thread: threading.Thread | None = None
        self._on_event = on_event
        self._on_status = on_status
        self._now = now
        para = bool(settings.get("paragraph_enabled", True))
        self.buffer = buffer or DialogueBuffer(
            merge_undelivered=bool(self.cfg["merge_undelivered"]),
            gap_sec=float(settings.get("paragraph_gap_sec", 2.0)) if para else None,
            force_gap_sec=float(settings.get("paragraph_force_gap_sec", 3.0)) if para else None,
            punctuate_on_commit=bool(self.cfg["punctuate_on_commit"]),
            sentence_newline=bool(self.cfg.get("newline_after_sentence", True)),
            blank_line=bool(self.cfg.get("break_blank_line", True)))
        self._seg: UtteranceSegmenter | None = None
        self.audio_tap = None            # 音声ブロックの追加の受け手: tap(x, drop_recent)。舌打ち検出用（controller が設定）
        self._q: queue.Queue = queue.Queue()
        self._dq: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._deliverer: threading.Thread | None = None
        self._lock = threading.RLock()       # 開始・終了の状態だけ。録音スレッドからは取らない（stop が録音を待つため）
        self._pend_lock = threading.Lock()   # 認識待ちの数
        self._accepting = False        # 録音スレッドから発話・自動処理を受け付けるか
        self._active = False
        self._pending = 0              # 認識待ちの発話数
        self._recent: deque[str] = deque(maxlen=max(0, int(self.cfg["context_sentences"])) or 1)
        self._idle_requested = False
        self.wav_path: Path | None = None
        self.started_at: dt.datetime | None = None
        self.ended_at: dt.datetime | None = None
        self.recognition_errors = 0
        self.utterances_total = 0
        self.last_error = ""
        self._end_report: dict | None = None
        self._rec_ratio = 0.35                  # 認識にかかる時間÷音声の長さ（移動平均。初期値は small の目安）
        self._rec_cur = None
        self._recognizing = 0          # いま認識器に渡している発話数（認識待ちのうち実行中）
        self._last_norm = ""           # 直前に下書きへ入れた発話の正規化形（同じ文の連続を捨てる用）
        self._last_norm_at = 0.0
        self.dropped_dup = 0           # 直前と同じ文として捨てた回数
        self.collapsed = 0             # 同じ文の大量出力を1回にまとめた（取り除いた文の）数
        self._seg_log: list[tuple[float, float, str]] = []   # セッション中に確定した発話（start, end, 最終の文字）。履歴を作る用

    # ---- 公開状態
    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_recording(self) -> bool:
        """録音中か（窓の「録音中」表示用）。"""
        return bool(self._active and getattr(self._session, "is_recording", False))

    @property
    def is_held(self) -> bool:
        return self.buffer.held

    def session_segments(self) -> list[tuple[float, float, str]]:
        """このセッションで確定した発話 [(start_sec, end_sec, text), ...]（時刻順。ノイズ除外・置換辞書適用後の最終の文字）。
        「録音全体の清書」をしないとき、再認識せずに履歴の1件を作るのに使う。"""
        return sorted(self._seg_log, key=lambda x: (x[0], x[1]))

    @property
    def pending_recognitions(self) -> int:
        return self._pending

    @staticmethod
    def uid_of(start_sec: float) -> float:
        """発話の ID（区切りの開始秒。区切ったあとの「認識中」の行と、清書の結果で同じになる）。"""
        return round(float(start_sec), 3)

    def snapshot(self) -> dict:
        seg = self._seg
        return {"active": self._active, "recording": self.is_recording, "held": self.buffer.held,
                "speaking": bool(seg and seg.in_speech), "silence_sec": seg.silence_sec if seg else 0.0,
                "pending_recognitions": self._pending, "recognizing": self._recognizing,
                "delivering": self.buffer.delivering, "draft_edited": self.buffer.draft_edited,
                "draft_text": self.buffer.draft_text, "undelivered_count": self.buffer.undelivered_count,
                "next_commit_is_retry": self.buffer.next_commit_is_retry,
                "committing": self._commit_lock.locked()}      # 確定の処理中（清書待ちの発話を待っている間を含む）

    def set_voice_min_dbfs(self, v: float | None) -> None:
        """認識開始音量を更新する（セッション中なら区切り器へ即時反映。次の発話から効く）。"""
        self.cfg["voice_min_dbfs"] = None if v is None else float(v)
        seg = self._seg
        if seg is not None:
            seg.set_loud_dbfs(self.cfg["voice_min_dbfs"])

    # ---- 開始・終了
    def start(self, wav_path: Path | None = None, live_enabled: bool = False) -> Path:
        """録音とセッションを始める。live_enabled は既存の逐次認識（既定は切る。CPU を認識に回すため）。"""
        with self._lock:
            if self._active:
                raise RuntimeError("すでにセッション中です")
            c = self.cfg
            self._seg = UtteranceSegmenter(
                gap_sec=float(c["utterance_gap_sec"]),
                abs_dbfs=float(self.s.get("silence_stop_peak_dbfs", -50.0)),
                min_voiced_sec=float(c["min_utterance_sec"]), max_utt_sec=float(c["max_utterance_sec"]),
                loud_dbfs=(float(c["voice_min_dbfs"]) if c.get("voice_min_dbfs") is not None else None),
                on_utterance=self._on_utterance)
            self._q, self._dq = queue.Queue(), queue.Queue()
            self._pending, self._idle_requested = 0, False
            self._pend_utts = {}
            self._seg_log = []
            self._last_norm, self._last_norm_at = "", 0.0
            self.dropped_dup = self.collapsed = 0
            self.buffer.begin_session()
            self._end_report = None
            self.ended_at = None
            self.utterances_total = self.recognition_errors = 0
            self._accepting = True
            self._active = True
            self._worker = threading.Thread(target=self._work_loop, name="kabeuchi-dialogue", daemon=True)
            self._deliverer = threading.Thread(target=self._deliver_loop, name="kabeuchi-dialogue-deliver", daemon=True)
            try:
                self.wav_path = self._session.start(wav_path, live_enabled=live_enabled, extra_parts=(self._on_audio,))
            except Exception:
                self._accepting = self._active = False
                raise
            self.started_at = self._now()
            self._worker.start()
            self._deliverer.start()
        self._emit("started", {"wav": str(self.wav_path)})
        return self.wav_path

    def stop(self, wait: float = 60.0) -> dict:
        """録音を止めてセッションを終える。残りの発話を認識（清書）し、確定済みの配達依頼を片付けてから戻る
        （合わせて wait 秒まで。作業スレッドからの自動終了では待たない）。清書が終わっていない発話は、清書が済んでから
        下書きへ入る（戻り値の pending_recognitions が 0 でなければ、wait 秒では終わらなかった＝終わり次第、下書きへ追加される）。
        戻り値は end_report()。"""
        if not self._shutdown():
            return self._end_report or self.end_report()
        me = threading.current_thread()
        if me not in (self._worker, self._deliverer):
            end = time.monotonic() + float(wait)
            for t in (self._worker, self._deliverer):
                if t is not None:
                    t.join(timeout=max(0.0, end - time.monotonic()))
        return self.end_report()

    def _shutdown(self) -> bool:
        """終了の処理を一度だけ行う。すでに終わっていれば False。ロックは録音を止める間は持たない。"""
        with self._lock:
            if not self._active:
                return False
            self._active = False
        try:
            self._session.stop()                   # 録音を止める（WAV は残る）
        except Exception as e:
            self.last_error = f"録音の停止で例外: {type(e).__name__}: {e}"
            self._on_status(self.last_error)
        seg = self._seg
        if seg is not None:
            seg.flush()                            # 最後の発話をキューへ
        self._accepting = False
        ct = self._commit_thread
        if ct is not None and ct is not threading.current_thread():
            ct.join(timeout=2.0)       # 確定の途中なら、配達の依頼を積み終えてから終える（積み残しを作らない）
        self.ended_at = self._now()
        self._q.put((_END,))
        return True

    def end_report(self) -> dict:
        """終了時の確認用。未配達・未確定の下書きが残っていれば needs_attention。"""
        r = self.buffer.end_report()
        r["wav"] = str(self.wav_path) if self.wav_path else ""
        r["started_at"] = self.started_at.isoformat(timespec="seconds") if self.started_at else ""
        r["ended_at"] = self.ended_at.isoformat(timespec="seconds") if self.ended_at else ""
        r["recognition_errors"] = self.recognition_errors
        r["pending_recognitions"] = self._pending      # 清書がまだ終わっていない発話（0 でなければ、終わり次第、下書きへ追加される）
        self._end_report = r
        return r

    def close(self) -> None:
        try:
            self.stop(wait=5.0)
        except Exception:
            pass

    # ---- 操作
    def commit(self) -> bool:
        """確定操作（ヘッドセット・窓のボタン・ホットキー）。**利用者の明示的な操作のときだけ**呼ばれる
        （このクラスは自分では呼ばない。無音による自動確定は無い）。未配達があれば再試行になる（バッファ側の規則）。
        受け付けたら True。いま話している途中の発話も含め、清書が済むのを最大 60 秒待ってから、その時点の下書き全体を確定する。
        確定の処理は別スレッド（呼び出し元の画面・ヘッドセットを止めない）。確定中の二重の操作は無視する。"""
        if not self._active:
            return False
        if not self._commit_lock.acquire(blocking=False):
            return True
        t = threading.Thread(target=self._commit_flow, name="kabeuchi-dialogue-commit", daemon=True)
        self._commit_thread = t
        t.start()
        return True

    def _unresolved_without_text(self) -> list[float]:
        """確定の前に待つ発話（清書待ちのすべて）。常に清書（small など）を待つ。"""
        with self._pend_lock:
            return list(self._pend_utts)

    def _commit_flow(self) -> None:
        try:
            seg = self._seg
            if seg is not None and not self.buffer.held:
                seg.flush()                      # いま話している途中の発話を区切って、清書待ちへ
            end = time.monotonic() + 60.0        # 清書が揃うのを待つ（上限 60 秒）
            while self._active and time.monotonic() < end and self._unresolved_without_text():
                time.sleep(0.05)
            req = self.buffer.commit()
        except Exception as e:   # 確定の処理は落とさない（下書きは残る）
            self.last_error = f"{type(e).__name__}: {e}"
            self._on_status(f"対話モードの確定でエラー（下書きは残っています）: {self.last_error}")
            req = None
        finally:
            self._commit_lock.release()     # 配達中かどうかはバッファが持つ（二重配達しない）ので、依頼を積む前に外してよい
        if req is not None:
            self._emit("commit_request", {"mode": req.mode, "text": req.text})
            self._dq.put(req)

    def hold(self, on: bool = True) -> None:
        """保留（取り込み停止）。いま話している途中の発話は、捨てずに区切って認識に回す（保留に入る前に話した分なので、
        認識が終われば下書きへ入る）。保留中に新しく話した分は取り込まない。録音（WAV）は止めない。確定済みのものには影響しない。"""
        if on and self._seg is not None and not self.buffer.held:
            try:
                self._seg.flush()            # 保留にする前に区切る（保留後だと _on_utterance が捨てる）
            except Exception:
                pass
        self.buffer.hold(on)
        try:     # 一時停止（保留）の入り・切りを記録（取り込まれたか・捨てたかの切り分け用）
            from dialogue_log import dlog
            dlog("hold", held=bool(on), pending=self._pending)
        except Exception:
            pass
        self._emit("hold", {"held": bool(on)})

    def set_draft_text(self, text: str, base_rev: int | None = None) -> bool:
        """下書きの手修正を反映する（DialogueBuffer.set_draft_text）。"""
        return self.buffer.set_draft_text(text, base_rev)

    WAVE_MIN_ABOVE_DB = 6.0        # 声の下限（silence_stop_peak_dbfs）よりこれだけ大きいピークの発話だけ波形を出す
    WAVE_MAX_SILENCE_SEC = 0.6     # 話している途中の波形は、最後の声からこれ以上の無音が続いたら出さない

    @staticmethod
    def _peak_dbfs(audio) -> float:
        try:
            from mic_check import to_dbfs
            return float(to_dbfs(float(np.abs(np.asarray(audio, dtype=np.int32)).max()))) if len(audio) else -120.0
        except Exception:
            return 0.0

    def pending_waves(self) -> list[dict]:
        """区切れたが、まだ文字になっていない発話（清書待ち）の波形。[{uid, start, end, env}]（時刻順）。
        env は棒の高さ（0〜8）。画面が下書き欄の「認識中…」の行を描くのに使う。保留中は出さない。
        いま話している途中の音は含めない（live_view が返す）。"""
        if self.buffer.held:
            return []
        out = []
        with self._pend_lock:
            pend = sorted(self._pend_utts.items())
        cache = self.__dict__.setdefault("_wave_cache", {})
        live = set()
        # 声のある発話だけ: 小さすぎる音（環境ノイズ）の塊は出さない
        floor = float(self.s.get("silence_stop_peak_dbfs", -50.0)) + self.WAVE_MIN_ABOVE_DB
        items = [(uid, u.start_sec, u.end_sec, u.audio) for uid, u in pend if self._peak_dbfs(u.audio) >= floor]
        from ui_dialogue_logic import wave_bars_for, wave_levels
        for uid, start, end, audio in items:
            live.add(uid)
            n = len(audio)
            hit = cache.get(uid)
            if hit is None or hit[0] != n:
                dur = n / float(SAMPLE_RATE)
                hit = (n, wave_levels(audio, wave_bars_for(dur)))
                cache[uid] = hit
            out.append({"uid": uid, "start": start, "end": end, "env": hit[1], "audio_s": n / float(SAMPLE_RATE)})
        for k in list(cache):
            if k not in live:
                del cache[k]
        out.sort(key=lambda d: d["uid"])
        # 認識が終わるまでの残り時間の見積もり（順番待ちの分を足す）。いま認識中のものは、始めてからの経過を引く
        acc = 0.0
        cur = self._rec_cur
        for d in out:
            need = self._rec_ratio * float(d.get("audio_s") or 0.0)
            if cur is not None and cur[0] == d["uid"]:
                need = max(0.5, need - (time.monotonic() - cur[1]))
            acc += need
            d["eta"] = acc
        return out

    LIVE_BARS = 24                 # 話している途中の波形の棒の数（1本＝0.1秒。右端が最新）

    def live_view(self) -> dict:
        """いま話している途中の音（まだ区切っていない発話）の表示用。{speaking, seconds, env}。
        speaking: 声があり、最後の声から WAVE_MAX_SILENCE_SEC 以内（無音のあいだは False。波形・長さは止める）。
        seconds: その発話の開始（出だしの preroll を含む）からの長さ。env: 直近 LIVE_BARS 本（0.1秒ごと）の棒の高さ（0〜8）。
        保留中・区切り器が無いときは speaking=False。"""
        seg = self._seg
        if seg is None or self.buffer.held or not self._active:
            return {"speaking": False, "seconds": 0.0, "env": (), "wave": ()}
        from ui_dialogue_logic import live_bars, live_wave
        # wave: 区切るまでの間（無音で待っているあいだも）出す、喋った長さに比例するバー（1本＝1/6秒）。
        # 末尾の無音は含めない（無音のあいだは伸ばさない）。画面は下書き欄の末尾の行に出す
        whole = seg.current_audio()
        wave: tuple = ()
        if whole is not None:
            a = whole[2]
            trim = int(max(0.0, float(seg.silence_sec)) * SAMPLE_RATE)
            wave = live_bars(a[:len(a) - trim] if 0 < trim < len(a) else a, SAMPLE_RATE)
        cur = seg.current_audio(max_silence_sec=self.WAVE_MAX_SILENCE_SEC)
        if cur is None:
            return {"speaking": False, "seconds": 0.0, "env": (), "wave": wave}
        _start, _end, audio = cur
        return {"speaking": True, "seconds": max(0.0, len(audio) / float(SAMPLE_RATE)),
                "env": live_wave(audio, self.LIVE_BARS), "wave": wave}

    def discard_draft(self) -> str:
        """未確定の下書きを破棄する。"""
        return self.buffer.discard_draft()

    def toggle_hold(self) -> bool:
        self.hold(not self.buffer.held)
        return self.buffer.held

    # ---- 録音スレッドから（軽く・例外を出さない）
    def _on_audio(self, x: np.ndarray) -> None:
        try:
            if not self._accepting:
                return
            seg = self._seg
            seg.feed(x)
            tap = self.audio_tap
            if tap is not None:      # 舌打ち2回の検出（実験）。検出したら、その合図の音を区切りから取り除く
                try:
                    tap(x, seg.drop_recent)
                except Exception:
                    pass
            sil = seg.silence_sec
            # 注意: ここから確定（commit）を呼ばない。無音で自動確定すると、利用者が何も操作していないのに
            # 下書きが宛先へ入ってしまう。確定は commit()（ボタン・ヘッドセット・ショートカット）だけ。
            idle = float(self.cfg["idle_stop_sec"])
            if idle > 0 and sil >= idle and not self._idle_requested:
                self._idle_requested = True
                self._q.put(("idle_stop",))
        except Exception:
            pass

    def _on_utterance(self, u: Utterance) -> None:   # 区切り器から（録音スレッドまたは commit 呼び出し元）
        try:
            if self.buffer.held:
                try:
                    from dialogue_log import dlog
                    dlog("held_drop", audio_s=round(len(u.audio) / SAMPLE_RATE, 1))     # 保留中に喋った分（取り込まない）
                except Exception:
                    pass
                return
            with self._pend_lock:
                self._pending += 1
                self._pend_utts[self.uid_of(u.start_sec)] = u
            self._q.put(("utt", u))
        except Exception:
            pass

    # ---- 作業スレッド（認識・確定）
    def _work_loop(self) -> None:
        _lower_priority()
        q = self._q
        while True:
            item = q.get()
            kind = item[0]
            try:
                if kind == "utt":
                    try:
                        self._recognizing = 1
                        self._recognize(item[1])
                    finally:
                        self._recognizing = 0
                        self._rec_cur = None
                        with self._pend_lock:
                            self._pending -= 1
                            self._pend_utts.pop(self.uid_of(item[1].start_sec), None)
                elif kind == "idle_stop":
                    self._emit("idle_stop", {})
                    self._shutdown()
                elif kind == _END:
                    break
            except Exception as e:  # 作業スレッドは落とさない
                self.last_error = f"{type(e).__name__}: {e}"
                self._on_status(f"対話モードの内部エラー（続行）: {self.last_error}")
        self._dq.put(None)   # 配達スレッドへ「これ以上依頼は来ない」
        self.end_report()
        self._emit("ended", {})

    def _recognize(self, u: Utterance) -> None:
        uid = self.uid_of(u.start_sec)
        ctx_n = int(self.cfg["context_sentences"])
        context = "".join(list(self._recent)[-ctx_n:]) if ctx_n > 0 else ""
        t_rec = time.monotonic()
        self._rec_cur = (uid, t_rec)                 # いま認識している発話（残り時間の見積もり用）
        try:
            text = (self._recognizer(u.audio, context) or "").strip()
            took = time.monotonic() - t_rec
            audio_s = len(u.audio) / float(SAMPLE_RATE)
            if audio_s >= 1.0 and took > 0:           # 認識の速さ（所要時間÷音声の長さ）の移動平均。短すぎる音は除く
                self._rec_ratio = 0.7 * self._rec_ratio + 0.3 * min(3.0, took / audio_s)
            try:     # 認識の速さの記録（待ちが溜まる原因の切り分け用。文字は書かない）
                from dialogue_log import dlog
                dlog("recognized", audio_s=round(len(u.audio) / SAMPLE_RATE, 1), took_s=round(time.monotonic() - t_rec, 1),
                     chars=len(text), pending=self._pending)
            except Exception:
                pass
        except Exception as e:   # 音声は WAV に残っているので、あとから清書で拾える
            self.recognition_errors += 1
            self.last_error = f"認識エラー: {type(e).__name__}: {e}"
            try:
                from dialogue_log import dlog
                dlog("recognition_error", error=f"{type(e).__name__}: {str(e)[:200]}", audio_s=round(len(u.audio) / SAMPLE_RATE, 1))
            except Exception:
                pass
            self._on_status(self.last_error)
            self._emit("recognition_error", {"error": self.last_error, "start_sec": u.start_sec})
            return
        if self._replacer is not None and text:
            try:
                text = self._replacer.apply(text)
            except Exception:
                pass
        if text and is_noise_text(text):      # 無音・ノイズに対する定番の幻覚（「ご視聴ありがとうございました」等）は空として扱う
            text = ""
        if text:      # 同じ文が3回以上出てきたら1回にまとめる（暴走した繰り返し）
            text, removed = collapse_repeats(text)
            if removed:
                self.collapsed += removed
                self._dlog("collapsed", n=removed)
        if text:      # 直前の発話と同じ文が続けて出たら、2回目以降を捨てる
            norm, now_m = normalize_for_dup(text), time.monotonic()
            if norm and len(norm) >= DUP_MIN_CHARS and norm == self._last_norm and (now_m - self._last_norm_at) <= DUP_WINDOW_SEC:
                self.dropped_dup += 1
                self._dlog("dropped_dup", chars=len(norm))
                text = ""
        self.utterances_total += 1
        # allow_held: 保留に入る前に話し終えて認識に回した発話は、保留中でも下書きへ入れる
        added = self.buffer.add_utterance(text, start_sec=u.start_sec, end_sec=u.end_sec, uid=uid, allow_held=True)
        if text and added:
            self._last_norm, self._last_norm_at = normalize_for_dup(text), time.monotonic()
            self._seg_log.append((float(u.start_sec), float(u.end_sec), text))
            self._recent.append(text)
            self._emit("utterance", {"text": text, "start_sec": u.start_sec})

    @staticmethod
    def _dlog(event: str, **fields) -> None:
        try:
            from dialogue_log import dlog
            dlog(event, **fields)
        except Exception:
            pass

    # ---- 配達スレッド
    def _deliver_loop(self) -> None:
        while True:
            req = self._dq.get()
            if req is None:
                return
            while req is not None:
                try:
                    result: Any = self._deliver(req.text)
                except Exception as e:   # 配達側の例外でも、テキストは未配達として保持する
                    result = e
                out = self.buffer.report(req, result)
                info = {"mode": req.mode, "ids": req.ids, "delivered": out.delivered,
                        "reason": out.reason, "stage": out.stage, "sent": out.sent, "send_reason": out.send_reason}
                self._emit("delivered" if out.delivered else "undelivered", info)
                # 再試行で、まとめずに古い順に1通ずつ送る設定のとき: 成功したら次の未配達へ進む
                if out.delivered and req.mode == "retry" and self.buffer.has_undelivered:
                    req = self.buffer.begin_retry()
                else:
                    req = None

    def _emit(self, name: str, info: dict) -> None:
        cb = self._on_event
        if cb is not None:
            try:
                cb(name, info)
            except Exception:
                pass
