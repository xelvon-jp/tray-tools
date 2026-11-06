# test_ai_templates.py
# AI依頼テンプレート集(snippets/ の AI依頼・*.txt)が、定型文として読み込めて
# 変数を展開できることを確かめる。中身の文面そのものは固定しない(本人が
# エディタで直す前提のファイルなので、あることと壊れていないことだけ見る)。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

EXPECTED = ["AI依頼・レビュー", "AI依頼・実装", "AI依頼・デバッグ",
            "AI依頼・調査", "AI依頼・テスト", "AI依頼・説明"]


def test_ai_templates_load_and_expand():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    import snippets

    templates = dict(snippets.load_templates())
    for name in EXPECTED:
        assert name in templates, f"{name} が読み込めていません"
        body = templates[name]
        assert body.strip(), f"{name} が空です"
        # プレビュー展開({input} を尋ねない形)で壊れず、変数が残らない
        expanded = snippets.expand_variables(body, preview=True)
        assert expanded is not None
        assert "{clipboard}" not in expanded
        assert "{input" not in expanded


ALL_TESTS = [test_ai_templates_load_and_expand]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
