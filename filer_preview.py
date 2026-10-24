# filer_preview.py
# 簡易ファイラの F3「プレビュー」(Files / OneCommander / yazi の横の見本に当たるもの)。
#
# ON にすると反対窓の場所をこの窓に差し替え、アクティブ窓のカーソルの中身を見せる。
#   - テキスト(ビュアと同じ拡張子の表): 先頭 200 行まで(読むのは最大 256KB)
#   - 画像(png jpg jpeg gif bmp webp ico): 窓に収まる大きさで読む(QImageReader に
#     縮めて読ませるので、巨大な画像でも全部を展開しない)
#   - フォルダ: 中のフォルダ・ファイルの数と、先頭の名前
#   - そのほか: 名前・サイズ・日時・属性
# 読み込みは load_preview を読み取りスレッドで呼ぶ(ネットワーク越しでも窓は止まらない)。
# QImage はスレッドをまたいで渡してよい(QPixmap にするのは UI スレッドで)。
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QImageReader, QPixmap
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QStackedWidget, QVBoxLayout, QWidget

import filer_archive
import filer_core as core


def _read_text(path: str):
    import filer_viewer  # 文字コードの判定はビュアと同じものを使う

    with open(path, "rb") as f:
        data = f.read(core.PREVIEW_TEXT_BYTES)
    encoding, start = filer_viewer.detect_encoding(data)
    text = data[start:].decode(encoding, errors="replace")
    lines = text.splitlines()[:core.PREVIEW_TEXT_LINES]
    label = filer_viewer.ENCODING_LABELS.get(encoding, encoding)
    return lines, label


def _read_image(path: str, box_w: int, box_h: int):
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid():
        w, h = core.fit_size(size.width(), size.height(), box_w, box_h)
        reader.setScaledSize(size.scaled(w, h, Qt.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise OSError(reader.errorString() or "画像として読めません")
    return image, (size.width(), size.height()) if size.isValid() else None


def _read_folder(path: str):
    dirs, files, names = 0, 0, []
    with os.scandir(path) as it:
        for entry in it:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                dirs += 1
            else:
                files += 1
            names.append((not is_dir, entry.name.casefold(), entry.name + ("\\" if is_dir else "")))
    names.sort()
    shown = [n for _d, _k, n in names[:core.PREVIEW_FOLDER_NAMES]]
    return dirs, files, shown


def load_preview(path: str, kind: str, entry, box=(400, 400), archive=None) -> dict:
    """プレビューの中身を作る(読み取りスレッドで呼ぶ)。

    archive は (書庫ファイル, 中のフォルダ) で、書庫の中のファイルのとき。小さいものだけ
    一時フォルダへ取り出して読み、大きいものは情報だけにする。"""
    result = {"kind": kind, "title": entry.name if entry else "", "lines": [], "image": None}
    try:
        if archive is not None and kind in ("text", "image"):
            if entry.size > core.PREVIEW_ARCHIVE_MAX:
                result.update(kind="info", lines=core.info_lines(entry.name, entry)
                              + ["", "（書庫の中の大きなファイルは中身を出しません）"])
                return result
            index = filer_archive.get_index(archive[0])
            plan = filer_archive.plan_extract(index, archive[1], [entry.name])
            if filer_archive.needs_password(index, plan):
                result.update(kind="info", lines=core.info_lines(entry.name, entry)
                              + ["", "（パスワード付きなので中身を出しません）"])
                return result
            dest = filer_archive.make_temp_dir()
            outcome = filer_archive.extract(index, plan, dest)
            if outcome.errors:
                raise OSError(outcome.errors[0])
            path = os.path.join(dest, entry.name)
        if kind == "text":
            lines, label = _read_text(path)
            result.update(lines=lines, note=label)
        elif kind == "image":
            image, original = _read_image(path, *box)
            result.update(image=image, note=f"{original[0]}×{original[1]}" if original else "")
        elif kind == "folder":
            dirs, files, names = _read_folder(path)
            result.update(lines=[f"{dirs} フォルダ / {files} ファイル", ""] + names
                          + (["…"] if dirs + files > len(names) else []))
        elif kind == "info":
            result.update(lines=core.info_lines(entry.name, entry))
    except Exception as e:  # noqa: BLE001  見本が作れないだけ。窓は止めない
        result.update(kind="info", image=None,
                      lines=(core.info_lines(entry.name, entry) if entry else [])
                      + ["", f"（読めません: {e}）"])
    return result


class PreviewWidget(QWidget):
    """反対窓の場所に出すプレビュー。キーは受け取らない(フォーカスはファイル窓のまま)。"""

    def __init__(self, parent, colors: dict, font: QFont):
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.colors = colors
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QLabel("Preview")
        self.header.setTextFormat(Qt.PlainText)
        self.header.setFont(font)
        self.header.setStyleSheet(f"background-color: {colors['header_inactive']};"
                                  f" color: {colors['header_inactive_text']}; padding: 0 4px;")
        layout.addWidget(self.header)
        self.stack = QStackedWidget()
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFocusPolicy(Qt.NoFocus)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setFont(font)
        self.text.setStyleSheet(f"background-color: {colors['background']};"
                                f" color: {colors['text']}; border: none;")
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setStyleSheet(f"background-color: {colors['background']};")
        self.stack.addWidget(self.text)
        self.stack.addWidget(self.image)
        layout.addWidget(self.stack, 1)
        self.last = None

    def image_box(self):
        size = self.stack.size()
        return max(size.width() - 8, 16), max(size.height() - 8, 16)

    def show_loading(self, title: str) -> None:
        self.header.setText(f"Preview: {title}  …")

    def show_result(self, result: dict) -> None:
        self.last = result
        note = result.get("note")
        self.header.setText(f"Preview: {result.get('title', '')}" + (f"  [{note}]" if note else ""))
        if result.get("image") is not None:
            self.image.setPixmap(QPixmap.fromImage(result["image"]))
            self.stack.setCurrentWidget(self.image)
        else:
            self.text.setPlainText("\n".join(result.get("lines") or []))
            self.stack.setCurrentWidget(self.text)
