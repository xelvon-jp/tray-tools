# -*- coding: utf-8 -*-
"""置換辞書・履歴/検索・プリセット・タイムライン・閲覧ロジックの単体テスト。"""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import _common as C
from history import History, first_sentence, snippet
from presets import SEED_PRESETS, build_prompt, load_presets, save_presets
from replacements import Replacer, load_pairs, save_pairs, suggest_replacements
from timeline import DRAFT, FINAL, WAVE, Timeline
from viewer_core import block_for_time, seek_time_for_block


def R(*pairs):
    return Replacer([{"from": a, "to": b} for a, b in pairs])


class ReplacerTest(unittest.TestCase):
    def test_basic(self):
        r = R(("一時解析", "一次解析"), ("不線", "付箋"))
        self.assertEqual(r.apply("一時解析の不線を貼る"), "一次解析の付箋を貼る")

    def test_longest_wins_on_overlap(self):
        r = R(("解析", "かいせき"), ("一時解析", "一次解析"))
        self.assertEqual(r.apply("一時解析と解析"), "一次解析とかいせき")
        # 登録順を逆にしても同じ
        r = R(("一時解析", "一次解析"), ("解析", "かいせき"))
        self.assertEqual(r.apply("一時解析と解析"), "一次解析とかいせき")

    def test_single_pass_no_chaining(self):
        r = R(("a", "b"), ("b", "c"))
        self.assertEqual(r.apply("ab"), "bc")

    def test_output_containing_key_is_not_reprocessed(self):
        # 置換結果の中に誤認識語が含まれていても、1パスなので再置換されず停止する
        r = R(("RAG", "RAG検索"))
        self.assertEqual(r.apply("RAGとRAG"), "RAG検索とRAG検索")
        r2 = R(("あ", "あい"), ("い", "いあ"))
        self.assertEqual(r2.apply("あい"), "あいいあ")

    def test_duplicates_last_wins_and_empty_ignored(self):
        r = R(("x", "1"), ("x", "2"), ("", "zzz"))
        self.assertEqual(r.apply("xx"), "22")
        self.assertEqual(len(r), 1)

    def test_regex_chars_are_literal(self):
        r = R(("a.b", "X"), ("(", "["))
        self.assertEqual(r.apply("a.b axb ("), "X axb [")

    def test_empty_inputs(self):
        self.assertEqual(R().apply("あ"), "あ")
        self.assertEqual(R(("a", "b")).apply(""), "")

    def test_delete_by_empty_replacement(self):
        self.assertEqual(R(("えー", "")).apply("えー、これは"), "、これは")

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            pairs = load_pairs(p)  # 無ければ見本を作る
            self.assertTrue(any(x["from"] == "不線" for x in pairs))
            save_pairs([{"from": "あ", "to": "い"}, {"from": "", "to": "x"}], p)
            self.assertEqual(load_pairs(p), [{"from": "あ", "to": "い"}])
            self.assertNotIn(b"\r", p.read_bytes())  # LF
            p.write_text("{壊れた", encoding="utf-8")
            self.assertEqual(load_pairs(p), [])

    def test_suggest(self):
        self.assertEqual(suggest_replacements("一時解析をする", "一次解析をする"), [("時", "次")])
        s = suggest_replacements("不線を貼る", "付箋を貼る")
        self.assertEqual(s, [("不線", "付箋")])
        # 挿入・削除のみは候補にしない／同一は空
        self.assertEqual(suggest_replacements("あいう", "あいうえ"), [])
        self.assertEqual(suggest_replacements("あいう", "あいう"), [])
        # 複数行は行ごとに比較
        self.assertEqual(suggest_replacements("壁内\nテスト", "壁打ち\nテスト"), [("内", "打ち")])


class PresetTest(unittest.TestCase):
    def test_seed_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.json"
            ps = load_presets(p)
            self.assertEqual([x["name"] for x in ps], [x["name"] for x in SEED_PRESETS])
            ps.append({"name": "新規", "prompt": "本文"})
            ps.append({"name": "  ", "prompt": "空名は捨てる"})
            save_presets(ps, p)
            again = load_presets(p)
            self.assertEqual(again[-1], {"name": "新規", "prompt": "本文"})
            self.assertEqual(len(again), len(SEED_PRESETS) + 1)
            self.assertNotIn(b"\r", p.read_bytes())

    def test_build_prompt(self):
        t = build_prompt("要約して", "今日は\n壁打ち")
        self.assertTrue(t.startswith("要約して\n\n"))
        self.assertIn("今日は\n壁打ち", t)
        self.assertTrue(t.index("要約して") < t.index("今日は"))
        self.assertTrue(t.endswith("\n"))
        # プロンプトが空なら文字起こしだけ
        self.assertEqual(build_prompt("", "本文"), "本文\n")
        # 前後の空白は整える
        self.assertEqual(build_prompt("  P  ", " T \n"), build_prompt("P", "T"))


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.h = History(Path(self._d.name) / "h.db")

    def tearDown(self):
        self.h.close()
        self._d.cleanup()

    def add(self, text, **kw):
        return self.h.add(text=text, segments=[(0.0, 3.0, text)], duration=3.0, model="small", **kw)

    def test_first_sentence(self):
        self.assertEqual(first_sentence("今日は壁打ちです。次に仕事。"), "今日は壁打ちです。")
        self.assertEqual(first_sentence("句点なし" * 20, 10), "句点なし" * 2 + "句点" + "…")
        self.assertEqual(first_sentence(""), "")

    def test_add_get_list_order(self):
        a = self.add("一件目。", created_at="2026-11-01T10:00:00")
        b = self.add("二件目。", created_at="2026-11-02T10:00:00")
        r = self.h.get(a)
        self.assertEqual((r.title, r.model, r.duration), ("一件目。", "small", 3.0))
        self.assertEqual(r.segments, [(0.0, 3.0, "一件目。")])
        self.assertEqual([x.id for x in self.h.list()], [b, a])  # 新しい順

    def test_search_and_or_case(self):
        self.add("基地局の評価をAIで自動化する。RAGを使う。")
        self.add("夕飯はカレーにする。")
        self.add("RAGの話だけ。")
        self.assertEqual(len(self.h.search("RAG")), 2)
        self.assertEqual(len(self.h.search("rag 基地局")), 1)      # AND・大文字小文字無視
        self.assertEqual(len(self.h.search("カレー 基地局")), 0)
        self.assertEqual(len(self.h.search("")), 3)                # 空=全件
        self.assertEqual(len(self.h.search("　")), 3)              # 全角空白のみ
        self.assertEqual(self.h.search("存在しない"), [])

    def test_search_covers_edited_text(self):
        i = self.add("不線を貼る。")
        self.h.save_edit(i, "付箋を貼る。")
        self.assertEqual(len(self.h.search("付箋")), 1)
        self.assertEqual(len(self.h.search("不線")), 1)  # 元の清書も検索対象のまま
        r = self.h.get(i)
        self.assertEqual(r.text, "不線を貼る。")           # 元の清書は残る
        self.assertEqual(r.current_text, "付箋を貼る。")
        self.assertEqual(r.title, "付箋を貼る。")
        self.h.revert_edit(i)
        self.assertEqual(self.h.get(i).current_text, "不線を貼る。")

    def test_update_final_resets_edit(self):
        i = self.add("古い。")
        self.h.save_edit(i, "直した。")
        self.h.update_final(i, text="新しい清書。", segments=[(0, 1, "新しい清書。")], duration=1.0, model="medium")
        r = self.h.get(i)
        self.assertEqual((r.text, r.edited_text, r.model), ("新しい清書。", None, "medium"))

    def test_find_by_wav(self):
        i = self.add("x。", wav_path="C:/a/b.wav")
        self.assertEqual(self.h.find_by_wav("C:/a/b.wav").id, i)
        self.assertIsNone(self.h.find_by_wav("C:/none.wav"))

    def test_delete_uses_trasher_and_keeps_files_on_failure(self):
        d = Path(self._d.name)
        files = [d / "a.wav", d / "a.txt", d / "a.md"]
        for f in files:
            f.write_bytes(b"x")
        i = self.add("消す。", wav_path=files[0], txt_path=files[1], md_path=files[2])
        trashed = []

        def trasher(p):
            if p.suffix == ".md":
                raise OSError("fail")
            trashed.append(p.name)
            p.unlink()
        failed = self.h.delete(i, trasher=trasher)
        self.assertEqual(trashed, ["a.wav", "a.txt"])
        self.assertEqual(failed, [str(files[2])])
        self.assertTrue(files[2].exists())
        self.assertIsNone(self.h.get(i))

    def test_delete_without_files(self):
        i = self.add("消す。", wav_path="C:/nonexistent.wav")
        self.assertEqual(self.h.delete(i, trasher=lambda p: self.fail("呼ばれない")), [])

    def test_snippet(self):
        t = "あ" * 50 + "基地局" + "い" * 50
        s = snippet(t, "基地局", 5)
        self.assertEqual(s, "…" + "あ" * 5 + "基地局" + "い" * 5 + "…")
        self.assertEqual(snippet("短い文", "ない"), "短い文")

    def test_real_recycle_bin_function_on_missing_file_is_noop(self):
        from history import send_to_recycle_bin
        send_to_recycle_bin(Path(self._d.name) / "nothing.wav")  # 存在しなければ何もしない


class TimelineTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(Timeline().rows(), [])

    def test_wave_rows_split_by_30s(self):
        t = Timeline(30)
        t.feed(C.sine(65, -12))
        rows = t.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r.state for r in rows], [WAVE] * 3)
        self.assertEqual(rows[0].label, "00:00–00:30")
        self.assertEqual(rows[2].label, "01:00–01:05")
        self.assertEqual(len(rows[0].peaks), 60)
        self.assertTrue(all(p > 0.5 for p in rows[0].peaks))

    def test_waveform_levels_follow_loudness(self):
        t = Timeline(30)
        t.feed(np.concatenate([C.sine(1, -10), np.zeros(C.SR, dtype=np.int16)]))
        p = t.rows()[0].peaks
        self.assertGreater(p[0], 0.8)
        self.assertEqual(p[-1], 0.0)

    def test_state_progression_wave_draft_final(self):
        t = Timeline(30)
        t.set_recording(True)
        t.feed(C.sine(70, -12))
        t.add_draft(5, 9, "最初の仮認識")
        t.add_draft(40, 44, "二行目")
        rows = t.rows()
        self.assertEqual([r.state for r in rows], [DRAFT, DRAFT, WAVE])
        self.assertEqual(rows[0].text, "最初の仮認識")
        self.assertTrue(rows[2].live)
        self.assertFalse(rows[0].live)
        t.set_recording(False)
        v = t.version
        t.set_final([(4.0, 10.0, "最初の清書。"), (31.0, 35.0, "二行目の清書。")])
        self.assertGreater(t.version, v)
        rows = t.rows()
        self.assertEqual([r.state for r in rows], [FINAL] * 3)  # 仮認識は清書で置き換わる
        self.assertEqual(rows[0].text, "最初の清書。")
        self.assertEqual(rows[1].text, "二行目の清書。")
        self.assertEqual(rows[2].text, "")
        self.assertEqual(t.final_text(), "最初の清書。\n二行目の清書。")

    def test_empty_draft_ignored_and_clear(self):
        t = Timeline()
        t.add_draft(0, 1, "  ")
        t.feed(C.sine(1, -10))
        self.assertEqual(t.rows()[0].state, WAVE)
        t.clear()
        self.assertEqual(t.rows(), [])
        self.assertFalse(t.is_final)

    def test_odd_block_sizes(self):
        t = Timeline()
        x = C.sine(3, -12)
        for i in range(0, len(x), 777):
            t.feed(x[i:i + 777])
        self.assertAlmostEqual(t.duration, 3.0, delta=0.5)

    def test_final_only_timeline_from_wav(self):
        # 履歴からの表示: 波形なしで清書だけ
        t = Timeline(30)
        t.set_final([(0, 5, "あ"), (35, 40, "い")], duration=45)
        self.assertEqual([r.text for r in t.rows()], ["あ", "い"])


class ViewerCoreTest(unittest.TestCase):
    SEGS = [(0.0, 4.0, "a"), (4.5, 9.0, "b"), (12.0, 15.0, "c")]

    def test_seek_exact(self):
        self.assertEqual(seek_time_for_block(self.SEGS, 1, 3), 4.5)

    def test_seek_proportional_when_edited(self):
        self.assertAlmostEqual(seek_time_for_block(self.SEGS, 1, 2, 30.0), 15.0)
        self.assertAlmostEqual(seek_time_for_block(self.SEGS, 4, 5), 15.0 * 4 / 5)

    def test_seek_invalid(self):
        self.assertIsNone(seek_time_for_block([], 0, 0))
        self.assertIsNone(seek_time_for_block([], 0, 3))
        self.assertIsNone(seek_time_for_block(self.SEGS, -1, 3))

    def test_block_for_time(self):
        self.assertEqual(block_for_time(self.SEGS, 5.0), 1)
        self.assertEqual(block_for_time(self.SEGS, 11.0), 1)
        self.assertEqual(block_for_time(self.SEGS, 99), 2)
        self.assertIsNone(block_for_time([(2.0, 3.0, "x")], 1.0))


if __name__ == "__main__":
    unittest.main()
