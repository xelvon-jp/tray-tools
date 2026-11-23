# -*- coding: utf-8 -*-
"""下書きの文章づくり（句点・改行の規則、確定時の句点補い）と、タイムラインの対話モード用の更新（純ロジック）。"""
import unittest

import _common as C  # noqa: F401
from dialogue_text import EDGE_PAD_SEC, compose_draft, finalize_text, utterance_gap
from timeline import DRAFT, FINAL, Timeline, WAVE


def E(text, start, end, edited=False):
    return {"text": text, "start_sec": start, "end_sec": end, "edited": edited}


def gap_entries(first, second, raw_gap):
    """直前の発話の終了から raw_gap 秒あとに次が始まる 2 つの発話。"""
    return [E(first, 0.0, 2.0), E(second, 2.0 + raw_gap, 4.0 + raw_gap)]


class GapRuleTest(unittest.TestCase):
    """間（= 記録上のすき間 + 区切り器の preroll/tail の 0.6 秒）が 2.0 / 3.0 秒の境界。"""
    G, F = 2.0, 3.0

    def c(self, entries):
        return compose_draft(entries, self.G, self.F)

    def test_short_gap_just_joins_without_adding_period(self):
        self.assertEqual(self.c(gap_entries("今日は", "晴れです。", 0.3)), "今日は晴れです。")     # 間 0.9 秒

    def test_period_gap_newlines_only_after_sentence_end(self):
        # 間 = 1.5 + 0.6 = 2.1 秒（2.0 以上、3.0 未満）
        self.assertEqual(self.c(gap_entries("終わりました。", "次です。", 1.5)), "終わりました。\n次です。")
        self.assertEqual(self.c(gap_entries("考え中で", "続きです。", 1.5)), "考え中で続きです。")    # 句点なしは改行しない

    def test_exactly_at_boundaries(self):
        raw_period = self.G - EDGE_PAD_SEC            # 間がちょうど 2.0
        self.assertEqual(self.c(gap_entries("おわり。", "つぎ。", raw_period)), "おわり。\nつぎ。")
        self.assertEqual(self.c(gap_entries("おわり。", "つぎ。", raw_period - 0.05)), "おわり。つぎ。")
        raw_force = self.F - EDGE_PAD_SEC             # 間がちょうど 3.0
        self.assertEqual(self.c(gap_entries("途中", "つぎ。", raw_force)), "途中。\nつぎ。")
        self.assertEqual(self.c(gap_entries("途中", "つぎ。", raw_force - 0.05)), "途中つぎ。")

    def test_force_gap_adds_period_or_replaces_comma(self):
        self.assertEqual(self.c(gap_entries("途中で止まった", "次です。", 5.0)), "途中で止まった。\n次です。")
        self.assertEqual(self.c(gap_entries("えっと、", "次です。", 5.0)), "えっと。\n次です。")
        self.assertEqual(self.c(gap_entries("すごい！", "次です。", 5.0)), "すごい！\n次です。")      # 既に句点系
        self.assertEqual(self.c(gap_entries("いいの？", "次です。", 5.0)), "いいの？\n次です。")
        self.assertEqual(self.c(gap_entries("Really?", "next", 5.0)), "Really?\nnext")
        self.assertEqual(self.c(gap_entries("「はい。」", "次", 5.0)), "「はい。」\n次")            # 閉じ括弧のあとでも文末
        self.assertEqual(self.c(gap_entries("ok", "次", 5.0)), "ok。\n次")                         # 英語終わり

    def test_adjacent_utterances_have_no_gap(self):
        """最長での強制の区切り（すき間 0）は、無音を挟んでいないので連結のまま。"""
        es = [E("前半", 0.0, 30.0), E("後半です。", 30.0, 33.0)]
        self.assertEqual(utterance_gap(es[0], es[1]), 0.0)
        self.assertEqual(self.c(es), "前半後半です。")

    def test_no_times_falls_back_to_plain_join(self):
        es = [{"text": "use"}, {"text": "python"}, {"text": "です"}]
        self.assertEqual(self.c(es), "use pythonです")
        self.assertEqual(compose_draft(es), "use pythonです")        # 規則なし（gap_sec=None）も同じ

    def test_rules_disabled(self):
        self.assertEqual(compose_draft(gap_entries("途中", "つぎ。", 9.0), None, None), "途中つぎ。")

    def test_edited_text_is_not_rewritten_only_newline_after(self):
        """手修正した下書きには規則を再適用しない（「。」を補わない）。直したあとの追記分との境目だけ改行を入れる。"""
        es = [E("直した文章", 0.0, 2.0, edited=True), E("次です。", 10.0, 12.0)]
        self.assertEqual(self.c(es), "直した文章\n次です。")
        es2 = [E("直した文章", 0.0, 2.0, edited=True), E("次です。", 2.2, 4.0)]
        self.assertEqual(self.c(es2), "直した文章次です。")
        # 直したあとの追記どうしには規則が効く
        es3 = es + [E("さらに", 20.0, 21.0)]
        self.assertEqual(self.c(es3), "直した文章\n次です。\nさらに")


class FinalizeTest(unittest.TestCase):
    def test_adds_period_when_missing(self):
        self.assertEqual(finalize_text("お願いします"), "お願いします。")
        self.assertEqual(finalize_text("どうしよう、"), "どうしよう。")
        self.assertEqual(finalize_text("English end"), "English end。")

    def test_keeps_when_already_terminated(self):
        for t in ("できた。", "本当？", "すごい！", "ok.", "「はい。」", "えーと…", "A, B,"[:-1] + "."):
            self.assertEqual(finalize_text(t), t)

    def test_emoji_and_symbols_are_left_alone(self):
        for t in ("ありがとう😊", "やった🎉", "ok👍", "これ→", "a;"):
            self.assertEqual(finalize_text(t), t)

    def test_off_and_empty(self):
        self.assertEqual(finalize_text("そのまま", punctuate=False), "そのまま")
        self.assertEqual(finalize_text("  "), "")
        self.assertEqual(finalize_text("  末尾の空白  "), "末尾の空白。")


class TimelineDialogueTest(unittest.TestCase):
    def rows(self, tl):
        import numpy as np
        if tl.duration < 10:
            tl.feed(np.zeros(16000 * 10, dtype=np.int16))      # 行（30 秒の枠）ができるだけの音声を流しておく
        return [r for r in tl.rows() if r.kind == "audio"]

    def test_provisional_replaced_by_final_and_committed_turns_white(self):
        tl = Timeline(30.0)
        tl.set_draft(1.0, 3.0, "かりにんしき")                 # 仮認識（オレンジ）
        self.assertEqual(self.rows(tl)[0].state, DRAFT)
        tl.set_draft(1.0, 3.0, "清書です")                     # 同じ ID（開始秒）で清書に置き換え
        r = self.rows(tl)[0]
        self.assertEqual((r.state, r.text), (DRAFT, "清書です"))
        self.assertEqual(len(tl._drafts), 1)
        tl.commit_drafts()
        r = self.rows(tl)[0]
        self.assertEqual((r.state, r.tag), (FINAL, "確定"))
        tl.set_draft(1.0, 3.0, "清書版が遅れて届いた")           # 確定済みの行の清書への置き換えでも白のまま
        self.assertEqual(self.rows(tl)[0].state, FINAL)

    def test_drop_uncommitted_keeps_committed(self):
        tl = Timeline(30.0)
        tl.set_draft(1.0, 2.0, "確定")
        tl.commit_drafts()
        tl.set_draft(5.0, 6.0, "確定前")
        tl.drop_uncommitted()
        self.assertEqual([d[2] for d in tl._drafts], ["確定"])
        tl.set_draft(5.0, 6.0, "")                            # 空は取り除く
        tl.remove_draft(1.0)
        self.assertEqual(tl._drafts, [])

    def test_pending_tag_and_normal_mode_unchanged(self):
        tl = Timeline(30.0)
        tl.add_pending(2.0, 4.0)
        self.assertEqual((self.rows(tl)[0].state, self.rows(tl)[0].tag), (WAVE, "認識中"))
        tl.remove_pending(2.0)
        self.assertEqual(self.rows(tl)[0].tag, "")
        t2 = Timeline(30.0)
        t2.add_draft(1.0, 2.0, "壁打ちの仮認識")                # 従来の経路は確定にならない
        self.assertEqual(self.rows(t2)[0].state, DRAFT)

    def test_drafts_stay_sorted_by_start(self):
        tl = Timeline(30.0)
        tl.set_draft(10.0, 11.0, "あと")
        tl.set_draft(2.0, 3.0, "さき")
        self.assertEqual([d[2] for d in tl._drafts], ["さき", "あと"])


class SentenceNewlineTest(unittest.TestCase):
    """句点系の直後の改行（文中）と、無音で区切った所の改行2つ（空行）。"""

    def test_break_sentences_inside_text(self):
        from dialogue_text import break_sentences as bs
        self.assertEqual(bs("今日は晴れです。明日は雨です。"), "今日は晴れです。\n明日は雨です。")       # 文末には足さない
        self.assertEqual(bs("本当？すごい！やった。"), "本当？\nすごい！\nやった。")
        self.assertEqual(bs("えっと、そうですね"), "えっと、そうですね")                               # 句点なし
        self.assertEqual(bs("はい。。そうです"), "はい。。\nそうです")                                  # 連続する句点は1かたまり
        self.assertEqual(bs("本当？！そう"), "本当？！\nそう")
        self.assertEqual(bs("「はい。」と言った。次です"), "「はい。」\nと言った。\n次です")          # 閉じかっこの直後
        self.assertEqual(bs("円周率は３．１４です。次"), "円周率は３．１４です。\n次")                # 数字の小数点は対象外
        self.assertEqual(bs("pi is 3.14. next"), "pi is 3.14. next")                                   # 英語の「.」は対象外
        self.assertEqual(bs("あ。 い"), "あ。\nい")                                                    # 句点のあとの空白は読み飛ばす
        self.assertEqual(bs("あ。\nい"), "あ。\nい")                                                  # すでに改行があれば足さない

    def entries(self, *items):
        return [{"text": t, "start_sec": s, "end_sec": e} for t, s, e in items]

    def test_compose_newline_after_sentence_only_when_enabled(self):
        e = self.entries(("あ。い。", 0.0, 2.0))
        self.assertEqual(compose_draft(e), "あ。い。")
        self.assertEqual(compose_draft(e, newline_after_sentence=True), "あ。\nい。")

    def test_silence_split_after_sentence_is_a_blank_line(self):
        e = self.entries(("一つ目です。", 0.0, 2.0), ("二つ目です。", 4.0, 6.0))      # 無音で区切った（間 2.0＋0.6 秒）
        out = compose_draft(e, 2.0, 3.0, newline_after_sentence=True, blank_line=True)
        self.assertEqual(out, "一つ目です。\n\n二つ目です。")
        # 間が短め（gap_sec 未満）でも、実際に無音で区切れた所なら（句点で終わっていれば）空行
        e2 = self.entries(("一つ目です。", 0.0, 2.0), ("二つ目です。", 2.4, 4.0))
        self.assertEqual(compose_draft(e2, 2.0, 3.0, blank_line=True), "一つ目です。\n\n二つ目です。")

    def test_silence_split_without_period_stays_joined_unless_forced(self):
        e = self.entries(("えっと", 0.0, 2.0), ("それで", 2.4, 4.0))                       # 句点なしで区切れた（話の途中かも）
        self.assertEqual(compose_draft(e, 2.0, 3.0, blank_line=True), "えっとそれで")
        e = self.entries(("えっと、", 0.0, 2.0), ("それで", 6.0, 8.0))                     # 3 秒以上: 句点を補って改行2つ
        self.assertEqual(compose_draft(e, 2.0, 3.0, blank_line=True), "えっと。\n\nそれで")

    def test_forced_max_length_split_is_not_a_break(self):
        e = self.entries(("長い話の前半", 0.0, 30.0), ("後半です。", 30.0, 33.0))          # 最長での強制の区切り（間 0）
        self.assertEqual(compose_draft(e, 2.0, 3.0, blank_line=True), "長い話の前半後半です。")

    def test_blank_line_off_keeps_old_rule(self):
        e = self.entries(("一つ目です。", 0.0, 2.0), ("二つ目です。", 4.0, 6.0))
        self.assertEqual(compose_draft(e, 2.0, 3.0, newline_after_sentence=True), "一つ目です。\n二つ目です。")
        e2 = self.entries(("一つ目です。", 0.0, 2.0), ("二つ目です。", 2.4, 4.0))
        self.assertEqual(compose_draft(e2, 2.0, 3.0), "一つ目です。二つ目です。")           # 従来: gap_sec 未満は連結

    def test_edited_text_is_not_reformatted_but_boundary_gets_blank_line(self):
        e = [{"text": "直した。文です。", "start_sec": 0.0, "end_sec": 2.0, "edited": True},
             {"text": "追記です。", "start_sec": 6.0, "end_sec": 8.0}]
        out = compose_draft(e, 2.0, 3.0, newline_after_sentence=True, blank_line=True)
        self.assertEqual(out, "直した。文です。\n\n追記です。")                              # 手修正の文には句点の改行を足さない
        e[1]["text"] = "追記。です。"
        self.assertEqual(compose_draft(e, 2.0, 3.0, newline_after_sentence=True, blank_line=True),
                         "直した。文です。\n\n追記。\nです。")

    def test_noise_text_detection(self):
        from dialogue_text import is_noise_text
        self.assertTrue(is_noise_text("ご視聴ありがとうございました"))
        self.assertTrue(is_noise_text("ご視聴ありがとうございました。"))
        self.assertTrue(is_noise_text("Thank you for watching!"))
        self.assertTrue(is_noise_text("..."))
        self.assertTrue(is_noise_text("。"))
        self.assertFalse(is_noise_text("ありがとうございました"))       # 普通の発話は捨てない
        self.assertFalse(is_noise_text("こんにちは"))
        self.assertFalse(is_noise_text(""))

    def test_collapse_repeats(self):
        from dialogue_text import collapse_repeats, normalize_for_dup
        self.assertEqual(collapse_repeats("はい。はい。はい。次です。"), ("はい。次です。", 2))
        self.assertEqual(collapse_repeats("はい。はい。次です。"), ("はい。はい。次です。", 0))     # 2回までは触らない
        self.assertEqual(collapse_repeats("A。B。A。B。A。"), ("A。B。B。", 2))
        self.assertEqual(collapse_repeats(""), ("", 0))
        self.assertEqual(normalize_for_dup("同じ、文です。 "), normalize_for_dup("同じ文です"))


class ClauseRunCollapseTest(unittest.TestCase):
    def test_comma_separated_runaway_is_collapsed(self):
        from dialogue_text import collapse_repeats
        t = "続けて話すとき、途中で言葉が抜けたり、 途中で言葉が抜けたり、 途中で言葉が抜けたり、 途中で言葉が抜けたりしないか。"
        out, n = collapse_repeats(t)
        self.assertEqual(out.count("途中で言葉が抜けたり、"), 1)
        self.assertGreaterEqual(n, 2)

    def test_two_repeats_and_short_clauses_are_kept(self):
        from dialogue_text import collapse_repeats
        self.assertEqual(collapse_repeats("はい、はい、はい、いいえ。"), ("はい、はい、はい、いいえ。", 0))
        t = "途中で言葉が抜けたり、途中で言葉が抜けたり、しない。"
        self.assertEqual(collapse_repeats(t), (t, 0))

if __name__ == "__main__":
    unittest.main()
