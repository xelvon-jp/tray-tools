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

comtypes.CoInitialize()
comtypes.client.GetModule("UIAutomationCore.dll")
import comtypes.gen.UIAutomationClient as UIA  # noqa: E402

# --- 差し替え箇所（別アプリへ移すときはここだけ） ---------------------------
SELECTORS = {
    "window_class": "Chrome_WidgetWin_1",
    "window_title": "Copilot",
    "render_class": "Chrome_RenderWidgetHostHWND",
    "input_automation_id": "userInput",
    "send_button": "メッセージの送信",
    "busy_button": "メッセージの割り込み",   # 回答中だけ出る
    "idle_marker": "Copilot と会話する",     # 入力待ちのときに出ている
    "assistant_marker": "Copilot の発言",
    "user_marker": "あなたの発言",
}

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


class Copilot:
    """Copilot アプリの窓を1つ掴んで、読み書きする。

    COM オブジェクトは属性として持ち続ける。関数の外に出すと即座に解放されて
    0xC0000005 で落ちる(tray-tools で何度も踏んだ罠)。"""

    def __init__(self):
        self.hwnd_main, self.hwnd_render = self._find_window()
        if not self.hwnd_main:
            raise RuntimeError("Copilot の窓が見つかりません")
        self._wake()
        self.uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        self.true_cond = self.uia.CreateTrueCondition()

    # -- 窓を探す ----------------------------------------------------------
    def _find_window(self):
        found = {}
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def on_window(hwnd, _l):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            if cls.value == SELECTORS["window_class"] and title.value == SELECTORS["window_title"]:
                found["main"] = hwnd

                def on_child(child, _l2):
                    cbuf = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(child, cbuf, 256)
                    if cbuf.value == SELECTORS["render_class"]:
                        found["render"] = child
                    return True

                user32.EnumChildWindows(hwnd, EnumProc(on_child), None)
            return True

        user32.EnumWindows(EnumProc(on_window), None)
        return found.get("main"), found.get("render")

    def _wake(self):
        """Chromium のアクセシビリティツリーを起こす。既に起きていれば無害。"""
        out = ctypes.c_size_t()
        user32.SendMessageTimeoutW(
            ctypes.c_void_p(self.hwnd_render or self.hwnd_main), WM_GETOBJECT, 0,
            ctypes.c_ssize_t(OBJID_CLIENT), SMTO_ABORTIFHUNG, 1000, ctypes.byref(out),
        )

    # -- 木を歩く ----------------------------------------------------------
    def _descendants(self):
        """毎回取り直す。中身が変わると前に取った要素は無効になるため。"""
        root = self.uia.ElementFromHandle(ctypes.c_void_p(self.hwnd_main))
        return root, root.FindAll(UIA.TreeScope_Descendants, self.true_cond)

    def _input_box(self, desc):
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                if (el.CurrentAutomationId or "") == SELECTORS["input_automation_id"]:
                    return el
            except Exception:
                continue
        return None

    def _bottom_buttons(self, root, desc):
        """入力欄まわり(窓の下端から170px)のボタン名。状態はここに出る。"""
        limit = root.CurrentBoundingRectangle.bottom - 170
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
        if SELECTORS["busy_button"] in names:
            return "busy"
        if SELECTORS["send_button"] in names:
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
        if SELECTORS["busy_button"] in names:
            state = "busy"
        elif SELECTORS["send_button"] in names:
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
                if name != SELECTORS["send_button"]:
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
                     stable_seconds=1.5, timeout=15.0, poll=0.3):
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
        # こちらの発言(user_marker)以降を捨てる
        marker_user = SELECTORS["user_marker"]
        if marker_user in new_part:
            new_part = new_part.split(marker_user)[0]
        # 先頭に assistant_marker があれば剥がす
        marker_ai = SELECTORS["assistant_marker"]
        if marker_ai in new_part:
            new_part = new_part.rsplit(marker_ai, 1)[1]
        return new_part.strip()

    def last_response(self):
        """互換のために残す。previous_length を知らずに呼ぶと会話が長くなるほど
        誤爆しやすい(rsplit の最後のマーカーが応答の見出しにヒットする等)。
        新しい呼び出しからは snapshot_length + new_response を使うこと。
        """
        text = self.document_text()
        marker = SELECTORS["assistant_marker"]
        if marker not in text:
            return text
        tail = text.rsplit(marker, 1)[1]
        return tail.split(SELECTORS["user_marker"])[0].strip()


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
