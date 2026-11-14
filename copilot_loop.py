# copilot_loop.py
# Copilot アプリを UI Automation で操る部品。疑似エージェントループの土台。
#
# 【なぜ UIA なのか】
# フォーカスを奪わずに、要素を名指しで読み書きできる。キー送信と違って
# 「別の窓に飛ぶ」余地が原理的に無いので、陽太さんが裏で作業していても事故らない
# (tray-tools の CLAUDE.md が SetForegroundWindow を禁じているのはそのため)。
#
# 【Chromium の落とし穴】
# Chromium は支援技術を検出するまでアクセシビリティツリーを作らない。素で覗くと
# 子孫が11個(窓枠だけ)しか見えない。レンダラの HWND へ WM_GETOBJECT を投げると
# 起きて、371個まで増える。これを毎回やる。
#
# 【応答テキストの取り方】
# 3案を実測して1つに決めた(詳細は Copilot.document_text の docstring):
#   1. Text 要素を座標順に並べる … コードがトークン単位で崩れる
#   2. TextPattern.DocumentRange  … 順序は正しいが @{ が @ と { に割れる
#   3. FindAll の並びのまま繋ぐ   … これが正解(木の順=DOM順が保たれる)
# 「並べ替えない」ことが肝。
#
# 【コードの切り出し】
# 応答から機械的に切り出すために、プロンプト側で #start <ID> / #end <ID> で
# 囲ませる(snippets/エージェントループ開始.txt の作法)。言語フェンスの検出に
# 頼るより確実で、実行漏れ・重複実行の検知にもなる。
#
# 【業務PC(M365 Copilot)への移植】
# 窓の条件もボタン名も SELECTORS にまとめてある。別アプリに移すときはここだけ
# 差し替える。コード側には日本語のボタン名を直書きしない。
import ctypes
import hashlib
import re
import sys
import time

try:
    import comtypes
    import comtypes.client
except ImportError:
    # comtypes は Windows の COM 用。他の環境では import だけ通し、
    # UIA を実際に使う段(_ensure_uia)で止める。状態の分類など COM と
    # 無関係な部分はそのまま動く(テストを通すためでもある)。
    comtypes = None

# UIAutomationCore の型ライブラリ。最初に本当に使うときまで読まない(_ensure_uia)。
UIA = None


def _ensure_uia():
    """UIA の型ライブラリを読み、このスレッドで COM を使えるようにする。

    【なぜ import 時にやらないか】
    常駐(tray-tools)は起動時にこのモジュールを読み込む。import しただけで
    CoInitialize と型ライブラリの読み込み(どちらも COM)が走ると、Copilot の機能を
    一度も使っていない常駐プロセスが COM を抱え込むことになる。

    実際それで事故った。2026-09-04 にこの機能が常駐へ入ってから、GC 中の
    comtypes 解放でプロセスごと即死する事故が15回起きている。落ちる場所は
    アイコン描画・ピッカーの採寸・設定保存とばらばらで、どれも Copilot とは
    関係ない。「たまたま GC が走ったところ」でしかないからで、犯人は
    「解放してはいけない状態の COM オブジェクトが常駐の中に居続けたこと」。
    Python の例外ではないので error.log には何も残らない(0xC0000005)。

    【なぜスレッドごとに呼ぶのか】
    COM のアパートメントはスレッドに紐づく。agent_loop はワーカースレッドで
    Copilot を作るので、そのスレッドでも CoInitialize が要る。ここを通れば
    どのスレッドから使っても初期化済みになる。2回目以降の CoInitialize は
    参照カウントが増えるだけで害はない。"""
    global UIA
    if comtypes is None:
        raise RuntimeError("comtypes が無い環境では UIA を使えません")
    comtypes.CoInitialize()
    if UIA is None:
        comtypes.client.GetModule("UIAutomationCore.dll")
        import comtypes.gen.UIAutomationClient as _UIA
        UIA = _UIA
    return UIA

# --- プロファイル（別アプリへ移すときはここに1つ足す） -----------------------
# 窓の探し方とボタン名を1アプリぶんまとめたもの。
#
# 【なぜ1組の定数をやめたのか】
# 手元PCの Copilot と業務PCの M365 Copilot は、実測で全部違った:
#   mscopilot.exe    class='Chrome_WidgetWin_1'           aid='userInput'
#   M365Copilot.exe  class='Microsoft 365 Copilot Host'   aid='m365chat-editor-target-element'
# 1組を書き換えて回す作りだと、PCを移るたびに書き換えが要る。両方を持っておいて、
# 実際に動いている窓に合うものを選ぶ。
#
# 【なぜ exe 名で照合するのか】
# タイトルは表示言語・開いている会話で変わる。クラスも当てにならない
# (Chrome_WidgetWin_1 には Claude も Chrome も居る)。exe 名はそのどちらの影響も
# 受けないので、これを第一の手掛かりにする。
#
# 【なぜボタン名がリストなのか】
# 文言はアプリ・バージョン・表示言語で変わる。1つの完全一致に賭けると、少し違う
# だけで「ずっと idle」に見えてしまい、しかも理由が画面に出ない。候補を並べて
# 「どれかに当てはまれば」で判定する。
BUILTIN_PROFILES = [
    {
        "name": "mscopilot",
        "process_name": "mscopilot.exe",
        "window_class": "Chrome_WidgetWin_1",
        "window_title_contains": "Copilot",
        "render_class": "Chrome_RenderWidgetHostHWND",
        "input_automation_id": "userInput",
        "send_button": ["メッセージの送信", "送信"],
        "busy_button": ["メッセージの割り込み", "生成を停止する", "停止"],
        "assistant_marker": "Copilot の発言",
        "user_marker": "あなたの発言",
        # 会話の末尾にくっついてくる入力欄のプレースホルダ。本文ではないので落とす。
        "idle_marker": ["Copilot へメッセージを送る", "Copilot と会話する"],
        # コードブロックとして描かれた本文の直前に付くボタン。これが目印になる
        # (code_blocks の説明を参照)。
        "code_copy_button": ["コードをコピー"],
        "input_band_px": 170,
    },
    {
        # 業務PCの M365 Copilot。値は uia_probe の実測(子孫335個)による。
        "name": "m365",
        "process_name": "M365Copilot.exe",
        "window_class": "Microsoft 365 Copilot Host",
        # タイトルは実測していないので条件にしない(exe とクラスで十分に絞れる)。
        "window_title_contains": "",
        # Chromium の窓クラスではないが、中身は WebView2 なのでレンダラの窓は居る
        # 見込み。見つからなければメイン窓へ WM_GETOBJECT を投げる道に落ちる。
        "render_class": "Chrome_RenderWidgetHostHWND",
        # ハイフンの位置は読み上げで拾ったので、聞き取り違いの余地がある綴りを
        # 両方入れてある(どちらかが当たれば動く)。
        "input_automation_id": [
            "m365-chat-editor-target-element",
            "m365chat-editor-target-element",
        ],
        "send_button": ["送信", "メッセージの送信"],
        "busy_button": ["生成を停止する", "メッセージの割り込み", "停止"],
        # 発言マーカーは未特定。M365 は 'あなたの発言' に相当する Text を出して
        # おらず、プローブの上位に出たのは会話本文だった。空のままにしてあるのは、
        # 当てずっぽうで埋めると agent_loop が誤った位置で応答を切り出すため。
        # 状態表示(copilot_watchdog)はマーカーを使わないので、空でも動く。
        "assistant_marker": "",
        "user_marker": "",
        # M365 側の文言は未実測。分かったらここに足す。
        "idle_marker": [],
        # 未実測。空のままだと「囲まれているか」を判定できないので、
        # そのときは判定を諦めて従来どおり実行する(code_blocks を参照)。
        "code_copy_button": [],
        "input_band_px": 170,
    },
]

# 既定のプロファイル。SELECTORS という名前は、これ1つで回していた頃からの呼び名。
# Copilot インスタンスは self.profile を見るので、実際に効くのはそちら。
SELECTORS = BUILTIN_PROFILES[0]

SETTINGS_PROFILES_KEY = "copilot_profiles"

# 入力欄の矩形を、どれだけ広げて「入力欄まわり」とみなすか(px)。
#
# 実測(2026-09-11, mscopilot のランディング画面):
#   入力欄    top=447 bottom=469 left=420 right=1110
#   送信ボタン top=488 left=1090 right=1126   ← 入力欄の真下41px、右端にほぼ重なる
# 40px では際どいので、composer の行が丸ごと入る程度に取る。プロファイルごとに
# composer_margin_px で上書きできる(アプリによって composer の作りが違う)。
COMPOSER_MARGIN_PX = 120

# 子孫がこれ未満なら、アクセシビリティツリーがまだ組み上がっていないとみなす。
# 実測: 起きる前は窓枠だけの11個、起きると300〜371個。境目は広く空いている。
MIN_AWAKE_DESCENDANTS = 30


def profiles(app_settings=None):
    """使うプロファイルを、優先順に並べて返す。

    settings.json の copilot_profiles を先に置く。業務PCでコードを書き換えずに
    直せるようにするため(あちらへは git で配るしかなく、settings.json は git
    管理外なので、PCごとに違う値を持たせても衝突しない)。
    同じ name のものは settings.json 側が勝つ。"""
    extra = []
    if isinstance(app_settings, dict):
        found = app_settings.get(SETTINGS_PROFILES_KEY)
        if isinstance(found, list):
            extra = [p for p in found if isinstance(p, dict) and p.get("name")]
    overridden = {p.get("name") for p in extra}
    return extra + [p for p in BUILTIN_PROFILES if p["name"] not in overridden]

CONTROL_BUTTON, CONTROL_TEXT, CONTROL_DOCUMENT = 50000, 50020, 50030
CONTROL_EDIT = 50004
CONTROL_MENU = 50009
CONTROL_MENU_ITEM, CONTROL_LIST_ITEM = 50011, 50008
CONTROL_WINDOW, CONTROL_PANE, CONTROL_DIALOG = 50032, 50033, 50031
VALUE_PATTERN, INVOKE_PATTERN, TEXT_PATTERN = 10002, 10000, 10014
# 「開く」系のボタン用 (teams_meeting の EXPAND_PATTERN と同じ値)。
EXPAND_COLLAPSE_PATTERN = 10005
# DL の押下で、Invoke の次に試す仕組み (1-32)。実機では、同じ項目でも
# 描画の状態で持っている仕組みが変わる。
SELECTION_ITEM_PATTERN, TOGGLE_PATTERN = 10010, 10015
LEGACY_ACCESSIBLE_PATTERN = 10018

# 種類コード → 表示名 (調査画面で人に見せるため。無いものはコードで出す)。
CONTROL_TYPE_NAMES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox",
    50003: "ComboBox", 50004: "Edit", 50005: "Hyperlink",
    50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar",
    50013: "RadioButton", 50014: "ScrollBar", 50015: "Slider",
    50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar",
    50022: "ToolTip", 50023: "Tree", 50024: "TreeItem",
    50025: "Custom", 50026: "Group", 50027: "Thumb",
    50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50032: "Window", 50033: "Pane",
}

# クラス名から「編集できそう」と見当を付けるための手掛かり (小文字の
# 部分一致)。網頁の contenteditable は、フレームワークのクラス名
# (ProseMirror 等) が付いていることが多い。
EDITABLE_CLASS_HINTS = (
    "prosemirror", "ql-editor", "codemirror", "monaco", "drafteditor",
    "contenteditable", "richtext", "rich-text", "textinput",
    "text-input", "inputarea", "input-area", "wysiwyg", "editor",
)

# 調査で拾う候補の上限 (木が大きい網頁でも、返事が際限なく大きく
# ならないように)。
SCAN_CANDIDATE_LIMIT = 300
WM_GETOBJECT, OBJID_CLIENT, SMTO_ABORTIFHUNG = 0x003D, 0xFFFFFFFC, 0x0002

# 埋め込みオブジェクト(ボタンや画像)の位置に入る文字。本文には要らない。
OBJECT_MARK = "￼"

try:
    # 専用の WinDLL(ctypes.windll は共有で、argtypes/restype が他モジュールと
    # 取り合いになるため)。copilot_status_process と muse_post が、この
    # user32 とその署名を借りに来るので、名前と署名は変えないこと。
    user32 = ctypes.WinDLL("user32", use_last_error=True)
except (AttributeError, OSError):
    # Windows 以外では WinDLL 自体が無い。import だけ通し、窓の実探査は
    # しない(状態の分類など user32 と無関係な部分はそのまま動く)。
    user32 = None

if user32 is not None:
    user32.SendMessageTimeoutW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
        ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
    ]
    # HWND は 64bit。argtypes を省くと ctypes が C int(32bit)に落として上位が消える
    # (CLAUDE.md「argtypes/restype は必須」)。
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindow.restype = ctypes.c_bool
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.IsIconic.restype = ctypes.c_bool
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_bool


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


if user32 is not None:
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = ctypes.c_bool
    # ESC の PostMessage 用 (1-67)。WPARAM は size_t、LPARAM は
    # ssize_t (署名なしだと HWND と同じく 64bit で化ける)。
    user32.PostMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
        ctypes.c_ssize_t]
    user32.PostMessageW.restype = ctypes.c_bool


# SendInput (マウス) の構造体。mouse_jiggler と同じ顔ぶれにしないと
# INPUT の大きさがずれ、SendInput が何もせず 0 を返す (64bit では
# INPUT 全体で40バイト)。使うのは mi だけ。
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT),
                ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUT_UNION)]


INPUT_MOUSE = 0
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN = 0x0001, 0x0002
MOUSEEVENTF_LEFTUP, MOUSEEVENTF_ABSOLUTE = 0x0004, 0x8000
SM_CXSCREEN, SM_CYSCREEN = 0, 1

if user32 is not None:
    user32.SendInput.argtypes = [
        ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int


def _click_at(x, y):
    """画面座標 (物理px) を実際にクリックする。SendInput の絶対移動と
    左ボタンの上げ下げを1組で送る。

    DL の押下で、UIA の仕組みがどれも使えないときの最終手段 (1-32)。
    それ以外の場所では使わないこと (この道具は原則 UIA だけで触る)。"""
    if user32 is None:
        raise RuntimeError("この環境ではマウス入力が使えません")
    width = user32.GetSystemMetrics(SM_CXSCREEN)
    height = user32.GetSystemMetrics(SM_CYSCREEN)
    if width <= 0 or height <= 0:
        raise RuntimeError("画面の大きさが分かりません")
    # 絶対座標は画面全体を 0..65535 に写した値で渡す。
    nx = int(x * 65535 / max(1, width - 1))
    ny = int(y * 65535 / max(1, height - 1))

    def _event(flags):
        ev = _INPUT()
        ev.type = INPUT_MOUSE
        ev.mi.dx = nx
        ev.mi.dy = ny
        ev.mi.dwFlags = flags
        return ev

    events = (_INPUT * 3)(
        _event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
        _event(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE),
        _event(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE))
    sent = user32.SendInput(3, events, ctypes.sizeof(_INPUT))
    if sent != 3:
        raise RuntimeError(f"クリックを送れませんでした (SendInput={sent})")


def _click_element(el):
    """要素の外接矩形の中心をクリックする。位置が取れなければ
    RuntimeError (_activate_element が理由として畳む)。"""
    rect = _bounding_rect(el)
    if rect is None:
        raise RuntimeError("要素の位置が分かりません")
    left, top, right, bottom = rect
    _click_at((left + right) // 2, (top + bottom) // 2)


def _post_escape(hwnd):
    """窓へ ESC のキー消息 (WM_KEYDOWN → WM_KEYUP) を PostMessage
    で投げる (1-67)。戻り値は短い状態語で、例外は出さない
    (呼び側の本筋を止めないため)。

    "sent" (2通とも投げた) / "no-hwnd" (宛先が無い、または
    この環境では投げられない) / "failed" (投げるのに失敗)。
    投げた先で本当に効いたかは、ここからは分からない。"""
    if not hwnd or user32 is None:
        return "no-hwnd"
    try:
        down = user32.PostMessageW(
            ctypes.c_void_p(hwnd), WM_KEYDOWN, VK_ESCAPE, 0)
        up = user32.PostMessageW(
            ctypes.c_void_p(hwnd), WM_KEYUP, VK_ESCAPE, 0)
    except Exception:  # noqa: BLE001  投げ損ねても本筋は続ける
        return "failed"
    return "sent" if down and up else "failed"


def window_bounds(hwnd):
    """窓の外接矩形 (left, top, right, bottom)(物理px)。最小化・非表示なら None。

    UIA を経由しないので桁違いに軽い。窓をドラッグに追従させるような短い周期の
    呼び出しはこちらを使うこと(UIA の走査を150msごとに回すと重すぎる)。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
        return None
    r = _RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    if r.right <= r.left or r.bottom <= r.top:
        return None
    return (r.left, r.top, r.right, r.bottom)


def _bounding_rect(element):
    """UIA 要素の外接矩形を (left, top, right, bottom) で返す。取れなければ None。

    最小化された窓は left が -32000 付近の座標を返す。そのまま窓の位置として使うと
    画面外にオーバーレイを置くことになるので、呼び側で弾けるよう素の値を渡す。"""
    try:
        r = element.CurrentBoundingRectangle
    except Exception:
        return None
    if r.right <= r.left or r.bottom <= r.top:
        return None
    return (r.left, r.top, r.right, r.bottom)


def _class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title_of(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _exe_of(hwnd):
    """窓を持っているプロセスの実行ファイル名。取れなければ ''。

    psutil が無い環境でも動くようにしておく(その場合は exe 名での照合を諦め、
    クラスとタイトルだけで絞る)。"""
    try:
        import psutil
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name()
    except Exception:  # noqa: BLE001
        return ""


_AUMID_IFACE = None


def _aumid_of(hwnd):
    """窓に付いているアプリの識別子(AppUserModelID)。取れなければ ''。

    【なぜ要るのか】Edge の PWA(Muse アプリ)と普通の Edge の窓は、exe も
    プロセス(pid)も窓クラスも同じ。2026-11-12 の実測で違ったのはこれだけ:
    PWA は 'muse.ai-B8452CE7_e417885h3ma38!App'、普通の Edge は 'MSEdge'。
    タイトルは Edge 側が "Microsoft\\u200b Edge" のようにゼロ幅文字を混ぜるなど
    当てにならない。"""
    global _AUMID_IFACE
    if comtypes is None:
        return ""
    try:
        from ctypes import wintypes as w
        if _AUMID_IFACE is None:
            class PROPERTYKEY(ctypes.Structure):
                _fields_ = [("fmtid", comtypes.GUID), ("pid", w.DWORD)]

            class PROPVARIANT(ctypes.Structure):
                _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                            ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                            ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]

            class IPropertyStore(comtypes.IUnknown):
                _iid_ = comtypes.GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
                _methods_ = [
                    comtypes.COMMETHOD([], comtypes.HRESULT, "GetCount",
                                       (["out"], ctypes.POINTER(w.DWORD))),
                    comtypes.COMMETHOD([], comtypes.HRESULT, "GetAt",
                                       (["in"], w.DWORD), (["in"], ctypes.c_void_p)),
                    comtypes.COMMETHOD([], comtypes.HRESULT, "GetValue",
                                       (["in"], ctypes.POINTER(PROPERTYKEY)),
                                       (["in"], ctypes.POINTER(PROPVARIANT))),
                ]
            key = PROPERTYKEY(comtypes.GUID("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)
            _AUMID_IFACE = (IPropertyStore, PROPVARIANT, key)
        IPropertyStore, PROPVARIANT, key = _AUMID_IFACE
        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # 既に別モードで初期化済みのスレッド。そのまま使える
        store = ctypes.POINTER(IPropertyStore)()
        hr = ctypes.windll.shell32.SHGetPropertyStoreForWindow(
            ctypes.c_void_p(hwnd), ctypes.byref(IPropertyStore._iid_), ctypes.byref(store))
        if hr != 0 or not store:
            return ""
        pv = PROPVARIANT()
        store.GetValue(ctypes.byref(key), ctypes.byref(pv))
        value = ctypes.wstring_at(pv.p) if pv.vt == 31 and pv.p else ""  # VT_LPWSTR
        if pv.vt:
            ctypes.windll.ole32.PropVariantClear(ctypes.byref(pv))
        store = None  # 同じスレッドでその場で手放す
        return value
    except Exception:  # noqa: BLE001
        return ""


def _find_render_child(hwnd, render_class):
    """レンダラの窓を再帰的に探す。見つからなければ None。

    EnumChildWindows は直接の子しか返さない環境がある(実測)。M365 Copilot のように
    Chromium 由来でない窓クラスを被せているアプリでは、レンダラが孫以下に居ることが
    あるので、コールバックの中で潜り直す。"""
    found = {"h": None}
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def walk(parent):
        def on_child(child, _l):
            if found["h"] is not None:
                return False
            if _class_of(child) == render_class:
                found["h"] = child
                return False
            walk(child)
            return found["h"] is None
        user32.EnumChildWindows(parent, EnumProc(on_child), None)

    walk(hwnd)
    return found["h"]


def find_window(profile):
    """プロファイルに合う可視の窓を探す。(hwnd_main, hwnd_render) を返す。

    照合は exe 名 → クラス → タイトル(部分一致)の順。指定が空の項目は見ない。
    exe 名が取れない環境では、その条件だけ飛ばして残りで絞る。

    app_id_prefix(AppUserModelID の頭)が指定されていて、窓から識別子が取れたときは
    タイトルを見ずに識別子だけで決める(PWA と普通の Edge はこれでしか分からない)。
    識別子が取れない窓だけ、従来どおりタイトルで判定する。"""
    want_exe = (profile.get("process_name") or "").lower()
    want_class = profile.get("window_class") or ""
    want_title = profile.get("window_title_contains") or ""
    want_app_id = (profile.get("app_id_prefix") or "").lower()
    found = {}
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def on_window(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        if want_class and _class_of(hwnd) != want_class:
            return True
        app_id = _aumid_of(hwnd).lower() if want_app_id else ""
        if app_id:
            if not app_id.startswith(want_app_id):
                return True
        elif want_title and want_title.lower() not in _title_of(hwnd).lower():
            return True
        if want_exe:
            exe = _exe_of(hwnd)
            if exe and exe.lower() != want_exe:
                return True
        found["main"] = hwnd
        return False

    user32.EnumWindows(EnumProc(on_window), None)
    main = found.get("main")
    if not main:
        return None, None
    return main, _find_render_child(main, profile.get("render_class") or "")


def _button_not_found_message(candidates):
    """送信ボタンの「見つからない」段の文。候補の件数と名前(最大3件)。

    入力欄の _input_not_found_message と同じ考え: 名前を添えると、
    プロファイルの目印と実物がどうずれているか、その場で見比べられる。"""
    names = []
    for _el, name, _class_name, _aid, _has_invoke in candidates or []:
        names.append((name or "").strip() or "(名前なし)")
        if len(names) >= 3:
            break
    count = len(candidates or [])
    if names:
        return (f"送信ボタンが見つかりませんでした"
                f"（ボタンは {count} 件ありました: "
                f"{'、'.join(names)}）")
    return f"送信ボタンが見つかりませんでした（ボタンは {count} 件でした）"


class InputStageError(RuntimeError):
    """入力欄まわりの失敗で、「どの段で落ちたか」が付いたもの。

    stage は "not_found"(目印に合う要素が無い) / "unreadable"(見つけたが
    中身が読めない) / "unwritable"(書き込む・押す段で失敗) のどれか。文は
    そのまま利用者へ見せられる日本語にしておく。送信ボタンまわり
    (press_send_button) でも同じ段分けで使う。

    これまでは、どの段で落ちても「入力欄が読めませんでした」の一文に
    畳まれていて、プロファイルの目印がずれているのか、要素の作りが
    違うのかが画面から分からなかった(2026-11-14、Edge の中の Muse
    へのテスト入力)。直し方が段ごとに違うので、分けて伝える。"""

    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


def _input_not_found_message(candidates):
    """「見つからない」段の文。候補の件数と名前(最大3件)を添える。

    名前を添えると、プロファイルの目印(名前)と実物がどうずれているか、
    その場で見比べられる。"""
    names = []
    for _el, name, _class_name, _has_value in candidates or []:
        names.append((name or "").strip() or "(名前なし)")
        if len(names) >= 3:
            break
    count = len(candidates or [])
    if names:
        return (f"入力欄が見つかりませんでした"
                f"（編集できそうな要素は {count} 件ありました: "
                f"{'、'.join(names)}）")
    return (f"入力欄が見つかりませんでした"
            f"（編集できそうな要素は {count} 件でした）")


# DL カードの名前が1行に潰れているとき、コメントの後ろにコードが
# 同じ行で続く。その始まりとみなす目印 (前後に空白のある形)。
_DOWNLOAD_CODE_STARTERS = (" import ", " from ", " def ", " class ")

# DL は2段で押す (1-23、実機の観察): カード自体のクリックはアプリ内の
# プレビューを開くだけで、ダウンロードは始まらない。本当の経路は、
# その発言の「その他のオプション」(…) を開いて、メニューの
# 「ダウンロード」を押すこと。名前は実機 (Edge の中の Muse) の
# 表示そのまま。
OPTIONS_BUTTON_NAME = "その他のオプション"
DOWNLOAD_ITEM_NAME = "ダウンロード"
# カードの後ろで「…」を探す上限 (要素数)。発言の中のボタン
# (リアクション / 返信 / …) はカードのすぐ後ろに並ぶので、40 でも
# 十分すぎるほど届く。次のカードに当たったら、そこで打ち切る
# (そこから先は別の発言の持ち物)。
OPTIONS_SEARCH_LIMIT = 40
# 「…」を押してからメニューが出るまで待つ上限と間隔 (秒)。
DOWNLOAD_MENU_TIMEOUT = 2.0
DOWNLOAD_MENU_POLL = 0.15
# 「ダウンロード」の押下が一通り失敗したとき、間を置いて
# 見つけ直し+押下を1回だけやり直す (メニューの描画が落ち着く
# のを待つ、1-32)。
DOWNLOAD_PRESS_RETRY_WAIT = 0.6

# Edge のダウンロード・フライアウト (パネル) の目印 (1-40)。
# ダウンロードが始まるたびに自動で開き、開いている間は自動 DL の
# 2段押下が実機で一度も成功していない (「…」メニューのあたりを
# 覆い、探索も惑わせる)。Edge の設定で自動表示は切れるが、PHAL は
# その設定に依存しない (入れている利用者もいる)。押下の前に、
# 見つけたら閉じてから進む。
FLYOUT_TITLE = "ダウンロード"
FLYOUT_CLOSE_BUTTON_NAME = "閉じる"
# パネルを探す深さの上限 (デスクトップ直下の要素から数えた段数)。
# 木全体を歩くと重いし、チャットの本文まで降りると誤認の元に
# なるので、浅い所だけを見る。
FLYOUT_SEARCH_DEPTH = 2
# 閉じる操作のあと、パネルが消えるのを待つ時間 (秒)。
FLYOUT_DISMISS_WAIT = 0.3

# ESC で閉じる (1-67、Nagato の案)。1-40 の UIA 解散は、実物の
# パネルの形 (見分けの組) が当たらず実機で閉じられなかった。
# こちらは要素を探さず、対象の窓へ ESC のキー消息を PostMessage
# で投げる。DL の到着が確かめられた直後 (窓側から standalone で
# 呼ばれる) と、押下の直前 (解散の手前の保険) の2か所だけで使う。
# 前面化 (SetForegroundWindow) も SendInput もしない (フォーカスを
# 奪わない原則は守る)。Chromium の Views バブルに届くかどうかは
# 実機でしか確かめられない (設定 ON で1周が判定)。
WM_KEYDOWN, WM_KEYUP, VK_ESCAPE = 0x0100, 0x0101, 0x1B

# 診断リクエストの目印 (1-43、Nagato の案)。AI の最新の発言の中に
# この行が単独で立っていたら、PHAL の窓側が「診断を送ってほしい」
# という呼びかけとして扱う (自動応答の切替が入っているときだけ)。
# 普段の文章に出てこない形にしてあり、前後の空白は許すが、行の
# 中身はこの文字列ちょうどでなければならない。
DIAG_REQUEST_MARKER = "PHAL:診断リクエスト"
# 目印より後ろに続く本文の上限 (文字)。最新の発言の中の目印だけを
# 相手にするため、後ろに会話が長く続いている (= 古い発言の中の
# 目印) ものは数えない。同じ発言の続きと、入力欄まわりの UI の
# 文字列が入る程度は見込んである。
DIAG_MARKER_TAIL_LIMIT = 4000

# 押下の仕組みの優先順 (DL の2段で共通に使う、1-32)。
#   (パターンID, インタフェース名, メソッド名, 表示名)
# 実機のループ試験サイクル2 (2026-11-15) で、「ダウンロード」の
# 項目はあるのに InvokePattern が無く、押せずに止まった。サイクル1
# では Invoke で押せており、同じ項目でも描画の状態で持っている
# 仕組みが変わる。1つに決め打ちせず、持っている仕組みを順に使う。
_ACTIVATION_CHAIN = (
    (INVOKE_PATTERN, "IUIAutomationInvokePattern", "Invoke", "Invoke"),
    (EXPAND_COLLAPSE_PATTERN, "IUIAutomationExpandCollapsePattern",
     "Expand", "ExpandCollapse"),
    (SELECTION_ITEM_PATTERN, "IUIAutomationSelectionItemPattern",
     "Select", "SelectionItem"),
    (TOGGLE_PATTERN, "IUIAutomationTogglePattern", "Toggle", "Toggle"),
    (LEGACY_ACCESSIBLE_PATTERN, "IUIAutomationLegacyIAccessiblePattern",
     "DoDefaultAction", "LegacyIAccessible"),
)


def _download_summary(name):
    """添付カードの名前から、表示用の要約を取り出す。取れなければ ""。

    カードの名前はファイルの中身の冒頭そのもの。"#!" で始まる
    スクリプトは、shebang 行の後ろのコメントか、続くコメント行が
    「何のファイルか」の一文になっている (例: 1行目が
    "#!phal python # 1-21: チャット確認とDLの行..." の形、または
    2行目が "# 1-21: ..." の形)。その一文を抜く。名前が1行に
    潰れている場合は、コードの始まりらしき所で切る。"""
    text = (name or "").lstrip()
    if not text.startswith("#!"):
        return ""
    lines = text.splitlines()
    segments = []
    first = lines[0] if lines else ""
    tail = first[2:]  # 冒頭の "#!" を落とす
    mark = tail.find("#")
    if mark >= 0:
        segments.append(tail[mark + 1:])
    for line in lines[1:4]:
        stripped = line.strip()
        if stripped.startswith("#"):
            segments.append(stripped.lstrip("#"))
        elif stripped:
            break
    for segment in segments:
        for starter in _DOWNLOAD_CODE_STARTERS:
            cut = segment.find(starter)
            if cut >= 0:
                segment = segment[:cut]
        summary = " ".join(segment.split())
        if len(summary) >= 4:
            return summary[:60]
    return ""


class Copilot:
    """Copilot アプリの窓を1つ掴んで、読み書きする。

    COM オブジェクトは属性として持ち続ける。関数の外に出すと即座に解放されて
    0xC0000005 で落ちる(tray-tools で何度も踏んだ罠)。"""

    def __init__(self, profile=None, app_settings=None):
        """profile を渡さなければ、動いている窓に合うものを自動で選ぶ。

        自動選択にしてあるのは、同じコードを手元PCと業務PCの両方で動かすため。
        どちらのアプリが起きているかは実行時にしか分からない。"""
        candidates = [profile] if profile else profiles(app_settings)
        for candidate in candidates:
            main, render = find_window(candidate)
            if main:
                self.profile = candidate
                self.hwnd_main, self.hwnd_render = main, render
                break
        else:
            names = "/".join(c.get("name", "?") for c in candidates)
            raise RuntimeError(f"Copilot の窓が見つかりません (試した設定: {names})")
        self._wake()
        # ここで初めて COM を触る。窓が見つからなかった場合は上で抜けているので、
        # Copilot が起動していない環境では COM をまったく初期化しない。
        uia_mod = _ensure_uia()
        self.uia = comtypes.client.CreateObject(
            uia_mod.CUIAutomation, interface=uia_mod.IUIAutomation)
        self.true_cond = self.uia.CreateTrueCondition()

    def close(self):
        """COM への参照を、掴んだのと同じスレッドで明示的に手放す。

        GC 任せにすると、解放が「たまたま次に GC が走った場所」まで先送りされる。
        そこが別のスレッドだと、アパートメントを跨いだ解放になってプロセスごと
        落ちる。使い終わりが分かっているなら、その場で捨てる。"""
        self.true_cond = None
        self.uia = None

    # -- プロファイルの参照 ------------------------------------------------
    def _names(self, key):
        """プロファイルの項目を必ずリストで返す(文字列1つでも書けるように)。"""
        value = self.profile.get(key) or []
        return [value] if isinstance(value, str) else list(value)

    def _has(self, names, key):
        """下段のボタン名の集合に、その役目のボタンが1つでも居るか。"""
        return any(w in names for w in self._names(key))

    def _wake(self):
        """Chromium のアクセシビリティツリーを起こす合図を送る。既に起きていれば無害。

        投げるだけで、組み上がるのを待たない。待つのは _descendants と wait_awake。"""
        out = ctypes.c_size_t()
        user32.SendMessageTimeoutW(
            ctypes.c_void_p(self.hwnd_render or self.hwnd_main), WM_GETOBJECT, 0,
            ctypes.c_ssize_t(OBJID_CLIENT), SMTO_ABORTIFHUNG, 1000, ctypes.byref(out),
        )

    def wait_awake(self, timeout=6.0, poll=0.3):
        """木が組み上がるまで待つ。組み上がったか(bool)を返す。

        起動直後や、ずっと裏に居たアプリを相手にするときはこれを通すこと。
        待たずに読むと窓枠だけの十数個しか見えず、入力欄もボタンも全部
        「見つかりません」になる(実測: 起きる前11個 / 起きた後371個)。
        UI スレッドから呼ぶと止まるので、そこでは短い timeout にするか、
        _descendants の自己修復に任せる。"""
        deadline = time.time() + max(0.0, timeout)
        while True:
            _root, desc = self._descendants(heal=False)
            if desc.Length >= MIN_AWAKE_DESCENDANTS:
                return True
            if time.time() >= deadline:
                return False
            self._wake()  # 1回目を取りこぼすアプリがある。投げ直しても害はない
            time.sleep(poll)

    # -- 木を歩く ----------------------------------------------------------
    def _descendants(self, heal=True):
        """毎回取り直す。中身が変わると前に取った要素は無効になるため。

        子孫が異様に少ないときは、木がまだ組み上がっていない。1回だけ起こし直して
        取り直す(heal)。ここで面倒を見ておかないと、呼び側それぞれが「入力欄が
        見つかりません」を別々に処理する羽目になり、しかも本当の原因が見えない。
        1回に限っているのは、UI スレッドから呼ばれても止まる時間を抑えるため。"""
        root = self.uia.ElementFromHandle(ctypes.c_void_p(self.hwnd_main))
        desc = root.FindAll(UIA.TreeScope_Descendants, self.true_cond)
        if heal and desc.Length < MIN_AWAKE_DESCENDANTS:
            self._wake()
            time.sleep(0.3)
            desc = root.FindAll(UIA.TreeScope_Descendants, self.true_cond)
        return root, desc

    def _input_box(self, desc):
        """入力欄の要素。見つからなければ None。

        照合の本体は _find_input_box(理由付け用の候補も一緒に返す)。
        こちらは要素だけ欲しい呼び側(状態判定など)のための薄い受け口。"""
        box, _candidates = self._find_input_box(desc)
        return box

    def _editable_candidates(self, desc):
        """編集できそうな要素の並び [(要素, 名前, クラス名, 値の読み書き可)]。

        名前・クラスでの照合の母集団。種類が Edit か Document で、
        キーボードでフォーカスできるものだけに絞る(調査 scan と同じ
        入り口)。この外の要素まで名前で合わせに行くと、無関係な表示
        部品を入力欄と取り違える。"""
        found = []
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                if el.CurrentControlType not in (CONTROL_EDIT,
                                                 CONTROL_DOCUMENT):
                    continue
                if not el.CurrentIsKeyboardFocusable:
                    continue
                name = el.CurrentName or ""
                class_name = el.CurrentClassName or ""
            except Exception:  # noqa: BLE001  消えかけの要素は読み飛ばす
                continue
            try:
                has_value = bool(el.GetCurrentPattern(VALUE_PATTERN))
            except Exception:  # noqa: BLE001
                has_value = False
            found.append((el, name, class_name, has_value))
        return found

    @staticmethod
    def _class_overlap(stored_class, class_name):
        """保存したクラス文字列と要素のクラス名で、共通する token の数。

        クラスは空白で区切られた token の列として比べる(順序は見ない)。"""
        stored = set((stored_class or "").split())
        if not stored:
            return 0
        return len(stored & set((class_name or "").split()))

    def _find_input_box(self, desc):
        """入力欄を探す。(要素, 候補の並び) を返す。無ければ (None, 候補)。

        照合の優先順位:
          1. AutomationId の完全一致(プロファイルに aid があるとき)。
             aid をリストで受けるのはボタン名と同じ理由。加えて、業務PC の
             値は画面を読み上げてもらって書き取ったもので、聞き取り違いの
             余地がある。候補を並べておけば、一発勝負にならずに済む
          2. 名前の一致(編集できそうな候補の中で)。網頁の中の入力欄は
             AutomationId を持たないことが多く、その場合は PHAL の調査
             画面(script_runner_ai)が拾った候補の名前とクラスを
             プロファイルに持つ。同名が複数あれば、保存したクラスとの
             token の重なりが多い物 → 値の読み書きができる物 → 木の
             並び順、で1つに決める
          3. クラスだけでの照合。token の重なりが、保存した token の
             半分以上(最低1つ)ある物の中で、一番合う物

        【なぜクラスを完全一致で見ないか】
        網頁のテキストエリアのクラスは、フレームワーク(Tailwind 等)の
        ユーティリティの列で、状態で付いたり外れたりする token が混ざる。
        完全一致に賭けた旧実装は、実機(Edge の中の Muse、2026-11-14)
        で「入力欄が読めませんでした」の一文だけを返して落ちた。名前は
        利用者向けのラベルで安定しているので、名前を軸にして、クラスは
        同名の切り分けと「半分以上合えば」の緩い条件に使う。

        候補の並びは、見つからなかった理由(「編集できそうな要素は N 件、
        名前は…」)を作る材料として一緒に返す。"""
        wanted = [w for w in self._names("input_automation_id") if w]
        if wanted:
            for i in range(desc.Length):
                el = desc.GetElement(i)
                try:
                    if (el.CurrentAutomationId or "") in wanted:
                        return el, []
                except Exception:
                    continue
        candidates = self._editable_candidates(desc)
        want_name = self.profile.get("input_name") or ""
        want_class = self.profile.get("input_class") or ""
        if want_name:
            named = [c for c in candidates if c[1] == want_name]
            if named:
                best = max(
                    range(len(named)),
                    key=lambda k: (
                        self._class_overlap(want_class, named[k][2]),
                        named[k][3], -k))
                return named[best][0], candidates
        if want_class:
            stored = set(want_class.split())
            scored = []
            for pos, cand in enumerate(candidates):
                overlap = self._class_overlap(want_class, cand[2])
                # 保存した token の半分以上が実物にもあること(最低1つ)。
                if overlap >= 1 and 2 * overlap >= len(stored):
                    scored.append((overlap, cand[3], -pos, cand[0]))
            if scored:
                return max(scored)[3], candidates
        return None, candidates

    def _composer_buttons(self, root, desc):
        """入力欄まわりのボタン名。送信・停止の判定はここに出る。

        【名前どおりに「入力欄」を基準にすること】
        以前は `_bottom_buttons` という名で、**窓の下端から一定の帯**を見ていた。
        会話中は入力欄が窓の下端にあるので、たまたま一致していただけだった。

        会話を1つも始めていないランディング画面では、入力欄が画面の中ほどに座る。
        実測(2026-09-11, mscopilot):

            窓        bottom=1392
            入力欄    top=447  bottom=469
            送信ボタン top=488   Name='メッセージの送信'  InvokePattern あり
            旧・帯の境目  top>=1222

        送信ボタンは名前も申し分なく、押せる状態で**画面にも見えている**のに、
        帯から900px以上外れているだけで捨てられていた。「送信ボタンが見つかりません」
        で1周目に落ちるのはこれ。**名前の問題でもアイコンの問題でもなかった。**

        だから入力欄の矩形を基準にする。送信ボタンは composer の一部として入力欄の
        すぐ隣(実測では真下41px)に置かれるので、入力欄の位置さえ掴めていれば
        画面のどこに composer があっても追いかけられる。

        入力欄を掴めなかったときだけ、従来の「窓の下端からの帯」に落ちる。入力欄を
        見失っている時点でどうせ書き込めないが、状態表示(copilot_watchdog)は
        それでも何か出したいので、黙って空を返すよりはましな方を残す。"""
        band = self._composer_band(desc)
        if band is None:
            top_limit = root.CurrentBoundingRectangle.bottom - int(
                self.profile.get("input_band_px") or 170)
            band = (top_limit, None, None, None)
        lo, hi, left, right = band

        names = []
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                if el.CurrentControlType != CONTROL_BUTTON:
                    continue
                r = el.CurrentBoundingRectangle
                if r.bottom < lo:
                    continue
                if hi is not None and r.top > hi:
                    continue
                # 横にも縛る。縛らないと、画面左の会話一覧に並ぶボタンまで
                # 同じ高さというだけで入ってくる(実測で「オプションを表示する」が
                # 5個混ざった)。送信ボタンは入力欄と横に重なる位置にある。
                if left is not None and (r.right < left or r.left > right):
                    continue
                name = (el.CurrentName or "").strip()
                if name:
                    names.append((name, el))
            except Exception:
                continue
        return names

    def _composer_band(self, desc):
        """入力欄まわりとみなす矩形 (上, 下, 左, 右)。入力欄を掴めなければ None。"""
        box = self._input_box(desc)
        if box is None:
            return None
        try:
            r = box.CurrentBoundingRectangle
        except Exception:  # noqa: BLE001  要素が消えた直後など
            return None
        margin = int(self.profile.get("composer_margin_px") or COMPOSER_MARGIN_PX)
        return (r.top - margin, r.bottom + margin,
                r.left - margin, r.right + margin)

    def _composer_scope(self, root, desc):
        """入力欄を含む composer の範囲(祖先要素)。絞れなければ None。

        調査 (scan) の候補を絞るための範囲。入力欄から
        RawViewWalker で親をたどり、ボタンを1つでも含む最初の
        祖先を composer とみなす。その内側だけを見れば、会話本文の
        発言ボタンやサイドバーの項目が候補に混ざらない (実機で
        送信ボタンの候補が膨大になった。2026-11-14)。

        窓の root 自身に行き着いたら、それは絞り込みにならない
        ので None を返す (呼び側は窓全体に落ちる)。入力欄の目印が
        無くて掴めないときも None。"""
        box, _candidates = self._find_input_box(desc)
        if box is None:
            return None
        try:
            walker = self.uia.RawViewWalker
        except Exception:  # noqa: BLE001  取れなければ絞らない
            return None

        def runtime_id(el):
            try:
                return tuple(el.GetRuntimeId())
            except Exception:  # noqa: BLE001
                return None

        root_id = runtime_id(root)
        current = box
        for _depth in range(8):
            try:
                parent = walker.GetParentElement(current)
            except Exception:  # noqa: BLE001
                return None
            if parent is None:
                return None
            if root_id is not None and runtime_id(parent) == root_id:
                return None
            try:
                sub = parent.FindAll(
                    UIA.TreeScope_Descendants, self.true_cond)
            except Exception:  # noqa: BLE001
                return None
            for i in range(sub.Length):
                try:
                    if sub.GetElement(i).CurrentControlType \
                            == CONTROL_BUTTON:
                        return parent
                except Exception:  # noqa: BLE001
                    continue
            current = parent
        return None

    # -- 状態 --------------------------------------------------------------
    def state(self):
        """'busy'(回答中) / 'ready'(送信できる) / 'idle'(入力待ち・空) を返す。"""
        root, desc = self._descendants()
        names = [n for n, _ in self._composer_buttons(root, desc)]
        if self._has(names, "busy_button"):
            return "busy"
        if self._has(names, "send_button"):
            return "ready"
        return "idle"

    def status_snapshot(self):
        """状態・入力欄の中身・入力欄の矩形・窓の矩形を、木の走査1回でまとめて取る。

        ステータスの常時表示(copilot_watchdog)は1〜2秒ごとに全部を欲しがる。
        state() と read_input() を別々に呼ぶと FindAll(実測371要素)を二度歩くので、
        ここで1回に畳んでいる。単発で状態だけ欲しいときは従来どおり state() でよい。

        戻り値の矩形は Win32 と同じ (left, top, right, bottom) の物理ピクセル。
        Qt に渡す前に capture_grab.device_bounds_to_logical を通すこと。
        取れなかった項目は None。
        """
        root, desc = self._descendants()
        names = [n for n, _ in self._composer_buttons(root, desc)]
        if self._has(names, "busy_button"):
            state = "busy"
        elif self._has(names, "send_button"):
            state = "ready"
        else:
            state = "idle"

        input_text = None
        input_rect = None
        box = self._input_box(desc)
        if box is not None:
            try:
                pattern = box.GetCurrentPattern(VALUE_PATTERN)
                if pattern:
                    value = pattern.QueryInterface(UIA.IUIAutomationValuePattern)
                    input_text = value.CurrentValue
            except Exception:
                pass
            input_rect = _bounding_rect(box)

        return {
            "state": state,
            "input_text": input_text,
            "input_rect": input_rect,
            "window_rect": _bounding_rect(root),
        }

    def wait_until_idle(self, timeout=300, poll=0.4,
                        settle_seconds=2.5, grace_seconds=1.5):
        """回答が終わるまで待つ。(終わったか, 経過秒) を返す。

        【前の実装がタイムアウトした】
        「busy を一度見てから idle に戻る」に依存していた。生成が短すぎたり poll の
        隙間で busy を見逃すと永久に idle 待ちになる(実測: 締めのお礼で301秒回った)。

        新しい判定:
          - 送信直後は grace_seconds だけ待つ(状態変化の立ち上がりを見逃さない)
          - idle が settle_seconds 連続したら完了
          - busy を見たらタイマーをリセット
        busy を経由してもしなくても同じ規則で終わるので、見逃し事故が起きない。
        """
        start = time.time()
        time.sleep(grace_seconds)
        idle_since = None
        while time.time() - start < timeout:
            current = self.state()
            if current == "busy":
                idle_since = None
            elif idle_since is None:
                idle_since = time.time()
            elif time.time() - idle_since >= settle_seconds:
                return True, time.time() - start
            time.sleep(poll)
        return False, time.time() - start

    # -- 書く --------------------------------------------------------------
    def set_input(self, text):
        """入力欄に文字を入れる。送信はしない。

        失敗は段つき(InputStageError)で上げる: 見つからない /
        書き込む仕組みが無い / 書き込み自体の失敗。呼び側が理由を
        一文に畳まなくて済むように。"""
        _root, desc = self._descendants()
        box, candidates = self._find_input_box(desc)
        if box is None:
            raise InputStageError(
                "not_found", _input_not_found_message(candidates))
        pattern = box.GetCurrentPattern(VALUE_PATTERN)
        if not pattern:
            raise InputStageError(
                "unwritable", "入力欄に書き込めません（ValuePattern が無い）")
        try:
            pattern.QueryInterface(
                UIA.IUIAutomationValuePattern).SetValue(text)
        except Exception as e:  # noqa: BLE001  理由は段つきの文に畳む
            raise InputStageError(
                "unwritable",
                f"入力欄に書き込めませんでした（{e}）") from e

    def read_input(self):
        """入力欄の今の文を読む。読めなければ InputStageError を上げる。

        【None を返す旧契約をやめた理由】
        以前は「見つからない・読めない」をどちらも None で返し、呼び側
        (copilot_io)が「読めませんでした」の一文に畳んでいた。だが
        「目印に合う要素が無い」と「見つけた要素の中身が読めない」は
        直し方が違う(前者はプロファイルの目印、後者は要素の作り)。
        段の分かる文で上げる。None の受け口は copilot_io 側の
        CopilotLoopTransport.input_text に残してある(テストの fake 等、
        None を返す実装との互換のため)。"""
        _root, desc = self._descendants()
        box, candidates = self._find_input_box(desc)
        if box is None:
            raise InputStageError(
                "not_found", _input_not_found_message(candidates))
        pattern = box.GetCurrentPattern(VALUE_PATTERN)
        if not pattern:
            raise InputStageError(
                "unreadable",
                "入力欄は見つかりましたが、中身を読めませんでした"
                "（ValuePattern が無い）")
        try:
            return pattern.QueryInterface(
                UIA.IUIAutomationValuePattern).CurrentValue
        except Exception as e:  # noqa: BLE001  理由は段つきの文に畳む
            raise InputStageError(
                "unreadable",
                "入力欄は見つかりましたが、中身を読めませんでした"
                f"（{e}）") from e

    def probe_input_candidates(self):
        """入力欄の候補を子孫から探して返す(プロファイル作りの下調べ用)。

        返すのは [{"automation_id", "control_type", "name"}] の並び。
        候補の条件は、種類が Edit か Document で、ValuePattern を
        持っていること (値の読み書きができる要素だけが入力欄に
        なりうる。tools/uia_probe の入力欄候補の拾い方と同じ考え)。
        木がまだ起きていないと候補が空になるので、先に wait_awake で
        組み上がりを待つ (待っても空なら、そのまま空を返す)。"""
        self.wait_awake(timeout=3.0)
        _root, desc = self._descendants()
        found = []
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                control_type = el.CurrentControlType
                if control_type not in (CONTROL_EDIT, CONTROL_DOCUMENT):
                    continue
                if not el.GetCurrentPattern(VALUE_PATTERN):
                    continue
                found.append({
                    "automation_id": (el.CurrentAutomationId or "").strip(),
                    "control_type": ("Edit" if control_type == CONTROL_EDIT
                                     else "Document"),
                    "name": (el.CurrentName or "").strip()[:60],
                })
            except Exception:  # noqa: BLE001  消えかけの要素は読み飛ばす
                continue
        return found

    def scan_input_candidates(self):
        """入力欄の候補を、プローブより深く探して返す (PHAL の調査用)。

        probe_input_candidates は「Edit か Document で ValuePattern を
        持つもの」だけを返す。網頁の中の入力欄 (contenteditable) は
        種類が Edit でなかったり ValuePattern が無かったりして、その
        網から漏れる。実測 (2026-11-14、Edge の中の Muse) では、
        ページ全体の Document (RootWebArea) だけが拾われて「入力欄」
        として失敗した。

        こちらは「キーボードでフォーカスできて、編集できそうなもの」
        を広く拾う: 種類が Edit か Document、またはクラス名から
        contenteditable 系と見当が付くもの。合わせるための目印
        (種類・名前・AutomationId・クラス・ValuePattern と
        TextPattern の有無) を全部付けて返し、どれが本物かは人が
        調査画面で選ぶ。木がまだ起きていないと候補が空になるので、
        先に wait_awake で組み上がりを待つ。

        入力欄の目印が既にあり、入力欄を含む composer の範囲が
        絞れる場合は、その内側の候補だけを返す (送信ボタンの調査と
        同じ絞り込み。_composer_scope を参照)。範囲で候補が
        無ければ、従来どおり窓全体から拾う。"""
        self.wait_awake(timeout=3.0)
        root, desc = self._descendants()
        scope = self._composer_scope(root, desc)
        if scope is not None:
            narrowed = self._input_candidate_dicts(
                scope.FindAll(UIA.TreeScope_Descendants, self.true_cond))
            if narrowed:
                return narrowed
        return self._input_candidate_dicts(desc)

    def _input_candidate_dicts(self, desc):
        """desc の中から、編集できそうな要素を候補の辞書で返す。"""
        found = []
        for i in range(desc.Length):
            if len(found) >= SCAN_CANDIDATE_LIMIT:
                break
            el = desc.GetElement(i)
            try:
                if not el.CurrentIsKeyboardFocusable:
                    continue
                control_type = el.CurrentControlType
                class_name = (el.CurrentClassName or "").strip()
                if control_type not in (CONTROL_EDIT, CONTROL_DOCUMENT) \
                        and not any(hint in class_name.lower()
                                    for hint in EDITABLE_CLASS_HINTS):
                    continue
                found.append({
                    "control_type": CONTROL_TYPE_NAMES.get(
                        control_type, f"種類{control_type}"),
                    "name": (el.CurrentName or "").strip()[:60],
                    "automation_id": (el.CurrentAutomationId or "").strip(),
                    "class_name": class_name,
                    "value_pattern": bool(
                        el.GetCurrentPattern(VALUE_PATTERN)),
                    "text_pattern": bool(
                        el.GetCurrentPattern(TEXT_PATTERN)),
                })
            except Exception:  # noqa: BLE001  消えかけの要素は読み飛ばす
                continue
        return found

    # -- 送信ボタン (PHAL のプロファイルが目印を持つとき) --------------
    def _button_candidates(self, desc):
        """ボタンの並び [(要素, 名前, クラス名, AutomationId, 押せるか)]。

        送信ボタンの照合の母集団。種類が Button のものだけに絞る
        (名前だけで探しに行くと、同じ文言の表示部品まで拾ってしまう。
        入力欄の _editable_candidates と同じ考え)。「押せるか」は
        InvokePattern の有無で、押す段の理由付けにも使う。"""
        found = []
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                if el.CurrentControlType != CONTROL_BUTTON:
                    continue
                name = el.CurrentName or ""
                class_name = el.CurrentClassName or ""
                automation_id = el.CurrentAutomationId or ""
            except Exception:  # noqa: BLE001  消えかけの要素は読み飛ばす
                continue
            try:
                has_invoke = bool(el.GetCurrentPattern(INVOKE_PATTERN))
            except Exception:  # noqa: BLE001
                has_invoke = False
            found.append((el, name, class_name, automation_id,
                          has_invoke))
        return found

    def _find_send_button(self, desc):
        """送信ボタンを探す。(要素, 候補の並び) を返す。無ければ (None, 候補)。

        照合の優先順位は入力欄 (_find_input_box) と同じ:
          1. AutomationId の完全一致 (プロファイルに send_automation_id)
          2. 名前の一致 (send_name)。同名が複数あれば、保存したクラス
             との token の重なり → 押せる物 → 木の並び順で1つに決める
          3. クラスだけ (send_class)。token の重なりが保存した token の
             半分以上 (最低1つ) ある物の中で、一番合う物

        目印は PHAL のプロファイルが持つ send_* の3点。組み込みの
        send_button (名前の並び) とは別物で、そちらは click_send が
        composer まわりで使う従来の経路のまま。"""
        candidates = self._button_candidates(desc)
        want_aid = (self.profile.get("send_automation_id") or "").strip()
        want_name = (self.profile.get("send_name") or "").strip()
        want_class = (self.profile.get("send_class") or "").strip()
        if want_aid:
            for cand in candidates:
                if cand[3] == want_aid:
                    return cand[0], candidates
        if want_name:
            named = [c for c in candidates
                     if (c[1] or "").strip() == want_name]
            if named:
                best = max(
                    range(len(named)),
                    key=lambda k: (
                        self._class_overlap(want_class, named[k][2]),
                        named[k][4], -k))
                return named[best][0], candidates
        if want_class:
            stored = set(want_class.split())
            scored = []
            for pos, cand in enumerate(candidates):
                overlap = self._class_overlap(want_class, cand[2])
                if overlap >= 1 and 2 * overlap >= len(stored):
                    scored.append((overlap, cand[4], -pos, cand[0]))
            if scored:
                return max(scored)[3], candidates
        return None, candidates

    def press_send_button(self):
        """プロファイルの目印 (send_*) で送信ボタンを探して押す。

        押すのは UIA の InvokePattern だけ。キー送信はしない
        (この道具はフォーカスを奪わない線引き)。失敗は段つき
        (InputStageError) で上げる: 目印が無い / ボタンが見つから
        ない (候補の件数と名前つき) / 押す仕組みが無い / 押す段で
        失敗。押せたら True。"""
        if not any((self.profile.get(key) or "").strip() for key in
                   ("send_automation_id", "send_name", "send_class")):
            raise InputStageError(
                "not_found", "送信ボタンの目印が設定されていません")
        _root, desc = self._descendants()
        el, candidates = self._find_send_button(desc)
        if el is None:
            raise InputStageError(
                "not_found", _button_not_found_message(candidates))
        pattern = el.GetCurrentPattern(INVOKE_PATTERN)
        if not pattern:
            raise InputStageError(
                "unwritable",
                "送信ボタンは見つかりましたが、押す仕組み"
                "（InvokePattern）がありません")
        try:
            pattern.QueryInterface(
                UIA.IUIAutomationInvokePattern).Invoke()
        except Exception as e:  # noqa: BLE001  理由は段つきの文に畳む
            raise InputStageError(
                "unwritable",
                f"送信ボタンを押せませんでした（{e}）") from e
        return True

    def scan_button_candidates(self):
        """送信ボタンの候補を拾って返す (PHAL の調査用)。

        種類が Button のものを全部拾い、合わせるための目印 (名前・
        AutomationId・クラス) と「押せるか (InvokePattern の有無)」を
        付けて返す。どれが送信ボタンかは人が調査画面で選ぶ。木が
        まだ起きていないと候補が空になるので、先に wait_awake で
        組み上がりを待つ (入力欄の調査と同じ作法)。

        実機 (Edge の中の Muse) では、窓全体だと会話の発言ボタンや
        サイドバーの項目まで全部入って候補が膨大になった
        (2026-11-14)。入力欄の目印があり、入力欄を含む composer の
        範囲が絞れる場合は、その内側のボタンだけを先に返す
        (_composer_scope)。範囲で候補が無ければ窓全体に落ちる。"""
        self.wait_awake(timeout=3.0)
        root, desc = self._descendants()
        scope = self._composer_scope(root, desc)
        if scope is not None:
            narrowed = self._button_candidate_dicts(
                scope.FindAll(UIA.TreeScope_Descendants, self.true_cond))
            if narrowed:
                return narrowed
        return self._button_candidate_dicts(desc)

    def _button_candidate_dicts(self, desc):
        """desc の中から、ボタンを候補の辞書で返す。"""
        found = []
        for el, name, class_name, automation_id, has_invoke in \
                self._button_candidates(desc):
            if len(found) >= SCAN_CANDIDATE_LIMIT:
                break
            try:
                found.append({
                    "control_type": CONTROL_TYPE_NAMES.get(
                        CONTROL_BUTTON, "Button"),
                    "name": (name or "").strip()[:60],
                    "automation_id": (automation_id or "").strip(),
                    "class_name": (class_name or "").strip(),
                    "invoke_pattern": has_invoke,
                    "value_pattern": bool(
                        el.GetCurrentPattern(VALUE_PATTERN)),
                    "text_pattern": bool(
                        el.GetCurrentPattern(TEXT_PATTERN)),
                })
            except Exception:  # noqa: BLE001  消えかけの要素は読み飛ばす
                continue
        return found

    # -- チャットのダウンロード候補 (PHAL の見張り用) ------------------
    def _download_candidates(self, desc):
        """添付カードのボタン [(位置, 要素, 名前, クラス)] (木の順のまま)。

        位置は desc の中のインデックス。押す段で「カードの後ろ」を
        起点に探すために持つ。

        目印は、実機 (Edge の中の Muse、2026-11-14) の観察による:
        添付ファイルのカードは種類が Button で、クラスに "group/card"
        を含む (名前は中身の冒頭そのもので、"#!phal python ..." の
        ように始まる)。候補はこのカードだけに限る。

        【名前だけの照合は使わない】1-21 の実機で、名前に ".phal" を
        含むサイドチャット一覧のスレッドボタンを「最新」と誤検出した
        (木の順で後ろに居たため)。窓の中には、名前に拡張子や "#!" を
        含む無関係なボタンがいくらでもある。"""
        found = []
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                if el.CurrentControlType != CONTROL_BUTTON:
                    continue
                name = el.CurrentName or ""
                class_name = el.CurrentClassName or ""
            except Exception:  # noqa: BLE001  消えかけの要素は読み飛ばす
                continue
            if "group/card" in class_name:
                found.append((i, el, name, class_name))
        return found

    def _pick_download_candidate(self, desc):
        """ダウンロード候補から「最新」を1件選ぶ。無ければ None。

        スクリプトらしいもの (名前が "#!" で始まる / 名前に拡張子
        .phal・.py・.ps1 を含む) を優先し、その中では木の順の最後
        (= 最新の発言のもの) を選ぶ。スクリプトらしいものが無ければ
        カード全体の最後。検出 (latest_download_candidate) と押下
        (press_download_candidate) で同じ選び方を使う。戻り値は
        (位置, 要素, 名前, クラス)。"""
        candidates = self._download_candidates(desc)
        if not candidates:
            return None
        scripty = [
            c for c in candidates
            if c[2].lstrip().startswith("#!")
            or any(ext in c[2].lower() for ext in (".phal", ".py", ".ps1"))]
        return (scripty or candidates)[-1]

    def _candidate_dict(self, desc):
        """子孫の並びから、ダウンロード候補の辞書を作る。

        latest_download_candidate と latest_chat_scan で同じ選び方を
        使うための切り出し。候補が無ければ None。"""
        picked = self._pick_download_candidate(desc)
        if picked is None:
            return None
        _index, _el, name, class_name = picked
        summary = _download_summary(name)
        label = summary if summary else " ".join(name.split())[:60]
        return {
            "name": name,
            "class_name": class_name,
            "label": label,
            "summary": summary,
        }

    def latest_download_candidate(self):
        """最新の AI 発言にある、ダウンロード候補を1件返す。

        戻り値は {"name", "class_name", "label", "summary"}。
        name はボタンの名前そのまま (押す段で探し直すための目印)、
        summary は名前が "#!" で始まるときにコメントから抜いた
        「何のファイルか」の一文 (取れなければ空)、label は表示用で、
        summary があればそれ、無ければ名前を空白で畳んで切ったもの。
        候補が無ければ None。

        【後ろの Text 要素は拾わない】旧実装は候補の後ろに続く Text を
        発言の冒頭として拾っていたが、実機では文書自体の名前
        ("Chat — Muse") が返ってきた。カードの名前にこそ中身の
        冒頭が入っているので、要約はそこからだけ作る (1-22)。"""
        _root, desc = self._descendants()
        return self._candidate_dict(desc)

    def _diag_marker_in(self, desc):
        """診断リクエストの目印 (DIAG_REQUEST_MARKER) を探す (1-43)。

        戻り値は {"id": 目印の同定子} か、無ければ None。同定子は
        「目印の行までの本文」のハッシュで、同じ発言なら何度走査
        しても同じ値になり、別の発言 (前に別の会話が積み上がって
        いる) では違う値になる。窓側はこれで「同じ発言で二度
        送らない」を判定する。

        数えるのは、目印より後ろの本文が DIAG_MARKER_TAIL_LIMIT
        以内のものだけ。会話のずっと前の発言に目印が残っていても、
        後ろに本文が長く続くので対象にならない。本文の取り方は
        document_text と同じ (Text 要素の名前を木の順に読む) だが、
        行の判定が要るので、要素ごとに名前を改行で割って行の列に
        してから見る。"""
        lines = []
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                if el.CurrentControlType != CONTROL_TEXT:
                    continue
                name = el.CurrentName or ""
            except Exception:  # noqa: BLE001  消えかけの要素は読み飛ばす
                continue
            lines.extend(name.replace(OBJECT_MARK, "").split("\n"))
        marker_at = None
        for index, line in enumerate(lines):
            if line.strip() == DIAG_REQUEST_MARKER:
                marker_at = index  # 最後の目印を相手にする
        if marker_at is None:
            return None
        tail = sum(len(line) + 1 for line in lines[marker_at + 1:])
        if tail > DIAG_MARKER_TAIL_LIMIT:
            return None  # 古い発言の中の目印。最新の呼びかけではない
        prefix = "\n".join(lines[:marker_at + 1])
        digest = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        return {"id": digest}

    def latest_chat_scan(self):
        """チャットの見張りの1回分 (候補 + 診断の目印) を、同じ
        子孫の並びから取る (1-43)。

        戻り値は {"candidate": 候補の辞書か None, "marker":
        {"id": ...} か None}。見張りはこの1回で両方を見る (走査を
        2回に分けない)。"""
        _root, desc = self._descendants()
        return {
            "candidate": self._candidate_dict(desc),
            "marker": self._diag_marker_in(desc),
        }

    def _message_options_button(self, desc, start):
        """カードの後ろにある、その発言の「その他のオプション」ボタン。

        カードの位置 (start) から木の順に後ろを探す。次の添付カードに
        当たったら、その発言は終わりなので打ち切る (そこから先の
        「…」は別の発言のもの)。上限は OPTIONS_SEARCH_LIMIT 件。
        見つからなければ None。"""
        for step in range(1, OPTIONS_SEARCH_LIMIT + 1):
            i = start + step
            if i >= desc.Length:
                break
            el = desc.GetElement(i)
            try:
                if el.CurrentControlType != CONTROL_BUTTON:
                    continue
                name = el.CurrentName or ""
                class_name = el.CurrentClassName or ""
            except Exception:  # noqa: BLE001  消えかけの要素は読み飛ばす
                continue
            if "group/card" in class_name:
                break  # 次の発言のカード。ここから先は別の発言
            if name.strip() == OPTIONS_BUTTON_NAME:
                return el
        return None

    def _activate_element(self, el):
        """要素を「押す」。使える仕組みを _ACTIVATION_CHAIN の順に
        試し、最後は座標クリックに落とす (1-32)。

        戻り値は使えた手段の表示名。全部駄目なら RuntimeError
        (試した手段と理由を畳んだ文) を投げる。段つき
        (InputStageError) への包み直しは呼び側の役目。"""
        errors = []
        for pattern_id, iface_name, method_name, label in \
                _ACTIVATION_CHAIN:
            pattern = el.GetCurrentPattern(pattern_id)
            if not pattern:
                continue
            try:
                target = pattern.QueryInterface(
                    getattr(UIA, iface_name))
                getattr(target, method_name)()
                return label
            except Exception as e:  # noqa: BLE001  次の仕組みに進む
                errors.append(f"{label}: {e}")
        try:
            _click_element(el)
            return "Click"
        except Exception as e:  # noqa: BLE001  理由は呼び側の文に畳む
            errors.append(f"Click: {e}")
        raise RuntimeError(
            " / ".join(errors) if errors else "押す仕組みがありません")

    def _activate_options_button(self, el):
        """「その他のオプション」を開く。

        実機 (2026-11-14 の全ボタン調査) では、このボタンに
        InvokePattern が無いものが複数あった (「押下: 不可」)。
        teams_meeting の「その他」も同じで、ExpandCollapse で開く。
        押下そのものは「ダウンロード」の項目と共通の手順
        (_activate_element) に寄せた (1-32)。"""
        try:
            self._activate_element(el)
        except RuntimeError as e:
            raise InputStageError(
                "unwritable",
                "その他のオプションボタンを押せませんでした"
                f"（押す仕組みが無く、クリックも効きませんでした: {e}）"
            ) from e

    @staticmethod
    def _find_download_item(desc):
        """開いたメニューの「ダウンロード」項目。無ければ None。

        種類は MenuItem が本命。作りによっては Button / ListItem で
        出ることもあるので、その3種に限って名前の完全一致で探す。"""
        if desc is None:
            return None
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                if el.CurrentControlType not in (
                        CONTROL_MENU_ITEM, CONTROL_BUTTON,
                        CONTROL_LIST_ITEM):
                    continue
                if (el.CurrentName or "").strip() == DOWNLOAD_ITEM_NAME:
                    return el
            except Exception:  # noqa: BLE001  消えかけの要素は読み飛ばす
                continue
        return None

    def _in_menu_context(self, el):
        """要素がメニューの中にあるか。親を RawViewWalker でたどり、
        Menu に当たれば True。

        デスクトップ全体の探索で拾った要素の確認にだけ使う
        (1-32)。Edge のダウンロード・フライアウトはダウンロードの
        たびに自動で開くので、ループ中は開いているのが常態。その
        中の要素 (ListItem 等) が名前だけで「ダウンロード」の項目に
        化ける恐れがあるため、メニューの外にあるものは項目として
        認めない。フライアウトを閉じに行く操作はしない (前提として
        受け入れる)。"""
        uia = getattr(self, "uia", None)
        if uia is None:
            return False
        try:
            walker = uia.RawViewWalker
        except Exception:  # noqa: BLE001  取れなければ認めない
            return False
        node = el
        for _depth in range(12):
            try:
                node = walker.GetParentElement(node)
            except Exception:  # noqa: BLE001
                return False
            if node is None:
                return False
            try:
                if node.CurrentControlType == CONTROL_MENU:
                    return True
            except Exception:  # noqa: BLE001  読めない親は辿り直せない
                return False
        return False

    def _wait_download_item(self):
        """「ダウンロード」項目が出るまで、少しだけ待って探す。

        戻り値は (項目, 見つけた場所, 余所の同名要素があったか)。
        場所は "window" (対象の窓の木) / "desktop" (デスクトップ
        全体) のどちらか。項目が無ければ (None, "", 余所フラグ)。

        探索は窓の木が本命。窓の外 (デスクトップ直下) にメニューを
        出す作りのアプリもあるので最後にデスクトップ全体からも
        1回探すが、そちらで拾ったものはメニューの中にあると
        確かめられたものだけを使う (1-32、フライアウト対策)。
        確かめられなかった同名要素は「余所」として呼び側に伝え、
        項目が見つからない段の文に添える。"""
        deadline = time.time() + DOWNLOAD_MENU_TIMEOUT
        while True:
            _root, desc = self._descendants(heal=False)
            item = self._find_download_item(desc)
            if item is not None:
                return item, "window", False
            if time.time() >= deadline:
                break
            time.sleep(DOWNLOAD_MENU_POLL)
        uia = getattr(self, "uia", None)
        true_cond = getattr(self, "true_cond", None)
        if uia is not None and true_cond is not None:
            try:
                desktop = uia.GetRootElement()
                desc = desktop.FindAll(
                    UIA.TreeScope_Descendants, true_cond)
                item = self._find_download_item(desc)
            except Exception:  # noqa: BLE001  探し方の保険。無ければ無い
                item = None
            if item is not None:
                if self._in_menu_context(item):
                    return item, "desktop", False
                return None, "", True
        return None, "", False

    def _target_process_id(self):
        """対象の窓 (チャット窓) のプロセス ID。取れなければ 0。

        ダウンロード・フライアウトの持ち主が「対象と同じブラウザ」
        か確かめるために使う。UIA の root 要素の CurrentProcessId
        で読む (窓ハンドル経由)。別アプリの同名パネルを閉じに行か
        ないための門で、取れないときは 0 を返し、呼び側は
        「確かめられないので触らない」に倒す。"""
        uia = getattr(self, "uia", None)
        hwnd = getattr(self, "hwnd_main", None)
        if uia is None or not hwnd:
            return 0
        try:
            root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
            return int(root.CurrentProcessId or 0)
        except Exception:  # noqa: BLE001  取れなければ触らない側に倒す
            return 0

    @staticmethod
    def _element_children(el, cond):
        """要素の子 (1段だけ) の並び。取れなければ空。"""
        try:
            found = el.FindAll(UIA.TreeScope_Children, cond)
            return [found.GetElement(i) for i in range(found.Length)]
        except Exception:  # noqa: BLE001  消えかけの要素は読み飛ばす
            return []

    def _flyout_close_button(self, panel):
        """パネルの中 (2段まで) にある「閉じる」ボタン。無ければ None。"""
        frontier = [panel]
        for _depth in range(2):
            children = []
            for el in frontier:
                children.extend(
                    self._element_children(el, self.true_cond))
            for child in children:
                try:
                    if child.CurrentControlType == CONTROL_BUTTON and \
                            (child.CurrentName or "").strip() == \
                            FLYOUT_CLOSE_BUTTON_NAME:
                        return child
                except Exception:  # noqa: BLE001  読めない要素は飛ばす
                    continue
            frontier = children
        return None

    def _flyout_at(self, el, pid, depth):
        """el かその子孫 (深さ上限まで) にフライアウトがあれば
        (パネル, 閉じるボタン) を返す。無ければ None。

        【見分け方】対象と同じプロセスの持ち物で、種類が
        Window / Pane / Dialog、名前がちょうど「ダウンロード」で、
        中に「閉じる」ボタンがあるか、畳む仕組み (ExpandCollapse)
        を持っているもの。名前だけで認めると「ダウンロード」の
        項目や見出しまで拾うので、形 (閉じるボタンか畳む仕組み)
        まで含めて1組にする。対象の窓そのものは、名前が一致して
        もパネルとは認めない (中を探るだけにする)。プロセスが
        違う要素は、その下も含めて相手にしない。"""
        try:
            if int(el.CurrentProcessId or 0) != pid:
                return None
            name = (el.CurrentName or "").strip()
            control_type = el.CurrentControlType
            handle = el.CurrentNativeWindowHandle
        except Exception:  # noqa: BLE001  読めない要素は相手にしない
            return None
        own_window = bool(handle) and \
            handle == getattr(self, "hwnd_main", None)
        if name == FLYOUT_TITLE and not own_window and \
                control_type in (CONTROL_WINDOW, CONTROL_PANE,
                                 CONTROL_DIALOG):
            close = self._flyout_close_button(el)
            collapsible = False
            if close is None:
                try:
                    collapsible = bool(el.GetCurrentPattern(
                        EXPAND_COLLAPSE_PATTERN))
                except Exception:  # noqa: BLE001
                    collapsible = False
            if close is not None or collapsible:
                return el, close
        if depth <= 0:
            return None
        for child in self._element_children(el, self.true_cond):
            hit = self._flyout_at(child, pid, depth - 1)
            if hit is not None:
                return hit
        return None

    def _find_downloads_flyout(self):
        """デスクトップ側からフライアウトを探す。無ければ None。

        探索は、デスクトップ直下の要素と、その浅い子孫 (深さ
        FLYOUT_SEARCH_DEPTH まで) だけ。木全体は歩かない。
        対象のプロセスが取れなければ、持ち主を確かめようがない
        ので無いものとして返す (触らない)。"""
        uia = getattr(self, "uia", None)
        cond = getattr(self, "true_cond", None)
        if uia is None or cond is None:
            return None
        pid = self._target_process_id()
        if not pid:
            return None
        try:
            desktop = uia.GetRootElement()
            tops = desktop.FindAll(UIA.TreeScope_Children, cond)
        except Exception:  # noqa: BLE001  探せなければ無いものとする
            return None
        for i in range(tops.Length):
            hit = self._flyout_at(
                tops.GetElement(i), pid, FLYOUT_SEARCH_DEPTH)
            if hit is not None:
                return hit
        return None

    def send_escape(self):
        """対象の窓へ ESC を送る (1-67)。状態語を返すだけで、
        例外は出さない。

        Edge のダウンロード・フライアウトを閉じるためのキー配送。
        宛先は、自分が掴んでいる窓のトップレベル (hwnd_main)。
        standalone の操作として窓側 (PHAL) からも呼ばれる (DL の
        到着が確かめられた直後)。前面化も SendInput もしない。"""
        return _post_escape(getattr(self, "hwnd_main", None))

    def _dismiss_downloads_flyout(self):
        """フライアウトが開いていれば閉じる。結果を短い語で返す。

        戻り値: "closed" (閉じるボタンを押した) / "collapsed"
        (畳んだ) / "absent" (無かった・探せなかった) / "left"
        (閉じられなかった・残っている)。どの結果でも例外は
        出さない。DL 押下の本筋は結果によらず続ける (既存の
        代替手順と再試行が保険として残っている)。"""
        found = self._find_downloads_flyout()
        if found is None:
            return "absent"
        panel, close = found
        acted = ""
        try:
            if close is not None:
                self._activate_element(close)
                acted = "closed"
            else:
                pattern = panel.GetCurrentPattern(
                    EXPAND_COLLAPSE_PATTERN)
                if pattern:
                    pattern.QueryInterface(
                        UIA.IUIAutomationExpandCollapsePattern
                    ).Collapse()
                    acted = "collapsed"
        except Exception:  # noqa: BLE001  閉じ損ねても本筋は続ける
            acted = ""
        if not acted:
            return "left"
        time.sleep(FLYOUT_DISMISS_WAIT)
        if self._find_downloads_flyout() is not None:
            return "left"
        return acted

    def press_download_candidate(self):
        """最新の添付をダウンロードする (2段で押す)。

        【カード自体は押さない】1-22 までの実装はカードを Invoke
        していたが、実機 (2026-11-15、持ち主の注釈つき画面) では、
        カードのクリックはアプリ内のプレビューを開くだけで、
        ダウンロードは始まらなかった。本当の経路は、その発言の
        右上にある「その他のオプション」(…) を開いて、メニューの
        「ダウンロード」を押すこと。

        手順: 最初に、対象の窓へ ESC を投げ (1-67)、Edge の
        ダウンロード・フライアウトが開いていれば閉じる
        (_dismiss_downloads_flyout、1-40)。開いている間は
        「…」の押下が実機で一度も成功していないため。
        そのあと、検出から時間が経つと木は変わるので、保存した要素は
        使わず、検出と同じ選び方 (_pick_download_candidate) で
        カードを探し直す → カードの後ろにある、その発言の「…」を
        押す → 「ダウンロード」の項目が出るのを少し待って押す。
        押下は共通の代替手順 (_activate_element) で行い、項目の
        押下が一通り失敗したら、間を置いて見つけ直し+押下を
        1回だけやり直す (1-32)。
        失敗は段つき (InputStageError): カードが無い / 「…」が
        無い / 「…」を押せない / メニューが出ない / 項目を押す
        仕組みが無くクリックも効かない。項目まわりの文には、
        項目をどちらの探索 (窓の木 / デスクトップ) で見つけたかを
        添える (次の切り分けのため)。押せたら True
        (＝「ダウンロード」の項目を押した、までを完了とする)。"""
        # フライアウトを閉じるのは最初にやる。まず ESC を投げ
        # (1-67)、次に UIA の解散 (1-40) を試す。閉じきれなかった
        # 場合は、そのことを項目まわりの失敗文に添える (次の
        # 切り分けのため。それ以外の段の文には足さない)。
        # ESC の結果も同じ文に短く添える (診断の控えに残る)。
        self._esc_before_press = self.send_escape()
        self._flyout_dismissal = self._dismiss_downloads_flyout()
        stuck = ("（ダウンロードパネルが開いたままでした）"
                 if self._flyout_dismissal == "left" else "")
        stuck += f"（ESC送信: {self._esc_before_press}）"
        _root, desc = self._descendants()
        picked = self._pick_download_candidate(desc)
        if picked is None:
            raise InputStageError(
                "not_found",
                "ダウンロードできるものが見つかりませんでした"
                "（チャットの内容が変わった可能性があります）")
        index, _el, _name, _class_name = picked
        options = self._message_options_button(desc, index)
        if options is None:
            raise InputStageError(
                "not_found",
                "その他のオプションボタンが見つかりませんでした"
                "（カードの後ろに、その発言の「…」がありませんでした）")
        self._activate_options_button(options)
        last_failure = None
        for attempt in range(2):
            if attempt:
                time.sleep(DOWNLOAD_PRESS_RETRY_WAIT)
            item, source, stray = self._wait_download_item()
            if item is None:
                if stray:
                    raise InputStageError(
                        "not_found",
                        "ダウンロードの項目が見つかりませんでした"
                        "（デスクトップに同名の要素はありましたが、"
                        "メニューの中とは確かめられませんでした）"
                        + stuck)
                raise InputStageError(
                    "not_found",
                    "ダウンロードの項目が見つかりませんでした"
                    "（メニューが開かなかった可能性があります）"
                    + stuck)
            try:
                self._activate_element(item)
                return True
            except RuntimeError as e:  # noqa: BLE001  1回だけやり直す
                last_failure = (e, source)
        error, source = last_failure
        where = "窓の木" if source == "window" else "デスクトップ"
        raise InputStageError(
            "unwritable",
            "ダウンロードの項目は見つかりましたが"
            f"（{where}で発見）、押す仕組みが無く、"
            f"クリックも効きませんでした（{error}）{stuck}") from error

    def send_prompt(self, text, attempts=2, wait=3.0):
        """入力欄に書いて送信する。送れたら True。

        【この再試行が効くのは「出るのが遅れた」場合だけ】
        SetValue の直後は、アプリがまだ送信ボタンを描いていないことがある。
        click_send は3秒までポーリングするので普通はそこで足りるが、足りなかった
        ときの保険として、書き直してもう一度だけ試す。

        【以前ここに書いてあった説明は誤りだった】
        「SetValue に気づかず送信ボタンが出ないことがあり、書き直すと再描画が走って
        直る」と書いていた。根拠は「3回続けて落ちたのに、直後に手でなぞったら0.18秒で
        通った」という観察。だが後日ちゃんと測ったところ、**原因は描画ではなく
        送信ボタンの探し方だった** —— 会話を始めていないランディング画面では入力欄が
        画面の中ほどにあり、「窓の下端からの帯」で探していたので900px以上外れていた
        (_composer_buttons を参照)。手でなぞったときに通ったのは、その時点で会話が
        始まっていて入力欄が下端に来ていたから。

        **同じ症状には複数の原因がありうる**、という教訓として残しておく。あのとき
        分からないまま再試行だけ足したのは、落ちなくする手当てとしては正しかったが、
        docstring に推測を断定として書いたのは間違いだった。

        押せなかったときは、そのとき入力欄まわりに見えていたボタン名を
        last_composer_buttons に残す。次に起きたときに、また推測から始めないで済むように。"""
        self.last_composer_buttons = []
        for _attempt in range(max(1, attempts)):
            self.set_input(text)
            if self.click_send(wait=wait):
                return True
            try:
                root, desc = self._descendants()
                self.last_composer_buttons = [
                    n for n, _el in self._composer_buttons(root, desc)]
            except Exception:  # noqa: BLE001  記録のために落ちない
                pass
        return False

    def click_send(self, wait=3.0, poll=0.2):
        """送信ボタンを押す。フォーカスは奪わない。

        set_input の直後は Chromium がまだ送信ボタンを出していないことがある
        (前実装では 2.6 秒で「送信ボタンが見つかりません」で停止した実例あり)。
        wait 秒までポーリングして、ready 状態になったら即押す。"""
        end = time.time() + max(0.0, wait)
        while True:
            root, desc = self._descendants()
            for name, el in self._composer_buttons(root, desc):
                if name not in self._names("send_button"):
                    continue
                pattern = el.GetCurrentPattern(INVOKE_PATTERN)
                if not pattern:
                    raise RuntimeError("送信ボタンを押せません（InvokePattern が無い）")
                pattern.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
                return True
            if time.time() >= end:
                return False
            time.sleep(poll)

    # -- 読む --------------------------------------------------------------
    def document_text(self):
        """会話全体を読み順どおりに取る。

        【3案を実測して、これに決めた】
        1. Text 要素を座標(top, left)で並べる … 駄目。構文強調でトークンごとに
           要素が分かれ、同じ行の要素は top が同じなので順序が定まらない。
        2. TextPattern.DocumentRange … 順序は正しいが、一部のトークン境界で改行が
           入る。@{ が @ と { に割れ、-Encoding UTF8 が2行になり構文エラーになる。
        3. FindAll の並びのまま Text の名前を繋ぐ … これが正解。FindAll は既に
           木の順(＝DOMの順)で返すので、並べ替えてはいけない。空白も改行も独立した
           Text 要素なので、連結するだけでコードが完全に復元される。

        つまり「並べ替えない」ことが肝。1案が失敗したのは座標で並べ替えたせい。"""
        _root, desc = self._descendants()
        parts = []
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                if el.CurrentControlType != CONTROL_TEXT:
                    continue
                name = el.CurrentName or ""
            except Exception:
                continue
            # コードブロックの言語ラベルは、**独立した Text 要素**として現れる
            # (実測 2026-09-12: '#start GO-001' / 'Powershell' / 'go mod init demo'
            #  の3要素が並ぶ)。素直に繋ぐと 'Powershellgo mod init demo' になり、
            # そのまま実行されて「'Powershellgo' は認識されません」で失敗する。
            #
            # 【文字列で剥がすのをやめた理由】
            # 繋いだ後から先頭の言語名を削る作りにしていたが、本物のコードの
            # 先頭語(`python -c ...`)まで食う/`Powershellgo` は剥がせない、と
            # どちらにも倒せなかった。**要素の切れ目が分かる読み取りの時点で
            # 行を分ける**ほうが素直で、後段の「行まるごとがラベルなら落とす」が
            # そのまま効く。情報も捨てていない。
            if name.strip().lower() in LANG_LABELS:
                parts.append("\n" + name.strip() + "\n")
                continue
            parts.append(name)
        return "".join(parts).replace(OBJECT_MARK, "")

    def snapshot_length(self):
        """今の全文の長さを返す。send する直前に控えておいて、あとで new_response に渡す。"""
        return len(self.document_text())

    def new_response(self, previous_length, min_delta=10,
                     stable_seconds=1.5, timeout=15.0, poll=0.3,
                     sent_prompt=None):
        """previous_length より後ろの新しい部分から、最後の Copilot 応答を取り出す。

        【なぜ前実装が壊れたか】
        rsplit("Copilot の発言", 1)[1] は「全文にある最後のマーカー以降」を取る。
        会話が長くなると、直近の応答内に見出しとして「Copilot の発言」が混ざる場合や、
        古いターンの断片が最後尾になる場合があり、27文字などの短片が返る。

        【新しい方針】
        送信直前の全文長を previous_length として渡し、その位置より後ろだけを扱う。
        こちらの発言(user_marker で始まる)を捨て、最後の assistant_marker 以降を取る。

        【UIA の遅延対策】
        wait_until_idle 直後は UIA ツリーがまだ確定していないことがある。文字数が
        stable_seconds 連続で動かなくなるまで待ってから取り出す。        """
        last_len = -1
        stable_since = None
        start = time.time()
        while time.time() - start < timeout:
            text = self.document_text()
            n = len(text)
            if n < previous_length:
                # 全文が縮んだ。伸びるのを待っても埋まらないので、待たずに抜ける
                # (下の取り出しが目安を捨てて全文から拾い直す)。
                break
            if n == last_len and n - previous_length >= min_delta:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= stable_seconds:
                    break
            else:
                stable_since = None
                last_len = n
            time.sleep(poll)
        text = self.document_text()
        new_part = text[previous_length:]

        # 目安が現在の全文より後ろを指していたら、目安を捨てて全文から拾う。
        #
        # 【永久に空になっていた】
        # 会話が長くなると Copilot は古い発言をアクセシビリティツリーから外すので、
        # **全文長は減ることがある**。送信直前に控えた previous_length が現在の
        # 全文長を超えると text[previous_length:] は常に空で、いくら待っても
        # 埋まらない。実測(2026-09-11): Copilot は既に DONE と答えていたのに、
        # ループは空応答として3分待ち、最後は異常終了扱いになった。
        # **やり切ったのに失敗として報告される**、いちばん困る誤りかた。
        #
        # 下の取り出しは「最後の AI 発言以降」を取るので、全文を渡しても直近の
        # 応答に着地する。previous_length は「どこから見るか」の目安であって、
        # そこに寄りかからない —— この関数はもともとその方針で書かれている。
        if not new_part.strip():
            new_part = text

        # 空判定を先に置くこと。空文字はどんな文字列にも「含まれる」ので、
        # マーカー未設定のプロファイル(M365 Copilot)では split("") が
        # ValueError: empty separator で落ちる。
        marker_user = self.profile.get("user_marker") or ""
        marker_ai = self.profile.get("assistant_marker") or ""

        # 【順序が肝】「最後の AI 発言以降を取る」→「次の人の発言の手前で切る」。
        #
        # 逆順(先に最初の user_marker で切る)にしていたら、実機で会話と無関係な
        # ニュース本文が「応答」として返った。previous_length は送信直前の全文長だが、
        # Copilot がそのときニュース面を出していると本文が短く、送信後に会話へ
        # 切り替わって全文がまるごと入れ替わる。すると new_part に過去のターンまで
        # 入り、「最初の"あなたの発言"の手前」＝ニュース本文になってしまう。
        #
        # 先に最後の AI 発言を捕まえておけば、previous_length がずれていても
        # 直近の応答に着地する。previous_length は「どこから見るか」の目安であって、
        # そこに寄りかからない作りにしておくこと。
        if marker_ai and marker_ai in new_part:
            new_part = new_part.rsplit(marker_ai, 1)[1]
        elif not marker_ai and sent_prompt:
            # マーカーを持たないアプリ用の代替。送った本文は会話に echo される
            # ので、その終わりまでを捨てれば残りが応答になる。マーカーを
            # 突き止められなくても、こちらの道で応答を取り出せる。
            new_part = strip_echoed_prompt(new_part, sent_prompt)
        if marker_user and marker_user in new_part:
            new_part = new_part.split(marker_user)[0]

        # 入力欄まわりの UI 文字列(「Copilot へメッセージを送る」等)が末尾に混ざる。
        # 本文ではないので、**最初に現れたところから後ろを丸ごと**落とす。
        #
        # 【末尾一致では足りなかった】
        # 以前は「末尾がちょうどその文字列なら削る」だった。ところが実機では
        # UI 文字列が続けて何本も並ぶ:
        #   'DONE' + 'Copilot へメッセージを送る' + 'グループを開始する' + '一緒に…'
        # 末尾は最後のものなので、どれとも一致せず素通りしていた。その結果、
        # 応答が 'DONEC…' となって完了語 DONE が単語の途中扱いになり、
        # **やり切ったのに finish-word で止まらなかった**(実測 2026-09-11)。
        #
        # 本文がこれらの文言を含むことは考えにくいが、含んでいればそこで切れる。
        # 取りこぼすより切りすぎるほうが、症状が目に見えるぶん扱いやすい。
        cut = len(new_part)
        for idle_marker in self._names("idle_marker"):
            if not idle_marker:
                continue
            at = new_part.find(idle_marker)
            if at != -1:
                cut = min(cut, at)
        return new_part[:cut].strip()

    def code_blocks(self):
        """コードブロックとして描かれた本文だけを集めて返す。判定できなければ None。

        【何のためにあるか】
        Copilot がコードを ``` で囲まずに返すと、本文が Markdown として描画され、
        **画面から文字が消える**。実測(2026-09-12):

            元:  # collect .py files ...      → 読取: collect .py files ...
            元:  "Lines`tPath"                → 読取: "LinestPath"
            元:  $_.FullName                  → 読取: $.FullName

        `#`(見出し)、`` ` ``(インラインコード)、`_`(強調)が Markdown の記号として
        食われる。**消えた文字はどの UIA 要素にも存在しない**ので、読み取り方を
        工夫しても取り戻せない(全型を走査して確認済み)。

        たちが悪いのは、これが「動かない」ではなく「**別のコードに化ける**」こと。
        コメント行から `#` が落ちれば、コメントのつもりの行が実行される。

        【見分け方】
        囲まれた本文には、直前に「コードをコピー」ボタンが付く。囲まれていない本文には
        付かない。実測で、壊れていた3つの応答はいずれもボタンを伴わず、無傷だった
        2つはいずれも伴っていた。名前はプロファイルで差し替えられる。

        目印の名前を持たないプロファイル(M365 は未実測)では判定できないので None を
        返す。呼ぶ側は「判定できない」と「囲まれていない」を混ぜないこと —— 混ぜると、
        未実測のアプリで何も実行できなくなる。"""
        wanted = self._names("code_copy_button")
        if not wanted:
            return None
        _root, desc = self._descendants()
        els = []
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                els.append((el.CurrentControlType, el.CurrentName or ""))
            except Exception:  # noqa: BLE001  消えた要素は飛ばす
                els.append((-1, ""))
        # **1本のブロックが、たくさんの Text 要素に割れている。**
        #
        # 【実測 2026-09-12】構文強調が効くと、トークンごとに要素が分かれる:
        #   BTN 'コードをコピー' / '(' / 'Get-Content' / ' ' / 'R' / ':\claude\tray'
        #   / '-tools' / '\src\hook.log ' / '| ' / 'Measure-Object' / ...
        # 最初の1個だけを見ていたので、ブロックの中身が `(` になっていた。当然どの
        # 行とも一致せず、**囲まれているのに「囲まれていない」と判定された**
        # (実測: 2周続けてそうなり、実行されないまま停止した)。
        # ヒアストリング(@' … '@)のときは丸ごと1要素になるので、これまでは表に
        # 出なかった。たまたま当たっていただけ。
        #
        # 【どこまでが1本か】
        # 同じブロックのトークンは**親が同じ**で、ブロックの外(`#end PS-003` や
        # 「期待: …」の段落)は別の親になる(実測: 親の RuntimeId が 1188065 →
        # 1188077/1188078 と変わる)。改行や行数では切れ目が分からないので、
        # 親が変わったところで切る。
        walker = None
        try:
            walker = self.uia.RawViewWalker
        except Exception:  # noqa: BLE001  取れなければ従来どおり先頭だけ見る
            walker = None

        def parent_id(index):
            """その要素の親の RuntimeId。取れなければ None(比べない)。"""
            if walker is None:
                return None
            try:
                par = walker.GetParentElement(desc.GetElement(index))
                return tuple(par.GetRuntimeId()) if par else None
            except Exception:  # noqa: BLE001  消えた要素・権限などで落ちうる
                return None

        blocks = []
        for i, (t, name) in enumerate(els):
            if t != CONTROL_BUTTON or name.strip() not in wanted:
                continue
            # ボタンと本文の間に Image が挟まるので、少し先まで見る。
            head = None
            for j in range(i + 1, min(i + 5, len(els))):
                if els[j][0] == CONTROL_TEXT:
                    head = j
                    break
            if head is None:
                continue
            own = parent_id(head)
            parts = [els[head][1]]
            j = head + 1
            while j < len(els) and els[j][0] == CONTROL_TEXT:
                if own is None or parent_id(j) != own:
                    break
                parts.append(els[j][1])
                j += 1
            blocks.append("".join(parts))
        return blocks

    def last_response(self):
        """互換のために残す。previous_length を知らずに呼ぶと会話が長くなるほど
        誤爆しやすい(rsplit の最後のマーカーが応答の見出しにヒットする等)。
        新しい呼び出しからは snapshot_length + new_response を使うこと。
        """
        text = self.document_text()
        marker = self.profile["assistant_marker"]
        if marker not in text:
            return text
        tail = text.rsplit(marker, 1)[1]
        return tail.split(self.profile["user_marker"])[0].strip()


def strip_echoed_prompt(text, prompt):
    """会話に echo された自分の発言を捨てて、その後ろ(＝応答)を返す。

    【何のためか】
    発言マーカー(「あなたの発言」など)を持たないアプリでも応答を取り出せるように
    するため。M365 Copilot はそういう Text を出しておらず、マーカー方式が使えない。
    だが「送った本文」はこちらが知っているので、それを目印にすればよい。

    【素の find では当たらない】
    UIA が返す文字列は、入力した本文と空白・改行が一致しない。要素の切れ目に
    空白が入ったり、折り返しが改行になったりする。そこで空白を全部落とした形で
    突き合わせ、見つけた位置を元の文字列の位置へ翻訳して返す。

    突き合わせは全文一致だけにしてある。「一部だけ当てて切る」も考えたが、外すと
    応答の頭が欠けたものが「正しく取れた」顔で下流へ流れる。見つからなければ
    text をそのまま返し、echo が残っていると分かる形で呼び側へ渡すほうがよい。
    (その場合、送った本文が応答の頭に付いたまま返る。)"""
    if not text or not prompt:
        return text
    # 空白を落とした文字列と、その各文字が元の何文字目だったかの対応表を作る。
    dense, index_map = [], []
    for i, ch in enumerate(text):
        if not ch.isspace():
            dense.append(ch)
            index_map.append(i)
    dense = "".join(dense)
    needle = "".join(ch for ch in prompt if not ch.isspace())
    if not needle:
        return text

    hit = dense.find(needle)
    if hit == -1:
        return text
    end_dense = hit + len(needle) - 1
    if end_dense >= len(index_map):
        return text
    return text[index_map[end_dense] + 1:].lstrip()


# --- スニペットの切り出し ---------------------------------------------------
# プロンプト側で #start <ID> 〜 #end <ID> で囲ませてあるので、それを拾う。
# 言語フェンスを当てにするより確実で、IDで実行漏れ・重複実行も検知できる。
# ID の直後に空白が入らない（#end 1Copilot… と続く）ので、末尾は否定先読みで
# 1 と 12 を取り違えないようにする。
#
# 【ID を数字だけにしていて拾えなかった】
# 以前は (\d+) だった。プロンプトは「ID（採番はあなた）」としか言っていないので、
# Copilot は数字以外も使う。実測(2026-09-12)で `#start PS-001` と採番され、
# パターンに当たらず no-snippet で1周目に止まった。英数字とハイフン・下線を許す。
#
# ただし **数字を1つ以上含むこと** を条件にする。そうしないと、地の文の
# 「#starting … #ending」のような並びを ID='ing' として拾ってしまう(数字縛りが
# 偶然その防波堤になっていた)。実測で現れた ID は 1 / 001 / 24 / PS-001 で、
# どれも数字を含む。
#
# ID の直後に区切りが無い場合(`#start PS-001Powershell(...)`)もあるが、#end 側との
# 後方参照が効くので、正規表現のバックトラックで正しい境目に落ち着く。
SNIPPET_ID = r"[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*"
SNIPPET_RE = re.compile(
    r"#start\s*(" + SNIPPET_ID + r")(.*?)#end\s*\1(?![A-Za-z0-9_-])", re.DOTALL)

# コードブロックの言語ラベルが、コードの1行目として紛れ込むことがある。
# 表示上のラベルであってコードではないので剥がす。
#
# 【剥がすのは「その行がラベルだけ」のときに限る】
# 以前は先頭の言語名を無条件に削っていた。そのせいで、**本物のコードの先頭語まで
# 食っていた**。実測(2026-09-11): Copilot が `python -c 'assert False'` と書いたのに
# `-c 'assert False'` が実行され、「'-c' は認識されません」で失敗した。
# `cmd /c ...` や `bash -lc ...` でも同じことが起きる。
#
# 実際の画面ではラベルは #start の**前**に付く(「Powershell#start 29」)ので、
# コード側にラベルが来るのは、フェンスが別行に落ちた場合だけ。それは行まるごとが
# ラベルになるので、その形だけを剥がせば足りる。
LANG_LABELS = ("powershell", "pwsh", "python", "bash", "sh", "cmd", "batch",
               "json", "yaml")


def strip_lang_label(code):
    """1行目がコードブロックの言語ラベルだけなら、その行を落とす。

    ラベルは独立した Text 要素として現れるので、document_text が読み取りの時点で
    前後に改行を入れている。ここはその行を捨てるだけでよい。

    【文字列の当てずっぽうはやめた】
    一時期、繋がってしまったラベルを「直後の文字」から見分けて剥がそうとした
    (`Powershell(` は剥がす、`python -c` は残す)。`Powershellgo mod init demo` の
    ように直後が英字の場合に破綻し、`pythonw.exe` と区別できない。
    切れ目が分かる読み取りの時点で分けるほうが素直で、ここは単純なままでいられる。
    """
    lines = (code or "").split(chr(10))
    if lines and lines[0].strip().lower() in LANG_LABELS:
        return chr(10).join(lines[1:])
    return code


# 画面から読んだコードで、アンダースコアが落ちた跡。
#
# 【なぜ起きるか】
# Copilot がコードを ``` で囲まずに返すと、描画側が Markdown として解釈する。
# `__text__` は太字、`_text_` は斜体なので、**下線そのものが消えて描画される**。
# 実測: `__dunder__` が `dunder` になり、`$_.Name` が `$.Name` になった。
# 画面にもそう出ているので、UIA の読み取りを工夫しても取り返せない。
#
# 【なぜ機械的に直してよいか】
# `$.` は PowerShell の構文として成立しない。出てきた時点で「本来 `$_.` だった」と
# 断定できる。当てずっぽうではない。
UNDERSCORE_LOST_RE = re.compile(r"\$(?=\.)")


def repair_lost_underscores(code):
    """落ちたアンダースコアを戻す。(直したコード, 直した数) を返す。

    直せるのは `$.` → `$_.` だけ。識別子の中で消えた下線(`my_var` → `myvar`)は
    元の綴りを知りようがないので触らない。**直しきれないことがある**前提で、
    呼び側は「直した数」を見て人に知らせること。黙って直すと、直せなかった箇所が
    あったときに原因が見えなくなる。"""
    repaired, count = UNDERSCORE_LOST_RE.subn("$_", code or "")
    return repaired, count


# #start の行に書かせる「自己申告」。転送で文字が落ちていないかを実行前に見るため。
#
# 【なぜ要るか】
# 画面から読む以上、描画の都合で文字が落ちる経路は塞ぎきれない(囲み忘れて Markdown
# として描かれると # や ` や _ が消える)。囲み判定で大半は止まるが、複数のコード
# ブロックの間に挟まった行までは見ていない。**原因が何であれ、届いた本文が送られた
# 本文と違えば止める**という歯止めを、別の筋で1本持っておく。
#
#   #start 3 lines=24 chars=812
#   #start 3 lines=24 hashes=3 backticks=2
#
# 書き方は「キー=数値」の並び。順番は問わない。書かれたキーだけ照合する。
SNIPPET_CLAIM_RE = re.compile(
    r"(lines|chars|hashes|backticks|underscores)\s*=\s*(\d+)", re.IGNORECASE)


def split_claim(code):
    """コードの1行目が自己申告だけなら (申告, 残りのコード) に分ける。

    申告が無ければ ({}, コードそのまま)。申告の行にコードが混ざっている場合も
    触らない —— 迷う形のときは**何もしないほうを選ぶ**。誤って1行目を捨てると、
    黙ってコードが変わることになる。それは今まさに塞ごうとしている事故そのもの。"""
    text = code or ""
    lines = text.split(chr(10))
    if not lines:
        return {}, code
    head = lines[0].strip()
    if not head:
        return {}, code
    found = SNIPPET_CLAIM_RE.findall(head)
    if not found:
        return {}, code
    if SNIPPET_CLAIM_RE.sub("", head).strip():
        # 申告以外のものが同じ行にある。コードかもしれないので手を出さない。
        return {}, code
    return {k.lower(): int(v) for k, v in found}, chr(10).join(lines[1:])


def extract_snippets_with_claims(text):
    """[(ID, コード, 申告), ...] を返す。落ちたアンダースコアはここで戻す。"""
    result = []
    for hit in SNIPPET_RE.finditer(text or ""):
        body = strip_lang_label(hit.group(2).strip(chr(10)))
        claim, body = split_claim(body.strip(chr(10)))
        # **申告を外したあとで、もう一度ラベルを見る。**
        # ラベルが目印と申告の後ろに付くことがある(実測 2026-09-12:
        # '#start PS-003 lines=3 …' / 'Powershell' / '(Get-Content …)')。
        # 1回目の strip_lang_label のときは1行目が申告なので剥がれず、
        # 申告を外した拍子にラベルがコードの1行目に昇格して、そのまま
        # `Powershell` という行が実行される。剥がす条件は「その行がラベルだけ」
        # なので、2回呼んでも本物のコードは削らない。
        body = strip_lang_label(body.strip(chr(10)))
        code, _fixed = repair_lost_underscores(body.strip(chr(10)))
        result.append((hit.group(1), code, claim))
    return result


def extract_snippets(text):
    """[(ID, コード), ...] を返す。自己申告は取り除いた本文を返す。"""
    return [(sid, code) for sid, code, _claim in extract_snippets_with_claims(text)]


# ---------------------------------------------------------------------------
# 返信の3ブロック形式（照合 / 仮説 / スニペット / 期待 / 確信度）
# ---------------------------------------------------------------------------
# 【なぜラベルを日本語の素の語で拾うのか】
# この部分はコードブロックの**外**に書かれる。つまり Markdown として描画されてから
# UIA で読むことになり、`**期待**` の `**` は画面に残らない。太字で書かれても素で
# 書かれても、届く文字列はどちらも `期待:` になる。だから目印を `**` に置かず、
# 行頭の語＋コロンに置く。囲みの中の記号が消える問題(split_claim 参照)と同じ理屈で、
# **描画後に残るものだけを手掛かりにする。**
PROTOCOL_KEYS = {
    "照合": "verdict",
    "仮説": "hypothesis",
    "期待": "expectation",
    "確信度": "confidence",
}

# ラベル行の見分け方。行頭の飾り(`-` `*` `#` `>` `・`、太字が消えた跡の空白)を
# 落としてから語を見る。**語の直後にコロンか行末が要る。**
# ここを緩めて「語で始まればラベル」にすると、「期待される結果は…」のような
# 普通の文がラベルに化ける。緩い判定は、いつか必ず当たる。
PROTOCOL_LABEL_RE = re.compile(
    r"^[\s\-\*#>・:：]*(" + "|".join(PROTOCOL_KEYS) + r")\s*(?:[:：]|$)")

# **画面から読んだ本文には、段落の切れ目が無い。**
#
# 【実測 2026-09-12】届いた文字列はこうなっていた:
#   '照合: 初回仮説: まずは hook.log の行数だけを読み取ればよい。…'
# 段落はそれぞれ独立した Text 要素なのに、document_text は要素を**繋ぐだけ**で
# 改行を入れない(そうしないとコードが復元できない。トークンも独立した要素だから)。
# 行頭だけを見ていると、2つ目以降のラベルが本文に飲み込まれる。実際それで
# 「照合: 初回」が『照合の中に一致という語がある』と読まれ、**初回の周が
# 「一致」として記録された**。記録が嘘になるのは、測れないことより悪い。
#
# ラベル＋コロンはこちらが決めた目印なので、そこに改行を入れ直してよい。
# 直すのはこの層。document_text には触らない(あちらはコードの復元が仕事)。
PROTOCOL_SPLIT_RE = re.compile(
    r"(?<!^)(?=(?:" + "|".join(PROTOCOL_KEYS) + r")\s*[:：])", re.MULTILINE)

# 照合の答え。**「不一致」を先に見ること**(「一致」は「不一致」の部分文字列)。
VERDICT_WORDS = [("不一致", "mismatch"), ("一致", "match"), ("初回", "first")]

# ログに残す1項目の上限。仮説3行・期待2行と指定しているので、これを超えるのは
# 指示どおりに書けていないとき。切ってよいが、切れることは承知しておく。
PROTOCOL_FIELD_LIMIT = 600


def parse_protocol(text):
    """応答から照合・仮説・期待・確信度を取り出す。無いキーは None。

    スニペット本体は先に取り除く。コードのコメントに「# 期待: …」と書かれること
    があり、それを拾うと**実行したコードの中身を AI の申告として記録する**ことに
    なる。記録がずれれば、あとで見た人が間違った結論を出す。"""
    stripped = SNIPPET_RE.sub(chr(10), text or "")
    stripped = PROTOCOL_SPLIT_RE.sub(chr(10), stripped)
    sections = {}
    current = None
    for raw in stripped.split(chr(10)):
        hit = PROTOCOL_LABEL_RE.match(raw)
        if hit:
            current = PROTOCOL_KEYS[hit.group(1)]
            rest = raw[hit.end():].strip()
            sections.setdefault(current, [])
            if rest:
                sections[current].append(rest)
            continue
        if current is None:
            continue
        if not raw.strip():
            # 空行でその項目は終わり。次の段落まで飲み込むと、仮説の続きに
            # 見出しや雑談が混ざる。
            current = None
            continue
        sections[current].append(raw.strip())

    out = {}
    for key in ("verdict", "hypothesis", "expectation", "confidence"):
        joined = chr(10).join(sections.get(key, [])).strip()
        out[key] = joined[:PROTOCOL_FIELD_LIMIT] if joined else None

    # 照合は「一致」「不一致」「初回」のどれかで始まる約束。続きは所見として残す。
    verdict_text = out.pop("verdict") or ""
    out["verdict"] = None
    out["verdict_note"] = None
    if verdict_text:
        # **語は「含まれるか」ではなく「そこから始まるか」で見る。**
        # 含まれるかで見ていたせいで、「照合: 初回」に続く仮説の文中の『一致』を
        # 拾い、初回の周を『一致』として記録した(実測 2026-09-12)。
        # 判定を1文字ぶん緩めると、集計そのものが静かに嘘になる。
        head = verdict_text.lstrip(" 　*-:：")
        for word, name in VERDICT_WORDS:
            if head.startswith(word):
                out["verdict"] = name
                # 所見は次の行に書かれることがある(実測)。改行も落とす。
                out["verdict_note"] = (
                    head[len(word):].strip().strip(" 　:：、。").strip() or None)
                break
        else:
            # 指示どおりの語が無い。判定はしないが、何と書いてきたかは残す。
            # 「どう書かせれば守られるか」はこれから測るところで、守られなかった
            # 実例こそ材料になる。
            out["verdict"] = "unclear"
            out["verdict_note"] = verdict_text

    # 確信度は「確信度:」の行ではなく、期待の文中に紛れることがある。
    level = None
    for src in (out.get("confidence"), out.get("expectation")):
        if not src:
            continue
        m = re.search(r"(高|中|低)", src)
        if m:
            level = m.group(1)
            break
    out["confidence"] = level
    return out


# 実行前に必ず目視する。ここに引っかかるものは自動実行しない。
DANGEROUS = [
    (r"\bRemove-Item\b", "ファイル削除"),
    (r"\bStop-Process\b", "プロセス強制終了"),
    (r"\bStop-Computer\b|\bRestart-Computer\b", "シャットダウン/再起動"),
    (r"\bSet-ExecutionPolicy\b", "実行ポリシー変更"),
    (r"\bInvoke-WebRequest\b|\bInvoke-RestMethod\b|\bcurl\b|\bwget\b", "外部通信"),
    (r"\bRegistry::|\bSet-ItemProperty\b.*HK", "レジストリ書き換え"),
    (r"\bFormat-Volume\b|\bdiskpart\b", "ディスク操作"),
    (r"\bgit\s+push\b|\bgit\s+reset\b", "git の破壊的操作"),
    (r"\bNew-Item\b.*-Force", "-Force 付きの作成（上書きの恐れ）"),
]


def risky_lines(code):
    """危なそうな行を [(行, 理由), ...] で返す。空なら見た限り安全。"""
    hits = []
    for line in (code or "").splitlines():
        for pattern, why in DANGEROUS:
            if re.search(pattern, line, re.IGNORECASE):
                hits.append((line.strip(), why))
                break
    return hits


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cp = Copilot()
    print("窓:", cp.hwnd_main, "/ レンダラ:", cp.hwnd_render)
    print("状態:", cp.state())
    print("入力欄:", repr(cp.read_input()))
    response = cp.last_response()
    print(f"最後の応答: {len(response)} 文字")
    snippets = extract_snippets(response)
    print(f"#start/#end のスニペット: {len(snippets)} 個")
    print()
    print("--- 応答の末尾 600 文字 ---")
    print(response[-600:])
