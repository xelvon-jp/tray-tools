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
import re
import sys
import time

import comtypes
import comtypes.client

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
        "input_band_px": 170,
    },
]

# 既定のプロファイル。SELECTORS という名前は、これ1つで回していた頃からの呼び名。
# Copilot インスタンスは self.profile を見るので、実際に効くのはそちら。
SELECTORS = BUILTIN_PROFILES[0]

SETTINGS_PROFILES_KEY = "copilot_profiles"

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
VALUE_PATTERN, INVOKE_PATTERN, TEXT_PATTERN = 10002, 10000, 10014
WM_GETOBJECT, OBJID_CLIENT, SMTO_ABORTIFHUNG = 0x003D, 0xFFFFFFFC, 0x0002

# 埋め込みオブジェクト(ボタンや画像)の位置に入る文字。本文には要らない。
OBJECT_MARK = "￼"

user32 = ctypes.windll.user32
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


user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
user32.GetWindowRect.restype = ctypes.c_bool


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
    exe 名が取れない環境では、その条件だけ飛ばして残りで絞る。"""
    want_exe = (profile.get("process_name") or "").lower()
    want_class = profile.get("window_class") or ""
    want_title = profile.get("window_title_contains") or ""
    found = {}
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def on_window(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        if want_class and _class_of(hwnd) != want_class:
            return True
        if want_title and want_title.lower() not in _title_of(hwnd).lower():
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
        """入力欄の要素。プロファイルの aid のどれかに一致するものを返す。

        aid をリストで受けるのはボタン名と同じ理由。加えて、業務PC の値は画面を
        読み上げてもらって書き取ったもので、聞き取り違いの余地がある。候補を
        並べておけば、一発勝負にならずに済む。"""
        wanted = self._names("input_automation_id")
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                if (el.CurrentAutomationId or "") in wanted:
                    return el
            except Exception:
                continue
        return None

    def _bottom_buttons(self, root, desc):
        """入力欄まわり(窓の下端から一定の帯)のボタン名。状態はここに出る。

        帯の高さをプロファイルで変えられるようにしてあるのは、アプリごとに入力欄の
        まわりの作りが違うため。M365 Copilot も 170px で送信・停止の両方が入ることを
        実測で確かめてあるが、レイアウトが変わったときにコードを触らず直せる。"""
        limit = root.CurrentBoundingRectangle.bottom - int(
            self.profile.get("input_band_px") or 170)
        names = []
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                if el.CurrentControlType != CONTROL_BUTTON:
                    continue
                if el.CurrentBoundingRectangle.top < limit:
                    continue
                name = (el.CurrentName or "").strip()
                if name:
                    names.append((name, el))
            except Exception:
                continue
        return names

    # -- 状態 --------------------------------------------------------------
    def state(self):
        """'busy'(回答中) / 'ready'(送信できる) / 'idle'(入力待ち・空) を返す。"""
        root, desc = self._descendants()
        names = [n for n, _ in self._bottom_buttons(root, desc)]
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
        names = [n for n, _ in self._bottom_buttons(root, desc)]
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
        """入力欄に文字を入れる。送信はしない。"""
        _root, desc = self._descendants()
        box = self._input_box(desc)
        if box is None:
            raise RuntimeError("入力欄が見つかりません")
        pattern = box.GetCurrentPattern(VALUE_PATTERN)
        if not pattern:
            raise RuntimeError("入力欄に書き込めません（ValuePattern が無い）")
        pattern.QueryInterface(UIA.IUIAutomationValuePattern).SetValue(text)

    def read_input(self):
        _root, desc = self._descendants()
        box = self._input_box(desc)
        if box is None:
            return None
        pattern = box.GetCurrentPattern(VALUE_PATTERN)
        if not pattern:
            return None
        return pattern.QueryInterface(UIA.IUIAutomationValuePattern).CurrentValue

    def click_send(self, wait=3.0, poll=0.2):
        """送信ボタンを押す。フォーカスは奪わない。

        set_input の直後は Chromium がまだ送信ボタンを出していないことがある
        (前実装では 2.6 秒で「送信ボタンが見つかりません」で停止した実例あり)。
        wait 秒までポーリングして、ready 状態になったら即押す。"""
        end = time.time() + max(0.0, wait)
        while True:
            root, desc = self._descendants()
            for name, el in self._bottom_buttons(root, desc):
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
                parts.append(el.CurrentName or "")
            except Exception:
                continue
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
        # こちらの発言(user_marker)以降を捨てる。
        # 空判定を先に置くこと。空文字はどんな文字列にも「含まれる」ので、
        # マーカー未設定のプロファイル(M365 Copilot)では split("") が
        # ValueError: empty separator で落ちる。
        marker_user = self.profile.get("user_marker") or ""
        if marker_user and marker_user in new_part:
            new_part = new_part.split(marker_user)[0]
        # 先頭に assistant_marker があれば剥がす
        marker_ai = self.profile.get("assistant_marker") or ""
        if marker_ai and marker_ai in new_part:
            new_part = new_part.rsplit(marker_ai, 1)[1]
        elif not marker_ai and sent_prompt:
            # マーカーを持たないアプリ用の代替。送った本文は会話に echo される
            # ので、その終わりまでを捨てれば残りが応答になる。マーカーを
            # 突き止められなくても、こちらの道で応答を取り出せる。
            new_part = strip_echoed_prompt(new_part, sent_prompt)
        return new_part.strip()

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
# ID の直後に空白が入らない（#end 1Copilot… と続く）ので、ID は数字で取り、
# 末尾は (?!\d) で 1 と 12 を取り違えないようにする。
SNIPPET_RE = re.compile(r"#start\s*(\d+)(.*?)#end\s*\1(?!\d)", re.DOTALL)

# コードブロックの言語ラベルが1行目の頭に貼り付く（「Powershell# 集計…」）。
# 表示上のラベルであってコードではないので剥がす。
LANG_LABEL_RE = re.compile(r"^(?:powershell|pwsh|python|bash|cmd|batch|json|yaml)",
                           re.IGNORECASE)


def extract_snippets(text):
    """[(ID, コード), ...] を返す。"""
    result = []
    for hit in SNIPPET_RE.finditer(text or ""):
        code = LANG_LABEL_RE.sub("", hit.group(2).strip("\n"), count=1)
        result.append((hit.group(1), code.strip("\n")))
    return result


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
