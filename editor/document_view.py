# editor/document_view.py
# EDDy の「文書のビュー」。1つの文書に付く表示と状態の束。
#
# 窓(EditorWindow)はタブの入れ物で、文書ごとのものはすべてこちらが
# 持つ(設計の決め: 今の EditorWindow を「文書のビュー」と「窓」に
# 分ける)。窓は今開いているビューを _edit / _doc 等のプロパティで
# 指すので、既存の処理は「今のタブ」に対してそのまま動く。
#
# ビューが持つもの:
#   - doc: 文書モデル(editor/document.py の TextDocument)
#   - edit: 本文(editor/text_edit.py の TextEdit)
#   - highlighter: 強調表示(edit の文書に付く)
#   - outline_pane: 左ペイン(アウトライン＋ファイル情報)
#   - watcher / watched_path: 開いているファイルの外部変更の監視
#   - external_change_pending: 外部変更があり、まだ決着していないか
#   - outline_timer: アウトライン再構築のまとめ待ち
#   - rx_generation / rx_pending: 正規表現の非同期実行の状態
#   - inc_base / inc_span: リアルタイム検索の起点と直前のヒット
#   - change_cache: マクロ記録のための、変化直前の選択とカーソル
#   - suppress_change_record: 読み込み・元に戻す等の最中は、文書の
#     変化をマクロ記録の材料にしないための印
#
# 信号の接続と処理は窓側の仕事(ビューは束ねるだけ)。ビュー同士は
# 互いを知らない。
from PySide6.QtCore import QFileSystemWatcher, QTimer
from PySide6.QtWidgets import QHBoxLayout, QWidget

from editor import document as docmod
from editor.outline_pane import OutlinePane
from editor.text_edit import TextEdit


class DocumentView(QWidget):
    """文書1つ分の表示(左ペイン＋本文)と、文書に付く状態の束。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = docmod.TextDocument()
        self.edit = TextEdit()
        self.highlighter = None     # 窓が正規表現を決めてから付ける
        self.outline_pane = OutlinePane()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.outline_pane)
        layout.addWidget(self.edit, 1)

        self.watcher = QFileSystemWatcher(self)
        self.watched_path = None
        self.external_change_pending = False

        self.outline_timer = QTimer(self)
        self.outline_timer.setSingleShot(True)

        self.rx_generation = 0
        self.rx_pending = None

        self.inc_base = None
        self.inc_span = None

        # マクロ記録のための控え(変化直前の (選択の開始, 選択の終了,
        # カーソル位置))と、記録から外す間の印。
        self.change_cache = (0, 0, 0)
        self.suppress_change_record = False

    def sync_text(self):
        """文書モデルの本文を、本文ウィジェットの今の内容に合わせる。"""
        self.doc.text = self.edit.toPlainText()

    def is_modified(self) -> bool:
        return self.edit.document().isModified()

    def track_change_cache(self):
        cursor = self.edit.textCursor()
        self.change_cache = (cursor.selectionStart(),
                             cursor.selectionEnd(), cursor.position())
