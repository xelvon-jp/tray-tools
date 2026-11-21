# -*- coding: utf-8 -*-
"""ダーク基調の配色・スタイルシート・アイコン。"""
from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap)

BG = "#14161a"
PANEL = "#1c1f26"
PANEL2 = "#232733"
BORDER = "#2d3340"
TEXT = "#e8eaee"
MUTED = "#8d95a4"
ACCENT = "#4c9aff"
ACCENT_HI = "#7db4ff"
REC = "#e5484d"
ORANGE = "#f2a33a"
TEAL = "#3dc9b8"
OKGREEN = "#46c27a"
WARN_BG = "#3a2d12"
ERR_BG = "#3d1a1d"
# 入力部品
CTRL_OFF_BORDER = "#8791a6"   # OFF の枠線（暗い背景ではっきり見える明るさ）
CTRL_DISABLED = "#566073"

FONT_FAMILY = "Yu Gothic UI"

# 表示サイズ（設定 display_size。再起動なしで切り替えられる）: 基準フォント / 時間表示 / コンパクトの時間表示
SIZES = {"small": (10, 20, 16), "standard": (11, 24, 18), "large": (12, 27, 20)}
SIZE_LABELS = {"small": "小", "standard": "標準", "large": "大"}
DEFAULT_SIZE = "small"
DISPLAY_SIZE = DEFAULT_SIZE
BASE_PT, TIME_PT, TIME_PT_COMPACT = SIZES[DEFAULT_SIZE]


def set_size(name: str) -> str:
    """表示サイズを切り替える（QSS も作り直す。呼び出し側が app.setStyleSheet / setFont に反映する）。"""
    global DISPLAY_SIZE, BASE_PT, TIME_PT, TIME_PT_COMPACT, QSS
    if name not in SIZES:
        name = DEFAULT_SIZE
    DISPLAY_SIZE = name
    BASE_PT, TIME_PT, TIME_PT_COMPACT = SIZES[name]
    QSS = build_qss()
    return name

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def app_font() -> QFont:
    f = QFont(FONT_FAMILY, BASE_PT)
    f.setStyleStrategy(QFont.PreferAntialias)
    return f


# ---------------------------------------------------------------- 矢印（SVG。拡大しても崩れない）
def _arrow_svg(up: bool, color: str) -> str:
    pts = "2,8 7,3 12,8" if up else "2,4 7,9 12,4"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="14" height="12" viewBox="0 0 14 12">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round"/></svg>\n')


def _ensure_arrows() -> dict[str, str]:
    """矢印の SVG を assets/ui に用意して、スタイルシート用のパス（スラッシュ区切り）を返す。"""
    want = {"arrow_down.svg": _arrow_svg(False, "#cfd5e2"), "arrow_up.svg": _arrow_svg(True, "#cfd5e2"),
            "arrow_down_dis.svg": _arrow_svg(False, CTRL_DISABLED), "arrow_up_dis.svg": _arrow_svg(True, CTRL_DISABLED)}
    for base in (ASSETS_DIR / "ui", Path(tempfile.gettempdir()) / "kabeuchi_stt_ui"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            for name, text in want.items():
                p = base / name
                if not p.exists() or p.read_text(encoding="utf-8") != text:
                    with open(p, "w", encoding="utf-8", newline="") as f:
                        f.write(text)
            return {n: (base / n).as_posix() for n in want}
        except OSError:
            continue
    return {n: "" for n in want}


def build_qss() -> str:
    a = _ensure_arrows()
    return f"""
* {{ font-family: "{FONT_FAMILY}", "Meiryo UI", "Segoe UI"; font-size: {BASE_PT}pt; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {BG}; top: -1px; }}
QTabBar::tab {{ background: {PANEL}; color: {MUTED}; padding: 8px 22px; border: 1px solid {BORDER};
               border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {BG}; color: {TEXT}; }}
QGroupBox {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; margin-top: 16px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {MUTED}; background: {BG}; }}
QPushButton {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #5b6270; background: {PANEL}; }}
QPushButton:checked {{ background: #24406b; border: 1px solid {ACCENT}; color: #ffffff; }}
QPushButton:checked:hover {{ background: #2c4d80; }}
QPushButton:checked:disabled {{ background: #1b2230; border: 1px solid #2c3a52; color: #5b6270; }}
QPushButton#ghost {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 6px 10px; color: #c3c9d6; }}
QPushButton#ghost:hover {{ background: {PANEL2}; border-color: {BORDER}; color: #ffffff; }}
QPushButton#ghost:pressed {{ background: {BORDER}; }}
QPushButton#ghost:disabled {{ background: transparent; border-color: transparent; color: #566073; }}
QPushButton#ghost:checked {{ background: #1c2d49; border: 1px solid #35609f; color: {ACCENT_HI}; }}
QPushButton#ghost:checked:hover {{ background: #223a5e; }}
QPushButton#ghostIcon {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 6px; color: #c3c9d6; }}
QPushButton#ghostIcon:hover {{ background: {PANEL2}; border-color: {BORDER}; }}
QPushButton#ghostIcon:pressed {{ background: {BORDER}; }}
QPushButton#ghostIcon:disabled {{ background: transparent; border-color: transparent; }}
QPushButton#ghostIcon:checked {{ background: #1c2d49; border: 1px solid #35609f; }}
QPushButton#ghostIcon:checked:hover {{ background: #223a5e; }}
QPushButton#rec {{ background: {REC}; border: none; font-weight: bold; padding: 8px 20px; color: white; }}
QPushButton#rec:hover {{ background: #f0585d; }}
QPushButton#rec:disabled {{ background: #5a2a2d; color: #b89a9b; }}
QPushButton#recSmall {{ background: {REC}; border: none; font-weight: bold; padding: 5px 14px; color: white; border-radius: 8px; }}
QPushButton#recSmall:hover {{ background: #f0585d; }}
QPushButton#flat {{ background: transparent; border: 1px solid transparent; padding: 4px 10px; color: {MUTED}; }}
QPushButton#flat:hover {{ color: {TEXT}; border-color: {BORDER}; }}
QPushButton#recStop {{ background: #2b2f3a; border: 2px solid {REC}; font-weight: bold; padding: 7px 19px; color: {TEXT}; }}
QPushButton#pause {{ background: transparent; border: 1px solid #3a4152; font-weight: bold; padding: 8px 16px; color: {TEXT}; }}
QPushButton#pause:hover {{ background: {PANEL2}; border-color: #566073; }}
QPushButton#pause:disabled {{ background: transparent; border-color: #2a2f3b; color: #566073; }}
QPushButton#pauseOn {{ background: #4a3512; border: 2px solid {ORANGE}; color: #ffe2b0; font-weight: bold; padding: 7px 15px; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: #0b1220; font-weight: bold; }}
QPushButton#primary:hover {{ background: #6aadff; }}
QPushButton#primary:disabled {{ background: #2a3a55; color: #6b7893; }}
QPushButton#danger {{ color: #ff8a8d; }}
QPushButton#danger:disabled {{ color: #6a4a4c; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTableWidget, QListWidget, QListView {{
    background: {PANEL}; border: 1px solid #3a4152; border-radius: 6px; selection-background-color: #2f5b99; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ padding: 5px 8px; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: #566073; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:on {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {CTRL_DISABLED}; background: #181b21; border-color: #2a2f3b; }}
QComboBox {{ padding-right: 30px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 26px;
    border-left: 1px solid #3a4152; border-top-right-radius: 6px; border-bottom-right-radius: 6px; background: {PANEL2}; }}
QComboBox::drop-down:hover {{ background: #2c3345; }}
QComboBox::down-arrow {{ image: url("{a['arrow_down.svg']}"); width: 12px; height: 10px; }}
QComboBox::down-arrow:disabled {{ image: url("{a['arrow_down_dis.svg']}"); }}
QComboBox QAbstractItemView {{ background: {PANEL}; selection-background-color: #2f5b99; border: 1px solid {BORDER}; outline: 0; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 28px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 24px;
    border-left: 1px solid #3a4152; border-bottom: 1px solid #3a4152; border-top-right-radius: 5px; background: {PANEL2}; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 24px;
    border-left: 1px solid #3a4152; border-bottom-right-radius: 5px; background: {PANEL2}; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{ background: #2f5b99; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{a['arrow_up.svg']}"); width: 10px; height: 8px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{a['arrow_down.svg']}"); width: 10px; height: 8px; }}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {{ image: url("{a['arrow_up_dis.svg']}"); }}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{ image: url("{a['arrow_down_dis.svg']}"); }}
QHeaderView::section {{ background: {PANEL2}; color: {MUTED}; border: none; border-right: 1px solid {BORDER}; padding: 4px 8px; }}
QTableWidget {{ gridline-color: {BORDER}; }}
QProgressBar {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 5px; text-align: center; height: 16px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QProgressBar:disabled {{ color: {CTRL_DISABLED}; }}
QProgressBar::chunk:disabled {{ background: #3a4a66; }}
QScrollBar:vertical {{ background: {BG}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #3a4152; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QScrollBar:horizontal {{ background: {BG}; height: 12px; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG}; }}
QListWidget::item {{ padding: 7px 8px; border-radius: 6px; }}
QListWidget::item:selected {{ background: #2a3550; color: {TEXT}; }}
QListWidget::item:hover {{ background: #232a3c; }}
QRadioButton {{ spacing: 8px; }}
QRadioButton::indicator {{ width: 18px; height: 18px; border: 2px solid {CTRL_OFF_BORDER}; border-radius: 11px; background: transparent; }}
QRadioButton::indicator:hover {{ border-color: {ACCENT_HI}; }}
QRadioButton::indicator:checked {{ border-color: {ACCENT}; background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
    stop:0 #ffffff, stop:0.42 #ffffff, stop:0.5 {ACCENT}, stop:1 {ACCENT}); }}
QRadioButton::indicator:disabled {{ border-color: #3a4152; background: transparent; }}
QRadioButton::indicator:checked:disabled {{ border-color: #3a4a66; background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
    stop:0 #8a93a5, stop:0.42 #8a93a5, stop:0.5 #3a4a66, stop:1 #3a4a66); }}
QRadioButton:disabled {{ color: {CTRL_DISABLED}; }}
QScrollBar::handle:horizontal {{ background: #3a4152; border-radius: 5px; min-width: 30px; }}
QSplitter::handle {{ background: {BG}; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; }}
QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; }}
QSlider::groove:horizontal {{ height: 6px; background: {BORDER}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QSlider::groove:horizontal:disabled {{ background: #262b36; }}
QSlider::sub-page:horizontal:disabled {{ background: #3a4a66; }}
QSlider::handle:horizontal:disabled {{ background: {CTRL_DISABLED}; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; }}
QMenu::item:selected {{ background: #2f5b99; }}
QMenu::item:disabled {{ color: {CTRL_DISABLED}; }}
"""


QSS = build_qss()


# ---------------------------------------------------------------- アイコン
def draw_icon_pixmap(size: int, recording: bool = False) -> QPixmap:
    """マイクをモチーフにしたアプリアイコン（自前描画）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    top, bottom = (("#ff7a7e", "#c4282d") if recording else ("#69b2ff", "#2c64cf"))
    g = QLinearGradient(0, 0, 0, s)
    g.setColorAt(0.0, QColor(top))
    g.setColorAt(1.0, QColor(bottom))
    p.setBrush(QBrush(g))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92), s * 0.22, s * 0.22)
    white = QColor("white")
    # マイク本体（カプセル）
    cap = QRectF(s * 0.37, s * 0.14, s * 0.26, s * 0.44)
    p.setBrush(QBrush(white))
    p.drawRoundedRect(cap, s * 0.13, s * 0.13)
    if size >= 32:  # 網目
        pen = QPen(QColor(bottom), max(1.0, s * 0.022))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for k in (0.26, 0.34, 0.42):
            p.drawLine(QPointF(s * 0.42, s * k), QPointF(s * 0.58, s * k))
    # 受け（U字）・支柱・台
    pen = QPen(white, max(1.2, s * 0.06))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    path.moveTo(s * 0.27, s * 0.44)
    path.arcTo(QRectF(s * 0.27, s * 0.30, s * 0.46, s * 0.40), 180, 180)
    p.drawPath(path)
    p.drawLine(QPointF(s * 0.5, s * 0.70), QPointF(s * 0.5, s * 0.81))
    p.drawLine(QPointF(s * 0.38, s * 0.82), QPointF(s * 0.62, s * 0.82))
    p.end()
    return pm


ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def make_icon(recording: bool = False) -> QIcon:
    """トレイ/ウィンドウ用アイコン。recording=True は赤（録音中）。"""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(draw_icon_pixmap(size, recording))
    return icon


def _png_bytes(pm: QPixmap) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    pm.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


def write_icon_files(out_dir: Path, name: str = "kabeuchi_stt") -> tuple[Path, Path]:
    """.png（256px）と .ico（PNG 圧縮の複数サイズ）を保存する。要 QGuiApplication。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    images = [(sz, _png_bytes(draw_icon_pixmap(sz))) for sz in ICON_SIZES]
    png = out_dir / f"{name}.png"
    png.write_bytes(images[-1][1])
    # ICO: ICONDIR + ICONDIRENTRY × n + PNG データ
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for sz, data in images:
        w = 0 if sz >= 256 else sz
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    ico = out_dir / f"{name}.ico"
    ico.write_bytes(head + entries + blobs)
    return png, ico
