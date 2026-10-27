# filer_viewer.py
# あっふの「簡易テキストビュア」(あふｗの簡易内蔵テキストビュアに寄せたもの)。
#
# 何百 MB のログでも固まらないことをいちばんに作ってある。
#   - 全文を読み込まない。別スレッドでファイルを先頭から読んで「行の始まりの位置」の
#     索引だけを作り、画面には見えている行だけをその都度ファイルから読んで描く
#     (自前の QAbstractScrollArea。QPlainTextEdit は全文を抱えるので使わない)
#   - 索引ができた所までは、作っている最中でもスクロール・表示できる
#   - 検索も別スレッド。Esc で止められる
#   - ファイルは読む人ごとに自分で開く(索引作り・検索・表示がそれぞれ別のハンドル)。
#     1つのハンドルや mmap を取り合わないので、鍵も閉じる順番も要らない
#
# 文字コードは BOM → UTF-8 として正しいか(先頭の数 MB) → cp932 の順に決める。
# 手で切り替えるキーはあふｗと同じ(Shift+O/S/U/I/E/J)。
import codecs
import os
import threading
import time
from array import array

from PySide6.QtCore import QEvent, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QInputDialog,
    QLabel,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------
# 文字コード(Qt を使わない部分。tests/test_filer.py で確かめる)
# ---------------------------------------------------------------
# 表示名と Python のコーデック名。キーはあふｗのテキストビュアの切り替えキー。
ENCODING_KEYS = {
    "O": "utf-8",
    "S": "cp932",
    "U": "utf-16-le",
    "I": "utf-16-be",
    "E": "euc_jp",
    "J": "iso2022_jp",
}
ENCODING_LABELS = {
    "utf-8": "UTF-8",
    "cp932": "SJIS",
    "utf-16-le": "UTF-16LE",
    "utf-16-be": "UTF-16BE",
    "euc_jp": "EUC-JP",
    "iso2022_jp": "JIS",
}
DETECT_BYTES = 4 * 1024 * 1024   # UTF-8 として正しいかを見る長さ
INDEX_CHUNK = 4 * 1024 * 1024    # 索引作りで一度に読む長さ
MAX_LINE_BYTES = 64 * 1024       # 1行を表示・検索に読む上限(1行が巨大なファイル対策)


def detect_encoding(head: bytes):
    """(コーデック名, 本文の始まりの位置)。BOM があればそれ、無ければ UTF-8 として
    正しいか(途中で切れた最後の1文字は見逃す)、だめなら cp932。"""
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8", 3
    if head.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", 2
    if head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", 2
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        decoder.decode(head, final=False)
        return "utf-8", 0
    except UnicodeDecodeError:
        return "cp932", 0


def bom_length(head: bytes, encoding: str) -> int:
    """手で切り替えたときの本文の始まり。その文字コードの BOM があれば飛ばす。"""
    if encoding == "utf-8" and head.startswith(codecs.BOM_UTF8):
        return 3
    if encoding == "utf-16-le" and head.startswith(codecs.BOM_UTF16_LE):
        return 2
    if encoding == "utf-16-be" and head.startswith(codecs.BOM_UTF16_BE):
        return 2
    return 0


def newline_of(encoding: str) -> bytes:
    """その文字コードでの改行(LF)のバイト列。行の区切りはこれで探す。"""
    if encoding == "utf-16-le":
        return b"\n\x00"
    if encoding == "utf-16-be":
        return b"\x00\n"
    return b"\n"


def scan_line_starts(data: bytes, base: int, newline: bytes, align: int, out, size: int) -> None:
    """data(ファイルの base バイト目から)の中の改行を探し、その次の位置(＝次の行の
    始まり)を out に足す。UTF-16 は2バイト単位なので、偶数位置(align)の一致だけを
    数える(文字の途中に見えた一致を取り違えない)。末尾の改行の後ろ(ファイルの終わり)は
    行として足さない。"""
    step = len(newline)
    pos = data.find(newline)
    while pos >= 0:
        if align <= 1 or (base + pos - align_origin(out)) % align == 0:
            start = base + pos + step
            if start < size:
                out.append(start)
            pos = data.find(newline, pos + step)
        else:
            pos = data.find(newline, pos + 1)


def align_origin(out) -> int:
    """UTF-16 の偶奇を数える基準(本文の始まり＝索引の先頭)。"""
    return out[0] if len(out) else 0


def decode_line(raw: bytes, encoding: str) -> str:
    """1行ぶんのバイト列を文字にする(読めないバイトは置換文字)。行末の改行は落とす。"""
    text = raw.decode(encoding, errors="replace")
    return text.rstrip("\r\n")


def find_in_line(text: str, word: str):
    """大文字小文字を無視して word を探す。見つかった桁(文字単位)か None。"""
    if not word:
        return None
    index = text.casefold().find(word.casefold())
    return index if index >= 0 else None


# ---------------------------------------------------------------
# 索引
# ---------------------------------------------------------------
class LineIndex:
    """行の始まりのバイト位置の並び。別スレッドで作り、作っている間も読める。

    starts[i] が i 行目の始まり。i 行目の終わりは starts[i+1]、最後の行はファイルの
    終わり。作っている最中は最後の行の終わりが分からないので、数えるのは確定した
    行(len(starts) - 1)まで。"""

    def __init__(self, path: str, encoding: str, data_start: int):
        self.path = path
        self.encoding = encoding
        self.data_start = data_start
        self.size = os.path.getsize(path)
        self.starts = array("Q", [data_start])
        self.done = False
        self.error = None
        self.scanned = data_start
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._build, daemon=True, name="viewer-index")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _build(self) -> None:
        newline = newline_of(self.encoding)
        align = 2 if self.encoding.startswith("utf-16") else 1
        try:
            with open(self.path, "rb") as f:
                f.seek(self.data_start)
                base = self.data_start
                carry = b""
                while not self._stop.is_set():
                    chunk = f.read(INDEX_CHUNK)
                    if not chunk:
                        break
                    data = carry + chunk
                    data_base = base - len(carry)
                    # 全部を探す。境目で割れた改行(UTF-16 の2バイト)は今回は見つからない
                    # ので、最後の (改行の長さ-1) バイトを次へ持ち越して次の回で拾う。
                    # 持ち越した部分で始まる完全な一致は今回あり得ないので、二重にならない。
                    scan_line_starts(data, data_base, newline, align, self.starts, self.size)
                    keep = len(newline) - 1
                    carry = data[len(data) - keep:] if keep else b""
                    base += len(chunk)
                    self.scanned = base
        except OSError as e:
            self.error = str(e)
        self.scanned = self.size
        self.done = True

    def count(self) -> int:
        """表示してよい行数。作っている間は確定した行だけ。"""
        if self.done:
            return 0 if self.size <= self.data_start else len(self.starts)
        return max(len(self.starts) - 1, 0)

    def progress(self) -> float:
        if self.done or self.size <= self.data_start:
            return 1.0
        return min(1.0, (self.scanned - self.data_start) / (self.size - self.data_start))

    def span(self, line: int):
        """line 行目の (始まり, 長さ)。"""
        start = self.starts[line]
        end = self.starts[line + 1] if line + 1 < len(self.starts) else self.size
        return start, min(end - start, MAX_LINE_BYTES)


class LineReader:
    """表示や検索のために行を読む(自分のハンドルを持つ。スレッドごとに1つ作る)。"""

    def __init__(self, index: LineIndex):
        self.index = index
        self._file = open(index.path, "rb")

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass

    def line(self, number: int) -> str:
        start, length = self.index.span(number)
        self._file.seek(start)
        return decode_line(self._file.read(length), self.index.encoding)

    def block(self, begin: int, end: int) -> str:
        """begin 行目から end 行目の手前までをまとめて文字にする(検索用。1行ずつ読むより
        桁違いに速い)。行の区切りの "\\n" は残す。"""
        starts = self.index.starts
        start = starts[begin]
        stop = starts[end] if end < len(starts) else self.index.size
        self._file.seek(start)
        return self._file.read(stop - start).decode(self.index.encoding, errors="replace")


SEARCH_BLOCK_LINES = 4000


def search_lines(reader, index, word: str, start: int, backward: bool, cancel) -> tuple:
    """word を start 行目から下(上)へ探す。("hit", (行, 桁, 長さ)) / ("none", word) /
    ("cancel", word)。索引がまだ届いていない所は、届くのを待ちながら進む。

    数千行ずつまとめて読んで小文字にして当たりを付け、当たった行だけ1行で確かめ直す
    (小文字化で文字数が変わる字があっても、桁は1行の方で正しく出す)。"""
    needle = word.lower()
    if backward:
        line = min(start, index.count() - 1)
        while line >= 0:
            if cancel.is_set():
                return ("cancel", word)
            begin = max(0, line - SEARCH_BLOCK_LINES + 1)
            text = reader.block(begin, line + 1).lower()
            pos = text.rfind(needle)
            while pos >= 0:
                hit = begin + text.count("\n", 0, pos)
                col = find_in_line(reader.line(hit), word)
                if col is not None:
                    return ("hit", (hit, col, len(word)))
                line_start = text.rfind("\n", 0, pos)
                if line_start < 0:
                    break
                pos = text.rfind(needle, 0, line_start)
            line = begin - 1
        return ("none", word)
    line = max(start, 0)
    while not cancel.is_set():
        count = index.count()
        if line >= count:
            if index.done:
                return ("none", word)
            time.sleep(0.05)
            continue
        end = min(line + SEARCH_BLOCK_LINES, count)
        text = reader.block(line, end).lower()
        pos = text.find(needle)
        while pos >= 0:
            hit = line + text.count("\n", 0, pos)
            col = find_in_line(reader.line(hit), word)
            if col is not None:
                return ("hit", (hit, col, len(word)))
            next_line = text.find("\n", pos)
            if next_line < 0:
                break
            pos = text.find(needle, next_line + 1)
        line = end
    return ("cancel", word)


# ---------------------------------------------------------------
# 表示
# ---------------------------------------------------------------
class TextArea(QAbstractScrollArea):
    """見えている行だけを描く。折り返さず、横はスクロール。"""

    def __init__(self, viewer):
        super().__init__(viewer)
        self.viewer = viewer
        self.setFocusPolicy(Qt.StrongFocus)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)
        self.horizontalScrollBar().valueChanged.connect(self.viewport().update)
        self._max_width = 0

    def line_height(self) -> int:
        return max(QFontMetrics(self.font()).height(), 1)

    def page_lines(self) -> int:
        return max(1, self.viewport().height() // self.line_height())

    def gutter_width(self) -> int:
        if not self.viewer.show_numbers:
            return 4
        digits = len(str(max(self.viewer.line_count(), 1)))
        return QFontMetrics(self.font()).horizontalAdvance("9" * (digits + 1)) + 8

    def update_ranges(self) -> None:
        count = self.viewer.line_count()
        bar = self.verticalScrollBar()
        bar.setRange(0, max(0, count - self.page_lines()))
        bar.setPageStep(self.page_lines())
        hbar = self.horizontalScrollBar()
        hbar.setRange(0, max(0, self._max_width + self.gutter_width() - self.viewport().width()))
        hbar.setPageStep(max(1, self.viewport().width() // 2))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_ranges()

    def event(self, event):
        # キーはビュアがまとめて扱う(Tab でフォーカスが動いたりしないように)。
        if event.type() == QEvent.KeyPress:
            try:
                self.viewer.handle_key(event)
            except Exception as e:  # noqa: BLE001  スロットで投げ切ると落ちる
                self.viewer.set_status(f"エラー: {e}")
            return True
        return super().event(event)

    def paintEvent(self, _event):
        viewer = self.viewer
        painter = QPainter(self.viewport())
        try:
            colors = viewer.colors
            painter.fillRect(self.viewport().rect(), QColor(colors["background"]))
            painter.setFont(self.font())
            fm = QFontMetrics(self.font())
            lh = self.line_height()
            top = self.verticalScrollBar().value()
            gutter = self.gutter_width()
            dx = gutter - self.horizontalScrollBar().value()
            count = viewer.line_count()
            width_seen = self._max_width
            for row in range(self.page_lines() + 1):
                number = top + row
                if number >= count:
                    break
                y = row * lh
                text = viewer.display_line(number)
                hit = viewer.highlight
                if hit is not None and hit[0] == number:
                    _line, col, length = hit
                    raw = viewer.raw_line(number)
                    before = raw[:col].expandtabs(viewer.tab_width)
                    word = raw[col:col + length].expandtabs(viewer.tab_width)
                    x0 = dx + fm.horizontalAdvance(before)
                    painter.fillRect(QRect(x0, y, max(fm.horizontalAdvance(word), 2), lh),
                                     QColor(colors["search"]))
                painter.setPen(QColor(colors["text"]))
                painter.drawText(dx, y + fm.ascent(), text)
                width_seen = max(width_seen, fm.horizontalAdvance(text))
                if viewer.show_numbers:
                    painter.fillRect(QRect(0, y, gutter - 4, lh), QColor(colors["gutter"]))
                    painter.setPen(QColor(colors["line_number"]))
                    painter.drawText(QRect(0, y, gutter - 8, lh),
                                     Qt.AlignRight | Qt.AlignVCenter, str(number + 1))
            if width_seen != self._max_width:
                self._max_width = width_seen
                QTimer.singleShot(0, self.update_ranges)
        finally:
            painter.end()


DEFAULT_VIEWER_COLORS = {
    "background": "#00001c",
    "text": "#ffffff",
    "gutter": "#0a0a30",
    "line_number": "#8080a0",
    "search": "#806000",
    "status_background": "#1a3a8a",
    "status_text": "#ffffff",
}


class TextViewer(QWidget):
    """1ファイルぶんのビュア窓。モードレスで、いくつ開いてもよい。"""

    closed = Signal(object)

    def __init__(self, path: str, title_name: str = None, font: QFont = None,
                 colors: dict = None, editor_callback=None, geometry=None, parent=None):
        super().__init__(parent, Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.path = path
        self.title_name = title_name or os.path.basename(path)
        self.colors = dict(DEFAULT_VIEWER_COLORS)
        if colors:
            self.colors.update({k: v for k, v in colors.items() if k in self.colors and v})
        self.editor_callback = editor_callback
        self.show_numbers = False
        self.tab_width = 4
        self.highlight = None          # (行, 桁, 長さ)
        self.search_word = ""
        self._search_thread = None
        self._search_cancel = None
        self._search_result = None     # 検索スレッドから UI へ渡す(タイマーで拾う)
        self._cache = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.area = TextArea(self)
        if font is not None:
            self.area.setFont(QFont(font))
        layout.addWidget(self.area, 1)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setContentsMargins(6, 1, 6, 1)
        self.status.setStyleSheet(f"background-color: {self.colors['status_background']};"
                                  f" color: {self.colors['status_text']};")
        layout.addWidget(self.status)
        self._message = ""

        with open(path, "rb") as f:
            self._head = f.read(DETECT_BYTES)
        encoding, start = detect_encoding(self._head)
        self.index = None
        self.reader = None
        self._open_index(encoding, start)

        if geometry:
            self.setGeometry(*geometry)
        else:
            self.resize(900, 650)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(100)
        self._refresh_title()

    # --- 索引と行 ---
    def _open_index(self, encoding: str, start: int) -> None:
        if self.index is not None:
            self.index.stop()
        if self.reader is not None:
            self.reader.close()
        self._cache.clear()
        self.highlight = None
        self.index = LineIndex(self.path, encoding, start)
        self.reader = LineReader(self.index)
        self.area.update_ranges()
        self.area.viewport().update()

    @property
    def encoding(self) -> str:
        return self.index.encoding

    def set_encoding(self, encoding: str) -> None:
        self._cancel_search()
        top = self.area.verticalScrollBar().value()
        self._open_index(encoding, bom_length(self._head, encoding))
        self.area.verticalScrollBar().setValue(top)
        self._refresh_title()

    def line_count(self) -> int:
        return self.index.count() if self.index is not None else 0

    def raw_line(self, number: int) -> str:
        text = self._cache.get(number)
        if text is None:
            if len(self._cache) > 4000:
                self._cache.clear()
            try:
                text = self.reader.line(number)
            except (OSError, IndexError):
                text = ""
            self._cache[number] = text
        return text

    def display_line(self, number: int) -> str:
        return self.raw_line(number).expandtabs(self.tab_width)

    def _tick(self) -> None:
        """索引作りと検索の進み具合を拾う(別スレッドから Qt を触らないため、こちらから見に行く)。"""
        try:
            if self.index is not None:
                self.area.update_ranges()
                if not self.index.done:
                    self.area.viewport().update()
                self._refresh_title()
            result = self._search_result
            if result is not None:
                self._search_result = None
                self._on_search_result(result)
        except Exception as e:  # noqa: BLE001
            self.set_status(f"エラー: {e}")

    def _refresh_title(self) -> None:
        label = ENCODING_LABELS.get(self.encoding, self.encoding)
        if self.index.done:
            lines = f"{self.line_count():,}行"
        else:
            lines = f"索引作成中 {self.index.progress() * 100:.0f}%（{self.line_count():,}行〜）"
        self.setWindowTitle(f"{self.title_name} - {label} - {lines}")
        top = self.area.verticalScrollBar().value() + 1
        status = f"{label}  {lines}  {top:,}行目  TAB{self.tab_width}"
        if self.index.error:
            status += f"  読み取りエラー: {self.index.error}"
        if self._message:
            status += f"  {self._message}"
        self.status.setText(status)

    def set_status(self, message: str) -> None:
        self._message = message
        self._refresh_title()

    # --- 移動 ---
    def scroll_to(self, line: int) -> None:
        self.area.update_ranges()
        bar = self.area.verticalScrollBar()
        bar.setValue(max(0, min(line, bar.maximum())))
        self._refresh_title()

    def top_line(self) -> int:
        return self.area.verticalScrollBar().value()

    def go_top(self) -> None:
        self.scroll_to(0)

    def go_bottom(self) -> None:
        # 索引を作っている途中なら、いま分かっている末尾まで。
        self.scroll_to(self.line_count())

    def jump_line(self, number: int) -> None:
        """1 始まりの行番号へ(その行を画面の上端に)。"""
        self.scroll_to(max(number - 1, 0))

    # --- 検索 ---
    def start_search(self, word: str, backward: bool) -> None:
        """word を下(上)へ探す。見つかった行へ移り、該当箇所を強調する。

        探すのは別スレッド。索引がまだ届いていない所は、届くのを待ちながら進む。"""
        if not word:
            return
        self._cancel_search()
        self.search_word = word
        if self.highlight is not None:
            start = self.highlight[0] + (-1 if backward else 1)
        else:
            start = self.top_line() + (-1 if backward else 0)
        cancel = threading.Event()
        self._search_cancel = cancel
        index = self.index
        self.set_status(f"検索中: {word}")

        def worker():
            reader = LineReader(index)
            try:
                self._search_result = search_lines(reader, index, word, start, backward, cancel)
            except Exception as e:  # noqa: BLE001
                self._search_result = ("error", str(e))
            finally:
                reader.close()

        self._search_thread = threading.Thread(target=worker, daemon=True, name="viewer-search")
        self._search_thread.start()

    def searching(self) -> bool:
        return self._search_thread is not None and self._search_thread.is_alive()

    def _cancel_search(self) -> None:
        if self._search_cancel is not None:
            self._search_cancel.set()
        self._search_cancel = None

    def _on_search_result(self, result) -> None:
        kind, value = result
        if kind == "hit":
            self.highlight = value
            line = value[0]
            # ヒット行が画面の上の方に来るように(上から3行目あたり)。
            if not (self.top_line() <= line < self.top_line() + self.area.page_lines() - 1):
                self.scroll_to(max(line - 2, 0))
            self.set_status(f"検索: {self.search_word}  {line + 1:,}行目")
            self.area.viewport().update()
        elif kind == "none":
            self.set_status(f"見つかりません: {value}")
        elif kind == "cancel":
            self.set_status("検索を中止しました")
        else:
            self.set_status(f"検索できません: {value}")

    # --- 入力の口(テストではここを差し替える) ---
    def ask_word(self, title: str):
        text, ok = QInputDialog.getText(self, title, "検索する文字列", text=self.search_word)
        return text if ok and text else None

    def ask_line(self):
        number, ok = QInputDialog.getInt(self, "行番号ジャンプ", "行番号",
                                         self.top_line() + 1, 1, max(self.line_count(), 1))
        return number if ok else None

    # --- キー(AFXWKEY.TXT の「簡易内蔵テキストビュア画面」に寄せる) ---
    def handle_key(self, event) -> None:
        key = event.key()
        mods = event.modifiers()
        shift = bool(mods & Qt.ShiftModifier)
        ctrl = bool(mods & Qt.ControlModifier)
        letter = chr(int(key)) if int(Qt.Key_A) <= int(key) <= int(Qt.Key_Z) else ""
        page = self.area.page_lines()

        if key == Qt.Key_Escape:
            # 検索中の Esc は検索の中止(あふｗの「各処理の中止」)。それ以外は閉じる。
            if self.searching():
                self._cancel_search()
            else:
                self.close()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if shift:
                self.open_editor()
            else:
                self.close()
            return
        if key == Qt.Key_Backspace or (letter == "Q" and not shift):
            self.close()        # BS はユーザーの AFXW.key で Enter(終了)に置き換え済み
            return
        if shift and not ctrl and letter in ENCODING_KEYS:
            self.set_encoding(ENCODING_KEYS[letter])
            return
        if key in (Qt.Key_Up, Qt.Key_Down) and (shift or ctrl):
            self.change_font(-1 if key == Qt.Key_Up else 1)
            return
        if key in (Qt.Key_Insert, Qt.Key_Delete):
            self.change_font(-1 if key == Qt.Key_Insert else 1)
            return
        if key == Qt.Key_Up:
            self.scroll_to(self.top_line() - 1)
        elif key == Qt.Key_Down:
            self.scroll_to(self.top_line() + 1)
        elif key in (Qt.Key_PageUp, Qt.Key_Left) and ctrl:
            self.go_top()
        elif key in (Qt.Key_PageDown, Qt.Key_Right) and ctrl:
            self.go_bottom()
        elif key in (Qt.Key_PageUp, Qt.Key_Left):
            self.scroll_to(self.top_line() - page)
        elif key in (Qt.Key_PageDown, Qt.Key_Right):
            self.scroll_to(self.top_line() + page)
        elif key in (Qt.Key_Home, Qt.Key_F1):
            self.go_top()
        elif key in (Qt.Key_End, Qt.Key_F2):
            self.go_bottom()
        elif (letter == "J" or key == Qt.Key_F3) and not shift:
            number = self.ask_line()
            if number is not None:
                self.jump_line(number)
        elif (letter == "F" or key == Qt.Key_F4) and not shift:
            self._search_new(backward=False)           # 下方単語検索
        elif ((letter == "F" or key == Qt.Key_F4) and shift) or (letter == "B" and not shift):
            self._search_new(backward=True)            # 上方単語検索
        elif (key in (Qt.Key_Space, Qt.Key_F5) or letter == "L") and shift:
            self.start_search(self.search_word, backward=True)    # 上方次検索
        elif letter == "H" and not shift:
            self.start_search(self.search_word, backward=True)    # 上方次検索
        elif key in (Qt.Key_Space, Qt.Key_F5) or letter == "L":
            self.start_search(self.search_word, backward=False)   # 下方次検索
        elif key in (Qt.Key_2, Qt.Key_4, Qt.Key_8) and not shift:
            self.tab_width = {Qt.Key_2: 2, Qt.Key_4: 4, Qt.Key_8: 8}[key]
            self.area.viewport().update()
            self._refresh_title()
        elif letter == "N" and shift:
            self.show_numbers = not self.show_numbers
            self.area.update_ranges()
            self.area.viewport().update()
        elif letter == "E" and not shift:
            self.open_editor()
        elif letter == "C" and ctrl:
            self.copy_visible()

    def _search_new(self, backward: bool) -> None:
        word = self.ask_word("上方検索" if backward else "下方検索")
        if word:
            self.highlight = None
            self.start_search(word, backward=backward)

    def change_font(self, step: int) -> None:
        font = QFont(self.area.font())
        size = max(8, min(32, font.pointSize() + step))
        font.setPointSize(size)
        self.area.setFont(font)
        self.area.update_ranges()
        self.area.viewport().update()

    def open_editor(self) -> None:
        if self.editor_callback is not None:
            self.editor_callback(self.path)

    def visible_text(self) -> str:
        top = self.top_line()
        last = min(top + self.area.page_lines(), self.line_count())
        return "\r\n".join(self.raw_line(n) for n in range(top, last))

    def copy_visible(self) -> None:
        """Ctrl+C: 画面に見えている行をまとめてクリップボードへ(キャレットや範囲選択は
        持たないので、見えている所を写す)。"""
        QGuiApplication.clipboard().setText(self.visible_text())
        self.set_status("見えている行をコピーしました")

    def closeEvent(self, event):
        try:
            self._cancel_search()
            self._timer.stop()
            if self.index is not None:
                self.index.stop()
            if self.reader is not None:
                self.reader.close()
            geometry = self.geometry()
            self.closed.emit([geometry.x(), geometry.y(), geometry.width(), geometry.height()])
        except Exception:  # noqa: BLE001  閉じられなくなるのが最悪
            pass
        super().closeEvent(event)
