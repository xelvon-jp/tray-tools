# -*- coding: utf-8 -*-
"""文字起こしコア（GUI・Qt・COM に依存しない）。

- LiveTranscriber : 録音中の逐次認識（軽いモデル）。feed() は積むだけで即戻る。
- transcribe_wav  : 停止後の清書（大きめのモデル）。WAV 全体を認識し txt/md を保存。
- モデルは settings の model_dir にオフライン手置きできる。
"""
from __future__ import annotations

import datetime as _dt
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from recorder import SAMPLE_RATE
from settings import resolve_dir

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "preprocessor_config.json", "vocabulary.*")


class ModelUnavailableError(RuntimeError):
    """モデルが手元に無く、ダウンロードもできなかった。"""


def offline_help(name: str, model_dir: Path, cause: str = "") -> str:
    repo = f"Systran/faster-whisper-{name}"
    return (
        f"モデル '{name}' を取得できませんでした。\n"
        f"（原因: {cause}）\n\n"
        "職場PCなどで SSL 検査により Hugging Face に接続できない場合は、別PCで取得して手置きしてください。\n"
        "【手順】\n"
        "1. ネットに出られる別PCで次を実行（faster-whisper を入れた Python で）:\n"
        f"   python -c \"from faster_whisper import download_model; print(download_model('{name}'))\"\n"
        "   表示されたフォルダ（config.json / model.bin / tokenizer.json / vocabulary.* などが入っている）が取得済みモデルです。\n"
        f"   ブラウザで https://huggingface.co/{repo}/tree/main から全ファイルを手動ダウンロードしても同じです。\n"
        f"2. そのフォルダの中身を、このPCの次の場所へそのままコピー:\n"
        f"   {model_dir / name}\\\n"
        "   （直下に model.bin と config.json が来る形。settings.json の model_dir で場所を変えられます）\n"
        "3. もう一度実行すると、ネットに出ずにそのモデルを使います。"
    )


def load_model(name: str, settings: dict, cpu_threads: int = 0, log: Callable[[str], None] = print):
    """手置き → 取得済みキャッシュ → ダウンロード の順に探してモデルを読む。"""
    from faster_whisper import WhisperModel

    model_dir = resolve_dir(settings.get("model_dir", "models"))
    device = settings.get("device", "cpu")
    ctype = settings.get("compute_type", "int8")
    kw = dict(device=device, compute_type=ctype, cpu_threads=cpu_threads)

    # 1) 手置き: model_dir/<name>/model.bin、または name 自体がフォルダパス
    for cand in (model_dir / name, Path(name)):
        if (cand / "model.bin").exists():
            log(f"モデル読込（手置き・オフライン）: {cand}")
            return WhisperModel(str(cand), local_files_only=True, **kw)
    # 2) model_dir を HF キャッシュ形式の置き場として、取得済みなら使う
    try:
        log(f"モデル読込（取得済みキャッシュ）: {name}")
        return WhisperModel(name, download_root=str(model_dir), local_files_only=True, **kw)
    except Exception:
        pass
    # 3) ダウンロード
    try:
        log(f"モデル '{name}' をダウンロードします（初回のみ）…")
        return WhisperModel(name, download_root=str(model_dir), local_files_only=False, **kw)
    except Exception as e:
        raise ModelUnavailableError(offline_help(name, model_dir, f"{type(e).__name__}: {e}")) from e


def _lower_thread_priority() -> None:
    """呼び出しスレッドの優先度を下げる（Windows のみ）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), -2)  # THREAD_PRIORITY_LOWEST
    except Exception:
        pass


# ---------------------------------------------------------------- 逐次認識
class LiveTranscriber:
    """録音中の逐次認識。無音区切りで仮認識して on_text(text) に渡す。

    feed() はキューに積むだけ。区切り検出も認識も専用の低優先度スレッド。
    認識が遅れても feed 側（=録音側）は一切待たされない。
    """

    FRAME = 480  # 30ms

    def __init__(self, settings: dict, on_text: Callable[[str], None] = lambda t: None,
                 on_status: Callable[[str], None] = lambda s: None,
                 on_segment: Callable[[float, float, str], None] | None = None,
                 model_context: Callable[[], object] | None = None):
        """on_text(text) は v1 互換。on_segment(start_sec, end_sec, text) は v2（タイムライン用）。
        model_context() は「with で model を返す」コンテキストマネージャを返す関数（常駐キャッシュ用）。
        """
        self.s = settings
        self.on_text = on_text
        self.on_status = on_status
        self.on_segment = on_segment
        self.model_context = model_context
        self._in: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.dropped_segments = 0
        self.processed_segments = 0
        self.pending_segments = 0
        self.error: str | None = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kabeuchi-live", daemon=True)
        self._thread.start()

    def feed(self, samples: np.ndarray) -> None:
        if self.error is None and not self._stop.is_set():
            self._in.put_nowait(samples)

    def stop(self, wait: float = 5.0):
        self._stop.set()
        self._in.put(None)
        if self._thread:
            self._thread.join(timeout=wait)

    # -- 内部
    def _run(self):
        _lower_thread_priority()
        seg_q: queue.Queue = queue.Queue()
        seg_thread = threading.Thread(target=self._segmenter, args=(seg_q,), daemon=True)
        seg_thread.start()
        import contextlib
        try:
            if self.model_context is not None:
                ctx = self.model_context()
            else:
                ctx = contextlib.nullcontext(load_model(
                    self.s.get("live_model", "base"), self.s,
                    cpu_threads=int(self.s.get("live_cpu_threads", 2)), log=self.on_status))
            model = ctx.__enter__()
        except Exception as e:
            self.error = str(e)
            self.on_status("逐次認識を無効化（録音は継続）: " + str(e).splitlines()[0])
            self._stop.set()
            self._in.put(None)
            return
        self.on_status("逐次認識 準備OK")
        try:
            self._loop(model, seg_q)
        finally:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass

    def _loop(self, model, seg_q):
        max_backlog = int(self.s.get("live_max_backlog", 20))
        lang = self.s.get("language", "ja")
        prompt = self.s.get("initial_prompt") or None
        while True:
            item = seg_q.get()
            if item is None or self._stop.is_set():
                break
            self.pending_segments = seg_q.qsize()
            # 遅れすぎたら古い区切りは捨てる（清書で必ず拾われる）
            while seg_q.qsize() > max_backlog:
                try:
                    item = seg_q.get_nowait()
                    self.dropped_segments += 1
                except queue.Empty:
                    break
                if item is None:
                    return
            start_sample, seg = item
            try:
                audio = seg.astype(np.float32) / 32768.0
                segs, _ = model.transcribe(
                    audio, language=lang, beam_size=1, vad_filter=False,
                    condition_on_previous_text=False, initial_prompt=prompt,
                    hotwords=self.s.get("hotwords") or None,
                    without_timestamps=True)
                text = "".join(x.text for x in segs if getattr(x, "no_speech_prob", 0) < 0.6).strip()
                self.processed_segments += 1
                if text:
                    if self.on_segment is not None:
                        self.on_segment(start_sample / SAMPLE_RATE,
                                        (start_sample + len(seg)) / SAMPLE_RATE, text)
                    self.on_text(text)
            except Exception as e:
                self.on_status(f"逐次認識エラー（録音は継続）: {e}")

    def _segmenter(self, seg_q: queue.Queue):
        """エネルギー(RMS)ベースの簡易VADで発話を区切り seg_q へ。"""
        s = self.s
        min_rms = float(s.get("vad_min_rms", 250))
        sil_frames = int(float(s.get("vad_silence_sec", 0.7)) * SAMPLE_RATE / self.FRAME)
        max_frames = int(float(s.get("vad_max_segment_sec", 25.0)) * SAMPLE_RATE / self.FRAME)
        min_speech = int(0.3 * SAMPLE_RATE / self.FRAME)
        preroll = 7  # 約0.2秒
        buf = np.zeros(0, dtype=np.int16)
        floor = min_rms / 2
        cur: list[np.ndarray] = []
        pre: list[np.ndarray] = []
        speech_n = 0
        sil_n = 0
        active = False
        consumed = 0       # ここまでに処理したフレーム数
        start_frame = 0    # 現在の区間の先頭フレーム（絶対位置）

        def flush():
            nonlocal cur, speech_n, sil_n, active
            if cur and speech_n >= min_speech:
                seg_q.put((start_frame * self.FRAME, np.concatenate(cur)))
            cur, speech_n, sil_n, active = [], 0, 0, False

        while True:
            x = self._in.get()
            if x is None:
                flush()
                seg_q.put(None)
                return
            buf = np.concatenate([buf, x])
            while len(buf) >= self.FRAME:
                fr, buf = buf[:self.FRAME], buf[self.FRAME:]
                consumed += 1
                rms = float(np.sqrt(np.mean(fr.astype(np.float32) ** 2)))
                thr = max(min_rms, floor * 2.5)
                voiced = rms > thr
                if not voiced:  # 騒音床を緩やかに追従
                    floor = floor * 0.98 + rms * 0.02
                if voiced:
                    if not active:
                        active = True
                        cur = list(pre)
                        start_frame = consumed - 1 - len(pre)
                    cur.append(fr)
                    speech_n += 1
                    sil_n = 0
                elif active:
                    cur.append(fr)
                    sil_n += 1
                    if sil_n >= sil_frames:
                        flush()
                else:
                    pre.append(fr)
                    if len(pre) > preroll:
                        pre.pop(0)
                if active and len(cur) >= max_frames:
                    flush()


# ---------------------------------------------------------------- 清書
@dataclass
class FinalResult:
    text: str
    segments: list = field(default_factory=list)  # (start, end, text)
    txt_path: Path | None = None
    md_path: Path | None = None
    audio_seconds: float = 0.0
    elapsed: float = 0.0
    gaps: list | None = None  # 区間ごとの「直前の無音」（音声から測定。整形に使う）


def wav_seconds(path: Path) -> float:
    """ヘッダ破損（録音中断）でも使えるよう、ファイルサイズから推定。"""
    try:
        return max(0.0, (Path(path).stat().st_size - 44) / 2 / SAMPLE_RATE)
    except OSError:
        return 0.0


def load_wav_float(path: Path) -> np.ndarray:
    """WAV を 16kHz mono float32 で読む。

    16kHz/mono/16bit の WAV（本ツールの録音）は自前で読む。ヘッダが未確定の
    中断WAVでもファイルサイズから読める。さらに PyAV の版違いによる
    decode_audio の不具合（av 19 で確認）も避けられる。それ以外の形式は
    faster-whisper の decode_audio に任せる。
    """
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(44)
        if (head[:4] == b"RIFF" and head[8:16] == b"WAVEfmt " and len(head) == 44
                and head[20:22] == b"\x01\x00" and head[22:24] == b"\x01\x00"
                and int.from_bytes(head[24:28], "little") == SAMPLE_RATE
                and int.from_bytes(head[34:36], "little") == 16
                and head[36:40] == b"data"):
            raw = f.read()
            raw = raw[: len(raw) // 2 * 2]
            return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    # 他の PCM16 WAV（SAPI 等で作った任意レート/ステレオ）は標準 wave で読んで変換
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() == 2:
                ch, sr = w.getnchannels(), w.getframerate()
                x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
                if ch > 1:
                    x = x.reshape(-1, ch).mean(axis=1)
                if sr != SAMPLE_RATE and len(x):
                    n = int(round(len(x) * SAMPLE_RATE / sr))
                    x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
                return x
    except Exception:
        pass
    from faster_whisper.audio import decode_audio  # mp3 等。PyAV の版によっては失敗しうる
    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)


def _mmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def save_outputs(wav_path: Path, segments: list, settings: dict, model_name: str,
                 out_dir: Path | None = None,
                 formatter: Callable[..., str] | None = None,
                 gaps: list | None = None) -> tuple[Path, Path, str]:
    """txt / md を保存。formatter（区間のリスト→全文）があれば全文はそれで整形（すき間による改行など）。
    元の区間（時刻つき）は変えない。md の「タイムスタンプ付き」は区間ごとのまま。"""
    out_dir = Path(out_dir) if out_dir else resolve_dir(settings.get("out_dir", "out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(wav_path).stem
    text = (formatter(segments, gaps=gaps) if gaps is not None else formatter(segments)) if formatter is not None else "\n".join(t for _, _, t in segments if t)
    txt = out_dir / f"{stem}.txt"
    md = out_dir / f"{stem}.md"
    txt.write_text(text + "\n", encoding="utf-8")
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 壁打ち文字起こし {stem}", "",
             f"- 清書日時: {now}", f"- モデル: {model_name}", f"- 音声: {Path(wav_path).name}", "",
             "## 全文", "", text, "", "## タイムスタンプ付き", ""]
    lines += [f"- [{_mmss(a)}] {t}" for a, _, t in segments if t]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt, md, text


def transcribe_wav(
    wav_path: Path,
    settings: dict,
    model_name: str | None = None,
    on_progress: Callable[[float], None] = lambda p: None,
    on_status: Callable[[str], None] = print,
    cancel: threading.Event | None = None,
    out_dir: Path | None = None,
    model=None,
    postprocess: Callable[[str], str] | None = None,
    formatter: Callable[..., str] | None = None,
) -> FinalResult:
    """WAV 全体を清書。進捗は 0.0〜1.0。"""
    wav_path = Path(wav_path)
    if not wav_path.exists():
        raise FileNotFoundError(f"WAV が見つかりません: {wav_path}")
    model_name = model_name or settings.get("final_model", "small")
    t0 = time.monotonic()
    if model is None:
        on_status(f"清書モデル '{model_name}' を準備中…")
        model = load_model(model_name, settings, cpu_threads=int(settings.get("final_cpu_threads", 0)),
                           log=on_status)
    on_status("清書中…")
    audio = load_wav_float(wav_path)
    segs, info = model.transcribe(
        audio,
        language=settings.get("language", "ja"),
        vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=settings.get("initial_prompt") or None,
        # initial_prompt は condition_on_previous_text=False だと最初の窓にしか効かない。
        # hotwords は全窓のプロンプトに入るので、専門語はこちらで渡す。
        hotwords=settings.get("hotwords") or None,
        beam_size=5,
    )
    total = len(audio) / SAMPLE_RATE or 1.0
    out = []
    for sg in segs:
        if cancel is not None and cancel.is_set():
            on_status("清書を中断しました（それまでの分は保存します）")
            break
        txt_seg = sg.text.strip()
        if postprocess is not None:
            txt_seg = postprocess(txt_seg)  # 置換辞書など（txt/md にも反映される）
        out.append((sg.start, sg.end, txt_seg))
        on_progress(min(1.0, sg.end / total))
    on_progress(1.0)
    gaps = None
    if formatter is not None:
        try:
            from gaps import silence_gaps
            gaps = silence_gaps(audio, out)
        except Exception:
            gaps = None  # 測れなくても清書は続ける（時刻のすき間だけで整形）
    txt, md, text = save_outputs(wav_path, out, settings, model_name, out_dir, formatter=formatter, gaps=gaps)
    return FinalResult(text=text, segments=out, txt_path=txt, md_path=md,
                       audio_seconds=total, elapsed=time.monotonic() - t0, gaps=gaps)
