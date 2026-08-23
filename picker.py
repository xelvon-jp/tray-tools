# picker.py
# 枠なしの小さな選択ウインドウ。定型文(snippets.py)とフォルダブックマーク(launcher.py)で
# 同じ窓を使うため、元は SnippetPicker だったものを汎用化して切り出した部品。
#
# 「表示名で絞り込んで1つ決める」以上のことはしない。決めた後に何をするか(コピー・移動・
# 登録)は呼び出し側の on_accept が行い、ウインドウの開閉はこちらが受け持つ。
import sys

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

WINDOW_WIDTH = 420

# プレビューを出すときは横も広げる。展開後の本文を折り返しだらけで見せても
# 「何がコピーされるか」が読み取れないため。
PREVIEW_WINDOW_WIDTH = 560

# 一覧に見せる行数。項目数に応じてこの範囲で高さを伸縮させる。少なすぎると
# 探しにくく、多すぎると画面を覆ってしまうので上下に枠を設ける。
MIN_VISIBLE_ROWS = 8
MAX_VISIBLE_ROWS = 20

# プレビューを出すときは一覧を控えめにする。両方を最大まで伸ばすと画面を覆ってしまう。
PREVIEW_MAX_VISIBLE_ROWS = 12

# プレビュー領域とキーヒント1行の高さ(px)。行数×フォント高さで求めない。
# QT_QPA_PLATFORM=offscreen ではフォントメトリクスが当てにならず、測った値だけを
# 根拠にすると環境によって潰れたり伸びたりするため、ここは固定値で持つ。
PREVIEW_HEIGHT = 150
HINT_LINE_HEIGHT = 15

# 画面の作業領域に対して、ウインドウが占めてよい高さの上限。
MAX_SCREEN_RATIO = 0.8

# 中央より少し上に出す。ちょうど中央だと視線移動が大きく、下端が画面の下寄りになる。
VERTICAL_OFFSET = -200

# 一覧の行の余白(px)。QListWidget の既定は行間が広く、一度に見える数が減る。
ITEM_PADDING_V = 1
ITEM_PADDING_H = 6

DEFAULT_PLACEHOLDER = "絞り込み（↑↓で選択 / Enterで決定 / Escで閉じる）"


class PickerWindow(QWidget):
    """枠なしの小さな選択ウインドウ。上の入力欄で絞り込み、下のリストから1つ決める。

    items は [(表示名, 任意のデータ), ...]。決定すると on_accept(表示名, データ) を呼び、
    その後このウインドウは必ず閉じて closed を出す(呼び出し側は参照を捨てるだけでよい)。
    on_accept は「閉じるかどうか」を決められない。二択にすると呼び出し側ごとに閉じ忘れの
    経路が生まれ、枠なし・最前面のウインドウが画面に残る事故になるため。

    preview_provider / hint / on_new / on_edit / on_open_folder はすべて任意。渡した
    ものだけが姿を現す。定型文には要るがフォルダブックマークには要らない、という差を
    ここで吸収する(共用の窓なので、既定では今までどおり「絞り込んで選ぶだけ」)。

    - preview_provider(データ) -> str … 選択中の項目の中身を下のプレビューに出す
    - hint … ウインドウ下部に出す小さな説明(改行可)
    - on_new() / on_edit(表示名, データ) / on_open_folder() … Ctrl+N / Ctrl+E / Ctrl+O。
      いずれも実行後はこのウインドウを閉じる(外部エディタで直した結果を出すには
      開き直す必要があり、古い一覧を残しておくと食い違うため)。"""

    closed = Signal()

    def __init__(
        self,
        title: str,
        items: list,
        on_accept,
        placeholder: str = None,
        preview_provider=None,
        hint: str = None,
        on_new=None,
        on_edit=None,
        on_open_folder=None,
    ):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle(title)

        self._items = list(items)
        self._on_accept = on_accept
        self._preview_provider = preview_provider
        self._on_new = on_new
        self._on_edit = on_edit
        self._on_open_folder = on_open_folder
        self._closed_emitted = False
        # 決定は一度きり。Enterの押しっぱなしなど、閉じるまでの間に二度呼ばれても弾く。
        # Ctrl+N などの副作用を持つキーも、走り出したらこのフラグで後続を止める。
        self._committing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        font = QFont("Meiryo", 10)
        self.search = QLineEdit()
        self.search.setFont(font)
        self.search.setPlaceholderText(placeholder or DEFAULT_PLACEHOLDER)
        self.search.textChanged.connect(self._refresh_list)
        # 入力欄にフォーカスがある間も ↑↓ / Enter / Esc を効かせたい。QLineEdit が先に
        # 食べてしまうので、イベントフィルタで横取りする。
        self.search.installEventFilter(self)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.setFont(font)
        # 項目間の隙間を殺す。padding(スタイルシート)と別に効くので両方詰める。
        self.list.setSpacing(0)
        # 全項目が同じ高さだと Qt が高さ計算を省ける。行高を測る _fit_to_items とも相性がよい。
        self.list.setUniformItemSizes(True)
        # itemActivated だけを繋ぐ。Windowsではダブルクリックでこれも飛ぶので、
        # itemDoubleClicked と両方繋ぐと _commit が二度走る({input}を含む定型文だと
        # 入力ダイアログが2回出てしまう)。
        self.list.itemActivated.connect(lambda _item: self._commit())
        layout.addWidget(self.list)

        # プレビュー。読み取り専用で、選択が動くたびに中身を差し替える。
        self.preview = None
        if preview_provider is not None:
            self.preview = QPlainTextEdit()
            self.preview.setObjectName("pickerPreview")
            self.preview.setFont(QFont("Meiryo", 9))
            self.preview.setReadOnly(True)
            # 折り返さない。テンプレートは行単位で書かれているので、折り返すと
            # 「実際にコピーされる形」と見た目がずれる。
            self.preview.setLineWrapMode(QPlainTextEdit.NoWrap)
            self.preview.setFixedHeight(PREVIEW_HEIGHT)
            layout.addWidget(self.preview)
            # currentItemChanged はクリック・↑↓・絞り込みのどれで動いても飛ぶ。
            # itemSelectionChanged と両方繋ぐと二度走るので、こちらだけにする。
            self.list.currentItemChanged.connect(self._on_current_changed)

        # キーヒント/変数の早見表。高さは行数から固定値で決める(offscreen対策)。
        self.hint = None
        self._hint_height = 0
        if hint:
            self.hint = QLabel(hint)
            self.hint.setObjectName("pickerHint")
            self.hint.setFont(QFont("Meiryo", 8))
            # QLabel は既定(AutoText)だと中身をHTMLとして解釈しうる。早見表には
            # {clipboard} のような記法をそのまま出したいので平文に固定する。
            self.hint.setTextFormat(Qt.PlainText)
            self.hint.setTextInteractionFlags(Qt.NoTextInteraction)
            self._hint_height = HINT_LINE_HEIGHT * (hint.count("\n") + 1)
            self.hint.setFixedHeight(self._hint_height)
            layout.addWidget(self.hint)

        # 見た目は toast / color_picker の情報パネルに揃える。地の色の指定を objectName で
        # 絞っているのは、子として開く QInputDialog までダークに塗り替えないため
        # (ボタンまで背景色を指定するとネイティブの見た目が消える)。プレビューと
        # ヒントを objectName で絞るのも同じ理由で、素の QPlainTextEdit / QLabel を
        # 指定すると QInputDialog.getMultiLineText の入力欄やラベルまで巻き込む。
        self.setObjectName("pickerWindow")
        self.setStyleSheet(
            "#pickerWindow { background-color: #141414; color: #ffffff; }"
            "#pickerWindow QLineEdit { background-color: #222222; color: #ffffff;"
            " border: 1px solid #3c3c3c; border-radius: 4px; padding: 5px; }"
            "QListWidget { background-color: #1c1c1c; color: #ffffff;"
            " border: 1px solid #3c3c3c; border-radius: 4px; }"
            f"QListWidget::item {{ padding: {ITEM_PADDING_V}px {ITEM_PADDING_H}px; }}"
            "QListWidget::item:selected { background-color: #2563eb; }"
            "#pickerPreview { background-color: #1c1c1c; color: #d4d4d4;"
            " border: 1px solid #3c3c3c; border-radius: 4px; padding: 4px; }"
            "#pickerHint { color: #8a8a8a; }"
        )

        self._refresh_list("")
        self._fit_to_items()
        self._move_to_center()

    def _fit_to_items(self):
        """項目数に合わせて高さを決める。

        絞り込みのたびには変えない。1文字打つごとにウインドウが伸び縮みすると
        目が落ち着かないうえ、中央配置なので位置まで動いてしまう。"""
        # 項目が無いと -1 が返る。環境によっては極端に小さい値を返すこともあるので、
        # 「フォントの高さ + スタイルシートの上下padding」を下限として噛ませる。
        row_height = max(
            self.list.sizeHintForRow(0),
            self.list.fontMetrics().height() + ITEM_PADDING_V * 2,
        )

        max_rows = MAX_VISIBLE_ROWS if self.preview is None else PREVIEW_MAX_VISIBLE_ROWS
        rows = min(max(len(self._items), MIN_VISIBLE_ROWS), max_rows)

        spacing = self.layout().spacing()
        margins = self.layout().contentsMargins()
        height = (
            self.search.sizeHint().height()
            + spacing
            + row_height * rows
            + self.list.frameWidth() * 2
            + margins.top()
            + margins.bottom()
        )
        # 追加分は測らずに固定値で足す。offscreen では sizeHint が当てにならず、
        # 測った値を信じるとプレビューが潰れた高さでウインドウが決まってしまう。
        if self.preview is not None:
            height += spacing + PREVIEW_HEIGHT
        if self.hint is not None:
            height += spacing + self._hint_height

        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        height = min(height, int(screen.availableGeometry().height() * MAX_SCREEN_RATIO))
        width = WINDOW_WIDTH if self.preview is None else PREVIEW_WINDOW_WIDTH
        self.resize(width, height)

    def _move_to_center(self):
        """カーソルのある画面の中央よりやや上に出す(toastと同じ理由でprimaryScreen固定にしない)。

        VERTICAL_OFFSET のぶん持ち上げるが、持ち上げすぎて画面の外に出ないよう
        作業領域に収まる範囲でクランプする(タスクバーの下に潜らせない)。"""
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        center = area.center()

        x = center.x() - self.width() // 2
        y = center.y() - self.height() // 2 + VERTICAL_OFFSET

        x = max(area.left(), min(x, area.right() - self.width()))
        y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        # 常駐アプリのメニューやホットキーから開くとフォーカスが来ないことがある。
        # 入力欄まで明示的にフォーカスを渡さないと、そのまま打った文字が前のアプリへ飛ぶ。
        self.raise_()
        self.activateWindow()
        self.search.setFocus()

    def closeEvent(self, event):
        super().closeEvent(event)
        # close() は複数の経路(Esc・決定後・呼び出し側)から呼ばれる。closed を受けた側が
        # また close() することもあるので、通知は1回だけに絞る。
        if not self._closed_emitted:
            self._closed_emitted = True
            self.closed.emit()

    def eventFilter(self, obj, event):
        if obj is self.search and event.type() == QEvent.KeyPress:
            if self._handle_shortcut(event):
                return True
            key = event.key()
            if key in (Qt.Key_Up, Qt.Key_Down):
                self._move_selection(-1 if key == Qt.Key_Up else 1)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._commit()
                return True
            if key == Qt.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        # 入力欄以外(一覧やプレビュー)にフォーカスが移っていても効かせる。
        if self._handle_shortcut(event):
            return
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def _handle_shortcut(self, event) -> bool:
        """Ctrl+N / Ctrl+E / Ctrl+O を処理したら True を返す。

        渡されなかった処理のキーは素通しする(フォルダブックマークのように編集手段を
        持たない呼び出し側で、押しても何も起きない代わりに黙って握り潰さないため)。"""
        if not (event.modifiers() & Qt.ControlModifier):
            return False
        key = event.key()
        if key == Qt.Key_N and self._on_new is not None:
            self._run_action(lambda: self._on_new())
            return True
        if key == Qt.Key_E and self._on_edit is not None:
            item = self.list.currentItem()
            if item is None:
                return True  # 選択が無いだけ。開くものが無いので何もしない。
            name, data = self._items[item.data(Qt.UserRole)]
            self._run_action(lambda: self._on_edit(name, data))
            return True
        if key == Qt.Key_O and self._on_open_folder is not None:
            self._run_action(lambda: self._on_open_folder())
            return True
        return False

    def _run_action(self, func) -> None:
        """Ctrl+N/E/O の処理を走らせて、このウインドウを閉じる。

        _commit と同じく例外はここで止める(Qtのスロット内で投げ切ると常駐ごと落ちる)。
        閉じるのは、外部エディタで直した結果を出すには開き直すしかなく、開いたままだと
        古い一覧が残って食い違うため。"""
        if self._committing:
            return
        self._committing = True
        try:
            func()
        except Exception as e:
            print(f"[tray-tools] ピッカーの操作に失敗しました: {e}", file=sys.stderr)
        finally:
            self.close()

    def _on_current_changed(self, current, _previous):
        """選択が動くたびにプレビューを差し替える。preview_provider を渡した時だけ繋がる。"""
        if current is None:
            self.preview.setPlainText("")
            return
        _name, data = self._items[current.data(Qt.UserRole)]
        try:
            self.preview.setPlainText(self._preview_provider(data) or "")
        except Exception as e:
            # プレビューが作れないだけで選べなくなるのは行き過ぎ。理由は残しつつ続ける。
            print(f"[tray-tools] プレビューを作れません: {e}", file=sys.stderr)
            self.preview.setPlainText("(プレビューを作れませんでした)")

    def _refresh_list(self, text: str):
        """前方一致(大文字小文字を無視)で候補そのものを減らす。絞り込んだ結果が
        見た目に残っていると ↑↓ で選べてしまい、Enterの行き先が分からなくなる。"""
        keyword = text.strip().lower()
        self.list.clear()
        for index, (name, _data) in enumerate(self._items):
            if keyword and not name.lower().startswith(keyword):
                continue
            item = QListWidgetItem(name)
            # データそのものではなく元リストの添字を持たせる。Qtの項目データは
            # QVariant を経由するため、タプルなどはPython側の型が変わって戻ることがある。
            item.setData(Qt.UserRole, index)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _move_selection(self, step: int):
        count = self.list.count()
        if count == 0:
            return
        row = (self.list.currentRow() + step) % count
        self.list.setCurrentRow(row)

    def _commit(self):
        if self._committing:
            return
        item = self.list.currentItem()
        if item is None:
            return
        self._committing = True
        name, data = self._items[item.data(Qt.UserRole)]
        try:
            # 閉じる前に呼ぶ。on_accept が QInputDialog を出す場合、まだ生きている
            # このウインドウを親にできる(閉じた後だとダイアログの位置と前面化が怪しくなる)。
            self._on_accept(name, data)
        except Exception as e:
            # Qtのスロット内で例外を投げ切るとアプリごと落ちうる。1件の失敗で常駐を
            # 巻き添えにしないよう、ここで止めて標準エラーに残す。
            print(f"[tray-tools] 選択の処理に失敗しました ({name}): {e}", file=sys.stderr)
        finally:
            self.close()
