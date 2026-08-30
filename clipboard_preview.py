# clipboard_preview.py
# 「クリップボードの書式を落とす」ときに、落とした結果を貼る前に見せる窓。
#
# なぜ別窓なのか
# --------------
# 最初は定型文ピッカー(Ctrl+Alt+V)の下段プレビューに相乗りさせていたが、
#   - 生の HTML を並べても「どう貼り付くのか」が読み取れない
#   - 高さが固定(150px)・折り返し無しなので、少し長いと下が切れる
# の2点で用を成さなかった。書式を見るには、書式のまま描いて見せるしかない。
#
# 何を見せるのか / 見せられないのか
# --------------------------------
# 左に「元のまま」、右に「その段階に落とした結果」を、どちらも QTextEdit へ
# setHtml() して**描画した状態**で並べる。これで分かるのは
#   - 落とした側で中身が消えていないか(段落・箇条書き・表・強調が残っているか)
#   - コピー元の色やフォントが落ちているか
# の2点で、これは目で見て確かめられる。
#
# 逆に「PowerPoint で改行が潰れるかどうか」はここでは再現できない。Qt のリッチ
# テキストは <p> を素直に段落として描くので、潰れる側の元 HTML も**左では
# ちゃんと改行されて見えてしまう**。潰れるかどうかは貼り先の実装の話で、
# 手元のどんな描画エンジンで見ても答えは出ない。
#
# そこを埋めるのが「元に戻す」の方。載せて → 実際に貼って → 違えば戻す、が
# 安全にできれば、貼り先そのもので確かめられる。窓の中でもそう案内している。
#
# 元に戻せること
# --------------
# 整形するとコピー元のリッチテキストは失われ、クリップボードに履歴は無いので
# Ctrl+Z も効かない。clipboard_format 側が整形の直前に中身を退避するので、
# この窓と定型文ピッカーの両方から書き戻せるようにしてある。
#
# 窓の大きさ
# ----------
# 既定は画面の作業領域に対する割合で決め、変えた大きさは settings.json に覚える
# (ピッカーと違って中身の分量が読めないので、固定値にすると必ずどちらかが困る)。
import json
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import clipboard_format
import settings as settings_module
from toast import show_toast

# 既定の大きさ。作業領域に対する割合で決める(4Kと1080pで同じ固定値は使えない)。
DEFAULT_WIDTH_RATIO = 0.62
DEFAULT_HEIGHT_RATIO = 0.66

# これより小さくすると左右に並べた意味が無くなる。
MIN_WIDTH = 720
MIN_HEIGHT = 420

# settings.json の置き場所。picker と違い「窓の大きさ」だけを覚える(位置は覚えない。
# 出すたびにカーソルのある画面の中央へ置きたいので、位置は毎回決め直す)。
SETTINGS_SECTION = "clipboard_format"
SETTINGS_KEY = "preview_size"

# 生の HTML を出す欄の高さ。折りたたみを開いたときだけ使う。
RAW_HEIGHT = 150

STYLE = (
    "#previewWindow { background-color: #141414; color: #ffffff; }"
    "#previewWindow QLabel { color: #d4d4d4; }"
    "#previewNote { color: #8a8a8a; }"
    "#previewHeading { color: #ffffff; font-weight: bold; }"
    # 中身のプレビューだけは明るい地にする。貼り先(PowerPoint / Word / Teams)は
    # たいてい白い紙なので、暗い地に白文字で見せると印象がずれる。
    "QTextEdit { background-color: #ffffff; color: #1a1a1a;"
    " border: 1px solid #3c3c3c; border-radius: 4px; }"
    "#previewRaw { background-color: #1c1c1c; color: #d4d4d4;"
    " border: 1px solid #3c3c3c; border-radius: 4px; }"
    "#previewWindow QPushButton { background-color: #262626; color: #ffffff;"
    " border: 1px solid #3c3c3c; border-radius: 4px; padding: 6px 12px; }"
    "#previewWindow QPushButton:hover { background-color: #303030; }"
    "#previewWindow QPushButton:checked { background-color: #2563eb;"
    " border-color: #2563eb; }"
    "#previewWindow QPushButton:disabled { color: #6a6a6a; border-color: #2a2a2a; }"
    "#previewApply { background-color: #2563eb; border-color: #2563eb; }"
    "#previewApply:hover { background-color: #3b7dff; }"
)


def _load_size(app_settings):
    """覚えてある大きさを (幅, 高さ) で返す。無ければ None。"""
    try:
        section = (app_settings or {}).get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            return None
        size = section.get(SETTINGS_KEY)
        if not (isinstance(size, (list, tuple)) and len(size) == 2):
            return None
        width, height = int(size[0]), int(size[1])
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            return None
        return width, height
    except Exception:
        return None


def _save_size(app_settings, settings_path, width: int, height: int) -> None:
    """大きさを settings.json へ書く。

    push_recent(snippets.py) と同じ流儀で、メモリ上の設定を丸ごと書き戻さず
    ファイルを読み直して該当キーだけ差し替える(既定値まで明示的に書かれて
    settings.json の姿が変わるのを避けるため)。"""
    if isinstance(app_settings, dict):
        section = app_settings.get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            section = app_settings[SETTINGS_SECTION] = {}
        section[SETTINGS_KEY] = [width, height]

    if not settings_path:
        return
    try:
        stored = {}
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                stored = json.load(f)
        if not isinstance(stored, dict):
            stored = {}
        section = stored.get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            section = stored[SETTINGS_SECTION] = {}
        section[SETTINGS_KEY] = [width, height]
        settings_module.save_settings(stored, settings_path)
    except Exception as e:
        # 大きさを覚えられないだけ。窓の開閉を止めるほどのことではない。
        print(f"[tray-tools] プレビューの大きさを保存できません: {e}")


class FormatPreviewWindow(QWidget):
    """整形後の見た目を確かめてから、クリップボードへ載せる窓。

    開いた時点のクリップボードを読んで抱え込み、以後は読み直さない。
    「元のまま」を比較対象として出し続けたいので、自分で書き換えた後の
    クリップボードを見に行っては困る。"""

    def __init__(self, app_settings=None, settings_path=None):
        super().__init__()
        self._app_settings = app_settings
        self._settings_path = settings_path

        # 開いた時点の中身。これが比較の基準になる。
        self._document, self._fragment, self._text = clipboard_format.read_clipboard()
        self._level = clipboard_format.DEFAULT_LEVEL

        self.setWindowTitle("クリップボードの書式")
        self.setObjectName("previewWindow")
        # 最前面に出す。貼り先のアプリを見ながら確かめたいので、後ろへ回られると困る。
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setStyleSheet(STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addLayout(self._build_level_row())

        self.description = QLabel("")
        self.description.setFont(QFont("Meiryo", 9))
        self.description.setWordWrap(True)
        layout.addWidget(self.description)

        # この窓が答えられることと答えられないことを、最初から書いておく。
        # 左が「ちゃんと改行されて」見えるのは Qt がそう描くからで、PowerPoint が
        # 同じに解釈する保証にはならない。そこを黙っていると、見た目を信じて
        # 貼って裏切られる。
        caveat = QLabel(
            "左右とも Qt での描画です。何が残る／落ちるかはここで分かりますが、"
            "貼り先での改行の扱いまでは再現できません。"
            "載せてから実際に貼って確かめて、違えば［元に戻す］で戻せます。"
        )
        caveat.setObjectName("previewNote")
        caveat.setFont(QFont("Meiryo", 8))
        caveat.setWordWrap(True)
        layout.addWidget(caveat)

        layout.addWidget(self._build_panes(), 1)

        self.raw = QPlainTextEdit()
        self.raw.setObjectName("previewRaw")
        self.raw.setFont(QFont("Consolas", 9))
        self.raw.setReadOnly(True)
        # こちらは折り返す。生の HTML は1行が長く、折り返さないと右へ消えていく
        # (下段プレビューで下が切れていたのと同じ失敗を繰り返さない)。
        self.raw.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.raw.setFixedHeight(RAW_HEIGHT)
        self.raw.setVisible(False)
        layout.addWidget(self.raw)

        self.status = QLabel("")
        self.status.setObjectName("previewNote")
        self.status.setFont(QFont("Meiryo", 8))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        layout.addLayout(self._build_button_row())

        self._install_shortcuts()
        self._resize_to_saved()
        self._move_to_center()
        self._refresh()

    # ------------------------------------------------------------------
    # 組み立て
    # ------------------------------------------------------------------
    def _build_level_row(self):
        """段階を選ぶボタンの列。1/2/3 でも選べる。"""
        row = QHBoxLayout()
        row.setSpacing(6)

        heading = QLabel("落とし方")
        heading.setObjectName("previewHeading")
        heading.setFont(QFont("Meiryo", 9))
        row.addWidget(heading)

        self._level_group = QButtonGroup(self)
        self._level_group.setExclusive(True)
        self._level_buttons = {}
        for index, (key, label, _description) in enumerate(clipboard_format.LEVELS, 1):
            button = QPushButton(f"{index}. {label}")
            button.setFont(QFont("Meiryo", 9))
            button.setCheckable(True)
            button.setChecked(key == self._level)
            button.clicked.connect(lambda _checked, k=key: self._select(k))
            self._level_group.addButton(button)
            self._level_buttons[key] = button
            row.addWidget(button)

        row.addStretch(1)

        self.raw_button = QPushButton("HTMLを見る")
        self.raw_button.setFont(QFont("Meiryo", 9))
        self.raw_button.setCheckable(True)
        self.raw_button.toggled.connect(self._toggle_raw)
        row.addWidget(self.raw_button)
        return row

    def _build_panes(self):
        """左「元のまま」/ 右「落とした結果」。境目はドラッグで動かせる。"""
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        self.before = self._make_pane()
        self.after = self._make_pane()
        splitter.addWidget(self._wrap_pane("元のまま（コピー元の書式）", self.before))
        splitter.addWidget(self._wrap_pane("落とした結果（何が残るか）", self.after))
        splitter.setSizes([1, 1])
        return splitter

    def _make_pane(self):
        view = QTextEdit()
        view.setReadOnly(True)
        # 読むための欄なので折り返す。ここを NoWrap にすると横に消えていく。
        view.setLineWrapMode(QTextEdit.WidgetWidth)
        return view

    def _wrap_pane(self, title: str, view):
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        label = QLabel(title)
        label.setObjectName("previewHeading")
        label.setFont(QFont("Meiryo", 9))
        column.addWidget(label)
        column.addWidget(view, 1)
        return box

    def _build_button_row(self):
        row = QHBoxLayout()
        row.setSpacing(6)

        note = QLabel("1/2/3 段階  ·  Enter この形にする  ·  Ctrl+Z 元に戻す  ·  Esc 閉じる")
        note.setObjectName("previewNote")
        note.setFont(QFont("Meiryo", 8))
        row.addWidget(note)
        row.addStretch(1)

        self.restore_button = QPushButton("元に戻す")
        self.restore_button.setFont(QFont("Meiryo", 9))
        self.restore_button.clicked.connect(self._restore)
        row.addWidget(self.restore_button)

        close_button = QPushButton("閉じる")
        close_button.setFont(QFont("Meiryo", 9))
        close_button.clicked.connect(self.close)
        row.addWidget(close_button)

        self.apply_button = QPushButton("この形にする")
        self.apply_button.setObjectName("previewApply")
        self.apply_button.setFont(QFont("Meiryo", 9))
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self._apply)
        row.addWidget(self.apply_button)
        return row

    def _install_shortcuts(self):
        """キーは QShortcut で持つ。keyPressEvent に集めると、QTextEdit へ
        フォーカスがある間は先にそちらへ食われてしまう。"""
        for index, key in enumerate(clipboard_format.LEVEL_KEYS, 1):
            shortcut = QShortcut(QKeySequence(str(index)), self)
            shortcut.activated.connect(lambda k=key: self._select(k))

        for sequence in ("Return", "Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(self._apply)

        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close)
        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self._restore)

    # ------------------------------------------------------------------
    # 大きさ・位置
    # ------------------------------------------------------------------
    def _resize_to_saved(self):
        screen = self._screen()
        area = screen.availableGeometry()
        saved = _load_size(self._app_settings)
        if saved is None:
            width = int(area.width() * DEFAULT_WIDTH_RATIO)
            height = int(area.height() * DEFAULT_HEIGHT_RATIO)
        else:
            width, height = saved
        # 覚えてある大きさが、いまの画面に収まるとは限らない(モニタ構成が変わる)。
        width = max(MIN_WIDTH, min(width, area.width()))
        height = max(MIN_HEIGHT, min(height, area.height()))
        self.resize(width, height)

    def _move_to_center(self):
        area = self._screen().availableGeometry()
        x = area.center().x() - self.width() // 2
        y = area.center().y() - self.height() // 2
        x = max(area.left(), min(x, area.right() - self.width()))
        y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)

    def _screen(self):
        from PySide6.QtGui import QCursor

        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    # ------------------------------------------------------------------
    # 中身
    # ------------------------------------------------------------------
    def _select(self, level: str) -> None:
        if level == self._level:
            return
        self._level = level
        button = self._level_buttons.get(level)
        if button is not None:
            button.setChecked(True)
        self._refresh()

    def _toggle_raw(self, shown: bool) -> None:
        self.raw.setVisible(shown)
        self.raw_button.setText("HTMLを隠す" if shown else "HTMLを見る")

    def _refresh(self) -> None:
        """左右のプレビューと状態表示を、いま選んでいる段階で作り直す。"""
        description = next(
            (desc for key, _label, desc in clipboard_format.LEVELS if key == self._level),
            "",
        )
        self.description.setText(description)

        if self._fragment is None:
            self.before.setPlainText("クリップボードに HTML がありません。")
            self.after.setPlainText("")
            self.raw.setPlainText("")
            self.status.setText("落とせる書式が載っていません。")
            self.apply_button.setEnabled(False)
            self._update_restore()
            return

        self.before.setHtml(self._document or self._fragment)

        new_document, new_text = clipboard_format.transform(
            self._fragment, self._text, self._level
        )
        if new_document is None:
            # テキストのみ。描画するものが無いので、載る文字をそのまま出す
            # (改行がどう残るかを見たい欄なので、等幅にはしない)。
            self.after.setPlainText(new_text or "")
            self.raw.setPlainText("(HTML を載せません)")
            self.status.setText(
                f"HTML を捨てて、テキスト {len(new_text or '')} 文字だけを載せます。"
            )
        else:
            self.after.setHtml(new_document)
            payload = clipboard_format.build_cf_html(new_document)
            _doc, _frag, offsets = clipboard_format.parse_cf_html(payload)
            self.raw.setPlainText(new_document)
            self.status.setText(
                f"CF_HTML {len(payload)} バイト "
                f"（StartFragment:{offsets.get('StartFragment')} / "
                f"EndFragment:{offsets.get('EndFragment')}）"
            )
        self.apply_button.setEnabled(True)
        self._update_restore()

    def _update_restore(self) -> None:
        available = clipboard_format.has_snapshot()
        self.restore_button.setEnabled(available)
        if available:
            self.restore_button.setToolTip(
                f"整形する前の中身に戻す\n{clipboard_format.snapshot_summary()}"
            )
        else:
            self.restore_button.setToolTip("まだ整形していないので、戻す先がありません")

    # ------------------------------------------------------------------
    # 実行
    # ------------------------------------------------------------------
    def _apply(self) -> None:
        if self._fragment is None:
            return
        ok, message = clipboard_format.apply_level(self._level)
        if ok:
            # 戻し方をここで言っておく。窓を閉じた後に「やっぱり違った」と
            # 気づくのが普通なので、そのとき探さずに済むようにする。
            message += "\n戻すには Ctrl+Alt+V →「元に戻す」"
        show_toast(f"クリップボードの書式\n{message}")
        if not ok:
            self._update_restore()
            return
        # 貼り付け(Ctrl+V の送信)はしない。誤爆したときの被害が読めないので、
        # 載せるところまでにして、貼るのは本人に任せる。
        self.close()

    def _restore(self) -> None:
        if not clipboard_format.has_snapshot():
            return
        _ok, message = clipboard_format.restore_snapshot()
        show_toast(f"クリップボードの書式\n{message}")
        # 戻した後は、いまのクリップボードを読み直して比較の基準にし直す。
        self._document, self._fragment, self._text = clipboard_format.read_clipboard()
        self._refresh()

    # ------------------------------------------------------------------
    def closeEvent(self, event):
        _save_size(self._app_settings, self._settings_path, self.width(), self.height())
        super().closeEvent(event)


# 開いた窓はここで参照を持つ。ローカル変数だけだと GC で消える。
_window = None


def open_preview(app_settings=None, settings_path=None):
    """プレビュー窓を開く。すでに開いていれば前面に呼び戻すだけ。"""
    global _window

    if _window is not None:
        try:
            if _window.isVisible():
                _window.raise_()
                _window.activateWindow()
                return _window
        except RuntimeError:
            # C++ 側が先に消えている。destroyed が届く前に呼ばれるとここへ来るので、
            # 参照を捨てて作り直す(この手の RuntimeError は落ちる形で出る)。
            _window = None

    if not clipboard_format.clipboard_has_html():
        show_toast("クリップボードの書式\n落とせる書式が載っていません")
        return None

    window = FormatPreviewWindow(app_settings, settings_path)
    window.destroyed.connect(_forget)
    window.setAttribute(Qt.WA_DeleteOnClose, True)
    _window = window
    window.show()
    window.raise_()
    window.activateWindow()
    return window


def _forget(*_args) -> None:
    global _window
    _window = None
