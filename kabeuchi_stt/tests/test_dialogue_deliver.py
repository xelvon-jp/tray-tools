# -*- coding: utf-8 -*-
"""AI対話モード: 配達の段階制御（フェイクの ops で全分岐）・プロトコル・ワーカーの純部分。
実機（Win32 / UIA / 入力送信 / ワーカーの起動）には触らない。
"""
import io
import json
import subprocess
import sys
import unittest

import _common as C
import dialogue_deliver as dd
import dialogue_worker as dw

PROFILE = {"process_name": "app.exe", "window_class": "K", "window_title_contains": "",
           "input_name": "入力", "input_control_type": "ComboBox", "click_rel": [50, 400],
           "window_size": [800, 600]}
ROOT = 100
OTHER = 999


class FakeOps:
    """宛先の窓・入力欄・前面・クリップボードを持つ偽物。キー送信とクリックは、呼ばれた瞬間の前面を記録する。"""

    def __init__(self, **kw):
        self.root = ROOT
        self.fg = OTHER
        self.iconic = False
        self.find_status = "ok"
        self.input_found = True
        self.text = ""                   # 入力欄の中身
        self.read_ok = True
        self.writable = True             # SetValue が成功と返るか
        self.uia_effective = True        # SetValue が実際に入力欄へ入るか（False は Copilot 型）
        self.uia_garbage = False         # SetValue が壊れた内容を残す
        self.restore_works = True
        self.focus_matches = True
        self.front_works = set()         # 前面になる前面化の手段
        self.point_root = ROOT           # 点の下の最上位の窓
        self.click_brings_front = True
        self.click_ok = True
        self.paste_effective = True
        self.paste_mismatch = False
        self.clip_restorable = True
        self.steal_on_clipboard_set = False
        self.last_input = None
        self.elem_rect = (10, 400, 790, 580)
        self.clip = "元のクリップボード"
        self.calls = []
        self.keys = []                   # (種類, 呼ばれた瞬間の前面, mode)
        self.flashed = []
        self.raise_on = set()
        self.__dict__.update(kw)

    def _c(self, name, *a):
        self.calls.append((name,) + a)
        if name in self.raise_on:
            raise RuntimeError(f"{name} が落ちた")

    def names(self):
        return [c[0] for c in self.calls]

    # --- 窓・入力欄 ---
    def find_window(self, profile, hwnd=0):
        self._c("find_window")
        if self.find_status == "ok":
            return {"status": "ok", "hwnd": self.root, "is_iconic": self.iconic, "rect": (0, 0, 800, 600)}
        return {"status": self.find_status, "hwnd": 0, "candidates": [{"hwnd": 1}, {"hwnd": 2}]}

    def wake(self, hwnd):
        self._c("wake")
        return {}

    def find_input(self, hwnd, profile):
        self._c("find_input")
        if not self.input_found:
            return {"found": False, "error": "見つからない", "candidates": []}
        return {"found": True, "how": "name", "element": {"rect": self.elem_rect}}

    def read_value(self, hwnd, profile):
        self._c("read_value")
        if not self.read_ok:
            return {"ok": False, "text": None}
        return {"ok": True, "text": self.text}

    def write_value(self, hwnd, profile, text):
        self._c("write_value", text)
        if not self.writable:
            return {"ok": False, "reason": "unwritable"}
        if self.uia_garbage and "こんにちは" in text:
            self.text = text + "??"
        elif self.uia_effective and (self.restore_works or text):
            self.text = text
        return {"ok": True}

    def set_focus(self, hwnd, profile):
        self._c("set_focus")
        return {"ok": True, "focused_matches": self.focus_matches}

    # --- 前面 ---
    def foreground_root(self):
        return self.fg

    def wait_foreground(self, hwnd, timeout):
        self._c("wait_foreground")
        return self.fg == hwnd

    def bring_to_front(self, hwnd, profile, method):
        self._c("bring_to_front", hwnd, method)
        if hwnd == self.root and method in self.front_works:
            self.fg = self.root
        elif hwnd != self.root:
            self.fg = hwnd
        return {}

    def root_of_point(self, x, y):
        self._c("root_of_point", x, y)
        return self.point_root

    def click(self, x, y, expected_root):
        self.keys.append(("click", self.fg, None))
        self._c("click", x, y)
        if not self.click_ok:
            return {"clicked": False, "reason": "PointMismatch"}
        if self.click_brings_front:
            self.fg = self.root
        return {"clicked": True}

    def last_input_ms(self):
        self._c("last_input_ms")
        return self.last_input

    def flash(self, hwnd):
        self.flashed.append(hwnd)

    # --- クリップボードと貼り付け ---
    def clipboard_snapshot(self):
        self._c("clipboard_snapshot")
        return {"restorable": self.clip_restorable, "formats": ["CF_DIB"], "saved": self.clip}

    def clipboard_set_text(self, text):
        self._c("clipboard_set_text", text)
        self.clip_after = text
        if self.steal_on_clipboard_set:
            self.fg = OTHER
        return True

    def clipboard_restore(self, snap):
        self._c("clipboard_restore")
        self.clip = snap["saved"]
        self.clip_after = None
        return True

    def paste(self, expected_root, mode):
        self.keys.append(("paste", self.fg, mode))
        self._c("paste", mode)
        if self.fg != expected_root:
            return {"sent": False, "reason": "前面が違う"}
        if self.paste_effective:
            t = self.clip_after
            if self.paste_mismatch:
                t = t + "余計"
            self.text = t if mode == "replace" else self.text + t
        return {"sent": True}


def run(ops, text="こんにちは", options=None, profile=None):
    return dd.deliver(ops, profile or PROFILE, text, options=options, sleep=lambda s: None)


class StageTest(unittest.TestCase):
    def assert_keys_only_when_foreground(self, ops):
        for kind, fg, _m in ops.keys:
            if kind == "paste":
                self.assertEqual(fg, ops.root, "前面確認の前に貼り付けが呼ばれた")

    def test_uia_success_touches_nothing_else(self):
        ops = FakeOps()
        r = run(ops)
        self.assertEqual((r["status"], r["stage"], r["readback"]), ("delivered", "uia", "ok"))
        self.assertEqual(ops.text, "こんにちは")
        for n in ("bring_to_front", "paste", "click", "clipboard_set_text", "set_focus"):
            self.assertNotIn(n, ops.names())
        self.assertEqual(ops.fg, OTHER)             # 背面のまま
        self.assertIn("total", r["timings"])

    def test_uia_ineffective_then_front_and_paste(self):
        """Copilot 型: SetValue は成功と返るが入らない。読み戻しの一致だけを成功とする。"""
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"})
        r = run(ops)
        self.assertEqual((r["status"], r["stage"]), ("delivered", "paste"))
        self.assertEqual(ops.text, "こんにちは")
        self.assertEqual(ops.keys, [("paste", ROOT, "insert")])
        self.assertEqual(ops.clip, "元のクリップボード")      # 復元された
        self.assertTrue(r["clipboard_restored"])
        self.assertEqual([c for c in ops.calls if c[0] == "bring_to_front"], [("bring_to_front", ROOT, "uia_set_focus")])

    def test_second_front_method_used_when_first_fails(self):
        ops = FakeOps(uia_effective=False, front_works={"set_foreground"})
        r = run(ops)
        self.assertEqual(r["status"], "delivered")
        methods = [c[2] for c in ops.calls if c[0] == "bring_to_front"]
        self.assertEqual(methods, ["uia_set_focus", "show_restore_then_set", "set_foreground"])
        self.assert_keys_only_when_foreground(ops)

    def test_minimized_window_tries_restore_first(self):
        ops = FakeOps(uia_effective=False, iconic=True, front_works={"show_restore_then_set"})
        run(ops)
        methods = [c[2] for c in ops.calls if c[0] == "bring_to_front"]
        self.assertEqual(methods[0], "show_restore_then_set")
        self.assertEqual(len(methods), 1)

    def test_already_foreground_skips_front(self):
        ops = FakeOps(uia_effective=False, fg=ROOT)
        r = run(ops)
        self.assertEqual((r["status"], r["stage"]), ("delivered", "paste"))
        self.assertNotIn("bring_to_front", ops.names())

    def test_front_impossible_uses_click_then_paste(self):
        ops = FakeOps(uia_effective=False)
        r = run(ops)
        self.assertEqual((r["status"], r["stage"]), ("delivered", "click_paste"))
        kinds = [k[0] for k in ops.keys]
        self.assertEqual(kinds, ["click", "paste"])
        self.assertEqual(ops.keys[1][1], ROOT)        # 貼り付けはクリックで前面になった後
        self.assertIn(("click", 400, 490), ops.calls)  # 入力欄の矩形の中心

    def test_click_point_covered_by_other_window_means_no_click(self):
        ops = FakeOps(uia_effective=False, point_root=OTHER)
        r = run(ops)
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_COVERED))
        self.assertEqual(ops.keys, [])
        self.assertEqual(ops.flashed, [ROOT])          # 点滅で知らせる

    def test_click_disabled(self):
        ops = FakeOps(uia_effective=False)
        r = run(ops, options={"allow_click": False})
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_FG_FAILED))
        self.assertEqual(ops.keys, [])

    def test_click_does_not_bring_front_then_no_paste(self):
        ops = FakeOps(uia_effective=False, click_brings_front=False)
        r = run(ops)
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_FG_FAILED))
        self.assertEqual([k[0] for k in ops.keys], ["click"])     # 貼り付けは呼ばれない

    def test_click_failed(self):
        ops = FakeOps(uia_effective=False, click_ok=False)
        r = run(ops)
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_CLICK_FAILED))
        self.assertNotIn("paste", [k[0] for k in ops.keys])

    def test_focus_not_confirmed_falls_to_click(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"}, focus_matches=False)
        r = run(ops)
        self.assertEqual((r["status"], r["stage"]), ("delivered", "click_paste"))
        self.assertEqual([k[0] for k in ops.keys], ["click", "paste"])

    def test_focus_not_confirmed_and_no_click_is_undelivered(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"}, focus_matches=False)
        r = run(ops, options={"allow_click": False})
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_NO_FOCUS))
        self.assertEqual(ops.keys, [])

    def test_foreground_lost_just_before_paste_sends_nothing(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"}, steal_on_clipboard_set=True)
        r = run(ops)
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_FG_LOST))
        self.assertNotIn("paste", ops.names())
        self.assertEqual(ops.clip, "元のクリップボード")       # 途中で止めても復元する

    def test_paste_readback_mismatch(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"}, paste_mismatch=True)
        r = run(ops)
        self.assertEqual((r["status"], r["reason"], r["readback"]), ("undelivered", dd.R_MISMATCH, "mismatch"))
        self.assertTrue(r["keys_sent"])       # キーは送った（入力欄に残っているかもしれない）ことを伝える

    def test_paste_has_no_effect(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"}, paste_effective=False)
        r = run(ops)
        self.assertEqual((r["status"], r["reason"], r["readback"]), ("undelivered", dd.R_MISMATCH, "empty"))

    def test_clipboard_not_restorable_gives_up_pasting(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"}, clip_restorable=False)
        r = run(ops)
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_CLIP_NOT_RESTORABLE))
        self.assertNotIn("clipboard_set_text", ops.names())
        self.assertEqual(ops.keys, [])
        r2 = run(FakeOps(uia_effective=False, front_works={"uia_set_focus"}, clip_restorable=False),
                 options={"allow_unrestorable_clipboard": True})
        self.assertEqual(r2["status"], "delivered")

    def test_everything_impossible_is_undelivered_with_flash(self):
        ops = FakeOps(uia_effective=False, writable=False, focus_matches=False, point_root=OTHER)
        r = run(ops)
        self.assertEqual(r["status"], "undelivered")
        self.assertEqual(ops.keys, [])
        self.assertEqual(ops.flashed, [ROOT])

    def test_all_scenarios_never_key_before_foreground(self):
        scenarios = [dict(uia_effective=False, front_works={"uia_set_focus"}),
                     dict(uia_effective=False),
                     dict(uia_effective=False, front_works={"set_foreground"}, focus_matches=False),
                     dict(uia_effective=False, click_brings_front=False),
                     dict(uia_effective=False, front_works={"uia_set_focus"}, steal_on_clipboard_set=True),
                     dict(uia_effective=False, fg=ROOT)]
        for kw in scenarios:
            ops = FakeOps(**kw)
            run(ops)
            self.assert_keys_only_when_foreground(ops)

    def test_ops_exceptions_are_caught(self):
        r = run(FakeOps(raise_on={"find_window"}))
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_OPS_ERROR))
        ops = FakeOps(raise_on={"write_value"}, front_works={"uia_set_focus"})
        r = run(ops)                                   # UIA 書き込みが落ちたら次の段階（前面化＋貼り付け）へ
        self.assertEqual((r["status"], r["stage"]), ("delivered", "paste"))
        r = run(FakeOps(uia_effective=False, raise_on={"bring_to_front", "click"}))
        self.assertEqual(r["status"], "undelivered")


class WindowTest(unittest.TestCase):
    def test_window_not_found_and_ambiguous_touch_nothing(self):
        for status, reason in (("none", dd.R_NOT_FOUND), ("ambiguous", dd.R_AMBIGUOUS)):
            ops = FakeOps(find_status=status)
            r = run(ops)
            self.assertEqual((r["status"], r["reason"]), ("undelivered", reason))
            self.assertEqual(ops.names(), ["find_window"])      # 以降は何も呼ばない
            self.assertEqual(ops.keys, [])
            self.assertEqual(ops.flashed, [])

    def test_input_not_found(self):
        ops = FakeOps(input_found=False)
        r = run(ops)
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_NO_INPUT))
        self.assertNotIn("write_value", ops.names())
        self.assertEqual(ops.keys, [])

    def test_empty_text(self):
        ops = FakeOps()
        self.assertEqual(run(ops, text="  \n")["reason"], dd.R_EMPTY)
        self.assertEqual(ops.calls, [])


class ExistingTextTest(unittest.TestCase):
    def test_append_via_uia(self):
        ops = FakeOps(text="書きかけ")
        r = run(ops)
        self.assertEqual(r["status"], "delivered")
        self.assertEqual(ops.text, "書きかけ\nこんにちは")

    def test_append_keeps_trailing_newline(self):
        ops = FakeOps(text="書きかけ\n")
        run(ops)
        self.assertEqual(ops.text, "書きかけ\nこんにちは")

    def test_placeholder_value_is_not_treated_as_a_draft(self):
        """空の入力欄がプレースホルダー（「一行メッセージ」）を値として返すアプリ。empty_values にあれば空として扱う。"""
        prof = dict(PROFILE, empty_values=["一行メッセージ"])
        ops = FakeOps(text="一行メッセージ")
        r = run(ops, profile=prof)
        self.assertEqual(r["status"], "delivered")
        self.assertEqual(ops.text, "こんにちは")                       # 本文の前に説明文を入れない
        ops = FakeOps(text="一行メッセージ")
        run(ops)                                                        # 登録が無ければ、書きかけとして残す（従来どおり）
        self.assertEqual(ops.text, "一行メッセージ\nこんにちは")

    def test_overwrite_via_uia(self):
        ops = FakeOps(text="書きかけ")
        r = run(ops, options={"existing_text": "overwrite"})
        self.assertEqual(r["status"], "delivered")
        self.assertEqual(ops.text, "こんにちは")

    def test_abort_touches_nothing(self):
        ops = FakeOps(text="書きかけ")
        r = run(ops, options={"existing_text": "abort"})
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_ABORT))
        self.assertNotIn("write_value", ops.names())
        self.assertEqual(ops.text, "書きかけ")
        self.assertEqual(ops.keys, [])

    def test_unreadable_existing_blocks_unless_overwrite(self):
        ops = FakeOps(read_ok=False)
        r = run(ops)
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_UNREADABLE))
        self.assertNotIn("write_value", ops.names())

    def test_append_via_paste_uses_append_mode(self):
        ops = FakeOps(text="書きかけ", uia_effective=False, front_works={"uia_set_focus"})
        r = run(ops)
        self.assertEqual((r["status"], r["stage"]), ("delivered", "paste"))
        self.assertEqual(ops.keys, [("paste", ROOT, "append")])
        self.assertEqual(ops.text, "書きかけ\nこんにちは")

    def test_overwrite_via_paste_uses_replace_mode(self):
        ops = FakeOps(text="書きかけ", uia_effective=False, front_works={"uia_set_focus"})
        r = run(ops, options={"existing_text": "overwrite"})
        self.assertEqual(r["status"], "delivered")
        self.assertEqual(ops.keys, [("paste", ROOT, "replace")])
        self.assertEqual(ops.text, "こんにちは")

    def test_garbage_from_uia_is_restored_before_paste(self):
        ops = FakeOps(text="書きかけ", uia_garbage=True, front_works={"uia_set_focus"})
        r = run(ops)
        # 壊れた内容（書いた文字列＋ゴミ）を元へ戻してから、追記モードで貼り付ける
        self.assertIn(("write_value", "書きかけ"), ops.calls)
        self.assertEqual((r["status"], r["stage"]), ("delivered", "paste"))

    def test_dirty_input_that_cannot_be_restored_stops(self):
        class Stuck(FakeOps):
            def write_value(self, hwnd, profile, text):
                self._c("write_value", text)
                if text.endswith("こんにちは"):
                    self.text = text + "??"
                return {"ok": True}                 # 元へ戻す書き込みは効かない
        ops = Stuck(text="書きかけ", front_works={"uia_set_focus"})
        r = run(ops)
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_DIRTY))
        self.assertEqual(ops.keys, [])

    def test_plan_existing(self):
        p = dd.plan_existing("", "T", "abort")
        self.assertEqual((p["action"], p["final"], p["mode"]), ("write", "T", "insert"))
        p = dd.plan_existing("  \n", "T", "append")
        self.assertEqual(p["mode"], "insert")
        p = dd.plan_existing("A", "T", "append")
        self.assertEqual((p["final"], p["payload"], p["mode"]), ("A\nT", "\nT", "append"))
        p = dd.plan_existing("A\n", "T", "append")
        self.assertEqual((p["final"], p["payload"]), ("A\nT", "T"))
        self.assertEqual(dd.plan_existing("A", "T", "abort")["action"], "abort")
        self.assertEqual(dd.plan_existing("A", "T", "overwrite")["mode"], "replace")
        self.assertEqual(dd.plan_existing(None, "T", "append")["action"], "abort")
        self.assertEqual(dd.plan_existing(None, "T", "overwrite")["action"], "write")


class PoliciesTest(unittest.TestCase):
    def test_keep_idle_foreground_waits_then_gives_up(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"}, last_input=100)
        r = run(ops, options={"keep_idle_foreground": True, "quiet_wait_sec": 1.0})
        self.assertEqual((r["status"], r["reason"]), ("undelivered", dd.R_USER_ACTIVE))
        self.assertNotIn("bring_to_front", ops.names())
        self.assertEqual(ops.keys, [])

    def test_keep_idle_foreground_proceeds_when_quiet(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"}, last_input=60000)
        r = run(ops, options={"keep_idle_foreground": True})
        self.assertEqual(r["status"], "delivered")

    def test_keep_idle_default_off_ignores_input(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"}, last_input=10)
        self.assertEqual(run(ops)["status"], "delivered")
        self.assertNotIn("last_input_ms", ops.names())

    def test_return_focus(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"})
        r = run(ops, options={"return_focus": True})
        self.assertEqual(r["status"], "delivered")
        self.assertTrue(r["returned_focus"])
        self.assertEqual(ops.fg, OTHER)
        self.assertEqual(ops.calls[-1][:3], ("bring_to_front", OTHER, "set_foreground"))

    def test_return_focus_default_off(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"})
        run(ops)
        self.assertEqual(ops.fg, ROOT)

    def test_merge_options(self):
        o = dd.merge_options({"existing_text": "bogus", "nope": 1, "quiet_input_sec": None})
        self.assertEqual(o["existing_text"], "append")
        self.assertNotIn("nope", o)
        self.assertEqual(o["quiet_input_sec"], dd.DEFAULT_OPTIONS["quiet_input_sec"])

    def test_click_point(self):
        self.assertEqual(dd.click_point((0, 0, 100, 50), None, {}), (50, 25))
        self.assertEqual(dd.click_point((0, 0, 0, 0), (10, 20, 500, 500), {"click_rel": [5, 6]}), (15, 26))
        self.assertIsNone(dd.click_point(None, None, {}))

    def test_result_is_json_serializable(self):
        r = run(FakeOps(uia_effective=False, front_works={"uia_set_focus"}))
        json.dumps(r)


class ProtocolTest(unittest.TestCase):
    def test_roundtrip_japanese_single_line(self):
        raw = dd.encode_line({"id": 1, "text": "こんにちは\n二行目"})
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(dd.decode_line(raw)["text"], "こんにちは\n二行目")

    def test_decode_errors(self):
        for bad in (b"", b"\n", b"not json", b"[1,2]", b"\xff\xfe", "42"):
            with self.assertRaises(ValueError):
                dd.decode_line(bad)


class WorkerPureTest(unittest.TestCase):
    def test_import_has_no_side_effects_and_no_forbidden_modules(self):
        code = ("import sys, io\n"
                "buf = io.StringIO(); sys.stdout = buf\n"
                "import dialogue_worker, dialogue_client\n"
                "sys.stdout = sys.__stdout__\n"
                "bad = [m for m in ('PySide6','numpy','faster_whisper','settings','hotkeys','comtypes',"
                "'sounddevice') if m in sys.modules]\n"
                "print('BAD', bad, 'OUT', repr(buf.getvalue()))\n")
        r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=str(C.ROOT), capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("BAD [] OUT ''", r.stdout)

    def test_worker_source_has_no_forbidden_imports(self):
        src = (C.ROOT / "dialogue_worker.py").read_text(encoding="utf-8")
        for name in ("PySide6", "numpy", "faster_whisper", "import settings", "import hotkeys",
                     "from settings", "from hotkeys"):
            self.assertNotIn(name, src.replace("PySide6・numpy・faster-whisper", ""), name)
        self.assertNotIn("cast(", src)             # COM ポインタの型変換は QueryInterface で行う

    def test_classify_clipboard(self):
        c = dw.classify_clipboard([13, 1, 7, 16])                        # テキスト系だけ
        self.assertEqual(c["unrestorable"], [])
        self.assertEqual(c["save"], [13, 1, 7, 16])
        c = dw.classify_clipboard([2, 8, 17])                            # 画像: BITMAP は DIB から作り直せる
        self.assertEqual((c["save"], c["skip"], c["unrestorable"]), ([8, 17], [2], []))
        c = dw.classify_clipboard([2])                                   # BITMAP だけ → 戻せない
        self.assertEqual(c["unrestorable"], [2])
        c = dw.classify_clipboard([13, 14])                              # 拡張メタファイル
        self.assertEqual(c["unrestorable"], [14])
        c = dw.classify_clipboard([13, 0x0201, 0x0301, 0x0080])          # 私的形式・GDI・OWNERDISPLAY
        self.assertEqual(c["unrestorable"], [0x0201, 0x0301, 0x0080])
        c = dw.classify_clipboard([13, 0xC013, 15])                      # 登録形式・HDROP は保存できる
        self.assertEqual(c["save"], [13, 0xC013, 15])

    def test_service_dispatch_and_errors(self):
        svc = dw.WorkerService(FakeOps())
        r = svc.handle({"id": 5, "op": "ping"})
        self.assertTrue(r["ok"] and r["result"]["pong"])
        self.assertEqual(r["id"], 5)
        self.assertFalse(svc.handle({"id": 6, "op": "format_c"})["ok"])
        r = svc.handle({"id": 7, "op": "deliver", "profile": PROFILE, "text": "やあ"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["status"], "delivered")
        json.dumps(r)
        self.assertFalse(svc.stop)
        self.assertEqual(svc.handle({"id": 8, "op": "shutdown"})["result"], {"bye": True})
        self.assertTrue(svc.stop)

    def test_service_survives_ops_exception(self):
        class Bad(FakeOps):
            def pick(self, args):
                raise RuntimeError("COM が落ちた")
        r = dw.WorkerService(Bad()).handle({"id": 1, "op": "pick"})
        self.assertFalse(r["ok"])
        self.assertIn("COM", r["error"])

    def test_serve_loop_with_bad_lines(self):
        stdin = io.BytesIO(b'{"id":1,"op":"ping"}\n\nnot json\n{"id":2,"op":"shutdown"}\n{"id":3,"op":"ping"}\n')
        out = io.BytesIO()
        dw.serve(dw.WorkerService(FakeOps()), stdin, out)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([x.get("id") for x in lines], [1, None, 2])    # shutdown の後は読まない
        self.assertFalse(lines[1]["ok"])
        for raw in out.getvalue().splitlines():
            json.loads(raw.decode("utf-8"))                              # 応答以外を書いていない

    def test_serve_ends_on_eof(self):
        out = io.BytesIO()
        self.assertEqual(dw.serve(dw.WorkerService(FakeOps()), io.BytesIO(b""), out), 0)
        self.assertEqual(out.getvalue(), b"")



class PlaceholderAfterSendTest(unittest.TestCase):
    """送信後の空の入力欄が、プレースホルダー（入力欄の名前など）を値として返すアプリでも、送信できたと判定する。"""

    def test_sent_is_confirmed_when_empty_field_returns_its_name(self):
        class Ops:
            def __init__(self): self.text, self.sent = None, False
            def find_window(self, p, h): return {"status": "ok", "hwnd": 1, "rect": (0, 0, 100, 100)}
            def foreground_root(self): return 1
            def wake(self, r): return {}
            def find_input(self, r, p): return {"found": True, "element": {"rect": (0, 0, 10, 10)}, "how": "x"}
            def read_value(self, r, p):
                if self.sent:
                    return {"ok": True, "text": "メッセージ"}
                return {"ok": True, "text": self.text if self.text is not None else "メッセージ"}
            def write_value(self, r, p, t): self.text = t; return {"ok": True}
            def find_button(self, *a): return {"found": True, "enabled": True, "name": "送信", "rect": (0, 0, 5, 5)}
            def invoke_button(self, *a): self.sent = True; return {"invoked": True}
        res = dd.deliver(Ops(), {"input_name": "メッセージ"}, "こんにちは",
                         options={"send": True, "wait_while_busy": False}, sleep=lambda s: None)
        self.assertEqual((res["status"], res.get("sent")), ("delivered", True))

if __name__ == "__main__":
    unittest.main()
