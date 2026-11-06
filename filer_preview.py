# filer_preview.py
# あっふの F3「プレビュー」(Files / OneCommander / yazi の横の見本に当たるもの)。
#
# ON にすると反対窓の場所をこの窓に差し替え、アクティブ窓のカーソルの中身を見せる。
#   - テキスト(ビュアと同じ拡張子の表): 先頭 200 行まで(読むのは最大 256KB)
#   - 画像(png jpg jpeg gif bmp webp ico): 窓に収まる大きさで読む(QImageReader に
#     縮めて読ませるので、巨大な画像でも全部を展開しない)。EXIF の向きは自動で当てる
#     (JPEG / TIFF は Qt、Qt が読まない PNG / WebP は自前。読み込みはスライドショーと
#     共通の image_load)。手動の回転・反転(R / L / H / V。状態は filer_core.ImageTurn)
#     は表示のときに PreviewWidget が当てる
#   - PDF: 1ページ目を絵にして出す(PySide6 の QtPdf。無ければ情報だけ)
#   - HTML / Markdown: QTextBrowser で整形して出す(相対パスの画像も読む。
#     リンクは辿らない)。読む範囲はテキストと同じ先頭 200 行まで
#   - フォルダ: 中のフォルダ・ファイルの数と、先頭の名前
#   - そのほか: 名前・サイズ・日時・属性
# 読み込みは load_preview を読み取りスレッドで呼ぶ(ネットワーク越しでも窓は止まらない)。
# QImage はスレッドをまたいで渡してよい(QPixmap にするのは UI スレッドで)。
import os

from PySide6.QtCore import QEvent, QUrl, Qt
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

import filer_archive
import filer_core as core
import image_load


def _read_text(path: str):
    import filer_viewer  # 文字コードの判定はビュアと同じものを使う

    with open(path, "rb") as f:
        data = f.read(core.PREVIEW_TEXT_BYTES)
    encoding, start = filer_viewer.detect_encoding(data)
    text = data[start:].decode(encoding, errors="replace")
    lines = text.splitlines()[:core.PREVIEW_TEXT_LINES]
    label = filer_viewer.ENCODING_LABELS.get(encoding, encoding)
    return lines, label


def _read_rich(path: str):
    """HTML / Markdown 用に、テキストと同じ範囲を1つの文字列で読む。"""
    lines, label = _read_text(path)
    return "\n".join(lines), label


def _read_pdf(path: str, box):
    """PDF の1ページ目を絵にする。(QImage, ページ数)。

    PySide6 の QtPdf を使う(追加の依存なし)。ドキュメントはこのスレッドの中で
    作って捨てる(スレッドをまたいで持たない)。QtPdf が無い環境では RuntimeError。"""
    try:
        from PySide6.QtPdf import QPdfDocument
    except ImportError as e:
        raise RuntimeError("PDF を表示できません（PySide6 の QtPdf がありません）") from e
    from PySide6.QtCore import QSize

    doc = QPdfDocument()
    error = doc.load(path)
    if error != QPdfDocument.Error.None_:
        raise RuntimeError(f"PDF を開けません（{error}）")
    pages = doc.pageCount()
    if pages <= 0:
        raise RuntimeError("PDF にページがありません")
    point = doc.pagePointSize(0)
    bw, bh = max(box[0], 16), max(box[1], 16)
    scale = min(bw / max(point.width(), 1.0), bh / max(point.height(), 1.0))
    size = QSize(max(1, int(point.width() * scale)), max(1, int(point.height() * scale)))
    image = doc.render(0, size)
    if image.isNull():
        raise RuntimeError("PDF の1ページ目を描けませんでした")
    return image, pages


# 画像の読み込み(縮小・EXIF の向き)はスライドショーと共通の image_load にある。
# ここでの名前(_read_image / turned_image)は今までどおり使えるように残す。
turned_image = image_load.turned_image
_read_image = image_load.read_image


def _photo_edits(path: str):
    """写真の補正（crs:）を読む（読み取りキャッシュ経由。無ければ None）。"""
    try:
        from photometa import metacache
        meta = metacache.read_meta_cached(path)
    except Exception:  # noqa: BLE001  補正が読めなくても見本は出す
        return None
    return meta.edits if meta else None


def _edited_image(path: str, edits, box):
    """補正を当てた画像を (QImage, 元の大きさ, 縮めたか) で返す。失敗したら None。

    image_load.read_image と同じ返し方。向きも含めて photometa.render で当てる。
    """
    try:
        from PIL import Image

        from photometa import render
        from PySide6.QtGui import QImage

        with Image.open(path) as im:
            img = render.apply_edits(im, edits)
            img.load()
        w, h = img.size
        bw, bh = box
        scaled = False
        if w > bw or h > bh:
            scale = min(bw / w, bh / h)
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                             Image.LANCZOS)
            scaled = True
        rgb = img.convert("RGB")
        qimg = QImage(rgb.tobytes(), rgb.size[0], rgb.size[1], rgb.size[0] * 3,
                      QImage.Format.Format_RGB888).copy()
        return qimg, (w, h), scaled
    except Exception:  # noqa: BLE001  当てられなければ通常の読み込みに任せる
        return None


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
    result = {"kind": kind, "title": entry.name if entry else "", "lines": [],
              "image": None, "rich": None, "path": path}
    try:
        if archive is not None and kind == "pdf":
            result.update(kind="info", lines=core.info_lines(entry.name, entry)
                          + ["", "（書庫の中の PDF は中身を出しません）"])
            return result
        if archive is not None and kind in ("text", "image", "html", "markdown"):
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
            result["path"] = path
        if kind == "text":
            lines, label = _read_text(path)
            result.update(lines=lines, note=label)
        elif kind == "pdf":
            image, pages = _read_pdf(path, box)
            result.update(image=image, note=f"PDF {pages}ページ・1ページ目")
        elif kind in ("html", "markdown"):
            text, label = _read_rich(path)
            result.update(rich=(kind, text), note=label)
        elif kind == "image":
            edited = None
            orig_image = None
            if archive is None:
                edits = _photo_edits(path)
                if edits is not None:
                    edited = _edited_image(path, edits, box)
            if edited is not None:
                image, original, scaled = edited
                raw, _raw_size, _raw_scaled = _read_image(path, *box)
                orig_image = raw   # 「元画像（押している間）」用
            else:
                image, original, scaled = _read_image(path, *box)
            result.update(image=image, note=f"{original[0]}×{original[1]}" if original else "",
                          scaled=scaled, orig_image=orig_image)
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
        from PySide6.QtWidgets import QPushButton
        self.orig_btn = QPushButton("元画像（押している間）")
        self.orig_btn.setFont(font)
        self.orig_btn.setFocusPolicy(Qt.NoFocus)   # キーはファイル窓のまま
        self.orig_btn.setEnabled(False)
        self.orig_btn.pressed.connect(lambda: self._show_original(True))
        self.orig_btn.released.connect(lambda: self._show_original(False))
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)
        header_row.addWidget(self.header, 1)
        header_row.addWidget(self.orig_btn)
        layout.addLayout(header_row)
        self._disp_pixmap = None    # いま出している（補正ありの）絵
        self._orig_pixmap = None    # 元画像ボタンで出す絵
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
        # HTML / Markdown の整形表示。リンクは辿らない(見本なので)
        self.rich = QTextBrowser()
        self.rich.setReadOnly(True)
        self.rich.setFocusPolicy(Qt.NoFocus)
        self.rich.setOpenLinks(False)
        self.rich.setOpenExternalLinks(False)
        self.rich.setStyleSheet(f"background-color: {colors['background']};"
                                f" color: {colors['text']}; border: none;")
        self.stack.addWidget(self.text)
        self.stack.addWidget(self.image)
        self.stack.addWidget(self.rich)
        layout.addWidget(self.stack, 1)
        self.last = None
        self.turn = core.TURN_IDENTITY      # 手動の回転・反転(R / L / H / V)
        self.turn_suffix = ""               # 回転固定の印(_R / _L)
        # 写真の★(右上の隅)。絵の上に重ねる。絵でないとき(テキスト等)は絵ごと隠れる。
        self.rating = QLabel(self.image)
        self.rating.setTextFormat(Qt.PlainText)
        self.rating.setAttribute(Qt.WA_TransparentForMouseEvents)
        rating_font = QFont(font)
        rating_font.setPointSize(max(font.pointSize(), 10) + 4)
        rating_font.setBold(True)
        self.rating.setFont(rating_font)
        self.rating.hide()
        self.image.installEventFilter(self)

    def set_rating_text(self, text: str, color: str = "#ffd400", background=None) -> None:
        """隅に出す★(🖼️ フォトフレーム用 ✓ / ☆ など)。background を渡すと一覧の★の列と
        同じ色の角丸バッジにする(無ければ半透明の黒地)。空なら消す。"""
        if not text:
            self.rating.hide()
            return
        self.rating.setStyleSheet(f"color: {color};"
                                  f" background-color: {background or 'rgba(0, 0, 0, 150)'};"
                                  " padding: 2px 8px; border-radius: 8px;")
        self.rating.setText(text)
        self.rating.adjustSize()
        self._place_rating()
        self.rating.show()
        self.rating.raise_()

    def _place_rating(self) -> None:
        self.rating.move(max(self.image.width() - self.rating.width() - 6, 0), 6)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt の上書き)
        try:
            if obj is self.image and event.type() == QEvent.Resize:
                self._place_rating()
        except Exception:  # noqa: BLE001  スロットで投げ切ると落ちる
            pass
        return super().eventFilter(obj, event)

    def image_box(self):
        size = self.stack.size()
        return max(size.width() - 8, 16), max(size.height() - 8, 16)

    def show_loading(self, title: str) -> None:
        suffix = getattr(self, "turn_suffix", "")
        self.header.setText(f"Preview: {title}  …" + (f"  {suffix}" if suffix else ""))

    def _show_original(self, on: bool) -> None:
        """元画像ボタン: 押している間だけ補正なしの絵に差し替える。"""
        if on and self._orig_pixmap is not None:
            self.image.setPixmap(self._orig_pixmap)
        elif not on and self._disp_pixmap is not None:
            self.image.setPixmap(self._disp_pixmap)

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
            self._disp_pixmap = pixmap
            orig = result.get("orig_image")
            self._orig_pixmap = None
            if orig is not None:
                opix = QPixmap.fromImage(turned_image(orig, turn))
                if opix.width() > box_w or opix.height() > box_h:
                    opix = opix.scaled(box_w, box_h, Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
                self._orig_pixmap = opix
            self.orig_btn.setEnabled(self._orig_pixmap is not None)
            self.image.setPixmap(pixmap)
            self.stack.setCurrentWidget(self.image)
        elif result.get("rich") is not None:
            self._disp_pixmap = None
            self._orig_pixmap = None
            self.orig_btn.setEnabled(False)
            kind, text = result["rich"]
            base = result.get("path") or ""
            self.rich.document().setBaseUrl(
                QUrl.fromLocalFile(os.path.dirname(base) + os.sep) if base else QUrl())
            if kind == "html":
                self.rich.setHtml(text)
            else:
                self.rich.setMarkdown(text)
            self.stack.setCurrentWidget(self.rich)
        else:
            self._disp_pixmap = None
            self._orig_pixmap = None
            self.orig_btn.setEnabled(False)
            self.text.setPlainText("\n".join(result.get("lines") or []))
            self.stack.setCurrentWidget(self.text)
