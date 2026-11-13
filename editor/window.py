# editor/window.py
# テキストエディタの窓。土台は QPlainTextEdit(editor/text_edit.py の
# TextEdit)、独立した QMainWindow。
#
# 第1段の範囲(仕様 docs/work-tools/spec.md 案B):
#   開く / 新規 / 上書き保存 / 名前を付けて保存(文字コードと改行は
#   開いたときのものを守り、変換は利用者が選んだときだけ)、
#   検索(次へ・前へ)、選択範囲を Copilot に投げる(copilot_io 経由)。
#
# 第2段の土台(同じく案B):
#   矩形選択は rect_edit の共用部品(text_edit 経由。こちらの矩形は
#   行を足せる)。マクロは「命令の列」の記録・再生(editor/macros.py)。
#   編集の操作は命令層(execute_command)を通し、キー割り当てと
#   マクロの再生が同じ命令を呼ぶ。F4 は直前の命令の繰り返し。
#   行の並べ替え・重複行の削除・選んだ範囲の変換(全角・半角と
#   大文字・小文字。editor/convert.py)も命令として載っている。
#
# 実機フィードバック(2026-11-13)で足したもの:
#   行番号、リボン風ツールバー、左ペイン(アウトラインのツリー＋下部の
#   ファイル情報)、フォントの既定(MS Gothic)と Ctrl+ホイールでの
#   大きさ変更、星印の行の強調表示、アウトライン・強調の正規表現の
#   設定ダイアログ、行末まで切り取り(Ctrl+K、命令 cut_to_end)。
#
# 第3段で足したもの:
#   正規表現の検索・置換(命令 replace_next / replace_all。実処理は
#   editor/rxfind.py が別プロセスで回す。固まる式でも窓は止まらない)、
#   フォルダ内の横断検索(grep。editor/grep.py と grep_dialog.py)、
#   文書内のすべて検索(ヒットした行の一覧から飛ぶ。editor/findall.py
#   と findall_dialog.py。検索バーの語と条件を引き継いで開く)、
#   不可視文字の表示・現在行の強調・太いカーソル・配色プリセット
#   (editor/colors.py。「秀丸風」あり)、外部で変更されたファイルの
#   監視(QFileSystemWatcher)と再読み込みの提案。
# 保存まわりの安全策(同じく第3段):
#   書き込みは原子的で、直前の内容は .bak に控える(document.py)。
#   開いた後に外部で変わっていたら、上書きの前に確認する。保存先の
#   文字コードで書けない文字があれば、化けを承知で書くか確認する。
#   改行が混在する文書は、開いたときに知らせる。混在していない文書
#   はその改行のまま保存し、混在した文書は明示的な保存(上書き保存・
#   名前を付けて保存)のときに3択(混在のまま／CRLF に統一／LF に
#   統一)で聞く(Nagato の決定、2026-11-13)。ダイアログ無しの書き
#   込み(save_to の直接呼び出し、grep して置換の一括書き込み、
#   セッションの控え)では聞かない。
# 表示の状態とフォントと正規表現は settings の editor 節に保存され、
# 呼び側が on_settings_change で受けて update_settings_section する。
#
# 最近使ったファイル(2026-11-13): 開いたファイルを editor 節の
# recent_files に新しい順で最大20件覚え、ファイルメニューの
# サブメニューから開き直せる。同じファイルは先頭へ繰り上げる。
# 無くなったファイルは、選んだときに静かに失敗して履歴から外す。
# 無題の新規文書は履歴に入れない(名前を付けて保存したら入る)。
#
# 編集マーク(2026-11-13): 開いて(または保存して)から編集した行は、
# 行番号の色が変わる(配色の line_number_edited_foreground)。
# マークの保持と追随は本文(text_edit)が行い、窓は開いた直後と
# 保存した直後に消して基準にする。次の編集行・前の編集行への
# 移動は命令(next_edited_line / prev_edited_line、既定は
# F2 / Ctrl+F2)で、端では反対側へ回る。
#
# ブックマーク(2026-11-13): 行に印を付けると、行番号のわきに ● が
# 出る(配色の line_number_bookmark_foreground)。印の保持と追随は
# 本文(text_edit)が編集マークと同じ作法で行い、窓は文書を開いた
# 直後と新規の直後だけ消す(保存では消えない。ファイルには保存
# しない、開いている間だけの印)。付け外し・次・前への移動は命令
# (toggle_bookmark / next_bookmark / prev_bookmark、既定は
# Ctrl+F7 / Ctrl+F8 / Ctrl+Shift+F8。F7 / F8 は秀丸に合わせて
# 切り取り・コピーが使う)、一覧は bookmark_dialog の窓(既定は
# Ctrl+Shift+B)から選んで飛ぶ。
#
# タブ＋メモ帳式セッション(2026-11-13): ファイルは窓の中のタブ
# として開く(ファイルごとに新しい窓は出さない)。文書ごとの表示と
# 状態は editor/document_view.py の DocumentView が持ち、窓は
# 「今のタブ」を _edit / _doc 等のプロパティで指す。編集した内容は
# 控え(ドラフト)を自動で取り続け(editor/session.py。置き場は
# %LOCALAPPDATA%\traytools\editor_session.json で、リポジトリの外)、
# ファイル自体は明示的に保存するまで書き換わらない。窓やタブを
# 閉じるとき「保存しますか」は出さず、次に開くと前回のタブ・
# 未保存の内容・カーソル位置がそのまま戻る(無題も同じ)。控えが
# 常に取れているので、未保存のタブは再起動の妨げに数えない
# (restart_loss_count)。先にあった間隔式の自動保存(間隔が来たら
# 実ファイルへ書く形)は、この方式に置き換えて取り除いた。
#
# 無題の保存では形式の画面を出さない(2026-11-13): 新規に作った
# 文書を保存するとき、文字コード・改行・BOM を選ぶ画面
# (SaveOptionsDialog)は出さず、ファイル名を付ける窓だけにする
# (煩わしいという Nagato の指摘)。形式は文書が持っている今の値
# (新規文書の既定 = UTF-8 / CRLF / BOM 無し)でそのまま書き、
# 変えたいときは保存後にステータスバーの文字コード表示から
# 変える。既に名前のある文書の「名前を付けて保存」では、従来
# どおり形式の画面を出す。
#
# キー割り当ての画面(2026-11-13): 命令と現在の割り当てを一覧し、
# その場でキーを押して変えられる(editor/keymap_dialog.py)。変更は
# editor 節の keymap(既定と違う項目だけの上書き分)へ差分で保存
# され、開いている窓のアクションへその場で付け直される。別の命令
# が使っているキーを付けたら、前の命令は割り当てなしになる
# (後勝ち。重なったままではショートカットがどちらも効かないため。
# 画面に知らせが出る)。
#
# リアルタイム検索(2026-11-13): 検索欄に打っている途中から、起点
# (打ち始めたときの本文カーソル位置)から一番近いヒットへその場で
# 飛ぶ。条件は検索バーの大小区別・正規表現をそのまま使う。起点は
# 直前のヒットが選択されている間は保たれるので、打ち進めても次々
# と先のヒットへずれていかない。Enter の次検索は、選ばれたヒット
# の後ろから続く。通常の文字列はその場で探し(str.find)、正規
# 表現は式の文法だけ先に確かめてから、少し間引いて(タイマー)
# 既存の非同期経路(_run_rx_async)で探す。ヒットが無ければ検索欄
# の背景が薄い赤になる(ステータスにも従来どおり出す)。
# 入力への反応は textEdited(人が打ったときだけ。setText では
# 来ない)なので、マクロ再生やプログラムからの設定では走らない。
# 正規表現まわりには、遅い式で固まらないための仕掛けが3つある:
#   各行は先頭200字だけ match する(outline.MATCH_HEAD_CHARS)、
#   設定ダイアログは OK のときに別プロセスで試験して遅い式を断る
#   (trial_patterns)、適用の前に「適用中」の印を書いて起動時に
#   残っていたら既定に戻す(pattern_applying、起動時の保険)。
#
# マクロの言語化・登録(2026-11-13): 記録したマクロは、1行1命令の
# テキスト形式で書き出し・読み込みできる(editor/macros.py の
# commands_to_text / commands_from_text。ファイル保存・読み込みは
# .txt でテキスト形式、.json で従来の JSON。読み込みは中身でも
# 見分ける)。名前を付けて登録したマクロは editor 節の macros に
# 残り、マクロメニューから実行できる。登録ごとにキーも1つ付け
# られ、組み込みの命令と重なるキーは効かない(組み込み優先。
# 管理画面では組み込みと重なるキーは付けられない)。管理画面
# (editor/macro_dialog.py)では、登録・名前の変更・削除・キー変更・
# テキストでの編集ができる。
#
# 拡張子別の設定(2026-11-13): 折り返し(方式と桁数)、Markdown の
# 色付け、フォント(名前と大きさ)、タブの幅を、ファイルの拡張子
# ごとに変えられる。持ち方は色の設定と同じ「全体+上書き」で、
# editor 節の extension_settings(拡張子 → 項目 → 値)に上書きだけを
# 持ち、上書きの無い項目は全体の設定に追随する(editor/
# ext_settings.py が対応表と解決、ext_settings_dialog.py が設定
# 画面)。適用はタブ(ビュー)ごとで、文書を読み込んだときと名前を
# 付けて保存したときに、その拡張子の実効値へ合わせ直す。無題は
# 全体に従う。全体のタブ幅は editor 節の tab_width(無いときは
# Qt の既定のまま触らない)。Ctrl+ホイールの大きさ変更は、効いて
# いる大きさの出どころ(その拡張子の上書きか全体か)へ書き戻す。
# タブキーでスペースを入れる類の扱いは無い(タブは常にタブ文字)。
#
# grep して置換(2026-11-13): フォルダ内の複数ファイルを対象に
# 置換する(editor/greplace.py と greplace_dialog.py)。検索メニュー
# と、grep の窓の「置換…」から開く。安全側の作り: まず対象ファイル
# と件数のプレビューを出し(書かない)、条件を変えたら実行は押せ
# なくなる。実行は確認のあとで、1ファイルごとにプレビュー時点の
# ディスクの印と照合してから書く(変わっていたものは飛ばす)。
# 開いているタブのファイルは対象から外す。書き込みは文書モデル
# 経由なので、文字コードと改行は保たれ、.bak が残る。書けない
# 文字コードのファイルはプレビューの時点で対象から外れる。
#
# 分割表示(2026-11-14): タブごとに、同じ文書を2ペインで同時に
# 眺めて編集できる(表示メニューの「左右に分割」「上下に分割」)。
# 2枚目は1枚目と QTextDocument を共有し、編集は即座にもう片方へ
# 反映される。カーソルとスクロールはペインごとに独立で、フォーカス
# のあるペインが「現在のペイン」として検索・保存・命令の対象になる
# (窓の _edit はビューの active_edit を指す)。編集マーク・ブック
# マーク・行ごとの改行種別の控えは、置き場(_TrackingState)を共有
# するので両ペインでそろう。追随の計算は文書に付いた1枚目だけが
# 行う。ペイン間の移動は命令 focus_next_pane(秀丸の F6 相当で、
# 既定も F6。キー割り当ての画面で変えられる)。分割の状態
# (向き・2枚目のカーソル・スプリッタの幅)はセッションの控えに残り、
# 次に開いたときも戻る。
#
# タスクバーの身元(2026-11-14): 窓は常駐・あっふ と同じプロセスで
# 開くので、そのままだとタスクバーで開いた側のアプリと同じボタン
# にまとめられてしまう。初回表示時(showEvent)に、窓へ EDDy 専用の
# AppUserModelID(TASKBAR_APP_ID)を書いて独立したボタンにする
# (Windows のみ。filer_shell.set_window_app_id 経由)。
#
# 落ちないための作法(このリポジトリの決まり):
#   - 外と触る入口(ファイル読み書き・送信)は例外を利用者への文にする。
#     スロットで投げ切るとプロセスごと終了する
#   - Copilot への送信は別スレッドで走らせ、結果は Signal 経由で窓に戻す
#     (ワーカースレッドから Qt を直接触らない。_AgentLoopBridge と同じ)。
#     UIA の実体は copilot_io の裏方プロセスが持つ(常駐の中で UIA を
#     動かすと pycaw と同居して即死するため。copilot_io 側の作法)
import json
import os
import re
import subprocess
import sys
import threading

from PySide6.QtCore import (
    QFileSystemWatcher,
    QObject,
    QSignalBlocker,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QKeySequence,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import copilot_io
import text_encoding
from editor import colors as colormod
from editor import convert as convmod
from editor import document as docmod
from editor import ext_settings as extmod
from editor import macro_py
from editor import macros as macmod
from editor import outline
from editor import editor_icon
from editor import reformat as refmtmod
from editor import rxfind
from editor import session as sessionmod
from editor.document_view import SPLIT_ORIENTATIONS, DocumentView
from editor.bookmark_dialog import BookmarkDialog
from editor.color_dialog import ColorSettingsDialog
from editor.ext_settings_dialog import ExtensionSettingsDialog
from editor.findall_dialog import FindAllDialog
from editor.grep_dialog import GrepDialog
from editor.greplace_dialog import GrepReplaceDialog
from editor.highlighter import MARKDOWN_EXTENSIONS, LineHighlighter
from editor import keymap as keymapmod
from editor.keymap_dialog import KeymapSettingsDialog
from editor.macro_dialog import MacroLibraryDialog
from editor.macro_py_dialog import MacroPythonDialog
from editor.outline_pane import OutlinePane
from editor.text_edit import TextEdit

# 利用者の目に触れる製品名(2026-11-13 に Nagato が命名)。内部の
# モジュール名(editor/ パッケージや settings の editor 節)は変えない。
APP_NAME = "EDDy"
# タスクバーで使う窓ごとのアプリID(AppUserModelID)。EDDy の窓は常駐や
# あっふ と同じプロセスで開くので、プロセスのID(traytools.app.1 /
# traytools.affx)のままだとタスクバーで同じアプリにまとめられてしまう。
# 窓にこのIDを書くと、別のアプリとして独立したボタンになる(Windows
# のみ。付け方は _apply_taskbar_identity)。
TASKBAR_APP_ID = "traytools.eddy"
ENCODING_ORDER = ["utf-8", "cp932", "utf-16-le", "utf-16-be", "euc_jp", "iso2022_jp"]
# フォントの既定。settings の editor 節(font_family / font_size)で
# 上書きできる。大きさは第1段までの既定(10pt)に合わせた。
DEFAULT_FONT_FAMILY = "MS Gothic"
DEFAULT_FONT_SIZE = 10
# アウトラインの再構築を編集に追随させるときのまとめ待ち(ms)。
OUTLINE_REBUILD_DELAY_MS = 300

# フォントの大きさの保存のまとめ待ち(ms)。Ctrl+ホイールの1ノッチ
# ごとに設定ファイルを書くと書き込みが嵐になるので、止まってから
# まとめて1回だけ書く。
FONT_SAVE_DELAY_MS = 600

# 正規表現の「適用中」の印に使う、settings の editor 節のキー。
# 式を適用する前に True を書き、正常に適用できたら False に戻す。
# 起動したときに True のまま残っていたら、前回は適用の途中で固まった
# 疑いがあるので、保存済みの式は使わず既定に戻して開く(起動時の保険)。
PATTERN_APPLYING_KEY = "pattern_applying"

# 最近使ったファイル(settings の editor 節 recent_files のキーと上限)。
# 開いた順ではなく「最近開いた順」で持ち、同じファイルは先頭へ
# 繰り上げる(remember_recent_file)。
RECENT_FILES_KEY = "recent_files"
RECENT_FILES_LIMIT = 20

# 控え(ドラフト)を書き出すまとめ待ち(ms)。打鍵やカーソル移動の
# たびにファイルへ書くと書き込みが嵐になるので、止まってから
# まとめて1回だけ書く(フォントの大きさの遅延保存と同じ作法)。
# 窓を閉じるときとアプリの終了時は、その場で書き切る。
SESSION_SAVE_DELAY_MS = 500
# リアルタイム検索で、正規表現のときに探し始めるまでの間引き(ms)。
# 打つたびに別プロセスを起こさないための待ちで、通常の文字列の
# ときは待たずにその場で探す。
INCREMENTAL_SEARCH_DELAY_MS = 120
# 検索の一致箇所すべての色付けの上限(件)。文書が大きく一致が
# 膨大でも固まらないよう、先頭からこの件数までだけ色を付ける。
SEARCH_HIGHLIGHT_LIMIT = 1000
# 編集中に色付けを付け直すまでの間引き(ms)。打つたびに数え直さ
# ないための待ち(セッションの控えの遅延と同じ考え方)。
HIGHLIGHT_REFRESH_DELAY_MS = 300

# キー割り当て(settings の editor 節 keymap のキー)。値は命令名 →
# キーの上書き分(既定と違う項目だけ。割り当てを外した命令は空文字列)
# で、実効の表は editor/keymap.py の merge_keymap で作る。
# キー割り当ての画面(editor/keymap_dialog.py)から変えると、その場で
# この窓のアクションへ付け直して、差分をこのキーで保存する。
KEYMAP_KEY = "keymap"

# 登録マクロ(settings の editor 節 macros のキー)。値は
# {名前: {"commands": [...], "key": "..."}} で、整えは
# editor/macros.py の sanitize_registered が行う。管理画面
# (editor/macro_dialog.py)から変えると、その場でマクロメニューへ
# 付け直して、差分をこのキーで保存する。
MACROS_KEY = "macros"

# 拡張子別の設定(settings の editor 節 extension_settings のキー)。
# 値は拡張子 → 項目 → 値の対応(全体と違う項目だけの上書き分)で、
# 実効の組は editor/ext_settings.py の resolve で作る。設定画面
# (editor/ext_settings_dialog.py)から変えると、その場で開いている
# タブへ付け直して、差分をこのキーで保存する。
EXTENSION_SETTINGS_KEY = "extension_settings"

# 全体のタブ幅(settings の editor 節 tab_width のキー)。値は半角の
# 文字数で、無いときは Qt の既定のまま触らない(None)。拡張子別の
# 上書きの受けにもなる(editor/ext_settings.py の tab_width)。


def sanitize_recent_files(value, limit=RECENT_FILES_LIMIT):
    """設定から読んだ履歴を整える(文字列だけ・重複なし・上限まで)。

    設定は手で書き換えられることもあるので、壊れた値が混ざって
    いても窓が開かなくならないよう、ここで黙って落とす。"""
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str) or not item:
            continue
        key = os.path.normcase(os.path.abspath(item))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def remember_recent_file(recent, path, limit=RECENT_FILES_LIMIT):
    """履歴の先頭に path を置いた新しい並びを返す。

    既に載っているファイルは重複させず、先頭へ繰り上げる。
    並びは最大で limit 件まで(古いものから落ちる)。"""
    target = os.path.abspath(path)
    key = os.path.normcase(target)
    kept = [item for item in sanitize_recent_files(recent)
            if os.path.normcase(os.path.abspath(item)) != key]
    return ([target] + kept)[:limit]


# 折り返しの方式(settings の editor 節 wrap_mode の値)。
#   column … 指定の桁数で折り返す / window … 窓の幅で折り返す /
#   none … 折り返さない。いずれも見た目だけで、文書は変えない。
WRAP_MODE_COLUMN = "column"
WRAP_MODE_WINDOW = "window"
WRAP_MODE_NONE = "none"
WRAP_MODES = (WRAP_MODE_COLUMN, WRAP_MODE_WINDOW, WRAP_MODE_NONE)
DEFAULT_WRAP_MODE = WRAP_MODE_WINDOW
DEFAULT_WRAP_COLUMN = 80
# 「桁数」の実体は QPlainTextEdit に無い(FixedColumnWidth は
# QTextEdit のもの)。本文の右に余白を取って見える幅を桁数ぶんに
# 狭める形で実現する(editor/text_edit.py の set_wrap_column)。

# 設定ダイアログの試験を別プロセスで待つ上限(秒)。試験自体の制限
# (outline.TRIAL_TIME_LIMIT_SEC)に、子プロセスの起動のぶんを足したもの。
# 上限を超えたら子プロセスごと止める(遅い式は呼び出し側では止められない)。
TRIAL_SUBPROCESS_TIMEOUT_SEC = outline.TRIAL_TIME_LIMIT_SEC + 2.0

# 試験の子プロセスで走らせるコード。標準入力の JSON
# ({"patterns": [...], "samples": [...]})を読み、各式を各試験文字列に
# match して、かかった秒数を標準出力へ1行で出す。Qt は読み込まない。
_TRIAL_CHILD_CODE = (
    "import json, re, sys, time\n"
    "data = json.load(sys.stdin)\n"
    "start = time.perf_counter()\n"
    "for pattern in data['patterns']:\n"
    "    rx = re.compile(pattern)\n"
    "    for sample in data['samples']:\n"
    "        rx.match(sample)\n"
    "sys.stdout.write(repr(time.perf_counter() - start))\n"
)


def trial_patterns(patterns, timeout=TRIAL_SUBPROCESS_TIMEOUT_SEC):
    """正規表現の並びが遅すぎないかを、別プロセスで試験する。

    戻り値は match にかかった秒数。時間切れ・起動失敗・応答が読めない
    ときは None(= 遅すぎるか、確かめられなかった。どちらも保存しない
    側に倒す)。遅い式は同じプロセスでは止められないので、子プロセス
    ごと打ち切れる形にしてある。"""
    payload = json.dumps({
        "patterns": [str(p) for p in patterns],
        "samples": outline.adversarial_samples(),
    })
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _TRIAL_CHILD_CODE],
            input=payload, capture_output=True, text=True,
            timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


class _SendBridge(QObject):
    """送信スレッド → 窓への結果の受け渡し(オブジェクトは使い回す)。"""

    finished = Signal(object)


class _RxBridge(QObject):
    """正規表現の処理スレッド → 窓への結果の受け渡し。

    rxfind.run_operation は別プロセスの完了を待つ(上限5秒)。GUI の
    スレッドで待つと、その間窓が固まる(レビュー req-20261113-107
    の指摘)。送信(_SendBridge)と同じく、裏スレッドで待って結果
    だけをシグナルで戻す。"""

    finished = Signal(object)


def dirty_window_state(windows):
    """エディタの窓の並びから、再起動・終了で失われる (件数,
    状態を読めなかった件数) を返す。

    再起動・終了の巻き添え判定(feature_screen.restart_blockers と
    filer.confirm_losses)が共用する。数えるのは「閉じたら失われる
    未保存の内容」で、窓が restart_loss_count を持てばそれを使う
    (EDDy は控えが常に取れているので、セッションが有効な窓は 0 を
    返す。未保存でも EDDy のために再起動を止めない)。持たない窓は
    is_dirty で代用する。読めない窓は「失われるものがあるかも」
    側に倒すため、落とさず件数として返す。"""
    dirty = 0
    unreadable = 0
    for window in windows or []:
        if window is None:
            continue
        try:
            counter = getattr(window, "restart_loss_count", None)
            if callable(counter):
                dirty += int(counter())
            elif window.is_dirty():
                dirty += 1
        except Exception:  # noqa: BLE001  読めない窓は「あるかも」側に倒す
            unreadable += 1
    return dirty, unreadable


def find_window_for_path(windows, path):
    """窓の並びから、path を開いている窓を探す。無ければ None。

    同じファイルを二重に開かないために、呼び側(ファイラ)が窓を
    作る前に使う。タブのどれかに開いていれば、その窓が当たる
    (open_paths を持つ窓)。パスの比べは、大文字小文字と表記の
    揺れを吸収するため normcase + abspath でそろえる。"""
    if not path:
        return None
    target = os.path.normcase(os.path.abspath(path))
    for window in windows or []:
        if window is None:
            continue
        try:
            if hasattr(window, "open_paths"):
                opened_paths = window.open_paths()
            else:
                opened_paths = [window.current_document().path]
        except Exception:  # noqa: BLE001  読めない窓は飛ばす
            continue
        for opened in opened_paths:
            if opened and os.path.normcase(
                    os.path.abspath(opened)) == target:
                return window
    return None


def _compile_or_default(text, default_rx, default_text):
    """正規表現の文字列をコンパイルする。不正なら (既定, 既定の文字列)。

    設定ファイルが手で書き換えられていても、窓が開かなくならないため。"""
    try:
        return re.compile(text), text
    except re.error:
        return default_rx, default_text


def _clipboard_text(text):
    """選択範囲の文字列を、クリップボードへ入れる形に直す。

    QTextCursor.selectedText() は段落の区切りを U+2029 で返すので、
    通常の改行 (LF) へ置き換える(通常のコピーがクリップボードへ
    入れる形に合わせる)。"""
    return text.replace("\u2029", "\n")


class SaveOptionsDialog(QDialog):
    """「名前を付けて保存」で、文字コード・改行・BOM を選ぶ小さな窓。

    既定は文書が持っている今の値。ここで変えたときだけ変換が起きる。
    出るのは、既に名前のある文書を別名で保存するときだけ。無題の
    文書の保存では出さない(形式は文書の既定でそのまま書く)。

    改行が混在した文書では、改行の選択肢の先頭に「混在のまま」が
    加わり、既定でそれが選ばれる(values() の改行は None になる。
    呼び側はその場合、行ごとの改行を保って書く)。"""

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.setWindowTitle("保存の形式")
        self._encoding = QComboBox()
        for enc in ENCODING_ORDER:
            self._encoding.addItem(
                text_encoding.ENCODING_LABELS.get(enc, enc), enc)
        self._encoding.setCurrentIndex(
            max(0, ENCODING_ORDER.index(doc.encoding))
            if doc.encoding in ENCODING_ORDER else 0)
        self._newline = QComboBox()
        if doc.mixed_newlines:
            # 混在した文書だけの選択肢。データは None(=改行を
            # 指定しない)で、呼び側が「混在のまま」と解釈する。
            self._newline.addItem("混在のまま", None)
        self._newline.addItem("CRLF", docmod.CRLF)
        self._newline.addItem("LF", docmod.LF)
        if doc.mixed_newlines:
            self._newline.setCurrentIndex(0)  # 混在のまま
        else:
            self._newline.setCurrentIndex(
                0 if doc.newline == docmod.CRLF else 1)
        self._bom = QCheckBox("BOM を付ける")
        self._bom.setChecked(doc.bom)
        self._encoding.currentIndexChanged.connect(self._sync_bom_enabled)
        self._sync_bom_enabled()

        form = QFormLayout(self)
        form.addRow("文字コード", self._encoding)
        form.addRow("改行", self._newline)
        form.addRow("", self._bom)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _sync_bom_enabled(self):
        supported = bool(text_encoding.bom_bytes(self._encoding.currentData()))
        self._bom.setEnabled(supported)
        if not supported:
            self._bom.setChecked(False)

    def values(self):
        """(文字コード, 改行, BOM を付けるか)。

        改行が混在した文書で「混在のまま」を選んだときだけ、改行は
        None になる(行ごとの改行を保って書く合図)。"""
        return (self._encoding.currentData(),
                self._newline.currentData(),
                self._bom.isChecked())


class PatternSettingsDialog(QDialog):
    """アウトラインとみなす行・強調表示する行の正規表現を編集する窓。

    不正な正規表現はエラーを出して受け付けない(保存しない)。文法が
    通っても、遅すぎる式(破滅的なバックトラックで窓が固まるもの)は
    試験してからでないと受け付けない。

    trial_runner は「式の並び → かかった秒数(確かめられなければ
    None)」の関数で、既定は別プロセスで試験する trial_patterns。
    テストでは差し替えられる。"""

    def __init__(self, outline_pattern, highlight_pattern, parent=None,
                 trial_runner=None):
        super().__init__(parent)
        self._trial_runner = (trial_runner if trial_runner is not None
                              else trial_patterns)
        self.setWindowTitle("アウトライン・強調の設定")
        self._outline_edit = QLineEdit(outline_pattern)
        self._highlight_edit = QLineEdit(highlight_pattern)
        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color: #c00000;")

        form = QFormLayout(self)
        form.addRow("アウトラインとみなす行の正規表現", self._outline_edit)
        form.addRow("強調表示する行の正規表現", self._highlight_edit)
        form.addRow(self._error_label)
        row = QHBoxLayout()
        self._reset_button = QPushButton("既定に戻す")
        self._reset_button.clicked.connect(self._reset_defaults)
        row.addWidget(self._reset_button)
        row.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        form.addRow(row)

    def _reset_defaults(self):
        self._outline_edit.setText(outline.DEFAULT_OUTLINE_PATTERN)
        self._highlight_edit.setText(outline.DEFAULT_HIGHLIGHT_PATTERN)
        self._error_label.clear()

    def validation_error(self):
        """不正があれば利用者向けの文を、無ければ None を返す。"""
        for label, value in (
                ("アウトライン", self._outline_edit.text()),
                ("強調表示", self._highlight_edit.text())):
            if not value.strip():
                return f"{label}の正規表現が空です"
            try:
                re.compile(value)
            except re.error as e:
                return f"{label}の正規表現が不正です（{e}）"
        return None

    def trial_error(self):
        """遅すぎる式なら利用者向けの文を、問題なければ None を返す。

        試験文字列は outline.adversarial_samples(意地の悪い並び)。
        試験全体で outline.TRIAL_TIME_LIMIT_SEC を超える式と、試験
        自体が確かめられなかった場合は、保存しない側に倒す。"""
        outline_text, highlight_text = self.values()
        try:
            seconds = self._trial_runner([outline_text, highlight_text])
        except Exception:  # noqa: BLE001  試験できないものは通さない
            seconds = None
        if seconds is None or seconds > outline.TRIAL_TIME_LIMIT_SEC:
            return ("正規表現の判定に時間がかかりすぎます"
                    "（窓が固まるおそれがあるため、保存しません）")
        return None

    def accept(self):  # noqa: N802  Qt の上書きではないが名前は Qt 側に合わせる
        error = self.validation_error()
        if error is None:
            error = self.trial_error()
        if error is not None:
            self._error_label.setText(error)
            return
        super().accept()

    def values(self):
        """(アウトラインの正規表現, 強調表示の正規表現)。"""
        return (self._outline_edit.text(), self._highlight_edit.text())


class _EdObject:
    """ed の受け物(属性に関数を動的に付けるだけの空の型)。"""


class _EditorPyApi:
    """Python マクロの API(ed)を、窓の命令層へつなぐ橋。

    名前の一覧と引数の形は editor/macro_py.py の API_SPEC と
    QUERY_NAMES が正本。ここは「ed の関数 → 窓の命令」の対応と、
    問い合わせ(本文・選択・カーソル位置の読み取り)だけを持つ。
    ファイルや OS へ触れる口は、API の側にもこちらにも無い。"""

    def __init__(self, window):
        self._window = window

    def api_object(self):
        """ed として渡すオブジェクト(属性が API の関数)。"""
        api = _EdObject()
        for name, params in macro_py.API_SPEC.items():
            setattr(api, name, self._make_handler(name, params))
        for query in macro_py.QUERY_NAMES:
            setattr(api, query, getattr(self, "_query_" + query))
        return api

    def _make_handler(self, name, params):
        window = self._window

        def call(*args, **kwargs):
            bound = {}
            for index, (param, _default) in enumerate(params):
                if index < len(args):
                    bound[param] = args[index]
            for key, value in kwargs.items():
                bound[key] = value
            return window.execute_command(name, bound, _note=False)

        return call

    # 問い合わせ(読み取りだけ)
    def _query_text(self):
        return self._window.text()

    def _query_selected_text(self):
        return self._window._edit.textCursor().selectedText()

    def _query_cursor_line(self):
        return self._window._edit.textCursor().blockNumber() + 1

    def _query_line_count(self):
        return self._window._edit.document().blockCount()


class EditorWindow(QMainWindow):
    """テキストエディタの窓。ファイルはタブとして開き、文書ごとの
    表示と状態は DocumentView(editor/document_view.py)が持つ。窓は
    「今のタブ」を _edit / _doc 等のプロパティで指す。閉じたら
    closed にジオメトリ([x, y, w, h])が流れる(ビュアと同じ形。
    呼び側が設定へ保存する)。

    editor_settings は settings の editor 節(辞書)。font_family /
    font_size / show_line_numbers / show_outline_pane / outline_pattern /
    highlight_pattern / pattern_applying(適用中の印。起動時の保険で
    読む)/ keymap(キー割り当ての上書き分。コンストラクタの
    keymap_overrides でも受ける)/ extension_settings(拡張子別の
    上書き分)/ tab_width(全体のタブ幅。無いときは Qt の既定)を
    読み(無いキーは既定)、表示やフォントや正規表現が変わったら、
    差分を on_settings_change(辞書を受ける関数)へ流すので、保存は
    呼び側の流儀(update_settings_section)に任せる。

    session_path は、タブと控えの置き場(editor/session.py の
    セッションファイル)。渡されたときだけ、前回のタブを戻し、
    編集中の控えを取り続ける。渡さなければ(テスト等)、タブは
    動くが控えの保存・復元はしない。"""

    closed = Signal(object)

    def __init__(self, path=None, *, geometry=None, keymap_overrides=None,
                 sender=None, font=None, editor_settings=None,
                 on_settings_change=None, session_path=None, parent=None):
        super().__init__(parent)
        # 窓のアイコンはテキストエディタ用(常駐の Rapture と区別する)。
        # 入口(feature_screen / ファイラ)はすべてこの窓を通るので、
        # ここで付ければ全部に付く。
        self.setWindowIcon(editor_icon.qicon())
        # タスクバーのアプリIDは、窓が画面に出たとき(showEvent)に
        # 1回だけ付ける。HWND が要るので作った直後には付けない。
        self._taskbar_identity_applied = False
        # タブとビュー。プロパティ(_edit / _doc 等)はこの並びの
        # 「今のタブ」を見るので、他の初期化より先に用意する。
        self._views = []
        self._tabs = None
        # セッション(タブと控え)。slot は session.claim で引き取った
        # 自分の窓ぶんの記録で、capture のたびに中身を差し替える。
        self._session_path = (str(session_path)
                              if session_path is not None else None)
        self._session_slot = None
        self._session_timer = QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.setInterval(SESSION_SAVE_DELAY_MS)
        self._session_timer.timeout.connect(self._flush_session)
        self._keymap = keymapmod.merge_keymap(keymap_overrides)
        self._keymap_overrides = keymapmod.sanitize_overrides(
            keymap_overrides)
        # キー割り当てが付いたアクションの控え(命令名 → アクションの
        # 並び)。_action が積み、キー割り当ての画面で変えたときに
        # _apply_keymap_to_actions が付け直す。検索バーの次・前への
        # ショートカットも同じく付け直すので、控えを持っておく。
        self._keymap_actions = {}
        self._find_next_shortcut = None
        self._find_prev_shortcut = None
        # 送信の実体は差し替えられる(テストは fake を入れる)。
        self._sender = sender if sender is not None else copilot_io.send_text
        self._send_thread = None
        self._bridge = _SendBridge(self)
        self._bridge.finished.connect(self._on_send_finished)
        # 正規表現の非同期実行(結果は世代で切り分ける)。世代と
        # 実行中の控えは文書ごと(ビュー)が持ち、受け渡しの橋だけ
        # 窓に置く(結果には、対象のビューも一緒に流す)。
        self._rx_bridge = _RxBridge(self)
        self._rx_bridge.finished.connect(self._on_rx_finished)
        # リアルタイム検索の間引きタイマー(正規表現のとき用)。
        # 起点と直前のヒットの控えは文書ごと(ビュー)が持つ。
        self._inc_timer = QTimer(self)
        self._inc_timer.setSingleShot(True)
        self._inc_timer.timeout.connect(self._do_incremental_search)
        # 検索の一致箇所の色付けの付け直し(編集で古い位置に残さ
        # ないため)。間引きして、止まってから付け直す。色を付け
        # ているビューも控える(タブを切り替えたら前の分を消す)。
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.setInterval(HIGHLIGHT_REFRESH_DELAY_MS)
        self._highlight_timer.timeout.connect(
            self._refresh_match_highlights)
        self._highlight_view = None
        # Copilot の入力欄に書きかけの文があるとき、上書きして送って
        # よいかの確認も差し替えられる(戻り値は bool)。
        self.ask_overwrite_input = None
        # 保存・外部変更まわりの確認も差し替えられる。
        # ask_external_overwrite: 開いた後に外部で変わったファイルを
        #   上書きしてよいか(戻り値は bool)
        # ask_external_reload: 外部で変わったファイルを再読み込み
        #   してよいか(戻り値は bool)
        # ask_unencodable_save: 保存先の文字コードで書けない文字が
        #   あるとき、置換文字で書いてよいか(引数は書けない文字の
        #   並び。戻り値は bool)
        # ask_mixed_newline: 改行が混在した文書を明示的に保存する
        #   とき、どう保存するか(戻り値は "keep"=混在のまま /
        #   "crlf"=CRLF に統一 / "lf"=LF に統一 / None=保存しない)
        self.ask_external_overwrite = None
        self.ask_external_reload = None
        self.ask_unencodable_save = None
        self.ask_mixed_newline = None
        # ask_session_save_failed: 閉じるときに控えが書けなかった
        #   場合の行き先(戻り値は "retry"=もう一度書く /
        #   "discard"=そのまま閉じる(未保存の編集は失われる) /
        #   "cancel"=閉じない)
        self.ask_session_save_failed = None
        self._pending_send_text = None
        self._on_settings_change = on_settings_change
        section = editor_settings if isinstance(editor_settings, dict) else {}
        # 最近使ったファイル(新しい順)。開くたびに先頭へ繰り上げて
        # _persist_settings で呼び側へ流す(保存は呼び側の流儀)。
        self._recent_files = sanitize_recent_files(
            section.get(RECENT_FILES_KEY))

        # マクロ(命令の列の記録・再生)と命令層の状態。文書の変化の
        # 信号が流れ始める前に用意する。
        self._recorder = macmod.MacroRecorder()
        # 記録中に起きた「記録できない操作」の件数(項目3の知らせ)。
        self._macro_skips = 0
        self._current_macro = []
        self._current_macro_name = ""
        # いまのマクロが Python のとき、そのソース(命令の列の
        # マクロでは None)。読み込み・登録マクロの再生で切り替わる。
        self._current_python_source = None
        self._python_block_cursor = None
        # 登録マクロ(名前 → {"commands", "key"})。メニューの組み立て
        # (_build_menus)でアクションにし、変更のたびに付け直す。
        self._registered_macros = macmod.sanitize_registered(
            section.get(MACROS_KEY))
        self._registered_macro_actions = {}
        self._registered_menu_items = []
        self._macro_menu = None
        self._in_command = False      # 命令の実行中(変化を二重に数えない)
        self._replaying = False       # マクロの再生中
        # 元に戻す・やり直しの最中かと、変化の直前の (選択の開始,
        # 選択の終了, カーソル位置) は、文書ごと(ビュー)が持つ
        # (suppress_change_record / change_cache)。
        self._command_impls = {
            "insert_text": self._cmd_insert_text,
            "delete_backward": self._cmd_delete_backward,
            "delete_forward": self._cmd_delete_forward,
            "delete_selection": self._cmd_delete_selection,
            "cut_to_end": self._cmd_cut_to_end,
            "delete_line": self._cmd_delete_line,
            "find_next": self._cmd_find_next,
            "find_prev": self._cmd_find_prev,
            "move_line_start": self._cmd_move_line_start,
            "move_line_end": self._cmd_move_line_end,
            "goto_line": self._cmd_goto_line,
            "next_edited_line": self._cmd_next_edited_line,
            "prev_edited_line": self._cmd_prev_edited_line,
            "toggle_bookmark": self._cmd_toggle_bookmark,
            "next_bookmark": self._cmd_next_bookmark,
            "prev_bookmark": self._cmd_prev_bookmark,
            "move_line_up": self._cmd_move_line_up,
            "move_line_down": self._cmd_move_line_down,
            "move_char_left": self._cmd_move_char_left,
            "move_char_right": self._cmd_move_char_right,
            "move_word_left": self._cmd_move_word_left,
            "move_word_right": self._cmd_move_word_right,
            "move_document_start": self._cmd_move_document_start,
            "move_document_end": self._cmd_move_document_end,
            "move_page_up": self._cmd_move_page_up,
            "move_page_down": self._cmd_move_page_down,
            "select_all": self._cmd_select_all,
            "sort_lines": self._cmd_sort_lines,
            "remove_duplicate_lines": self._cmd_remove_duplicate_lines,
            "reformat_text": self._cmd_reformat_text,
            "convert_halfwidth": self._cmd_convert_halfwidth,
            "convert_fullwidth": self._cmd_convert_fullwidth,
            "convert_lower": self._cmd_convert_lower,
            "convert_upper": self._cmd_convert_upper,
            "replace_next": self._cmd_replace_next,
            "replace_all": self._cmd_replace_all,
        }

        # 本文のフォントと、ビューごとに効かせる表示の希望。実際の
        # 適用はビューを作るとき(_add_view)に行う。
        if font is not None:
            self._edit_font = font
        else:
            family = section.get("font_family") or DEFAULT_FONT_FAMILY
            size = section.get("font_size")
            if not isinstance(size, int) or size <= 0:
                size = DEFAULT_FONT_SIZE
            self._edit_font = QFont(str(family), size)
        self._line_numbers_pref = bool(section.get("show_line_numbers", True))

        # アウトライン・強調の正規表現(実効値)。設定が空なら既定、
        # 不正なら既定に倒す(窓が開かなくならないため)。
        # 起動時の保険: 前回の適用で「適用中」の印(pattern_applying)が
        # 残っていたら、前回は適用の途中で固まった疑いがある。保存済み
        # の式は使わず既定に戻して開く(設定への書き戻しは、印を立てる
        # ときに一緒に流す)。
        self._pattern_reset_updates = None
        if section.get(PATTERN_APPLYING_KEY):
            outline_text = ""
            highlight_text = ""
            self._pattern_reset_updates = {
                "outline_pattern": "",
                "highlight_pattern": "",
            }
        else:
            outline_text = section.get("outline_pattern") or ""
            highlight_text = section.get("highlight_pattern") or ""
        outline_rx, self._outline_pattern_text = _compile_or_default(
            outline_text or outline.DEFAULT_OUTLINE_PATTERN,
            outline.DEFAULT_OUTLINE_RX, outline.DEFAULT_OUTLINE_PATTERN)
        self._outline_rx = outline_rx
        highlight_rx, self._highlight_pattern_text = _compile_or_default(
            highlight_text or outline.DEFAULT_HIGHLIGHT_PATTERN,
            outline.DEFAULT_HIGHLIGHT_RX, outline.DEFAULT_HIGHLIGHT_PATTERN)
        # 強調表示は文書ごと(ビュー)が持つ。式の実効値だけ窓に置き、
        # ビューを作るときにその文書のハイライタへ渡す。
        self._highlight_rx = highlight_rx
        self._pattern_guard_armed = False

        # 表示の設定(第3段): 不可視文字・現在行の強調・太いカーソル・
        # 配色。ここでは設定を読むだけ(保存は切り替えのときに流す)。
        # 適用はビューごと(_add_view)。
        self._invisibles_pref = bool(section.get("show_invisibles"))
        self._current_line_pref = bool(
            section.get("highlight_current_line", True))
        self._wide_cursor_pref = bool(section.get("wide_cursor"))
        # 配色は「プリセット+上書き」(editor/colors.py)。プリセット名の
        # 正本は color_preset で、旧キー color_scheme は移行前の設定を
        # 読むための受け(書き込みは color_preset のみ)。
        scheme_name = (section.get("color_preset")
                       or section.get("color_scheme")
                       or colormod.DEFAULT_SCHEME)
        if scheme_name not in colormod.COLOR_SCHEMES:
            scheme_name = colormod.DEFAULT_SCHEME
        self._color_scheme_name = scheme_name
        self._color_overrides = colormod.sanitize_overrides(
            section.get("color_overrides"))

        # 折り返し(方式と桁数)と、Markdown の色付けの希望。適用は
        # ビューごと(保存は切り替えのときに流す)。Markdown が実際に
        # 効くかは、そのタブのファイルの拡張子で決まる
        # (_update_markdown_highlight)。
        wrap_mode = section.get("wrap_mode")
        self._wrap_mode = (wrap_mode if wrap_mode in WRAP_MODES
                           else DEFAULT_WRAP_MODE)
        wrap_column = section.get("wrap_column")
        self._wrap_column = (wrap_column if isinstance(wrap_column, int)
                             and wrap_column > 0 else DEFAULT_WRAP_COLUMN)
        self._markdown_pref = bool(section.get("markdown_highlight", True))
        self._outline_pane_pref = bool(section.get("show_outline_pane", True))

        # 拡張子別の設定(全体+上書き。editor/ext_settings.py)。
        # 全体のフォント(名前と大きさ)は、上の _edit_font を正本に
        # して控える(Ctrl+ホイールの書き戻し先の判定に使う)。
        self._font_family = self._edit_font.family()
        base_size = self._edit_font.pointSize()
        self._font_size_base = (base_size if base_size > 0
                                else DEFAULT_FONT_SIZE)
        self._tab_width = extmod.sanitize_tab_width(
            section.get("tab_width"))
        self._ext_settings = extmod.sanitize(
            section.get(EXTENSION_SETTINGS_KEY))

        # フォントの大きさの遅延保存(Ctrl+ホイールの嵐対策)。
        # _pending_font_persist は、たまっている保存の差分そのもの
        # (全体の大きさなら {"font_size": n}、拡張子の上書きなら
        # {"extension_settings": {...}})。
        self._pending_font_size = None
        self._pending_font_persist = None
        self._font_save_timer = QTimer(self)
        self._font_save_timer.setSingleShot(True)
        self._font_save_timer.setInterval(FONT_SAVE_DELAY_MS)
        self._font_save_timer.timeout.connect(self._flush_pending_font_size)

        # タブと最初のビュー(無題)。外部変更の監視・正規表現の状態・
        # マクロ記録の控え等の文書ごとの状態は、ビューが持ち、
        # 信号の接続もビューごと(_connect_view)。
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.tabCloseRequested.connect(self.close_tab)
        self._tabs.currentChanged.connect(self._on_current_tab_changed)
        self._add_view()
        # Ctrl+Tab / Ctrl+Shift+Tab でタブを切り替える。
        self._next_tab_shortcut = QShortcut(
            QKeySequence("Ctrl+Tab"), self)
        self._next_tab_shortcut.setContext(
            Qt.ShortcutContext.WindowShortcut)
        self._next_tab_shortcut.activated.connect(self.activate_next_tab)
        self._prev_tab_shortcut = QShortcut(
            QKeySequence("Ctrl+Shift+Tab"), self)
        self._prev_tab_shortcut.setContext(
            Qt.ShortcutContext.WindowShortcut)
        self._prev_tab_shortcut.activated.connect(self.activate_prev_tab)

        self._find_bar = self._build_find_bar()
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._tabs)
        layout.addWidget(self._find_bar)
        self.setCentralWidget(central)
        self._find_bar.hide()

        self._build_menus()
        self._build_toolbar()
        self._enc_label = QLabel()
        self._pos_label = QLabel()
        self.statusBar().addPermanentWidget(self._enc_label)
        self.statusBar().addPermanentWidget(self._pos_label)
        if geometry and len(geometry) == 4:
            self.setGeometry(*geometry)
        else:
            self.resize(760, 560)
        # ここから先で式が実際に走る(アウトライン抽出と強調の適用)。
        # 適用の前に「適用中」の印を立て、無事に開けたら消す。途中で
        # 固まれば印が残り、次回の起動で既定に戻る(起動時の保険)。
        self._arm_pattern_guard()
        self._refresh_all()
        # セッションがあれば、前回のタブと控えを戻す。そのうえで
        # 起動時に渡されたファイルを開く(戻したタブにあれば、
        # そのタブを前に出すだけ)。
        restored = self._restore_session()
        if path:
            self.open_path(path)
        elif restored:
            self._refresh_all()
        self._disarm_pattern_guard()
        if self._session_path is not None:
            # 終了時(再起動含む)にも控えを書き切る。aboutToQuit は
            # 窓の closeEvent を経ずに落ちる経路の受け。
            app = QGuiApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._flush_session)
        # キーの重なりがあれば、起動時にステータスバーで知らせる
        # (黙ってどちらかを落とさない。付け方は _shortcut_keys_for
        # の先勝ちで、表そのものは変えない)。
        conflicts = self.keymap_conflicts()
        if conflicts:
            detail = "、".join(
                f"『{keys}』（{'・'.join(
                    keymapmod.command_label(n) for n in names)}）"
                for keys, names in conflicts.items())
            self.statusBar().showMessage(
                f"キー割り当てに重なりがあります：{detail}。"
                "キー割り当ての設定で直せます。", 15000)
        # 開いた直後は本文にフォーカスを置く(ショートカットは本文に
        # フォーカスがあるときだけ効く作りなので)。
        self._edit.setFocus()

    # ---------------------------------------------------------------
    # ビュー(タブ)とセッション
    # ---------------------------------------------------------------
    # 文書ごとの状態は DocumentView が持つ。窓の既存の処理は
    # 「今のタブ」に対して書かれているので、_edit / _doc 等は
    # プロパティで今のビューへ寄せる。
    def _current_view(self):
        """今開いているタブのビュー。タブが無ければ None。"""
        if self._tabs is None or not self._views:
            return None
        index = self._tabs.currentIndex()
        if 0 <= index < len(self._views):
            return self._views[index]
        return self._views[-1]

    @property
    def _edit(self):
        # 今のタブの「現在のペイン」。分割していなければ1枚目で、
        # 検索・保存・命令の対象はフォーカスのあるペインになる。
        return self._current_view().active_edit()

    @_edit.setter
    def _edit(self, value):
        self._current_view().edit = value

    @property
    def _doc(self):
        return self._current_view().doc

    @_doc.setter
    def _doc(self, value):
        self._current_view().doc = value

    @property
    def _highlighter(self):
        return self._current_view().highlighter

    @_highlighter.setter
    def _highlighter(self, value):
        self._current_view().highlighter = value

    @property
    def _outline_pane(self):
        return self._current_view().outline_pane

    @property
    def _rx_pending(self):
        return self._current_view().rx_pending

    @_rx_pending.setter
    def _rx_pending(self, value):
        self._current_view().rx_pending = value

    @property
    def _rx_generation(self):
        return self._current_view().rx_generation

    @_rx_generation.setter
    def _rx_generation(self, value):
        self._current_view().rx_generation = value

    @property
    def _inc_base(self):
        return self._current_view().inc_base

    @_inc_base.setter
    def _inc_base(self, value):
        self._current_view().inc_base = value

    @property
    def _inc_span(self):
        return self._current_view().inc_span

    @_inc_span.setter
    def _inc_span(self, value):
        self._current_view().inc_span = value

    @property
    def _suppress_change_record(self):
        return self._current_view().suppress_change_record

    @_suppress_change_record.setter
    def _suppress_change_record(self, value):
        self._current_view().suppress_change_record = value

    @property
    def _external_change_pending(self):
        return self._current_view().external_change_pending

    @_external_change_pending.setter
    def _external_change_pending(self, value):
        self._current_view().external_change_pending = value

    def _add_view(self):
        """新しいタブ(無題のビュー)を足して返す。

        表示の設定(フォント・行番号・配色・折り返し等)は窓の設定値
        でそろえ、文書ごとの信号を窓へつなぐ。"""
        view = DocumentView(self._tabs)
        self._apply_font_to_view(view)
        if not self._line_numbers_pref:
            view.edit.set_line_numbers_visible(False)
        view.edit.set_show_invisibles(self._invisibles_pref)
        view.edit.set_current_line_enabled(self._current_line_pref)
        view.edit.setCursorWidth(3 if self._wide_cursor_pref else 1)
        view.highlighter = LineHighlighter(
            view.edit.document(), self._highlight_rx)
        view.outline_pane.setVisible(self._outline_pane_pref)
        view.outline_timer.setInterval(OUTLINE_REBUILD_DELAY_MS)
        self._apply_color_scheme_to_view(view)
        self._apply_wrap_to_view(view)
        self._update_markdown_highlight(view)
        self._connect_view(view)
        # キー割り当ての付いたアクションは、本文にフォーカスがある
        # ときだけ効く作りなので、新しい本文にも登録する
        # (メニューより前に作った最初のビューは _action 側で登録
        # される。_keymap_actions が空の間はここでは何もしない)。
        for actions in self._keymap_actions.values():
            for action in actions:
                view.edit.addAction(action)
        # 登録マクロのアクションも同じ作法(本文にフォーカスがある
        # ときだけショートカットが効く)。
        for action in self._registered_macro_actions.values():
            view.edit.addAction(action)
        self._views.append(view)
        self._tabs.addTab(view, "")
        self._update_tab_label(view)
        return view

    def _connect_view(self, view):
        """ビューの文書ごとの信号を、窓の処理へつなぐ。"""
        document = view.edit.document()
        document.modificationChanged.connect(
            lambda _modified, v=view: self._on_view_modified(v))
        document.contentsChanged.connect(
            lambda v=view: self._on_view_contents_changed(v))
        document.contentsChange.connect(
            lambda pos, removed, added, v=view:
            self._on_document_change_for_macro(v, pos, removed, added))
        self._connect_edit(view, view.edit)
        view.watcher.fileChanged.connect(
            lambda path, v=view: self._on_watched_file_changed(v, path))
        view.outline_timer.timeout.connect(
            lambda v=view: self._rebuild_outline(v))
        view.outline_pane.jump_requested.connect(
            lambda line, v=view: self._jump_to_line(line, v))

    def _connect_edit(self, view, edit):
        """本文1枚ぶんの信号を、窓の処理へつなぐ。

        文書の信号は _connect_view が文書につなぐ(分割の2枚目と
        文書を共有しても、文書の信号は1回だけつながる)。こちらは
        ペインごとの信号だけ: カーソル・選択・フォントの大きさ・
        マクロ記録のためのキー通知。分割の2枚目にも同じようにつなぐ
        (_split_view)。"""
        edit.cursorPositionChanged.connect(
            lambda v=view: self._on_view_cursor_moved(v))
        edit.selectionChanged.connect(
            lambda v=view: self._on_view_selection_changed(v))
        edit.font_size_changed.connect(
            lambda size, v=view: self._on_font_size_changed(size, v))
        edit.move_key.connect(self._on_move_key)
        edit.command_key.connect(self._on_command_key)
        edit.unrecorded_move.connect(self._note_macro_skip)
        edit.mouse_pressed.connect(self._note_macro_skip)
        edit.undo_redo_started.connect(
            lambda v=view: setattr(v, "suppress_change_record", True))
        edit.undo_redo_finished.connect(
            lambda v=view: setattr(v, "suppress_change_record", False))

    def _on_view_modified(self, view):
        self._update_tab_label(view)
        if view is self._current_view():
            self._refresh_title()
        self._schedule_session_save()

    def _on_view_cursor_moved(self, view):
        view.track_change_cache()
        if view is self._current_view() and hasattr(self, "_pos_label"):
            self._refresh_position()
        self._schedule_session_save()

    def _on_view_selection_changed(self, view):
        # 選択だけが変わったときも、マクロ記録の材料(直前の選択)を
        # 取り直す。カーソル位置が動かない全選択(selectAll で末尾に
        # いた場合など)では cursorPositionChanged が出ないため、
        # ここで取り直さないと古い選択で切り分けてしまう。
        view.track_change_cache()
        if view is self._current_view() and hasattr(self, "_pos_label"):
            self._refresh_position()

    def _on_view_contents_changed(self, view):
        view.outline_timer.start()
        self._refresh_pane_counts(view)
        self._schedule_session_save()
        # 検索の色付けは、編集で位置がずれる。検索バーが出ている
        # 間は、少し待ってから付け直す(古い位置に残さない)。
        if view is self._current_view() \
                and getattr(self, "_find_bar", None) is not None \
                and self._find_bar.isVisible() \
                and self._find_edit.text():
            self._highlight_timer.start()

    def _update_tab_label(self, view):
        """タブの見出しを、文書の名前(未保存なら *)に合わせる。"""
        try:
            index = self._views.index(view)
        except ValueError:
            return
        name = view.doc.display_name
        if view.is_modified():
            name += " *"
        self._tabs.setTabText(index, name)

    def _activate_view(self, view):
        try:
            index = self._views.index(view)
        except ValueError:
            return
        self._tabs.setCurrentIndex(index)

    def activate_next_tab(self):
        """次のタブへ(Ctrl+Tab)。端では反対側へ回る。"""
        if len(self._views) > 1:
            index = (self._tabs.currentIndex() + 1) % len(self._views)
            self._tabs.setCurrentIndex(index)

    def activate_prev_tab(self):
        """前のタブへ(Ctrl+Shift+Tab)。端では反対側へ回る。"""
        if len(self._views) > 1:
            index = (self._tabs.currentIndex() - 1) % len(self._views)
            self._tabs.setCurrentIndex(index)

    def _on_current_tab_changed(self, _index):
        view = self._current_view()
        if view is None or not hasattr(self, "_enc_label"):
            return  # 組み立ての途中(タブだけ先にできる間)は何もしない
        self._reset_incremental_search()
        self._refresh_all()
        # 検索の色付けは今のタブだけに付ける。切り替えたら、
        # 前のタブの分を消して今のタブへ付け直す。
        if hasattr(self, "_find_bar"):
            self._refresh_match_highlights()
        if view.external_change_pending:
            # 裏のタブで起きた外部変更は、前に出たときに決着させる。
            self._handle_external_change(view)

    def _remove_view(self, view):
        """ビューをタブから外す。タブが無くなったら無題を1つ足す。"""
        watched = view.watcher.files()
        if watched:
            view.watcher.removePaths(watched)
        view.watched_path = None
        try:
            index = self._views.index(view)
        except ValueError:
            return
        self._views.pop(index)
        self._tabs.removeTab(index)
        view.deleteLater()
        if not self._views:
            self._add_view()

    def close_tab(self, index=None):
        """タブを閉じる。確認は出さない(控えが書ける限り)。

        閉じたタブはセッションからも外れる(次に開いても戻らない。
        メモ帳と同じ)。窓を閉じた場合は、開いているタブが全部
        そのまま戻る。最後のタブを閉じたら、新しい無題のタブに
        置き換わる(窓は閉じない)。

        ただし、そのタブに未保存の編集があり、控えが書けないとき
        は、窓を閉じるときと同じ3択で聞く(もう一度書く／そのまま
        閉じる／閉じない。既定は閉じない)。黙って閉じると、タブが
        控えからも外れて編集がどこにも残らないため。未保存の編集
        が無いタブは、控えが書けなくても聞かずに閉じる。"""
        if index is None:
            index = self._tabs.currentIndex()
        if not 0 <= index < len(self._views):
            return
        view = self._views[index]
        if view.is_modified() and not self._flush_session():
            if not self._confirm_tab_close_without_session():
                return
        self._remove_view(view)
        self._flush_session()

    def _confirm_tab_close_without_session(self) -> bool:
        """控えが書けないままタブを閉じてよいか、利用者と決める。

        閉じてよければ True。窓を閉じるときの
        _confirm_close_without_session と同じ3択で、違いは
        「閉じない」がイベントでなく戻り値で伝わることだけ。"""
        while True:
            choice = self._ask_session_save_failed_choice()
            if choice == "retry":
                if self._flush_session():
                    return True
                continue
            return choice == "discard"

    def close_current_tab_or_window(self):
        """「閉じる」(Ctrl+W)。タブが複数あれば今のタブを閉じ、
        1つだけなら窓を閉じる(タブが無かった頃の動きと同じ)。"""
        if len(self._views) > 1:
            self.close_tab(self._tabs.currentIndex())
        else:
            self.close()

    # ---------------------------------------------------------------
    # 分割表示(同じ文書を2ペインで見る)
    # ---------------------------------------------------------------
    def _split_view(self, view, orientation_name):
        """そのタブを分割表示にする。2枚目の本文を返す。

        orientation_name は "horizontal"(左右) / "vertical"(上下)。
        既に分割していれば向きだけ変わる。新しい2枚目には、1枚目と
        そろえた表示(フォント・配色・折り返し等)と信号の接続と
        アクションの登録を行う(セッションからの復元でも使う)。"""
        orientation = SPLIT_ORIENTATIONS.get(orientation_name)
        if orientation is None or view is None:
            return None
        already = view.split_edit is not None
        second = view.split(orientation)
        if not already:
            self._setup_pane(view, second)
        return second

    def _setup_pane(self, view, edit):
        """新しく足したペイン(分割の2枚目)を、1枚目とそろえる。

        表示の適用はビュー単位の口(_apply_font_to_view 等)が両
        ペインへ行き渡るので、それを呼び直すのが本体。こちらの
        ペイン固有の分は、行番号などの希望と、信号の接続と、
        ショートカット付きアクションの登録。"""
        self._apply_font_to_view(view)
        if not self._line_numbers_pref:
            edit.set_line_numbers_visible(False)
        edit.set_show_invisibles(self._invisibles_pref)
        edit.set_current_line_enabled(self._current_line_pref)
        edit.setCursorWidth(3 if self._wide_cursor_pref else 1)
        self._apply_color_scheme_to_view(view)
        self._apply_wrap_to_view(view)
        self._connect_edit(view, edit)
        for actions in self._keymap_actions.values():
            for action in actions:
                edit.addAction(action)
        for action in self._registered_macro_actions.values():
            edit.addAction(action)

    def split_current_view(self, orientation_name):
        """今のタブを分割する(メニュー・キー割り当ての受け口)。"""
        view = self._current_view()
        if view is None:
            return
        second = self._split_view(view, orientation_name)
        if second is not None:
            second.setFocus()
            self._schedule_session_save()
            # 新しくできたペインにも、検索の色付けを付ける。
            self._refresh_match_highlights()

    def unsplit_current_view(self):
        """今のタブの分割をやめる(メニュー・キー割り当ての受け口)。"""
        view = self._current_view()
        if view is None or view.split_edit is None:
            return
        focused_second = view.active_edit() is view.split_edit
        view.unsplit()
        if focused_second:
            view.edit.setFocus()
        self._schedule_session_save()

    def is_split(self, view=None) -> bool:
        """そのタブ(既定は今のタブ)が分割表示かどうか。"""
        if view is None:
            view = self._current_view()
        return view is not None and view.split_edit is not None

    def focus_next_pane(self):
        """もう片方のペインへフォーカスを移す(秀丸の F6 相当)。

        分割していないタブでは何もしない。キー割り当ての命令
        focus_next_pane として用意してあり、既定は F6(キー割り
        当ての画面で変えられる)。"""
        view = self._current_view()
        if view is None or view.split_edit is None:
            return
        target = (view.edit if view.active_edit() is view.split_edit
                  else view.split_edit)
        target.setFocus()

    @staticmethod
    def _same_path(a, b):
        return (a is not None and b is not None
                and os.path.normcase(os.path.abspath(a))
                == os.path.normcase(os.path.abspath(b)))

    def find_view_for_path(self, path):
        """path を開いているタブ(ビュー)を探す。無ければ None。"""
        if not path:
            return None
        for view in self._views:
            if self._same_path(view.doc.path, path):
                return view
        return None

    def _is_pristine(self, view):
        """手付かずの無題か(開くときに置き換えてよいタブか)。"""
        return (view.doc.path is None and not view.is_modified()
                and view.edit.toPlainText() == "")

    # ---------------------------------------------------------------
    # セッション(タブと控えの保存・復元)
    # ---------------------------------------------------------------
    def _restore_session(self) -> bool:
        """セッションがあれば、前回のタブと控えを戻す。戻したら True。

        戻せなかったタブ(変更の無いタブで、ファイルが消えている
        もの)は静かに落とす。控えのあるタブは、ファイルが消えて
        いても内容を戻す(本文が失われる経路を作らない)。"""
        if self._session_path is None:
            return False
        try:
            self._session_slot = sessionmod.claim(self._session_path)
        except Exception:  # noqa: BLE001  控えが読めなくても窓は開く
            self._session_slot = None
            return False
        self._notify_corrupt_session()
        entries = list((self._session_slot or {}).get("tabs") or [])
        if not entries:
            return False
        initial = self._current_view()
        restored = []
        for entry in entries:
            view = self._add_view()
            if self._restore_view(view, entry):
                restored.append(view)
            else:
                self._remove_view(view)
        if not restored:
            return False
        if initial is not None and self._is_pristine(initial):
            self._remove_view(initial)
        active = (self._session_slot or {}).get("active") or 0
        try:
            active = int(active)
        except (TypeError, ValueError):
            active = 0
        active = max(0, min(active, len(restored) - 1))
        self._activate_view(restored[active])
        # 戻した顔ぶれで控えを取り直す(落としたタブを記録から消す)。
        self._flush_session()
        return True

    def _restore_view(self, view, entry) -> bool:
        """セッションの記録1件をビューへ戻す。戻せたら True。"""
        path = entry.get("path")
        dirty = bool(entry.get("dirty"))
        if not dirty:
            if path is None:
                self._apply_loaded_doc(view, docmod.TextDocument())
                return True
            try:
                doc = docmod.load_document(path)
            except OSError:
                return False  # 変更の無いタブでファイルが消えた
            self._apply_loaded_doc(view, doc)
            self._restore_cursor(view, entry)
            self._restore_split(view, entry)
            return True
        text = entry.get("text") or ""
        doc = None
        if path is not None:
            try:
                doc = docmod.load_document(path)
            except OSError:
                doc = None
        external_pending = False
        if doc is None:
            # ファイルが読めない(消えた等)。控えの形式で文書を作り、
            # 本文は控えから戻す(保存すればファイルは作り直される)。
            doc = docmod.TextDocument(
                path=path,
                encoding=entry.get("encoding")
                or docmod.DEFAULT_ENCODING,
                bom=bool(entry.get("bom")),
                newline=entry.get("newline") or docmod.DEFAULT_NEWLINE)
        elif (entry.get("disk_mtime") is not None
                and (entry.get("disk_mtime") != doc.disk_mtime
                     or entry.get("disk_size") != doc.disk_size)):
            # ディスクが、控えを取った時点から変わっていた。控えを
            # 優先して戻し、外部変更として扱う: ディスクの印は控え
            # 時点のものにしておくので、保存のときに既存の確認
            # (外部で変更されています)がそのまま効く。
            external_pending = True
            doc.disk_mtime = entry.get("disk_mtime")
            doc.disk_size = entry.get("disk_size")
        self._apply_loaded_doc(view, doc, text=text, modified=True,
                               external_pending=external_pending)
        self._restore_cursor(view, entry)
        self._restore_split(view, entry)
        if external_pending and view is self._current_view():
            self.statusBar().showMessage(
                "このファイルは外部で変更されました"
                "（編集中の内容はそのままです。保存するときに確認します）", 0)
        return True

    @staticmethod
    def _restore_cursor(view, entry):
        """記録しておいたカーソル位置と選択を戻す。

        位置は QTextCursor の単位で記録してあり、範囲外は Qt 側が
        文書の範囲に収める。"""
        try:
            position = int(entry.get("cursor") or 0)
            anchor = int(entry.get("anchor") or 0)
        except (TypeError, ValueError):
            return
        cursor = view.edit.textCursor()
        cursor.setPosition(anchor)
        if position != anchor:
            cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
        view.edit.setTextCursor(cursor)

    def _restore_split(self, view, entry):
        """記録しておいた分割の状態を、そのタブへ戻す。

        向きが記録に無ければ何もしない。2枚目のカーソルと、
        スプリッタの幅(高さ)も記録どおりに戻す。"""
        name = entry.get("split")
        if name not in SPLIT_ORIENTATIONS:
            return
        second = self._split_view(view, name)
        if second is None:
            return
        try:
            position = int(entry.get("split_cursor") or 0)
            anchor = int(entry.get("split_anchor") or 0)
        except (TypeError, ValueError):
            position = anchor = 0
        cursor = second.textCursor()
        cursor.setPosition(anchor)
        if position != anchor:
            cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
        second.setTextCursor(cursor)
        sizes = entry.get("split_sizes")
        if (isinstance(sizes, list) and len(sizes) == 2
                and all(isinstance(size, int) and size > 0
                        for size in sizes)):
            view.splitter.setSizes(sizes)

    def _capture_session_state(self):
        """いまのタブの顔ぶれを、セッションの記録の形で返す。

        変更の無いタブはパスとカーソルだけ(戻すときはディスクから
        読み直す)。変更のあるタブは本文と形式とディスクの印まで
        控える(ファイル自体は書き換わらない)。"""
        tabs = []
        for view in self._views:
            view.sync_text()
            doc = view.doc
            cursor = view.edit.textCursor()
            entry = {
                "path": doc.path,
                "dirty": bool(view.is_modified()),
                "cursor": cursor.position(),
                "anchor": cursor.anchor(),
            }
            if view.split_edit is not None:
                # 分割の状態(向き・2枚目のカーソル・幅)も控える。
                # 戻すときに、同じ文書を同じ眺めで開き直せる。
                split_cursor = view.split_edit.textCursor()
                entry["split"] = view.split_name()
                entry["split_cursor"] = split_cursor.position()
                entry["split_anchor"] = split_cursor.anchor()
                entry["split_sizes"] = [
                    int(size) for size in view.splitter.sizes()]
            if entry["dirty"]:
                entry.update({
                    "text": doc.text,
                    "encoding": doc.encoding,
                    "bom": doc.bom,
                    "newline": doc.newline,
                    "disk_mtime": doc.disk_mtime,
                    "disk_size": doc.disk_size,
                })
            tabs.append(entry)
        index = self._tabs.currentIndex() if self._tabs is not None else 0
        return {"tabs": tabs, "active": max(0, index)}

    def _schedule_session_save(self):
        """控えの書き出しを予約する(まとめ待ち。止まってから書く)。"""
        if self._session_path is not None and self._session_slot is not None:
            self._session_timer.start()

    def _flush_session(self):
        """控えをいま書き出す。セッションが無ければ何もしない。

        スロットの中身をいまのタブで差し替えてから、ファイル全体
        (他の窓のスロットと未使用分を含む)を書く。失敗しても編集は
        止めない(ステータスに残すだけ)。書けたら True、失敗したら
        False を返す(閉じる側が「無言で消えない」ための判断に使う)。"""
        if self._session_path is None or self._session_slot is None:
            return True
        self._session_timer.stop()
        try:
            state = self._capture_session_state()
            self._session_slot.clear()
            self._session_slot.update(state)
            notice = sessionmod.save(self._session_path)
        except Exception:  # noqa: BLE001  控えの失敗で編集を止めない
            self.statusBar().showMessage("控えを保存できませんでした", 4000)
            return False
        if notice is not None:
            self._notify_newer_session(notice)
        return True

    def _notify_corrupt_session(self):
        """壊れた控えを退避していたら、ステータスで知らせる。

        退避そのものは session.load_session_file が読み込みのときに
        済ませている。ここでは記録を受け取って文にするだけ。"""
        notice = sessionmod.take_corrupt_notice(self._session_path)
        if notice is None:
            return
        backup = notice.get("backup")
        if backup:
            self.statusBar().showMessage(
                "控えのファイルが壊れていたため、"
                f"{os.path.basename(backup)} へ退避しました"
                "（前回のタブは戻せませんでした）", 15000)
        else:
            self.statusBar().showMessage(
                "控えのファイルが壊れていて読み直せませんでした"
                "（前回のタブは戻せませんでした）", 15000)

    def _notify_newer_session(self, notice):
        """新しい側の控えを退避していたら、ステータスで知らせる。

        退避そのものは session.save が保存のときに済ませている。
        ここでは結果を受け取って文にするだけ。"""
        backup = notice.get("backup")
        if backup:
            self.statusBar().showMessage(
                "他の EDDy が書いた新しい控えがあったため、"
                f"{os.path.basename(backup)} へ退避しました"
                "（この窓の控えを優先しました）", 15000)
        else:
            self.statusBar().showMessage(
                "他の EDDy が書いた新しい控えがありましたが、"
                "退避できませんでした（この窓の控えを優先しました）",
                15000)

    def _ask_session_save_failed_choice(self):
        """閉じるときに控えが書けなかった場合の行き先を聞く。

        戻り値は "retry"(もう一度書く) / "discard"(そのまま閉じる。
        未保存の編集は失われる) / "cancel"(閉じない)。
        差し替え口(ask_session_save_failed)があればそちらを使う。"""
        if self.ask_session_save_failed is not None:
            return self.ask_session_save_failed()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText(
            "控え（自動保存）を保存できませんでした。\n"
            "このまま閉じると、未保存の編集が失われます。")
        retry_button = box.addButton(
            "もう一度試す", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton(
            "そのまま閉じる", QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = box.addButton(
            "閉じない", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is retry_button:
            return "retry"
        if clicked is discard_button:
            return "discard"
        return "cancel"

    def _confirm_close_without_session(self, event) -> bool:
        """控えが書けないまま閉じてよいか、利用者と決める。

        閉じてよければ True(呼び側は閉じる処理を続ける)、閉じ
        なければ event を ignore して False。未保存の編集が無い
        ときは聞かない(失われるものが無い)。"""
        if not any(view.is_modified() for view in self._views):
            return True
        while True:
            choice = self._ask_session_save_failed_choice()
            if choice == "retry":
                if self._flush_session():
                    return True
                continue
            if choice == "discard":
                return True
            event.ignore()
            return False

    # ---------------------------------------------------------------
    # 組み立て
    # ---------------------------------------------------------------
    def _action(self, label, slot, keymap_name=None, menu=None):
        action = QAction(label, self)
        if keymap_name:
            # 命令名を objectName にも残す。本文側が、矩形選択中の
            # キーを「どの命令のキーか」で照合するために使う
            # (text_edit の _block_clipboard_key)。
            action.setObjectName(keymap_name)
            keys = self._shortcut_keys_for(keymap_name)
            if keys:
                action.setShortcut(QKeySequence(keys))
            # ショートカットは本文にフォーカスがあるときだけ効かせる。
            # 窓全体に効かせると、検索欄で打った Ctrl+K が本文の
            # 行末まで切り取りとして動いてしまう(レビュー項目6)。
            action.setShortcutContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self._edit.addAction(action)
            self._keymap_actions.setdefault(keymap_name, []).append(action)
        action.triggered.connect(slot)
        if menu is not None:
            menu.addAction(action)
        return action

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("ファイル(&F)")
        self._action("新規(&N)", self.new_document, "new", file_menu)
        self._act_open = self._action(
            "開く(&O)…", self.open_dialog, "open", file_menu)
        self._act_save = self._action(
            "上書き保存(&S)", lambda: self.save(), "save", file_menu)
        self._act_save_as = self._action(
            "名前を付けて保存(&A)…", self.save_as, "save_as", file_menu)
        # 中身は開くたびに作り直す(開いた直後の履歴とずれないため)。
        self._recent_menu = file_menu.addMenu("最近使ったファイル(&R)")
        self._recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        self._rebuild_recent_menu()
        file_menu.addSeparator()
        # 「閉じる」はタブを閉じる(複数あるとき)。1つだけなら窓を
        # 閉じる(close_current_tab_or_window)。
        self._action("閉じる(&C)", self.close_current_tab_or_window,
                     "close_window", file_menu)

        edit_menu = self.menuBar().addMenu("編集(&E)")
        # 元に戻す等は QPlainTextEdit の標準キー(Ctrl+Z 等)が効く。
        # メニューからは同じ槽を呼ぶだけ(最中の変化は記録しない)。
        self._action("元に戻す(&U)", self._undo_edit, None, edit_menu)
        self._action("やり直し(&R)", self._redo_edit, None, edit_menu)
        edit_menu.addSeparator()
        # 切り取り・コピー・貼り付けは、秀丸の F7 / F8 / F9 を受ける
        # ためキー割り当ての命令にもする。槽は呼ぶたびに今のペインを
        # 引く(組み立て時点の本文へ固定すると、タブや分割を切り替え
        # たあとも最初のペインを触ってしまう)。Ctrl+X / Ctrl+C /
        # Ctrl+V の標準キーも従来どおり効く。
        self._action("切り取り(&T)", lambda: self._edit.cut(),
                     "cut", edit_menu)
        self._action("コピー(&C)", lambda: self._edit.copy(),
                     "copy", edit_menu)
        self._action("貼り付け(&P)", lambda: self._edit.paste(),
                     "paste", edit_menu)
        self._action("すべて選択(&A)", self._edit.selectAll, None, edit_menu)
        edit_menu.addSeparator()
        self._action("単語をコピー(&W)", self.copy_word_at_cursor,
                     "copy_word", edit_menu)
        self._action("行をコピー(&Y)", self.copy_current_line,
                     "copy_line", edit_menu)
        self._action("追加切り抜き(&Q)", self.cut_append_selection,
                     "cut_append", edit_menu)
        self._action("追加コピー(&X)", self.copy_append_selection,
                     "copy_append", edit_menu)
        # バックスペース(Ctrl+H)はメニューには出さない。本文に
        # フォーカスがあるときだけ効くアクションとして置く(命令
        # delete_backward を呼ぶので、マクロにも記録される)。
        self._action("バックスペース", lambda: self.execute_command(
            "delete_backward"), "backspace")
        edit_menu.addSeparator()
        self._act_cut_eol = self._action(
            "行末まで切り取り(&K)",
            lambda: self.execute_command("cut_to_end"),
            "cut_to_end", edit_menu)
        self._action("行を削除(&D)",
                     lambda: self.execute_command("delete_line"),
                     "delete_line", edit_menu)
        edit_menu.addSeparator()
        self._action("行を並べ替え（昇順）(&S)",
                     lambda: self.execute_command(
                         "sort_lines", {"order": "asc"}),
                     None, edit_menu)
        self._action("行を並べ替え（降順）(&R)",
                     lambda: self.execute_command(
                         "sort_lines", {"order": "desc"}),
                     None, edit_menu)
        self._action("重複行を削除(&U)",
                     lambda: self.execute_command("remove_duplicate_lines"),
                     None, edit_menu)
        self._action("整形(&M)",
                     lambda: self.execute_command("reformat_text"),
                     "reformat_text", edit_menu)
        convert_menu = edit_menu.addMenu("変換(&T)")
        self._action("全角 → 半角(&H)",
                     lambda: self.execute_command("convert_halfwidth"),
                     None, convert_menu)
        self._action("半角 → 全角(&F)",
                     lambda: self.execute_command("convert_fullwidth"),
                     None, convert_menu)
        self._action("大文字 → 小文字(&L)",
                     lambda: self.execute_command("convert_lower"),
                     None, convert_menu)
        self._action("小文字 → 大文字(&U)",
                     lambda: self.execute_command("convert_upper"),
                     None, convert_menu)
        self._action("指定行へ移動(&G)…", self.ask_goto_line,
                     "goto_line", edit_menu)
        self._action("次の編集行へ(&N)",
                     lambda: self.execute_command("next_edited_line"),
                     "next_edited_line", edit_menu)
        self._action("前の編集行へ(&P)",
                     lambda: self.execute_command("prev_edited_line"),
                     "prev_edited_line", edit_menu)
        edit_menu.addSeparator()
        self._action("ブックマークの付け外し(&B)",
                     lambda: self.execute_command("toggle_bookmark"),
                     "toggle_bookmark", edit_menu)
        self._action("次のブックマークへ(&J)",
                     lambda: self.execute_command("next_bookmark"),
                     "next_bookmark", edit_menu)
        self._action("前のブックマークへ(&V)",
                     lambda: self.execute_command("prev_bookmark"),
                     "prev_bookmark", edit_menu)
        self._action("ブックマーク一覧(&L)…", self.show_bookmark_dialog,
                     "bookmark_list", edit_menu)
        edit_menu.addSeparator()
        self._action("直前の操作を繰り返す(&F)", self.repeat_last,
                     "repeat_last", edit_menu)

        search_menu = self.menuBar().addMenu("検索(&S)")
        self._act_find = self._action(
            "検索(&F)…", self.show_find_bar, "find", search_menu)
        self._action("次を検索(&N)", lambda: self.execute_command("find_next"),
                     "find_next", search_menu)
        self._action("前を検索(&P)", lambda: self.execute_command("find_prev"),
                     "find_prev", search_menu)
        search_menu.addSeparator()
        self._action("置換(&R)…", self.show_find_bar, "replace", search_menu)
        self._action("すべて検索(&A)…", self.show_find_all_dialog,
                     "find_all", search_menu)
        self._action("フォルダ内を検索(&G)…", self.show_grep_dialog,
                     None, search_menu)
        self._action("フォルダ内を検索して置換(&H)…",
                     self.show_grep_replace_dialog, None, search_menu)

        view_menu = self.menuBar().addMenu("表示(&V)")
        self._act_outline = QAction("アウトライン(&O)", self)
        self._act_outline.setCheckable(True)
        # 初期値は設定(利用者の選択)から取る。窓がまだ表示されて
        # いない組み立て中に isVisible() を見ると、ペインを表示に
        # していても必ず False になり、ボタンの表示だけ OFF に
        # ずれる(Nagato の実機報告、2026-11-14 の件2)。
        self._act_outline.setChecked(self._outline_pane_pref)
        self._act_outline.toggled.connect(self.set_outline_pane_visible)
        view_menu.addAction(self._act_outline)
        self._act_line_numbers = QAction("行番号(&L)", self)
        self._act_line_numbers.setCheckable(True)
        self._act_line_numbers.setChecked(self._edit.line_numbers_visible())
        self._act_line_numbers.toggled.connect(self.set_line_numbers_visible)
        view_menu.addAction(self._act_line_numbers)
        view_menu.addSeparator()
        self._act_invisibles = QAction("不可視文字(&I)", self)
        self._act_invisibles.setCheckable(True)
        self._act_invisibles.setChecked(self._edit.show_invisibles())
        self._act_invisibles.toggled.connect(self.set_invisibles_visible)
        view_menu.addAction(self._act_invisibles)
        self._act_current_line = QAction("現在行の強調(&C)", self)
        self._act_current_line.setCheckable(True)
        self._act_current_line.setChecked(self._edit.current_line_enabled())
        self._act_current_line.toggled.connect(
            self.set_current_line_highlight)
        view_menu.addAction(self._act_current_line)
        self._act_wide_cursor = QAction("太いカーソル(&W)", self)
        self._act_wide_cursor.setCheckable(True)
        self._act_wide_cursor.setChecked(self._edit.cursorWidth() > 1)
        self._act_wide_cursor.toggled.connect(self.set_wide_cursor)
        view_menu.addAction(self._act_wide_cursor)
        scheme_menu = view_menu.addMenu("配色(&S)")
        self._scheme_actions = {}
        for name in colormod.scheme_names():
            action = QAction(name, self)
            action.setCheckable(True)
            action.setChecked(name == self._color_scheme_name)
            action.triggered.connect(
                lambda _checked=False, n=name: self.apply_color_scheme(n))
            scheme_menu.addAction(action)
            self._scheme_actions[name] = action
        self._action("色の設定(&E)…", self.show_color_settings,
                     None, view_menu)
        self._action("キー割り当ての設定(&K)…", self.show_keymap_settings,
                     None, view_menu)
        self._action("拡張子別の設定(&X)…", self.show_extension_settings,
                     None, view_menu)
        wrap_menu = view_menu.addMenu("折り返し(&R)")
        self._wrap_actions = {}
        for mode, label in ((WRAP_MODE_COLUMN, "桁数で折り返す(&C)"),
                            (WRAP_MODE_WINDOW, "ウィンドウ幅(&W)"),
                            (WRAP_MODE_NONE, "折り返さない(&N)")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(mode == self._wrap_mode)
            action.triggered.connect(
                lambda _checked=False, m=mode: self.set_wrap_mode(m))
            wrap_menu.addAction(action)
            self._wrap_actions[mode] = action
        wrap_menu.addSeparator()
        self._action("折り返し桁数の設定(&S)…", self._ask_wrap_column,
                     None, wrap_menu)
        self._act_markdown = QAction("Markdown の色付け(&M)", self)
        self._act_markdown.setCheckable(True)
        self._act_markdown.setChecked(self._markdown_pref)
        self._act_markdown.toggled.connect(self.set_markdown_highlight)
        view_menu.addAction(self._act_markdown)
        view_menu.addSeparator()
        # 分割表示(同じ文書を2ペインで見る)。ペインの切り替えだけ
        # 秀丸に合わせて既定で F6 を付け、分割そのものはキー割り
        # 当ての画面で付けられる(命令 split_horizontal /
        # split_vertical / close_split / focus_next_pane)。
        self._action("左右に分割(&H)",
                     lambda: self.split_current_view("horizontal"),
                     "split_horizontal", view_menu)
        self._action("上下に分割(&V)",
                     lambda: self.split_current_view("vertical"),
                     "split_vertical", view_menu)
        self._action("分割をやめる(&U)", self.unsplit_current_view,
                     "close_split", view_menu)
        self._action("次のペインへ(&N)", self.focus_next_pane,
                     "focus_next_pane", view_menu)
        view_menu.addSeparator()
        self._action("アウトライン・強調の設定(&P)…",
                     self.show_pattern_settings, None, view_menu)

        macro_menu = self.menuBar().addMenu("マクロ(&M)")
        self._macro_menu = macro_menu
        self._act_macro_record = QAction("記録開始(&R)", self)
        self._act_macro_record.setCheckable(True)
        self._act_macro_record.toggled.connect(self.set_macro_recording)
        macro_menu.addAction(self._act_macro_record)
        # 記録の開始／終了にもキー割り当て(既定は Shift+F1)を付ける。
        # _action で作るアクションと同じ作法(本文にフォーカスがある
        # ときだけ効く・付け直しと新しい本文への登録の対象にする)。
        record_keys = self._shortcut_keys_for("macro_record")
        if record_keys:
            self._act_macro_record.setShortcut(QKeySequence(record_keys))
        self._act_macro_record.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._edit.addAction(self._act_macro_record)
        self._keymap_actions.setdefault("macro_record", []).append(
            self._act_macro_record)
        # play_macro は引数 commands を取るので、QAction.triggered の
        # bool をそのまま渡さないよう lambda で受ける(False が
        # 「マクロなし」として伝わり、再生できなくなる)。
        self._action("再生(&P)", lambda: self.play_macro(),
                     "macro_play", macro_menu)
        self._action("Python で開く(&Y)…",
                     lambda: self.show_python_macro_dialog(),
                     None, macro_menu)
        macro_menu.addSeparator()
        self._action("マクロを保存(&S)…", self.save_macro_dialog,
                     None, macro_menu)
        self._action("マクロを開いて再生(&O)…", self.load_macro_dialog,
                     None, macro_menu)
        macro_menu.addSeparator()
        self._action("マクロの管理(&G)…", self.show_macro_library,
                     None, macro_menu)
        # 登録マクロの項目はこの後ろへ足す(変更のたびに付け直す)。
        self._rebuild_registered_macro_menu()

        copilot_menu = self.menuBar().addMenu("Copilot(&C)")
        self._act_send = self._action(
            "選択範囲を Copilot に投げる(&S)", self.send_selection,
            "send_selection", copilot_menu)

    def _rebuild_recent_menu(self):
        """最近使ったファイルのサブメニューを、いまの履歴で作り直す。

        メニューを開くたびに呼ばれる(aboutToShow)ので、開いた
        直後の状態ともずれない。履歴が無ければ、空と分かる
        無効な項目だけを出す。"""
        menu = self._recent_menu
        menu.clear()
        if not self._recent_files:
            empty = menu.addAction("（履歴はありません）")
            empty.setEnabled(False)
            return
        for path in self._recent_files:
            action = menu.addAction(path)
            action.triggered.connect(
                lambda _checked=False, p=path: self.open_recent_file(p))
        menu.addSeparator()
        clear = menu.addAction("履歴を消す(&C)")
        clear.triggered.connect(self.clear_recent_files)

    def _build_toolbar(self):
        """リボン風のツールバー。メニューと同じ QAction を並べるので、
        処理もチェック状態も二重にならない。"""
        toolbar = QToolBar("操作", self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for action in (self._act_open, self._act_save, self._act_save_as,
                       self._act_find, self._act_send):
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self._act_outline)
        toolbar.addAction(self._act_line_numbers)
        self.addToolBar(toolbar)
        self._toolbar = toolbar

    def _build_find_bar(self):
        bar = QFrame()
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(4)
        row = QHBoxLayout()
        row.addWidget(QLabel("検索"))
        self._find_edit = QLineEdit()
        self._find_edit.returnPressed.connect(
            lambda: self.execute_command("find_next"))
        # リアルタイム検索: 人が打ったときだけ(textEdited)反応する。
        # setText では来ないので、マクロ再生やテストの一括設定では
        # 走らない。
        self._find_edit.textEdited.connect(self._on_find_edited)
        row.addWidget(self._find_edit, 1)
        self._find_case = QCheckBox("大文字小文字を区別")
        self._find_case.toggled.connect(self._on_find_condition_changed)
        row.addWidget(self._find_case)
        self._find_regex = QCheckBox("正規表現")
        self._find_regex.toggled.connect(self._on_find_condition_changed)
        row.addWidget(self._find_regex)
        next_button = QPushButton("次へ")
        next_button.clicked.connect(lambda: self.execute_command("find_next"))
        row.addWidget(next_button)
        prev_button = QPushButton("前へ")
        prev_button.clicked.connect(lambda: self.execute_command("find_prev"))
        row.addWidget(prev_button)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self._hide_find_bar)
        row.addWidget(close_button)
        outer.addLayout(row)
        replace_row = QHBoxLayout()
        replace_row.addWidget(QLabel("置換"))
        self._replace_edit = QLineEdit()
        replace_row.addWidget(self._replace_edit, 1)
        replace_button = QPushButton("置換")
        replace_button.clicked.connect(
            lambda: self.execute_command("replace_next"))
        replace_row.addWidget(replace_button)
        replace_all_button = QPushButton("すべて置換")
        replace_all_button.clicked.connect(
            lambda: self.execute_command("replace_all"))
        replace_row.addWidget(replace_all_button)
        outer.addLayout(replace_row)
        escape = QShortcut(QKeySequence("Esc"), bar)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self._hide_find_bar)
        # 本文側のショートカットは本文にフォーカスがあるときだけ
        # 効くので、検索欄にフォーカスがあっても次・前へ進めるよう、
        # 検索バーにも同じキー(F3 等)を置く。
        # 割り当てが空でもオブジェクトは作っておく(あとからキー割り
        # 当ての画面で付け直せるように。空のままでは発火しない)。
        next_shortcut = QShortcut(
            QKeySequence(self._keymap.get("find_next") or ""), bar)
        next_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        next_shortcut.activated.connect(
            lambda: self.execute_command("find_next"))
        self._find_next_shortcut = next_shortcut
        prev_shortcut = QShortcut(
            QKeySequence(self._keymap.get("find_prev") or ""), bar)
        prev_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        prev_shortcut.activated.connect(
            lambda: self.execute_command("find_prev"))
        self._find_prev_shortcut = prev_shortcut
        return bar

    # ---------------------------------------------------------------
    # 設定の保存(呼び側へ差分を流す)
    # ---------------------------------------------------------------
    def _persist_settings(self, updates):
        """表示・フォント・正規表現の変更を呼び側へ渡す。

        スロットから呼ばれるので、保存に失敗しても窓を落とさない。"""
        if self._on_settings_change is None:
            return
        try:
            self._on_settings_change(dict(updates))
        except Exception:  # noqa: BLE001  保存の失敗で編集を止めない
            self.statusBar().showMessage("設定を保存できませんでした", 4000)

    def _arm_pattern_guard(self):
        """正規表現の適用の前に「適用中」の印(PATTERN_APPLYING_KEY)を
        設定へ書く。

        保存の口が無い(呼び側が設定を持たない)ときは何もしない。
        起動時の保険で既定に戻す場合は、その書き戻し(式を空にする)
        も同じ書き込みで流す。"""
        if self._on_settings_change is None:
            return
        updates = {PATTERN_APPLYING_KEY: True}
        if self._pattern_reset_updates:
            updates.update(self._pattern_reset_updates)
            self._pattern_reset_updates = None
        self._persist_settings(updates)
        self._pattern_guard_armed = True

    def _disarm_pattern_guard(self):
        """適用が無事に済んだので「適用中」の印を消す。"""
        if not self._pattern_guard_armed:
            return
        self._pattern_guard_armed = False
        self._persist_settings({PATTERN_APPLYING_KEY: False})

    # ---------------------------------------------------------------
    # 表示の切り替え
    # ---------------------------------------------------------------
    def set_outline_pane_visible(self, visible):
        visible = bool(visible)
        self._outline_pane_pref = visible
        for view in self._views:
            view.outline_pane.setVisible(visible)
        if self._act_outline.isChecked() != visible:
            blocker = QSignalBlocker(self._act_outline)
            self._act_outline.setChecked(visible)
        self._persist_settings({"show_outline_pane": visible})

    def outline_pane_visible(self) -> bool:
        # 表示の実体ではなく設定(利用者の選択)を返す。組み立て中は
        # isVisible() が常に False で、当てにならないため。
        return self._outline_pane_pref

    def set_line_numbers_visible(self, visible):
        visible = bool(visible)
        self._line_numbers_pref = visible
        for view in self._views:
            for edit in view.panes():
                edit.set_line_numbers_visible(visible)
        if self._act_line_numbers.isChecked() != visible:
            blocker = QSignalBlocker(self._act_line_numbers)
            self._act_line_numbers.setChecked(visible)
        self._persist_settings({"show_line_numbers": visible})

    def line_numbers_visible(self) -> bool:
        return self._edit.line_numbers_visible()

    def set_invisibles_visible(self, visible):
        visible = bool(visible)
        self._invisibles_pref = visible
        for view in self._views:
            for edit in view.panes():
                edit.set_show_invisibles(visible)
        if self._act_invisibles.isChecked() != visible:
            blocker = QSignalBlocker(self._act_invisibles)
            self._act_invisibles.setChecked(visible)
        self._persist_settings({"show_invisibles": visible})

    def invisibles_visible(self) -> bool:
        return self._edit.show_invisibles()

    def set_current_line_highlight(self, enabled):
        enabled = bool(enabled)
        self._current_line_pref = enabled
        for view in self._views:
            for edit in view.panes():
                edit.set_current_line_enabled(enabled)
        if self._act_current_line.isChecked() != enabled:
            blocker = QSignalBlocker(self._act_current_line)
            self._act_current_line.setChecked(enabled)
        self._persist_settings({"highlight_current_line": enabled})

    def current_line_highlight(self) -> bool:
        return self._edit.current_line_enabled()

    def set_wide_cursor(self, wide):
        wide = bool(wide)
        self._wide_cursor_pref = wide
        for view in self._views:
            for edit in view.panes():
                edit.setCursorWidth(3 if wide else 1)
        if self._act_wide_cursor.isChecked() != wide:
            blocker = QSignalBlocker(self._act_wide_cursor)
            self._act_wide_cursor.setChecked(wide)
        self._persist_settings({"wide_cursor": wide})

    def wide_cursor(self) -> bool:
        return self._edit.cursorWidth() > 1

    # ---------------------------------------------------------------
    # 拡張子別の設定(全体+上書き。editor/ext_settings.py)
    # ---------------------------------------------------------------
    def _view_extension(self, view) -> str:
        """そのタブのファイルの拡張子(小文字・ドット無し)。
        無題・ビューが無ければ ""(全体の設定に従う)。"""
        if view is None or view.doc is None:
            return ""
        return extmod.extension_of(view.doc.path)

    def _global_display_values(self) -> dict:
        """全体の表示設定の値(拡張子別の解決の土台)。"""
        return {
            "wrap_mode": self._wrap_mode,
            "wrap_column": self._wrap_column,
            "markdown_highlight": self._markdown_pref,
            "font_family": self._font_family,
            "font_size": self._font_size_base,
            "tab_width": self._tab_width,
        }

    def _effective_display(self, view) -> dict:
        """そのタブの実効の表示設定(全体に拡張子の上書きを重ねた組)。"""
        return extmod.resolve(self._global_display_values(),
                              self._ext_settings,
                              self._view_extension(view))

    def _apply_font_to_view(self, view):
        """実効のフォントとタブ幅を、1つのビューの本文へ適用する
        (保存はしない)。分割していれば両ペインへ行き渡る。"""
        effective = self._effective_display(view)
        for edit in view.panes():
            edit.setFont(
                QFont(effective["font_family"], effective["font_size"]))
            edit.set_tab_width(effective["tab_width"])

    def _apply_view_display(self, view):
        """実効の表示設定(折り返し・Markdown・フォント・タブ幅)を、
        1つのビューへまとめて適用する(保存はしない)。

        文書を読み込んだとき・名前を付けて保存したときのように、
        そのタブの拡張子が決まった(変わった)タイミングで呼ぶ。"""
        self._apply_wrap_to_view(view)
        self._update_markdown_highlight(view)
        self._apply_font_to_view(view)

    def apply_extension_settings(self, settings_map):
        """拡張子別の設定画面からの変更(上書きの対応)を反映して
        保存する。開いているタブは、その場でその拡張子の実効値に
        合わせ直す。保存は extension_settings キーへの差分のみ。"""
        self._ext_settings = extmod.sanitize(settings_map)
        for view in self._views:
            self._apply_view_display(view)
        self._persist_settings({
            EXTENSION_SETTINGS_KEY: {
                ext: dict(items)
                for ext, items in self._ext_settings.items()},
        })

    def show_extension_settings(self):
        """拡張子別の設定画面(editor/ext_settings_dialog.py)を開く。

        変更はその場でこの窓へ反映され、設定へ保存される
        (apply_extension_settings 経由)。"""
        current_ext = self._view_extension(self._current_view())
        dialog = ExtensionSettingsDialog(
            self, settings_map=self._ext_settings,
            global_values=self._global_display_values(),
            extensions=self._known_extensions(),
            current_extension=current_ext or "txt",
            on_change=self.apply_extension_settings)
        dialog.exec()

    def extension_settings(self) -> dict:
        """設定へ保存している上書き分(拡張子 → 項目 → 値)の写し。"""
        return {ext: dict(items)
                for ext, items in self._ext_settings.items()}

    def _known_extensions(self) -> list:
        """設定済みと、開いているタブの拡張子の並び(画面の候補用)。"""
        found = set(self._ext_settings)
        for view in self._views:
            ext = self._view_extension(view)
            if ext:
                found.add(ext)
        return sorted(found)

    # ---------------------------------------------------------------
    # 折り返し(見た目だけ。文書の内容は変えない)
    # ---------------------------------------------------------------
    def _apply_wrap_to_view(self, view):
        """そのタブの実効の方式・桁数を、本文へ適用する(保存はしない)。
        分割していれば両ペインへ行き渡る。"""
        effective = self._effective_display(view)
        if effective["wrap_mode"] == WRAP_MODE_NONE:
            wrap_mode = QPlainTextEdit.LineWrapMode.NoWrap
        else:
            # 「桁数」も「ウィンドウ幅」も、折り返し自体は窓幅で
            # 行う。「桁数」は右の余白で見える幅を狭めて実現する。
            wrap_mode = QPlainTextEdit.LineWrapMode.WidgetWidth
        # 桁数は、方式が「桁数」のときだけ余白を取る。
        column = (effective["wrap_column"]
                  if effective["wrap_mode"] == WRAP_MODE_COLUMN else None)
        for edit in view.panes():
            edit.setLineWrapMode(wrap_mode)
            edit.set_wrap_column(column)

    def _apply_wrap_mode(self):
        """方式・桁数を、全部のタブへ適用する(保存はしない)。

        各タブは自分の拡張子の実効値で適用される(上書きのある
        タブは、全体を変えても上書きのまま)。"""
        for view in self._views:
            self._apply_wrap_to_view(view)

    def _apply_wrap_column(self):
        """桁数を全部のタブへ伝える(各タブは実効値で適用)。"""
        for view in self._views:
            self._apply_wrap_to_view(view)

    def set_wrap_mode(self, mode):
        if mode not in WRAP_MODES:
            mode = DEFAULT_WRAP_MODE
        self._wrap_mode = mode
        self._apply_wrap_mode()
        for name, action in self._wrap_actions.items():
            if action.isChecked() != (name == mode):
                blocker = QSignalBlocker(action)
                action.setChecked(name == mode)
        self._persist_settings({"wrap_mode": mode})

    def wrap_mode(self) -> str:
        return self._wrap_mode

    def set_wrap_column(self, column):
        try:
            column = int(column)
        except (TypeError, ValueError):
            return
        if column <= 0:
            return
        self._wrap_column = column
        if self._wrap_mode == WRAP_MODE_COLUMN:
            self._apply_wrap_column()
        self._persist_settings({"wrap_column": column})

    def wrap_column(self) -> int:
        return self._wrap_column

    def _ask_wrap_column(self):
        try:
            value, ok = QInputDialog.getInt(
                self, APP_NAME, "折り返しの桁数", self._wrap_column,
                8, 1000, 1)
            if ok:
                self.set_wrap_column(value)
                # 桁数を決めたら、その方式で見えるように切り替える。
                if self._wrap_mode != WRAP_MODE_COLUMN:
                    self.set_wrap_mode(WRAP_MODE_COLUMN)
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"設定できませんでした ({e})")

    # ---------------------------------------------------------------
    # Markdown の色付け(拡張子で判定し、希望で切り替える)
    # ---------------------------------------------------------------
    def _update_markdown_highlight(self, view=None):
        """そのタブのファイルが Markdown なら色付けを効かせる。

        効くのは、利用者の希望(設定 markdown_highlight)が入っていて、
        かつ拡張子が .md / .markdown のときだけ。ただし、その拡張子に
        拡張子別の上書き(markdown_highlight)があれば、そちらが
        そのまま効く(.md で切ることも、.txt で入れることもできる)。
        view が無ければ全部のタブへ適用する。"""
        if view is None:
            for item in self._views:
                self._update_markdown_highlight(item)
            return
        ext = self._view_extension(view)
        overrides = self._ext_settings.get(ext) or {}
        if "markdown_highlight" in overrides:
            enabled = bool(overrides["markdown_highlight"])
        else:
            enabled = bool(self._markdown_pref) \
                and ext in MARKDOWN_EXTENSIONS
        if view.highlighter is not None:
            view.highlighter.set_markdown_enabled(enabled)

    def set_markdown_highlight(self, enabled):
        enabled = bool(enabled)
        self._markdown_pref = enabled
        if self._act_markdown.isChecked() != enabled:
            blocker = QSignalBlocker(self._act_markdown)
            self._act_markdown.setChecked(enabled)
        self._update_markdown_highlight()
        self._persist_settings({"markdown_highlight": enabled})

    def markdown_highlight(self) -> bool:
        """利用者の希望(設定値)。実際に効いているかは拡張子にも
        よるので、ハイライタ側の markdown_enabled を見ること。"""
        return self._markdown_pref

    def _apply_color_scheme_to_view(self, view):
        """実効の配色(プリセット+上書き)を、1つのビューの本文と
        ハイライタへ反映する。"""
        scheme = colormod.resolve_scheme(
            self._color_scheme_name, self._color_overrides)
        for edit in view.panes():
            edit.apply_colors(scheme)
        if view.highlighter is not None:
            view.highlighter.set_background(
                QColor(scheme["star_background"]))
            view.highlighter.set_markdown_colors(scheme)

    def _apply_color_scheme_to_views(self):
        """実効の配色(プリセット+上書き)を全部のタブへ反映する。"""
        for view in self._views:
            self._apply_color_scheme_to_view(view)

    def _sync_scheme_actions(self):
        for scheme_name, action in self._scheme_actions.items():
            if action.isChecked() != (scheme_name == self._color_scheme_name):
                blocker = QSignalBlocker(action)
                action.setChecked(scheme_name == self._color_scheme_name)

    def apply_color_scheme(self, name):
        """配色プリセット(editor/colors.py)を切り替えて保存する。

        上書きした項目はそのまま残り、上書きの無い項目は新しい
        プリセットに追随する。"""
        if name not in colormod.COLOR_SCHEMES:
            name = colormod.DEFAULT_SCHEME
        self._color_scheme_name = name
        self._apply_color_scheme_to_views()
        self._sync_scheme_actions()
        self._persist_settings({"color_preset": name})

    def apply_color_settings(self, name, overrides):
        """色の設定画面からの変更(プリセットと上書き)を反映して保存する。"""
        if name not in colormod.COLOR_SCHEMES:
            name = colormod.DEFAULT_SCHEME
        self._color_scheme_name = name
        self._color_overrides = colormod.sanitize_overrides(overrides)
        self._apply_color_scheme_to_views()
        self._sync_scheme_actions()
        self._persist_settings({
            "color_preset": name,
            "color_overrides": dict(self._color_overrides),
        })

    def show_color_settings(self):
        """色の設定画面(editor/color_dialog.py)を開く。

        変更はその場でこの窓へ反映され、設定へ保存される
        (apply_color_settings 経由)。"""
        dialog = ColorSettingsDialog(
            self, preset_name=self._color_scheme_name,
            overrides=self._color_overrides,
            on_change=self.apply_color_settings)
        dialog.exec()

    def color_scheme_name(self) -> str:
        return self._color_scheme_name

    def color_overrides(self) -> dict:
        return dict(self._color_overrides)

    def apply_keymap_settings(self, overrides):
        """キー割り当ての画面からの変更(上書き分)を反映して保存する。

        実効の表を作り直し、登録済みのアクションと検索バーの
        ショートカットへ付け直す(開いているこの窓にその場で効く)。
        保存は keymap キーへの差分(丸ごと書き換えはしない)。"""
        self._keymap_overrides = keymapmod.sanitize_overrides(overrides)
        self._keymap = keymapmod.merge_keymap(self._keymap_overrides)
        self._apply_keymap_to_actions()
        # 登録マクロのショートカットは、組み込みとの重なりで付け
        # 直しが要るので、こちらも作り直す。
        self._rebuild_registered_macro_menu()
        self._persist_settings({KEYMAP_KEY: dict(self._keymap_overrides)})

    def _apply_keymap_to_actions(self):
        """実効のキー割り当てを、_action で登録したアクションと
        検索バーの次・前へのショートカットへ付け直す。"""
        for name, actions in self._keymap_actions.items():
            keys = self._shortcut_keys_for(name)
            sequence = QKeySequence(keys) if keys else QKeySequence()
            for action in actions:
                action.setShortcut(sequence)
        for name, shortcut in (("find_next", self._find_next_shortcut),
                               ("find_prev", self._find_prev_shortcut)):
            if shortcut is not None:
                keys = self._shortcut_keys_for(name)
                shortcut.setKey(
                    QKeySequence(keys) if keys else QKeySequence())

    def show_keymap_settings(self):
        """キー割り当ての画面(editor/keymap_dialog.py)を開く。

        変更はその場でこの窓へ反映され、設定へ保存される
        (apply_keymap_settings 経由)。"""
        dialog = KeymapSettingsDialog(
            self, keymap=self._keymap,
            on_change=self.apply_keymap_settings)
        dialog.exec()

    def keymap(self) -> dict:
        """実効のキー割り当て(命令名 → キー)の写し。"""
        return dict(self._keymap)

    def keymap_conflicts(self):
        """重なっているキー → 命令名の並び(COMMAND_LABELS の順)。

        表(と保存される上書き分)はそのままにして、実行時の
        付け方だけ _shortcut_keys_for が先勝ちで決める。"""
        by_key = {}
        for name, _label in keymapmod.COMMAND_LABELS:
            keys = self._keymap.get(name) or ""
            if keys:
                normalized = QKeySequence(keys).toString(
                    QKeySequence.SequenceFormat.PortableText)
                by_key.setdefault(normalized, []).append(name)
        return {keys: names for keys, names in by_key.items()
                if len(names) > 1}

    def _shortcut_keys_for(self, name, table=None):
        """その命令に実際に付けるキー。

        重なりがあるキーは、並び(COMMAND_LABELS)で先の命令が
        持ち、後ろの命令には付けない。両方に付けると Qt の
        ショートカットの競合でどちらも効かなくなるため。表は
        変えないので、重なりはキー割り当ての画面で見て直せる。"""
        table = self._keymap if table is None else table
        keys = table.get(name) or ""
        if not keys:
            return ""
        normalized = QKeySequence(keys).toString(
            QKeySequence.SequenceFormat.PortableText)
        for other, _label in keymapmod.COMMAND_LABELS:
            if other == name:
                break
            other_keys = table.get(other) or ""
            if other_keys and QKeySequence(other_keys).toString(
                    QKeySequence.SequenceFormat.PortableText) == normalized:
                return ""
        return keys

    def keymap_overrides(self) -> dict:
        """設定へ保存している上書き分(既定と違う項目だけ)の写し。"""
        return dict(self._keymap_overrides)

    def font_size(self) -> int:
        return self._edit.font_size()

    def _on_font_size_changed(self, size, view=None):
        # Ctrl+ホイールは連続して流れてくるので、設定への保存は
        # その場ではせず、止まってから最後の差分を1回だけ書く
        # (遅延保存)。控えへの反映はその場で行う。
        if view is None:
            view = self._current_view()
        self._pending_font_size = size
        # 書き戻し先は、そのタブに効いている大きさの出どころ。
        # その拡張子に大きさの上書きがあれば上書きへ、無ければ
        # 全体の大きさへ(従来どおり、他のタブにも同じ大きさが
        # 当たる)。上書きのある他のタブは、自分の上書きのまま。
        ext = self._view_extension(view)
        overrides = self._ext_settings.get(ext) or {}
        if ext and "font_size" in overrides:
            updated = {name: dict(items)
                       for name, items in self._ext_settings.items()}
            updated[ext]["font_size"] = size
            self._ext_settings = extmod.sanitize(updated)
            self._pending_font_persist = {
                EXTENSION_SETTINGS_KEY: {
                    name: dict(items)
                    for name, items in self._ext_settings.items()},
            }
        else:
            self._font_size_base = size
            self._pending_font_persist = {"font_size": size}
        self._font_save_timer.start()
        for item in self._views:
            self._apply_font_to_view(item)

    def _flush_pending_font_size(self):
        """たまっているフォントの大きさの保存を、いま書く。"""
        if self._pending_font_persist is None:
            self._pending_font_size = None
            return
        updates = self._pending_font_persist
        self._pending_font_persist = None
        self._pending_font_size = None
        self._font_save_timer.stop()
        self._persist_settings(updates)

    # ---------------------------------------------------------------
    # アウトラインと左ペインの情報
    # ---------------------------------------------------------------
    def _rebuild_outline(self, view=None):
        if view is None:
            view = self._current_view()
        if view is None:
            return
        entries, truncated = outline.extract_outline_ex(
            view.edit.toPlainText(), self._outline_rx,
            limit=outline.OUTLINE_ENTRY_LIMIT)
        view.outline_pane.set_outline(
            outline.build_outline_tree(entries), truncated=truncated)

    def _jump_to_line(self, line_number, view=None):
        if view is None:
            view = self._current_view()
        if view is None:
            return
        if view is not self._current_view():
            self._activate_view(view)
        document = view.edit.document()
        if line_number < 0 or line_number >= document.blockCount():
            return
        # ブロック番号で引く。findBlockByLineNumber は折り返した
        # 「見た目の行」で数えるので、長い行があると別の行へ飛ぶ。
        block = document.findBlockByNumber(line_number)
        cursor = QTextCursor(block)
        # 飛ぶ先は、分割していれば現在のペイン(見ている側)。
        target = view.active_edit()
        target.setTextCursor(cursor)
        target.centerCursor()
        target.setFocus()

    def _file_size_text(self, view=None):
        if view is None:
            view = self._current_view()
        path = view.doc.path if view is not None else None
        if path:
            try:
                return f"{os.path.getsize(path):,} バイト"
            except OSError:
                pass
        return "—"

    def _refresh_pane_info(self, view=None):
        if view is None:
            view = self._current_view()
        if view is None:
            return
        doc = view.doc
        label = text_encoding.ENCODING_LABELS.get(doc.encoding,
                                                  doc.encoding)
        format_text = (
            f"{label} / BOM {'あり' if doc.bom else 'なし'} / "
            f"{docmod.NEWLINE_LABELS.get(doc.newline, '')}")
        if doc.lossy_decode:
            format_text += " / 一部読み替えあり"
        view.outline_pane.set_file_info(
            path=doc.path, format_text=format_text,
            size_text=self._file_size_text(view))
        self._refresh_pane_counts(view)

    def _refresh_pane_counts(self, view=None):
        if view is None:
            view = self._current_view()
        if view is None:
            return
        document = view.edit.document()
        # 文字数は characterCount で数える(toPlainText の全文コピーは
        # 大きい文書で打鍵のたびに重い。characterCount は末尾の段落
        # 区切りを1つ余分に数えるので、その分を引く)。
        view.outline_pane.set_counts(
            document.blockCount(), max(0, document.characterCount() - 1))

    # ---------------------------------------------------------------
    # アウトライン・強調の正規表現の設定
    # ---------------------------------------------------------------
    def show_pattern_settings(self):
        try:
            dialog = PatternSettingsDialog(
                self._outline_pattern_text, self._highlight_pattern_text,
                self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                outline_text, highlight_text = dialog.values()
                self._set_patterns(outline_text, highlight_text)
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"設定を開けませんでした ({e})")

    def _set_patterns(self, outline_text, highlight_text):
        """正規表現を差し替え、開いている窓へ即反映して保存する。

        適用の前に「適用中」の印を立てる。適用の途中で固まれば印が
        残り、次回の起動で既定に戻る(起動時の保険)。"""
        self._arm_pattern_guard()
        self._outline_pattern_text = outline_text
        self._highlight_pattern_text = highlight_text
        self._outline_rx = outline.compile_pattern(outline_text)
        self._highlight_rx = outline.compile_pattern(highlight_text)
        for view in self._views:
            if view.highlighter is not None:
                view.highlighter.set_pattern(self._highlight_rx)
        self._rebuild_outline()
        # 既定と同じ式は、文字列ではなく空で保存する(空 = 組み込み
        # 既定を使う、という決め)。こうしておくと、組み込み既定を
        # あとで良くしたときに、保存済みの窓も追随する。
        updates = {
            "outline_pattern": (
                "" if outline_text == outline.DEFAULT_OUTLINE_PATTERN
                else outline_text),
            "highlight_pattern": (
                "" if highlight_text == outline.DEFAULT_HIGHLIGHT_PATTERN
                else highlight_text),
        }
        if self._pattern_guard_armed:
            # 適用が済んだので、印も同じ書き込みで消す。
            updates[PATTERN_APPLYING_KEY] = False
            self._pattern_guard_armed = False
        self._persist_settings(updates)

    # ---------------------------------------------------------------
    # 表示の更新
    # ---------------------------------------------------------------
    def _refresh_title(self):
        dirty = self._edit.document().isModified()
        name = self._doc.display_name
        self.setWindowTitle(f"{name}{' *' if dirty else ''} - {APP_NAME}")

    def _refresh_status_labels(self):
        label = text_encoding.ENCODING_LABELS.get(self._doc.encoding,
                                                  self._doc.encoding)
        text = f"{label} / {docmod.NEWLINE_LABELS.get(self._doc.newline, '')}"
        if self._doc.lossy_decode:
            text += " / 一部読み替えあり"
        if self._doc.mixed_newlines:
            # 混在のままでは保存しない(明示的な保存のときに、混在
            # のままか・どちらへ統一かを聞く)。統一を選んで保存
            # すれば mixed_newlines が落ち、表示も _refresh_all
            # 経由で一緒に消える。
            text += " / 改行混在（保存時に選びます）"
        self._enc_label.setText(text)

    def _refresh_position(self):
        cursor = self._edit.textCursor()
        line = cursor.blockNumber() + 1
        column = cursor.positionInBlock() + 1
        text = f"{line} 行 {column} 桁"
        if cursor.hasSelection():
            # 選択範囲の文字数と行数。改行は1文字に数える(選択文字列
            # では段落の区切りが U+2029 で、そのまま1文字)。
            # 行数は、選択の文字がまたがっている行の数(末尾が次の
            # 行頭ちょうどのときは、その行は数えない)。
            chars = len(cursor.selectedText())
            document = self._edit.document()
            start_block = document.findBlock(
                cursor.selectionStart()).blockNumber()
            end_pos = cursor.selectionEnd()
            if end_pos > cursor.selectionStart():
                end_pos -= 1
            end_block = document.findBlock(end_pos).blockNumber()
            text += f"（選択 {chars} 文字 / {end_block - start_block + 1} 行）"
        self._pos_label.setText(text)
        self._outline_pane.set_cursor_position(line, column)

    def _refresh_all(self, view=None):
        """窓の表示を、今のタブ(または渡されたビュー)に合わせ直す。"""
        if view is None:
            view = self._current_view()
        if view is None:
            return
        if view is self._current_view():
            self._refresh_title()
            self._refresh_status_labels()
            self._refresh_position()
        self._update_markdown_highlight(view)
        self._refresh_pane_info(view)
        # ここで同期的に作り直すので、編集の追随で予約された分は
        # 要らない(二重に走らせない)。
        view.outline_timer.stop()
        self._rebuild_outline(view)

    # ---------------------------------------------------------------
    # 開く・新規
    # ---------------------------------------------------------------
    def open_path(self, path) -> bool:
        """ファイルをタブとして開く。失敗したら理由をステータスに
        出して False。

        既にタブにあれば、そのタブを前に出すだけ(読み直さない)。
        今のタブが手付かずの無題なら、そこへ読み込む(起動直後に
        空のタブが残らないように)。それ以外は新しいタブを足す。
        未保存の編集があっても確認は出さない(今のタブはそのまま
        残るので、捨てることにはならない)。"""
        existing = self.find_view_for_path(path)
        if existing is not None:
            self._activate_view(existing)
            existing.active_edit().setFocus()
            # 「最近使った」順は、タブを前に出したことも使うこと
            # として数える(履歴の先頭へ繰り上がる)。
            self._note_recent_file(existing.doc.path)
            return True
        try:
            doc = docmod.load_document(path)
        except OSError as e:
            self.statusBar().showMessage(f"開けませんでした: {path} ({e})")
            return False
        current = self._current_view()
        if current is not None and self._is_pristine(current):
            view = current
        else:
            view = self._add_view()
            self._activate_view(view)
        self._apply_loaded_doc(view, doc)
        self._show_load_notices(doc)
        self._note_recent_file(doc.path)
        view.active_edit().setFocus()
        return True

    def _apply_loaded_doc(self, view, doc, *, text=None, modified=False,
                          external_pending=False):
        """読み込んだ(または控えから戻した)文書をビューへ据える。

        text を渡すと、文書モデルの本文ではなくその文字列を表示
        する(控えの復元用。行ごとの改行種別の控えは使わず、文書の
        改行設定で描く)。監視の張り替えもここで行う。"""
        view.doc = doc
        view.suppress_change_record = True
        try:
            view.edit.setPlainText(doc.text if text is None else text)
        finally:
            view.suppress_change_record = False
        if text is None:
            # 行ごとの改行種別の控えを本文へ渡す(改行記号の描き分け用)。
            view.edit.set_line_newlines(doc.line_newlines, doc.newline)
        else:
            view.edit.set_line_newlines(None, doc.newline)
        # 読み込んだ内容が編集マークの基準になる。
        view.edit.clear_edited_lines()
        # ブックマークは前の文書の印なので、開き直したら消す。
        view.edit.clear_bookmarks()
        view.edit.document().setModified(modified)
        # 分割していれば、控えの消去(編集マーク・ブックマーク)は
        # 共有の置き場に効くので、もう片方の行番号も合わせ直す。
        view.refresh_pane_areas()
        view.external_change_pending = external_pending
        self._update_tab_label(view)
        self._update_watcher(view)
        # 拡張子が決まったので、表示設定はその拡張子の実効値に
        # 合わせ直す(拡張子別の設定)。
        self._apply_view_display(view)
        if view is self._current_view():
            self._refresh_all()
        else:
            self._refresh_pane_info(view)
            self._rebuild_outline(view)

    def _set_loaded_doc(self, doc):
        """読み込んだ文書を今のタブに据える(開く・再読み込みの
        共通処理)。読み替え・改行混在の知らせもここで出す。"""
        self._apply_loaded_doc(self._current_view(), doc)
        self._show_load_notices(doc)

    def _show_load_notices(self, doc):
        notices = []
        if doc.lossy_decode:
            notices.append(
                "一部の文字を読み替えました（保存すると置換文字で書かれます）")
        if doc.mixed_newlines:
            notices.append(
                "改行コードが混在しています"
                "（保存するときに、混在のままか統一かを聞きます）")
        if notices:
            self.statusBar().showMessage("。".join(notices))

    def reload_from_disk(self) -> bool:
        """ディスクの内容で読み直す(外部変更の再読み込み)。

        未保存の編集があるときは呼ばない前提(呼び側で切り分ける)。"""
        path = self._doc.path
        if not path:
            return False
        try:
            doc = docmod.load_document(path)
        except OSError as e:
            self.statusBar().showMessage(f"再読み込みできませんでした ({e})")
            return False
        self._set_loaded_doc(doc)
        self.statusBar().showMessage("再読み込みしました", 4000)
        return True

    def open_dialog(self):
        try:
            start = os.path.dirname(self._doc.path) if self._doc.path else ""
            path, _ = QFileDialog.getOpenFileName(
                self, "開く", start, "すべてのファイル (*)")
            if path:
                self.open_path(path)
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"開けませんでした ({e})")

    def new_document(self):
        """新しい無題のタブを開く。確認は出さない(今のタブは
        そのまま残る)。"""
        view = self._add_view()
        self._activate_view(view)
        view.active_edit().setFocus()
        self._schedule_session_save()

    # ---------------------------------------------------------------
    # 最近使ったファイル
    # ---------------------------------------------------------------
    def recent_files(self):
        """最近使ったファイルの並び(新しい順)。控えを返す。"""
        return list(self._recent_files)

    def _note_recent_file(self, path):
        """開いた(または名前を付けて保存した)ファイルを履歴の
        先頭へ。並びが変わったときだけ設定へ流す。"""
        if not path:
            return
        updated = remember_recent_file(self._recent_files, path)
        if updated != self._recent_files:
            self._recent_files = updated
            self._persist_settings({RECENT_FILES_KEY: list(updated)})

    def _forget_recent_file(self, path):
        """履歴から1件外す(無くなったファイルを選んだとき)。"""
        key = os.path.normcase(os.path.abspath(path))
        updated = [item for item in self._recent_files
                   if os.path.normcase(os.path.abspath(item)) != key]
        if updated != self._recent_files:
            self._recent_files = updated
            self._persist_settings({RECENT_FILES_KEY: list(updated)})

    def clear_recent_files(self):
        """履歴を全部消す(サブメニューの「履歴を消す」)。"""
        if not self._recent_files:
            return
        self._recent_files = []
        self._persist_settings({RECENT_FILES_KEY: []})

    def open_recent_file(self, path) -> bool:
        """履歴の項目から開く。

        既にタブに開いているファイルなら読み直さず、そのタブを
        前に出す。無くなっているファイルは、ダイアログを出さず
        静かに失敗して、履歴からも外す(タブに残っていても、
        履歴の項目としては外す。文書そのものはタブに残る)。"""
        if not os.path.isfile(path):
            self._forget_recent_file(path)
            self.statusBar().showMessage(
                f"ファイルが見つかりませんでした: {path}", 4000)
            return False
        existing = self.find_view_for_path(path)
        if existing is not None:
            self._activate_view(existing)
            existing.active_edit().setFocus()
            self._note_recent_file(existing.doc.path)
            return True
        return self.open_path(path)

    # ---------------------------------------------------------------
    # 外部で変更されたファイルの監視
    # ---------------------------------------------------------------
    def _update_watcher(self, view=None):
        """そのビューの監視対象を、その文書のファイルに合わせる。"""
        if view is None:
            view = self._current_view()
        if view is None:
            return
        path = view.doc.path
        if view.watched_path and view.watched_path != path:
            view.watcher.removePath(view.watched_path)
            view.watched_path = None
        if path and os.path.exists(path):
            if path not in view.watcher.files():
                view.watcher.addPath(path)
            view.watched_path = path
        else:
            view.watched_path = None

    def _on_watched_file_changed(self, view, _path):
        # 自分の保存(原子的な置き換え)でも発火する。ディスクの印が
        # 控えと一致していれば自分の書き込みなので、監視を張り直す
        # だけ(置き換えで監視が外れるため)。
        if not docmod.external_changed(view.doc):
            self._update_watcher(view)
            return
        if view is self._current_view():
            self._handle_external_change(view)
        else:
            # 裏のタブは印だけ立てて、前に出たときに決着させる
            # (_on_current_tab_changed が _handle_external_change を
            # 呼ぶ)。編集中に裏でダイアログは出さない。
            view.external_change_pending = True

    def _handle_external_change(self, view=None):
        """外部で変わったことへの反応(今のタブについて)。

        未保存の編集が無ければ再読み込みを提案する。編集中なら、
        打鍵の邪魔をしないよう知らせるだけに留め、上書きの可否は
        保存直前の確認(更新時刻のチェック)に任せる。"""
        if view is None:
            view = self._current_view()
        if view is None or view.doc.path is None:
            return
        if not docmod.external_changed(view.doc):
            view.external_change_pending = False
            return
        if view.is_modified():
            view.external_change_pending = True
            self.statusBar().showMessage(
                "このファイルは外部で変更されました"
                "（編集中の内容はそのままです。保存するときに確認します）", 0)
            return
        # 未編集なら、ここで決着させる(再読み込みするかどうかは
        # 利用者が決める)。印は落とすので、断っても次の外部変更
        # までは聞き直さない。
        view.external_change_pending = False
        if self._ask_reload_external():
            self.reload_from_disk()

    @property
    def external_change_pending(self) -> bool:
        """外部変更があり、まだ決着していない(編集中に知らされた)か。"""
        return self._external_change_pending

    def _ask_reload_external(self) -> bool:
        if self.ask_external_reload is not None:
            return bool(self.ask_external_reload())
        answer = QMessageBox.question(
            self, APP_NAME,
            "このファイルは外部で変更されました。\n再読み込みしますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        return answer == QMessageBox.StandardButton.Yes

    # ---------------------------------------------------------------
    # 保存
    # ---------------------------------------------------------------
    def _sync_text(self):
        self._doc.text = self._edit.toPlainText()
        # 行ごとの改行種別も、本文が追随させている今の並びへ合わせる
        # (混在の判定は、読み込み時点の控えではなく今の状態でする)。
        # 控えが無いビュー(控えから戻した下書き等)では、文書側の
        # 控え(読み込み時の並び)をそのまま使う。
        kinds = self._edit.line_newlines()
        if kinds is not None:
            self._doc.line_newlines = kinds
            self._doc.mixed_newlines = docmod.kinds_are_mixed(kinds)

    def _current_line_newline_kinds(self):
        """「混在のまま」保存に使う、行ごとの改行種別の今の並び。

        本文の控えがあればそれ、無ければ文書側の控え(読み込み時の
        並び)。どちらも無ければ None(その場合は文書の改行設定で
        補って書かれる)。"""
        kinds = self._edit.line_newlines()
        if kinds is not None:
            return kinds
        return self._doc.line_newlines

    def _ask_mixed_newline_choice(self):
        """改行が混在した文書を保存するときの3択。

        戻り値は "keep"(混在のまま) / "crlf"(CRLF に統一) /
        "lf"(LF に統一) / None(やめる。窓を閉じた場合もこちら)。
        差し替え口(ask_mixed_newline)があればそちらを使う。"""
        if self.ask_mixed_newline is not None:
            return self.ask_mixed_newline()
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setText(
            "改行コードが混在しています。\n"
            "保存するときの改行をどうしますか？")
        keep_button = box.addButton(
            "混在のまま", QMessageBox.ButtonRole.AcceptRole)
        crlf_button = box.addButton(
            "CRLF に統一", QMessageBox.ButtonRole.AcceptRole)
        lf_button = box.addButton(
            "LF に統一", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep_button:
            return "keep"
        if clicked is crlf_button:
            return "crlf"
        if clicked is lf_button:
            return "lf"
        return None

    def _mixed_newline_save_args(self):
        """明示的な保存(上書き保存・名前を付けて保存)の前に、改行の
        混在をどう扱うか決める。

        混在していなければ {}(引数なし=従来どおり)。混在していれば
        利用者に聞き、決まったら save_to へ渡す引数の dict、やめた
        ら None を返す。ダイアログ無しで書く経路(save_to の直接の
        呼び出しや、grep して置換の一括書き込み)では通らないので、
        そちらでは聞かれない。"""
        self._sync_text()
        if not self._doc.mixed_newlines:
            return {}
        choice = self._ask_mixed_newline_choice()
        if choice == "keep":
            return {"line_newlines": self._current_line_newline_kinds()}
        if choice == "crlf":
            return {"newline": docmod.CRLF}
        if choice == "lf":
            return {"newline": docmod.LF}
        self.statusBar().showMessage(
            "保存をやめました（改行の扱いが選ばれていません）", 8000)
        return None

    def save(self) -> bool:
        """上書き保存。保存先が無ければ「名前を付けて保存」に回す。

        改行が混在していれば、保存の前に3択で聞く(やめたら保存
        しない)。"""
        if self._doc.path is None:
            return self.save_as()
        args = self._mixed_newline_save_args()
        if args is None:
            return False
        return self.save_to(self._doc.path, **args)

    def save_to(self, path, encoding=None, newline=None, bom=None,
                line_newlines=None) -> bool:
        """ダイアログ無しで、指定の形式で書く(呼び側・テスト用)。

        改行の混在の3択はここでは聞かない(聞くのは save /
        save_as の仕事で、決まった結果を newline / line_newlines で
        受け取る)。line_newlines を渡すと、行ごとの改行を保って
        書く(「混在のまま」)。

        保存の前の確認が2つある(どちらも黙って進めない):
          - 上書き先が開いているファイル自身で、開いた後に外部で
            変わっていたら、上書きしてよいか確かめる
          - 保存先の文字コードで書けない文字があれば、置換文字に
            化けるのを承知で書くか確かめる"""
        self._sync_text()
        if (path is not None and self._doc.path is not None
                and os.path.normcase(os.path.abspath(path))
                == os.path.normcase(os.path.abspath(self._doc.path))
                and docmod.external_changed(self._doc)):
            if not self._confirm_external_overwrite():
                self.statusBar().showMessage(
                    "保存をやめました（ファイルが外部で変更されています）",
                    8000)
                return False
        enc = encoding if encoding is not None else self._doc.encoding
        bad = docmod.find_unencodable(self._doc.text, enc)
        errors = "strict"
        if bad:
            if not self._confirm_unencodable_save(bad, enc):
                self.statusBar().showMessage(
                    "保存をやめました（書けない文字があります）", 8000)
                return False
            errors = "replace"
        try:
            docmod.save_document(self._doc, path=path, encoding=encoding,
                                 newline=newline, bom=bom, errors=errors,
                                 line_newlines=line_newlines)
        except (OSError, UnicodeEncodeError, ValueError) as e:
            self.statusBar().showMessage(f"保存できませんでした: {e}")
            return False
        self._edit.document().setModified(False)
        self._external_change_pending = False
        # 描き分けの控えも、実際に書いた内容に合わせ直す
        # (doc.line_newlines は save_document が書いたバイト列から
        # 数え直してある。統一すればそろい、混在のままなら混在のまま)。
        self._edit.set_line_newlines(self._doc.line_newlines,
                                     self._doc.newline)
        # 保存した内容が編集マークの新しい基準になる。
        self._edit.clear_edited_lines()
        # 分割していれば、もう片方の行番号(編集マーク)も合わせ直す。
        self._current_view().refresh_pane_areas()
        self._refresh_all()
        self._update_watcher()
        # 名前を付けて保存して拡張子が付いた(変わった)ら、表示
        # 設定はその拡張子の実効値に切り替える(拡張子別の設定)。
        self._apply_view_display(self._current_view())
        # 名前を付けて保存したら、そのファイルも履歴に入れる
        # (無題の新規文書は、名前が付くまでは入らない)。
        self._note_recent_file(self._doc.path)
        self.statusBar().showMessage("保存しました", 4000)
        return True

    def _confirm_external_overwrite(self) -> bool:
        """開いた後に外部で変わったファイルを上書きしてよいか。"""
        if self.ask_external_overwrite is not None:
            return bool(self.ask_external_overwrite())
        answer = QMessageBox.question(
            self, APP_NAME,
            "このファイルは、開いた後に外部で変更されています。\n"
            "そのまま保存すると、外部での変更は上書きされて消えます。"
            "保存しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _confirm_unencodable_save(self, chars, encoding) -> bool:
        """書けない文字を置換文字にして書いてよいか。"""
        if self.ask_unencodable_save is not None:
            return bool(self.ask_unencodable_save(list(chars)))
        preview = "、".join(chars[:10])
        if len(chars) > 10:
            preview += " …"
        label = text_encoding.ENCODING_LABELS.get(encoding, encoding)
        answer = QMessageBox.question(
            self, APP_NAME,
            f"保存先の文字コード（{label}）では書けない文字があります"
            f"（{preview}）。\n"
            "そのまま保存すると、それらの文字は置換文字に化けます。"
            "保存しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def save_as(self) -> bool:
        try:
            start = self._doc.path or ""
            path, _ = QFileDialog.getSaveFileName(
                self, "名前を付けて保存", start, "すべてのファイル (*)")
            if not path:
                return False
            if self._doc.path is None:
                # 無題の文書は、形式を選ぶ画面を出さない(煩わしい
                # という Nagato の指摘、2026-11-13)。文書が持って
                # いる今の形式(新規文書の既定)でそのまま書く。
                # 形式を変えたいときは、保存したあとにステータス
                # バーの文字コード表示から変える。ただし改行が
                # 混在していれば(貼り付け等で混ざった場合)、3択は
                # ここでも聞く。
                args = self._mixed_newline_save_args()
                if args is None:
                    return False
                return self.save_to(path, **args)
            self._sync_text()
            dialog = SaveOptionsDialog(self._doc, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            encoding, newline, bom = dialog.values()
            if newline is None:
                # 混在した文書で「混在のまま」が選ばれた(形式の
                # 画面にだけある選択)。行ごとの改行を保って書く。
                return self.save_to(
                    path, encoding=encoding, bom=bom,
                    line_newlines=self._current_line_newline_kinds())
            return self.save_to(path, encoding=encoding, newline=newline,
                                bom=bom)
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"保存できませんでした ({e})")
            return False

    # ---------------------------------------------------------------
    # タスクバーの身元(Windows のみ)
    # ---------------------------------------------------------------
    def showEvent(self, event):  # noqa: N802  Qt の上書き
        super().showEvent(event)
        if not self._taskbar_identity_applied:
            self._taskbar_identity_applied = True
            self._apply_taskbar_identity()

    def _apply_taskbar_identity(self) -> None:
        """窓に EDDy 専用の AppUserModelID を付ける(Windows のみ)。

        EDDy の窓は常駐・あっふ と同じプロセスで開くため、付けないと
        タスクバーで開いた側のアプリと同じボタンにまとめられてしまう。
        窓ごとにIDを書けば、EDDy は独立したボタンとグループになる
        (filer_shell.set_window_app_id。Relaunch* は書かないので、
        ピン留めからの再起動は対象外)。Windows でなければ何もしない。
        付けられなくても編集は止めない。"""
        if sys.platform != "win32":
            return
        try:
            import filer_shell

            filer_shell.set_window_app_id(int(self.winId()), TASKBAR_APP_ID)
        except Exception:  # noqa: BLE001  身元が付かないだけ。操作は止めない
            pass

    # ---------------------------------------------------------------
    # 閉じる(確認は出さない。控えが内容を守る)
    # ---------------------------------------------------------------
    def closeEvent(self, event):  # noqa: N802  Qt の上書き
        # 「保存しますか」は出さない。控えはここで書き切るので、
        # 未保存の内容も次に開いたときにタブとして戻る。
        # 遅延保存のたまりも、閉じる前に書き切る。
        # ただし控えが書けないときは別で、そのまま閉じると未保存の
        # 編集が無言で消える。書けなかったら利用者に選ばせる
        # (もう一度書く／そのまま閉じる／閉じない)。
        self._flush_pending_font_size()
        if not self._flush_session() \
                and not self._confirm_close_without_session(event):
            return
        # セッションのスロットは手放す(中身はファイルに残り、
        # 次に開く窓が引き取ってタブを戻す)。
        if self._session_path is not None and self._session_slot is not None:
            try:
                sessionmod.release(self._session_path, self._session_slot)
            except Exception:  # noqa: BLE001  閉じる処理は止めない
                pass
            self._session_slot = None
        # 監視はここで外す。窓の破棄後に監視イベントが届くと
        # (Linux の inotify では)壊れることがあるため。
        for view in self._views:
            watched = view.watcher.files()
            if watched:
                view.watcher.removePaths(watched)
            view.watched_path = None
        g = self.geometry()
        self.closed.emit([g.x(), g.y(), g.width(), g.height()])
        super().closeEvent(event)

    # ---------------------------------------------------------------
    # 編集(標準に無いもの)
    # ---------------------------------------------------------------
    def cut_to_end_of_line(self):
        """行末まで切り取り(命令 cut_to_end、既定は Ctrl+K)。

        選択範囲があれば通常の切り取り。無ければ、カーソル位置から
        行末まで(改行そのものは含めない)を切り取ってクリップボードへ
        入れる。行末にカーソルがあるときは何もしない。削除は1回の
        undo で戻せる単位にする。"""
        cursor = self._edit.textCursor()
        if cursor.hasSelection():
            self._edit.cut()
            return
        if cursor.atBlockEnd():
            return
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                            QTextCursor.MoveMode.KeepAnchor)
        text = cursor.selectedText()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        cursor.beginEditBlock()
        cursor.removeSelectedText()
        cursor.endEditBlock()
        self._edit.setTextCursor(cursor)

    def copy_word_at_cursor(self):
        """単語をコピー(キー割り当ての命令 copy_word、既定は F5)。

        選択範囲があれば、その範囲をコピーする(通常のコピーと
        同じ)。無ければ、カーソル位置の単語をコピーする。単語の
        切り取りは写し用のカーソルで行い、本体のカーソルと選択は
        動かさない。単語が取れない位置(空白の上など)では何もしない。"""
        cursor = self._edit.textCursor()
        if cursor.hasSelection():
            self._edit.copy()
            return
        probe = QTextCursor(cursor)
        probe.select(QTextCursor.SelectionType.WordUnderCursor)
        text = probe.selectedText()
        if text:
            QGuiApplication.clipboard().setText(text)

    def copy_current_line(self):
        """行をコピー(キー割り当ての命令 copy_line、既定は Shift+F5)。

        選択範囲があれば、その範囲をコピーする(通常のコピーと
        同じ)。無ければ、カーソルのある行全体をコピーする。行の
        後ろに改行があれば改行も含める(貼ると行として入る)。文書
        の最後の行で後ろに改行が無いときは、行の文字だけになる。
        カーソルと選択は動かさない。"""
        cursor = self._edit.textCursor()
        if cursor.hasSelection():
            self._edit.copy()
            return
        block = cursor.block()
        text = block.text()
        if block.next().isValid():
            text += "\n"
        QGuiApplication.clipboard().setText(text)

    def copy_append_selection(self):
        """追加コピー(キー割り当ての命令 copy_append、既定は
        Shift+F8)。選択範囲を、クリップボードの今の中身の末尾へ
        足してコピーする。選択が無ければ何もしない。矩形選択の
        ときは矩形の内容を足す(本文側の copy_append が受ける)。"""
        if self._edit.copy_append():
            return
        cursor = self._edit.textCursor()
        if not cursor.hasSelection():
            return
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(
            clipboard.text() + _clipboard_text(cursor.selectedText()))

    def cut_append_selection(self):
        """追加切り抜き(キー割り当ての命令 cut_append、既定は
        Shift+F7)。選択範囲を切り取り、クリップボードの今の中身の
        末尾へ足す。選択が無ければ何もしない。切り取りは1回の
        undo で戻せる単位にする。矩形選択のときは矩形を切り取って
        足す(本文側の cut_append が受ける)。"""
        if self._edit.cut_append():
            return
        cursor = self._edit.textCursor()
        if not cursor.hasSelection():
            return
        clipboard = QGuiApplication.clipboard()
        combined = clipboard.text() + _clipboard_text(
            cursor.selectedText())
        cursor.beginEditBlock()
        cursor.removeSelectedText()
        cursor.endEditBlock()
        self._edit.setTextCursor(cursor)
        clipboard.setText(combined)

    # ---------------------------------------------------------------
    # 命令層(キー割り当てとマクロの再生が同じ命令を呼ぶ)
    # ---------------------------------------------------------------
    def execute_command(self, name, args=None, _note=True) -> bool:
        """名前つき命令を1つ実行する。

        実行できたら True。実行した命令は記録係(MacroRecorder)へ
        渡る(記録中なら列に残り、F4 の「直前の命令」にもなる)。
        再生の最中は _note=False で呼ばれ、記録には混ざらない。
        命令の実体(_cmd_*)は「記録に残す引数」を返し、None なら
        その回は記録しない(空の検索など)。"""
        impl = self._command_impls.get(name)
        if impl is None:
            return False
        self._in_command = True
        try:
            effective = impl(args or {})
        finally:
            self._in_command = False
            self._track_change_cache()
        if _note and effective is not None:
            self._recorder.note_command(name, effective)
        return True

    def repeat_last(self):
        """F4: 直前の命令をもう一度実行する(命令が無ければ何もしない)。
        記録できない操作(矩形編集・マウス移動など)が間に起きていたら、
        控えは消えているので何もしない(_note_macro_skip が消す)。"""
        last = self._recorder.last_command
        if last is None:
            return
        name, args = last
        self.execute_command(name, dict(args))

    def _move_cursor(self, operation, args):
        count = args.get("count", 1)
        try:
            count = max(1, int(count))
        except (TypeError, ValueError):
            count = 1
        extend = bool(args.get("extend"))
        mode = (QTextCursor.MoveMode.KeepAnchor if extend
                else QTextCursor.MoveMode.MoveAnchor)
        cursor = self._edit.textCursor()
        for _ in range(count):
            cursor.movePosition(operation, mode)
        self._edit.setTextCursor(cursor)
        return {"count": count, "extend": extend}

    def _cmd_move_line_start(self, args):
        return self._move_cursor(
            QTextCursor.MoveOperation.StartOfBlock, args)

    def _cmd_move_line_end(self, args):
        return self._move_cursor(
            QTextCursor.MoveOperation.EndOfBlock, args)

    def _cmd_goto_line(self, args):
        """指定行へ移動(命令 goto_line)。line は1始まりで、範囲外は
        文書の範囲に収める。移動そのものは _jump_to_line(grep や
        アウトラインからの移動と同じ作法: 行頭に置いて中央に寄せる)。
        数字として読めない line は None(移動も記録もしない)。記録に
        残るのは、収めたあとの行番号。"""
        try:
            line = int(args.get("line"))
        except (TypeError, ValueError):
            return None
        block_count = self._edit.document().blockCount()
        line = max(1, min(line, max(1, block_count)))
        self._jump_to_line(line - 1)
        return {"line": line}

    def edited_lines(self):
        """編集マークの付いた行(1始まり、昇順)。

        開いて(または保存して)から編集した行。本文がブロック番号で
        持っているものを、利用者向けの行番号に直して返す。"""
        return [number + 1 for number in self._edit.edited_lines()]

    def _cmd_next_edited_line(self, args):
        return self._move_to_edited_line(forward=True)

    def _cmd_prev_edited_line(self, args):
        return self._move_to_edited_line(forward=False)

    def _move_to_edited_line(self, forward):
        """編集マークのある行へ順に移動する(命令 next_edited_line /
        prev_edited_line)。カーソルより後ろ(前)の直近のマークへ
        飛び、端まで行ったら反対側の端に回る。マークが無ければ
        None(移動も記録もしない)。移動そのものは _jump_to_line
        (行頭に置いて中央に寄せる)。"""
        marks = self._edit.edited_lines()
        if not marks:
            return None
        current = self._edit.textCursor().blockNumber()
        if forward:
            target = next((n for n in marks if n > current), marks[0])
        else:
            target = next(
                (n for n in reversed(marks) if n < current), marks[-1])
        self._jump_to_line(target)
        return {}

    def bookmarks(self):
        """ブックマークの付いた行(1始まり、昇順)。

        開いている間だけの印。本文がブロック番号で持っているものを、
        利用者向けの行番号に直して返す。"""
        return [number + 1 for number in self._edit.bookmarks()]

    def _cmd_toggle_bookmark(self, args):
        """カーソル行のブックマークの付け外し(命令 toggle_bookmark)。"""
        line = self._edit.textCursor().blockNumber()
        self._edit.toggle_bookmark(line)
        # 印はペイン間で共有なので、もう片方の行番号も合わせ直す。
        view = self._current_view()
        if view is not None:
            view.refresh_pane_areas()
        return {}

    def _cmd_next_bookmark(self, args):
        return self._move_to_bookmark(forward=True)

    def _cmd_prev_bookmark(self, args):
        return self._move_to_bookmark(forward=False)

    def _move_to_bookmark(self, forward):
        """ブックマークのある行へ順に移動する(命令 next_bookmark /
        prev_bookmark)。カーソルより後ろ(前)の直近の印へ飛び、
        端まで行ったら反対側の端に回る。印が無ければ None(移動も
        記録もしない)。移動そのものは _jump_to_line(行頭に置いて
        中央に寄せる)。"""
        marks = self._edit.bookmarks()
        if not marks:
            return None
        current = self._edit.textCursor().blockNumber()
        if forward:
            target = next((n for n in marks if n > current), marks[0])
        else:
            target = next(
                (n for n in reversed(marks) if n < current), marks[-1])
        self._jump_to_line(target)
        return {}

    def _cmd_move_line_up(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.Up, args)

    def _cmd_move_line_down(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.Down, args)

    def _cmd_move_char_left(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.Left, args)

    def _cmd_move_char_right(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.Right, args)

    def _cmd_move_word_left(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.WordLeft, args)

    def _cmd_move_word_right(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.WordRight, args)

    def _cmd_move_document_start(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.Start, args)

    def _cmd_move_document_end(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.End, args)

    def _cmd_move_page_up(self, args):
        return self._move_page(args, forward=False)

    def _cmd_move_page_down(self, args):
        return self._move_page(args, forward=True)

    def _move_page(self, args, forward):
        """1画面分(見えている行数ぶん)の移動。count は画面の数。
        本文の page_step_lines で数えるので、再生時も押したときと
        同じ行数ぶん動く(窓の大きさが変われば行数も変わる)。"""
        count = args.get("count", 1)
        try:
            count = max(1, int(count))
        except (TypeError, ValueError):
            count = 1
        extend = bool(args.get("extend"))
        mode = (QTextCursor.MoveMode.KeepAnchor if extend
                else QTextCursor.MoveMode.MoveAnchor)
        operation = (QTextCursor.MoveOperation.Down if forward
                     else QTextCursor.MoveOperation.Up)
        cursor = self._edit.textCursor()
        for _ in range(self._edit.page_step_lines() * count):
            cursor.movePosition(operation, mode)
        self._edit.setTextCursor(cursor)
        self._edit.ensureCursorVisible()
        return {"count": count, "extend": extend}

    def _cmd_select_all(self, args):
        self._edit.selectAll()
        return {}

    def _cmd_insert_text(self, args):
        text = str(args.get("text") or "")
        if not text:
            return None
        cursor = self._edit.textCursor()
        cursor.insertText(text)
        self._edit.setTextCursor(cursor)
        return {"text": text}

    def _cmd_delete_backward(self, args):
        return self._delete_chars(args, forward=False)

    def _cmd_delete_forward(self, args):
        return self._delete_chars(args, forward=True)

    def _delete_chars(self, args, forward):
        count = args.get("count", 1)
        try:
            count = max(1, int(count))
        except (TypeError, ValueError):
            count = 1
        cursor = self._edit.textCursor()
        for _ in range(count):
            if forward:
                cursor.deleteChar()
            else:
                cursor.deletePreviousChar()
        self._edit.setTextCursor(cursor)
        return {"count": count}

    def _cmd_delete_selection(self, args):
        cursor = self._edit.textCursor()
        if not cursor.hasSelection():
            return None
        cursor.removeSelectedText()
        self._edit.setTextCursor(cursor)
        return {}

    def _cmd_cut_to_end(self, args):
        self.cut_to_end_of_line()
        return {}

    def _cmd_delete_line(self, args):
        cursor = self._edit.textCursor()
        block = cursor.block()
        pos = block.position()
        text_len = len(block.text())
        if block.next().isValid():
            start, end = pos, pos + text_len + 1       # 改行ごと消す
        elif pos > 0:
            start, end = pos - 1, pos + text_len       # 前の改行ごと
        else:
            start, end = pos, pos + text_len           # 1行だけの文書
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._edit.setTextCursor(cursor)
        return {}

    # ---------------------------------------------------------------
    # 行の並べ替え・重複行の削除・整形・範囲の変換
    # ---------------------------------------------------------------
    def _target_line_range(self):
        """並べ替え・重複削除の対象の (開始ブロック, 終了ブロック)。

        複数行にまたがる選択があれば、その行の範囲(選択の終わりが
        行頭ちょうどのときは、その行は含めない)。無ければ文書全体。
        どちらの場合も、文書の最後のブロックが空(末尾の改行の後ろ)
        なら対象から外す。含めると並べ替えで末尾の改行が消え、
        空行が先頭へ来てしまう。"""
        document = self._edit.document()
        cursor = self._edit.textCursor()
        if not cursor.hasSelection():
            first, last = 0, document.blockCount() - 1
        else:
            start_block = document.findBlock(cursor.selectionStart())
            end_block = document.findBlock(cursor.selectionEnd())
            first = start_block.blockNumber()
            last = end_block.blockNumber()
            if last > first and cursor.selectionEnd() == end_block.position():
                last -= 1
        if (last > first and last == document.blockCount() - 1
                and not document.findBlockByNumber(last).text()):
            last -= 1
        return first, last

    def _replace_line_range(self, first, last, new_lines):
        """first〜last ブロックの本文を new_lines で置き換える。
        1回の undo で戻せる単位にし、カーソルは範囲の頭へ置く。"""
        document = self._edit.document()
        start_block = document.findBlockByNumber(first)
        end_block = document.findBlockByNumber(last)
        start = start_block.position()
        end = end_block.position() + len(end_block.text())
        cursor = self._edit.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.beginEditBlock()
        cursor.insertText("\n".join(new_lines))
        cursor.endEditBlock()
        cursor.setPosition(start)
        self._edit.setTextCursor(cursor)

    def _cmd_sort_lines(self, args):
        order = args.get("order")
        order = "desc" if order == "desc" else "asc"
        first, last = self._target_line_range()
        if last - first < 1:
            return None  # 1行以下では並べ替える意味が無い
        document = self._edit.document()
        lines = [document.findBlockByNumber(n).text()
                 for n in range(first, last + 1)]
        new_lines = sorted(lines, reverse=(order == "desc"))
        if new_lines == lines:
            return None  # 変わらないなら記録も undo も作らない
        self._replace_line_range(first, last, new_lines)
        return {"order": order}

    def _cmd_remove_duplicate_lines(self, args):
        first, last = self._target_line_range()
        document = self._edit.document()
        lines = [document.findBlockByNumber(n).text()
                 for n in range(first, last + 1)]
        seen = set()
        new_lines = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                new_lines.append(line)
        if len(new_lines) == len(lines):
            return None  # 重複が無ければ何もしない
        self._replace_line_range(first, last, new_lines)
        return {}

    def _cmd_reformat_text(self, args):
        """整形(命令 reformat_text)。対象は並べ替えと同じ行範囲
        (選択があればその行、無ければ文書全体)。段落の中の改行を
        取り除いてから、句点で文ごとに改行し直し、行が折り返しの
        桁数を超えるときだけ読点でも改行する。実処理は reformat.py
        (Markdown を壊さない保護込み)。置き換えは行範囲の置換な
        ので、入る改行は文書の改行設定に従い、1回の undo で戻る。
        変わらなければ None(記録も undo も作らない)。"""
        first, last = self._target_line_range()
        document = self._edit.document()
        lines = [document.findBlockByNumber(n).text()
                 for n in range(first, last + 1)]
        # 桁数は、そのタブの実効値(拡張子別の上書きがあればそれ)。
        column = self._effective_display(
            self._current_view())["wrap_column"]
        new_lines = refmtmod.format_lines(lines, column)
        if not new_lines or new_lines == lines:
            return None
        self._replace_line_range(first, last, new_lines)
        return {}

    def _convert_selection(self, func):
        """選択範囲の文字列を func で変換して置き換える。選択が無い、
        または変換で変わらなければ None(記録しない)。"""
        cursor = self._edit.textCursor()
        if not cursor.hasSelection():
            return None
        # 段落の区切りは U+2029 で返るので、変換の前に \n に戻す。
        text = cursor.selectedText().replace("\u2029", "\n")
        new_text = func(text)
        if new_text == text:
            return None
        cursor.beginEditBlock()
        cursor.insertText(new_text)
        cursor.endEditBlock()
        self._edit.setTextCursor(cursor)
        return {}

    def _cmd_convert_halfwidth(self, args):
        return self._convert_selection(convmod.to_halfwidth)

    def _cmd_convert_fullwidth(self, args):
        return self._convert_selection(convmod.to_fullwidth)

    def _cmd_convert_lower(self, args):
        return self._convert_selection(convmod.to_lower)

    def _cmd_convert_upper(self, args):
        return self._convert_selection(convmod.to_upper)

    def _cmd_find_next(self, args):
        return self._find_command(args, forward=True)

    def _cmd_find_prev(self, args):
        return self._find_command(args, forward=False)

    def _find_command(self, args, forward):
        word = args.get("text")
        if word is None:
            word = self._find_edit.text()
        else:
            word = str(word)
        if not word:
            return None
        case = args.get("case")
        case = self._find_case.isChecked() if case is None else bool(case)
        regex = args.get("regex")
        regex = self._find_regex.isChecked() if regex is None else bool(regex)
        self._do_find(forward, word, case, regex)
        return {"text": word, "case": case, "regex": regex}

    # ---------------------------------------------------------------
    # 置換(検索バーとマクロが同じ命令を通る)
    # ---------------------------------------------------------------
    def _cmd_replace_next(self, args):
        return self._replace_command(args, replace_all=False)

    def _cmd_replace_all(self, args):
        return self._replace_command(args, replace_all=True)

    def _replace_options(self, args):
        """置換の材料(検索語・置換語・大小区別・正規表現か)。

        引数で渡ってこなかった分は検索バーの今の値を使う(記録には、
        実際に使った値が全部残る)。"""
        word = args.get("text")
        word = self._find_edit.text() if word is None else str(word)
        repl = args.get("repl")
        repl = self._replace_edit.text() if repl is None else str(repl)
        case = args.get("case")
        case = self._find_case.isChecked() if case is None else bool(case)
        regex = args.get("regex")
        regex = self._find_regex.isChecked() if regex is None else bool(regex)
        return word, repl, case, regex

    def _replace_command(self, args, replace_all):
        word, repl, case, regex = self._replace_options(args)
        if not word:
            return None
        # 置換の最中に、間引き待ちのリアルタイム検索が割り込んで
        # 世代を進めてしまわないよう、先に止める(_do_find と同じ)。
        self._inc_timer.stop()
        text = self._edit.toPlainText()
        if regex and not self._replaying:
            # 正規表現の置換は裏スレッドで回す。結果が返る前に材料は
            # 記録する(見つからなかった回も記録に残るが、再生のとき
            # 見つからなければ何も起きない)。マクロ再生中は、命令の
            # 順序を守るため同期で処理する。
            if replace_all:
                self._start_replace_all_regex(text, word, repl, case)
            else:
                self._start_replace_next_regex(text, word, repl, case)
            return {"text": word, "repl": repl, "case": case, "regex": regex}
        try:
            if replace_all:
                done = self._apply_replace_all(text, word, repl, case, regex)
            else:
                done = self._apply_replace_next(text, word, repl, case, regex)
        except rxfind.RegexFault as e:
            self.statusBar().showMessage(str(e), 8000)
            return None
        if not done:
            self.statusBar().showMessage(f"「{word}」は見つかりません", 4000)
            return None
        return {"text": word, "repl": repl, "case": case, "regex": regex}

    def _start_replace_all_regex(self, text, word, repl, case):
        """正規表現の全部置換(非同期)。結果が返るまでに文書が
        変わっていたら、古い結果で上書きしないよう置換をやめる。"""
        revision = self._edit.document().revision()

        def done(result):
            if self._edit.document().revision() != revision:
                self.statusBar().showMessage(
                    "文書が変わったため、置換をやめました", 4000)
                return
            new_text, count = result["text"], result["count"]
            if not count:
                self.statusBar().showMessage(
                    f"「{word}」は見つかりません", 4000)
                return
            self._suppress_change_record = True
            try:
                cursor = self._edit.textCursor()
                cursor.beginEditBlock()
                cursor.movePosition(QTextCursor.MoveOperation.Start)
                cursor.movePosition(QTextCursor.MoveOperation.End,
                                    QTextCursor.MoveMode.KeepAnchor)
                cursor.insertText(new_text)
                cursor.endEditBlock()
                self._edit.setTextCursor(cursor)
            finally:
                self._suppress_change_record = False
            self._track_change_cache()
            self.statusBar().showMessage(f"{count} 件置換しました", 4000)

        self._run_rx_async("replace_all", done, text=text, pattern=word,
                           repl=repl, regex=True, case_sensitive=case)

    def _start_replace_next_regex(self, text, word, repl, case):
        """正規表現の次置換(非同期)。守りは全部置換と同じ。"""
        cursor = self._edit.textCursor()
        start = self._py_index(cursor.selectionStart(), text)
        revision = self._edit.document().revision()

        def done(result):
            if self._edit.document().revision() != revision:
                self.statusBar().showMessage(
                    "文書が変わったため、置換をやめました", 4000)
                return
            if result["span"] is None:
                self.statusBar().showMessage(
                    f"「{word}」は見つかりません", 4000)
                return
            span = result["span"]
            expanded = result["replacement"]
            begin16 = self._utf16_index(span[0], text)
            end16 = self._utf16_index(span[1], text)
            self._suppress_change_record = True
            try:
                cursor2 = self._edit.textCursor()
                cursor2.setPosition(begin16)
                cursor2.setPosition(end16, QTextCursor.MoveMode.KeepAnchor)
                cursor2.beginEditBlock()
                cursor2.insertText(expanded)
                cursor2.endEditBlock()
                cursor2.setPosition(begin16)
                cursor2.setPosition(begin16 + self._utf16_len(expanded),
                                    QTextCursor.MoveMode.KeepAnchor)
                self._edit.setTextCursor(cursor2)
            finally:
                self._suppress_change_record = False
            self._track_change_cache()

        self._run_rx_async("replace_one", done, text=text, pattern=word,
                           repl=repl, start=start, regex=True,
                           case_sensitive=case)

    def _apply_replace_all(self, text, word, repl, case, regex):
        """全部置換して、文書へ一括で反映する。置換したら True。"""
        if regex:
            result = rxfind.run_operation(
                "replace_all", text=text, pattern=word, repl=repl,
                regex=True, case_sensitive=case)
            new_text, count = result["text"], result["count"]
        else:
            rx = rxfind.compile_search(word, regex=False,
                                       case_sensitive=case)
            new_text, count = rxfind.replace_all_text(
                text, rx, repl, regex=False)
        if not count:
            return False
        cursor = self._edit.textCursor()
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(QTextCursor.MoveOperation.End,
                            QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(new_text)
        cursor.endEditBlock()
        self._edit.setTextCursor(cursor)
        self.statusBar().showMessage(f"{count} 件置換しました", 4000)
        return True

    def _apply_replace_next(self, text, word, repl, case, regex):
        """カーソル(選択の開始)以後の最初のマッチを1件だけ置換する。
        置換したら True。置き換えた箇所を選択状態にする。"""
        cursor = self._edit.textCursor()
        start = self._py_index(cursor.selectionStart(), text)
        if regex:
            result = rxfind.run_operation(
                "replace_one", text=text, pattern=word, repl=repl,
                start=start, regex=True, case_sensitive=case)
            if result["span"] is None:
                return False
            span = result["span"]
            expanded = result["replacement"]
        else:
            rx = rxfind.compile_search(word, regex=False,
                                       case_sensitive=case)
            span = rxfind.find_span_next(text, rx, start)
            if span is None:
                return False
            match = rx.match(text, span[0])
            expanded = rxfind.expand_replacement(match, repl, regex=False)
        begin16 = self._utf16_index(span[0], text)
        end16 = self._utf16_index(span[1], text)
        cursor.setPosition(begin16)
        cursor.setPosition(end16, QTextCursor.MoveMode.KeepAnchor)
        cursor.beginEditBlock()
        cursor.insertText(expanded)
        cursor.endEditBlock()
        cursor.setPosition(begin16)
        cursor.setPosition(begin16 + self._utf16_len(expanded),
                           QTextCursor.MoveMode.KeepAnchor)
        self._edit.setTextCursor(cursor)
        return True

    # ---------------------------------------------------------------
    # 位置の単位の変換(Python の文字列の位置 ⇔ QTextCursor の位置)
    # ---------------------------------------------------------------
    @staticmethod
    def _utf16_len(text):
        """QTextCursor の単位(UTF-16 のコード単位)での長さ。"""
        return len(text.encode("utf-16-le")) // 2

    def _utf16_index(self, py_index, text):
        """Python の文字列の位置を、QTextCursor の位置へ。

        絵文字(サロゲートペア)があると両者はずれる。第2段のマクロ
        記録と同じ理由で、正規表現の結果( Python 側の位置)を本文へ
        戻すときに要る。"""
        return self._utf16_len(text[:py_index])

    def _py_index(self, utf16_index, text):
        """QTextCursor の位置を、Python の文字列の位置へ。"""
        data = text.encode("utf-16-le")
        return len(data[:utf16_index * 2].decode("utf-16-le"))

    # ---------------------------------------------------------------
    # マクロの記録(文書の変化と移動キーの切り分け)
    # ---------------------------------------------------------------
    def _track_change_cache(self, view=None):
        """そのビューの「直前の選択・カーソル」の控えを取り直す。"""
        if view is None:
            view = self._current_view()
        if view is not None:
            view.track_change_cache()

    def _on_move_key(self, name, extend):
        # 移動は本文が標準で済ませている。ここでは「起きたこと」と
        # して記録係へ渡すだけ(実行はしない)。
        self._recorder.note_command(name, {"count": 1, "extend": bool(extend)})

    def _on_command_key(self, name, extend):
        # PageUp / PageDown / Ctrl+A は、命令層で実行する(本文の
        # keyPressEvent が command_key で寄せてくる)。
        self.execute_command(name, {"extend": bool(extend)})

    def _note_macro_skip(self):
        """記録できない操作が起きたときの共通の口。

        その操作が「直前の操作」になるので、F4 の控えは記録中か
        どうかによらず消す(残すと、F4 がその前の命令を繰り返して
        見た目と違う結果になる。第2段レビューの残件)。
        件数を数えて知らせるのは記録中だけ(再生結果が実物と違う
        ことに利用者が気づけるように)。"""
        self._recorder.clear_last_command()
        if not self._recorder.recording:
            return
        self._macro_skips += 1
        self.statusBar().showMessage(
            f"● マクロ記録中（記録できない操作 {self._macro_skips} 件）", 0)

    def _on_document_change_for_macro(self, view, pos, removed, added):
        try:
            if not (self._in_command or self._replaying
                    or view.suppress_change_record):
                doc = view.edit.document()
                sel_start, sel_end, cursor_before = view.change_cache
                selection_replaced = (
                    removed > 0
                    and sel_start is not None and sel_end is not None
                    and sel_start == pos and sel_end == pos + removed)
                if (not selection_replaced
                        and pos == 0 and added > 0
                        and added >= doc.characterCount() - 1
                        and (removed > 0 or added > 1)):
                    # 文書の丸ごと差し替え(ファイルの読み込み等)は
                    # 編集の記録にしない。ただし記録中に起きたら、
                    # 知らせる対象にする。条件の最後の節は、空の文書
                    # への最初の打鍵(removed=0, added=1)を差し替えと
                    # 間違えないため。選択範囲の上書き(全選択して
                    # 打ち直す等)は、選択が変わった範囲と一致する
                    # のでここには入れず、下の切り分けで「選択範囲の
                    # 削除 + 挿入」として記録する。
                    self._recorder.break_insert_chain()
                    self._note_macro_skip()
                elif (added == 0 and removed == 1 and pos == 0
                        and sel_start == sel_end and cursor_before == 0
                        and doc.characterCount() <= 1):
                    # 差し替えの前半で出る「空にする削除」も記録
                    # しない(実体は末尾の区切り文字だけ)。代償として、
                    # 1文字だけの文書での Del は記録に残らない。
                    self._recorder.break_insert_chain()
                    self._note_macro_skip()
                else:
                    inserted = ""
                    if added > 0:
                        # pos / added は UTF-16 の単位。Python の文字列
                        # の位置とは絵文字(サロゲートペア)でずれるので、
                        # 挿入された文字列はカーソルで取る。
                        cursor = QTextCursor(doc)
                        cursor.setPosition(pos)
                        cursor.setPosition(
                            pos + added, QTextCursor.MoveMode.KeepAnchor)
                        inserted = cursor.selectedText().replace(
                            "\u2029", "\n")
                    recorded = self._recorder.feed_change(
                        pos, removed, added, inserted,
                        sel_start, sel_end, cursor_before)
                    if not recorded:
                        # 選択範囲を打って上書きする入力、矩形選択での
                        # 編集など、命令に切り分けられなかった変化。
                        # 黙って外さず知らせる(記録中は件数。F4 の
                        # 控えは記録中でなくても消える)。
                        self._note_macro_skip()
        except Exception:  # noqa: BLE001  記録の失敗で編集を止めない
            pass
        finally:
            self._track_change_cache(view)

    def _undo_edit(self):
        self._suppress_change_record = True
        try:
            self._edit.undo()
        finally:
            self._suppress_change_record = False
            self._track_change_cache()

    def _redo_edit(self):
        self._suppress_change_record = True
        try:
            self._edit.redo()
        finally:
            self._suppress_change_record = False
            self._track_change_cache()

    # ---------------------------------------------------------------
    # マクロ(記録・再生・保存)
    # ---------------------------------------------------------------
    @property
    def macro_recording(self) -> bool:
        return self._recorder.recording

    def recorded_commands(self) -> list:
        """記録中の命令の列(写し)。"""
        return [dict(entry, args=dict(entry.get("args") or {}))
                for entry in self._recorder.commands]

    def set_macro_recording(self, on):
        on = bool(on)
        if on == self._recorder.recording:
            return
        if on:
            self._recorder.start()
            self._macro_skips = 0
            self._act_macro_record.setText("記録終了(&R)")
            self.statusBar().showMessage("● マクロ記録中", 0)
        else:
            self._recorder.stop()
            self._current_macro = self.recorded_commands()
            self._current_macro_name = ""
            self._current_python_source = None
            self._act_macro_record.setText("記録開始(&R)")
            message = (f"マクロの記録を終了しました"
                       f"（{len(self._current_macro)} 手）")
            if self._macro_skips:
                message += (f"。記録できない操作が {self._macro_skips} 件"
                            "あり、再生結果が実物と違う場合があります")
            self.statusBar().showMessage(message, 8000)
        action = getattr(self, "_act_macro_record", None)
        if action is not None and action.isChecked() != on:
            blocker = QSignalBlocker(action)
            action.setChecked(on)

    def play_macro(self, commands=None) -> bool:
        """マクロを再生する。commands が無ければ、最後に記録・読み
        込みしたマクロ。再生全体は1回の undo で戻せる単位にする。"""
        if commands is None and self._current_python_source:
            return self.play_python_macro(self._current_python_source)
        if commands is None:
            commands = self._current_macro
        if not commands:
            self.statusBar().showMessage("再生するマクロがありません", 4000)
            return False
        self._replaying = True
        cursor = self._edit.textCursor()
        cursor.beginEditBlock()
        try:
            for entry in commands:
                self.execute_command(entry["cmd"],
                                     dict(entry.get("args") or {}),
                                     _note=False)
        finally:
            cursor.endEditBlock()
            self._replaying = False
            self._track_change_cache()
        return True

    # ---------------------------------------------------------------
    # Python マクロ(editor/macro_py.py)
    # ---------------------------------------------------------------
    def _make_python_session(self, source):
        """Python マクロの実行セッションを作る。文法が読めなければ
        ステータスに出して None。"""
        try:
            return macro_py.PythonStepper(
                source, _EditorPyApi(self).api_object())
        except macro_py.PythonMacroError as e:
            self.statusBar().showMessage(
                f"Python マクロのエラー: {e}", 8000)
            return None

    def _begin_python_run(self):
        """Python マクロの実行区間の開始(undo の単位と、再生中の印)。"""
        self._replaying = True
        self._python_block_cursor = self._edit.textCursor()
        self._python_block_cursor.beginEditBlock()

    def _end_python_run(self):
        if self._python_block_cursor is not None:
            self._python_block_cursor.endEditBlock()
            self._python_block_cursor = None
        self._replaying = False
        self._track_change_cache()

    def play_python_macro(self, source) -> bool:
        """Python のマクロを通しで実行する。

        実行は EDDy の中だけで完結する(外部プロセスには投げない)。
        全体は1回の undo で戻せる単位にする(play_macro と同じ)。
        エラーは行番号つきでステータスに出す。"""
        session = self._make_python_session(source)
        if session is None:
            return False
        self._begin_python_run()
        try:
            session.run_all()
        except macro_py.PythonMacroError as e:
            self.statusBar().showMessage(
                f"Python マクロのエラー: {e}", 8000)
            return False
        finally:
            self._end_python_run()
        if session.output:
            self.statusBar().showMessage(
                "print: " + " / ".join(session.output), 8000)
        return True

    def show_python_macro_dialog(self, source=None, name="",
                                 on_save=None):
        """マクロの Python を別窓で見て・直して・実行する画面を開く。

        source が無ければ、いまのマクロ(記録した命令の列を Python
        へ変換したもの)を開く。on_save は (名前, ソース) を受ける
        登録の口で、渡されなければ「登録マクロとして新規に残す」
        口を使う(登録マクロから開いたときは、呼び側がキーの
        引き継ぎなどの都合を付けて渡す)。"""
        if on_save is None:
            def on_save(name, new_source):
                self.register_python_macro(name, new_source)
        if source is None:
            if self._current_python_source:
                source = self._current_python_source
            else:
                commands = (self.recorded_commands()
                            if self._recorder.recording
                            else self._current_macro)
                source = macro_py.commands_to_python(
                    commands, self._current_macro_name or "マクロ")
        dialog = MacroPythonDialog(
            self, source=source, name=name or self._current_macro_name,
            session_factory=self._make_python_session,
            begin_run=self._begin_python_run,
            end_run=self._end_python_run,
            on_save=on_save)
        dialog.exec()

    def save_macro(self, path, name="") -> bool:
        """いまのマクロ(記録中なら記録中の列)を書く。

        拡張子が .py なら Python、.txt ならテキスト形式(1行1命令)、
        それ以外は従来の JSON で書く。いまのマクロが Python の
        ときは、.py にそのまま書く(Python → 命令の列への変換は
        しないので、他の形式には書けない)。"""
        lowered = str(path).lower()
        if (not self._recorder.recording
                and self._current_python_source):
            if not lowered.endswith(".py"):
                self.statusBar().showMessage(
                    "Python のマクロは .py で保存してください", 6000)
                return False
            try:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(self._current_python_source)
            except OSError as e:
                self.statusBar().showMessage(
                    f"マクロを保存できませんでした: {e}")
                return False
            return True
        commands = (self.recorded_commands() if self._recorder.recording
                    else self._current_macro)
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                if lowered.endswith(".py"):
                    f.write(macro_py.commands_to_python(commands, name))
                elif lowered.endswith(".txt"):
                    f.write(macmod.commands_to_text(commands, name))
                else:
                    f.write(macmod.macro_to_json(commands, name))
        except OSError as e:
            self.statusBar().showMessage(f"マクロを保存できませんでした: {e}")
            return False
        return True

    def load_macro(self, path) -> list:
        """マクロのファイルを読み、いまのマクロにする。命令の列を
        返す(Python のマクロは命令の列が無いので空を返し、ソースは
        _current_python_source に持つ)。形式は中身で見分ける
        ({ か [ で始まれば JSON、それ以外はテキスト形式として
        読んでみて、読めなければ Python として読む)。読めなければ
        ValueError / OSError を上げる(呼び側が伝える)。"""
        with open(path, encoding="utf-8") as f:
            text = f.read()
        # 先頭の BOM と空白を除いて、{ か [ で始まれば JSON。
        head = text.lstrip("\ufeff \t\r\n")
        if head.startswith(("{", "[")):
            name, commands = macmod.macro_from_json(text)
            self._current_python_source = None
        else:
            try:
                name, commands = macmod.commands_from_text(text)
            except ValueError:
                # テキスト形式で読めなければ、Python として読む
                # (文法が読めなければ、そちらのエラーを上げる)。
                invalid = macro_py.validate_python(text)
                if invalid is not None:
                    raise ValueError(invalid)
                self._current_macro = []
                self._current_macro_name = (
                    os.path.splitext(os.path.basename(str(path)))[0])
                self._current_python_source = text
                return []
            self._current_python_source = None
        self._current_macro = commands
        self._current_macro_name = name
        return commands

    def save_macro_dialog(self):
        try:
            path, _ = QFileDialog.getSaveFileName(
                self, "マクロを保存", "",
                "マクロ (*.json *.txt *.py);;すべてのファイル (*)")
            if not path:
                return
            name = os.path.splitext(os.path.basename(path))[0]
            if self.save_macro(path, name):
                self.statusBar().showMessage("マクロを保存しました", 4000)
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"マクロを保存できませんでした ({e})")

    def load_macro_dialog(self):
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, "マクロを開く", "",
                "マクロ (*.json *.txt *.py);;すべてのファイル (*)")
            if not path:
                return
            commands = self.load_macro(path)
        except Exception as e:  # noqa: BLE001  理由は利用者への文にする
            self.statusBar().showMessage(f"マクロを読めませんでした: {e}")
            return
        # Python のマクロは命令の列が空で返るので、その場合は
        # 「いまのマクロ」の再生(play_macro の Python 振り分け)に任せる。
        self.play_macro(commands if commands else None)

    # ---------------------------------------------------------------
    # 登録マクロ(名前を付けて設定に残し、メニューとキーで呼ぶ)
    # ---------------------------------------------------------------
    def registered_macros(self) -> dict:
        """登録マクロの対応(名前 → {"commands", "key"} または
        {"python", "key"})の写し。"""
        return macmod.copy_registered(self._registered_macros)

    def apply_registered_macros(self, mapping):
        """登録マクロの変更を反映して保存する。

        管理画面(editor/macro_dialog.py)からの変更を受ける口。
        メニューの項目とショートカットを付け直し、保存は macros
        キーへの差分のみ(丸ごと書き換えはしない)。"""
        self._registered_macros = macmod.sanitize_registered(mapping)
        self._rebuild_registered_macro_menu()
        self._persist_settings(
            {MACROS_KEY: macmod.copy_registered(self._registered_macros)})

    def register_macro(self, name, commands, key=""):
        """いまのマクロなどを、名前を付けて登録する。"""
        mapping = self.registered_macros()
        mapping[str(name)] = {"commands": macmod.copy_commands(commands),
                              "key": key}
        self.apply_registered_macros(mapping)

    def register_python_macro(self, name, source, key=""):
        """Python のマクロを、名前を付けて登録する。"""
        mapping = self.registered_macros()
        mapping[str(name)] = {"python": source, "key": key}
        self.apply_registered_macros(mapping)

    def play_registered_macro(self, name) -> bool:
        """登録マクロを再生する。無ければステータスに出して False。"""
        entry = self._registered_macros.get(name)
        if entry is None:
            self.statusBar().showMessage(
                f"登録マクロがありません（{name}）", 4000)
            return False
        if "python" in entry:
            self._current_python_source = entry["python"]
            self._current_macro_name = name
            return self.play_python_macro(entry["python"])
        self._current_python_source = None
        self._current_macro = macmod.copy_commands(entry["commands"])
        self._current_macro_name = name
        return self.play_macro(self._current_macro)

    def show_macro_library(self):
        """マクロの管理画面(editor/macro_dialog.py)を開く。

        変更はその場でメニューへ反映され、設定へ保存される
        (apply_registered_macros 経由)。画面で「実行」を押したら、
        閉じたあとにそのマクロを再生する。"""
        reserved = {}
        for cmd_name, keys in self._keymap.items():
            if keys:
                reserved[keys] = keymapmod.command_label(cmd_name)
        dialog = MacroLibraryDialog(
            self, macros=self._registered_macros,
            reserved_keys=reserved,
            current_commands=lambda: (
                self.recorded_commands() if self._recorder.recording
                else macmod.copy_commands(self._current_macro)),
            on_change=self.apply_registered_macros)
        dialog.exec()
        if dialog.run_requested:
            self.play_registered_macro(dialog.run_requested)
        if dialog.python_requested:
            self._open_registered_macro_python(dialog.python_requested)

    def _open_registered_macro_python(self, name):
        """登録マクロを Python の別窓で開く。

        Python の登録はそのソースを、命令の列の登録は Python へ
        変換したものを開く。別窓で保存すると、Python の登録として
        残る(キーは引き継ぐ)。"""
        entry = self._registered_macros.get(name)
        if entry is None:
            return
        key = entry.get("key") or ""
        if "python" in entry:
            source = entry["python"]
        else:
            source = macro_py.commands_to_python(
                entry.get("commands") or [], name)

        def save_python(new_name, new_source):
            self.register_python_macro(new_name, new_source, key=key)

        self.show_python_macro_dialog(
            source=source, name=name, on_save=save_python)

    def _rebuild_registered_macro_menu(self):
        """登録マクロのメニュー項目とショートカットを作り直す。

        ショートカットは、組み込みの命令が使っているキーとは付け
        ない(組み込み優先)。登録どうしで重なったら先の登録が勝つ
        (管理画面では後勝ちで解消されるので、重なりが残るのは
        設定を手で書き換えた場合だけ)。"""
        menu = self._macro_menu
        for item in self._registered_menu_items:
            if menu is not None:
                menu.removeAction(item)
            item.deleteLater()
        self._registered_menu_items = []
        self._registered_macro_actions = {}
        if menu is None:
            return
        if self._registered_macros:
            self._registered_menu_items.append(menu.addSeparator())
        used = set()
        for keys in self._keymap.values():
            if keys:
                normalized = QKeySequence(keys).toString(
                    QKeySequence.SequenceFormat.PortableText)
                if normalized:
                    # 照合は正規化した形で(表記ゆれで重なりを
                    # 見落とさないため)。
                    used.add(normalized.split(", ")[0])
        for name, entry in self._registered_macros.items():
            action = QAction(name, self)
            keys = (entry.get("key") or "").strip()
            normalized = (QKeySequence(keys).toString(
                QKeySequence.SequenceFormat.PortableText) if keys else "")
            if normalized and normalized not in used:
                action.setShortcut(QKeySequence(normalized))
                used.add(normalized)
            action.setShortcutContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut)
            for view in self._views:
                for edit in view.panes():
                    edit.addAction(action)
            action.triggered.connect(
                lambda _checked=False, n=name:
                self.play_registered_macro(n))
            menu.addAction(action)
            self._registered_menu_items.append(action)
            self._registered_macro_actions[name] = action

    # ---------------------------------------------------------------
    # 検索
    # ---------------------------------------------------------------
    def show_find_bar(self):
        self._reset_incremental_search()
        self._find_bar.show()
        self._find_edit.setFocus()
        self._find_edit.selectAll()
        self._refresh_match_highlights()

    def _hide_find_bar(self):
        self._reset_incremental_search()
        self._find_bar.hide()
        self._clear_match_highlights()
        self._edit.setFocus()

    # ---------------------------------------------------------------
    # 指定行ジャンプ
    # ---------------------------------------------------------------
    def ask_goto_line(self):
        """行番号を聞いて、その行へ移動する(編集メニューと Ctrl+G)。

        入力は折り返し桁数の設定と同じ QInputDialog の小さな窓。
        Enter で移動して閉じ、Esc では何もせず閉じる(数字でない
        入力は、入力欄の検証で確定できない)。範囲は文書の行数まで
        (命令側でも収めるので、再生時の範囲外はそちらで収まる)。"""
        try:
            block_count = self._edit.document().blockCount()
            current = self._edit.textCursor().blockNumber() + 1
            value, ok = QInputDialog.getInt(
                self, APP_NAME, "移動する行", current,
                1, max(1, block_count), 1)
            if ok:
                self.execute_command("goto_line", {"line": value})
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"移動できませんでした ({e})")

    # ---------------------------------------------------------------
    # ブックマークの一覧
    # ---------------------------------------------------------------
    def show_bookmark_dialog(self):
        """ブックマークの一覧の窓を開く。

        項目は開いた時点の印と行の内容(この窓はモーダルなので、
        開いている間に本文は変わらない)。選んだ行への移動は
        _jump_to_line(行頭に置いて中央に寄せる)。"""
        try:
            document = self._edit.document()
            entries = []
            for number in self._edit.bookmarks():
                block = document.findBlockByNumber(number)
                entries.append(
                    (number + 1, block.text() if block.isValid() else ""))
            dialog = BookmarkDialog(
                self, entries=entries,
                on_jump=lambda line: self._jump_to_line(max(0, line - 1)))
            dialog.exec()
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(
                f"ブックマークを開けませんでした ({e})")

    # ---------------------------------------------------------------
    # フォルダ内の横断検索(grep)
    # ---------------------------------------------------------------
    def show_grep_dialog(self):
        try:
            folder = (os.path.dirname(self._doc.path)
                      if self._doc.path else "")
            word = self._find_edit.text()
            if not word:
                cursor = self._edit.textCursor()
                if cursor.hasSelection():
                    word = cursor.selectedText().replace("\u2029", "\n")
                    if "\n" in word:
                        word = ""
            pending = []
            dialog = GrepDialog(
                self, folder=folder, word=word,
                case_sensitive=self._find_case.isChecked(),
                on_jump=self.open_path_at_line,
                on_replace=lambda f, w, c: pending.append((f, w, c)))
            dialog.exec()
            if pending:
                f, w, c = pending[0]
                self.show_grep_replace_dialog(
                    folder=f, word=w, case_sensitive=c)
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"検索を開けませんでした ({e})")

    def show_grep_replace_dialog(self, folder=None, word=None,
                                 case_sensitive=None):
        """フォルダ内の横断置換(grep して置換)の窓を開く。

        条件の初期値は検索バーから引き継ぐ(フォルダは今の文書の
        フォルダ、語が空なら選択中の文字列。grep の窓と同じ作法)。
        開いているファイルは対象から外すため、開いているパスの
        取得関数を渡す。"""
        try:
            if folder is None:
                folder = (os.path.dirname(self._doc.path)
                          if self._doc.path else "")
            if word is None:
                word = self._find_edit.text()
                if not word:
                    cursor = self._edit.textCursor()
                    if cursor.hasSelection():
                        word = cursor.selectedText().replace("\u2029", "\n")
                        if "\n" in word:
                            word = ""
            if case_sensitive is None:
                case_sensitive = self._find_case.isChecked()
            dialog = GrepReplaceDialog(
                self, folder=folder, word=word,
                repl=self._replace_edit.text(),
                case_sensitive=case_sensitive,
                regex=self._find_regex.isChecked(),
                open_paths=self.open_paths,
                on_jump=self.open_path_at_line)
            dialog.exec()
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"置換を開けませんでした ({e})")

    # ---------------------------------------------------------------
    # 文書内のすべて検索(ヒットした行の一覧)
    # ---------------------------------------------------------------
    def show_find_all_dialog(self):
        """今の文書のすべて検索の窓を開く。

        検索の語と条件(大小区別・正規表現)は検索バーから引き継ぐ
        (語が空なら、選択中の1行を語にする。grep の窓と同じ作法)。
        選んだ行への移動は _jump_to_line(行頭に置いて中央に寄せる)。"""
        try:
            word = self._find_edit.text()
            if not word:
                cursor = self._edit.textCursor()
                if cursor.hasSelection():
                    word = cursor.selectedText().replace("\u2029", "\n")
                    if "\n" in word:
                        word = ""
            dialog = FindAllDialog(
                self, text=self._edit.toPlainText(), word=word,
                case_sensitive=self._find_case.isChecked(),
                regex=self._find_regex.isChecked(),
                on_jump=lambda line: self._jump_to_line(max(0, line - 1)))
            dialog.exec()
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"検索を開けませんでした ({e})")

    def open_path_at_line(self, path, line) -> bool:
        """ファイルを開いて、行(1始まり)へ飛ぶ。grep の結果から使う。

        既に同じファイルをタブに開いていれば、そのタブを前に
        出して飛ぶだけ。別のファイルなら新しいタブで開く。"""
        existing = self.find_view_for_path(path)
        if existing is not None:
            self._activate_view(existing)
            self._jump_to_line(max(0, line - 1), existing)
            return True
        if not self.open_path(path):
            return False
        self._jump_to_line(max(0, line - 1))
        return True

    # ---------------------------------------------------------------
    # 検索の一致箇所すべての色付け
    # ---------------------------------------------------------------
    def _refresh_match_highlights(self):
        """いまの検索語に一致する箇所すべてへ色を付ける。

        現在のヒットは選択の色で見分けるため、色付けからは除く
        (_apply_match_highlights)。検索バーが出ていない・語が
        空のときは消す。通常検索はその場で数え(文字列の走査は
        速い)、正規表現は別プロセスの find_all に任せる(固まる
        式はそちらの時間切れが守る)。件数の上限は
        SEARCH_HIGHLIGHT_LIMIT で、超えたら先頭から上限まで。"""
        view = self._current_view()
        if view is None:
            return
        if not self._find_bar.isVisible() or not self._find_edit.text():
            self._clear_match_highlights()
            return
        word = self._find_edit.text()
        case_sensitive = self._find_case.isChecked()
        text = view.edit.toPlainText()
        if self._find_regex.isChecked():
            try:
                rxfind.compile_search(word, regex=True,
                                      case_sensitive=case_sensitive)
            except rxfind.RegexFault:
                self._clear_match_highlights()
                return
            revision = view.edit.document().revision()

            def done(result, view=view, word=word, text=text,
                     revision=revision):
                if self._current_view() is not view:
                    return
                if not self._find_bar.isVisible() \
                        or self._find_edit.text() != word:
                    return
                if view.edit.document().revision() != revision:
                    # 計算の間に編集された。付け直しは編集側の
                    # 間引きタイマー(_highlight_timer)が担う。
                    return
                self._apply_match_highlights(
                    view, self._spans_to_utf16(result["spans"], text))

            self._run_rx_async(
                "find_all", done, text=text, pattern=word,
                regex=True, case_sensitive=case_sensitive,
                limit=SEARCH_HIGHLIGHT_LIMIT)
            return
        spans = []
        start = 0
        while len(spans) < SEARCH_HIGHLIGHT_LIMIT:
            index = docmod.find_next(text, word, start, case_sensitive)
            if index < 0:
                break
            spans.append((index, index + len(word)))
            start = index + max(1, len(word))
        self._apply_match_highlights(
            view, self._spans_to_utf16(spans, text))

    def _spans_to_utf16(self, spans, text):
        """(開始, 終了) の並びを Python の位置から QTextCursor の
        位置へ。本文にサロゲートペアが無ければ両者は一致する
        ので、その場合は変換しない(件数が多いときの節約)。"""
        if self._utf16_len(text) == len(text):
            return list(spans)
        return [(self._utf16_index(start, text),
                 self._utf16_index(end, text)) for start, end in spans]

    def _apply_match_highlights(self, view, spans):
        """色付けの範囲を、ビューの両ペインへ反映する。

        前に別のビューへ付けていた分は消す(色は今のタブだけに
        付ける)。現在のヒット(アクティブなペインで選択中の範囲
        と一致するもの)は、選択の色で見分けるため除く。"""
        if self._highlight_view is not None \
                and self._highlight_view is not view:
            for pane in self._highlight_view.panes():
                pane.set_match_highlights([])
        self._highlight_view = view
        cursor = view.active_edit().textCursor()
        if cursor.hasSelection():
            current = (cursor.selectionStart(), cursor.selectionEnd())
            if current in spans:
                spans.remove(current)
        for pane in view.panes():
            pane.set_match_highlights(spans)

    def _clear_match_highlights(self):
        """色付けをすべて消す(検索バーを閉じたとき)。"""
        self._highlight_timer.stop()
        if self._highlight_view is not None:
            for pane in self._highlight_view.panes():
                pane.set_match_highlights([])
            self._highlight_view = None
        view = self._current_view()
        if view is not None:
            for pane in view.panes():
                pane.set_match_highlights([])

    def _do_find(self, forward: bool, word=None, case_sensitive=None,
                 regex=None) -> bool:
        """検索の実体。word / case_sensitive / regex が None なら検索欄の
        値を使う(命令層からは、記録・再生のために使った値を渡してくる)。"""
        # 間引き待ちのリアルタイム検索が残っていたら止める(Enter を
        # 押した直後に、遅れて来たリアルタイム検索が選択を戻して
        # しまわないように)。
        self._inc_timer.stop()
        if word is None:
            word = self._find_edit.text()
        if not word:
            return False
        if case_sensitive is None:
            case_sensitive = self._find_case.isChecked()
        if regex is None:
            regex = self._find_regex.isChecked()
        if regex:
            return self._do_find_regex(forward, word, case_sensitive)
        text = self._edit.toPlainText()
        cursor = self._edit.textCursor()
        if forward:
            start = (cursor.selectionEnd() if cursor.hasSelection()
                     else cursor.position())
            index = docmod.find_next(text, word, start, case_sensitive)
            if index < 0:
                index = docmod.find_next(text, word, 0, case_sensitive)
        else:
            start = (cursor.selectionStart() if cursor.hasSelection()
                     else cursor.position())
            index = docmod.find_prev(text, word, start, case_sensitive)
            if index < 0:
                index = docmod.find_prev(text, word, len(text), case_sensitive)
        if index < 0:
            self.statusBar().showMessage(f"「{word}」は見つかりません", 4000)
            self._refresh_match_highlights()
            return False
        cursor.setPosition(index)
        cursor.setPosition(index + len(word), QTextCursor.MoveMode.KeepAnchor)
        self._edit.setTextCursor(cursor)
        self._edit.centerCursor()
        self._refresh_match_highlights()
        return True

    # ---------------------------------------------------------------
    # リアルタイム検索(打っている途中からヒットへ飛ぶ)
    # ---------------------------------------------------------------
    def _reset_incremental_search(self):
        """リアルタイム検索の状態を初期化する(検索バーを開く・
        閉じるときと、検索語が空になったとき)。"""
        self._inc_timer.stop()
        self._inc_base = None
        self._inc_span = None
        self._set_find_not_found(False)

    def _set_find_not_found(self, not_found):
        """ヒットが無いときは検索欄の背景を薄い赤にする。見つ
        かったとき・語が空のときは元の見た目に戻す(ステータス
        バーの「見つかりません」と併用する、この窓の作法)。"""
        self._find_edit.setStyleSheet(
            "background-color: #ffd7d7;" if not_found else "")

    def _mark_incremental_not_found(self, word):
        self._set_find_not_found(True)
        self.statusBar().showMessage(f"「{word}」は見つかりません", 4000)
        # 一致が無いので、色付けも付け直して消す。
        self._refresh_match_highlights()

    def _on_find_edited(self, _text):
        """検索欄への入力のたびに呼ばれる。textEdited は人が打った
        ときだけ来る(setText では来ない)ので、マクロ再生やプロ
        グラムからの設定では走らない。"""
        self._request_incremental_search()

    def _on_find_condition_changed(self, _checked):
        """大小区別・正規表現の切り替えでも探し直す。検索バーが
        出ていないときは何もしない(材料だけ先に設定される使い方
        で、勝手に検索が走らないように)。"""
        if self._find_bar.isVisible():
            self._request_incremental_search()

    def _request_incremental_search(self):
        word = self._find_edit.text()
        if not word:
            self._reset_incremental_search()
            return
        if not self._find_regex.isChecked():
            self._inc_timer.stop()
            self._do_incremental_search()
            return
        # 正規表現: 式の文法だけ先に確かめる(コンパイル自体は固ま
        # らない。破滅的な式の実行は別プロセス側が時間切れで守る)。
        # 不正なら、その場で知らせて探さない(打ちかけの式で、別
        # プロセスを起こし続けないためでもある)。
        try:
            rxfind.compile_search(
                word, regex=True,
                case_sensitive=self._find_case.isChecked())
        except rxfind.RegexFault as e:
            self._inc_timer.stop()
            self._set_find_not_found(True)
            self.statusBar().showMessage(str(e), 8000)
            return
        # 打つたびに別プロセスを起こさないよう、少し待ってから
        # 探す(間引き)。新しい入力が来たらタイマーは数え直し。
        self._inc_timer.start(INCREMENTAL_SEARCH_DELAY_MS)

    def _incremental_base(self) -> int:
        """リアルタイム検索の起点(本文の位置)。

        直前のヒットがそのまま選択されている間は、打ち始めた
        ときの起点を使い続ける(打つたびに次のヒットへずれていか
        ないように)。選択が他へ移っていたら、そこを新しい起点
        にする。"""
        cursor = self._edit.textCursor()
        if self._inc_span is not None and cursor.hasSelection() and \
                (cursor.selectionStart(), cursor.selectionEnd()) \
                == self._inc_span:
            return self._inc_base
        base = (cursor.selectionStart() if cursor.hasSelection()
                else cursor.position())
        self._inc_base = base
        return base

    def _apply_incremental_hit(self, start, end, base):
        """ヒットを選択にして、起点とヒットの範囲を控える。"""
        cursor = self._edit.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self._edit.setTextCursor(cursor)
        self._edit.centerCursor()
        self._inc_base = base
        self._inc_span = (start, end)
        self._refresh_match_highlights()

    def _do_incremental_search(self):
        """検索欄の今の語で、起点から一番近いヒットへ飛ぶ。

        通常の文字列はその場で探す(str.find は大きな文書でも速く、
        既存の次検索も同じ作法)。正規表現は _do_incremental_regex
        (既存の非同期経路)に回す。"""
        if not self._find_bar.isVisible():
            return
        word = self._find_edit.text()
        if not word:
            self._set_find_not_found(False)
            return
        case_sensitive = self._find_case.isChecked()
        if self._find_regex.isChecked():
            self._do_incremental_regex(word, case_sensitive)
            return
        text = self._edit.toPlainText()
        base = self._incremental_base()
        index = docmod.find_next(text, word, base, case_sensitive)
        if index < 0 and base > 0:
            index = docmod.find_next(text, word, 0, case_sensitive)
        if index < 0:
            self._mark_incremental_not_found(word)
            return
        self._set_find_not_found(False)
        self._apply_incremental_hit(index, index + len(word), base)

    def _do_incremental_regex(self, word, case_sensitive):
        """リアルタイム検索の正規表現版。実処理は既存の非同期
        経路(_run_rx_async。別プロセス＋世代管理で、古い結果は
        捨てられる)に乗せ、起点より後ろに無ければ先頭から探す
        (巻き戻しは1回だけ、次検索の作法と同じ)。"""
        text = self._edit.toPlainText()
        base16 = self._incremental_base()
        base = self._py_index(base16, text)
        revision = self._edit.document().revision()

        def apply_span(span):
            self._set_find_not_found(False)
            self._apply_incremental_hit(
                self._utf16_index(span[0], text),
                self._utf16_index(span[1], text), base16)

        def done(result):
            if self._edit.document().revision() != revision:
                return  # 検索の間に文書が変わった。結果は古い
            span = result["span"]
            if span is not None:
                apply_span(span)
                return
            if base == 0:
                self._mark_incremental_not_found(word)
                return

            def wrapped(result2):
                if self._edit.document().revision() != revision:
                    return
                span2 = result2["span"]
                if span2 is None:
                    self._mark_incremental_not_found(word)
                    return
                apply_span(span2)

            self._run_rx_async("find_next", wrapped, text=text,
                               pattern=word, start=0, regex=True,
                               case_sensitive=case_sensitive)

        self._run_rx_async("find_next", done, text=text, pattern=word,
                           start=base, regex=True,
                           case_sensitive=case_sensitive)

    # ---------------------------------------------------------------
    # 正規表現の非同期実行(待ちは裏スレッド、適用は GUI スレッド)
    # ---------------------------------------------------------------
    def _run_rx_async(self, op, on_done, **kwargs):
        """rxfind.run_operation を裏スレッドで回し、結果を on_done
        (結果の辞書)へ GUI スレッドで渡す。

        失敗(RegexFault)はステータスに出して on_done は呼ばない。
        新しい実行が始まったら古い結果は捨てる(検索や置換を連打
        しても、古い結果が後の状態を上書きしない)。結果は発注した
        タブについてのもので、そのタブが今のタブでなくなっていたら
        捨てる(裏のタブの本文を、目を離した直後に差し替えない
        ため)。世代と待ち受けはビューごとに持つ。"""
        view = self._current_view()
        if view is None:
            return
        view.rx_generation += 1
        generation = view.rx_generation
        view.rx_pending = (generation, on_done)
        bridge = self._rx_bridge

        def run():
            try:
                payload = (view, generation,
                           rxfind.run_operation(op, **kwargs))
            except rxfind.RegexFault as e:
                payload = (view, generation, e)
            except Exception as e:  # noqa: BLE001  理由は利用者への文にする
                payload = (view, generation, rxfind.RegexFault(
                    f"検索を実行できませんでした（{e}）"))
            bridge.finished.emit(payload)

        threading.Thread(target=run, daemon=True,
                         name="editor-rxfind").start()

    def _on_rx_finished(self, payload):
        view, generation, result = payload
        pending = view.rx_pending
        if pending is None or pending[0] != generation:
            return  # 新しい実行が始まっている。古い結果は捨てる
        view.rx_pending = None
        if view is not self._current_view():
            return  # 発注したタブは裏に回った。結果は捨てる
        if isinstance(result, rxfind.RegexFault):
            self.statusBar().showMessage(str(result), 8000)
            return
        pending[1](result)

    def _do_find_regex(self, forward: bool, word, case_sensitive) -> bool:
        """正規表現の検索(対話操作)。実処理は別プロセスで、待ちは
        裏スレッド(_run_rx_async)なので、遅い式でも窓は固まらない。
        マクロ再生中は、命令の順序を守るため同期版を使う。"""
        if self._replaying:
            return self._do_find_regex_sync(forward, word, case_sensitive)
        text = self._edit.toPlainText()
        cursor = self._edit.textCursor()
        if forward:
            start16 = (cursor.selectionEnd() if cursor.hasSelection()
                       else cursor.position())
        else:
            start16 = (cursor.selectionStart() if cursor.hasSelection()
                       else cursor.position())
        start = self._py_index(start16, text)
        revision = self._edit.document().revision()
        op = "find_next" if forward else "find_prev"

        def done(result):
            if self._edit.document().revision() != revision:
                return  # 検索の間に文書が変わった。結果は古いので捨てる
            span = result["span"]
            if span is not None:
                self._apply_find_span(span, text)
                return
            wrap_start = 0 if forward else len(text)

            def wrapped(result2):
                if self._edit.document().revision() != revision:
                    return
                span2 = result2["span"]
                if span2 is None:
                    self.statusBar().showMessage(
                        f"「{word}」は見つかりません", 4000)
                    return
                self._apply_find_span(span2, text)

            self._run_rx_async(op, wrapped, text=text, pattern=word,
                               start=wrap_start, regex=True,
                               case_sensitive=case_sensitive)

        self._run_rx_async(op, done, text=text, pattern=word, start=start,
                           regex=True, case_sensitive=case_sensitive)
        return True

    def _apply_find_span(self, span, text):
        """検索で得た (開始, 終了) を選択にする。text は検索時点の
        本文(位置の単位の変換に使う)。"""
        cursor = self._edit.textCursor()
        cursor.setPosition(self._utf16_index(span[0], text))
        cursor.setPosition(self._utf16_index(span[1], text),
                           QTextCursor.MoveMode.KeepAnchor)
        self._edit.setTextCursor(cursor)
        self._edit.centerCursor()
        self._refresh_match_highlights()

    def _do_find_regex_sync(self, forward: bool, word, case_sensitive) -> bool:
        """正規表現の検索の同期版(マクロ再生専用)。実処理は
        rxfind が別プロセスで回すので、不正な式はエラー表示に、
        固まる式は時間切れの失敗になる。"""
        text = self._edit.toPlainText()
        cursor = self._edit.textCursor()
        if forward:
            start16 = (cursor.selectionEnd() if cursor.hasSelection()
                       else cursor.position())
        else:
            start16 = (cursor.selectionStart() if cursor.hasSelection()
                       else cursor.position())
        start = self._py_index(start16, text)
        op = "find_next" if forward else "find_prev"
        try:
            span = rxfind.run_operation(
                op, text=text, pattern=word, start=start,
                regex=True, case_sensitive=case_sensitive)["span"]
            if span is None:
                wrap_start = 0 if forward else len(text)
                span = rxfind.run_operation(
                    op, text=text, pattern=word, start=wrap_start,
                    regex=True, case_sensitive=case_sensitive)["span"]
        except rxfind.RegexFault as e:
            self.statusBar().showMessage(str(e), 8000)
            return False
        if span is None:
            self.statusBar().showMessage(f"「{word}」は見つかりません", 4000)
            return False
        cursor.setPosition(self._utf16_index(span[0], text))
        cursor.setPosition(self._utf16_index(span[1], text),
                           QTextCursor.MoveMode.KeepAnchor)
        self._edit.setTextCursor(cursor)
        self._edit.centerCursor()
        self._refresh_match_highlights()
        return True

    # ---------------------------------------------------------------
    # Copilot へ送る
    # ---------------------------------------------------------------
    def send_selection(self):
        cursor = self._edit.textCursor()
        if not cursor.hasSelection():
            self.statusBar().showMessage("選択範囲がありません")
            return
        if self._send_thread is not None:
            self.statusBar().showMessage("Copilot へ送信中です")
            return
        # QPlainTextEdit の選択文字列は段落の区切りが U+2029 になる。
        text = cursor.selectedText().replace("\u2029", "\n")
        self._start_send(text, force=False)

    def _start_send(self, text, force):
        """送信を裏のスレッドで始める。sender の呼び方は
        sender(text) が通常、sender(text, True) が「入力欄の上書きを
        承知で送る」再送(copilot_io.send_text の force 引数)。"""
        self._pending_send_text = text
        self.statusBar().showMessage("Copilot へ送信しています…")

        def run():
            try:
                if force:
                    result = self._sender(text, True)
                else:
                    result = self._sender(text)
            except Exception as e:  # noqa: BLE001  理由は利用者への文にする
                result = copilot_io.SendResult(
                    False, f"Copilot に送れませんでした（{e}）")
            self._bridge.finished.emit(result)

        self._send_thread = threading.Thread(
            target=run, daemon=True, name="editor-copilot-send")
        self._send_thread.start()

    def _on_send_finished(self, result):
        self._send_thread = None
        if getattr(result, "needs_confirm", False):
            # Copilot の入力欄に書きかけの文がある。そのまま送ると
            # 上書きされて消えるので、利用者に確かめてから送り直す。
            if self._confirm_overwrite_input():
                text = self._pending_send_text
                if text:
                    self._start_send(text, force=True)
                    return
            self.statusBar().showMessage(
                "送信をやめました（入力欄の文はそのままです）", 8000)
            return
        self.statusBar().showMessage(result.message, 0 if not result.ok else 8000)

    def _confirm_overwrite_input(self) -> bool:
        """Copilot の入力欄の書きかけを上書きして送ってよいか。"""
        if self.ask_overwrite_input is not None:
            return bool(self.ask_overwrite_input())
        answer = QMessageBox.question(
            self, APP_NAME,
            "Copilot の入力欄に書きかけの文があります。\n"
            "そのまま送ると、選択している範囲で上書きされて、"
            "書きかけの文は消えます。送りますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    # ---------------------------------------------------------------
    # テスト・呼び側からの参照
    # ---------------------------------------------------------------
    def is_dirty(self) -> bool:
        """どれかのタブに未保存の変更があるか。再起動・終了の
        巻き添え判定(feature_screen.restart_blockers、
        filer.confirm_losses)から呼ばれる。"""
        return any(view.is_modified() for view in self._views)

    def restart_loss_count(self) -> int:
        """窓を閉じたら失われる未保存の内容の件数。

        セッション(控え)が有効な窓は、閉じても控えから戻るので
        常に 0(= EDDy のために再起動を止めない)。セッションが
        無効な窓(テスト等)は、未保存のタブの数を返す
        (dirty_window_state がこれを使う)。"""
        if self._session_path is not None:
            return 0
        return sum(1 for view in self._views if view.is_modified())

    def tab_count(self) -> int:
        """開いているタブの数。"""
        return len(self._views)

    def open_paths(self) -> list:
        """タブで開いているファイルのパス(無題は除く)。"""
        return [view.doc.path for view in self._views if view.doc.path]

    def contains_path(self, path) -> bool:
        """そのファイルがタブに開いているか。"""
        return self.find_view_for_path(path) is not None

    def activate_path(self, path) -> bool:
        """そのファイルのタブを前に出す。開いていなければ False。

        外部から「ファイルを開く」要求が来たときに、呼び側
        (feature_screen・filer の単一インスタンス経路)が使う。"""
        view = self.find_view_for_path(path)
        if view is None:
            return False
        self._activate_view(view)
        return True

    def current_document(self) -> docmod.TextDocument:
        self._sync_text()
        return self._doc

    def text(self) -> str:
        return self._edit.toPlainText()
