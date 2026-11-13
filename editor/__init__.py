# editor/__init__.py
# テキストエディタ(案B)のパッケージ。秀丸エディタが職場で使えなくなった
# 代替として、まずは動くもの(第1段)を目指す。仕様は collab-workspace の
# docs/work-tools/spec.md「案B テキストエディタ」。
#
# 中身:
#   document.py  文書モデル(Qt 非依存)。文字コードと改行の保持、検索
#   keymap.py    キー割り当ての表(命令 → QKeySequence)
#   window.py    窓(QPlainTextEdit が土台の QMainWindow)
#
# 既存のビューワー(filer_viewer)は「見る」ための作りなので拡張しない。
# 文字コードの判定だけ text_encoding.py に切り出して共用している。
from editor.window import EditorWindow


def create_window(path=None, **kwargs) -> EditorWindow:
    """エディタの窓を作る。開閉と参照の保持は呼び側(feature_screen /
    filer)の仕事(他の小窓と同じ作法)。"""
    return EditorWindow(path, **kwargs)


__all__ = ["EditorWindow", "create_window"]
