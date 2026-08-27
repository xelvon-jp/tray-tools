# taskbar_launcher.py
# タスクバーウィジェット(taskbar_widget.py)にマウスを乗せている間だけ、その真上に出る
# 縦一列のランチャ。
#
# 本体は「タスクバーの時計に化ける」のが仕事で、幅が実測59px前後しかない。そこへ並べら
# れるのは Rapture と音声の2つが限界で、画面定規・カラーピッカー・定型文・フォルダブック
# マークへは通知領域(＝プライマリのタスクバー)まで戻らないと届かなかった。ウィジェットを
# 置いた目的がまさに「別の画面へ視線と手を戻さないこと」なので、これでは半分しか効かない。
# そこで、タスクバーの外へはみ出す別の窓を1枚出し、そこにアイコンを縦に並べる。
#
# taskbar_widget.py に混ぜず別モジュールにしてあるのは、あちらが既に900行近くあり、かつ
# 「時計に化ける窓」と「その上に出るパネル」で寿命もイベントも別物のため(capture_window.py
# や color_picker.py と同じで、窓1つにつき1モジュール)。
#
# import は taskbar_widget → ここ の一方通行。逆向きに参照すると循環するので、ウィジェット
# 側の値(背景色など)は引数か公開メソッド越しに受け取る。定数を一部持ち直しているのも同じ
# 理由(あちらから import できない)。
#
# 座標はすべてQtの論理座標で扱う。Win32(物理ピクセル)は一切混ぜないので、拡大率を変えた
# 環境でも変換は要らない(混ぜるときは capture_grab.device_bounds_to_logical を通すこと)。
import sys
import traceback
from pathlib import Path

from PIL import Image, ImageDraw
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

import window_tools
from qt_image import pil_to_qpixmap
from toast import show_toast

RAPTURE_ICON_PATH = Path(__file__).resolve().parent / "icons" / "rapture.png"

# 項目1つぶんの正方形の1辺(論理px)。本体の高さ(既定31px)より少し大きくしてある。
# 本体はタスクバーの時計に重ねる都合で高さを選べないが、こちらは何にも縛られないので、
# 狙って押せる大きさを優先する。
DEFAULT_ITEM_SIZE = 36

# ツールチップをパネルの横に出すときの、文字幅への足し分と隙間(論理px)。
# 内側の余白は環境で変わるので実測せず、少し多めに見ておく。
# 名前を出す小窓の内側の余白と、パネルとの隙間(論理px)。
LABEL_PADDING_X = 8
LABEL_PADDING_Y = 3
LABEL_GAP = 6
LABEL_FLIP_GAP = 6
ITEM_SIZE_MIN = 16

# アイコンの絵の周囲に空ける余白(px)。当たり判定は項目の正方形いっぱいで、絵だけを
# 当たり判定にはしない(本体の _zones と同じ理由。周りが死に領域になる)。
ICON_MARGIN = 6
ICON_SIZE_MIN = 8

# パネルの外周に空ける余白(px)。
PANEL_PADDING = 3

# カーソルが離れてからパネルを畳むまでの猶予(ms)。本体からパネルへ移る途中には必ず
# 「どちらにも乗っていない」瞬間があるので、0にすると項目へ届く前に閉じる。
DEFAULT_CLOSE_DELAY_MS = 300

# タスクバーは常に最前面なので、その上に出したものは押し上げ続けないと裏へ回る
# (taskbar_widget.TOPMOST_INTERVAL_MS と同じ値・同じ理由。あちらは import できない)。
TOPMOST_INTERVAL_MS = 500

# 背景の明るさから強調色の向きを決めるしきい値(0〜255)。taskbar_widget の文字色の判定と
# 同じ値にしてある(同じ背景を見ているので、判断が食い違うと見た目がちぐはぐになる)。
BRIGHTNESS_THRESHOLD = 140

# 背景に対して強調・枠をどれだけ寄せるか(0〜1)。暗い背景には白を、明るい背景には黒を
# 寄せる。実測値がどんな色でも「一段明るい/暗い」が必ず出る量として目で合わせた。
HIGHLIGHT_RATIO_ON_DARK = 0.30
HIGHLIGHT_RATIO_ON_LIGHT = 0.18
BORDER_RATIO = 0.35

_DEFAULT_ICON_COLOR = "#64748b"


def _guard(where: str, notify: bool = True) -> None:
    """スロットの中で起きた例外をここで止め、標準エラー(と必要なら通知)へ回す。

    PySide6 はスロットから例外が投げ切られるとプロセスごと終了する。パネルはマウスと
    タイマーだけで動くので、1回の失敗で常駐アプリが消えては割に合わない。

    taskbar_widget._guard と同じ役目だが、あちらを呼ぶと import が循環する
    (taskbar_widget → ここ の一方通行を保つ)。

    notify=False は周期タイマーと描画用。毎周期トーストを出すと画面が埋まる。"""
    traceback.print_exc()
    print(f"[tray-tools] ランチャ: {where}に失敗しました", file=sys.stderr)
    if notify:
        show_toast(f"タスクバーのランチャ\n{where}に失敗しました")


# ---------------------------------------------------------------
# アイコンの絵
#
# Rapture は icons/rapture.png、音声は AudioFeature が作ったものをそのまま使う
# (どちらも通知領域に出ているものと同じでなければならない)。残りは絵が用意されていない
# ので、feature_audio._make_icon_image と同じ流儀で Pillow で描く。
#
# 絵文字を描く手もあるが採らない。並ぶ6つのうち2つ(Rapture・音声)が「色の付いた丸に白い
# 図形」で固定されており、そこへ Segoe UI Emoji の絵を混ぜると書体も色も別物になって、
# 1列に並べたときに明らかに浮く。Pillowなら同じ64x64・同じ丸で揃えられるうえ、依存も
# 増えない(feature_audio が既に Pillow を使っている)。
# ---------------------------------------------------------------
def _draw_ruler(draw: ImageDraw.ImageDraw, color: str) -> None:
    """画面定規。目盛りの付いた物差し。"""
    draw.rounded_rectangle((10, 24, 54, 40), radius=3, fill="white")
    for x in (19, 28, 37, 46):
        draw.line((x, 24, x, 32), fill=color, width=3)


def _draw_dropper(draw: ImageDraw.ImageDraw, color: str) -> None:
    """カラーピッカー。スポイト(上のゴム球＋細い軸＋下の尖った先端)。

    軸を太くすると24pxまで縮めたときに球と一体化してマイクに見える。球は小さめ、軸は
    細く、先端は軸から色ぶんの隙間を空けて離す。"""
    draw.ellipse((37, 11, 55, 29), fill="white")
    draw.line((20, 44, 41, 23), fill="white", width=6)
    draw.line((23, 47, 26, 44), fill=color, width=3)
    draw.polygon([(10, 54), (15, 40), (24, 49)], fill="white")


def _draw_snippet(draw: ImageDraw.ImageDraw, color: str) -> None:
    """定型文。文字の並んだ紙。行は背景色で抜いて描く(白い紙の上なので)。"""
    draw.rounded_rectangle((16, 11, 48, 53), radius=4, fill="white")
    for y in (21, 29, 37):
        draw.line((22, y, 42, y), fill=color, width=4)
    draw.line((22, 45, 34, 45), fill=color, width=4)


def _draw_folder(draw: ImageDraw.ImageDraw, color: str) -> None:
    """フォルダブックマーク。タブ付きのフォルダ。"""
    draw.polygon([(10, 18), (27, 18), (32, 26), (10, 26)], fill="white")
    draw.rounded_rectangle((10, 22, 54, 48), radius=4, fill="white")


def _draw_presenter(draw: ImageDraw.ImageDraw, color: str) -> None:
    """発表者ツール。スクリーンと脚。24px相当まで縮めても「画面」に見えるよう、
    枠は塗りつぶしにして中の余白で見せる(細い線だと潰れる)。"""
    draw.rounded_rectangle((10, 14, 54, 42), radius=3, fill="white")
    draw.rectangle((15, 19, 49, 37), fill=color)
    draw.rectangle((30, 42, 34, 50), fill="white")
    draw.rectangle((21, 50, 43, 53), fill="white")


def _draw_laser(draw: ImageDraw.ImageDraw, color: str) -> None:
    """レーザーポインタ。光点と、そこから四方へ伸びる光。芯だけだと24px相当で
    ただの点になり、下のスポットライトと見分けが付かないので線を足す。"""
    for x0, y0, x1, y1 in ((32, 8, 32, 20), (32, 44, 32, 56), (8, 32, 20, 32), (44, 32, 56, 32)):
        draw.line((x0, y0, x1, y1), fill="white", width=5)
    draw.ellipse((23, 23, 41, 41), fill="white")


def _draw_spotlight(draw: ImageDraw.ImageDraw, color: str) -> None:
    """スポットライト。暗い面に丸く明るい部分を1つ。丸を白で塗り、周りを色のままに
    することで「周りが暗い」ほうを見せる。"""
    draw.rounded_rectangle((8, 12, 56, 52), radius=5, fill=color, outline="white", width=3)
    draw.ellipse((22, 20, 46, 44), fill="white")


def _draw_blank(draw: ImageDraw.ImageDraw, color: str) -> None:
    """黒画面/白画面。覆われた画面そのもの。黒と白は丸の色で描き分けるので、
    絵は共通のものを1つだけ持つ。"""
    draw.rounded_rectangle((10, 14, 54, 46), radius=4, fill="white")
    draw.rectangle((30, 46, 34, 52), fill="white")


def _draw_globe(draw: ImageDraw.ImageDraw, color: str) -> None:
    """サイトを取り込んで開く。地球儀(丸＋赤道＋経線)。

    線は白い丸を先に塗ってから色で抜く。細い白線を色の上に描くと24px相当で消えるが、
    白い面の中に色の筋を入れる形なら面積が残るので潰れにくい(定型文の絵と同じ手)。"""
    draw.ellipse((10, 10, 54, 54), fill="white")
    draw.line((12, 32, 52, 32), fill=color, width=4)
    draw.ellipse((23, 10, 41, 54), outline=color, width=4)


_GLYPHS = {
    "ruler": _draw_ruler,
    "dropper": _draw_dropper,
    "snippet": _draw_snippet,
    "folder": _draw_folder,
    "presenter": _draw_presenter,
    "globe": _draw_globe,
    "laser": _draw_laser,
    "spotlight": _draw_spotlight,
    "blank": _draw_blank,
}


def _make_item_image(color: str, glyph) -> Image.Image:
    """色の付いた丸の上に白い図形を1つ描く。feature_audio._make_icon_image と同じ作り。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=color)
    glyph(draw, color)
    return img


# 描いた絵はプロセスに1組あれば足りる(ディスプレイの数だけパネルがあるので、
# 各パネルで描き直すと同じ絵を何枚も持つことになる)。
_pixmap_cache = {}


def _item_pixmap(key: str) -> QPixmap:
    """項目のアイコン。作れなければ空の QPixmap(描画側で捨てる)。

    音声だけはここを通さない。AudioFeature が持っている「いま通知領域に出ているもの」を
    そのつど貰う必要があり、キャッシュすると切り替えても絵が古いままになる。"""
    cached = _pixmap_cache.get(key)
    if cached is not None:
        return cached

    pixmap = QPixmap()
    try:
        item = ITEMS.get(key) or {}
        icon = item.get("icon")
        if icon == "rapture":
            # 通知領域のRaptureと同じ絵にする。ここだけ描かずファイルから読む。
            if RAPTURE_ICON_PATH.exists():
                pixmap = QPixmap(str(RAPTURE_ICON_PATH))
        else:
            glyph = _GLYPHS.get(icon)
            if glyph is not None:
                pixmap = pil_to_qpixmap(
                    _make_item_image(item.get("color", _DEFAULT_ICON_COLOR), glyph)
                )
    except Exception:
        # 絵が1つ出ないだけの話。パネルごと出ないほうが困るので握りつぶす。
        _guard("アイコンの生成", notify=False)
        pixmap = QPixmap()

    _pixmap_cache[key] = pixmap
    return pixmap


# ---------------------------------------------------------------
# 並べる項目
#
# 呼び先はメソッド名(文字列)で持つ。束縛済みのメソッドを辞書に抱えると、Featureを
# 差し替えたときに古いほうを掴んだままになるうえ、名前が変わったときに import 時点では
# なく実行時に落ちる。名前で引けば「無ければ通知して何もしない」に落とせる。
# ---------------------------------------------------------------
ITEMS = {
    "capture": {
        "label": "キャプチャ",
        "feature": "screen",
        "method": "start_capture",
        # 遅延なしの即キャプチャ。通知領域の中クリック・本体の中クリックと同じ入口。
        "args": (0,),
        "icon": "rapture",
    },
    "audio": {
        "label": "音声出力切替",
        "feature": "audio",
        "method": "do_toggle",
        "args": (),
        "icon": "audio",
    },
    "ruler": {
        "label": "画面定規",
        "feature": "screen",
        "method": "start_ruler",
        "args": (),
        "icon": "ruler",
        "color": "#16a34a",
    },
    "color_picker": {
        "label": "カラーピッカー",
        "feature": "screen",
        "method": "start_color_picker",
        "args": (),
        "icon": "dropper",
        "color": "#a855f7",
    },
    "snippets": {
        "label": "定型文",
        "feature": "screen",
        "method": "start_snippet_picker",
        "args": (),
        "icon": "snippet",
        "color": "#0891b2",
    },
    "bookmarks": {
        "label": "フォルダブックマーク",
        "feature": "screen",
        "method": "start_launcher",
        "args": (),
        "icon": "folder",
        "color": "#f59e0b",
    },
    # 同梱の presenter.html を既定のブラウザで開く(設定 tools.presenter で差し替え可)。
    "presenter": {
        "label": "発表者ツール",
        "feature": "screen",
        "method": "start_presenter",
        "args": (),
        "icon": "presenter",
        "color": "#7c3aed",
    },
    # 任意のサイトを取り込んで発表者ツールとして開く(web_presenter.py)。URL を尋ねる
    # ダイアログが出るので、押してすぐ何かが起きる他の項目とは毛色が違う。既定の並び
    # (DEFAULT_ITEMS)には入れていない: パネルは縦に伸びる一方なので、使う人だけが
    # settings.json の launcher_items に足す。
    "web_presenter": {
        "label": "サイトを取り込んで開く",
        "feature": "screen",
        "method": "start_web_presenter",
        "args": (),
        "icon": "globe",
        "color": "#0d9488",
    },
    # 画面に重ねるプレゼン支援(presenter_overlay.py)。押すたびに出す/畳むが入れ替わる。
    # 出ているかどうかはここでは示せない(パネルはマウスを乗せている間だけの表示で、
    # 状態を出し続ける場所が無い)ので、状態を見たいときは通知領域のメニューを開く。
    "laser": {
        "label": "レーザーポインタ",
        "feature": "screen",
        "method": "toggle_presenter_overlay",
        "args": ("laser",),
        "icon": "laser",
        "color": "#dc2626",
    },
    "spotlight": {
        "label": "スポットライト",
        "feature": "screen",
        "method": "toggle_presenter_overlay",
        "args": ("spotlight",),
        "icon": "spotlight",
        "color": "#ca8a04",
    },
    "blackout": {
        "label": "黒画面",
        "feature": "screen",
        "method": "toggle_presenter_overlay",
        "args": ("black",),
        "icon": "blank",
        "color": "#1f2937",
    },
    "whiteout": {
        "label": "白画面",
        "feature": "screen",
        "method": "toggle_presenter_overlay",
        "args": ("white",),
        "icon": "blank",
        "color": "#94a3b8",
    },
}

# 既定の並び。上から順にパネルへ縦に並ぶ。
DEFAULT_ITEMS = ["capture", "audio", "ruler", "color_picker", "snippets", "bookmarks"]

# settings.json に書かれがちな別名を正規のキーへ寄せる。hotkeys セクションでは同じ機能が
# capture_now / audio_toggle / snippet_picker / launcher という名前で並んでおり、そちらを
# 見ながら書くと素直にその名前を書いてしまう。弾くより受け取るほうが親切。
ITEM_ALIASES = {
    "capture_now": "capture",
    "rapture": "capture",
    "audio_toggle": "audio",
    "sound": "audio",
    "screen_ruler": "ruler",
    "picker": "color_picker",
    "color": "color_picker",
    "snippet": "snippets",
    "snippet_picker": "snippets",
    "launcher": "bookmarks",
    "folder": "bookmarks",
    "presenter": "presenter",
    "web": "web_presenter",
    "site": "web_presenter",
    "presenter_web": "web_presenter",
    # プレゼン支援は hotkeys セクションでの名前(presenter_laser 等)でも受ける。
    "presenter_laser": "laser",
    "presenter_spotlight": "spotlight",
    "presenter_blackout": "blackout",
    "presenter_whiteout": "whiteout",
    "black": "blackout",
    "white": "whiteout",
}

# 同じ書き損じで毎回警告を出さないための記録(設定を読むたびに呼ばれるため)。
_warned_items = set()


def _warn_once(message: str) -> None:
    if message in _warned_items:
        return
    _warned_items.add(message)
    print(f"[tray-tools] ランチャ: {message}", file=sys.stderr)


def _as_int(value, default: int) -> int:
    """設定値を整数にする。数として読めない値が書かれていたら既定に落とす。
    設定は手で書き換えられるので、書き損じでランチャが出ないのは避ける
    (taskbar_widget._as_int と同じ作法・同じ理由)。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_items(config: dict) -> list:
    """設定から、実際に並べる項目のキーを順番どおりに返す。1つも無ければ空リスト。

    空リストを返すのは「ランチャを出さない」の意味。launcher_enabled を False にするか、
    launcher_items を [] にすれば、マウスを乗せても従来どおり本体だけになる。

    知らない名前は黙って飛ばす(警告は標準エラーへ1回だけ)。1つ書き損じただけで
    ランチャ全体が出ないより、書けているものだけ出るほうが直しやすい。"""
    if not config.get("launcher_enabled", True):
        return []

    raw = config.get("launcher_items")
    if raw is None:
        raw = DEFAULT_ITEMS
    if not isinstance(raw, (list, tuple)):
        _warn_once("launcher_items が配列ではありません。既定の並びを使います")
        raw = DEFAULT_ITEMS

    keys = []
    for value in raw:
        if not isinstance(value, str):
            _warn_once(f"launcher_items に文字列でない値があります: {value!r}")
            continue
        name = value.strip().lower()
        key = ITEM_ALIASES.get(name, name)
        if key not in ITEMS:
            _warn_once(f"知らない項目名です: {value!r}")
            continue
        if key in keys:
            # 同じものを2つ並べても押し間違えるだけなので、先に書いてあるほうを採る。
            continue
        keys.append(key)
    return keys


def _brightness(color: QColor) -> float:
    """ITU-R BT.601 の輝度。taskbar_widget._auto_text_color と同じ式。"""
    return (color.red() * 299 + color.green() * 587 + color.blue() * 114) / 1000


def _blend(color: QColor, other: QColor, ratio: float) -> QColor:
    """color を other 側へ ratio(0〜1)だけ寄せた色。

    QColor.lighter()/darker() を使わないのは、あれがHSVの明度を倍率で動かすため。
    真っ黒(明度0)には何倍しても効かず、背景はタスクバーの実測値で何色にもなりうる。
    どんな色でも必ず差が出る単純な線形補間にする。"""
    ratio = min(max(ratio, 0.0), 1.0)
    return QColor(
        round(color.red() * (1 - ratio) + other.red() * ratio),
        round(color.green() * (1 - ratio) + other.green() * ratio),
        round(color.blue() * (1 - ratio) + other.blue() * ratio),
    )


class ItemLabel(QWidget):
    """項目の名前を出す小窓。

    QToolTip を使わないのは、showText に渡した座標がそのまま使われないため。Qtは
    カーソルで文字が隠れないよう内部で下へずらすが、縦に並ぶこのパネルではその「下」が
    次の項目にあたり、どれの説明なのか分からなくなる(実際そう見えていた)。ずらす量は
    Qt側の都合で決まり、こちらからは指定できないので、自前の窓にして位置を握る。

    配色はパネルから貰う。タスクバーの色を実測して決めているので、ここだけ既定の
    ツールチップ色にすると浮く。"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.ToolTip  # タスクバーに出さず、フォーカスも奪わない
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._text = ""
        self._background = QColor("#202020")
        self._foreground = QColor("#ffffff")
        self._border = QColor("#606060")

    def apply_colors(self, background: QColor, foreground: QColor, border: QColor) -> None:
        self._background = QColor(background)
        self._foreground = QColor(foreground)
        self._border = QColor(border)
        self.update()

    def show_for(self, text: str, right: int, center_y: int, screen: QRect) -> None:
        """text を、右端が right・縦の中心が center_y になる位置に出す。

        右端で揃えるのは、パネルの左隣に並べたいため。文字数が違っても
        パネル側の辺がそろう。左が画面からはみ出すときは右隣へ回す。"""
        metrics = QFontMetrics(self.font())
        width = metrics.horizontalAdvance(text) + LABEL_PADDING_X * 2
        height = metrics.height() + LABEL_PADDING_Y * 2
        x = right - width
        if not screen.isEmpty() and x < screen.left():
            x = right + LABEL_FLIP_GAP
        self._text = text
        self.setGeometry(x, center_y - height // 2, width, height)
        self.show()
        self.raise_()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background)
        painter.setPen(self._border)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.setPen(self._foreground)
        painter.drawText(self.rect(), Qt.AlignCenter, self._text)


class LauncherPanel(QWidget):
    """タスクバーウィジェットの真上に出る、アイコンを縦一列に並べた枠なしの窓。

    ディスプレイごとのウィジェット1つにつき1枚。生成はウィジェットが行い、参照も
    ウィジェットが持つ(ローカル変数だけだとGCで即消える)。

    開閉はウィジェットと共同で決める。カーソルは本体とパネルの間を必ず一度「どちらにも
    乗っていない」状態を通って移動するので、leaveEvent が来た瞬間に閉じると項目へ届く
    前に消える。離れてから DEFAULT_CLOSE_DELAY_MS だけ待ち、そのときに本体とパネルの
    どちらにも乗っていなければ閉じる。

    閉じる判定に underMouse() や leaveEvent の有無を使わず、毎回カーソルの座標を見るのは、
    この窓が Qt.Tool でフォーカスを取らないため。ツールチップや他の窓が上に出た拍子に
    Enter/Leave が実際とずれて飛んでくる経路があり(本体がメニューで踏んだ罠と同じ)、
    座標で見ればどう飛んで来ようと結論は変わらない。

    WA_TranslucentBackground は使わない(本体と同じ流儀)。背景は必ず不透明に塗る。"""

    def __init__(self, widget, screen_feature, audio_feature, config: dict):
        super().__init__()
        # 本体のウィジェット。位置の基準・色・「閉じたときの表示の戻し」で参照する。
        # 親子(setParent)にはしない。子にすると本体の矩形へクリップされ、タスクバーの
        # 外へはみ出せない。
        self._widget = widget
        self._screen = screen_feature
        self._audio = audio_feature
        # taskbar_widget と同じ辞書(app_settings["taskbar_widget"])をそのまま持つ。
        # 開くたびに読み直すので、settings.json を直して再起動すれば並びが変わる。
        self._config = config

        self._keys = []
        self._item_size = DEFAULT_ITEM_SIZE
        self._hover_index = -1
        # 名前を出す小窓。参照を持たないとGCで消えるのでここで掴んでおく。
        self._label = ItemLabel()
        self._background = QColor("#202020")
        self._highlight = QColor("#3a3a3a")
        self._border = QColor("#606060")

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            # 本体と同じく、押しても作業中のウィンドウからフォーカスを奪わない。
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        # ボタンを押していなくても mouseMoveEvent を貰う(どの項目に乗っているかを
        # 追うため)。これが無いと強調も名前の表示も動かない。
        self.setMouseTracking(True)

        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._on_close_tick)

        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(TOPMOST_INTERVAL_MS)
        self._topmost_timer.timeout.connect(self._on_topmost_tick)

    # ---------------------------------------------------------------
    # 開閉
    # ---------------------------------------------------------------
    def is_open(self) -> bool:
        return self.isVisible()

    def _close_delay(self) -> int:
        return max(_as_int(self._config.get("launcher_close_delay_ms"), DEFAULT_CLOSE_DELAY_MS), 0)

    def notify_widget_enter(self) -> None:
        """本体にカーソルが乗った。ウィジェットの enterEvent から呼ばれる。"""
        self._close_timer.stop()
        self.open_panel()

    def notify_widget_leave(self) -> None:
        """本体からカーソルが離れた。ウィジェットの leaveEvent から呼ばれる。

        パネルへ移っただけかもしれないので、ここでは閉じずに猶予を置く。"""
        if self.isVisible():
            self._close_timer.start(self._close_delay())

    def open_panel(self) -> None:
        """項目を並べ直し、本体の真上に出す。並べるものが無ければ何もしない。"""
        if self.isVisible():
            # 既に出ている。パネルから本体へカーソルが戻ったときにここへ来るので、
            # 出し直すと今カーソルが乗っている項目の強調が飛ぶ。
            self._close_timer.stop()
            return

        keys = resolve_items(self._config)
        if not keys:
            return

        self._keys = keys
        self._item_size = max(
            _as_int(self._config.get("launcher_item_size"), DEFAULT_ITEM_SIZE), ITEM_SIZE_MIN
        )
        self._hover_index = -1
        self.refresh_colors()

        width = self._item_size + PANEL_PADDING * 2
        height = self._item_size * len(keys) + PANEL_PADDING * 2
        self.setGeometry(self._place(width, height))
        self.show()

    def close_panel(self) -> None:
        """畳む。カーソルがどこにあっても閉じる(判断は呼ぶ側が済ませている)。"""
        self._close_timer.stop()
        self._hover_index = -1
        self._label.hide()
        if self.isVisible():
            self.hide()
        # 本体は「パネルが開いている間はアイコンのまま」にしているので、閉じたことを
        # 伝えて時計へ戻す判断をさせる(メニューを閉じたあとの _popup と同じ役目)。
        try:
            self._widget.on_launcher_closed()
        except Exception:
            _guard("本体の表示の戻し", notify=False)

    def _on_close_tick(self):
        try:
            if self._cursor_on_widget_or_panel():
                # まだどちらかに乗っている。leaveEvent が飛んで来ない経路(別の窓が上に
                # 出た拍子など)で開きっぱなしにならないよう、次の周期でもう一度見る。
                self._close_timer.start(self._close_delay())
                return
            self.close_panel()
        except Exception:
            _guard("ランチャを閉じる処理", notify=False)
            self.close_panel()

    def _cursor_on_widget_or_panel(self) -> bool:
        """カーソルが本体かパネルのどちらかに乗っているか。

        本体とパネルは接しているが別の窓なので、境目をまたぐ一瞬はどちらの Enter も
        来ていない状態になる。矩形で見れば、その一瞬も「乗っている」と判定できる。"""
        pos = QCursor.pos()
        if self.isVisible() and self.geometry().contains(pos):
            return True
        return bool(self._widget.isVisible() and self._widget.geometry().contains(pos))

    # ---------------------------------------------------------------
    # 位置
    # ---------------------------------------------------------------
    def _place(self, width: int, height: int) -> QRect:
        """本体の真上・中央そろえ。画面からはみ出すぶんは画面の中へ押し戻す。

        上に入りきらないとき(本体を画面の上のほうへドラッグした等)に下方向へ回さないのは、
        このウィジェットの本来の居場所がタスクバーの上＝画面の下端だから。下へ出したパネルは
        画面の外か、良くてもタスクバーの裏になり、出しても押しに行けない。上端で止めておけば、
        少なくとも全部の項目が見えて押せる。押し戻した結果まれに本体と重なることはあるが、
        そのときも「見えていて押せる」ほうを採る。"""
        anchor = self._widget.geometry()
        x = anchor.center().x() - width // 2
        y = anchor.top() - height

        bounds = _screen_bounds(anchor)
        if not bounds.isEmpty():
            # QRect.right()/bottom() は最後のピクセルを指すので、排他的な端は +1。
            x = min(max(x, bounds.left()), bounds.right() + 1 - width)
            y = min(max(y, bounds.top()), bounds.bottom() + 1 - height)
        return QRect(x, y, width, height)

    # ---------------------------------------------------------------
    # 表示・タイマー
    # ---------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        self._topmost_timer.start()
        self._on_topmost_tick()  # 出した直後にタスクバーの上へ回す(500ms待たせない)

    def hideEvent(self, event):
        self._close_timer.stop()
        self._topmost_timer.stop()
        super().hideEvent(event)

    def _on_topmost_tick(self):
        try:
            # winId() はネイティブハンドルを必要なら作ってから返す。ctypes へ渡すので
            # int にしておく(HWNDは c_void_p で受ける側の約束)。
            window_tools.push_topmost(int(self.winId()))
        except Exception:
            _guard("最前面への押し上げ", notify=False)

    def refresh_colors(self) -> None:
        """本体が使っている色に合わせる。

        パネルの下にあるのはデスクトップや他のアプリなので、本体と同じように自分の位置を
        実測しても意味のある地の色は出ない(そのときどきの窓の色を拾うだけ)。本体が
        タスクバーで実測した色をそのまま借りて、1つの部品に見えるようにする。"""
        try:
            background, text = self._widget.current_colors()
        except Exception:
            _guard("色の取得", notify=False)
            return
        self._background = QColor(background)
        ratio = (
            HIGHLIGHT_RATIO_ON_LIGHT
            if _brightness(self._background) >= BRIGHTNESS_THRESHOLD
            else HIGHLIGHT_RATIO_ON_DARK
        )
        other = QColor("#000000") if ratio == HIGHLIGHT_RATIO_ON_LIGHT else QColor("#ffffff")
        self._highlight = _blend(self._background, other, ratio)
        self._border = _blend(self._background, QColor(text), BORDER_RATIO)
        self._label.apply_colors(self._background, QColor(text), self._border)
        self.update()

    def notify_audio_changed(self) -> None:
        """音声デバイスが切り替わった(本体が読み直した)。出ていれば描き直す。

        絵は AudioFeature から毎回貰うので、こちらは描き直しの合図だけでよい。"""
        if self.isVisible():
            self.update()

    # ---------------------------------------------------------------
    # 描画
    # ---------------------------------------------------------------
    def _item_rect(self, index: int) -> QRect:
        """index番目の項目の矩形(パネル内のローカル座標)。

        設定に書いた順で上から下へ並べる。カーソルにいちばん近いのは下端なので「よく使う
        ものを下に」という並べ方もありうるが、settings.json の並びと見た目の並びが逆さま
        だと、直したときにどれが動いたのか分からなくなる。見たままを採る。"""
        return QRect(
            PANEL_PADDING,
            PANEL_PADDING + index * self._item_size,
            self._item_size,
            self._item_size,
        )

    def _icon_rect(self, cell: QRect) -> QRect:
        size = max(min(cell.width(), cell.height()) - ICON_MARGIN * 2, ICON_SIZE_MIN)
        return QRect(
            cell.x() + (cell.width() - size) // 2,
            cell.y() + (cell.height() - size) // 2,
            size,
            size,
        )

    def _index_at(self, pos) -> int:
        """その座標にある項目の番号。どれにも当たらなければ -1(外周の余白)。"""
        for index in range(len(self._keys)):
            if self._item_rect(index).contains(pos):
                return index
        return -1

    def _pixmap_for(self, key: str):
        if key == "audio":
            # 通知領域に出ているものと同じ絵。読み直し(COM越し)はしない。本体がマウスを
            # 乗せている間だけ回しているタイマーで既に最新に保たれている。
            return self._audio.current_icon_pixmap()
        return _item_pixmap(key)

    def paintEvent(self, event):
        painter = QPainter(self)
        # 下にあるのは他のアプリの窓なので、まず不透明に塗り潰す。
        painter.fillRect(self.rect(), self._background)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        # 枠。地の色だけだと、背景が似た色のアプリの上に出たときに境目が分からない。
        painter.setPen(self._border)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

        for index, key in enumerate(self._keys):
            cell = self._item_rect(index)
            if index == self._hover_index:
                # どれを押すのかが分からないと使えないので、乗っている項目を必ず塗る。
                painter.setPen(Qt.NoPen)
                painter.setBrush(self._highlight)
                painter.drawRoundedRect(cell.adjusted(1, 1, -1, -1), 4, 4)
            pixmap = self._pixmap_for(key)
            if pixmap is None or pixmap.isNull():
                continue
            painter.drawPixmap(self._icon_rect(cell), pixmap)

    # ---------------------------------------------------------------
    # マウス
    # ---------------------------------------------------------------
    def enterEvent(self, event):
        try:
            self._close_timer.stop()
        except Exception:
            _guard("ランチャへのカーソル進入の処理", notify=False)

    def leaveEvent(self, event):
        try:
            self._hover_index = -1
            self.update()
            # 本体へ戻る途中かもしれないので、ここでも即座には閉じない。
            self._close_timer.start(self._close_delay())
        except Exception:
            _guard("ランチャからのカーソル離脱の処理", notify=False)

    def mouseMoveEvent(self, event):
        try:
            index = self._index_at(event.position().toPoint())
            if index == self._hover_index:
                return
            self._hover_index = index
            self.update()
            self._show_label(index, event.globalPosition().toPoint())
        except Exception:
            _guard("カーソル位置の追跡", notify=False)

    def _show_label(self, index: int, global_pos) -> None:
        """乗っている項目の名前を出す。絵だけでは何のアイコンか分からない。

        パネルの中に文字を入れる案は採らない。名前が入る幅(「フォルダブックマーク」で
        100px超)まで広げると、タスクバーの上に常時それだけの面積が居座ることになり、
        「アイコンを縦に並べる」という形が崩れる。ツールチップなら幅を取らない。

        出す位置はパネルの左隣、項目と同じ高さ。詳しくは ItemLabel を参照。"""
        label = "" if index < 0 else (ITEMS.get(self._keys[index], {}).get("label") or "")
        if not label:
            self._label.hide()
            return
        cell = self._item_rect(index)
        center_y = self.mapToGlobal(QPoint(0, cell.center().y())).y()
        self._label.show_for(
            label,
            self.geometry().left() - LABEL_GAP,
            center_y,
            _screen_bounds(self.geometry()),
        )

    def mousePressEvent(self, event):
        try:
            if event.button() != Qt.LeftButton:
                return
            index = self._index_at(event.position().toPoint())
            if index < 0:
                return
            key = self._keys[index]
            # 先に畳んでから呼ぶ。キャプチャ・定規・カラーピッカーは全画面のオーバーレイを
            # 出すので、パネルが残っているとその上に乗り上げる(こちらは最前面に居続ける)。
            self.close_panel()
            self._invoke(key)
        except Exception:
            _guard("項目の実行")

    def _invoke(self, key: str) -> None:
        """項目に紐づいた機能を呼ぶ。

        呼び先はメソッド名で引く。名前が変わって見つからないときは、落とさずに通知して
        何もしない(常駐アプリなので、1つ壊れただけで全部止めるわけにいかない)。"""
        item = ITEMS.get(key)
        if item is None:
            return
        target = self._audio if item.get("feature") == "audio" else self._screen
        method = getattr(target, item.get("method", ""), None)
        if not callable(method):
            print(
                f"[tray-tools] ランチャ: {item.get('label')} の呼び先が見つかりません"
                f" ({item.get('method')})",
                file=sys.stderr,
            )
            show_toast(f"タスクバーのランチャ\n{item.get('label')}を呼び出せません")
            return
        method(*item.get("args", ()))


def _screen_bounds(rect: QRect) -> QRect:
    """その矩形がいちばん広く重なっている画面の矩形。どれとも重ならなければ空の矩形。

    中心点で screenAt() を引くのではなく重なりの面積で選ぶ(taskbar_widget._screen_for と
    同じ理由。タスクバーの矩形は画面の端いっぱいに置かれ、環境によっては数px はみ出す)。

    返すのはQtの論理座標。Win32から貰った値は一切混ざっていないので変換は不要。"""
    best = QRect()
    best_area = 0
    if QGuiApplication.instance() is None:
        return best
    for screen in QGuiApplication.screens():
        overlap = screen.geometry().intersected(rect)
        area = overlap.width() * overlap.height()
        if area > best_area:
            best_area = area
            best = screen.geometry()
    return best
