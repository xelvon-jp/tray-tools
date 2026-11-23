# -*- coding: utf-8 -*-
"""AI対話モード: 宛先プロファイル・プリセット・入力欄の選択・窓の絞り込み・読み戻し判定（純関数）。
Win32 / UIA には触らない。
"""
import datetime
import os
import re
import tempfile
import unittest
from pathlib import Path

import _common as C  # noqa: F401  （import パスと一時データ置き場）
import dialogue_profile as dp

ROOT = Path(__file__).resolve().parent.parent


def el(index, ctype="Edit", name="", aid="", cls="", rect=(0, 0, 0, 0), has_value=None):
    return {"index": index, "control_type": ctype, "name": name, "automation_id": aid,
            "class_name": cls, "rect": rect, "has_value": has_value}


def win(hwnd, exe="app.exe", cls="Chrome_WidgetWin_1", title="chat - App", iconic=False):
    return {"hwnd": hwnd, "root": hwnd, "exe": exe, "class": cls, "title": title, "is_iconic": iconic}


class BuildProfileTest(unittest.TestCase):
    def test_title_hint(self):
        self.assertEqual(dp.title_hint("会話A - アプリ"), "会話A")
        self.assertEqual(dp.title_hint("アプリ"), "")
        self.assertEqual(dp.title_hint(""), "")
        self.assertEqual(dp.title_hint("x | y - z"), "x")

    def test_build_profile(self):
        w = {"exe": "app.exe", "class": "Cls", "title": "会話A - アプリ", "rect": (100, 200, 1100, 900),
             "render_class": "Chrome_RenderWidgetHostHWND"}
        e = {"control_type": "ComboBox", "name": "入力", "automation_id": "ed", "class_name": "edit",
             "chain": [{"control_type": "Pane", "name": "p"}, {"control_type": "Window", "name": "w"}]}
        p = dp.build_profile(w, e, (150, 260), "宛先1", now=datetime.datetime(2026, 1, 2, 3, 4, 5))
        self.assertEqual(p["schema"], dp.SCHEMA)
        self.assertEqual(p["process_name"], "app.exe")
        self.assertEqual(p["window_title_contains"], "会話A")
        self.assertEqual(p["click_rel"], [50, 60])
        self.assertEqual(p["window_size"], [1000, 700])
        self.assertEqual([c["control_type"] for c in p["input_chain"]], ["Window", "Pane", "ComboBox"])
        self.assertEqual(p["captured_at"], "2026-01-02T03:04:05")

    def test_build_profile_tolerates_missing(self):
        p = dp.build_profile(None, None, None, "x")
        self.assertEqual(p["process_name"], "")
        self.assertEqual(p["click_rel"], [0, 0])


class PresetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dlg_presets_")
        self.prof = dp.build_profile({"exe": "a.exe", "class": "K", "title": "t - A", "rect": (0, 0, 10, 10)},
                                     {"name": "n"}, (1, 1), "宛先 A")

    def test_roundtrip_list_delete(self):
        path = dp.save_preset(self.prof, self.tmp)
        self.assertTrue(path.endswith(".json"))
        self.assertTrue(os.path.isdir(dp.targets_dir(self.tmp)))
        self.assertEqual(dp.load_preset("宛先 A", self.tmp)["process_name"], "a.exe")
        self.assertTrue(dp.preset_exists("宛先 A", self.tmp))
        lst = dp.list_presets(self.tmp)
        self.assertEqual([p["name"] for _p, p in lst], ["宛先 A"])
        self.assertTrue(dp.delete_preset("宛先 A", self.tmp))
        self.assertFalse(dp.delete_preset("宛先 A", self.tmp))
        self.assertEqual(dp.list_presets(self.tmp), [])

    def test_no_silent_overwrite_and_lf(self):
        path = dp.save_preset(self.prof, self.tmp)
        with self.assertRaises(FileExistsError):
            dp.save_preset(self.prof, self.tmp)
        dp.save_preset({**self.prof, "process_name": "b.exe"}, self.tmp, overwrite=True)
        self.assertEqual(dp.load_target(path)["process_name"], "b.exe")
        raw = Path(path).read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_bad_files_skipped_and_schema(self):
        d = dp.targets_dir(self.tmp)
        os.makedirs(d)
        Path(d, "broken.json").write_text("{not json", encoding="utf-8")
        Path(d, "other.json").write_text('{"schema": "x"}', encoding="utf-8")
        Path(d, "old.json").write_text('{"schema": "stage0-v1", "name": "old"}', encoding="utf-8")
        Path(d, "memo.txt").write_text("x", encoding="utf-8")
        self.assertEqual([p["name"] for _p, p in dp.list_presets(self.tmp)], ["old"])   # 段階0の形式は読める
        with self.assertRaises(ValueError):
            dp.load_target(os.path.join(d, "broken.json"))
        with self.assertRaises(ValueError):
            dp.load_target(os.path.join(d, "other.json"))

    def test_sanitize_name(self):
        self.assertEqual(dp.sanitize_name("a/b:c"), "a_b_c")
        self.assertEqual(dp.sanitize_name("  "), "target")
        self.assertEqual(dp.sanitize_name("CON"), "_CON")
        self.assertEqual(dp.sanitize_name("日本語 名"), "日本語 名")
        self.assertLessEqual(len(dp.sanitize_name("x" * 200)), 80)

    def test_default_base_uses_env_and_does_not_create(self):
        self.assertEqual(dp.default_base_dir(), os.environ["KABEUCHI_DATA_DIR"])
        self.assertTrue(dp.targets_dir().endswith(dp.TARGETS_SUBDIR))
        self.assertEqual(dp.list_presets(tempfile.mkdtemp(prefix="dlg_empty_")), [])   # 無いフォルダでも落ちない


class SelectWindowTest(unittest.TestCase):
    profile = {"process_name": "app.exe", "window_class": "Chrome_WidgetWin_1",
               "window_title_contains": "", "captured_title": "chat - App"}

    def test_match_rules(self):
        self.assertTrue(dp.match_window(win(1, exe="APP.EXE"), self.profile))
        self.assertFalse(dp.match_window(win(1, exe="x.exe"), self.profile))
        self.assertFalse(dp.match_window(win(1), {}))     # 条件が空は何にも一致しない
        self.assertFalse(dp.match_window(win(1, title="abc"), {**self.profile, "window_title_contains": "zzz"}))
        self.assertTrue(dp.match_window(win(1, title="ABC def"), {**self.profile, "window_title_contains": "abc"}))

    def test_unique(self):
        r = dp.select_window([win(1), win(2, exe="other.exe")], self.profile)
        self.assertEqual((r["status"], r["hwnd"]), ("ok", 1))

    def test_none(self):
        r = dp.select_window([win(2, exe="other.exe")], self.profile)
        self.assertEqual((r["status"], r["hwnd"]), ("none", 0))
        self.assertEqual(dp.select_window([], self.profile)["status"], "none")

    def test_ambiguous_never_picks(self):
        r = dp.select_window([win(1, title="a"), win(2, title="b")], self.profile)
        self.assertEqual((r["status"], r["hwnd"]), ("ambiguous", 0))
        self.assertEqual(len(r["candidates"]), 2)

    def test_held_wins_if_still_matching(self):
        r = dp.select_window([win(1, title="a"), win(2, title="b")], self.profile, held_hwnd=2)
        self.assertEqual((r["status"], r["hwnd"], r["reason"]), ("ok", 2, "held"))

    def test_held_that_no_longer_matches_is_not_used(self):
        r = dp.select_window([win(1)], self.profile, held_hwnd=9)
        self.assertEqual((r["status"], r["hwnd"]), ("ok", 1))
        r = dp.select_window([win(2, exe="other.exe")], self.profile, held_hwnd=2)
        self.assertEqual(r["status"], "none")

    def test_title_exact_breaks_tie_only_if_single(self):
        r = dp.select_window([win(1, title="chat - App"), win(2, title="other - App")], self.profile)
        self.assertEqual((r["status"], r["hwnd"], r["reason"]), ("ok", 1, "title_exact"))
        r = dp.select_window([win(1, title="chat - App"), win(2, title="chat - App")], self.profile)
        self.assertEqual(r["status"], "ambiguous")

    def test_children_collapse_to_root(self):
        a = {**win(10), "root": 1}
        b = {**win(11), "root": 1}
        r = dp.select_window([a, b], self.profile)
        self.assertEqual((r["status"], r["hwnd"]), ("ok", 1))


class ChooseInputTest(unittest.TestCase):
    def test_automation_id_first(self):
        infos = [el(0, name="メッセージ", rect=(0, 0, 10, 10)),
                 el(1, aid="editor", rect=(0, 0, 10, 10))]
        idx, how, _n = dp.choose_input(infos, {"input_automation_id": "editor", "input_name": "メッセージ"})
        self.assertEqual((idx, how), (1, "automation_id"))

    def test_name_with_type_then_class_then_rect(self):
        infos = [el(0, "Button", name="入力", rect=(0, 0, 5, 5)), el(1, "ComboBox", name="入力", rect=(0, 0, 5, 5))]
        self.assertEqual(dp.choose_input(infos, {"input_name": "入力", "input_control_type": "ComboBox"})[:2],
                         (1, "name"))
        infos = [el(0, "ComboBox", cls="tiptap", rect=(0, 0, 5, 5))]
        self.assertEqual(dp.choose_input(infos, {"input_control_type": "ComboBox", "input_class_name": "tiptap"})[:2],
                         (0, "control_type+class"))
        infos = [el(0, "Document", rect=(0, 0, 500, 500), has_value=True),
                 el(1, "Edit", rect=(10, 10, 60, 40), has_value=True)]
        idx, how, _n = dp.choose_input(infos, {"click_rel": [20, 20], "window_size": [500, 500]},
                                       win_rect=(0, 0, 500, 500))
        self.assertEqual((idx, how), (1, "rect"))
        idx, how, _n = dp.choose_input(infos, {"click_rel": [20, 20], "window_size": [400, 400]},
                                       win_rect=(0, 0, 500, 500))
        self.assertEqual(how, "rect(size_changed)")

    def test_prefers_visible(self):
        infos = [el(0, aid="a", rect=(0, 0, 0, 0)), el(1, aid="a", rect=(0, 0, 9, 9))]
        self.assertEqual(dp.choose_input(infos, {"input_automation_id": "a"})[0], 1)

    def test_not_found(self):
        idx, how, note = dp.choose_input([el(0, name="x")], {"input_name": "y"})
        self.assertIsNone(idx)
        self.assertTrue(note)


class DescendTest(unittest.TestCase):
    def test_button_wrapping_combobox(self):
        """Muse型: クリックで当たったのは入力欄を包むボタン。中の ComboBox（値パターンあり）へ降りる。"""
        infos = [el(0, "Button", name="包み", rect=(0, 0, 300, 100), has_value=False),
                 el(1, "ComboBox", name="メッセージ", rect=(10, 10, 290, 60), has_value=True)]
        r = dp.resolve_input(infos, {"input_name": "包み", "input_control_type": "Button"})
        self.assertEqual((r["index"], r["descended"], r["parent_index"]), (1, True, 0))
        self.assertTrue(r["how"].endswith("+descend"))

    def test_document_without_value_descends_to_smallest_at_click_point(self):
        infos = [el(0, "Document", name="全体", rect=(0, 0, 800, 600), has_value=False),
                 el(1, "ComboBox", name="入力", rect=(10, 500, 700, 580), has_value=True),
                 el(2, "Edit", name="検索", rect=(600, 10, 790, 40), has_value=True),
                 el(3, "Edit", name="小さい", rect=(20, 510, 60, 530), has_value=True)]
        prof = {"input_name": "全体", "input_control_type": "Document", "click_rel": [300, 540],
                "window_size": [800, 600]}
        r = dp.resolve_input(infos, prof, win_rect=(0, 0, 800, 600))
        self.assertEqual(r["index"], 1)        # クリック点を含むものだけに絞った上で最小（3 は点を含まない）

    def test_value_pattern_element_not_descended(self):
        infos = [el(0, "ComboBox", name="プロンプト", rect=(0, 0, 100, 50), has_value=True),
                 el(1, "Edit", rect=(5, 5, 20, 20), has_value=True)]
        r = dp.resolve_input(infos, {"input_name": "プロンプト"})
        self.assertEqual((r["index"], r["descended"]), (0, False))

    def test_no_target_inside_returns_the_hit_with_note(self):
        infos = [el(0, "Button", name="b", rect=(0, 0, 100, 50), has_value=False),
                 el(1, "Edit", rect=(200, 200, 300, 250), has_value=True)]     # 矩形の外
        r = dp.resolve_input(infos, {"input_name": "b"})
        self.assertEqual((r["index"], r["descended"]), (0, False))
        self.assertTrue(r["note"])

    def test_descend_input_ignores_invisible_and_valueless(self):
        infos = [el(1, "Edit", rect=(0, 0, 0, 0), has_value=True),
                 el(2, "Edit", rect=(1, 1, 5, 5), has_value=False),
                 el(3, "Button", rect=(1, 1, 5, 5), has_value=True)]
        self.assertIsNone(dp.descend_input(infos, (0, 0, 10, 10)))
        self.assertIsNone(dp.descend_input(infos, None))

    def test_not_found_passthrough(self):
        r = dp.resolve_input([el(0, name="x")], {"input_name": "y"})
        self.assertIsNone(r["index"])


class ReadbackTest(unittest.TestCase):
    def test_equivalent(self):
        self.assertTrue(dp.texts_equivalent("a\r\nb  \n", "\n a\nb"))
        self.assertTrue(dp.texts_equivalent("abc", "abc\n\n"))
        self.assertFalse(dp.texts_equivalent("abc", "abd"))

    def test_judge(self):
        self.assertEqual(dp.judge_readback("x", None), "unreadable")
        self.assertEqual(dp.judge_readback("x", " \n"), "empty")
        self.assertEqual(dp.judge_readback("x\ny", "x\r\ny "), "ok")
        self.assertEqual(dp.judge_readback("x", "xy"), "mismatch")

    def test_cleared(self):
        self.assertEqual(dp.judge_cleared(None), "unreadable")
        self.assertEqual(dp.judge_cleared("  "), "ok")
        self.assertEqual(dp.judge_cleared("a"), "remain")

    def test_expected_point(self):
        pt, changed = dp.expected_point({"click_rel": [10, 20], "window_size": [100, 100]}, (50, 60, 150, 160))
        self.assertEqual((pt, changed), ((60, 80), False))
        _pt, changed = dp.expected_point({"click_rel": [10, 20], "window_size": [100, 100]}, (50, 60, 250, 160))
        self.assertTrue(changed)
        pt, changed = dp.expected_point({}, (5, 6, 9, 9))
        self.assertEqual((pt, changed), ((5, 6), False))


class PublicRepoGuardTest(unittest.TestCase):
    """公開リポジトリに入るコード: 実パス・メールアドレスを書かない。"""

    def test_dialogue_sources_have_no_personal_paths(self):
        bad_path = re.compile(r"[A-Za-z]:\\Us" + r"ers\\|/Us" + "ers/|" + "yo" + "tan")
        email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
        files = sorted(ROOT.glob("dialogue_*.py")) + sorted((ROOT / "tests").glob("test_dialogue_*.py"))
        self.assertGreaterEqual(len(files), 4)
        for f in files:
            text = f.read_text(encoding="utf-8")
            self.assertIsNone(bad_path.search(text), f.name)
            self.assertIsNone(email.search(text), f.name)
            self.assertNotIn("\r\n", text, f.name + " は LF")


if __name__ == "__main__":
    unittest.main()
