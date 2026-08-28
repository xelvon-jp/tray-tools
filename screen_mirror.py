# screen_mirror.py
# 手元の画面の一部を切り取り、別のモニタへ全画面でミラーする「画面ミラー」。
#
# 使い方はこうなる。手元(モニタ1)で範囲を選ぶ → 画面共有に出しているモニタ2へ、その
# 範囲だけが全画面で映る。範囲を選んだあとも中は普通に操作できる(ミラーは見ているだけ)
# ので、ブラウザでリンクを辿ろうが PowerPoint を送ろうが、そのまま向こうに映る。
#
# なぜこの方式か。同梱の presenter.html はローカルHTML資料専用で、リンク遷移をすると
# 制御から外れる。WebEngine を内蔵して自前のブラウザにする案もあったが、それは実質
# ブラウザを作ることになる。画面をそのまま映せば操作の問題が丸ごと消える——対象が
# ブラウザでも PowerPoint でも PDF でも関係ない。presenter.html はローカル資料用として
# そのまま残す(併存)。画面へ重ねる側(presenter_overlay.py)も残す。
#
# レーザーとスポットライトの描き方は presenter_overlay.py と共有している
# (presenter_overlay.draw_laser / draw_spotlight / laser_params / spotlight_params)。
# あちらは「カーソルのある画面に重ねる」もの、こちらは「ミラー先に描く」ものだが、
# 光点の暈もスポットの穴も設定キーも同じでよいので、描画と設定読みだけをあちらへ寄せた。
# 2箇所に同じ絵を書くと、片方だけ直したときに見た目が食い違う。
#
# 選ぶ範囲の大きさは自由で、固定されるのは縦横比だけ(既定 16:9)。800x450 でも
# 1600x900 でも 1920x1080 でも選べる。そのぶん、大きく選ぶほど重くなる。
#
# ホットキー(Ctrl+Alt+P)は「開始」と「範囲の選び直し」。以前は押すたびに開始/終了の
# トグルだったが、発表の途中で映す場所を変えたいときに、いったん共有が途切れて
# しまうのが困る。今は、ミラー中に押すと映したまま範囲選択だけをやり直す
# (選び直しの最中はミラーを静止させる。範囲選択の減光オーバーレイが全モニタを覆うので、
# そのまま撮ると向こうには減光した画面が映ってしまう)。
# 終わらせる手段は別に3つある——トレイメニューの「⏹ 終了」、手元のツールバーの ✕、
# 専用ホットキー(既定 Ctrl+Alt+Q)。押しても終われない状態を作らないこと。
#
# 手元の枠(SourceFrameWindow)の下にはツールバー(MirrorToolbarWindow)を出す。
# レーザー・スポットライト・黒画面・白画面・カンペ・静止・選び直し・終了をアイコンで
# 並べたもの。枠と同じで撮影範囲の外に置く(自分を撮ると無限に入れ子になる)。
# 右側の説明欄は掴みしろも兼ねていて、掴んでドラッグすると範囲ごと動き(move_source_by)、
# その右端の ◢ を掴むと範囲の大きさが変わる(begin_resize / resize_source_to)。どちらも
# 映したまま効くので、少し動かしたい・少し広げたいだけのときに選び直さずに済む。
#
# 範囲の右隣にはカンペ(MirrorNotesWindow)を出す。notes/ フォルダに置いた Markdown を
# 読むだけのパネルで、# の見出しを ◀ ▶ でワンクリックで行き来できる。中身のファイルは
# mirror_notes.py が受け持つ。ここも撮影範囲の外に置くが、理由は枠やツールバーとは
# 違う——入れ子になるからではなく、カンペは発表者だけが見るものだから。掛かった時点で
# 手元のメモが共有側に丸見えになるので、置けないなら出さない(右が無理なら左、それも
# 無理なら出さない)。共有側には何も足さない。
#
# 静止(フリーズ)は、手元で資料を切り替える間その様子を見せないための機能。止めている
# 間は撮らないので、向こうには最後の1枚が出たままになる。気付かずに静止したまま話し
# 続けるのが最悪なので、手元の枠を橙に変え、帯に「静止中」と出し、ツールバーのアイコン
# も点灯させる(ミラー先には何も出さない。見ている側に知らせる情報ではない)。
#
# 実測(このPC。設計の根拠)。capture_grab.grab_region() 1枚あたり:
#   1280x720   16.9ms  → 30fpsで回して 29.6fps / CPU 71%(1コア換算)
#   1920x1080  33.5ms  → 30fpsで回して 25.8fps / CPU 108%(コマ落ちする)
# 拡大は QPixmap.scaled() の 2560x1440 で 7.2ms(SmoothTransformation)。既定を30fpsにし、
# 負荷が問題になる環境では設定 fps で落とせる。1920x1080 を映したいなら fps を 20 程度に
# するのが現実的。
#
# 拡大すれば必ず滲む。Qt の選択肢は SmoothTransformation(双線形。滑らかだが滲む)と
# FastTransformation(最近傍。鮮明だがブロック状)の2つしかなく、間は無い。本当の解は
# 「等倍で映すこと」で、そのために範囲選択のプリセットに「ミラー先と同じ解像度」を
# 入れてある。等倍なら補間そのものが起きないので、両方式の出力が1画素も違わない(実測で
# 完全一致)。
#
# 等倍にできないときのために scaling 設定を持たせた(smooth / fast / auto、既定 auto)。
# auto は倍率が整数のときだけ Fast、それ以外は Smooth。根拠は実測(640x360 の文字と細線を
# drawImage で拡大し、画素を数えたもの):
#
#   2倍    Smooth: 元に無い中間色が 16.8% / 隣接画素の差の平均  9.42 / 1.63ms
#          Fast  : 中間色 0%             / 隣接画素の差の平均 12.15 / 1.30ms ← 鮮明
#   1.33倍 1px幅の縦線が Fast では 1px(58本)と 2px(28本)に割れる(2倍なら全部2px)
#
# 整数倍では Fast が明確に鮮明で(輪郭のコントラストが約1.3倍残り、滲みの正体である
# 中間色が1つも生えない)、半端な倍率では線の太さが場所によって変わって汚くなる。
# だから整数倍だけ Fast にする。速さの差はどちらの向きにも小さく、選ぶ理由にしていない。
#
# 大きい範囲も選べてしまうので、目安を2か所に出す。範囲選択中はその大きさで出せる
# おおよそのフレームレート(estimated_fps)、ミラー中は実測値を手元の枠の帯に出す
# (帯は範囲の外にあるのでミラーには映り込まない)。止めはしない——止めるほどのことでは
# ないし、重くても映したい場面はある。数字が見えていれば自分で選べる。
#
# 座標はすべてQtの論理座標で扱う。Win32(物理ピクセル)を混ぜるのはマウスのボタン状態を
# 読む GetAsyncKeyState だけで、あれは座標を返さないので変換は要らない
# (座標を混ぜるときは capture_grab.device_bounds_to_logical を通すこと)。
import ctypes
import sys
import time
import traceback

from PIL import Image, ImageDraw
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QRegion,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import mirror_notes
import presenter_overlay
from capture_grab import grab_region
from capture_overlay import SelectionOverlay
from qt_image import pil_to_qpixmap
from toast import show_toast

# ---------------------------------------------------------------
# 既定値
#
# settings.py の DEFAULT_SETTINGS["screen_mirror"] と同じ値を持っている。あちらから
# import しないのは、settings.py をPySide6に依存させないため(presenter_overlay と同じ理由)。
# 値を変えるときは両方。
# ---------------------------------------------------------------

# 1秒あたり何枚送るか。上の実測のとおり、1280x720 なら 30fps でほぼ落ちずに回る。
# 上限を60にしてあるのは、これ以上を指定できても1枚17msの取得が追いつかないため
# (指定だけ増えてコマ落ちが増える)。
DEFAULT_FPS = 30
MAX_FPS = 60

# 選択範囲の比率。キー(1/2/3)とホイールで選択中に切り替えられる。
# 値は (設定に書く名前, 画面に出す名前, 幅÷高さ or None)。
ASPECTS = (
    ("free", "自由", None),
    ("4:3", "4:3", 4.0 / 3.0),
    ("16:9", "16:9", 16.0 / 9.0),
)
DEFAULT_ASPECT = "16:9"

# ミラー先に描くカーソル。実カーソル(GetCursorInfo/DrawIconEx)は使わず、矢印を自前で描く。
# 拡大率によらず一定の大きさにする(どの範囲を選んでも見え方が揃う)。少し大きめなのは、
# 画面共有で相手に届くころには縮んでいるため。
DEFAULT_CURSOR_SIZE = 34
DEFAULT_CURSOR_COLOR = "#ffffff"
DEFAULT_CURSOR_OUTLINE = "#101010"

# クリックの可視化。画面共有では「今押した」が伝わりにくいので、押した瞬間に波紋を出す。
DEFAULT_CLICK_RIPPLE = True
DEFAULT_CLICK_RIPPLE_MS = 420
DEFAULT_CLICK_RIPPLE_RADIUS = 62
DEFAULT_CLICK_RIPPLE_COLOR = "#ffd400"

# 手元に出す「いまミラーしている範囲」の枠。
DEFAULT_SOURCE_FRAME = True
DEFAULT_SOURCE_FRAME_COLOR = "#00c8ff"
DEFAULT_SOURCE_FRAME_WIDTH = 3
DEFAULT_SOURCE_FRAME_OPACITY = 0.55

# 静止(フリーズ)中の枠の色。手元だけで分かればよいので、通常の枠より目立たせる
# (気付かずに静止したまま話し続けるのが最悪。ここは主張してよい)。
DEFAULT_FREEZE_FRAME_COLOR = "#ff8c00"
DEFAULT_FREEZE_FRAME_OPACITY = 0.9

# 黒画面/白画面をミラー先だけに出している間の枠の色。静止とは別の色にする——手元は
# 普通に見えているので、見た目だけでは「向こうに何が出ているか」がまったく分からない。
# 気付かずに黒画面のまま話し続けるのは静止と同じかそれ以上に最悪なので、ここも主張する。
DEFAULT_BLANK_FRAME_COLOR = "#a855f7"
DEFAULT_BLANK_FRAME_OPACITY = 0.9

# 手元の枠に添える帯の高さ(論理px)。実測フレームレートと範囲の大きさをここへ出す。
# 帯は選択範囲の外側(枠のさらに外)にあるのでミラーには映り込まない。
SOURCE_FRAME_BAND_HEIGHT = 18
SOURCE_FRAME_BAND_INTERVAL_MS = 1000

# 手元の枠の下に出すツールバー。ボタン1つぶんの正方形の1辺と、右に添える説明の幅
# (どちらも論理px)。説明は「乗せているボタンの名前」を出す場所で、別窓のツールチップを
# 出さずに済ませるためにツールバーの中に持っている(窓が1枚増えると最前面の押し合いが
# 増えるうえ、撮影範囲に入らない置き場所をもう1つ探すことになる)。
DEFAULT_TOOLBAR = True
TOOLBAR_BUTTON_SIZE = 30
TOOLBAR_PADDING = 3
TOOLBAR_LABEL_WIDTH = 190
TOOLBAR_GAP = 2
# アイコンの点灯/消灯を実物へ合わせ直す間隔(ms)。レーザー等はホットキーやトレイメニュー
# からも切り替わるので、押されたときだけ描き直すと嘘の状態が残る。
TOOLBAR_REFRESH_INTERVAL_MS = 400
# 消えているアイコンの丸の色。点いているときは項目ごとの色になる(_TOOLBAR_ITEMS)。
TOOLBAR_OFF_COLOR = "#475569"
TOOLBAR_BACKGROUND = QColor(20, 20, 20, 225)
# ツールバー右側の説明欄は、そのままミラー範囲を動かすための「タイトルバー」も兼ねる。
# 別に帯を1本足すより、既にある窓の使っていない場所を使うほうが窓が増えない
# (窓が増えると最前面の押し合いが増え、撮影範囲の外に置き場所をもう1つ探すことになる)。
# アイコンの上をドラッグしても動かないのは意図的。押すつもりで少し滑っただけで範囲ごと
# 動いては困る(付箋で「画像部分のドラッグで動く」をやめたのと同じ理由)。
TOOLBAR_GRIP_WIDTH = 12
# 説明欄の右端から分ける、大きさを変える掴みしろの幅(論理px)。移動(欄のどこでも掴める)
# と取り違えないよう、拡縮はこの帯の中だけにする。窓の右下の ◢ と同じ役どころ。
TOOLBAR_RESIZE_WIDTH = 18
# ドラッグ後に出す一言(半径や暗さの値など)を消すまでの時間(ms)。
TOOLBAR_HINT_MS = 2500

# ---------------------------------------------------------------
# 手元のカンペ(発表者だけが見るメモ)
#
# 撮影範囲の右隣に置くパネル。中身は notes/ フォルダの Markdown で、ファイルの面倒は
# mirror_notes.py が見る。枠・ツールバーと同じで、撮影範囲に1画素も掛けてはいけない
# (掛かれば共有側に丸見えになる。カンペは「手元だけに見えるもの」なので、これは
# 入れ子になるより悪い——見せてはいけないものが見えるということ)。
# ---------------------------------------------------------------
DEFAULT_NOTES = True
# パネルの幅(論理px)。設定 notes_width で変えられる。
DEFAULT_NOTES_WIDTH = 380
# これより狭くするくらいなら出さない。折り返しだらけで読めないものを置いても、
# 撮影範囲の外の場所を1つ潰すだけで得が無い。
NOTES_MIN_WIDTH = 240
# 撮影範囲が小さいときでも、これだけの高さは確保する(範囲と同じ高さでは数行しか
# 読めない)。伸ばす向きは上——下へ伸ばすとツールバーに掛かる。
NOTES_MIN_HEIGHT = 320
# 枠との隙間(論理px)。0にすると枠と地続きに見えて、どこまでが撮影範囲か分かりにくい。
NOTES_GAP = 6
# 本文の文字。発表中に手元で読むものなので、その場で変えられる(A-/A+ と Ctrl+ホイール)。
DEFAULT_NOTES_FONT_SIZE = 11
NOTES_FONT_MIN = 7
NOTES_FONT_MAX = 32
NOTES_FONT_FAMILY = "Meiryo"
# 文字の大きさとカンペの選択を settings.json へ書き戻すまでの待ち(ms)。Ctrl+ホイールは
# 1回転で何目盛りも飛んでくるので、そのたびに書くとファイルを何十回も開き直すことになる
# (SPOTLIGHT_SAVE_DELAY_MS と同じ理由・同じ値)。
NOTES_SAVE_DELAY_MS = 800
# 目次/一覧の高さの上限(論理px)。本文が見えなくなるほど広げない。
NOTES_SIDE_HEIGHT = 150

# スポットライトをその場で調整する幅。ホイール1目盛りぶん。
SPOTLIGHT_RADIUS_STEP = 10
SPOTLIGHT_RADIUS_MIN = 20
SPOTLIGHT_RADIUS_MAX = 1200
SPOTLIGHT_DIM_STEP = 0.05
SPOTLIGHT_DIM_MIN = 0.0
# 1.0(真っ黒)まで許すと、周りが完全に潰れて「どこを指しているか」以外は何も見えなくなる。
# スポットライトは資料の一部に注目させる道具で、資料を消す道具ではない。
SPOTLIGHT_DIM_MAX = 0.95
# 調整した値を settings.json へ書き戻すまでの待ち(ms)。ホイールは1回転で何目盛りも
# 飛んでくるので、そのたびに書くとファイルを何十回も開き直すことになる。
SPOTLIGHT_SAVE_DELAY_MS = 800

# ミラー範囲をドラッグで動かすときの歯止め。画面(全モニタの合算)の中へこれだけは
# 残す(論理px)。範囲まるごと画面外へ送れてしまうと、手元から掴む手段が無くなる。
# 大きい範囲では、これより先に「中心が画面の中に残ること」のほうが効く(move_source_by)。
MOVE_MIN_VISIBLE = 120

# ---------------------------------------------------------------
# ミラー範囲の拡縮(ツールバー右端の ◢ をドラッグ / ホイール)
#
# 移動だけあって大きさが変えられなかったので、少し広げたいだけでも Ctrl+Alt+P で
# 選び直すしかなかった。選び直すとオーバーレイが全モニタを覆うぶん共有側が一瞬止まる
# ので、微調整には大げさすぎる。
#
# 掴みしろをツールバーの右端に置いたのは、他の置き場所がどれも高くつくため。
#   ・枠(SourceFrameWindow)の角や辺 … あの窓は WindowTransparentForInput で、自分では
#     マウスを受け取れない。透過をやめると、穴(＝撮影範囲)の外周でクリックを吸い始める
#     ので、映しながら操作するというこの機能の前提が崩れる。
#   ・四隅に小さい窓を置く … 最前面の窓が4枚増える。このアプリでは窓が増えるたびに
#     最前面の押し合いが増え、撮影範囲に掛からない置き場所を探す仕事も増えている
#     (TOOLBAR_GRIP_WIDTH のコメントと同じ話)。掴みしろ4つのために払う額ではない。
# ツールバーは既に「撮影範囲の外に置けた矩形」で、右端の説明欄は移動用の掴みしろも
# 兼ねている。そこへ幅 TOOLBAR_RESIZE_WIDTH ぶんを分けるのがいちばん安い。
# ---------------------------------------------------------------

# 下限(論理px)。240x135 は 16:9 の最小で、ミラー先2560x1440へ出すと10.6倍——ここまで
# 拡大すると何を映しても滲んで読めない。これ以下を選べても得が無い。
SIZE_MIN_WIDTH = 240
SIZE_MIN_HEIGHT = 135
# 上限(論理px)。GRAB_MS_PER_PIXEL の実測から 3840x2160 は1枚122ms(=8fps)で、ここが
# 「映せるが使い物にならない」の入口になる。実際にはデスクトップ全体の大きさとも
# 突き合わせる(存在しない広さは撮れない)。
SIZE_MAX_WIDTH = 3840
SIZE_MAX_HEIGHT = 2160
# 掴んで動かしている間、実際に窓へ反映する間隔(ms)。マウスは毎秒100回以上動くが、
# 1回あたり枠の setMask を張り直し、ツールバーとカンペの置き場所を計算し直して動かす
# ことになる。そのたびに全部やるほどの価値は無い(このプロジェクトは「よく走る経路で
# 重い処理をしない」で何度も痛い目を見ている)。間引いても取りこぼさないのは、大きさを
# 「掴んだ点からの絶対量」で決めているから——飛ばした回の結果は次の1回に含まれる。
SIZE_APPLY_INTERVAL_MS = 16
# ホイール1目盛りで変える幅(論理px)。Shift/Ctrl を押している間は細かく。
# ドラッグでは狙いにくい「あと少しだけ」を、数字を見ながら詰めるための道。
SIZE_WHEEL_STEP = 64
SIZE_WHEEL_FINE_STEP = 16
# ホイールで変えたあと、枠とツールバーを作り直すまでの待ち(ms)。1目盛りごとに作り直すと
# 掴んでいる窓が消えるうえ、続けて回している間ずっと窓を捨てては作ることになる。
SIZE_SETTLE_DELAY_MS = 450

# 拡大方法。"smooth"(双線形) / "fast"(最近傍) / "auto"(倍率が整数なら fast)。
# 詳しくは冒頭の実測コメント。
SCALING_MODES = ("auto", "smooth", "fast")
DEFAULT_SCALING = "auto"
# 「整数倍」と見なす許容差。ミラー先2560x1440へ1280x720を映すとちょうど2.0になるが、
# 端数のある構成では 1.9999… になりうる。
SCALE_INTEGER_TOLERANCE = 0.002

# 範囲選択中に一発で選べる矩形。settings.json の screen_mirror.presets で置き換えられる。
#
#   width/height … 大きさ(論理px)。"size": "target" と書くとミラー先のモニタと同じ
#                  解像度になる(＝等倍。拡大しないので1画素も滲まない)。
#   x/y          … 始点。省略するとその画面の中央に置く。既定では「今カーソルのある
#                  モニタの左上」からの相対で、"screen" に QScreen.name() を書けば
#                  そのモニタに固定できる。相対にしてあるのは、(0,100) のような値が
#                  「作業しているモニタのタスクバー/アドレスバーを外した位置」の意味で
#                  書かれるため。絶対座標にすると、プライマリでないモニタで作業して
#                  いる人の手元では別のモニタを指してしまう。
# 先頭が「範囲を選ばずに開始したとき」の既定になる。画面の左上へ寄せ、縦だけ
# タイトルバーのぶん下げてある("titlebar"。ブラウザやアプリの枠を外して中身から
# 映したいため)。settings.py の DEFAULT_SETTINGS と同じ内容を持つ規約なので、
# 片方を変えたらもう片方も直すこと。
DEFAULT_PRESETS = (
    {"label": "FHD（左上）", "x": 0, "y": "titlebar", "width": 1920, "height": 1080},
    {"label": "等倍（ミラー先と同じ）", "size": "target"},
    {"label": "上を100空ける", "x": 0, "y": 100, "width": 1600, "height": 900},
    {"label": "HD", "width": 1280, "height": 720},
)
# プリセットの一覧を範囲選択の画面に出す枠。Ctrl+数字でも選べる。
PRESET_PANEL_MARGIN = 28
PRESET_PANEL_WIDTH = 380
PRESET_ROW_HEIGHT = 26
PRESET_PANEL_PADDING = 10
# Ctrl+1〜9 まで。10個目からはクリックでだけ選べる(数字が2桁になると押しにくい)。
PRESET_MAX_KEYS = 9

# 範囲の大きさから所要時間を見積もるための係数。上の実測2点を直線で結んだもの。
#   1280x720  (921,600px) → 921600*GRAB_MS_PER_PIXEL + GRAB_MS_BASE = 16.9ms
#   1920x1080(2,073,600px) →                                          33.4ms
# 拡大のぶん(SCALE_MS)は範囲の大きさではなくミラー先の大きさで決まるので定数で置く。
# あくまで「このPCでの目安」で、正確さは求めていない(選ぶときに桁が分かればよい)。
GRAB_MS_PER_PIXEL = 1.43e-5
GRAB_MS_BASE = 3.7
SCALE_MS = 7.2

# 最前面へ押し上げ直す間隔(ms)。ミラー先のモニタには会議アプリの窓も居るので、
# 押し上げ続けないと裏へ回って何も見えなくなる
# (presenter_overlay.TOPMOST_INTERVAL_MS と同じ値・同じ理由)。
TOPMOST_INTERVAL_MS = 500

# 矢印カーソルの形。高さ1.0・幅0.60 に正規化した多角形で、描くときに cursor_size 倍する。
# Windowsの標準カーソルと同じ「左上が尖った矢印」の輪郭。数字を正規化して持つのは、
# 大きさを設定で変えても形が崩れないようにするため。
_CURSOR_POLYGON = (
    (0.00, 0.00),
    (0.00, 0.85),
    (0.20, 0.66),
    (0.34, 1.00),
    (0.48, 0.94),
    (0.34, 0.62),
    (0.60, 0.61),
)

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02

# Zオーダーで「自分のひとつ手前(上)」を引く。GetWindow の第2引数。
GW_HWNDPREV = 3
# 手前を何枚まで遡って調べるか。最前面グループの上に何十枚も居ることは無いので、
# ここに当たったら「調べきれなかった」とみなして押し上げる側に倒す。
Z_ORDER_SCAN_LIMIT = 64

_user32 = ctypes.windll.user32
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_user32.GetWindow.restype = ctypes.c_void_p
_user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
_user32.IsWindowVisible.restype = ctypes.c_int


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


_user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
_user32.GetWindowRect.restype = ctypes.c_int


def _window_bounds(hwnd):
    """ウィンドウの矩形(物理px, 排他的な right/bottom)。取れなければ None。"""
    rect = _RECT()
    if not _user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        return None
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return rect


def _overlapped_from_above(hwnd: int) -> bool:
    """自分より手前に、自分の矩形へ重なっている可視ウィンドウが居るか。

    最前面を保つために raise_() を撒くのをやめるための判定。押し上げは「Zオーダーを
    触る」操作で、そのたびにWindowsは並びを組み替え、カーソルの下にあるウィンドウへ
    当たり判定とカーソル形状の問い合わせを出し直す。ミラー中は枠・ツールバー・ミラー窓の
    3枚が500msごとにこれをやり、さらにタスクバーウィジェットも同じ周期で押し上げていた。
    誰にも覆われていないなら押し上げる必要は無い。

    判断が付かないときは True(＝押し上げる)に倒す。ここが誤って False を返すと、
    ミラー窓が会議アプリの裏へ回って共有先に何も映らなくなる——ちらつきより遥かに重い。"""
    try:
        mine = _window_bounds(hwnd)
        if mine is None:
            return True
        current = hwnd
        for _ in range(Z_ORDER_SCAN_LIMIT):
            current = _user32.GetWindow(ctypes.c_void_p(current), GW_HWNDPREV)
            if not current:
                return False  # 手前に誰も居ない＝いちばん上に居る
            if not _user32.IsWindowVisible(ctypes.c_void_p(current)):
                continue
            other = _window_bounds(current)
            if other is None:
                continue
            if (
                other.left < mine.right
                and other.right > mine.left
                and other.top < mine.bottom
                and other.bottom > mine.top
            ):
                return True
        return True
    except Exception:
        return True


def _guard(where: str, notify: bool = True) -> None:
    """スロットの中で起きた例外をここで止め、標準エラー(と必要なら通知)へ回す。

    PySide6 はスロットから例外が投げ切られるとプロセスごと終了する。ミラーは
    毎フレーム走るタイマーで動くので、1回の失敗で常駐アプリごと消えては割に合わない。
    しかも通常起動は pythonw.exe で標準エラーがどこにも出ず、落ちた理由が残らない。

    notify=False は周期タイマーと描画用。毎フレームトーストを出すと画面が埋まる
    (presenter_overlay._guard と同じ役目・同じ理由)。"""
    traceback.print_exc()
    print(f"[tray-tools] 画面ミラー: {where}に失敗しました", file=sys.stderr)
    if notify:
        show_toast(f"画面ミラー\n{where}に失敗しました")


def _as_int(value, default: int) -> int:
    """設定値を整数にする。読めない値が書かれていたら既定に落とす
    (settings.json は手で編集する前提。書き損じで機能ごと出ないのは避ける)。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float, minimum: float = 0.0, maximum: float = None) -> float:
    """設定値を小数にして範囲へ丸める。不透明度は0〜1の外を書かれると描画が破綻する。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < minimum:
        return minimum
    if maximum is not None and number > maximum:
        return maximum
    return number


def _as_color(value, default: str) -> QColor:
    """設定値を色にする。QColor は不正な文字列でも例外を投げず無効な色を返す。"""
    color = QColor(value) if isinstance(value, str) and value else QColor()
    return color if color.isValid() else QColor(default)


def mirror_config(app_settings: dict) -> dict:
    """settings.json の screen_mirror セクション。無ければ空辞書。"""
    return (app_settings or {}).get("screen_mirror", {}) or {}


def cursor_pos() -> QPoint:
    """カーソルの位置(Qt論理座標)。

    関数にしてあるのは検証のため。実際にカーソルを動かさずに確かめたいので、
    テスト側は screen_mirror.cursor_pos を差し替える(呼び出しは毎回この名前を
    モジュールから引くので、差し替えがそのまま効く)。"""
    return QCursor.pos()


def available_screens() -> list:
    """今つながっているモニタ(QScreen)の一覧。

    cursor_pos と同じで、関数にしてあるのは検証のため。モニタが3枚以上ある構成の
    挙動を、実機を繋がずに確かめられるようにしてある(名前と矩形だけを持つ偽物を
    並べて差し替える)。呼ぶ側は毎回この名前をモジュールから引くこと。"""
    return QGuiApplication.screens()


def desktop_bounds() -> QRect:
    """全モニタを合わせた矩形。範囲を動かす/大きさを変えるときの「画面の中」の定義。

    available_screens() を通すので、検証でモニタ構成を差し替えればここも一緒に変わる。"""
    bounds = QRect()
    for screen in available_screens():
        bounds = bounds.united(screen.geometry())
    return bounds


def mouse_buttons_down() -> tuple:
    """左・右ボタンが今押されているか (left, right)。

    グローバルなマウスフックは使わない。keyboard ライブラリのフックはCOMとGCが衝突して
    プロセスごと落ちる持病があり(hotkeys.init_keyboard のコメント参照)、同じ轍を踏みたく
    ない。ミラーは既にフレームごとのループを持っているので、そこで GetAsyncKeyState を
    読めば足りる(30fpsなら33ms以内に必ず気付く)。

    下位ビット(0x0001)は「前回呼んでから押されたか」で、他のアプリが同じAPIを呼ぶと
    そちらに取られる。今の状態を示す上位ビット(0x8000)だけを見て、押した瞬間の判定は
    こちらで前フレームと比べて出す。"""
    try:
        left = bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        right = bool(_user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)
    except OSError:
        # ctypes の呼び出しが失敗することは通常無いが、ここで投げるとフレームごとに
        # 落ちることになる。クリック波紋が出ないだけにしておく。
        return False, False
    return left, right


def aspect_ratio(key: str):
    """比率の名前("16:9"等)から 幅÷高さ を引く。自由なら None。"""
    for name, _label, ratio in ASPECTS:
        if name == key:
            return ratio
    return None


def aspect_label(key: str) -> str:
    for name, label, _ratio in ASPECTS:
        if name == key:
            return label
    return key


def estimated_fps(width: int, height: int, limit: int = DEFAULT_FPS) -> float:
    """その大きさを映したときに出せそうなフレームレートの目安。

    範囲は自由に選べるので、大きく選べば重い範囲も選べてしまう。選んでいる最中に
    「この大きさだと何fpsくらいか」が見えていれば、自分で加減できる。
    係数の出どころは GRAB_MS_PER_PIXEL のコメント(このPCでの実測2点)。"""
    frame_ms = (width * height) * GRAB_MS_PER_PIXEL + GRAB_MS_BASE + SCALE_MS
    if frame_ms <= 0:
        return float(limit)
    return min(float(limit), 1000.0 / frame_ms)


def scaling_mode(app_settings: dict) -> str:
    """設定の拡大方法。読めない値なら既定("auto")。"""
    value = str(mirror_config(app_settings).get("scaling") or DEFAULT_SCALING).lower()
    return value if value in SCALING_MODES else DEFAULT_SCALING


def use_smooth(scale: float, mode: str) -> bool:
    """その拡大率で滑らかな補間(SmoothTransformation)を使うか。

    "auto" は倍率が整数のときだけ Fast にする。整数倍なら元の1画素が正方形へそのまま
    分かれるので、中間色が生えない=輪郭が鈍らない。等倍(1.0)もここに含まれる
    (等倍ならどちらの方式でも結果は同じだが、無駄な補間を通さない)。
    半端な倍率で Fast にすると、行によって太さの違う文字になって読めたものではないので
    Smooth に落とす。縮小(1.0未満)も Smooth——最近傍の縮小は画素を間引くだけで、
    細い線が丸ごと消える。"""
    if mode == "smooth":
        return True
    if mode == "fast":
        return False
    if scale < 1.0:
        return True
    return abs(scale - round(scale)) > SCALE_INTEGER_TOLERANCE


def preset_entries(app_settings: dict) -> list:
    """設定に書かれたプリセットの定義(辞書の並び)。書かれていなければ既定。

    中身の妥当性はここでは見ない(解像度を引くのに画面が要るため)。実際の矩形にするのは
    preset_rects。"""
    entries = mirror_config(app_settings).get("presets")
    if not isinstance(entries, (list, tuple)) or not entries:
        return [dict(entry) for entry in DEFAULT_PRESETS]
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


# プリセットの x/y に書ける別名と、その画素数。数字の代わりに置ける。
# "titlebar" は、ウィンドウの枠を外して中身から映したいときのため。ブラウザや
# アプリのタイトルバーのぶんだけ下げる。実際の高さはテーマで変わるので、Windows の
# メトリクスから引く(取れなければ Windows 11 の既定値で妥協する)。
_TITLEBAR_FALLBACK = 32


def _titlebar_height() -> int:
    """タイトルバーの高さ(論理px)。取れなければ既定値。"""
    try:
        style = QApplication.style()
        if style is not None:
            value = style.pixelMetric(QStyle.PM_TitleBarHeight)
            if value and value > 0:
                return int(value)
    except Exception:
        pass
    return _TITLEBAR_FALLBACK


def _preset_offset(value) -> int:
    """プリセットの x/y を画素数にする。別名(titlebar)も受ける。"""
    if isinstance(value, str):
        key = value.strip().lower()
        if key == "titlebar":
            return _titlebar_height()
    return _as_int(value, 0)


def preset_rects(app_settings: dict, anchor_screen=None, target_screen=None) -> list:
    """プリセットを [(見出し, QRect(グローバル論理座標)), ...] にする。

    anchor_screen は x/y を測る基準のモニタ(既定では今カーソルのあるモニタ)、
    target_screen は "size": "target" が指すミラー先のモニタ。どちらも QScreen だが、
    検証のために「geometry() と name() を持つだけの偽物」を渡せるようにしてある
    (available_screens を差し替えるのと同じ流儀)。

    大きさは書かれたとおりに作る。基準のモニタに収まらなくても縮めない——等倍のために
    ミラー先と同じ大きさを指定しているのに、収まらないからと縮められては意味が無い。
    はみ出すときは始点だけモニタの中へ寄せる(掴めない位置に置かない)。"""
    rects = []
    screens = available_screens()
    for entry in preset_entries(app_settings):
        try:
            screen = anchor_screen
            wanted = str(entry.get("screen") or "")
            if wanted:
                for candidate in screens:
                    if candidate.name() == wanted:
                        screen = candidate
                        break
            if screen is None:
                continue
            area = screen.geometry()

            if str(entry.get("size") or "").lower() == "target":
                if target_screen is None:
                    continue
                size = target_screen.geometry()
                width, height = size.width(), size.height()
            else:
                width = _as_int(entry.get("width"), 0)
                height = _as_int(entry.get("height"), 0)
            if width <= 0 or height <= 0:
                continue

            if entry.get("x") is None or entry.get("y") is None:
                # 始点の指定が無いものは、そのモニタの中央に置く。
                left = area.x() + (area.width() - width) // 2
                top = area.y() + (area.height() - height) // 2
            else:
                left = area.x() + _preset_offset(entry.get("x"))
                top = area.y() + _preset_offset(entry.get("y"))

            # 始点だけモニタの中へ寄せる(大きさは変えない)。
            left = min(max(left, area.left()), max(area.right() - width + 1, area.left()))
            top = min(max(top, area.top()), max(area.bottom() - height + 1, area.top()))

            label = str(entry.get("label") or "") or f"{width}x{height}"
            rects.append((label, QRect(left, top, width, height)))
        except Exception:
            # 1つ書き損じただけで一覧ごと出ないのは困る。その行だけ落とす。
            _guard("プリセットの解釈", notify=False)
    return rects


def pick_target_screen(screens, wanted_name: str, source_rect: QRect = None):
    """ミラーを出すモニタを選ぶ。決められなければ None。

    screens を引数で受けるのは、モニタが3枚以上ある構成の挙動を確かめられるようにする
    ため(実機を繋がずに、名前と矩形だけを持つ偽物を並べて呼べる)。選び方そのものは
    QGuiApplication を見なくても決まるので、外から渡すほうが素直でもある。

    優先するのは設定で指定された名前。無い/見つからないときは「選択範囲が乗っていない
    モニタ」の先頭に落とす(自分を撮ると無限に入れ子になるので、範囲のあるモニタは
    候補から外す)。"""
    if not screens:
        return None

    if wanted_name:
        for screen in screens:
            if screen.name() == wanted_name:
                return screen

    if source_rect is None:
        return screens[0]
    for screen in screens:
        if not screen.geometry().intersects(source_rect):
            return screen
    return None


def screen_label(screen) -> str:
    """モニタ選択メニューに出す名前。QScreen.name() だけでは "\\\\.\\DISPLAY1" のような
    文字列で見分けが付かないので、解像度と主モニタかどうかを添える。"""
    geometry = screen.geometry()
    parts = [f"{screen.name()}", f"{geometry.width()}x{geometry.height()}"]
    if screen is QGuiApplication.primaryScreen():
        parts.append("主")
    return "  ".join(parts)


# ---------------------------------------------------------------
# 範囲選択(比率固定つき)
# ---------------------------------------------------------------
class AspectSelectionOverlay(SelectionOverlay):
    """比率を選べる範囲選択オーバーレイ。

    減光・選択枠・小さすぎる選択の扱いは capture_overlay.SelectionOverlay のままで、
    今回の差分は「比率固定」だけ(画面定規が情報表示だけを差し替えているのと同じ流儀)。

    固定のしかたは、ドラッグ中の終点 self._current を比率に合う位置へ寄せるだけにした。
    paintEvent も mouseReleaseEvent も基底が self._current から矩形を作るので、1箇所を
    直せば見えている枠と確定する矩形が必ず一致する(両方に同じ計算を書くと、片方だけ
    直したときにズレる)。"""

    # ウィンドウ単位の選択は使わない。あれはキャプチャのために「クリックした窓を丸ごと
    # 撮る」機能で、非ドラッグ中は対象の窓を黄色い枠で予告する。こちらが選ぶのは
    # 「画面のこの範囲」であって窓ではないうえ、比率を保った矩形を作りたいので、
    # 窓の形に引っぱられては困る(画面定規が同じ理由で切っている)。
    window_pick_enabled = False

    def __init__(
        self,
        aspect: str = DEFAULT_ASPECT,
        fps_limit: int = DEFAULT_FPS,
        presets=None,
        reselecting: bool = False,
    ):
        super().__init__()
        self.aspect = aspect if aspect_ratio(aspect) is not None or aspect == "free" else DEFAULT_ASPECT
        # 選んでいる大きさで出せそうなフレームレートを添えるために持つ(estimated_fps)。
        self.fps_limit = fps_limit
        # [(見出し, QRect(グローバル)), ...]。解決済みのものを受け取るだけにしてある
        # (どのモニタを基準にするか・ミラー先はどれか、を知っているのは呼ぶ側なので)。
        self.presets = list(presets or [])
        # ミラー中の「選び直し」で開いたか。Esc の意味が変わる(やめてもミラーは続く)ので、
        # 出す文言だけを変える。
        self.reselecting = bool(reselecting)
        # プリセット一覧の枠(ローカル座標)。中身が無ければ空のまま=描かない。
        self._preset_panel = QRect()
        self._preset_rows = []
        self._preset_hover = -1
        self._layout_presets()

    # ---------------------------------------------------------------
    # プリセット
    #
    # 呼び出し方は2つ。一覧をクリックするのと Ctrl+数字。数字だけにしなかったのは、
    # 1/2/3 が既に比率の切り替えで埋まっているため。一覧を画面に出しておくのは、
    # 覚えていなくても使えるようにするため(設定で増やせる以上、中身は人によって違う)。
    # ---------------------------------------------------------------
    def _layout_presets(self) -> None:
        """一覧を出す場所を決める。カーソルのあるモニタの左上へ寄せる。

        置き先を1度だけ決めて動かさないのは、ドラッグ中に一覧が付いてくると、選んでいる
        枠と重なって邪魔になるため。"""
        if not self.presets:
            return
        try:
            screen = QGuiApplication.screenAt(cursor_pos())
            area = screen.geometry() if screen is not None else QRect(self._origin, self.size())
            height = PRESET_PANEL_PADDING * 2 + PRESET_ROW_HEIGHT * (len(self.presets) + 1)
            panel = QRect(
                area.x() + PRESET_PANEL_MARGIN,
                area.y() + PRESET_PANEL_MARGIN,
                PRESET_PANEL_WIDTH,
                height,
            )
            self._preset_panel = panel.translated(-self._origin)
            self._preset_rows = [
                QRect(
                    self._preset_panel.x() + PRESET_PANEL_PADDING,
                    self._preset_panel.y() + PRESET_PANEL_PADDING + PRESET_ROW_HEIGHT * (index + 1),
                    self._preset_panel.width() - PRESET_PANEL_PADDING * 2,
                    PRESET_ROW_HEIGHT,
                )
                for index in range(len(self.presets))
            ]
        except Exception:
            self._preset_panel = QRect()
            self._preset_rows = []
            _guard("プリセット一覧の配置", notify=False)

    def _preset_index_at(self, point_local) -> int:
        if point_local is None:
            return -1
        for index, row in enumerate(self._preset_rows):
            if row.contains(point_local):
                return index
        return -1

    def _apply_preset(self, index: int) -> bool:
        """プリセットを選んで確定する。比率には合わせない——プリセットは大きさまで
        決めたものなので、比率で丸めたら指定した意味が無くなる。"""
        if not 0 <= index < len(self.presets):
            return False
        self.selection_made.emit(QRect(self.presets[index][1]))
        return True

    # ---------------------------------------------------------------
    # 比率
    # ---------------------------------------------------------------
    def _ratio(self):
        return aspect_ratio(self.aspect)

    def _cycle_aspect(self, step: int) -> None:
        names = [name for name, _label, _ratio in ASPECTS]
        index = names.index(self.aspect) if self.aspect in names else 0
        self.aspect = names[(index + step) % len(names)]
        self._reconstrain()

    def _reconstrain(self) -> None:
        """比率を変えたときに、いま引いている枠をその比率へ合わせ直す。
        ドラッグの途中で切り替えられるようにするための処理。"""
        if self._dragging and self._start is not None and self._current is not None:
            self._current = self._constrained_corner(self._start, self._current)
        self.update()

    def _constrained_corner(self, start: QPoint, current: QPoint) -> QPoint:
        """終点を比率に合う位置へ寄せる。自由なら素通し。

        幅・高さは QRect(start, current) の大きさ、つまり両端を含む(差 +1)。差のまま
        計算すると1pxぶんずれて、確定した矩形の比率が狙いから外れる。
        どちらの辺に合わせるかは「大きいほうへ合わせる」。引いた面積が減る向きに
        合わせると、対角へ引いているのに枠が縮んで操作感が悪い。"""
        ratio = self._ratio()
        if ratio is None:
            return current

        dx = current.x() - start.x()
        dy = current.y() - start.y()
        sign_x = 1 if dx >= 0 else -1
        sign_y = 1 if dy >= 0 else -1
        width = abs(dx) + 1
        height = abs(dy) + 1

        if width >= height * ratio:
            height = max(int(round(width / ratio)), 1)
        else:
            width = max(int(round(height * ratio)), 1)

        return QPoint(
            start.x() + sign_x * (width - 1),
            start.y() + sign_y * (height - 1),
        )

    def _window_rect_at(self, point_local):
        """クリックでウィンドウ全体を選んだときも比率へ合わせる。

        基底はウィンドウの矩形をそのまま返すが、比率を固定しているのにここだけ
        素通しでは、確定した瞬間に枠の形が変わることになる。ウィンドウの中央を保って
        内側へ収める(はみ出す向きに広げると画面外や隣の窓まで映してしまう)。"""
        rect_global = super()._window_rect_at(point_local)
        ratio = self._ratio()
        if rect_global is None or ratio is None:
            return rect_global

        width = rect_global.width()
        height = rect_global.height()
        if width > height * ratio:
            width = max(int(round(height * ratio)), 1)
        else:
            height = max(int(round(width / ratio)), 1)
        fitted = QRect(0, 0, width, height)
        fitted.moveCenter(rect_global.center())
        return fitted

    # ---------------------------------------------------------------
    # 入力
    # ---------------------------------------------------------------
    def mousePressEvent(self, event):
        # 一覧の上で押したときはドラッグを始めない(基底へ渡さない)。渡すと、離した
        # 位置でウィンドウ単位の選択が走って、押した項目と関係ない範囲が確定する。
        if event.button() == Qt.LeftButton:
            index = self._preset_index_at(event.position().toPoint())
            if index >= 0:
                try:
                    self._apply_preset(index)
                except Exception:
                    _guard("プリセットの選択", notify=False)
                return
        super().mousePressEvent(event)
        if self._dragging and self._start is not None:
            self._current = self._constrained_corner(self._start, self._current)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        # 基底が self._current にカーソル位置をそのまま入れたあとで寄せ直す。
        if self._dragging and self._start is not None and self._current is not None:
            self._current = self._constrained_corner(self._start, self._current)
            self.update()
        else:
            hover = self._preset_index_at(event.position().toPoint())
            if hover != self._preset_hover:
                self._preset_hover = hover
                self.update(self._preset_panel)

    def wheelEvent(self, event):
        """ホイールで比率を回す。ドラッグしながら左手を使わずに切り替えられる。"""
        try:
            self._cycle_aspect(1 if event.angleDelta().y() < 0 else -1)
        except Exception:
            _guard("比率の切り替え", notify=False)

    def keyPressEvent(self, event):
        # Ctrl+1〜9 でプリセット、1/2/3 で比率、Space/Tab で順送り。
        # Esc は基底(キャンセル)へ渡す。
        key = event.key()
        if Qt.Key_1 <= key <= Qt.Key_9:
            index = key - Qt.Key_1
            if event.modifiers() & Qt.ControlModifier:
                if index < PRESET_MAX_KEYS:
                    try:
                        self._apply_preset(index)
                    except Exception:
                        _guard("プリセットの選択", notify=False)
                return
            if key in (Qt.Key_1, Qt.Key_2, Qt.Key_3):
                self.aspect = ASPECTS[index][0]
                self._reconstrain()
                return
        if key in (Qt.Key_Space, Qt.Key_Tab):
            self._cycle_aspect(1)
            return
        super().keyPressEvent(event)

    # ---------------------------------------------------------------
    # 表示
    # ---------------------------------------------------------------
    def _draw_info(self, painter, rect_local):
        """基底の「幅x高さ (始点)」に、比率と負荷の目安を1行足す。

        目安を出すのは、大きさを自由に選べるから。大きく選ぶほど重くなるが、それは
        選び終えて映してみるまで分からない。選んでいる最中に数字が見えていれば、
        重いと思った時点で引き直せる。"""
        super()._draw_info(painter, rect_local)
        fps = estimated_fps(rect_local.width(), rect_local.height(), self.fps_limit)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 10))
        painter.drawText(
            QPoint(rect_local.x(), min(rect_local.bottom() + 18, self.height() - 4)),
            f"比率: {aspect_label(self.aspect)}　目安 {fps:.0f}fps",
        )

    def _draw_hover_info(self, painter):
        super()._draw_hover_info(painter)
        if self._hover is None:
            return
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 9))
        painter.drawText(
            QPoint(self._hover.x() + 14, self._hover.y() + 16),
            f"比率: {aspect_label(self.aspect)}  （1=自由 2=4:3 3=16:9 / ホイールで切替）",
        )

    def paintEvent(self, event):
        super().paintEvent(event)
        # 基底の QPainter は super() を抜けた時点で畳まれているので、ここで1本作り直す。
        # 一覧は最後に描く(選択枠の減光を後から重ねられて薄くならないように)。
        try:
            self._draw_presets()
        except Exception:
            _guard("プリセット一覧の描画", notify=False)

    def _draw_presets(self) -> None:
        if self._preset_panel.isEmpty() or not self._preset_rows:
            return
        painter = QPainter(self)
        painter.fillRect(self._preset_panel, QColor(16, 16, 16, 225))
        painter.setPen(QPen(QColor(0, 200, 255, 160), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self._preset_panel.adjusted(0, 0, -1, -1))

        header = self._preset_panel.adjusted(
            PRESET_PANEL_PADDING, PRESET_PANEL_PADDING, -PRESET_PANEL_PADDING, 0
        )
        header.setHeight(PRESET_ROW_HEIGHT)
        painter.setFont(QFont("Meiryo", 9, QFont.Bold))
        painter.setPen(QColor(0, 200, 255))
        painter.drawText(header, Qt.AlignVCenter | Qt.AlignLeft, "プリセット（クリック / Ctrl+数字）")
        painter.setPen(QColor(190, 190, 190))
        painter.drawText(
            header,
            Qt.AlignVCenter | Qt.AlignRight,
            "Esc: 選び直しをやめる" if self.reselecting else "Esc: やめる",
        )

        painter.setFont(QFont("Meiryo", 9))
        for index, row in enumerate(self._preset_rows):
            label, rect = self.presets[index]
            if index == self._preset_hover:
                painter.fillRect(row, QColor(0, 200, 255, 60))
            key = f"Ctrl+{index + 1}" if index < PRESET_MAX_KEYS else "　　　"
            painter.setPen(QColor(0, 200, 255))
            painter.drawText(row, Qt.AlignVCenter | Qt.AlignLeft, key)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(row.adjusted(58, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, label)
            painter.setPen(QColor(170, 170, 170))
            painter.drawText(
                row,
                Qt.AlignVCenter | Qt.AlignRight,
                f"{rect.width()}x{rect.height()} ({rect.x()},{rect.y()})",
            )


# ---------------------------------------------------------------
# 最前面に居続ける枠なし窓の共通部分
# ---------------------------------------------------------------
class _TopmostWindow(QWidget):
    """ミラー窓と手元の枠に共通の作り。枠なし・最前面・前面を奪わない。

    presenter_overlay._Overlay と作りは同じだが、あちらは「設定 target_screen で出す先を
    決める」ところまで持っている。こちらは出す先が呼び出し側の都合(ミラー先のモニタ /
    選択範囲のまわり)で決まるので、共通なのは最前面の維持だけ。それだけのために
    継承すると place_on_target を無効化して回ることになるので、ここに小さく持つ。"""

    closed = Signal()

    # マウスを透過するか。透過する側は自分ではマウスもキーも受け取れない。
    click_through = False

    def __init__(self, geometry: QRect):
        super().__init__()
        flags = (
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            # 発表中に前面を奪わない。操作しているのは手元のアプリで、そちらから
            # キーボードの相手を取り上げたら発表が止まる。
            | Qt.WindowDoesNotAcceptFocus
        )
        if self.click_through:
            # WA_TransparentForMouseEvents だけではQtが自分の中でイベントを配らなく
            # なるだけで、Windowsから見れば依然そこに窓がある(クリックは吸われる)。
            # 下のアプリまで通すには WS_EX_TRANSPARENT(＝WindowTransparentForInput)が要る。
            flags |= Qt.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if self.click_through:
            self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.setGeometry(geometry)

        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._on_topmost)

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self._topmost_timer.start(TOPMOST_INTERVAL_MS)

    def hideEvent(self, event):
        self._topmost_timer.stop()
        super().hideEvent(event)

    def _on_topmost(self):
        """最前面を保つ。ただし、実際に誰かに覆われているときだけ押し上げる。

        以前は周期のたびに無条件で raise_() していた。やめたのは、Zオーダーを触る操作が
        毎回カーソルの当たり判定と形状の問い合わせを引き起こすため(_overlapped_from_above
        のコメント参照)。覆われていなければ並びは既に正しいので、触る理由が無い。"""
        try:
            if not self.isVisible():
                return
            if _overlapped_from_above(int(self.winId())):
                self.raise_()
        except Exception:
            _guard("最前面の維持", notify=False)


# ---------------------------------------------------------------
# 手元に出す「いまミラーしている範囲」の枠
# ---------------------------------------------------------------
class SourceFrameWindow(_TopmostWindow):
    """選択範囲のまわりに薄い枠を出しっぱなしにする窓。手元のどこを映しているかを
    見失わないためのもの。

    枠がミラーに映り込んではいけない(自分を撮ると入れ子になる)。方法は3つ考えられる。
      1. 範囲の外側に描く
      2. キャプチャの直前に隠して、撮り終えたら出す
      3. 透明(アルファ0)で範囲を覆い、そこは何も足さないことに賭ける
    採ったのは 1。2 は毎フレーム show/hide することになり、30fpsでちらつくうえ
    キャプチャ1枚あたりの時間に窓の表示切替が乗る(実測17msの予算に対して割に合わない)。
    3 は合成結果が元のピクセルと寸分違わない前提に乗ることになり、確かめようが無い。
    1 なら「撮る範囲には最初から居ない」ことが形で保証できる。

    そのうえで setMask で範囲のぶんを窓から切り抜いている。枠の内側を透明に塗るのでは
    なく、窓そのものに穴を空ける。こうすると撮る範囲にこの窓のピクセルが1枚も無いことが
    Windows から見ても確かで、ついでに穴の部分のクリックは何もしなくても下へ抜ける。

    枠の外側に細い帯を1本足し、そこへ実測フレームレートを出す(status を渡したとき)。
    範囲は自由に選べるので大きく選べば重くなるが、映している最中にそれが分かる場所が
    どこにも無かった。帯も範囲の外なので、これもミラーには映り込まない。"""

    click_through = True

    def __init__(self, source_rect: QRect, app_settings: dict, status=None):
        cfg = mirror_config(app_settings)
        self._width = max(_as_int(cfg.get("source_frame_width"), DEFAULT_SOURCE_FRAME_WIDTH), 1)
        self._color = _as_color(cfg.get("source_frame_color"), DEFAULT_SOURCE_FRAME_COLOR)
        opacity = _as_float(
            cfg.get("source_frame_opacity"), DEFAULT_SOURCE_FRAME_OPACITY, 0.0, 1.0
        )
        self._color.setAlphaF(opacity)
        # 静止中の色。枠ごと色を変えるのは、帯の文字より先に目に入るのが枠だから。
        self._normal_color = QColor(self._color)
        self._frozen_color = _as_color(
            cfg.get("freeze_frame_color"), DEFAULT_FREEZE_FRAME_COLOR
        )
        self._frozen_color.setAlphaF(
            _as_float(
                cfg.get("freeze_frame_opacity"), DEFAULT_FREEZE_FRAME_OPACITY, 0.0, 1.0
            )
        )
        # ミラー先だけを黒画面/白画面にしている間の色。手元は普通に見えているので、
        # 枠の色以外に「向こうが覆われている」と分かる手がかりが無い。
        self._blank_color = _as_color(cfg.get("blank_frame_color"), DEFAULT_BLANK_FRAME_COLOR)
        self._blank_color.setAlphaF(
            _as_float(cfg.get("blank_frame_opacity"), DEFAULT_BLANK_FRAME_OPACITY, 0.0, 1.0)
        )
        self.frozen = False
        self.blank_kind = None

        # 呼ぶと1行の文字列を返すもの(実測fps)。無ければ帯そのものを作らない。
        self._status = status
        self._status_text = ""
        band = SOURCE_FRAME_BAND_HEIGHT if status is not None else 0

        # 帯は範囲の上に出す。上に置けない(画面の端に寄せて選んだ)ときだけ下へ回す。
        # 上を既定にするのは、下端はタスクバーと重なりやすいため。
        above = band if band and self._fits_above(source_rect, band) else 0
        below = band - above
        # 帯を上下どちらに置いたかは、あとで大きさを変えるとき(resize_to)にも要る。
        # 作り直さずに形だけ合わせ直すので、置いた側を覚えておかないと再現できない。
        self._band_height = band
        self._above = above
        self._below = below
        # outer_rect と同じ式。ここで呼ばないのは、まだ QWidget.__init__ を通していない
        # 自分のメソッドを呼びに行かないため(PySide6 は C++ 側が未初期化の間に触ると
        # 何が起きるか保証が無い)。式を変えるときは両方。
        outer = source_rect.adjusted(
            -self._width, -self._width - above, self._width, self._width + below
        )
        super().__init__(outer)
        self.setAttribute(Qt.WA_TranslucentBackground)

        # 窓の中の、撮られる範囲にあたる矩形。ここを窓から切り抜く。
        self._hole = QRect(
            self._width, self._width + above, source_rect.width(), source_rect.height()
        )
        self.setMask(QRegion(self.rect()).subtracted(QRegion(self._hole)))

        self._band = QRect()
        if band:
            self._band = QRect(0, 0 if above else self.height() - band, self.width(), band)
            self._status_timer = QTimer(self)
            self._status_timer.timeout.connect(self._on_status)
            self._status_timer.start(SOURCE_FRAME_BAND_INTERVAL_MS)

    def outer_rect(self, source_rect: QRect) -> QRect:
        """その範囲を囲むときの窓の外形。枠の太さと帯のぶんだけ外へ広げたもの。

        ツールバーとカンペの置き場所はこの矩形を基準に決まる(toolbar_geometry /
        notes_geometry の anchor_rect)。大きさを変えている最中は窓を作り直さないので、
        「作ったらこうなる外形」を先に引けるよう関数にしてある。"""
        return source_rect.adjusted(
            -self._width,
            -self._width - self._above,
            self._width,
            self._width + self._below,
        )

    def resize_to(self, source_rect: QRect) -> None:
        """映す範囲の大きさが変わったので、窓の形だけ合わせ直す。

        作り直さないのは、掴んでいる間に窓を作り直すとドラッグが切れるため
        (MirrorController.end_move と同じ理由)。帯は作ったときと同じ側に置く——
        大きさを変えるときは左上を固定するので範囲の上端は動かず、「上に帯を置けるか」
        の答えも変わらない(_fits_above は上端と帯の高さだけで決まる)。

        setMask を張り直すのはここ。毎フレームの経路では触らない(張り直しはカーソルの
        ちらつきの元になるので、生成時と、人が掴んで動かしている間だけに留める)。
        穴を追わせないと、広げた範囲の中に枠の実体が入って自分が映り込む。"""
        try:
            self.setGeometry(self.outer_rect(source_rect))
            self._hole = QRect(
                self._width,
                self._width + self._above,
                source_rect.width(),
                source_rect.height(),
            )
            self.setMask(QRegion(self.rect()).subtracted(QRegion(self._hole)))
            if self._band_height:
                self._band = QRect(
                    0,
                    0 if self._above else self.height() - self._band_height,
                    self.width(),
                    self._band_height,
                )
            # 帯は1秒ごとにしか引き直さない。掴んでいる間ずっと古い大きさとfpsが出て
            # いては何を選んでいるか分からないので、ここでは待たずに引き直す。
            self._status_text = self._status() if self._status is not None else ""
            self.update()
        except Exception:
            _guard("枠の大きさの変更", notify=False)

    @staticmethod
    def _fits_above(source_rect: QRect, band: int) -> bool:
        """範囲の上に帯を置けるか。置き先の画面の上端よりはみ出すなら置けない。"""
        screen = QGuiApplication.screenAt(source_rect.topLeft())
        top = screen.geometry().top() if screen is not None else 0
        return source_rect.top() - band >= top

    def set_frozen(self, frozen: bool) -> None:
        """静止中かどうかを受け取り、枠の色と帯の文字を切り替える。"""
        try:
            frozen = bool(frozen)
            if frozen == self.frozen:
                return
            self.frozen = frozen
            self._apply_style()
        except Exception:
            _guard("枠の色の切り替え", notify=False)

    def set_blank(self, kind) -> None:
        """ミラー先が黒画面/白画面で覆われているかを受け取る("black"/"white"/None)。"""
        try:
            kind = kind if kind in ("black", "white") else None
            if kind == self.blank_kind:
                return
            self.blank_kind = kind
            self._apply_style()
        except Exception:
            _guard("枠の色の切り替え", notify=False)

    def _apply_style(self) -> None:
        """いまの状態(黒/白画面・静止)に合わせて枠の色と帯の文字を引き直す。

        帯は1秒ごとにしか更新しないので、ここでは待たずに引き直す。状態を切り替えた
        瞬間に手元の見た目が変わらないと、切り替わったのかどうかが分からない。

        黒/白画面を静止より優先するのは、見ている側に届いている絵がそれだから。
        静止していようがいまいが、覆っている間は覆った色しか向こうには出ていない。"""
        if self.blank_kind is not None:
            self._color = self._blank_color
        elif self.frozen:
            self._color = self._frozen_color
        else:
            self._color = self._normal_color
        self._status_text = self._status() if self._status is not None else ""
        self.update()

    def _on_status(self):
        try:
            text = self._status() if self._status is not None else ""
            if text != self._status_text:
                self._status_text = text
                self.update(self._band)
        except Exception:
            _guard("枠の表示の更新", notify=False)

    def paintEvent(self, event):
        # マスクで穴が空いているので、全面を塗っても枠と帯の部分にしか色は乗らない。
        painter = QPainter(self)
        painter.fillRect(event.rect(), self._color)
        if self._band.isEmpty() or not self._status_text:
            return
        # 帯は枠と同じ色のままだと文字が読めない。下地を敷いてから白で書く。
        painter.fillRect(self._band, QColor(20, 20, 20, 200))
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 9))
        painter.drawText(
            self._band.adjusted(6, 0, -6, 0),
            Qt.AlignVCenter | Qt.AlignLeft,
            self._status_text,
        )


# ---------------------------------------------------------------
# 手元のツールバー
#
# アイコンは taskbar_launcher と同じ流儀で Pillow に描かせる(色の付いた丸に白い図形)。
# あちらから import せず持ち直しているのは、静止・選び直し・終了の3つがここにしか無く、
# 向こうへ足すと「ランチャに並べられる項目」だと誤解されるため(あの一覧は settings.json
# の launcher_items に書ける名前そのもの)。丸と白図形という作りだけを揃えてある。
# ---------------------------------------------------------------
def _icon_laser(draw: ImageDraw.ImageDraw, color: str) -> None:
    """レーザーポインタ。光点と四方へ伸びる光(taskbar_launcher._draw_laser と同じ絵)。"""
    for x0, y0, x1, y1 in ((32, 8, 32, 20), (32, 44, 32, 56), (8, 32, 20, 32), (44, 32, 56, 32)):
        draw.line((x0, y0, x1, y1), fill="white", width=5)
    draw.ellipse((23, 23, 41, 41), fill="white")


def _icon_spotlight(draw: ImageDraw.ImageDraw, color: str) -> None:
    """スポットライト。暗い面に丸く明るい部分を1つ。"""
    draw.rounded_rectangle((8, 12, 56, 52), radius=5, fill=color, outline="white", width=3)
    draw.ellipse((22, 20, 46, 44), fill="white")


def _icon_black(draw: ImageDraw.ImageDraw, color: str) -> None:
    """黒画面。中身を黒く塗った画面。白画面と並ぶので、中の色で描き分ける
    (丸の色だけで分けると、小さくしたときに見分けが付かない)。"""
    draw.rounded_rectangle((10, 14, 54, 46), radius=4, fill="#101010", outline="white", width=3)
    draw.rectangle((30, 46, 34, 52), fill="white")


def _icon_white(draw: ImageDraw.ImageDraw, color: str) -> None:
    """白画面。中身を白く塗った画面。"""
    draw.rounded_rectangle((10, 14, 54, 46), radius=4, fill="white")
    draw.rectangle((30, 46, 34, 52), fill="white")


def _icon_notes(draw: ImageDraw.ImageDraw, color: str) -> None:
    """カンペ。紙に横線を数本。黒画面/白画面と同じ「四角い面」だが、あちらは画面の形
    (横長・脚つき)、こちらは紙の形(縦長)にして小さくしても見分けが付くようにする。"""
    draw.rounded_rectangle((16, 8, 48, 56), radius=4, fill="white")
    for y in (18, 27, 36, 45):
        draw.line((22, y, 42 if y != 45 else 34, y), fill=color, width=4)


def _icon_freeze(draw: ImageDraw.ImageDraw, color: str) -> None:
    """静止。一時停止の2本線。動画の停止と同じ記号にする(説明が要らない)。"""
    draw.rectangle((20, 14, 29, 50), fill="white")
    draw.rectangle((35, 14, 44, 50), fill="white")


def _icon_reselect(draw: ImageDraw.ImageDraw, color: str) -> None:
    """範囲を選び直す。選択範囲の四隅の鉤。"""
    for x, y, dx, dy in ((12, 12, 1, 1), (52, 12, -1, 1), (12, 52, 1, -1), (52, 52, -1, -1)):
        draw.line((x, y, x + 16 * dx, y), fill="white", width=5)
        draw.line((x, y, x, y + 16 * dy), fill="white", width=5)


def _icon_stop(draw: ImageDraw.ImageDraw, color: str) -> None:
    """ミラーを終了。×印。"""
    draw.line((18, 18, 46, 46), fill="white", width=7)
    draw.line((46, 18, 18, 46), fill="white", width=7)


# 並ぶ順に (キー, 説明, 丸の色, 絵)。キーは押されたときに呼ぶ処理の名前でもある。
_TOOLBAR_ITEMS = (
    ("laser", "レーザーポインタ", "#dc2626", _icon_laser),
    ("spotlight", "スポットライト", "#ca8a04", _icon_spotlight),
    ("black", "黒画面", "#334155", _icon_black),
    ("white", "白画面", "#94a3b8", _icon_white),
    # カンペは「向こうに何が出ているか」を切り替えるものではないので、黒画面/白画面と
    # 静止の間に置く。押しても共有側は何も変わらない、手元だけの項目。
    ("notes", "カンペ（手元だけに出る）", "#0d9488", _icon_notes),
    ("freeze", "静止（もう一度で解除）", "#ff8c00", _icon_freeze),
    ("reselect", "範囲を選び直す", "#2563eb", _icon_reselect),
    ("stop", "ミラーを終了", "#b91c1c", _icon_stop),
)

# 描いた絵はプロセスに1組あれば足りる(taskbar_launcher._pixmap_cache と同じ理由)。
_toolbar_pixmaps = {}


def _toolbar_pixmap(key: str) -> QPixmap:
    """ツールバーのアイコン。作れなければ空の QPixmap(描く側で捨てる)。"""
    cached = _toolbar_pixmaps.get(key)
    if cached is not None:
        return cached
    pixmap = QPixmap()
    try:
        for name, _label, color, glyph in _TOOLBAR_ITEMS:
            if name != key:
                continue
            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse((2, 2, 62, 62), fill=color)
            glyph(draw, color)
            pixmap = pil_to_qpixmap(image)
            break
    except Exception:
        # 絵が1つ出ないだけの話。ツールバーごと出ないほうが困るので握りつぶす。
        _guard("ツールバーのアイコンの生成", notify=False)
        pixmap = QPixmap()
    _toolbar_pixmaps[key] = pixmap
    return pixmap


def toolbar_size() -> tuple:
    """ツールバーの (幅, 高さ)。論理px。"""
    count = len(_TOOLBAR_ITEMS)
    width = (
        TOOLBAR_PADDING * 2
        + TOOLBAR_BUTTON_SIZE * count
        + TOOLBAR_GAP * max(count - 1, 0)
        + TOOLBAR_LABEL_WIDTH
    )
    return width, TOOLBAR_PADDING * 2 + TOOLBAR_BUTTON_SIZE


def toolbar_geometry(anchor_rect: QRect, source_rect: QRect):
    """ツールバーを置く矩形。置き場所が無ければ None。

    anchor_rect は手元の枠(枠が無ければ選択範囲そのもの)。その真下に、収まらなければ
    真上に置く。左端は枠に揃える。

    撮影範囲(source_rect)に少しでも掛かるなら None を返す。掛かったままでは自分が
    ミラーに映り込み、映った自分をまた撮る入れ子になる。ツールバーが出ないより、
    映り込むほうが害が大きい(枠と同じ判断)。"""
    width, height = toolbar_size()
    screen = QGuiApplication.screenAt(anchor_rect.center())
    area = screen.geometry() if screen is not None else QRect(anchor_rect)

    left = min(max(anchor_rect.left(), area.left()), max(area.right() - width + 1, area.left()))
    top = anchor_rect.bottom() + 1
    if top + height - 1 > area.bottom():
        top = anchor_rect.top() - height
    rect = QRect(left, top, width, height)
    if rect.intersects(source_rect):
        return None
    return rect


class MirrorToolbarWindow(_TopmostWindow):
    """手元の枠の下に出す、プレゼン支援の操作パネル。

    枠(SourceFrameWindow)へ生やさずに別の窓にしてあるのは、あちらがマウスを透過する
    (WindowTransparentForInput)ため。透過した窓は自分ではクリックを受け取れないので、
    同じ窓に押せるものを置くことはできない。透過をやめると、今度は穴の部分(＝撮影範囲)の
    クリックまで枠が吸ってしまい、映しながら操作するというこの機能の前提が壊れる。

    置き場所は撮影範囲の外。枠と同じ理由で、ミラーに映り込んではいけない
    (映り込むなら出さない。toolbar_geometry が None を返す)。

    押されたときの処理は呼ぶ側から辞書で受け取る。中身が「終了」だとこの窓自身が
    片付けられるので、押した場所から直接は呼ばず QTimer で次の回へ回す
    (自分のイベントハンドラの中で自分を delete すると落ちる)。

    右側の説明欄はタイトルバーも兼ねていて、そこをドラッグするとミラー範囲ごと動く
    (TOOLBAR_GRIP_WIDTH のコメント参照)。さらにその右端 TOOLBAR_RESIZE_WIDTH ぶんが
    大きさを変える掴みしろ(◢)で、ドラッグでもホイールでも変えられる
    (SIZE_MIN_WIDTH 前後のコメント参照)。ホイールはスポットライトの調整にも使う。"""

    click_through = False

    def __init__(self, geometry: QRect, actions: dict, state=None,
                 adjust=None, move=None, move_end=None,
                 resize_begin=None, resize_to=None, resize_end=None, resize_steps=None):
        super().__init__(geometry)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._actions = dict(actions or {})
        # 呼ぶと「そのキーが今点いているか」を返すもの。無ければ全部消灯で描く。
        self._state = state
        # スポットライトの調整。adjust(半径の増減, 暗さの増減) を呼ぶと、変えた後の値を
        # 1行の文字列で返してくる(説明欄に出す)。無ければホイールは何もしない。
        self._adjust = adjust
        # 範囲の移動。move(dx, dy) を動かしている間に呼び、離したら move_end()。
        self._move = move
        self._move_end = move_end
        # 範囲の拡縮。掴んだ時点で resize_begin() を呼び、動かしている間は
        # resize_to(掴んだ点からのdx, dy, 比率を捨てるか) を呼び、離したら resize_end()。
        # 移動(差分の積み上げ)と違って「掴んだ点からの絶対量」を渡すのは、反映を間引いて
        # いるため——飛ばした回のぶんは次の1回に含まれるので、取りこぼしが残らない。
        self._resize_begin = resize_begin
        self._resize_to = resize_to
        self._resize_end = resize_end
        # ホイールでの拡縮。resize_steps(目盛り数, 細かくするか) を呼ぶ。
        self._resize_steps = resize_steps
        self._on = {key: False for key, _label, _color, _glyph in _TOOLBAR_ITEMS}
        self._hover = -1
        # ドラッグ中に掴んでいる位置(グローバル)。掴んでいなければ None。
        self._drag_from = None
        # 大きさを変えるドラッグで掴んだ位置(グローバル)。掴んでいなければ None。
        self._size_from = None
        # 今カーソルの形を何にしてあるか。動かすたびに setCursor しないための控え。
        self._cursor_zone = ""
        # 調整した値などを一時的に説明欄へ出すための一言と、その期限(perf_counter)。
        self._hint = ""
        self._hint_until = 0.0

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh_state)

    # ---------------------------------------------------------------
    # 位置
    # ---------------------------------------------------------------
    def _button_rect(self, index: int) -> QRect:
        return QRect(
            TOOLBAR_PADDING + (TOOLBAR_BUTTON_SIZE + TOOLBAR_GAP) * index,
            TOOLBAR_PADDING,
            TOOLBAR_BUTTON_SIZE,
            TOOLBAR_BUTTON_SIZE,
        )

    def _index_at(self, point) -> int:
        for index in range(len(_TOOLBAR_ITEMS)):
            if self._button_rect(index).contains(point):
                return index
        return -1

    def _title_rect(self) -> QRect:
        """右側の説明欄。そのままミラー範囲を動かすタイトルバーでもある。"""
        return QRect(
            self.width() - TOOLBAR_LABEL_WIDTH - TOOLBAR_PADDING,
            TOOLBAR_PADDING,
            TOOLBAR_LABEL_WIDTH,
            TOOLBAR_BUTTON_SIZE,
        )

    def _resize_rect(self) -> QRect:
        """説明欄の右端。ここを掴むと範囲の大きさが変わる(◢)。"""
        area = self._title_rect()
        return QRect(
            area.right() - TOOLBAR_RESIZE_WIDTH + 1,
            area.y(),
            TOOLBAR_RESIZE_WIDTH,
            area.height(),
        )

    def _move_rect(self) -> QRect:
        """説明欄のうち、掴むと範囲が動くほう(◢ を除いた残り)。"""
        return self._title_rect().adjusted(0, 0, -TOOLBAR_RESIZE_WIDTH, 0)

    def _update_cursor(self, local) -> None:
        """カーソルの形を、その場所で何が起きるかに合わせる。

        掴みしろが見た目だけだと、点々や ◢ が飾りにしか見えない。形が変われば掴めると
        分かる。変わったときだけ setCursor するのは、動かすたびに呼ぶと形の問い合わせが
        毎回走るため(カーソルまわりは _on_topmost で一度痛い目を見ている)。"""
        if self._resize_steps is not None and self._resize_rect().contains(local):
            zone = "size"
        elif self._move is not None and self._move_rect().contains(local):
            zone = "move"
        else:
            zone = ""
        if zone == self._cursor_zone:
            return
        self._cursor_zone = zone
        if zone == "size":
            self.setCursor(Qt.SizeFDiagCursor)
        elif zone == "move":
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.unsetCursor()

    @staticmethod
    def _spotlight_index() -> int:
        for index, (key, _label, _color, _glyph) in enumerate(_TOOLBAR_ITEMS):
            if key == "spotlight":
                return index
        return -1

    # ---------------------------------------------------------------
    # 状態
    # ---------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_state()
        self._refresh_timer.start(TOOLBAR_REFRESH_INTERVAL_MS)

    def hideEvent(self, event):
        self._refresh_timer.stop()
        super().hideEvent(event)

    def refresh_state(self):
        """実物の状態へ合わせ直す。レーザー等はホットキーやトレイメニューからも
        切り替わるので、押されたときだけ描き直すと嘘の状態が残る。"""
        try:
            if self._state is None:
                return
            changed = False
            for key, _label, _color, _glyph in _TOOLBAR_ITEMS:
                value = bool(self._state(key))
                if value != self._on.get(key):
                    self._on[key] = value
                    changed = True
            if self._hint and time.perf_counter() >= self._hint_until:
                self._hint = ""
                changed = True
            if changed:
                self.update()
        except Exception:
            _guard("ツールバーの状態の更新", notify=False)

    def show_hint(self, text: str) -> None:
        """説明欄へ一言を出す。TOOLBAR_HINT_MS 後に refresh_state が消す。"""
        self._hint = str(text or "")
        self._hint_until = time.perf_counter() + TOOLBAR_HINT_MS / 1000.0
        self.update(self._title_rect())

    # ---------------------------------------------------------------
    # 入力
    # ---------------------------------------------------------------
    def mouseMoveEvent(self, event):
        try:
            if self._size_from is not None:
                # 大きさを変えている最中。渡すのは掴んだ点からの絶対量で、掴んだ側の
                # 窓(このツールバー)が動いてもずれない。Shift を押している間だけ比率を
                # 捨てる(既定は掴んだときの比率を保つ。MirrorController._resized_rect)。
                if self._resize_to is None:
                    return
                delta = event.globalPosition().toPoint() - self._size_from
                text = self._resize_to(
                    delta.x(), delta.y(), bool(event.modifiers() & Qt.ShiftModifier)
                )
                if text:
                    self.show_hint(text)
                return
            if self._drag_from is not None:
                position = event.globalPosition().toPoint()
                delta = position - self._drag_from
                if delta.isNull():
                    return
                # 掴んだ点は「動かせた分」だけ進める。歯止めに当たって動けなかった分を
                # ここで足してしまうと、戻すときにその分だけ空振りする。
                moved = self._move(delta.x(), delta.y()) if self._move is not None else (0, 0)
                self._drag_from = position - QPoint(delta.x() - moved[0], delta.y() - moved[1])
                if moved[0] or moved[1]:
                    self.move(self.x() + moved[0], self.y() + moved[1])
                return
            local = event.position().toPoint()
            self._update_cursor(local)
            index = self._index_at(local)
            if index != self._hover:
                self._hover = index
                self.update()
        except Exception:
            _guard("ツールバーの反応", notify=False)

    def leaveEvent(self, event):
        self._hover = -1
        if self._cursor_zone:
            self._cursor_zone = ""
            self.unsetCursor()
        self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        """スポットライトの大きさと暗さをその場で変える。

        素のホイールで半径、Shift(またはCtrl)を押しながらで暗さ。効くのはスポットライトの
        アイコンの上と、スポットライトが点いている間のツールバー全体
        (点けたあとは、どこで回しても効いたほうが手数が少ない)。

        ただし ◢ の上だけは範囲の大きさに割り当てる。先に見るのは、スポットライトが
        点いている間はツールバー全体が調整の場所になるため——◢ を後回しにすると、
        点けた瞬間に大きさが変えられなくなる。ドラッグは狙った大きさへ一息に持っていく
        道、ホイールは「あと少しだけ」を数字を見ながら詰める道で、用途が違う。"""
        try:
            local = event.position().toPoint()
            if self._resize_steps is not None and self._resize_rect().contains(local):
                steps = event.angleDelta().y() / 120.0
                if not steps:
                    event.ignore()
                    return
                text = self._resize_steps(
                    steps, bool(event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier))
                )
                if text:
                    self.show_hint(text)
                event.accept()
                return
            if self._adjust is None:
                event.ignore()
                return
            index = self._index_at(local)
            if index != self._spotlight_index() and not self._on.get("spotlight"):
                event.ignore()
                return
            steps = event.angleDelta().y() / 120.0
            if not steps:
                event.ignore()
                return
            fine = bool(event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier))
            if fine:
                text = self._adjust(0, SPOTLIGHT_DIM_STEP * steps)
            else:
                text = self._adjust(SPOTLIGHT_RADIUS_STEP * steps, 0.0)
            if text:
                self.show_hint(text)
            event.accept()
        except Exception:
            _guard("スポットライトの調整", notify=False)

    def mousePressEvent(self, event):
        try:
            if event.button() != Qt.LeftButton:
                return
            local = event.position().toPoint()
            if self._resize_to is not None and self._resize_rect().contains(local):
                # 右端の ◢ を掴んだ。大きさを変える側。begin が False を返したら
                # (ミラーが畳まれた直後など)何も始めない。
                if self._resize_begin is None or self._resize_begin():
                    self._size_from = event.globalPosition().toPoint()
                return
            if self._move is not None and self._move_rect().contains(local):
                # タイトルバーを掴んだ。アイコンの上では始めない(押すつもりで滑った
                # だけで範囲ごと動くのを防ぐ。TOOLBAR_GRIP_WIDTH のコメント参照)。
                self._drag_from = event.globalPosition().toPoint()
                return
            index = self._index_at(local)
            if index < 0:
                return
            key = _TOOLBAR_ITEMS[index][0]
            action = self._actions.get(key)
            if action is None:
                return
            # 押した場所から直接呼ばない。「終了」や「選び直し」はこの窓を片付けるので、
            # 自分のイベントハンドラの中で自分が消えることになる。
            QTimer.singleShot(0, action)
        except Exception:
            _guard("ツールバーの操作", notify=False)

    def mouseReleaseEvent(self, event):
        try:
            if event.button() != Qt.LeftButton:
                return
            if self._size_from is not None:
                self._size_from = None
                if self._resize_end is None:
                    return
                # 移動と同じ理由で次の回へ回す。end はこの窓を作り直す。
                QTimer.singleShot(0, self._resize_end)
                return
            if self._drag_from is None:
                return
            self._drag_from = None
            if self._move_end is None:
                return
            # 離したところで枠とツールバーを作り直す(帯を上下どちらに置くか、ツールバーが
            # 収まるかは範囲ごとに決まるため)。この窓自身が作り直されるので、押した場所
            # からは直接呼ばず次の回へ回す(mousePressEvent と同じ理由)。
            QTimer.singleShot(0, self._move_end)
        except Exception:
            _guard("範囲の移動の後始末", notify=False)

    # ---------------------------------------------------------------
    # 描画
    # ---------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setBrush(TOOLBAR_BACKGROUND)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 6, 6)

        for index, (key, _label, color, _glyph) in enumerate(_TOOLBAR_ITEMS):
            cell = self._button_rect(index)
            on = bool(self._on.get(key))
            if on:
                # 点いているものは下地を敷いて縁取る。消えているものは半分の濃さで描く。
                # 同じ絵の濃淡だけで分けると、離れた席から手元を見たときに分からない。
                accent = QColor(color)
                accent.setAlpha(150)
                painter.setBrush(accent)
                painter.setPen(QPen(QColor(255, 255, 255, 210), 2))
                painter.drawRoundedRect(cell.adjusted(1, 1, -1, -1), 5, 5)
            elif index == self._hover:
                painter.setBrush(QColor(255, 255, 255, 40))
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(cell.adjusted(1, 1, -1, -1), 5, 5)

            pixmap = _toolbar_pixmap(key)
            if pixmap.isNull():
                continue
            painter.setOpacity(1.0 if on else 0.62)
            painter.drawPixmap(cell.adjusted(3, 3, -3, -3), pixmap)
            painter.setOpacity(1.0)

        self._draw_label(painter)

    def _draw_label(self, painter: QPainter) -> None:
        """右側の説明。乗せているボタンの名前を出す場所で、何も乗せていないときは
        いま向こうに何が出ているか(黒/白画面・静止)を出す。気付かないまま話し続けるのが
        最悪なので、既定の表示をここに割り当てている。

        ここはタイトルバーでもあるので、左端に掴みしろの点々を描き、右端には大きさを
        変える ◢ を描く。文字が入るのはその間。"""
        area = self._title_rect()

        # 右端の ◢。窓の右下と同じ「斜めの線3本」にする。掴んでいる間は明るくして、
        # 今どちらを掴んでいるのかが分かるようにする(点々の濃さと同じ作法)。
        size_grip = self._resize_rect()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 200 if self._size_from is not None else 110), 2))
        for offset in (0, 5, 10):
            painter.drawLine(
                size_grip.right() - 2 - offset,
                size_grip.bottom() - 4,
                size_grip.right() - 2,
                size_grip.bottom() - 4 - offset,
            )

        # 掴みしろ。点を2列。ここを引いた残りが文字の場所になる。
        grip = QRect(area.x(), area.y(), TOOLBAR_GRIP_WIDTH, area.height())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 90 if self._drag_from is None else 190))
        for row in range(4):
            for column in range(2):
                painter.drawEllipse(
                    QRectF(
                        grip.x() + 2 + column * 5,
                        grip.center().y() - 7 + row * 5,
                        2.4,
                        2.4,
                    )
                )
        # 文字は点々と ◢ の間。◢ に文字が重なると、どちらも読めなくなる。
        area = area.adjusted(TOOLBAR_GRIP_WIDTH + 2, 0, -TOOLBAR_RESIZE_WIDTH, 0)

        blank = "black" if self._on.get("black") else ("white" if self._on.get("white") else None)
        bold = False
        if self._hint:
            text = self._hint
            painter.setPen(QColor(250, 220, 120))
        elif 0 <= self._hover < len(_TOOLBAR_ITEMS):
            text = _TOOLBAR_ITEMS[self._hover][1]
            painter.setPen(QColor(235, 235, 235))
        elif blank is not None:
            # 手元は普通に見えたままなので、ここに出さないと気付きようが無い。
            text = "⬛ 共有側は黒画面" if blank == "black" else "⬜ 共有側は白画面"
            painter.setPen(QColor(DEFAULT_BLANK_FRAME_COLOR))
            bold = True
        elif self._on.get("freeze"):
            text = "⏸ 静止中"
            painter.setPen(QColor(DEFAULT_FREEZE_FRAME_COLOR))
            bold = True
        else:
            text = "ドラッグで移動／◢で大きさ"
            painter.setPen(QColor(150, 150, 150))
        painter.setBrush(Qt.NoBrush)
        painter.setFont(QFont("Meiryo", 9, QFont.Bold if bold else QFont.Normal))
        metrics = QFontMetrics(painter.font())
        painter.drawText(
            area,
            Qt.AlignVCenter | Qt.AlignLeft,
            metrics.elidedText(text, Qt.ElideRight, area.width()),
        )


# ---------------------------------------------------------------
# 手元のカンペ(発表者だけが見るメモ)
# ---------------------------------------------------------------
def notes_geometry(anchor_rect: QRect, source_rect: QRect, width: int = DEFAULT_NOTES_WIDTH,
                   avoid=()):
    """カンペのパネルを置く矩形。置き場所が無ければ None。

    anchor_rect は手元の枠(枠が無ければ選択範囲そのもの)。その右隣に置く。右に入らない
    ときは左へ回し、それも無理なら None を返す。None のときは出さない——カンペは
    発表者だけが見るものなので、撮影範囲に1画素でも掛かった時点で手元のメモが共有側へ
    丸見えになる。出ないほうがはるかにましで、これはツールバー(映り込むと入れ子になる)
    より強い理由になる。

    高さは範囲の高さに合わせ、足りなければ NOTES_MIN_HEIGHT まで「上へ」伸ばす。下へ
    伸ばさないのは、範囲の真下がツールバーの場所だから(ボタンを覆ってしまう)。伸ばした
    結果ツールバーや撮影範囲に掛かる構成では、avoid に渡された矩形との交差で弾かれる。

    avoid には撮影範囲以外の「掛かってはいけない矩形」を入れる(ミラー先のモニタ全体と
    ツールバー)。ミラー先を入れるのは、そこへ置くと共有側に映るうえ、最前面の押し合いに
    なって共有側でチカチカするため。"""
    width = int(max(width, NOTES_MIN_WIDTH))
    screen = QGuiApplication.screenAt(anchor_rect.center())
    area = screen.geometry() if screen is not None else QRect(anchor_rect)

    height = min(max(anchor_rect.height(), NOTES_MIN_HEIGHT), area.height())
    top = anchor_rect.bottom() - height + 1
    top = min(max(top, area.top()), max(area.bottom() - height + 1, area.top()))

    blocked = [QRect(source_rect)] + [QRect(rect) for rect in avoid if rect is not None]

    def usable(rect: QRect):
        if rect.left() < area.left() or rect.right() > area.right():
            return None
        for other in blocked:
            if not other.isNull() and rect.intersects(other):
                return None
        return rect

    # 右が既定。左に回すのは右に入らないときだけ——資料(撮影範囲)を左、手元のメモを右に
    # 置くのが読む向きに合う。
    right_space = area.right() - (anchor_rect.right() + NOTES_GAP)
    left_space = (anchor_rect.left() - NOTES_GAP) - area.left()
    for left in (anchor_rect.right() + 1 + NOTES_GAP, anchor_rect.left() - NOTES_GAP - width):
        found = usable(QRect(left, top, width, height))
        if found is not None:
            return found

    # 既定の幅では入らなかった。広いほうの空きに合わせて詰めてみる。狭くても読めれば
    # 出したほうがよいが、NOTES_MIN_WIDTH を割るなら折り返しだらけで読めないので諦める。
    if right_space >= left_space:
        narrowed = min(width, right_space)
        left = anchor_rect.right() + 1 + NOTES_GAP
    else:
        narrowed = min(width, left_space)
        left = anchor_rect.left() - NOTES_GAP - narrowed
    if narrowed >= NOTES_MIN_WIDTH:
        found = usable(QRect(left, top, narrowed, height))
        if found is not None:
            return found
    return None


def _clamp_notes_font(size) -> int:
    """本文の文字の大きさを扱える範囲へ丸める。設定ファイルからも来るので必ず通す。"""
    return min(max(_as_int(size, DEFAULT_NOTES_FONT_SIZE), NOTES_FONT_MIN), NOTES_FONT_MAX)


# パネルの見た目。半透明にせず不透明で塗るのは、これが「読むための窓」だから——下の
# アプリが透けると本文が読みにくい(ツールバーはアイコンを並べるだけなので半透明でよい)。
# 角も丸めない。丸めるには WA_TranslucentBackground が要り、そのぶん合成が増える。
NOTES_STYLE = """
QWidget { background: transparent; color: #e8e8e8; }
QWidget#mirrorNotes { background: #141414; border: 1px solid #3f3f46; }
QPushButton {
    background: rgba(255, 255, 255, 26); border: none; border-radius: 4px; padding: 2px 4px;
}
QPushButton:hover { background: rgba(255, 255, 255, 64); }
QPushButton:checked { background: rgba(13, 148, 136, 200); }
QTextEdit, QListWidget {
    background: #0b0b0b; border: 1px solid #3f3f46; border-radius: 4px;
    selection-background-color: rgba(13, 148, 136, 160);
}
QListWidget::item { padding: 1px 2px; }
QListWidget::item:selected { background: rgba(13, 148, 136, 160); }
"""


class MirrorNotesWindow(_TopmostWindow):
    """撮影範囲の右隣に出す、発表者だけが見るカンペ(メモ)のパネル。

    中身は notes/ フォルダの Markdown(mirror_notes.py が読む)。ここは読むだけで、
    その場での編集はしない——発表中に文章を書き換えることはまず無いし、編集できる窓を
    最前面に置くと、話しながら誤って打ち込む事故のほうが起こりやすい。書き足すのは
    「編集」ボタンから外部エディタで、直したら「再読込」で読み直す。

    見出しの拾い方は、生のテキストを正規表現で舐めるのではなく、setMarkdown した後の
    QTextDocument から blockFormat().headingLevel() で拾う。こうすると、表示されている
    ものと目次が必ず一致する——コードブロック(```)の中の # を見出しと数えてしまう、
    といった食い違いが原理的に起きない。ジャンプ先の文書内位置もそのまま取れる。

    ## を目次に入れるかは迷うところだが、入れることにした(字下げして並べる)。カンペの
    中身は「# 章」ではなく「## 話すこと」の側に書かれるので、## を落とすと目次から
    行きたい場所へ辿り着けない。ただし ◀ ▶ の前後ボタンが辿るのは # だけにしてある
    ——発表中にワンクリックで動かしたいのは章単位で、小見出しまで刻むと押す回数が
    増えて目的の場所を通り過ぎる。「2/3」の表示も # の数で数える。

    ツールチップは付けない。ツールチップは別の窓として出るので、パネルの左端で出ると
    撮影範囲に掛かりうる(それが共有側に映る)。ボタンは日本語の短い語にして、見れば
    分かる状態にしてある(ツールバーが説明欄を窓の中に持っているのと同じ判断)。

    フォーカスは奪わない。窓自体が WindowDoesNotAcceptFocus で、中の部品もすべて
    Qt.NoFocus にしてある。発表中に操作しているのは手元のアプリで、そこからキーボードの
    相手を取り上げたら発表が止まる。"""

    click_through = False

    def __init__(self, geometry: QRect, note_name: str = "",
                 font_size: int = DEFAULT_NOTES_FONT_SIZE, on_state=None):
        super().__init__(geometry)
        self.setObjectName("mirrorNotes")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(NOTES_STYLE)

        # 状態が変わったことを知らせる先。on_state(カンペ名, 文字の大きさ) を呼ぶ。
        # 設定への書き戻しは呼び元(MirrorController)がまとめて遅延させる。
        self._on_state = on_state
        self._name = ""
        self._font_size = _clamp_notes_font(font_size)
        # (見出しの深さ, 文字, 文書内の位置) を上から順に。目次も前後ボタンもこれを見る。
        self._headings = []
        # そのうち深さ1(＝「#」)のものの添字。前後ボタンと「2/3」はこれだけを辿る。
        self._tops = []
        # いま見ている見出し(_headings の添字)。-1 は「最初の見出しより上」。
        self._current = -1
        # 見出しの画面上の高さ。毎回 blockBoundingRect を引くとスクロールのたびに
        # 見出しの数だけ問い合わせることになるので、レイアウトが変わるまで使い回す。
        self._top_cache = None
        self._side_mode = ""
        self._position_text = ""

        self._build_ui()
        self._apply_font()
        self.load(note_name)

    # ---------------------------------------------------------------
    # 組み立て
    # ---------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        chrome = QFont(NOTES_FONT_FAMILY, 9)

        # 1行目: カンペの名前と、ファイルを触るボタン。
        head = QHBoxLayout()
        head.setSpacing(3)
        self.title = QLabel("")
        self.title.setFont(QFont(NOTES_FONT_FAMILY, 9, QFont.Bold))
        head.addWidget(self.title, 1)
        self.list_button = self._button("一覧", chrome, self._toggle_files, checkable=True)
        self.edit_button = self._button("編集", chrome, self._on_edit)
        self.folder_button = self._button("フォルダ", chrome, self._on_folder, width=56)
        for button in (self.list_button, self.edit_button, self.folder_button):
            head.addWidget(button)
        layout.addLayout(head)

        # 2行目: 見出しの移動と、読むための道具。
        bar = QHBoxLayout()
        bar.setSpacing(3)
        self.prev_button = self._button("◀", chrome, lambda: self.step_section(-1), width=26)
        self.next_button = self._button("▶", chrome, lambda: self.step_section(1), width=26)
        bar.addWidget(self.prev_button)
        bar.addWidget(self.next_button)
        self.position = QLabel("")
        self.position.setFont(chrome)
        self.position.setMinimumWidth(1)
        bar.addWidget(self.position, 1)
        self.toc_button = self._button("目次", chrome, self._toggle_toc, checkable=True)
        self.smaller_button = self._button("A-", chrome, lambda: self.zoom(-1), width=26)
        self.bigger_button = self._button("A+", chrome, lambda: self.zoom(1), width=26)
        self.reload_button = self._button("再読込", chrome, self.reload, width=48)
        for button in (self.toc_button, self.smaller_button, self.bigger_button,
                       self.reload_button):
            bar.addWidget(button)
        layout.addLayout(bar)

        # 目次とカンペ一覧は同じ場所に出す(窓の中で切り替える)。別窓にすると、撮影範囲に
        # 掛からない置き場所をもう1つ探すことになる。
        self.side = QListWidget()
        self.side.setFont(chrome)
        self.side.setFocusPolicy(Qt.NoFocus)
        self.side.setUniformItemSizes(True)
        self.side.setMaximumHeight(NOTES_SIDE_HEIGHT)
        self.side.itemClicked.connect(self._on_side_clicked)
        self.side.hide()
        layout.addWidget(self.side)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setFocusPolicy(Qt.NoFocus)
        self.view.setLineWrapMode(QTextEdit.WidgetWidth)
        # Ctrl+ホイールで文字の大きさを変える。QTextEdit が先にホイールを食べるので、
        # ビューポートに入れたフィルタで横取りする(picker の QLineEdit と同じ手)。
        self.view.viewport().installEventFilter(self)
        self.view.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        layout.addWidget(self.view, 1)

        # レイアウトが変われば見出しの高さも変わる。文字の大きさを変えたときも、
        # 窓を作った直後(まだ組版されていない)にも来る。
        self.view.document().documentLayout().documentSizeChanged.connect(
            self._invalidate_tops
        )

        self.status = QLabel("")
        self.status.setFont(chrome)
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)

    def _button(self, text: str, font: QFont, slot, width: int = 34,
                checkable: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setFont(font)
        button.setFocusPolicy(Qt.NoFocus)
        button.setFixedSize(width, 22)
        button.setCheckable(checkable)
        # 押した処理の中で例外を投げ切るとプロセスごと終わる。各スロットが自分で try して
        # いるが、ここでも一枚受けておく(ラムダの中で落ちる余地を残さない)。
        button.clicked.connect(lambda _checked=False, f=slot: self._run(f))
        return button

    @staticmethod
    def _run(func) -> None:
        try:
            func()
        except Exception:
            _guard("カンペの操作", notify=False)

    # ---------------------------------------------------------------
    # 読み込み
    # ---------------------------------------------------------------
    def note_name(self) -> str:
        return self._name

    def load(self, name: str = "") -> None:
        """カンペを1つ読んで表示する。名前が無効なら実在するものへ寄せる。"""
        try:
            resolved = mirror_notes.resolve_name(name or "")
            if not resolved:
                self._name = ""
                self.title.setText("カンペ（notes フォルダ）")
                self._render(mirror_notes.EMPTY_GUIDE)
                self._set_status("")
                return
            text = mirror_notes.read_note(resolved)
            self._name = resolved
            self.title.setText(resolved)
            if text is None:
                # 読めない理由(文字コード違い・消された)は手元にだけ出す。共有側には
                # 何も出ないので、ここに出さないと気付きようが無い。
                self._render("")
                self._set_status("このファイルを読めません（UTF-8 で保存してください）")
                return
            self._render(text)
            self._set_status("")
        except Exception:
            _guard("カンペの読み込み", notify=False)

    def reload(self) -> None:
        """外部エディタで直した内容を読み直す。見ていた章はできるだけ保つ。"""
        try:
            order = self._current_top_order()
            self.load(self._name)
            if 0 <= order < len(self._tops):
                self.jump_to_heading(self._tops[order])
        except Exception:
            _guard("カンペの再読み込み", notify=False)

    def _render(self, text: str) -> None:
        """本文を描き直し、見出しと目次を作り直す。

        .txt でも setMarkdown を通す。カンペの見出しは # で書く約束なので、拡張子で
        描き方を変えると「.txt にしたらジャンプできない」という分かりにくい差になる。"""
        self.view.setMarkdown(text or "")
        # 見出しを拾い直してからスクロールを戻す。逆にすると、位置を戻したことで走る
        # _on_scrolled が「新しい文書」を「古い見出しの位置」で見ることになる。
        self._collect_headings()
        self._invalidate_tops()
        self._current = -1
        self.view.verticalScrollBar().setValue(0)
        self._rebuild_side()
        self._update_position()

    def _collect_headings(self) -> None:
        """組版済みの文書から見出しを拾う。見た目と目次を必ず一致させるため、生の
        テキストではなく QTextDocument の blockFormat().headingLevel() を見る。"""
        self._headings = []
        self._tops = []
        document = self.view.document()
        block = document.begin()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            if level in (1, 2):
                text = block.text().strip()
                if text:
                    if level == 1:
                        self._tops.append(len(self._headings))
                    self._headings.append((level, text, block.position()))
            block = block.next()

    # ---------------------------------------------------------------
    # 目次 / カンペ一覧(同じ場所に切り替えて出す)
    # ---------------------------------------------------------------
    def _rebuild_side(self) -> None:
        try:
            self.side.clear()
            if self._side_mode == "toc":
                for index, (level, text, _position) in enumerate(self._headings):
                    # ## は字下げして並べる。見出しの深さは目次の並びでしか分からない。
                    item = QListWidgetItem(("　" if level == 2 else "") + text)
                    item.setData(Qt.UserRole, index)
                    self.side.addItem(item)
            elif self._side_mode == "files":
                for name in mirror_notes.list_notes():
                    item = QListWidgetItem(name)
                    item.setData(Qt.UserRole, name)
                    self.side.addItem(item)
                    if name == self._name:
                        self.side.setCurrentItem(item)
            self.side.setVisible(bool(self._side_mode) and self.side.count() > 0)
            if self._side_mode and self.side.count() == 0:
                self._set_status(
                    "見出し（#）がありません" if self._side_mode == "toc"
                    else "notes フォルダにカンペがありません"
                )
        except Exception:
            _guard("カンペの目次の更新", notify=False)

    def _set_side_mode(self, mode: str) -> None:
        self._side_mode = "" if self._side_mode == mode else mode
        self.toc_button.setChecked(self._side_mode == "toc")
        self.list_button.setChecked(self._side_mode == "files")
        self._rebuild_side()

    def _toggle_toc(self) -> None:
        self._set_side_mode("toc")

    def _toggle_files(self) -> None:
        self._set_side_mode("files")

    def _on_side_clicked(self, item) -> None:
        try:
            if item is None:
                return
            data = item.data(Qt.UserRole)
            if self._side_mode == "toc":
                self.jump_to_heading(int(data))
                return
            if self._side_mode == "files" and isinstance(data, str):
                self.load(data)
                # 選んだら一覧は畳む。読むために開いたパネルなので、本文の場所を
                # 一覧に占領されたままにしない。
                self._side_mode = ""
                self.list_button.setChecked(False)
                self.toc_button.setChecked(False)
                self._rebuild_side()
                self._notify_state()
        except Exception:
            _guard("カンペの選択", notify=False)

    # ---------------------------------------------------------------
    # 見出しの移動
    # ---------------------------------------------------------------
    def _invalidate_tops(self, *args) -> None:
        self._top_cache = None

    def _heading_tops(self) -> list:
        """見出しの画面上の高さ(文書座標)。レイアウトが変わるまで使い回す。"""
        if self._top_cache is None:
            document = self.view.document()
            layout = document.documentLayout()
            cache = []
            for _level, _text, position in self._headings:
                block = document.findBlock(position)
                cache.append(
                    float(layout.blockBoundingRect(block).top()) if block.isValid() else 0.0
                )
            self._top_cache = cache
        return self._top_cache

    def jump_to_heading(self, index: int) -> None:
        """見出しを本文の一番上に持ってくる。"""
        try:
            if not (0 <= index < len(self._headings)):
                return
            position = self._headings[index][2]
            cursor = QTextCursor(self.view.document())
            cursor.setPosition(position)
            # 先にカーソルを置く(ensureCursorVisible が走る)。その後で改めてスクロール
            # 位置を決めるので、見出しは必ず一番上に来る。
            self.view.setTextCursor(cursor)
            tops = self._heading_tops()
            if index < len(tops):
                self.view.verticalScrollBar().setValue(int(round(tops[index])))
            self._current = index
            self._update_position()
        except Exception:
            _guard("カンペのジャンプ", notify=False)

    def section_order(self) -> int:
        """いま見ている「#」の章の並び順(0起点)。最初の章より上なら -1。

        範囲を動かすとこのパネルは作り直される(置ける場所が位置ごとに変わるため)。
        作り直したあとに読んでいた場所へ戻すために、呼び元がこれを控えておく。"""
        return self._current_top_order()

    def restore_section(self, order: int) -> None:
        """section_order() で控えた章へ戻す。

        黙って先頭へ戻ると、範囲をちょっと動かしただけでどこを話していたか見失う。"""
        try:
            if 0 <= order < len(self._tops):
                self.jump_to_heading(self._tops[order])
        except Exception:
            _guard("カンペの位置の復元", notify=False)

    def _current_top_order(self) -> int:
        """いま見ている場所が「#」の何番目の章か(0起点)。最初の章より上なら -1。"""
        order = -1
        for position, index in enumerate(self._tops):
            if index <= self._current:
                order = position
            else:
                break
        return order

    def step_section(self, delta: int) -> None:
        """前後の「#」へ移る。端では止まる(丸めて先頭へ戻ったりはしない——発表中に
        意図せず最初へ飛ぶと、どこを話していたか見失う)。"""
        try:
            if not self._tops:
                return
            order = min(max(self._current_top_order() + delta, 0), len(self._tops) - 1)
            self.jump_to_heading(self._tops[order])
        except Exception:
            _guard("カンペの章の移動", notify=False)

    def _on_scrolled(self, value) -> None:
        """手でスクロールされたときも「いま何番目か」を合わせる。"""
        try:
            tops = self._heading_tops()
            current = -1
            for index, top in enumerate(tops):
                # 1画素の丸めで1つ手前に居ることになるのを防ぐ余裕。
                if top <= value + 2:
                    current = index
                else:
                    break
            if current != self._current:
                self._current = current
                self._update_position()
        except Exception:
            _guard("カンペの現在位置の更新", notify=False)

    def _update_position(self) -> None:
        """「2/3 見出し」を出す。いま何番目のセクションを見ているかが分かるように。"""
        if not self._tops:
            self._position_text = "見出し（#）なし" if self._headings else ""
        else:
            order = self._current_top_order()
            number = order + 1 if order >= 0 else 0
            label = ""
            if 0 <= self._current < len(self._headings):
                level, text, _position = self._headings[self._current]
                label = ("## " if level == 2 else "# ") + text
            self._position_text = f"{number}/{len(self._tops)}　{label}".rstrip()
        self._draw_position()

    def _draw_position(self) -> None:
        metrics = QFontMetrics(self.position.font())
        width = max(self.position.width(), 40)
        self.position.setText(metrics.elidedText(self._position_text, Qt.ElideRight, width))

    def _set_status(self, text: str) -> None:
        self.status.setText(text or "")
        self.status.setVisible(bool(text))

    # ---------------------------------------------------------------
    # 文字の大きさ
    # ---------------------------------------------------------------
    def font_size(self) -> int:
        return self._font_size

    def zoom(self, steps: int) -> None:
        self.set_font_size(self._font_size + int(steps))

    def set_font_size(self, size: int, notify: bool = True) -> None:
        """本文の文字の大きさを変える。見出しは Markdown の比率のまま一緒に伸び縮みする
        (既定フォントを差し替えるだけで、文字ごとの指定は書き換えない)。"""
        try:
            size = _clamp_notes_font(size)
            if size == self._font_size:
                return
            self._font_size = size
            self._apply_font()
            if notify:
                self._notify_state()
        except Exception:
            _guard("カンペの文字の大きさ", notify=False)

    def _apply_font(self) -> None:
        self.view.document().setDefaultFont(QFont(NOTES_FONT_FAMILY, self._font_size))
        self._invalidate_tops()

    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.Wheel and obj is self.view.viewport():
                if event.modifiers() & Qt.ControlModifier:
                    steps = event.angleDelta().y() / 120.0
                    if steps:
                        self.zoom(1 if steps > 0 else -1)
                    return True
        except Exception:
            _guard("カンペのホイール操作", notify=False)
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self._invalidate_tops()
            self._draw_position()
        except Exception:
            _guard("カンペの再配置", notify=False)

    # ---------------------------------------------------------------
    # ファイルを触る
    # ---------------------------------------------------------------
    def _on_edit(self) -> None:
        """既定のエディタで開く。カンペが1つも無ければ作ってから開く。

        外部プログラムを起こす経路なので必ず受ける(関連付けが無ければ例外が飛ぶ。
        mirror_notes 側でメモ帳へ落とすところまで面倒を見ている)。"""
        try:
            path = mirror_notes.open_in_editor(self._name)
            if path is None:
                self._set_status("エディタで開けませんでした")
                return
            self._set_status(f"編集中: {path.name}（直したら「再読込」）")
            if not self._name:
                # 作られたばかり。名前を引き直して、そのまま読めるようにする。
                self.load(path.stem)
                self._notify_state()
        except Exception:
            _guard("カンペの編集", notify=False)

    def _on_folder(self) -> None:
        try:
            if mirror_notes.open_folder() is None:
                self._set_status("notes フォルダを開けませんでした")
        except Exception:
            _guard("カンペのフォルダを開く", notify=False)

    def _notify_state(self) -> None:
        if self._on_state is None:
            return
        try:
            self._on_state(self._name, self._font_size)
        except Exception:
            _guard("カンペの状態の通知", notify=False)


# ---------------------------------------------------------------
# ミラー先の窓
# ---------------------------------------------------------------
class MirrorWindow(_TopmostWindow):
    """選んだモニタに全画面で出し、手元の選択範囲を毎フレーム映す窓。

    マウスは透過しない。透過すると、全画面で覆っているせいで発表者からは見えない
    「下のアプリ」へクリックが素通しで届く。見えないものを誤って操作するくらいなら、
    クリックを吸って何も起きないほうが安全(presenter_overlay.BlankOverlay が透過しない
    のと同じ判断)。閉じる手段はホットキーとトレイメニューにある。"""

    click_through = False

    def __init__(self, source_rect: QRect, screen, app_settings: dict):
        super().__init__(screen.geometry())
        self.source_rect = QRect(source_rect)
        self.screen_name = screen.name()
        cfg = mirror_config(app_settings)

        fps = min(max(_as_int(cfg.get("fps"), DEFAULT_FPS), 1), MAX_FPS)
        self.fps = fps

        self._cursor_size = max(_as_int(cfg.get("cursor_size"), DEFAULT_CURSOR_SIZE), 8)
        self._cursor_color = _as_color(cfg.get("cursor_color"), DEFAULT_CURSOR_COLOR)
        self._cursor_outline = _as_color(cfg.get("cursor_outline"), DEFAULT_CURSOR_OUTLINE)

        self._ripple_enabled = bool(cfg.get("click_ripple", DEFAULT_CLICK_RIPPLE))
        self._ripple_ms = max(_as_int(cfg.get("click_ripple_ms"), DEFAULT_CLICK_RIPPLE_MS), 1)
        self._ripple_radius = max(
            _as_int(cfg.get("click_ripple_radius"), DEFAULT_CLICK_RIPPLE_RADIUS), 4
        )
        self._ripple_color = _as_color(cfg.get("click_ripple_color"), DEFAULT_CLICK_RIPPLE_COLOR)

        # レーザーとスポットライトの見た目は presenter_overlay の設定
        # (presenter_overlay セクション)をそのまま使う。同じ道具の同じ光点なので、
        # 色や大きさを2箇所で設定させる意味が無い。
        self._laser = presenter_overlay.laser_params(app_settings)
        self._spot = presenter_overlay.spotlight_params(app_settings)
        self.laser_on = False
        self.spotlight_on = False

        # 黒画面/白画面。ミラー中は「ミラー先だけ」を覆う("black"/"white"/None)。
        # 手元の画面へ重ねる presenter_overlay.BlankOverlay は使わない——あれはカーソルの
        # ある画面を覆うので、手元まで真っ黒になって次に何を見せるか準備できなくなる
        # (撮る範囲ごと覆われた結果がミラーにも映る、という以前の作りを改めたもの)。
        blank_cfg = presenter_overlay.overlay_config(app_settings)
        self.blank_kind = None
        self._blank_colors = {
            "black": _as_color(
                blank_cfg.get("blank_black_color"), presenter_overlay.DEFAULT_BLANK_BLACK
            ),
            "white": _as_color(
                blank_cfg.get("blank_white_color"), presenter_overlay.DEFAULT_BLANK_WHITE
            ),
        }
        for color in self._blank_colors.values():
            # 透けると意味が無い(BlankOverlay と同じ判断)。
            color.setAlpha(255)

        # 拡大方法(smooth / fast / auto)。冒頭の実測コメントを参照。
        self._scaling = scaling_mode(app_settings)

        # 静止(フリーズ)。手元で資料を切り替える間、その様子を見せないための状態。
        # 立っている間は撮りに行かないので、向こうには最後の1枚が出たままになる。
        self.frozen = False

        # 直近のフレーム。QImage は暗黙的共有なので、grab_region が返す copy() 済みの
        # ものをそのまま持つ(こちらでさらに触らない)。
        self._frame = None
        self._cursor_global = cursor_pos()
        self._buttons = (False, False)
        # (押した位置(グローバル), 押した時刻) の並び。古いものから消えていく。
        self._ripples = []

        # 実測のフレームレート。手元の枠の帯に出すのと、検証で数字を見るために数える。
        # current_fps は直近1秒ぶん(始めてからの平均だと、重い範囲に変えても数字が
        # なかなか動かない)。measured_fps() は始めてからの通算。
        self.frame_count = 0
        self.start_time = None
        self.current_fps = 0.0
        self._fps_mark_time = None
        self._fps_mark_count = 0

        self._timer = QTimer(self)
        # 既定(CoarseTimer)はWindowsで十数ms単位に丸められ、30fpsを狙っても実測が
        # 目に見えて落ちる(presenter_overlay の poll と同じ理由)。
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(max(int(round(1000.0 / fps)), 1))
        self._timer.timeout.connect(self._on_tick)

    # ---------------------------------------------------------------
    # 座標の対応
    # ---------------------------------------------------------------
    def video_rect(self) -> QRectF:
        """窓の中で実際に映像が出る矩形(ローカル座標)。

        アスペクト比は保ち、余った部分は黒のまま残す(引き伸ばすと文字が歪む)。"""
        source = self.source_rect
        if source.width() <= 0 or source.height() <= 0:
            return QRectF(0, 0, 0, 0)
        scale = min(self.width() / source.width(), self.height() / source.height())
        width = source.width() * scale
        height = source.height() * scale
        return QRectF(
            (self.width() - width) / 2.0,
            (self.height() - height) / 2.0,
            width,
            height,
        )

    def scale(self) -> float:
        """手元1pxがミラー先で何pxになるか。"""
        source = self.source_rect
        if source.width() <= 0 or source.height() <= 0:
            return 1.0
        return min(self.width() / source.width(), self.height() / source.height())

    def map_to_window(self, point_global: QPoint) -> QPointF:
        """手元のグローバル座標(論理)を、この窓の中の座標へ直す。
        カーソル・レーザー・スポット・クリック波紋の位置はすべてここを通す。"""
        video = self.video_rect()
        scale = self.scale()
        return QPointF(
            video.x() + (point_global.x() - self.source_rect.x()) * scale,
            video.y() + (point_global.y() - self.source_rect.y()) * scale,
        )

    def contains_source(self, point_global: QPoint) -> bool:
        return self.source_rect.contains(point_global)

    def set_source_rect(self, source_rect: QRect) -> None:
        """映す範囲を差し替える。ミラーを畳まずに範囲だけ選び直すための入口。

        窓もタイマーもそのままなので、向こうの画面は消えない(畳んで作り直すと、
        画面共有に出しているモニタが一瞬黒くなる)。前のフレームは大きさが違うから
        捨てる——引き伸ばして1枚だけ歪んだ絵を出すことになる。波紋も捨てる
        (前の範囲で押した位置なので、新しい範囲では別の場所を指す)。"""
        self.source_rect = QRect(source_rect)
        self._frame = None
        self._ripples = []
        self._buttons = (False, False)
        if not self.frozen and self.blank_kind is None:
            try:
                self.grab_frame()
            except Exception:
                _guard("フレームの更新", notify=False)
        self.update()

    def move_source_rect(self, source_rect: QRect) -> None:
        """大きさを変えずに位置だけ差し替える。ドラッグで範囲を動かしている間の入口。

        set_source_rect と違って撮り直さないし、前のフレームも捨てない。ドラッグ中は
        マウスが動くたびにここへ来るので(毎秒100回以上ありうる)、1枚17msの取得を
        呼んでいたら追いつかない。大きさは変わらないので前の絵をそのまま出しておけば
        歪まず、次のフレーム(最大33ms後)には新しい位置の絵に入れ替わる。

        波紋だけは捨てる。押した位置は前の範囲の座標なので、動かした先では別の場所を指す。"""
        self.source_rect = QRect(source_rect)
        self._ripples = []
        self._buttons = (False, False)

    def resize_source_rect(self, source_rect: QRect) -> None:
        """位置(左上)はそのままに、大きさを差し替える。範囲を掴んで拡げ縮めする入口。

        move_source_rect と同じで撮り直さない。掴んで動かしている間はマウスが動くたびに
        ここへ来るので、1枚17msの取得を呼んでいたら追いつかない。前の絵はそのまま出して
        おく——比率を保って変えている限り拡大率が変わるだけで歪まず、次のフレーム
        (最大33ms後)には新しい範囲の絵に入れ替わる。Shift で比率を捨てたときだけ、
        その1フレームぶんは縦横の伸びが違う絵になる。

        move_source_rect と違って描き直しを頼むのは、静止中と黒/白画面の間はタイマーが
        止まっていて誰も描き直さないため。映像の出る矩形(video_rect)は範囲の比率から
        決まるので、頼まないと帯の位置が古いまま残る。"""
        self.source_rect = QRect(source_rect)
        self._ripples = []
        self._buttons = (False, False)
        self.update()

    def set_blank(self, kind) -> None:
        """ミラー先だけを黒(または白)で覆う。kind は "black"/"white"/None。

        覆っている間は撮りに行かない。向こうには単色しか出ていないので、撮る意味が
        無いうえ、手元で資料を切り替える(まさにこの機能を使う場面)ときにCPUを空けられる。"""
        kind = kind if kind in ("black", "white") else None
        if kind == self.blank_kind:
            return
        self.blank_kind = kind
        if kind is not None:
            self.current_fps = 0.0
        else:
            # 覆っていた間は数えていないので、区間の起点を取り直す(set_frozen と同じ)。
            self._fps_mark_time = None
            self._fps_mark_count = self.frame_count
        self.update()

    def set_spotlight(self, radius: int, dim: float) -> None:
        """スポットライトの半径と暗さを差し替える。発表中にその場で変えるための入口。

        窓もタイマーも作り直さない。draw_spotlight へ渡す値を持ち替えるだけで、
        次に描くときから新しい大きさになる。"""
        self._spot["radius"] = max(int(radius), 1)
        self._spot["dim_alpha"] = int(round(max(min(float(dim), 1.0), 0.0) * 255))
        if self.spotlight_on:
            self.update()

    def set_frozen(self, frozen: bool) -> None:
        """静止を切り替える。立てた瞬間に1回描き直すのは、最後のフレームに描いてある
        カーソルやレーザーを消すため(止まった絵の上でカーソルだけが古い位置に残ると、
        見ている側には「発表者がそこを指したまま黙っている」ように見える)。"""
        frozen = bool(frozen)
        if frozen == self.frozen:
            return
        self.frozen = frozen
        if frozen:
            self.current_fps = 0.0
        else:
            # 止めていた間は数えていないので、区間の起点を取り直す。
            self._fps_mark_time = None
            self._fps_mark_count = self.frame_count
        self.update()

    # ---------------------------------------------------------------
    # 表示・周期
    # ---------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        self.start_time = time.perf_counter()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _on_tick(self):
        """周期の本体。ここは必ず try で受けること(PySide6はスロットから例外が
        投げ切られるとプロセスごと終わる)。1枚撮って、入力を読んで、描き直す。"""
        try:
            if self.frozen or self.blank_kind is not None:
                # 静止中は撮りに行かないし描き直しもしない。カーソルも読まない——
                # 止まった絵の上でカーソルだけが動いたら、指している場所が嘘になる。
                # 黒/白画面で覆っている間も同じ(向こうには単色しか出ていない)。
                return
            self.grab_frame()
            self._update_fps()
            self._poll_input()
            self.update()
        except Exception:
            _guard("フレームの更新", notify=False)

    def _update_fps(self) -> None:
        """直近1秒で何枚出せたかを数え直す。"""
        now = time.perf_counter()
        if self._fps_mark_time is None:
            self._fps_mark_time = now
            self._fps_mark_count = self.frame_count
            return
        elapsed = now - self._fps_mark_time
        if elapsed >= 1.0:
            self.current_fps = (self.frame_count - self._fps_mark_count) / elapsed
            self._fps_mark_time = now
            self._fps_mark_count = self.frame_count

    def grab_frame(self) -> None:
        """手元の選択範囲を1枚撮る。

        devicePixelRatio を1に落としておく。grab_region は「この画像は物理ピクセルで
        持っているが論理サイズはこれ」という印を付けて返すが、こちらは映像を枠へ
        引き伸ばして描くだけで、論理サイズを使う場面が無い。印が付いたままだと
        drawImage の転送元がどちらの単位なのか曖昧になるので、生のピクセルとして扱う。
        grab_region が返すのは copy() 済みの独立した画像なので、書き換えてよい。

        include_layered=False にしてあるのは、毎フレーム撮り続ける経路だから。詳しくは
        capture_grab._grab のコメント(CAPTUREBLT を外す理由=カーソルのちらつき)。"""
        frame = grab_region(self.source_rect, include_layered=False)
        frame.setDevicePixelRatio(1.0)
        self._frame = frame
        self.frame_count += 1

    def _poll_input(self) -> None:
        """カーソル位置とマウスのボタンを読む。押した瞬間だけ波紋を1つ足す。"""
        self._cursor_global = cursor_pos()

        if not self._ripple_enabled:
            return
        buttons = mouse_buttons_down()
        now = time.perf_counter()
        for index in (0, 1):
            if buttons[index] and not self._buttons[index]:
                self._ripples.append((QPoint(self._cursor_global), now))
        self._buttons = buttons

        # 出し終えたものを捨てる。放っておくと発表中ずっと溜まる。
        limit = self._ripple_ms / 1000.0
        self._ripples = [item for item in self._ripples if now - item[1] <= limit]

    def measured_fps(self) -> float:
        """始めてからの通算フレームレート。診断用(設定した fps と実際が違うことがある)。"""
        if not self.start_time:
            return 0.0
        elapsed = time.perf_counter() - self.start_time
        return self.frame_count / elapsed if elapsed > 0 else 0.0

    def status_text(self) -> str:
        """手元の枠の帯に出す1行。範囲の大きさと、出せている実測フレームレート。

        設定値も併記するのは、数字が低いときに「設定で落としてある」のか
        「重くて出ていない」のかが区別できないと、手の打ちようが無いため。"""
        if self.blank_kind is not None:
            # 手元は普通に見えたままなので、ここに出さないと「向こうは真っ黒」だと
            # 気付きようが無い(静止より優先して出す。覆っている間は静止も見えない)。
            kind = "黒画面" if self.blank_kind == "black" else "白画面"
            return f"⬛ 共有側だけ{kind}（手元は見えています） {self.source_rect.width()}x{self.source_rect.height()}"
        if self.frozen:
            # 静止していることは、数字より先に読めるところへ置く。
            return (
                f"⏸ 静止中（動いていません） {self.source_rect.width()}x"
                f"{self.source_rect.height()}"
            )
        return (
            f"ミラー中 {self.source_rect.width()}x{self.source_rect.height()}"
            f"　{self.current_fps:.0f}fps / 設定 {self.fps}fps"
            f"　拡大 {self.scale():.2f}倍 {'滑らか' if self.smooth_scaling() else '鮮明'}"
        )

    def smooth_scaling(self) -> bool:
        """今の拡大率で滑らかな補間を使うか(設定 scaling と拡大率で決まる)。"""
        return use_smooth(self.scale(), self._scaling)

    # ---------------------------------------------------------------
    # 描画
    # ---------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        # 余白は黒。映像より先に全面を塗る(前のフレームの端が残らないように)。
        painter.fillRect(event.rect(), QColor(0, 0, 0))

        if self.blank_kind is not None:
            # 黒画面/白画面。ミラー先だけを覆い、手元には何もしない。映像もカーソルも
            # レーザーも描かない——覆う目的は「いま見せない」ことなので、上に何か出たら
            # 台無しになる。静止中の印と同じで、ミラー先には状態も出さない
            # (知る必要があるのは発表者だけ。手元の枠とツールバーに出している)。
            painter.fillRect(event.rect(), self._blank_colors.get(self.blank_kind, QColor(0, 0, 0)))
            return

        if self._frame is None:
            return

        video = self.video_rect()
        # 拡大方法。既定(auto)は倍率が整数のときだけ最近傍にする。等倍で映していれば
        # どちらでも同じ絵になる(そもそも補間が起きない)。理由は冒頭の実測コメント。
        painter.setRenderHint(QPainter.SmoothPixmapTransform, self.smooth_scaling())
        painter.drawImage(video, self._frame, QRectF(self._frame.rect()))

        if self.frozen:
            # 静止中はカーソルもレーザーも波紋も描かない。絵が止まっているのに指先だけ
            # 動くと、見ている側には嘘になる。ミラー先には静止中の印も出さない——
            # 静止していることを知る必要があるのは発表者だけで、そのための表示は
            # 手元の枠とツールバーに出している。
            return

        painter.setRenderHint(QPainter.Antialiasing)
        inside = self.contains_source(self._cursor_global)
        center = self.map_to_window(self._cursor_global)

        # スポットライトは映像の上・カーソルの下。減光でカーソルまで暗くしては本末転倒。
        if self.spotlight_on and inside:
            presenter_overlay.draw_spotlight(painter, video, center, **self._spot)

        self._draw_ripples(painter)

        if not inside:
            # 範囲の外にカーソルが出たら何も描かない。
            #
            # 端に貼り付けて見せる案もあったが採らない。ミラーは「この範囲を見せている」
            # という約束で、実際には指していない場所に光点や矢印が居座るのは嘘になる
            # (見ている側は指されたところを見る)。消えること自体が「発表者はいま範囲の
            # 外を触っている」という正しい情報になるし、戻ってくれば同じ位置に現れる。
            return
        if self.laser_on:
            # レーザー中はカーソルを描かない。同じ位置に2つ重ねると、光点の芯に矢印が
            # 刺さって何を指しているのか分からなくなる。
            presenter_overlay.draw_laser(painter, center, **self._laser)
        else:
            self._draw_cursor(painter, center)

    def _draw_cursor(self, painter: QPainter, center: QPointF) -> None:
        """矢印を1つ描く。実カーソルの絵は使わない(GetCursorInfo/DrawIconEx を持ち込むと
        ハンドルの後始末が増えるうえ、拡大率によって大きさが変わる)。

        大きさは拡大率によらず一定。選んだ範囲が狭いほど拡大率は上がるが、それに合わせて
        カーソルまで大きくすると、範囲ごとに見え方が変わって落ち着かない。

        先端が center。白で塗って濃い色で縁取るのは、明るい資料でも暗い資料でも輪郭が
        残るようにするため。"""
        size = float(self._cursor_size)
        polygon = QPolygonF(
            [QPointF(center.x() + x * size, center.y() + y * size) for x, y in _CURSOR_POLYGON]
        )
        pen = QPen(self._cursor_outline)
        pen.setWidthF(max(size * 0.06, 1.5))
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(self._cursor_color)
        painter.drawPolygon(polygon)

    def _draw_ripples(self, painter: QPainter) -> None:
        """クリックした瞬間の波紋。押した位置から輪が広がって薄くなる。

        画面共有では「今押した」が伝わらない(カーソルの形は変わらないし、押した音も
        向こうには届かない)。左右どちらのボタンでも同じ絵を出す——見ている側に必要なのは
        「操作した」ことで、どちらのボタンかではない。"""
        if not self._ripples:
            return
        now = time.perf_counter()
        duration = self._ripple_ms / 1000.0
        painter.setBrush(Qt.NoBrush)
        for point_global, started in self._ripples:
            if not self.contains_source(point_global):
                continue
            progress = min(max((now - started) / duration, 0.0), 1.0)
            # 大きさは拡大率によらず一定(カーソルと同じ理由)。
            radius = self._ripple_radius * (0.25 + 0.75 * progress)
            color = QColor(self._ripple_color)
            color.setAlphaF(0.85 * (1.0 - progress))
            pen = QPen(color)
            pen.setWidthF(max(self._ripple_radius * 0.08, 2.0))
            painter.setPen(pen)
            painter.drawEllipse(self.map_to_window(point_global), radius, radius)


# ---------------------------------------------------------------
# 開始・終了の管理
# ---------------------------------------------------------------
class MirrorController:
    """範囲選択 → ミラー窓 → 手元の枠、の3つの参照と開閉を1か所で持つ。

    参照をここで抱えるのは、開いた窓の参照を持たないとGCで即消えるため(このアプリで
    何度も踏んでいる)。ScreenFeature がこれを1つだけ属性で持つ。

    どのメソッドも例外を投げない。呼び元はメニューのスロットとホットキーで、どちらも
    投げ切ると常駐ごと終わる場所だから(presenter_overlay.OverlayController と同じ作法)。"""

    def __init__(self, app_settings: dict, settings_path=None, notify=None):
        self.app_settings = app_settings
        self.settings_path = settings_path
        self._notify = notify
        self._overlay = None       # 範囲選択中のオーバーレイ
        self._mirror = None        # ミラー先の窓
        self._frame = None         # 手元の枠
        self._toolbar = None       # 手元のツールバー
        self._notes = None         # 手元のカンペ(発表者だけが見るメモ)
        # 「ミラーを続けたまま範囲だけ選び直している」最中か。選び終えたときの行き先
        # (新規に始めるのか、今の窓の範囲を差し替えるのか)がこれで決まる。
        self._reselecting = False
        # 選び直しに入る前の静止の状態。選び直しの間は必ず静止させるので、終わったら
        # ここへ戻す(自分で静止させていた人の状態を勝手に解除しない)。
        self._frozen_before = False
        # プレゼン支援(レーザー等)の切り替えと状態を借りる先。ScreenFeature が
        # attach_presenter で渡す。ツールバーの黒画面/白画面はこちらにしか無い。
        self._presenter_toggle = None
        self._presenter_state = None
        # 「この画面はミラー窓が覆っている」を知らせる先。ScreenFeature が
        # attach_screen_cover で渡す(タスクバーウィジェットを引っ込めさせるため)。
        self._screen_cover = None
        self._covered_screen = None
        # 前回選んだ比率。次に始めるときの初期値にする(設定ファイルには書かない。
        # 発表ごとに選び直す性質の値で、残すほどのものではない)。
        self.aspect = str(mirror_config(app_settings).get("aspect") or DEFAULT_ASPECT)

        # カンペを出すかどうか。既定は設定 notes(既定 True)。ツールバーの「カンペ」で
        # 切り替えたぶんは、その起動の間だけ覚える(比率と同じ扱い。発表ごとに要否が
        # 変わる性質の値で、押すたびに settings.json を書きに行くほどのものではない)。
        cfg = mirror_config(app_settings)
        self._notes_wanted = bool(cfg.get("notes", DEFAULT_NOTES))
        # どのカンペを開いていたか / 何ポイントで読んでいたか。こちらは設定に残す
        # (次の発表でも同じものを同じ大きさで開きたい。毎回選び直す性質の値ではない)。
        self._notes_name = str(cfg.get("notes_file") or "")
        self._notes_font_size = _clamp_notes_font(cfg.get("notes_font_size"))

        # スポットライトの調整を settings.json へ書き戻すのを遅らせるためのタイマー。
        # ホイールは1回転で何目盛りも飛んでくるので、そのたびに書くとファイルを何十回も
        # 開き直すことになる(SPOTLIGHT_SAVE_DELAY_MS)。
        self._spot_save_timer = QTimer()
        self._spot_save_timer.setSingleShot(True)
        self._spot_save_timer.timeout.connect(self._flush_spotlight)

        # カンペの状態(開いていたファイル・文字の大きさ)の書き戻しも同じ理由で遅らせる。
        # Ctrl+ホイールは1回転で何目盛りも飛んでくる。
        self._notes_save_timer = QTimer()
        self._notes_save_timer.setSingleShot(True)
        self._notes_save_timer.timeout.connect(self._flush_notes)

        # 大きさを変えている間の控え。掴んだ瞬間の範囲と比率を持ち、以降の大きさは
        # そこからの絶対量で決める(SIZE_APPLY_INTERVAL_MS のコメント参照)。
        self._size_base = None
        self._size_ratio = 16.0 / 9.0
        # 間引いて反映しなかった最後の (dx, dy, 比率を捨てるか)。離すときに当てる。
        self._size_pending = None
        self._size_applied = 0.0
        # ホイールで変えたあと、枠とツールバーを作り直すまでの待ち。回している間は
        # 押し直され続けるので、手が止まってから一度だけ走る。
        self._size_settle_timer = QTimer()
        self._size_settle_timer.setSingleShot(True)
        self._size_settle_timer.timeout.connect(self.end_resize)

    def attach_screen_cover(self, callback) -> None:
        """「いまミラー窓が覆っている画面の名前」を知らせる先を受け取る。

        タスクバーウィジェットに引っ込んでもらうために要る。あちらは
        GetForegroundWindow で全画面アプリを見分けるが、ミラー窓は前面にならない作りなので
        永久に気付けない(taskbar_widget.set_app_covered のコメントに詳しく書いた)。
        こちらは自分がどの画面を覆っているかを知っているので、こちらから教える。"""
        self._screen_cover = callback
        if callback is not None:
            try:
                callback(self._covered_screen)
            except Exception:
                _guard("画面を覆っていることの通知", notify=False)

    def _set_covered_screen(self, name) -> None:
        """覆っている画面の名前を控え、知らせる先があれば知らせる。"""
        name = name or None
        if name == self._covered_screen:
            return
        self._covered_screen = name
        if self._screen_cover is None:
            return
        try:
            self._screen_cover(name)
        except Exception:
            _guard("画面を覆っていることの通知", notify=False)

    def attach_presenter(self, toggle, is_on) -> None:
        """手元のツールバーから呼ぶ、プレゼン支援の切り替えと状態取得を受け取る。

        ここで受け取るのは ScreenFeature.toggle_presenter_overlay と
        _presenter_overlay_active。黒画面/白画面の窓を持っているのはあちら
        (presenter_overlay.OverlayController)で、こちらからは触れない。レーザーと
        スポットライトも、行き先の振り替え・通知・トレイメニューのチェックが
        あちらに揃っているので、同じ入口へ回すほうが状態が食い違わない。"""
        self._presenter_toggle = toggle
        self._presenter_state = is_on

    # ---------------------------------------------------------------
    # 状態
    # ---------------------------------------------------------------
    def is_active(self) -> bool:
        """ミラーが出ているか。範囲を選んでいる最中は含まない。"""
        return self._mirror is not None

    def is_selecting(self) -> bool:
        return self._overlay is not None

    def is_reselecting(self) -> bool:
        """ミラーを続けたまま範囲を選び直している最中か。"""
        return self._reselecting

    def is_frozen(self) -> bool:
        return self._mirror is not None and bool(self._mirror.frozen)

    def notify(self, message: str) -> None:
        if self._notify is not None:
            self._notify("画面ミラー", message)

    # ---------------------------------------------------------------
    # ミラー先のモニタ
    # ---------------------------------------------------------------
    def target_screen(self, source_rect: QRect = None):
        """ミラーを出すモニタ(QScreen)。決められなければ None。

        設定 target_screen_name に QScreen.name() が書いてあればそれ。無い/見つからない
        ときは「選択範囲が乗っていないモニタ」の先頭を選ぶ。モニタが3枚以上ある構成では
        どれに出すかを選べないと困るので、メニューから選んだ名前をここで引く形にしてある
        (選び方の本体は pick_target_screen)。"""
        wanted = str(mirror_config(self.app_settings).get("target_screen_name") or "")
        return pick_target_screen(available_screens(), wanted, source_rect)

    def set_target_screen(self, name: str) -> None:
        """ミラー先のモニタを選び直して保存する。動作中なら出し直す。"""
        try:
            self.app_settings.setdefault("screen_mirror", {})["target_screen_name"] = name
            self._save_keys({"target_screen_name": name})
            if self.is_active():
                source_rect = QRect(self._mirror.source_rect)
                self.stop()
                self._start_mirror(source_rect)
        except Exception:
            _guard("ミラー先の変更")

    def _save_keys(self, values: dict, section_name: str = "screen_mirror") -> None:
        """settings.json の指定セクションへ、指定したキーだけを書き戻す。

        メモリ上の app_settings は既定値をマージ済みなので、それを丸ごと書き出すと
        未設定の既定値まで明示的に書かれてファイルの姿が変わってしまう
        (launcher.save_bookmark と同じ作法)。

        セクション名を引数にしてあるのは、スポットライトの半径と暗さが
        presenter_overlay セクションの値だから(画面へ重ねる側と共有している。
        同じ道具の同じ穴を2箇所で設定させる意味が無い)。"""
        if not self.settings_path:
            return
        try:
            import json
            import os

            import settings as settings_module

            stored = {}
            if os.path.exists(self.settings_path):
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    stored = json.load(f)
            if not isinstance(stored, dict):
                stored = {}
            section = stored.setdefault(section_name, {})
            if not isinstance(section, dict):
                section = {}
                stored[section_name] = section
            section.update(values)
            settings_module.save_settings(stored, self.settings_path)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            print(f"[tray-tools] 画面ミラーの設定を保存できません: {e}", file=sys.stderr)

    # ---------------------------------------------------------------
    # 開始・終了
    # ---------------------------------------------------------------
    def activate(self) -> bool:
        """ホットキー(Ctrl+Alt+P)とメニューの入口。「開始」または「範囲の選び直し」。

        以前はここが開始/終了のトグルだった。やめたのは、発表の途中で映す場所を変えたく
        なったときに、いったんミラーを畳むことになるため——画面共有に出しているモニタが
        一瞬黒くなり、見ている側からは事故に見える。今は映したまま範囲だけを選び直す。

        そのぶん、このキーでは終われない。終わらせる手段はトレイメニューの「⏹ 終了」、
        手元のツールバーの ✕、専用ホットキー(既定 Ctrl+Alt+Q)の3つ。押しても終われない
        状態を作らないよう、必ずどれかは残しておくこと。

        戻り値は「範囲選択が出たか」。"""
        try:
            if self.is_selecting():
                # 選択中にもう一度押されたら、その選択をやめる(ミラー中なら映したまま)。
                # 押すたびに選択オーバーレイが増えるのを防ぐ意味もある。
                self._on_canceled()
                return False
            if self.is_active():
                return self.start_reselect()
            return self.start_selection()
        except Exception:
            _guard("画面ミラーの操作")
            return False

    def start_selection(self) -> bool:
        """範囲選択オーバーレイを出す。選び終えたらミラーが始まる。"""
        if self._overlay is not None or self._mirror is not None:
            return False
        if len(available_screens()) < 2:
            # 1枚しか無いPCでは、映した先が撮る対象そのものになって入れ子になる。
            self.notify("モニタが1台しかありません")
            return False
        self._reselecting = False
        return self._open_overlay(False)

    def start_reselect(self) -> bool:
        """ミラーを続けたまま、範囲だけ選び直す。

        選び直しの間はミラーを静止させる。範囲選択オーバーレイは全モニタを覆う減光窓
        なので、撮り続けたままだと向こうには「減光した自分の画面」が映る。静止させれば
        最後の1枚が出たままになり、見ている側からは何も起きていないように見える。
        手元の枠とツールバーも畳む(新しい範囲の外側へ作り直すので、動かすより単純)。"""
        if self._mirror is None:
            return self.start_selection()
        if self._overlay is not None:
            return False
        self._frozen_before = bool(self._mirror.frozen)
        self._mirror.set_frozen(True)
        self._hide_frame()
        self._reselecting = True
        if not self._open_overlay(True):
            self._reselecting = False
            self._restore_after_reselect()
            return False
        return True

    def _open_overlay(self, reselecting: bool) -> bool:
        try:
            fps_limit = min(
                max(_as_int(mirror_config(self.app_settings).get("fps"), DEFAULT_FPS), 1), MAX_FPS
            )
            self._overlay = AspectSelectionOverlay(
                self.aspect, fps_limit, self.presets(), reselecting
            )
            self._overlay.selection_made.connect(self._on_selection_made)
            self._overlay.canceled.connect(self._on_canceled)
            self._overlay.show()
        except Exception:
            self._overlay = None
            _guard("範囲選択の表示")
            return False
        return True

    # ---------------------------------------------------------------
    # プリセット
    # ---------------------------------------------------------------
    def anchor_screen(self, target_screen=None):
        """プリセットの x/y を測る基準のモニタ。今カーソルのあるモニタ。

        ただしミラー先だけは選ばない。そこを基準にすると、プリセットで作った範囲が
        ミラー先に乗って始められない(自分を撮ることになる)。"""
        target_name = target_screen.name() if target_screen is not None else ""
        screens = available_screens()
        current = QGuiApplication.screenAt(cursor_pos())
        if current is not None and current.name() != target_name:
            return current
        for screen in screens:
            if screen.name() != target_name:
                return screen
        return current

    def presets(self) -> list:
        """範囲選択に出すプリセット [(見出し, QRect), ...]。"""
        try:
            target = self.target_screen()
            return preset_rects(self.app_settings, self.anchor_screen(target), target)
        except Exception:
            _guard("プリセットの用意", notify=False)
            return []

    def _close_selection(self) -> None:
        """オーバーレイは canceled をemitするだけで自分では閉じない。参照を捨てるだけだと
        全画面を覆うウィジェットが消えるかどうかGC任せになり、マウス操作を奪ったまま
        残り得る(feature_screen._on_canceled と同じ作法)。"""
        overlay = self._overlay
        self._overlay = None
        if overlay is None:
            return
        try:
            overlay.close()
            overlay.deleteLater()
        except RuntimeError:
            pass

    def _on_canceled(self) -> None:
        try:
            if self._overlay is not None:
                self.aspect = self._overlay.aspect
            self._close_selection()
            if self._reselecting:
                # 選び直しをやめただけ。ミラーは畳まない(Esc に「終了」の意味を
                # 持たせると、範囲を選び直そうとして思い直しただけで共有が切れる)。
                self._reselecting = False
                self._restore_after_reselect()
        except Exception:
            _guard("範囲選択の後始末", notify=False)

    def _restore_after_reselect(self) -> None:
        """選び直しに入る前の状態へ戻す。静止も枠もツールバーも、入る前と同じにする。"""
        if self._mirror is None:
            return
        self._mirror.set_frozen(self._frozen_before)
        self._show_frame(self._mirror.source_rect)

    def _on_selection_made(self, rect_global: QRect) -> None:
        try:
            if self._overlay is not None:
                self.aspect = self._overlay.aspect
            source_rect = QRect(rect_global)
            reselecting = self._reselecting
            self._reselecting = False
            self._close_selection()
            if reselecting and self._mirror is not None:
                self._apply_source_rect(source_rect)
            else:
                self._start_mirror(source_rect)
        except Exception:
            _guard("画面ミラーの開始")

    def _apply_source_rect(self, source_rect: QRect) -> bool:
        """選び直した範囲へ切り替える。ミラー窓はそのまま使い回す。"""
        if self._mirror is None:
            return False
        # ミラー窓はミラー先のモニタ全面に出しているので、その矩形がそのまま
        # 「映してはいけない範囲」になる。
        if self._mirror.geometry().intersects(source_rect):
            self.notify(
                "選んだ範囲がミラー先に重なっています\n"
                "範囲はそのままにしました（もう一度選び直してください）"
            )
            self._restore_after_reselect()
            return False

        self._mirror.set_source_rect(source_rect)
        self._mirror.set_frozen(self._frozen_before)
        self._show_frame(source_rect)
        self.notify(f"映す範囲を {source_rect.width()}x{source_rect.height()} に変えました")
        return True

    def _start_mirror(self, source_rect: QRect) -> bool:
        screen = self.target_screen(source_rect)
        if screen is None:
            self.notify("ミラー先のモニタが見つかりません")
            return False
        if screen.geometry().intersects(source_rect):
            # 自分を撮ると無限に入れ子になる。設定でミラー先を指定していると起こりうる。
            self.notify(
                f"選択範囲がミラー先({screen.name()})に重なっています\n"
                "別のモニタを選ぶか、範囲を選び直してください"
            )
            return False

        try:
            self._mirror = MirrorWindow(source_rect, screen, self.app_settings)
            self._mirror.show()
        except Exception:
            self._mirror = None
            _guard("ミラー窓の表示")
            return False

        # このモニタのタスクバーウィジェットに引っ込んでもらう。押し上げ合いになると
        # 共有側でチカチカする(attach_screen_cover のコメント参照)。
        self._set_covered_screen(screen.name())
        self._show_frame(source_rect)

        self.notify(
            f"{screen.name()} へ {source_rect.width()}x{source_rect.height()} を"
            f"{self._mirror.fps}fpsで表示中"
        )
        return True

    # ---------------------------------------------------------------
    # 手元の枠とツールバーとカンペ
    # ---------------------------------------------------------------
    def _show_frame(self, source_rect: QRect) -> None:
        """手元の枠・ツールバー・カンペを、この範囲に合わせて出し直す。

        動かさずに作り直すのは、どれも「範囲の外側のどこに置けるか」を作るときに
        決めているため(枠は帯を上に置けるか、ツールバーは下に収まるか、カンペは右に
        入るか)。範囲が変わればその答えも変わるので、位置だけ動かしても正しくならない。

        作り直すぶん、カンペの「どこまで読んでいたか」だけは持ち越す。範囲をドラッグで
        少し動かしただけで先頭へ戻ると、発表中にどこを話していたか見失う。"""
        notes_section = self._notes.section_order() if self._notes is not None else -1
        self._hide_frame()
        if self._mirror is None:
            return
        cfg = mirror_config(self.app_settings)

        anchor = QRect(source_rect)
        if bool(cfg.get("source_frame", DEFAULT_SOURCE_FRAME)):
            try:
                self._frame = SourceFrameWindow(
                    source_rect, self.app_settings, status=self._mirror.status_text
                )
                self._frame.set_frozen(self._mirror.frozen)
                self._frame.set_blank(self._mirror.blank_kind)
                self._frame.show()
                # ツールバーは枠のさらに外に置く(枠の帯と重ならないように)。
                anchor = QRect(self._frame.geometry())
            except Exception:
                # 枠が出ないだけならミラーは続けられる。ここで畳むほうが損。
                self._frame = None
                _guard("手元の枠の表示", notify=False)

        toolbar_rect = QRect()
        if bool(cfg.get("toolbar", DEFAULT_TOOLBAR)):
            try:
                geometry = toolbar_geometry(anchor, source_rect)
                if geometry is None:
                    # 撮影範囲に掛からずに置ける場所が無かった。出さないほうがまし
                    # (映り込むと、映った自分をまた撮る入れ子になる)。
                    self.notify("ツールバーを置く場所がありません\n範囲の外に余白がある位置を選んでください")
                else:
                    self._toolbar = MirrorToolbarWindow(
                        geometry,
                        self._toolbar_actions(),
                        self._toolbar_state,
                        adjust=self.adjust_spotlight,
                        move=self.move_source_by,
                        move_end=self.end_move,
                        resize_begin=self.begin_resize,
                        resize_to=self.resize_source_to,
                        resize_end=self.end_resize,
                        resize_steps=self.resize_source_by_steps,
                    )
                    self._toolbar.show()
                    toolbar_rect = QRect(geometry)
            except Exception:
                self._toolbar = None
                _guard("ツールバーの表示", notify=False)

        # カンペは最後。ツールバーの場所が決まってからでないと、そこを避けられない。
        if self._notes_wanted and bool(cfg.get("notes", DEFAULT_NOTES)):
            try:
                geometry = notes_geometry(
                    anchor,
                    source_rect,
                    _as_int(cfg.get("notes_width"), DEFAULT_NOTES_WIDTH),
                    avoid=(toolbar_rect, QRect(self._mirror.geometry())),
                )
                if geometry is None:
                    # 撮影範囲(かツールバー、かミラー先)に掛からずに置ける場所が無かった。
                    # カンペは発表者だけが見るものなので、掛けるくらいなら出さない。
                    self.notify(
                        "カンペを置く場所がありません\n範囲の左右に余白がある位置を選んでください"
                    )
                else:
                    self._notes = MirrorNotesWindow(
                        geometry,
                        self._notes_name,
                        self._notes_font_size,
                        on_state=self._on_notes_state,
                    )
                    self._notes.show()
                    self._notes.restore_section(notes_section)
            except Exception:
                self._notes = None
                _guard("カンペの表示", notify=False)

    def _hide_frame(self) -> None:
        """枠・ツールバー・カンペを畳む。ミラーはそのまま。"""
        for name in ("_notes", "_toolbar", "_frame"):
            window = getattr(self, name, None)
            setattr(self, name, None)
            if window is None:
                continue
            try:
                window.close()
                window.deleteLater()
            except RuntimeError:
                pass
            except Exception:
                _guard("手元の枠の後始末", notify=False)

    def _toolbar_actions(self) -> dict:
        """ツールバーの各アイコンを押したときに呼ぶもの。キーは _TOOLBAR_ITEMS と揃える。"""
        return {
            "laser": lambda: self.toggle_presenter("laser"),
            "spotlight": lambda: self.toggle_presenter("spotlight"),
            "black": lambda: self.toggle_presenter("black"),
            "white": lambda: self.toggle_presenter("white"),
            "notes": self.toggle_notes,
            "freeze": self.toggle_freeze,
            "reselect": self.start_reselect,
            "stop": self.stop,
        }

    def _toolbar_state(self, key: str) -> bool:
        """そのアイコンが今点いているか。"""
        if key == "freeze":
            return self.is_frozen()
        if key == "notes":
            return self.is_notes_open()
        if key in ("laser", "spotlight", "black", "white"):
            return self.is_presenter_on(key)
        return False

    def stop(self) -> None:
        """ミラーも枠もツールバーもカンペも選択も畳む。終了時の後始末からも呼ぶ。"""
        self._reselecting = False
        self._close_selection()
        # 大きさを変えている途中で終了されることがある(ツールバーの ✕ は掴みしろの
        # 隣にある)。畳んだ後に作り直しが走らないよう、控えも止める。
        self._size_settle_timer.stop()
        self._size_base = None
        self._size_pending = None
        # 先に知らせる。ミラー窓を畳んでからだと、タスクバーウィジェットが戻るまでの
        # 間に「誰も居ない画面」ができる。
        self._set_covered_screen(None)
        for name in ("_notes", "_toolbar", "_frame", "_mirror"):
            window = getattr(self, name, None)
            setattr(self, name, None)
            if window is None:
                continue
            try:
                window.close()
                window.deleteLater()
            except RuntimeError:
                # C++側が先に消えている場合。参照はもう外したので何もしなくてよい。
                pass
            except Exception:
                _guard("画面ミラーの後始末", notify=False)

    def close_all(self) -> None:
        """終了時の後始末。枠なし・最前面の窓を残したままイベントループを畳むと、
        画面に貼り付いたまま消えないことがある(taskbar_widget と同じ理由)。"""
        self.stop()

    # ---------------------------------------------------------------
    # ミラー先に描くレーザー・スポットライト
    # ---------------------------------------------------------------
    def toggle_light(self, kind: str) -> bool:
        """ミラー先のレーザー/スポットライトを切り替える。戻り値は切り替え後の状態。

        ミラー中はレーザーもスポットも「ミラー先に描く」ものになる。手元の画面に
        重ねてしまうと、それがそのまま撮られて向こうにも映り、二重に見えるうえ
        減光まで焼き込まれる(スポットライトは映像が暗くなって戻せない)。"""
        if self._mirror is None:
            return False
        try:
            if kind == "laser":
                self._mirror.laser_on = not self._mirror.laser_on
                return self._mirror.laser_on
            if kind == "spotlight":
                self._mirror.spotlight_on = not self._mirror.spotlight_on
                return self._mirror.spotlight_on
        except Exception:
            _guard("ミラー先の光点の切り替え", notify=False)
        return False

    def is_light_on(self, kind: str) -> bool:
        if self._mirror is None:
            return False
        if kind == "laser":
            return bool(self._mirror.laser_on)
        if kind == "spotlight":
            return bool(self._mirror.spotlight_on)
        return False

    def toggle_presenter(self, kind: str) -> bool:
        """ツールバーからプレゼン支援(レーザー/スポット/黒画面/白画面)を切り替える。

        attach_presenter で本体へ繋がっていればそちらへ回す。行き先の振り替え・通知・
        トレイメニューのチェックがあちらに揃っているので、同じ入口を通したほうが
        状態が食い違わない。繋がっていないとき(この module だけを動かしたとき)は、
        ここで扱えるレーザーとスポットライトだけを直接切り替える。"""
        try:
            if self._presenter_toggle is not None:
                return bool(self._presenter_toggle(kind))
            if kind in ("laser", "spotlight"):
                return self.toggle_light(kind)
            if kind in ("black", "white"):
                return self.toggle_blank(kind)
        except Exception:
            _guard("プレゼン支援の切り替え", notify=False)
        return False

    def is_presenter_on(self, kind: str) -> bool:
        try:
            if self._presenter_state is not None:
                return bool(self._presenter_state(kind))
            if kind in ("black", "white"):
                return self.is_blank_on(kind)
            return self.is_light_on(kind)
        except Exception:
            _guard("プレゼン支援の状態", notify=False)
            return False

    # ---------------------------------------------------------------
    # 静止(フリーズ)
    # ---------------------------------------------------------------
    def set_freeze(self, frozen: bool) -> bool:
        """ミラーを静止させる/戻す。戻り値は切り替え後の状態。

        止めている間は撮らないので、向こうには最後の1枚が出たままになる。手元で資料を
        切り替えたり、見せたくないものを開いたりする間に使う。

        手元には3か所で出す(枠の色・帯の文字・ツールバーのアイコンと文字)。ミラー先には
        何も出さない——静止していることを知る必要があるのは発表者だけで、向こうに印を
        出したら「今は見せていない」と宣言することになる。"""
        try:
            if self._mirror is None:
                return False
            if self._reselecting:
                # 選び直しの最中は静止を借りている。ここで解除されると、範囲選択の
                # 減光オーバーレイがそのまま向こうへ映る。
                return bool(self._mirror.frozen)
            self._mirror.set_frozen(frozen)
            if self._frame is not None:
                self._frame.set_frozen(self._mirror.frozen)
            if self._toolbar is not None:
                self._toolbar.refresh_state()
            return bool(self._mirror.frozen)
        except Exception:
            _guard("静止の切り替え")
            return self.is_frozen()

    def toggle_freeze(self) -> bool:
        return self.set_freeze(not self.is_frozen())

    # ---------------------------------------------------------------
    # カンペ(発表者だけが見るメモ)
    #
    # 共有側には何も出ない・何も変わらない。ここで作る窓は撮影範囲の外にしか置かない
    # (notes_geometry。置けなければ出さない)ので、押しても向こうの絵は1画素も動かない。
    # ---------------------------------------------------------------
    def is_notes_open(self) -> bool:
        return self._notes is not None

    def toggle_notes(self) -> bool:
        """カンペのパネルを出す/畳む。戻り値は切り替え後の状態。

        枠とツールバーごと作り直すのは _show_frame の作法に従うため(置ける場所は
        範囲ごとに決まるので、位置だけ動かしても正しくならない)。ツールバーの押した
        場所からは QTimer 越しに呼ばれるので、ここで自分を作り直しても問題ない。"""
        try:
            if self._mirror is None:
                return False
            self._notes_wanted = not self.is_notes_open()
            self._show_frame(self._mirror.source_rect)
            return self.is_notes_open()
        except Exception:
            _guard("カンペの切り替え", notify=False)
            return self.is_notes_open()

    def _on_notes_state(self, name: str, font_size: int) -> None:
        """カンペ側で「別のファイルを選んだ」「文字の大きさを変えた」ときに呼ばれる。

        すぐ効かせて、書き戻しは遅らせる(Ctrl+ホイールは目盛りが連続で飛んでくる。
        adjust_spotlight と同じ作法)。"""
        try:
            self._notes_name = str(name or "")
            self._notes_font_size = _clamp_notes_font(font_size)
            section = self.app_settings.setdefault("screen_mirror", {})
            section["notes_file"] = self._notes_name
            section["notes_font_size"] = self._notes_font_size
            self._notes_save_timer.start(NOTES_SAVE_DELAY_MS)
        except Exception:
            _guard("カンペの状態の記録", notify=False)

    def _flush_notes(self) -> None:
        """遅らせておいた書き戻し。タイマーのスロットなので必ず try で受ける。"""
        try:
            self._save_keys(
                {
                    "notes_file": self._notes_name,
                    "notes_font_size": self._notes_font_size,
                }
            )
        except Exception:
            _guard("カンペの設定の保存", notify=False)

    # ---------------------------------------------------------------
    # スポットライトの調整(発表中にその場で変える)
    #
    # 値の置き場は presenter_overlay セクション。画面へ重ねる側と同じ穴・同じ設定キーで、
    # ここで変えた大きさは次に手元へ重ねたときにもそのまま効く。
    # ---------------------------------------------------------------
    def spotlight_values(self) -> tuple:
        """いまの (半径, 暗さ)。設定に書かれていなければ既定。"""
        cfg = presenter_overlay.overlay_config(self.app_settings)
        radius = _as_int(
            cfg.get("spotlight_radius"), presenter_overlay.DEFAULT_SPOTLIGHT_RADIUS
        )
        dim = _as_float(
            cfg.get("spotlight_dim"), presenter_overlay.DEFAULT_SPOTLIGHT_DIM, 0.0, 1.0
        )
        return (
            min(max(radius, SPOTLIGHT_RADIUS_MIN), SPOTLIGHT_RADIUS_MAX),
            min(max(dim, SPOTLIGHT_DIM_MIN), SPOTLIGHT_DIM_MAX),
        )

    def adjust_spotlight(self, radius_delta: float, dim_delta: float) -> str:
        """スポットライトの半径と暗さを増減する。戻り値は手元に出す1行。

        すぐ効かせて、書き戻しは遅らせる(ホイールは目盛りが連続で飛んでくるので、
        1目盛りごとに settings.json を開き直すことになる)。効かせる先は「今出ている
        ミラー窓」と「メモリ上の設定」の両方——後者を更新しないと、次に手元へ重ねる
        スポットライトが古い値のままになる。"""
        try:
            radius, dim = self.spotlight_values()
            radius = int(
                min(
                    max(round(radius + radius_delta), SPOTLIGHT_RADIUS_MIN),
                    SPOTLIGHT_RADIUS_MAX,
                )
            )
            dim = min(max(dim + dim_delta, SPOTLIGHT_DIM_MIN), SPOTLIGHT_DIM_MAX)
            # 0.05刻みで足し引きすると 0.7200000000000001 のような値が溜まる。
            dim = round(dim, 3)

            section = self.app_settings.setdefault("presenter_overlay", {})
            section["spotlight_radius"] = radius
            section["spotlight_dim"] = dim
            if self._mirror is not None:
                self._mirror.set_spotlight(radius, dim)
            self._spot_save_timer.start(SPOTLIGHT_SAVE_DELAY_MS)
            return f"スポット 半径 {radius} / 暗さ {int(round(dim * 100))}%"
        except Exception:
            _guard("スポットライトの調整", notify=False)
            return ""

    def _flush_spotlight(self) -> None:
        """遅らせておいた書き戻し。タイマーのスロットなので必ず try で受ける。"""
        try:
            radius, dim = self.spotlight_values()
            self._save_keys(
                {"spotlight_radius": radius, "spotlight_dim": dim},
                section_name="presenter_overlay",
            )
        except Exception:
            _guard("スポットライトの設定の保存", notify=False)

    # ---------------------------------------------------------------
    # 黒画面・白画面(ミラー先だけ)
    # ---------------------------------------------------------------
    def toggle_blank(self, kind: str) -> bool:
        """ミラー先だけを黒(または白)で覆う。戻り値は切り替え後の状態。

        以前は presenter_overlay の黒画面をそのまま出していた。あれは「カーソルのある
        画面」を覆うので、撮る範囲ごと覆われた結果がミラーにも映る——向こうは黒くなるが、
        手元まで真っ黒になる。次に何を見せるか準備するために手元は見えていなければ
        ならないので、ミラー中は覆う先を向こうだけにする。

        手元には枠の色・帯の文字・ツールバーの点灯と文字で出す(静止のときと同じ流儀)。
        ミラー先には何も足さない。覆う目的は「いま見せない」ことなので、上に印が乗ったら
        台無しになる。"""
        if self._mirror is None:
            return False
        try:
            if kind not in ("black", "white"):
                return False
            new_kind = None if self._mirror.blank_kind == kind else kind
            self._mirror.set_blank(new_kind)
            if self._frame is not None:
                self._frame.set_blank(new_kind)
            if self._toolbar is not None:
                self._toolbar.refresh_state()
            return new_kind == kind
        except Exception:
            _guard("黒画面の切り替え", notify=False)
        return False

    def is_blank_on(self, kind: str) -> bool:
        return self._mirror is not None and self._mirror.blank_kind == kind

    # ---------------------------------------------------------------
    # ミラー範囲の移動(ツールバーのタイトルバーをドラッグ)
    # ---------------------------------------------------------------
    def _source_rect_allowed(self, rect: QRect) -> bool:
        """その矩形を「映す範囲」にしてよいか。移動でも拡縮でも同じ条件を通す。

        歯止めは2つ。ミラー先のモニタに重ねないこと(自分を撮ると無限に入れ子になる)と、
        画面の外へ丸ごと出さないこと(手元から掴む手段が無くなる)。

        大きさの上下限はここでは見ない。移動では変わらないし、拡縮では新しい大きさを
        作る側(_resized_rect)で丸めてから来る——ここで弾くと、上限に当たった瞬間に
        「動かない」だけになって理由が分からない。"""
        if self._mirror is None:
            return False
        if rect.width() <= 0 or rect.height() <= 0:
            return False
        # ミラー窓はミラー先のモニタ全面に出しているので、その矩形がそのまま
        # 「映してはいけない範囲」になる。
        if rect.intersects(self._mirror.geometry()):
            return False
        allowed = desktop_bounds()
        # 中心は画面の中に残す。掴めなくなるのを防ぐのが第一だが、ツールバーの
        # 置き場所も範囲の中心にあるモニタを基準に探している(toolbar_geometry)。
        # 中心が画面の外へ出ると基準のモニタが見つからず、離した瞬間に作り直した
        # ツールバーが画面の外へ行って二度と掴めなくなる(実際そうなっていた)。
        if not allowed.contains(rect.center()):
            return False
        # 範囲が画面より大きいことは(等倍プリセットで端数のある構成だと)ありうるので、
        # 「丸ごと収まること」は求めない。最低限だけ残す。
        visible = rect.intersected(allowed)
        if visible.isEmpty():
            return False
        return (
            visible.width() >= min(MOVE_MIN_VISIBLE, rect.width())
            and visible.height() >= min(MOVE_MIN_VISIBLE, rect.height())
        )

    def move_source_by(self, dx: int, dy: int) -> tuple:
        """ミラー範囲を (dx, dy) 動かす。戻り値は「実際に動かせた量」。

        歯止めは _source_rect_allowed(拡縮と共通)。当たったら、当たった向きの成分だけ
        捨てて残りは動かす——縦に突き当たったからと横まで止まると、縁に沿って
        滑らせられずに使い勝手が悪い。

        大きさは変えない(変えるのは resize_source_to のほう)。枠とツールバーはドラッグ中は
        動かすだけで、作り直すのは離したとき(end_move)。"""
        if self._mirror is None:
            return (0, 0)
        try:
            base = QRect(self._mirror.source_rect)
            fits = self._source_rect_allowed

            for candidate in ((dx, dy), (dx, 0), (0, dy)):
                if not candidate[0] and not candidate[1]:
                    continue
                moved = base.translated(candidate[0], candidate[1])
                if fits(moved):
                    self._mirror.move_source_rect(moved)
                    # 枠とカンペは平行移動で追わせる。作り直すのは離したとき(end_move)
                    # ——置ける場所の答えは位置ごとに変わるが、掴んでいる間に窓を作り
                    # 直すとドラッグが切れる。
                    for window in (self._frame, self._notes):
                        if window is not None:
                            window.move(
                                window.x() + candidate[0], window.y() + candidate[1]
                            )
                    return candidate
            return (0, 0)
        except Exception:
            _guard("ミラー範囲の移動", notify=False)
            return (0, 0)

    def end_move(self) -> None:
        """ドラッグを離したところで、枠とツールバーを新しい範囲へ作り直す。

        動かしている間は平行移動で済ませているが、帯を範囲の上に置けるか・ツールバーが
        下に収まるかは位置ごとに決まる。答えが変わっているかもしれないので、最後に一度
        本来の手順(_show_frame)を通す。"""
        try:
            if self._mirror is None:
                return
            self._show_frame(self._mirror.source_rect)
        except Exception:
            _guard("ミラー範囲の移動の後始末", notify=False)

    # ---------------------------------------------------------------
    # ミラー範囲の拡縮(ツールバー右端の ◢ をドラッグ / ホイール)
    #
    # 決めたこと(理由は各所のコメントに分けて書いた):
    #   掴む場所 … ツールバー右端の ◢ (SIZE_MIN_WIDTH の上のコメント)
    #   比率    … 掴んだときの比率を保つ。Shift の間だけ自由 (_resized_rect)
    #   固定辺  … 左上。右下だけが動く (_resized_rect)
    # ---------------------------------------------------------------
    def begin_resize(self) -> bool:
        """掴んだ瞬間の範囲と比率を控える。以降の大きさはここからの絶対量で決める。

        差分の積み上げにしないのは、反映を間引く(SIZE_APPLY_INTERVAL_MS)ため。掴んだ
        点からの距離で決めていれば、何回か飛ばしても次の1回で正しい大きさに追いつく。
        移動のほうが差分を積んでいるのは、あちらが歯止めに当たった分を掴んだ点の側で
        差し引く作りだから(mouseMoveEvent)。こちらは歯止めに当たったら「その大きさに
        しない」だけで、掴んだ点は動かさない。"""
        try:
            if self._mirror is None:
                return False
            base = QRect(self._mirror.source_rect)
            if base.width() <= 0 or base.height() <= 0:
                return False
            self._size_settle_timer.stop()
            self._size_base = base
            self._size_ratio = base.width() / float(base.height())
            self._size_pending = None
            self._size_applied = 0.0
            return True
        except Exception:
            _guard("ミラー範囲の拡縮の開始", notify=False)
            return False

    def _resized_rect(self, dx: int, dy: int, free: bool) -> QRect:
        """掴んだ点から (dx, dy) 動いたときの、新しい範囲。

        左上を固定して右下だけを動かす。窓の大きさを変えるときの当たり前の作法である
        うえに、上端を動かさないことに実利がある——手元の枠は「範囲の上に帯を置けるか」
        で外形が決まっていて(SourceFrameWindow._fits_above)、上端が動かなければその答えは
        変わらない。掴んでいる間に帯が上下へ飛ぶと、枠もツールバーもまとめて跳ねる。

        比率は掴んだときのものを保つ。ミラー先のモニタは決まった比率なので、崩すと上下か
        左右に黒帯が増える(MirrorWindow.video_rect は引き伸ばさず余りを黒で残す)。
        「今出ている絵のまま大きさだけ変えたい」がこの機能の用なので、既定は保つ側へ
        倒す。おまけに、保っている限り拡大率が変わるだけなので、次のフレームが来るまでの
        間に前の絵を引き伸ばしても歪まない(resize_source_rect)。Shift の間だけ自由。

        保つときは、掴んだ点からの移動量を対角線((比率,1)の向き)へ落として1つの伸び量に
        する。「右へ引けば横に、下へ引けば縦に」が素直に混ざり、縮めるときも同じ式で効く
        (縦横の変化率の max を取る作りだと、斜めに引いたときに縮められなくなる)。"""
        base = self._size_base if self._size_base is not None else QRect(0, 0, 16, 9)
        ratio = self._size_ratio if self._size_ratio > 0 else 16.0 / 9.0
        if free:
            width = float(base.width() + dx)
            height = float(base.height() + dy)
        else:
            step = (dx * ratio + dy) / (ratio * ratio + 1.0)
            width = base.width() + step * ratio
            height = base.height() + step
        # 歯止め。小さすぎ(拡大しすぎて読めない)と大きすぎ(1枚の取得が間に合わない)を
        # 止め、デスクトップ全体より広い範囲も選ばせない(存在しない広さは撮れない)。
        bounds = desktop_bounds()
        max_width = min(SIZE_MAX_WIDTH, max(bounds.width(), SIZE_MIN_WIDTH))
        max_height = min(SIZE_MAX_HEIGHT, max(bounds.height(), SIZE_MIN_HEIGHT))
        if free:
            width = min(max(width, SIZE_MIN_WIDTH), max_width)
            height = min(max(height, SIZE_MIN_HEIGHT), max_height)
        else:
            if width < 1.0 or height < 1.0:
                # 掴んだ点より左上へ大きく戻された。0や負のままでは倍率が出せないが、
                # 縦横を別々に1へ丸めると比率が壊れる(そこを丸めた結果、下限が
                # 240x135 ではなく 240x240 の正方形になっていた)。比率だけ残した
                # 最小の形に置き換えてから、下の _fit_ratio で下限まで広げる。
                width, height = ratio, 1.0
            # 比率を保ったまま収める。片方だけ丸めると比率が崩れるので、倍率で寄せる。
            width, height = self._fit_ratio(width, height, max_width, max_height)
        return QRect(base.x(), base.y(), int(round(width)), int(round(height)))

    @staticmethod
    def _fit_ratio(width: float, height: float, max_width: float, max_height: float) -> tuple:
        """縦横の比を変えずに、上下限の中へ収めた (幅, 高さ)。

        先に上限へ縮め、次に下限へ広げる。逆にすると、下限へ広げた結果が上限を超える
        構成(モニタが極端に細長いとき)で上限を破る。"""
        shrink = min(max_width / width, max_height / height, 1.0)
        width *= shrink
        height *= shrink
        grow = max(SIZE_MIN_WIDTH / width, SIZE_MIN_HEIGHT / height, 1.0)
        return width * grow, height * grow

    def _size_hint(self, rect: QRect) -> str:
        """ツールバーの説明欄へ出す一言。今の大きさと、その大きさで出せそうなfps。

        推定fpsを添えるのは、大きくするほど重くなるのが選んでいる最中に分からないため。
        範囲選択の画面には既に同じ数字を出しているので(estimated_fps)、ここでも同じ
        ものを出す。手元の枠の帯には実測が出るが、あちらは1秒ごとの更新なので、
        掴んでいる間の手応えにはならない。"""
        limit = self._mirror.fps if self._mirror is not None else DEFAULT_FPS
        return (
            f"{rect.width()}x{rect.height()}　目安 "
            f"{estimated_fps(rect.width(), rect.height(), limit):.0f}fps"
        )

    def _apply_resize(self, rect: QRect) -> bool:
        """新しい範囲をミラーと手元の窓へ反映する。置けない大きさなら False。

        掴んでいる間は窓を作り直さない——作り直すと、掴んでいるツールバーごと消えて
        ドラッグが切れる(end_move と同じ話)。枠は形だけ合わせ直し(SourceFrameWindow.
        resize_to)、ツールバーとカンペは置き場所を計算し直して動かす。本来の手順を
        通すのは離したとき(end_resize)。

        ツールバーの置き場所が無くなる大きさは、そもそもその大きさにしない。掴んでいる
        間にツールバーを引っ込めると、掴んでいたものが消えてドラッグが切れる。カンペは
        引っ込めてよい(掴んでいない)が、窓は残す——閉じると読んでいた場所を失う。"""
        if self._mirror is None:
            return False
        if not self._source_rect_allowed(rect):
            return False

        # 枠があるときは、その外形がツールバーとカンペの置き場所の基準になる
        # (_show_frame と同じ順序)。まだ動かす前に、置けるかどうかだけ先に確かめる。
        anchor = QRect(rect)
        if self._frame is not None:
            anchor = self._frame.outer_rect(rect)

        toolbar_rect = QRect()
        if self._toolbar is not None:
            geometry = toolbar_geometry(anchor, rect)
            if geometry is None:
                return False
            toolbar_rect = QRect(geometry)

        notes_rect = None
        if self._notes is not None:
            cfg = mirror_config(self.app_settings)
            notes_rect = notes_geometry(
                anchor,
                rect,
                _as_int(cfg.get("notes_width"), DEFAULT_NOTES_WIDTH),
                avoid=(toolbar_rect, QRect(self._mirror.geometry())),
            )

        # ここから先は失敗しない。映す範囲を先に差し替えてから手元の窓を追わせる。
        self._mirror.resize_source_rect(rect)
        if self._frame is not None:
            self._frame.resize_to(rect)
        if self._toolbar is not None and toolbar_rect.topLeft() != self._toolbar.pos():
            self._toolbar.move(toolbar_rect.topLeft())
        if self._notes is not None:
            if notes_rect is None:
                # 置ける場所が無い大きさになった。カンペは発表者だけが見るものなので、
                # 撮影範囲に掛けるくらいなら引っ込める(notes_geometry と同じ判断)。
                if self._notes.isVisible():
                    self._notes.hide()
            else:
                if notes_rect != self._notes.geometry():
                    self._notes.setGeometry(notes_rect)
                if not self._notes.isVisible():
                    self._notes.show()
        return True

    def resize_source_to(self, dx: int, dy: int, free: bool = False) -> str:
        """掴んだ点から (dx, dy) 動いたときの大きさへ変える。戻り値は説明欄へ出す一言。"""
        if self._mirror is None or self._size_base is None:
            return ""
        try:
            now = time.perf_counter()
            if (now - self._size_applied) * 1000.0 < SIZE_APPLY_INTERVAL_MS:
                # 間引く。掴んだ点からの絶対量なので、取っておけば離すときに追いつく。
                self._size_pending = (dx, dy, free)
                return ""
            self._size_pending = None
            self._size_applied = now
            rect = self._resized_rect(dx, dy, free)
            if rect == self._mirror.source_rect:
                # 歯止めに当たっているか、まだ1px も動いていない。数字だけは出す。
                return self._size_hint(rect)
            if not self._apply_resize(rect):
                return ""
            return self._size_hint(rect)
        except Exception:
            _guard("ミラー範囲の拡縮", notify=False)
            return ""

    def resize_source_by_steps(self, steps: float, fine: bool = False) -> str:
        """ホイールで大きさを変える。1目盛りで幅を SIZE_WHEEL_STEP(細かくは
        SIZE_WHEEL_FINE_STEP)だけ動かし、比率は今の範囲のものを保つ。

        掴んでいる状態が無いので、そのつど今の範囲を基準にし直す。渡す (dx, dy) を
        (幅の伸び, 幅の伸び÷比率) にしてあるのは、_resized_rect が対角線へ落とす式を
        通しても幅がちょうど狙いどおりになるため(この向きに落とすと元に戻る)。"""
        try:
            if self._mirror is None or not steps:
                return ""
            base = QRect(self._mirror.source_rect)
            if base.width() <= 0 or base.height() <= 0:
                return ""
            self._size_base = base
            self._size_ratio = base.width() / float(base.height())
            self._size_pending = None
            step = (SIZE_WHEEL_FINE_STEP if fine else SIZE_WHEEL_STEP) * steps
            rect = self._resized_rect(
                int(round(step)), int(round(step / self._size_ratio)), False
            )
            if rect != self._mirror.source_rect and not self._apply_resize(rect):
                return ""
            # 手が止まったら本来の手順で作り直す。回している間は押し直され続ける。
            self._size_settle_timer.start(SIZE_SETTLE_DELAY_MS)
            return self._size_hint(rect)
        except Exception:
            _guard("ミラー範囲の拡縮", notify=False)
            return ""

    def end_resize(self) -> None:
        """離した(または手が止まった)ところで、枠・ツールバー・カンペを作り直す。

        先に、間引いて反映しそこねた最後の1回を当てる。掴んだ点からの絶対量なので、
        ここで一度当てれば離した瞬間の大きさになる。

        作り直すのは end_move と同じ理由。動かしている間は形を合わせ直すだけで済ませて
        いるが、ツールバーを上下どちらに置くか・カンペを左右どちらに置くかは大きさごとに
        決まるし、引っ込めたカンペを出し直すのもここ。"""
        try:
            self._size_settle_timer.stop()
            pending = self._size_pending
            self._size_pending = None
            if pending is not None and self._size_base is not None and self._mirror is not None:
                self._apply_resize(self._resized_rect(pending[0], pending[1], pending[2]))
            self._size_base = None
            if self._mirror is None:
                return
            self._show_frame(self._mirror.source_rect)
        except Exception:
            _guard("ミラー範囲の拡縮の後始末", notify=False)
