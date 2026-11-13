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
#   - edit: 本文(editor/text_edit.py の TextEdit)。分割していない
#     ときはこれだけが本文
#   - split_edit / split_orientation: 分割表示の2枚目の本文と向き。
#     分割していないときは両方 None。2枚目は1枚目と文書
#     (QTextDocument)を共有し(edit.adopt_shared_document)、
#     カーソルとスクロールはペインごとに独立。編集マーク等の控えも
#     置き場を共有するので、両ペインの表示はそろう
#   - splitter: 本文を並べる QSplitter(分割していないときは
#     1枚目だけが入っている)
#   - _active_edit: 最後にフォーカスがあったペイン。窓の _edit は
#     こちらを指し、検索・保存などの操作はフォーカスのあるペイン
#     が対象になる
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
# 互いを知らない。ただしペインのフォーカスの追跡だけは、ビューの
# 中で閉じる(現在のペインの切り替えは表示の都合のため)。
from PySide6.QtCore import QFileSystemWatcher, QTimer, Qt
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QWidget

from editor import document as docmod
from editor.outline_pane import OutlinePane
from editor.text_edit import TextEdit

# 分割の向きの名前(セッションの記録で使う文字列)と Qt の向き。
SPLIT_ORIENTATIONS = {
    "horizontal": Qt.Orientation.Horizontal,   # 左右に並べる
    "vertical": Qt.Orientation.Vertical,       # 上下に並べる
}


class DocumentView(QWidget):
    """文書1つ分の表示(左ペイン＋本文)と、文書に付く状態の束。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = docmod.TextDocument()
        self.edit = TextEdit()
        self.split_edit = None
        self.split_orientation = None
        self._active_edit = self.edit
        self.edit.focused.connect(
            lambda: self._note_pane_focus(self.edit))
        self.highlighter = None     # 窓が正規表現を決めてから付ける
        self.outline_pane = OutlinePane()
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.edit)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.outline_pane)
        layout.addWidget(self.splitter, 1)

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

    # ---------------------------------------------------------------
    # 分割表示
    # ---------------------------------------------------------------
    def panes(self):
        """本文のペインの並び(1枚目、分割していれば2枚目)。"""
        if self.split_edit is None:
            return [self.edit]
        return [self.edit, self.split_edit]

    def active_edit(self):
        """現在のペイン(最後にフォーカスがあった本文)。

        検索・保存などの操作の対象。分割していないときは1枚目。"""
        if self.split_edit is not None and self._active_edit is self.split_edit:
            return self.split_edit
        return self.edit

    def _note_pane_focus(self, edit):
        self._active_edit = edit

    def split_name(self):
        """分割の向きの名前(セッションの記録用)。分割していなければ
        None。"""
        if self.split_orientation is None:
            return None
        for name, orientation in SPLIT_ORIENTATIONS.items():
            if orientation == self.split_orientation:
                return name
        return None

    def split(self, orientation):
        """分割表示にする。2枚目の本文を返し、現在のペインにする。

        既に分割していれば、向きだけ変える。2枚目は1枚目と文書を
        共有し、カーソルは1枚目と同じ位置から始める(スクロールも
        そこに合わせる)。表示のそろえ(フォント・配色・折り返し)と
        信号の接続は、窓がこの後で行う(窓の _split_view)。"""
        if self.split_edit is not None:
            self.split_orientation = orientation
            self.splitter.setOrientation(orientation)
            return self.split_edit
        second = TextEdit()
        second.adopt_shared_document(
            self.edit.document(), self.edit.tracking_state())
        second.focused.connect(
            lambda: self._note_pane_focus(second))
        self.split_orientation = orientation
        self.splitter.setOrientation(orientation)
        self.splitter.addWidget(second)
        self.split_edit = second
        second.setTextCursor(self.edit.textCursor())
        self._active_edit = second
        return second

    def unsplit(self):
        """分割をやめる。2枚目を外して捨て、現在のペインは1枚目に
        戻す。文書そのものには触らない。"""
        second = self.split_edit
        if second is None:
            return
        self.split_edit = None
        self.split_orientation = None
        if self._active_edit is second:
            self._active_edit = self.edit
        # QSplitter には取り外しの口が無いので、親を外すと
        # スプリッタからも外れる。
        second.setParent(None)
        second.deleteLater()

    def refresh_pane_areas(self):
        """両ペインの行番号エリアを描き直す。

        控えはペイン間で共有なので、文書を変えない操作(保存に伴う
        編集マークの消去、ブックマークの付け外し)が片方で起きたら、
        もう片方の描画も合わせ直す。"""
        for edit in self.panes():
            edit.refresh_line_number_area()

    # ---------------------------------------------------------------
    def sync_text(self):
        """文書モデルの本文を、本文ウィジェットの今の内容に合わせる。"""
        self.doc.text = self.edit.toPlainText()

    def is_modified(self) -> bool:
        return self.edit.document().isModified()

    def track_change_cache(self):
        cursor = self.active_edit().textCursor()
        self.change_cache = (cursor.selectionStart(),
                             cursor.selectionEnd(), cursor.position())
