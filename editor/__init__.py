# editor/__init__.py
# テキストエディタ(案B)のパッケージ。秀丸エディタが職場で使えなくなった
# 代替として、まずは動くもの(第1段)を目指す。仕様は collab-workspace の
# docs/work-tools/spec.md「案B テキストエディタ」。
#
# 中身:
#   document.py     文書モデル(Qt 非依存)。文字コードと改行の保持、検索
#   convert.py      全角・半角と英字の大文字・小文字の変換(Qt 非依存)
#   keymap.py       キー割り当ての表(命令 → QKeySequence)
#   macros.py       マクロ(命令の列の記録の切り分けと JSON。Qt 非依存)
#   outline.py      アウトラインの抽出と階層付け(Qt 非依存の純粋関数)
#   rxfind.py       正規表現の検索・置換(Qt 非依存。別プロセスで実行)
#   grep.py         フォルダ内の横断検索の純粋部分(Qt 非依存)
#   grep_dialog.py  横断検索の窓(入力・進捗・結果の一覧)
#   colors.py       配色プリセット(標準・秀丸風)
#   text_edit.py    本文(QPlainTextEdit＋行番号＋Ctrl+ホイールで大きさ
#                   ＋矩形選択は rect_edit.py の共用部品)
#   outline_pane.py 左ペイン(アウトラインのツリー＋下部のファイル情報)
#   highlighter.py  星印の行などを強調表示する QSyntaxHighlighter
#   window.py       窓(QMainWindow。リボン風ツールバー付き)
#
# 既存のビューワー(filer_viewer)は「見る」ための作りなので拡張しない。
# 文字コードの判定だけ text_encoding.py に切り出して共用している。
from editor.window import (
    EditorWindow,
    dirty_window_state,
    find_window_for_path,
)


def create_window(path=None, **kwargs) -> EditorWindow:
    """エディタの窓を作る。開閉と参照の保持は呼び側(feature_screen /
    filer)の仕事(他の小窓と同じ作法)。"""
    return EditorWindow(path, **kwargs)


__all__ = [
    "EditorWindow",
    "create_window",
    "dirty_window_state",
    "find_window_for_path",
]
