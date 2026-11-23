# -*- coding: utf-8 -*-
"""AI対話モード: 部品の結線（Qt / COM 非依存）。

DialogueSession（録りっぱなし・バッファ）・WorkerClient（UIA ワーカー）・HeadsetButton・宛先プリセットを
ひとつにまとめ、窓（ui_dialogue.py）はこのクラスだけを触る。部品はすべて注入できる（フェイクで統合テストできる）。

スレッド:
- on_event(name, info) は作業スレッド・配達スレッド・ヘッドセットのスレッドから呼ばれる。窓は Qt シグナルで渡すこと。
- 宛先を選ぶ（pick）・窓の確認（resolve）・ワーカーの起動は専用スレッド（UI を止めない）。
- 配達は DialogueSession の配達スレッドが deliver_callback（このクラスの _deliver）を呼ぶ。
  直前に WorkerClient.deliver が AllowSetForegroundWindow を呼ぶ（前面化ロック対策）。

守ること:
- ワーカー・ヘッドセットはセッション開始（または宛先選択）のときに起こし、終了・shutdown() で必ず止める。何度呼んでも安全。
- 確定したテキストは捨てない（バッファ側の保証）。配達の失敗・例外はすべて「未配達」の結果として返す。
"""
from __future__ import annotations

import atexit
import threading
import time
from pathlib import Path
from typing import Any, Callable

import dialogue_profile as dp
import ui_dialogue_logic as L
from dialogue_buffer import DialogueBuffer
from dialogue_log import dlog
from dialogue_session import DialogueSession
from settings import dialogue_config

PICK_TIMEOUT_SEC = 30.0


class DialogueController:
    def __init__(self, settings: dict, session, recognizer: Callable[[Any, str], str], replacer=None,
                 client_factory: Callable[[], Any] | None = None,
                 headset_factory: Callable[[Callable[[], None]], Any] | None = None,
                 mouse_factory: Callable[[Callable[[], None], str], Any] | None = None,
                 click_factory: Callable[[Callable[[], None]], Any] | None = None,
                 base_dir=None, on_event: Callable[[str, dict], None] | None = None,
                 on_status: Callable[[str], None] = lambda m: None,
                 on_last_target: Callable[[str], None] | None = None,
                 grant: Callable[[], Any] | None = None,
                 on_settings_changed: Callable[[], None] | None = None,
                 session_factory: Callable[..., Any] = DialogueSession):
        """session: session.Session（録音）。recognizer(audio, context)->text。
        client_factory() -> WorkerClient 互換（既定は dialogue_client.WorkerClient(dialogue.worker_python)）。
        headset_factory(callback) -> HeadsetButton 互換（既定は dialogue_headset.HeadsetButton）。
        mouse_factory(callback, button) -> dialogue_mouse.MouseButton 互換（既定は MouseButton。マウスのサイドボタンで確定）。
        click_factory(callback) -> dialogue_click.ClickCommit 互換（既定は ClickCommit。舌打ち2回で確定・実験）。"""
        self.s = settings
        self._rec_session = session
        self._recognizer = recognizer
        self._replacer = replacer
        self._client_factory = client_factory
        self._headset_factory = headset_factory
        self._mouse_factory = mouse_factory
        self._click_factory = click_factory
        self.base_dir = base_dir
        self._on_event = on_event
        self._on_status = on_status
        self._on_last_target = on_last_target
        self._grant = grant
        self._on_settings_changed = on_settings_changed
        self._session_factory = session_factory
        self.client = None
        self.headset = None
        self.mouse = None
        self.mouse_reason = ""
        self.click = None
        self.click_reason = ""
        self.ds: Any = None                 # DialogueSession（直近のもの。終了後も未配達の操作に使う）
        self._lock = threading.RLock()
        self._closed = False
        self._stopping = False
        # 宛先
        self.profile: dict | None = None
        self.target_state = L.TG_NONE
        self.resolve_result: dict | None = None
        self.target_error = ""
        # 宛先を選ぶ
        self._pick_thread: threading.Thread | None = None
        self._pick_deadline = 0.0
        self.picked: dict | None = None     # 直近の pick の結果（status が "picked" のとき profile を持つ）
        self.headset_reason = ""
        self.last_end_report: dict | None = None
        # 統合 GUI: 配達する直前にテキストを加工する（None=そのまま。再試行でも毎回かかる）
        self.text_transform: Callable[[str], str] | None = None
        # 前置き（どの宛先にも）。prefix_text: 利用者が「前置き ▾」で下書きの先頭へ挿入したもの（次の確定の1回だけ使う）。
        # sending_prefix: 確定した瞬間に prefix_text から移された、いま送っている（または再試行を待つ）メッセージの前置き。
        # 実際に送る文字列 = 前置き本文 + 改行 + 下書き。送れたら（または破棄したら）消える。宛先「なし」では使わない。
        self.prefix_text = ""
        self.sending_prefix = ""
        last = str(self.cfg.get("last_target") or "")
        if last:
            try:
                self.profile = dp.load_preset(last, self.base_dir)
                self.target_state = L.TG_UNCHECKED
            except Exception:
                self.profile = None

    # ---- 設定
    @property
    def cfg(self) -> dict:
        return dialogue_config(self.s)

    @property
    def active(self) -> bool:
        return bool(self.ds is not None and self.ds.is_active)

    def snapshot(self) -> dict:
        return self.ds.snapshot() if self.ds is not None else {"active": False}

    def commit_method_names(self) -> list:
        """いま有効な確定の手段（画面の案内用。Ctrl+Enter・画面ボタンは常に使えるので含めない）。"""
        c = self.cfg
        names = []
        if c.get("headset_button"):
            names.append("ヘッドセット")
        if c.get("mouse_button"):
            names.append("マウス")
        if c.get("click_commit"):
            names.append("舌打ち")
        return names

    def set_option(self, key: str, value) -> None:
        """設定の dialogue 節の1キーだけ差し替えて保存する（パネルのチェックなど）。"""
        d = self.s.get("dialogue")
        if not isinstance(d, dict):
            d = self.s["dialogue"] = {}
        d[key] = value
        cb = self._on_settings_changed
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    @property
    def record_only(self) -> bool:
        """宛先「なし（記録だけ）」: 確定した内容を、入力欄へ届けず、クリップボードにも入れず、確定済みとして記録に残すだけにする
        （確定すると下書きから消えて、確定履歴に「記録のみ」で残る。配達は成功扱い）。"""
        return bool(self.cfg.get("dest_record_only"))

    def set_record_only(self, on: bool) -> None:
        self.set_option("dest_record_only", bool(on))
        self._emit("target", self.target_view_info())
        if not on and self.active and self.profile:
            self.resolve_async()

    def set_event_sink(self, fn: Callable[[str, dict], None] | None) -> None:
        self._on_event = fn

    def _emit(self, name: str, info: dict | None = None) -> None:
        cb = self._on_event
        if cb is not None:
            try:
                cb(name, info or {})
            except Exception:
                pass

    def _client(self):
        with self._lock:
            if self.client is None:
                if self._client_factory is not None:
                    self.client = self._client_factory()
                else:
                    from dialogue_client import WorkerClient
                    self.client = WorkerClient(str(self.cfg.get("worker_python") or ""))
                dlog("worker_client_created", custom_python=bool(self.cfg.get("worker_python")))
            return self.client

    # ---- プリセット
    def preset_names(self) -> list[str]:
        try:
            return [p.get("name") or Path(path).stem for path, p in dp.list_presets(self.base_dir)]
        except Exception:
            return []

    def select_preset(self, name: str) -> bool:
        """プリセットを宛先にする（セッション中なら窓を探し直す）。読めなければ False。"""
        try:
            prof = dp.load_preset(name, self.base_dir)
        except Exception as e:
            self._emit("target_error", {"error": f"プリセットを読めません: {e}"})
            return False
        with self._lock:
            self.profile = prof
            self.target_state, self.resolve_result, self.target_error = L.TG_UNCHECKED, None, ""
            if self.client is not None:
                self.client.held_hwnd = 0
        self._remember(name)
        self._emit("target", self.target_view_info())
        if self.active:
            self.resolve_async()
        return True

    def delete_preset(self, name: str) -> bool:
        ok = dp.delete_preset(name, self.base_dir)
        if ok and self.profile and self.profile.get("name") == name:
            with self._lock:
                self.profile, self.target_state, self.resolve_result = None, L.TG_NONE, None
            self._remember("")
            self._emit("target", self.target_view_info())
        return ok

    def _remember(self, name: str) -> None:
        d = self.s.setdefault("dialogue", {})
        if isinstance(d, dict):
            d["last_target"] = name
        cb = self._on_last_target
        if cb is not None:
            try:
                cb(name)
            except Exception:
                pass

    def target_view_info(self) -> dict:
        v = L.view_target(self.profile, self.target_state, self.resolve_result, self.target_error)
        return {"view": v}

    def target_view(self) -> L.TargetView:
        return L.view_target(self.profile, self.target_state, self.resolve_result, self.target_error)

    # ---- 窓の確認（握る）
    def resolve_async(self) -> None:
        threading.Thread(target=self._resolve_now, name="kabeuchi-dialogue-resolve", daemon=True).start()

    def _resolve_now(self) -> None:
        prof = self.profile
        if not prof or self.record_only:
            return
        try:
            res = self._client().resolve(prof)
            state, err = L.target_state_from_resolve(res), ""
        except Exception as e:   # WorkerError 含む。握れなかっただけで、配達の側でも再度探す
            res, state, err = None, L.TG_ERROR, f"{type(e).__name__}: {e}"
        with self._lock:
            if self.profile is not prof:    # 確認中に宛先が変わった
                return
            self.resolve_result, self.target_state, self.target_error = res, state, err
        self._emit("target", self.target_view_info())

    # ---- 宛先を選ぶ（クリックを待つ）
    @property
    def picking(self) -> bool:
        t = self._pick_thread
        return bool(t is not None and t.is_alive())

    def pick_remaining(self) -> float:
        return max(0.0, self._pick_deadline - time.monotonic()) if self.picking else 0.0

    def pick_async(self, name: str = "", exclude_roots=(), timeout: float = PICK_TIMEOUT_SEC) -> bool:
        """入力欄のクリックを待つ（別スレッド）。結果は on_event の "picked" / "pick_failed"。
        すでに選択中なら False。"""
        with self._lock:
            if self.picking or self._closed:
                return False
            self._pick_deadline = time.monotonic() + float(timeout)
            self._pick_thread = threading.Thread(
                target=self._pick_run, args=(name, tuple(exclude_roots), float(timeout)),
                name="kabeuchi-dialogue-pick", daemon=True)
            self._pick_thread.start()
        self._emit("pick_started", {"timeout": float(timeout)})
        return True

    def _pick_run(self, name: str, exclude, timeout: float) -> None:
        try:
            res = self._client().pick(name, timeout=timeout, exclude_roots=exclude)
        except Exception as e:
            res = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        try:
            if res.get("status") == "picked" and isinstance(res.get("profile"), dict):
                self.picked = res
                self._emit("picked", {"profile": res["profile"], "warnings": res.get("warnings") or [],
                                      "window": res.get("window") or {}})
            else:
                self.picked = None
                self._emit("pick_failed", {"status": res.get("status"), "text": L.pick_failure_text(res),
                                           "error": res.get("error", "")})
        finally:
            if not self.active:      # セッション外で起こしたワーカーは、選び終えたら止める
                self._stop_client()

    def save_picked(self, name: str, overwrite: bool = False) -> dict:
        """直近に選んだ宛先を名前を付けて保存し、宛先にする。名前が不正なら ValueError、
        同名があって overwrite=False なら FileExistsError（黙って上書きしない）。"""
        if not self.picked or not isinstance(self.picked.get("profile"), dict):
            raise ValueError("保存する宛先がありません（先に［宛先を選ぶ］）")
        chk = L.validate_preset_name(name, self.preset_names())
        if not chk.ok:
            raise ValueError(chk.message)
        prof = {**self.picked["profile"], "name": chk.name}
        dp.save_preset(prof, self.base_dir, overwrite=overwrite)
        with self._lock:
            self.profile = prof
            self.target_state, self.resolve_result, self.target_error = L.TG_UNCHECKED, None, ""
            self.picked = None
            if self.client is not None:
                self.client.held_hwnd = 0
        self._remember(chk.name)
        self._emit("target", self.target_view_info())
        if self.active:
            self.resolve_async()
        return prof

    # ---- 配達（配達スレッドから）
    def _delivery_options(self) -> dict:
        c = self.cfg
        return {"existing_text": c["existing_text"], "keep_idle_foreground": bool(c["keep_idle_foreground"]),
                "return_focus": bool(c["return_focus"]), "quiet_input_sec": float(c["quiet_input_sec"]),
                "send": bool(c["send_after_deliver"]), "wait_while_busy": bool(c["wait_while_busy"]),
                "busy_wait_sec": float(c["busy_wait_sec"]),
                "force_on_mismatch": bool(c.get("force_on_mismatch", False))}

    def _deliver(self, text: str) -> dict:
        """DialogueSession の deliver_callback。失敗・例外はすべて未配達の結果として返す。"""
        if self.record_only:
            # 宛先「なし（記録だけ）」: 入力欄には何もしない・クリップボードにも入れない。配達済み（記録）として扱う
            return {"status": "delivered", "stage": "record", "sent": ""}
        tf = self.text_transform
        if tf is not None:
            try:
                text = tf(text) or text
            except Exception:
                pass
        if self.sending_prefix:
            text = L.compose_with_prefix(self.sending_prefix, text)
        prof = self.profile
        if not prof:
            return {"status": "undelivered", "stage": "", "reason": "no_target"}
        try:
            self._emit("delivering", {"target": dp.profile_label(prof)})
            client = self._client()
            _t0 = time.monotonic()
            res = client.deliver(prof, text, options=self._delivery_options())
            try:
                r = res if isinstance(res, dict) else {}
                dlog("deliver_result", status=r.get("status"), stage=r.get("stage"), reason=r.get("reason"),
                     sent=r.get("sent"), send_stage=r.get("send_stage"), chars=len(text),
                     wall_ms=int((time.monotonic() - _t0) * 1000), timings=r.get("timings"), calls=r.get("calls"))
            except Exception:
                pass
            return res
        except Exception as e:
            dlog("deliver_result", status="undelivered", reason=f"ワーカーとの通信に失敗: {type(e).__name__}: {e}",
                 chars=len(text))
            return {"status": "undelivered", "stage": "", "reason": f"ワーカーとの通信に失敗: {type(e).__name__}: {e}"}

    def clear_sending_prefix(self) -> None:
        """送っているメッセージ（未配達）を破棄したとき: その前置きも捨てる。"""
        self.sending_prefix = ""

    def _on_session_event(self, name: str, info: dict) -> None:
        if name == "commit_request" and info.get("mode") == "commit":
            # 新規の確定: 挿入されていた前置きは、このメッセージのものになる（以後の再試行にも付く）。画面は info["prefix"] を見る
            self.sending_prefix, self.prefix_text = self.prefix_text, ""
            info = dict(info, prefix=self.sending_prefix)
        elif name == "delivered":
            info = dict(info, prefix=self.sending_prefix)
            self.sending_prefix = ""
        if name in ("delivered", "undelivered", "commit_request", "ended", "idle_stop", "recognition_error"):
            dlog("session_event", name=name, mode=info.get("mode"), ok=info.get("delivered"), reason=info.get("reason"),
                 sent=info.get("sent"))
        self._emit(name, info)
        if name == "ended" and not self._stopping and not self._closed:
            # 無音の自動終了など、利用者の操作によらずセッションが終わった: ヘッドセット・ワーカーを止めて終了の確認へ
            threading.Thread(target=self.stop, name="kabeuchi-dialogue-autostop", daemon=True).start()

    # ---- セッション
    def start(self, wav_path=None) -> Path:
        """セッションを始める（録音・認識・ヘッドセット受付）。窓の確認とワーカーの起動は裏で。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("閉じています")
            if self.active:
                raise RuntimeError("すでにセッション中です")
            old = self.ds.buffer if self.ds is not None else None
            carry = old if (old is not None and (old.has_undelivered or old.has_draft)) else None   # 持ち越し
            ds = self._session_factory(self.s, self._rec_session, self._recognizer, self._deliver,
                                       replacer=self._replacer, buffer=carry,
                                       on_event=self._on_session_event, on_status=self._on_status)
            wav = ds.start(wav_path, live_enabled=False)
            self.ds = ds
        atexit.register(self.shutdown)    # 異常終了でもヘッドセット・ワーカーを残さない（何度呼んでも安全）
        self.last_end_report = None
        c = self.cfg
        dlog("session_start", headset_button=bool(c.get("headset_button")), mouse_button=str(c.get("mouse_button") or ""), click_commit=bool(c.get("click_commit")), send_after_deliver=bool(c.get("send_after_deliver")),
             target=bool(self.profile), carried=bool(carry))
        self._start_headset()
        self._start_mouse()
        self._start_click(ds)
        if self.profile and not self.record_only:
            self.resolve_async()
        return wav

    def _start_headset(self) -> None:
        if not self.cfg.get("headset_button"):
            dlog("headset_skipped", why="設定 headset_button が OFF")
            return
        try:
            if self.headset is None:
                if self._headset_factory is not None:
                    self.headset = self._headset_factory(self._on_headset)
                else:
                    from dialogue_headset import HeadsetButton
                    self.headset = HeadsetButton(self._on_headset, mode=self.cfg.get("headset_mode", "auto"),
                                                 vid_pid=self.cfg.get("headset_vid_pid", "3511:2EF2"))
            res = self.headset.start()
            self.headset_reason = "" if res.ok else res.reason
            if not res.ok:
                self._emit("headset_failed", {"reason": res.reason})
        except Exception as e:
            self.headset_reason = f"{type(e).__name__}: {e}"
            dlog("headset_start_error", error=self.headset_reason)
            self._emit("headset_failed", {"reason": self.headset_reason})

    def _start_mouse(self) -> None:
        """マウスのサイドボタン（dialogue.mouse_button）。ヘッドセットとは独立。空なら何も起動しない。失敗しても例外は出さない。"""
        from dialogue_mouse import normalize_button
        button = normalize_button(self.cfg.get("mouse_button"))
        self.mouse_reason = ""
        if not button:
            return
        try:
            if self.mouse is None or getattr(self.mouse, "button", button) != button:
                if self._mouse_factory is not None:
                    self.mouse = self._mouse_factory(self._on_mouse, button)
                else:
                    from dialogue_mouse import MouseButton
                    self.mouse = MouseButton(self._on_mouse, button)
            res = self.mouse.start()
            self.mouse_reason = "" if res.ok else res.reason
            if not res.ok:
                self._emit("mouse_failed", {"reason": res.reason})
        except Exception as e:
            self.mouse_reason = f"{type(e).__name__}: {e}"
            dlog("mouse_start_error", via="mouse", error=self.mouse_reason)
            self._emit("mouse_failed", {"reason": self.mouse_reason})

    def _on_mouse(self) -> None:       # マウスフックの別スレッドから。軽くして戻る
        dlog("mouse_callback", via="mouse", active=self.active)
        self.commit("mouse")

    def mouse_state(self) -> str:
        m = self.mouse
        return m.state if m is not None else "stopped"

    def mouse_diagnostics(self) -> dict:
        """マウス経路の状態（MouseButton.diagnostics）。まだ作っていなければ停止扱い。"""
        m = self.mouse
        try:
            d = dict(m.diagnostics()) if m is not None and hasattr(m, "diagnostics") else {}
        except Exception:
            d = {}
        d.setdefault("state", self.mouse_state())
        d.setdefault("reason", self.mouse_reason)
        return d

    def _stop_mouse(self) -> None:
        m = self.mouse
        if m is not None:
            try:
                m.stop()
            except Exception:
                pass

    def _start_click(self, ds=None) -> None:
        """舌打ち2回で確定（dialogue.click_commit・実験）。設定が OFF なら何も作らない。失敗しても例外は出さず他の経路に影響しない。"""
        self.click_reason = ""
        if not self.cfg.get("click_commit"):
            return
        try:
            if self.click is None:
                if self._click_factory is not None:
                    self.click = self._click_factory(self._on_click)
                else:
                    from dialogue_click import ClickCommit
                    self.click = ClickCommit(self._on_click, enabled=True)
            res = self.click.start()
            self.click_reason = "" if res.ok else res.reason
            if res.ok:
                if ds is None:
                    ds = self.ds
                if ds is not None:
                    ds.audio_tap = self._click_feed
            else:
                self._emit("click_failed", {"reason": res.reason})
        except Exception as e:
            self.click_reason = f"{type(e).__name__}: {e}"
            dlog("click_start_error", via="click", error=self.click_reason)
            self._emit("click_failed", {"reason": self.click_reason})

    def _click_feed(self, x, drop=None) -> None:     # 録音スレッドから（セッションの音声の入口）。軽く・例外を出さない
        c = self.click
        if c is None:
            return
        try:
            c.feed(x, drop)
        except Exception:
            pass

    def _on_click(self) -> None:       # 検出の別スレッドから。軽くして戻る
        dlog("click_callback", via="click", active=self.active)
        self.commit("click")

    def click_state(self) -> str:
        c = self.click
        return c.state if c is not None else "stopped"

    def click_diagnostics(self) -> dict:
        """舌打ち経路の状態（ClickCommit.diagnostics）。まだ作っていなければ停止扱い。"""
        c = self.click
        try:
            d = dict(c.diagnostics()) if c is not None and hasattr(c, "diagnostics") else {}
        except Exception:
            d = {}
        d.setdefault("state", self.click_state())
        d.setdefault("reason", self.click_reason)
        return d

    def _stop_click(self) -> None:
        c = self.click
        ds = self.ds
        if ds is not None:
            try:
                ds.audio_tap = None
            except Exception:
                pass
        if c is not None:
            try:
                c.stop()
            except Exception:
                pass

    def apply_headset_setting(self) -> None:
        """設定で headset_button が変わった: セッション中なら、ヘッドセットの独占をいま登録・解除する（セッション外は何もしない）。"""
        if not self.active:
            return
        if self.cfg.get("headset_button"):
            if self.headset_state() != "active":
                self._start_headset()
        else:
            self._stop_headset()
            self.headset_reason = ""

    def _on_headset(self) -> None:     # ヘッドセットのスレッドから。軽くして戻る
        dlog("headset_callback", active=self.active)
        self._emit("headset_press", {})
        self.commit("headset")

    def headset_state(self) -> str:
        h = self.headset
        return h.state if h is not None else "stopped"

    def headset_diagnostics(self) -> dict:
        """独占の状態（HeadsetButton.diagnostics）。まだ作っていなければ停止扱い。"""
        h = self.headset
        try:
            d = dict(h.diagnostics()) if h is not None and hasattr(h, "diagnostics") else {}
        except Exception:
            d = {}
        d.setdefault("state", self.headset_state())
        d.setdefault("reason", self.headset_reason)
        return d

    def commit(self, source: str = "window") -> str:
        """確定操作（窓のボタン / ヘッドセット / ホットキー）。何になったか（press_action）を返す。"""
        snap = self.snapshot()
        act = L.press_action(snap)
        # 押したことの通知を先に出す（結果の通知「届けました」はそのあとに来る。順序が逆だと結果が上書きされる）
        self._emit("pressed", {"source": source, "action": act})
        if act in (L.PRESS_COMMIT, L.PRESS_RETRY):
            if source in ("headset", "hotkey", "mouse", "click"):
                # 入力を受けた直後に、ワーカーへ前面化の権利を渡す（配達の直前にもう一度渡される）
                try:
                    (self._grant or _default_grant)()
                except Exception:
                    pass
            if not self.ds.commit():
                act = L.PRESS_IGNORE
        dlog("commit_call", source=source, action=act, active=bool(snap.get("active")),
             undelivered=snap.get("undelivered_count"), delivering=snap.get("delivering"),
             draft_chars=len(snap.get("draft_text") or ""), speaking=snap.get("speaking"),
             pending=snap.get("pending_recognitions"))
        return act

    def hold(self, on: bool | None = None) -> bool:
        if self.ds is None or not self.active:
            return False
        if on is None:
            return self.ds.toggle_hold()
        self.ds.hold(on)
        return self.ds.is_held

    def ops(self) -> L.UndeliveredOps:
        return L.UndeliveredOps(self.ds)

    def stop(self, wait: float = 60.0) -> dict:
        """セッションを終える。清書が終わっていない発話は、清書が済む（最大 wait 秒）のを待って下書きへ入れてから戻る。
        ヘッドセットの独占とワーカーは、どの経路でも止める。戻りは end_report。"""
        with self._lock:
            if self._stopping:
                return self.last_end_report or {}
            self._stopping = True
        ds = self.ds
        report: dict = {}
        try:
            try:
                if ds is not None:
                    report = ds.stop(wait=wait)
            finally:
                self._stop_headset()
                self._stop_mouse()
                self._stop_click()
                self._stop_client()
            self.last_end_report = report
            self._emit("stopped", {"report": report})
            return report
        finally:
            self._stopping = False

    def _stop_headset(self) -> None:
        h = self.headset
        if h is not None:
            try:
                h.stop()
            except Exception:
                pass

    def _stop_client(self, timeout: float | None = None) -> None:
        """ワーカーを止める。shutdown 済みのクライアントは再利用できないので捨てる（次は作り直す）。
        クリック待ち（pick）の最中は shutdown が待たされるので、timeout があればそこで諦めて殺す。"""
        with self._lock:
            c, self.client = self.client, None
        if c is None:
            return

        def run():
            try:
                c.shutdown()
            except Exception:
                pass
        t = threading.Thread(target=run, name="kabeuchi-dialogue-client-stop", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            try:
                c._proc.kill()          # 応答待ちで固まっている場合の最後の手段
            except Exception:
                pass

    def shutdown(self) -> None:
        """アプリ終了・窓を閉じるときの後始末（何度呼んでも安全）。"""
        self._closed = True
        try:
            if self.ds is not None and self.ds.is_active:
                self.ds.close()
        except Exception:
            pass
        self._stop_headset()
        self._stop_mouse()
        self._stop_click()
        self._stop_client(timeout=3.0)


def _default_grant():
    from dialogue_client import allow_foreground
    return allow_foreground()
