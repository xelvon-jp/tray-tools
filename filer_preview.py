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

from PySide6.QtCore import QEvent, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
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
    # QtPdf は背景が透明のまま返す。背景色の指定が無い PDF は、窓の紺色が透けて
    # 黒い字が読めなくなるので、紙の色(白)の上に重ねる。
    from PySide6.QtGui import QColor, QImage, QPainter

    paper = QImage(image.size(), QImage.Format_RGB32)
    paper.fill(QColor("white"))
    painter = QPainter(paper)
    painter.drawImage(0, 0, image)
    painter.end()
    return paper, pages


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


def _log_failure(where: str, exc: Exception) -> None:
    """読めなかった理由を filer_error.log に残す(画面は info に落ちる)。
    except の中から呼ぶ(traceback を使う)。filer.py の _log_exception と同じ書式・同じファイル。"""
    try:
        import traceback
        from datetime import datetime
        from pathlib import Path

        log = Path(__file__).resolve().parent / "filer_error.log"
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {where} =====\n"
                    f"{traceback.format_exc()}")
    except Exception:  # noqa: BLE001  ログに書けなくても見本の処理は続ける
        pass


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
        if archive is not None and kind in ("text", "image", "html", "markdown", "video",
                                            "audio", "xlsx", "model3d"):
            if entry.size > core.PREVIEW_ARCHIVE_MAX:
                result.update(kind="info", lines=core.info_lines(entry.name, entry)
                              + ["", "（書庫の中のファイルが大きいため中身を出しません。"
                                 f"上限 {core.PREVIEW_ARCHIVE_MAX // (1024 * 1024)}MB）"])
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
            ext = os.path.splitext(path)[1].lstrip(".").lower()
            if archive is None and ext not in ("heic", "heif", "psd", "svg"):
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
        elif kind in ("video", "audio"):
            result.update(lines=core.info_lines(entry.name, entry))
            if kind == "video":
                # サムネと長さ・解像度は OpenCV で、この読み取りスレッドの中で取る(速い)。
                # 取れない・cv2 が無いときは thumb を付けず、GUI スレッドの VideoProbe
                # (QMediaPlayer。従来方式)に任せる。音声のタグは従来どおり VideoProbe。
                try:
                    import filer_video

                    got = filer_video.cv_thumb(path)
                    if got is not None:
                        result.update(thumb=got[0], video_meta=got[1])
                except Exception as e:  # noqa: BLE001  従来方式へ落とす
                    _log_failure("videoCv", e)
        elif kind == "xlsx":
            import filer_formats

            text, note = filer_formats.read_xlsx(path)
            result.update(rich=("table", text), note=note)
        elif kind == "model3d":
            import filer_formats

            if entry.size > filer_formats.MODEL_MAX_BYTES:
                raise OSError(f"ファイルが大きすぎるため3Dの絵は作りません（"
                              f"{entry.size // (1024 * 1024)}MB。"
                              f"上限 {filer_formats.MODEL_MAX_BYTES // (1024 * 1024)}MB）")
            image, note = filer_formats.model_preview(path, box)
            result.update(image=image, note=note)
        elif kind == "info":
            lines = core.info_lines(entry.name, entry)
            if os.path.splitext(entry.name)[1].lower() == ".shd":
                import filer_formats

                try:
                    header, version = filer_formats.shd_header(path)
                    lines += ["", f"Shade 3D  ver.{version}" if version else header,
                              "（Shade 3D の独自形式のため、中身のプレビューは作れません）"]
                except Exception as e:  # noqa: BLE001
                    _log_failure("shdHeader", e)
                    lines += ["", f"（Shade 3D のヘッダを読めません: {e}）"]
            result.update(lines=lines)
    except Exception as e:  # noqa: BLE001  見本が作れないだけ。窓は止めない
        _log_failure(f"preview:{kind}", e)
        result.update(kind="info", image=None, rich=None,
                      lines=(core.info_lines(entry.name, entry) if entry else [])
                      + ["", f"（読めません: {e}）"])
    return result


VIEWER_ACCENT = "#b45309"      # ビュア状態の帯と枠の色(琥珀。ふだんの紺とはっきり違う色)
VIEWER_FRAME = 3                # ビュア状態の枠の太さ
VIEWER_HINT = "   Enter/Esc: 閉じる   Ctrl+Enter: メニュー"


class PreviewWidget(QWidget):
    """反対窓の場所に出すプレビュー。キーは受け取らない(フォーカスはファイル窓のまま)。

    テキストは簡易テキストビュア(filer_viewer.TextViewer の埋め込み版)で出す。
    全文が読め、検索などもできる。キーを受けるのはビュアにフォーカスを渡したときだけ
    (focus_viewer)で、Esc / Tab / Enter で一覧へ戻る(leave)。"""

    done = Signal()     # ビュアで Esc / Enter(見終わった)
    menu = Signal()     # ビュアで Ctrl+Enter(拡張子別メニュー)
    video_move = Signal(int)    # 内蔵プレーヤで ↑↓(前・次の動画)
    video_done = Signal()       # 内蔵プレーヤで Esc(再生をやめて情報の表示へ)
    move = Signal(int)  # ビュアで ←→(前・次のファイル)
    edit = Signal()     # ビュアで Enter(編集したい)
    leave = Signal()    # ビュアから「一覧へ戻りたい」(呼び出し側がフォーカスを戻す)

    def __init__(self, parent, colors: dict, font: QFont):
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.colors = colors
        self._font = font
        self.viewer = None          # 埋め込みの簡易テキストビュア(最初のテキストで作る)
        self._head = None           # 頭に出す (題名, 注記, 印)。ビュア状態の切替で文言を作り直す
        self.viewer_mode = False    # ビュア状態(Enter で入る)。見た目を変えて区別する
        self.player_view = None     # 内蔵プレーヤ(動画。Ctrl+Enter のメニューから使うとき作る)
        self.probe = None           # いま動画の情報・サムネを取っている VideoProbe
        self._video_path = None
        self._video_lines = []
        self._video_audio = False
        self.web = None             # ブラウザ表示(WebEngine)。HTML で Tab/Enter したときだけ作る
        self._html_path = None      # いま整形表示している HTML のパス(ブラウザ表示で開く)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QLabel("Preview")
        self.header.setTextFormat(Qt.PlainText)
        # 長いファイル名で窓の最小幅が広がり、左右の仕切りが動くのを防ぐ
        # (QLabel は既定で文字が全部入る幅を最小幅にする。はみ出た分は切れる)
        self.header.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
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
        # 動画の情報ページ: 上にサムネ、下に情報(名前・サイズ・日時・属性・長さ・解像度など)
        self.video_page = QWidget()
        video_layout = QVBoxLayout(self.video_page)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)
        self.video_thumb = QLabel()
        self.video_thumb.setAlignment(Qt.AlignCenter)
        self.video_thumb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.video_thumb.setStyleSheet("background-color: #000000;")
        self.video_info = QLabel()
        self.video_info.setTextFormat(Qt.PlainText)
        self.video_info.setFont(font)
        self.video_info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.video_info.setStyleSheet(f"background-color: {colors['background']};"
                                      f" color: {colors['text']}; padding: 6px 8px;")
        video_layout.addWidget(self.video_thumb, 1)
        video_layout.addWidget(self.video_info)
        self.stack.addWidget(self.video_page)
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

    # --- ビュア状態の見た目(Enter で入ったとき。F3 / V だけのプレビューと見分ける) ---
    def _header_prefix(self) -> str:
        return "◆ VIEWER" if self.viewer_mode else "Preview"

    def set_viewer_mode(self, on: bool) -> None:
        """ビュア状態にする/戻す。頭の帯を目立つ色にして、周りに枠を付ける。"""
        on = bool(on)
        if on == self.viewer_mode:
            return
        self.viewer_mode = on
        margin = VIEWER_FRAME if on else 0
        self.layout().setContentsMargins(margin, margin, margin, margin)
        if on:
            self.header.setStyleSheet(
                f"background-color: {VIEWER_ACCENT}; color: #ffffff; font-weight: bold;"
                " padding: 0 4px;")
        else:
            self.header.setStyleSheet(
                f"background-color: {self.colors['header_inactive']};"
                f" color: {self.colors['header_inactive_text']}; padding: 0 4px;")
        self._refresh_header()      # 頭の文言(プレフィックス・操作の案内)を更新
        self.update()

    def _refresh_header(self) -> None:
        if self._head is None:
            self.header.setText(self._header_prefix())
            return
        title, note, suffix = self._head
        self.header.setText(f"{self._header_prefix()}: {title}"
                            + (f"  [{note}]" if note else "")
                            + (f"  {suffix}" if suffix else "")
                            + (VIEWER_HINT if self.viewer_mode else ""))

    def paintEvent(self, event):  # noqa: N802 (Qt の上書き)
        super().paintEvent(event)
        if self.viewer_mode:
            from PySide6.QtGui import QPainter

            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor(VIEWER_ACCENT))
            painter.end()

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
        self.header.setText(f"{self._header_prefix()}: {title}  …"
                            + (f"  {suffix}" if suffix else ""))

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
        if result.get("kind") not in ("video", "audio"):
            self._stop_probe()
        if self.web is not None and self.web.url().toString() not in ("", "about:blank"):
            self.web.setUrl(QUrl("about:blank"))    # 別のファイルへ移ったら前のページは手放す
        note = result.get("note") or ""
        image = result.get("image")
        turn = getattr(self, "turn", core.TURN_IDENTITY)
        if image is not None:
            label = core.turn_label(turn)
            if label:
                note = f"{note} {label}" if note else label
        suffix = getattr(self, "turn_suffix", "")
        self._head = (result.get("title", ""), note, suffix)
        self._refresh_header()
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
        elif result.get("kind") in ("video", "audio"):
            self._disp_pixmap = None
            self._orig_pixmap = None
            self.orig_btn.setEnabled(False)
            self._show_video_info(result)
        elif result.get("rich") is not None:
            self._disp_pixmap = None
            self._orig_pixmap = None
            self.orig_btn.setEnabled(False)
            kind, text = result["rich"]
            base = result.get("path") or ""
            self._html_path = base if kind == "html" and base else None
            self.rich.document().setBaseUrl(
                QUrl.fromLocalFile(os.path.dirname(base) + os.sep) if base else QUrl())
            if kind == "html" and self._show_html_in_web(base):
                return      # HTML は一覧で選んだ時点からブラウザ表示(Enter 前後で見た目を揃える)
            if kind in ("html", "table"):
                self.rich.setHtml(text)
            else:
                self.rich.setMarkdown(text)
            self.stack.setCurrentWidget(self.rich)
        elif self._show_in_viewer(result):
            self._disp_pixmap = None
            self._orig_pixmap = None
            self.orig_btn.setEnabled(False)
        else:
            self._disp_pixmap = None
            self._orig_pixmap = None
            self.orig_btn.setEnabled(False)
            self.text.setPlainText("\n".join(result.get("lines") or []))
            self.stack.setCurrentWidget(self.text)

    # --- 簡易テキストビュア(テキストのプレビュー) ---
    def _show_in_viewer(self, result: dict) -> bool:
        """テキストを簡易テキストビュアで出す。出せたら True。

        読めない(消えた・権限なし等)ときは False で、呼び出し側が従来の
        テキスト表示(読み取りスレッドが作った先頭 200 行)に戻す。"""
        path = result.get("path")
        if result.get("kind") != "text" or not path or not os.path.isfile(path):
            return False
        try:
            if self.viewer is None:
                import filer_viewer  # 使うときに読む

                self.viewer = filer_viewer.TextViewer(
                    path, title_name=result.get("title") or None, font=self._font,
                    colors={"background": self.colors.get("background"),
                            "text": self.colors.get("text")},
                    embedded=True, parent=self)
                self.viewer.leave.connect(self.leave)
                self.viewer.edit.connect(self.edit)
                self.viewer.menu.connect(self.menu)
                self.viewer.move.connect(self.move)
                self.viewer.done.connect(self.done)
                self.stack.addWidget(self.viewer)
            else:
                self.viewer.load(path, result.get("title") or None)
        except OSError:
            return False
        self.stack.setCurrentWidget(self.viewer)
        return True

    def viewer_shown(self) -> bool:
        """いま簡易テキストビュアを出しているか。"""
        return self.viewer is not None and self.stack.currentWidget() is self.viewer

    def focus_viewer(self) -> bool:
        """ビュアにフォーカスを渡す(スクロール・検索のキーが効くようになる)。"""
        if not self.viewer_shown():
            return False
        self.viewer.area.setFocus()
        return True

    # --- 動画(情報とサムネ。再生は内蔵プレーヤ) ---
    def _stop_probe(self) -> None:
        if self.probe is not None:
            self.probe.close()
            self.probe.deleteLater()
            self.probe = None

    def _show_video_info(self, result: dict) -> None:
        """動画の情報ページ。名前・サイズ・日時・属性はすぐ、サムネ・長さ・解像度などは
        読めてから出す(読めなければ出さない)。"""
        self._stop_probe()
        path = result.get("path")
        self._video_path = path
        self._video_audio = result.get("kind") == "audio"
        self._video_lines = list(result.get("lines") or [])
        self.video_thumb.clear()
        self.video_thumb.setText("読み込み中…" if path else "")
        self.video_thumb.setStyleSheet("background-color: #000000; color: #aaaaaa;")
        self._set_video_info({})
        self.stack.setCurrentWidget(self.video_page)
        if not path or not os.path.isfile(path):
            return
        thumb = result.get("thumb")
        if thumb is not None and not thumb.isNull():
            # 読み取りスレッドが OpenCV で取ってきたもの。プローブは要らない
            self._on_probe(path, thumb, result.get("video_meta") or {})
            return
        try:
            import filer_video  # 使うときに読む(QtMultimedia は重い)

            self.probe = filer_video.VideoProbe(path, self, audio=self._video_audio)
            self.probe.done.connect(lambda image, meta, p=path: self._on_probe(p, image, meta))
            self.probe.start()
        except Exception:  # noqa: BLE001  QtMultimedia が無い環境は、情報だけ出す
            self.video_thumb.setText("")
            self.probe = None

    def _set_video_info(self, meta: dict) -> None:
        import filer_video

        lines = list(self._video_lines)
        lines += filer_video.describe_meta(meta)
        lines += ["", "Ctrl+Enter → 内蔵プレーヤで再生（↑↓ で前後の"
                  + ("曲" if self._video_audio else "動画") + "）"]
        self.video_info.setText(chr(10).join(lines))

    def _on_probe(self, path: str, image, meta: dict) -> None:
        if path != self._video_path or self.stack.currentWidget() is not self.video_page:
            return
        self._set_video_info(meta)
        if image is None or image.isNull():
            audio = self._video_audio
            self.video_thumb.setStyleSheet("background-color: #000000; color: #666666;"
                                           + (" font-size: 64px;" if audio else ""))
            self.video_thumb.setText("♪" if audio else "サムネイルを作れませんでした")
            return
        import filer_video

        self.video_page.layout().activate()     # 出したばかりでも、箱の大きさを確定させてから収める
        size = self.video_thumb.size()
        scaled = filer_video.thumbnail_image(image, max(size.width() - 8, 16),
                                             max(size.height() - 8, 16))
        self.video_thumb.setText("")
        self.video_thumb.setPixmap(QPixmap.fromImage(scaled))

    # --- 内蔵プレーヤ(動画) ---
    def player_shown(self) -> bool:
        return self.player_view is not None and self.stack.currentWidget() is self.player_view

    def play_video(self, path: str, title: str = "", counter: str = "") -> bool:
        """動画を内蔵プレーヤで最初から再生する(反対窓いっぱい)。出せたら True。"""
        try:
            if self.player_view is None:
                import filer_video  # 使うときに読む

                self.player_view = filer_video.VideoPlayerView(self._font, self)
                self.player_view.done.connect(self.video_done)
                self.player_view.move.connect(self.video_move)
                self.player_view.menu.connect(self.menu)
                self.stack.addWidget(self.player_view)
            self._stop_probe()
            self.player_view.play(path, title, counter)
        except Exception:  # noqa: BLE001  QtMultimedia が使えない環境
            self.player_view = None
            return False
        self._head = (title, "内蔵プレーヤ", "")
        self._refresh_header()
        self.stack.setCurrentWidget(self.player_view)
        self.player_view.setFocus()
        return True

    def focus_player(self) -> None:
        if self.player_shown():
            self.player_view.setFocus()

    def stop_player(self) -> None:
        """再生をやめて動画を手放す(ファイルを掴んだままにしない)。"""
        if self.player_view is not None:
            self.player_view.stop()

    # --- ブラウザ表示(HTML。Chromium の WebEngine) ---
    def web_shown(self) -> bool:
        return self.web is not None and self.stack.currentWidget() is self.web

    def enter_web(self) -> bool:
        """整形表示している HTML を、ブラウザ(WebEngine)で出してフォーカスを渡す。

        普段の整形表示(QTextBrowser)は軽いが簡易で、flex / grid などが崩れる。
        ここで初めて WebEngine を作る(重いので、使わなければ読み込まない)。
        ローカルのファイルだけを開き、JavaScript は切る(見本なので)。
        出せたら True。WebEngine が無い・HTML でないときは False。"""
        if self.web_shown():
            self.web.setFocus()
            return True
        if self.stack.currentWidget() is not self.rich or not self._html_path:
            return False
        try:
            if self.web is None:
                self.web = self._make_web()
                self.stack.addWidget(self.web)
            self.web.setUrl(QUrl.fromLocalFile(self._html_path))
        except Exception:  # noqa: BLE001  WebEngine が使えない環境では従来の表示のまま
            self.web = None
            return False
        self.stack.setCurrentWidget(self.web)
        self.web.setFocus()
        return True

    def _show_html_in_web(self, path: str) -> bool:
        """HTML をブラウザ(WebEngine)で出す。フォーカスは渡さない(一覧のまま動ける)。

        出せたら True。WebEngine が使えないときは False で、呼び出し側が簡易表示にする。"""
        if not path:
            return False
        try:
            if self.web is None:
                self.web = self._make_web()
                self.stack.addWidget(self.web)
            self.web.setUrl(QUrl.fromLocalFile(path))
        except Exception:  # noqa: BLE001
            self.web = None
            return False
        self.stack.setCurrentWidget(self.web)
        return True

    def _make_web(self):
        from PySide6.QtGui import QKeySequence, QShortcut
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView

        class LocalOnlyPage(QWebEnginePage):
            def acceptNavigationRequest(self, url, nav_type, is_main_frame):
                # リンクは辿らない(見本なので)。最初の読み込みだけ通す
                return nav_type == QWebEnginePage.NavigationTypeTyped                     or url.scheme() in ("file", "about")

        view = QWebEngineView()
        view.setPage(LocalOnlyPage(view))
        settings = view.settings()
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, False)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.PluginsEnabled, False)
        view.page().setBackgroundColor(QColor("#ffffff"))   # ページの既定は白(ブラウザと同じ)
        # キーは Chromium が受けるので、ショートカットで拾う。Esc / BS は見終わった、
        # Tab は一覧へ戻る(テキストのビュアと同じ)
        for key, signal in (("Esc", self.done), ("Backspace", self.done),
                            ("Return", self.done), ("Enter", self.done), ("Tab", self.leave)):
            shortcut = QShortcut(QKeySequence(key), view)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda s=signal: self._leave_web(s))
        for key, step in (("Left", -1), ("Right", 1)):      # ←→ は前・次のファイル
            shortcut = QShortcut(QKeySequence(key), view)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda s=step: self.move.emit(s))
        for key in ("Ctrl+Return", "Ctrl+Enter"):           # Ctrl+Enter は拡張子別メニュー
            shortcut = QShortcut(QKeySequence(key), view)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.menu.emit)
        return view

    def _leave_web(self, signal) -> None:
        """ブラウザ表示から出る。読み込んだページは手放す(ファイルを掴まない)。"""
        # ページは出したまま(一覧の移動で別のファイルへ移るときに手放す)。閉じるときは shutdown。
        signal.emit()

    def shutdown(self) -> None:
        """プレビューを閉じるとき。ビュアの読み込み・検索のスレッドを止め、動画も手放す。"""
        self._stop_probe()
        self.stop_player()
        if self.web is not None:
            self.web.setUrl(QUrl("about:blank"))
        if self.viewer is not None:
            self.viewer.shutdown()
