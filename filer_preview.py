# filer_preview.py
# あっふの F3「プレビュー」(Files / OneCommander / yazi の横の見本に当たるもの)。
#
# ON にすると反対窓の場所をこの窓に差し替え、アクティブ窓のカーソルの中身を見せる。
#   - テキスト(ビュアと同じ拡張子の表): 先頭 200 行まで(読むのは最大 256KB)
#   - 画像(png jpg jpeg gif bmp webp ico): 窓に収まる大きさで読む(QImageReader に
#     縮めて読ませるので、巨大な画像でも全部を展開しない)。EXIF の向きは自動で当てる
#     (JPEG / TIFF は Qt、Qt が読まない PNG / WebP は自前)。手動の回転・反転
#     (R / L / H / V。状態は filer_core.ImageTurn)は表示のときに PreviewWidget が当てる
#   - フォルダ: 中のフォルダ・ファイルの数と、先頭の名前
#   - そのほか: 名前・サイズ・日時・属性
# 読み込みは load_preview を読み取りスレッドで呼ぶ(ネットワーク越しでも窓は止まらない)。
# QImage はスレッドをまたいで渡してよい(QPixmap にするのは UI スレッドで)。
import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont, QImage, QImageIOHandler, QImageReader, QPixmap, QTransform
from PySide6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

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


def turned_image(image: QImage, turn) -> QImage:
    """core の向きの行列(TURN_*)を当てた絵。元のままならそのまま返す。
    QImage.transformed は回したぶんの平行移動を自分で補うので、行列だけ渡せばよい。"""
    if tuple(turn) == core.TURN_IDENTITY or image.isNull():
        return image
    m11, m12, m21, m22 = turn
    return image.transformed(QTransform(m11, m12, m21, m22, 0, 0), Qt.FastTransformation)


def _read_image(path: str, box_w: int, box_h: int):
    """(絵, 表示の向きでの元の大きさ (幅, 高さ) or None, 縮めて読んだか)。

    EXIF の向き(カメラ・スマホの縦撮り)は当ててから返す。JPEG / TIFF は Qt の
    自動回転(setAutoTransform)で、Qt が EXIF を読まない WebP / PNG は自前で読んで
    (core.container_exif_orientation)当てる。"""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    transformation = reader.transformation()
    qt_rotated = bool(transformation & QImageIOHandler.TransformationRotate90)
    own_turn = core.TURN_IDENTITY
    if transformation == QImageIOHandler.TransformationNone:
        own_turn = core.EXIF_ORIENTATION_TURNS.get(
            core.container_exif_orientation(path), core.TURN_IDENTITY)
    rotated = qt_rotated or core.turn_swaps_size(own_turn)
    scaled = False
    if size.isValid():
        # EXIF の回転(スマホの縦撮り写真など)があると、読み出した後に
        # 縦横が入れ替わる。縮小の大きさは「表示後の縦横」で決めないと、
        # 回転したぶん箱からはみ出し、窓の大きさまで変わってしまう。
        # setScaledSize は回転の前(保存されている向き)の大きさで渡す。
        shown = QSize(size.height(), size.width()) if rotated else size
        w, h = core.fit_size(shown.width(), shown.height(), box_w, box_h)
        target = shown.scaled(w, h, Qt.KeepAspectRatio)
        if rotated:
            target = QSize(target.height(), target.width())
        if target != size:
            reader.setScaledSize(target)
            scaled = True
    image = reader.read()
    if image.isNull():
        raise OSError(reader.errorString() or "画像として読めません")
    image = turned_image(image, own_turn)
    if size.isValid():
        original = (size.height(), size.width()) if rotated else (size.width(), size.height())
    else:
        original = None
    return image, original, scaled


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
            image, original, scaled = _read_image(path, *box)
            result.update(image=image, note=f"{original[0]}×{original[1]}" if original else "",
                          scaled=scaled)
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
        # 絵の大きさで窓側の寸法が決まらないようにする(寸法は窓が決める)
        self.image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.image.setStyleSheet(f"background-color: {colors['background']};")
        self.stack.addWidget(self.text)
        self.stack.addWidget(self.image)
        layout.addWidget(self.stack, 1)
        self.last = None
        self.turn = core.TURN_IDENTITY      # 手動の回転・反転(R / L / H / V)
        self.turn_suffix = ""               # 回転固定の印(_R / _L)

    def image_box(self):
        size = self.stack.size()
        return max(size.width() - 8, 16), max(size.height() - 8, 16)

    def show_loading(self, title: str) -> None:
        suffix = getattr(self, "turn_suffix", "")
        self.header.setText(f"Preview: {title}  …" + (f"  {suffix}" if suffix else ""))

    def set_turn(self, turn, suffix: str = "", rerender: bool = True) -> None:
        """手動の回転・反転(core.ImageTurn の turn)と固定の印。rerender なら、いま出して
        いる絵にも当て直す(別のファイルへ移るときは、古い絵を向きだけ変えて一瞬見せない
        よう rerender=False で次の結果を待つ)。"""
        self.turn = tuple(turn)
        self.turn_suffix = suffix
        if rerender and self.last is not None:
            self.show_result(self.last)

    def show_result(self, result: dict) -> None:
        self.last = result
        note = result.get("note") or ""
        image = result.get("image")
        turn = getattr(self, "turn", core.TURN_IDENTITY)
        if image is not None:
            label = core.turn_label(turn)
            if label:
                note = f"{note} {label}" if note else label
        suffix = getattr(self, "turn_suffix", "")
        self.header.setText(f"Preview: {result.get('title', '')}" + (f"  [{note}]" if note else "")
                            + (f"  {suffix}" if suffix else ""))
        if image is not None:
            pixmap = QPixmap.fromImage(turned_image(image, turn))
            box_w, box_h = self.image_box()
            if pixmap.width() > box_w or pixmap.height() > box_h:
                # 大きさの分からない形式など、箱に収まらない絵が来ても
                # ここで収める(窓の大きさを絵に合わせない)
                pixmap = pixmap.scaled(box_w, box_h, Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
            self.image.setPixmap(pixmap)
            self.stack.setCurrentWidget(self.image)
        else:
            self.text.setPlainText("\n".join(result.get("lines") or []))
            self.stack.setCurrentWidget(self.text)
