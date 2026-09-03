# tools/uia_probe.py
# Copilot 系アプリ(Chromium ベース)の UI Automation ツリーを覗いて、
# copilot_loop.SELECTORS の候補を提案する調査ツール。
#
# 【何のためのファイルか】
# 業務PCの M365 Copilot 版など、別のアプリで agent_loop を動かすときは、
# copilot_loop.py の SELECTORS を書き換える必要がある(窓のクラス名、入力欄の
# AutomationId、送信ボタンの日本語名、回答中に出るボタンの日本語名、など)。
# 目視で調べると時間がかかるし、UIA ツリーを起こす手順(WM_GETOBJECT を投げる)
# を知らないと何も見えない。これを対話的にやってくれる。
#
# 【使い方】
# 1. 対象アプリを開いておく(Copilot、M365 Copilot、Copilot Studio など)
# 2. python tools/uia_probe.py --title "Copilot"
#    タイトル完全一致で窓を探す。--class で ClassName でも絞れる。
# 3. 出力を見て copilot_loop.py の SELECTORS を書き換える
#
# 【読み方】
# 出力は3つのブロックに分かれる:
#   [窓]         見つけた窓の情報(class / title / process 名)
#   [下段のボタン] 入力欄まわり(下端から170px以内)のボタン名。ここに
#                「送信」「停止」「会話する」相当のボタンが並ぶ。
#                回答中に切り替わる名前を突き止めるため、--watch を付けて
#                何秒か眺めるとよい。
#   [入力欄候補]  ValuePattern を持ち、AutomationId が付いている要素。
#                Chromium アプリは Edit ではなく ComboBox で作られていることも
#                多いので、type にはこだわらない。
#
# 【なぜ WM_GETOBJECT を投げるのか】
# Chromium は支援技術を検出するまでアクセシビリティツリーを作らない。素で覗くと
# 子孫が10個ちょっと(窓枠だけ)しか見えない。レンダラの HWND へ WM_GETOBJECT を
# 投げて「読みに来る奴が居るぞ」と教えると起きて、数百個の子孫が見えるようになる。
# これは実測で確認済み(11個 → 371個)。プローブでも本体でも同じ処理をする。
#
# 【依存】
# - comtypes(既に venv に入っている)
# - PySide6 は不要。プローブは軽く保つ。
import argparse
import collections
import ctypes
import sys
import time

import comtypes
import comtypes.client

comtypes.CoInitialize()
comtypes.client.GetModule("UIAutomationCore.dll")
import comtypes.gen.UIAutomationClient as UIA  # noqa: E402

CONTROL_BUTTON, CONTROL_EDIT, CONTROL_COMBO = 50000, 50003, 50004
VALUE_PATTERN, TEXT_PATTERN = 10002, 10014
WM_GETOBJECT, OBJID_CLIENT, SMTO_ABORTIFHUNG = 0x003D, 0xFFFFFFFC, 0x0002

TYPE_NAMES = {
    50000: "Button", 50003: "Edit", 50004: "ComboBox", 50005: "Link",
    50006: "Image", 50007: "ListItem", 50008: "List", 50011: "MenuItem",
    50020: "Text", 50021: "ToolBar", 50026: "Group", 50030: "Document",
    50032: "Window", 50033: "Pane", 50025: "Custom",
}

user32 = ctypes.windll.user32
user32.SendMessageTimeoutW.argtypes = [
    ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
    ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
]


def _class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title_of(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _pid_of(hwnd):
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def find_windows(title=None, cls=None):
    """条件に合うトップレベル窓を返す。空条件だと Chromium 系すべて。"""
    hits = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def on(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        t = _title_of(hwnd)
        c = _class_of(hwnd)
        if title is not None and t != title:
            return True
        if cls is not None and c != cls:
            return True
        if not title and not cls and "Chrome_WidgetWin" not in c:
            return True
        hits.append((hwnd, c, t))
        return True

    user32.EnumWindows(EnumProc(on), None)
    return hits


def find_render_child(hwnd):
    """Chromium のレンダラ HWND を返す。ツリーを起こす対象になる。

    EnumChildWindows は環境によって直接の子だけを返す(孫まで辿らない)ことが
    実測で確認されている。見つからなかったら、コールバックの中で再帰的に呼び直す。
    さらに、RenderWidget が既に閉じている(タブが非表示など)場合も無いので、
    その場合は None を返す。"""
    found = {"h": None}
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def walk(root_hwnd):
        def on(child, _l):
            if found["h"] is not None:
                return False
            if _class_of(child) == "Chrome_RenderWidgetHostHWND":
                found["h"] = child
                return False
            walk(child)
            return found["h"] is None
        user32.EnumChildWindows(root_hwnd, EnumProc(on), None)

    walk(hwnd)
    return found["h"]


def wake_accessibility(hwnd):
    """WM_GETOBJECT を投げて Chromium にツリーを作らせる。"""
    out = ctypes.c_size_t()
    user32.SendMessageTimeoutW(
        ctypes.c_void_p(hwnd), WM_GETOBJECT, 0, ctypes.c_ssize_t(OBJID_CLIENT),
        SMTO_ABORTIFHUNG, 1000, ctypes.byref(out),
    )


def probe(hwnd_main, hwnd_render, top_px=170):
    """1回覗いて (下段ボタン, 入力欄候補, 全子孫数) を返す。"""
    wake_accessibility(hwnd_render or hwnd_main)
    uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    root = uia.ElementFromHandle(ctypes.c_void_p(hwnd_main))
    desc = root.FindAll(UIA.TreeScope_Descendants, uia.CreateTrueCondition())

    win_bottom = root.CurrentBoundingRectangle.bottom
    limit = win_bottom - top_px

    bottom_buttons = []
    input_candidates = []
    for i in range(desc.Length):
        el = desc.GetElement(i)
        try:
            t = el.CurrentControlType
            name = (el.CurrentName or "").strip()
            aid = (el.CurrentAutomationId or "").strip()
            rect = el.CurrentBoundingRectangle
            if t == CONTROL_BUTTON and rect.top >= limit and name:
                bottom_buttons.append(name)
            if t in (CONTROL_EDIT, CONTROL_COMBO):
                has_value = bool(el.GetCurrentPattern(VALUE_PATTERN))
                if has_value or aid:
                    input_candidates.append({
                        "type": TYPE_NAMES.get(t, t),
                        "automation_id": aid,
                        "name": name[:60],
                        "y": rect.top,
                    })
        except Exception:
            continue
    return bottom_buttons, input_candidates, desc.Length


def print_report(name, hwnd_main, hwnd_render, bottom, inputs, total):
    print(f"[窓] {name}")
    print(f"  HWND: {hwnd_main}  レンダラ: {hwnd_render}")
    print(f"  子孫: {total} 個")
    print()
    print("[下段のボタン] (窓の下から170px以内)")
    if not bottom:
        print("  (見つかりません — アプリを前面に出すとツリーが起きることがある)")
    for name_ in bottom:
        print(f"  - {name_}")
    print()
    print("[入力欄候補] Edit / ComboBox で AutomationId 付き、または ValuePattern を持つもの")
    if not inputs:
        print("  (見つかりません)")
    for entry in inputs:
        print(f"  - {entry['type']:8}  aid={entry['automation_id']!r:22}  "
              f"y={entry['y']:>6}  name={entry['name']!r}")


def watch(hwnd_main, hwnd_render, seconds):
    """指定秒数、下段のボタン集合を眺める。変化したときだけ表示。
    「入力待ち → 送信ボタン → 回答中 → 停止ボタン → 入力待ち」を捉えるため。"""
    print(f"[watch] {seconds}秒、下段のボタン集合を見ます。")
    print("  対象アプリで実際にメッセージを送信して、遷移を捉えてください。")
    last = None
    end = time.time() + seconds
    while time.time() < end:
        try:
            bottom, _inputs, _total = probe(hwnd_main, hwnd_render)
        except Exception as e:
            print(f"  読み取り失敗: {e}")
            time.sleep(1)
            continue
        key = tuple(sorted(set(bottom)))
        if key != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {list(key)}")
            last = key
        time.sleep(0.5)


def suggest_selectors(cls, title, inputs, bottom):
    """観察結果から SELECTORS の候補を組み立てて出す。"""
    print()
    print("[SELECTORS の候補] — copilot_loop.py にコピーする用")
    aid = ""
    for entry in inputs:
        if entry["automation_id"]:
            aid = entry["automation_id"]
            break
    print("SELECTORS = {")
    print(f"    \"window_class\":         {cls!r},")
    print(f"    \"window_title\":         {title!r},")
    print(f"    \"render_class\":         \"Chrome_RenderWidgetHostHWND\",")
    print(f"    \"input_automation_id\":  {aid!r},   # ← 入力欄候補から選ぶ")
    print(f"    \"send_button\":          \"\",   # ← 送信ボタンの日本語名 (watch で確認)")
    print(f"    \"busy_button\":          \"\",   # ← 回答中に出るボタン (停止/割り込み等)")
    print(f"    \"idle_marker\":          \"\",   # ← 入力待ちのときに常に見えるボタン")
    print(f"    \"assistant_marker\":     \"\",   # ← AI 側の発言の頭に出る文字列")
    print(f"    \"user_marker\":          \"\",   # ← 自分側の発言の頭に出る文字列")
    print("}")
    print()
    print("いま見えている下段のボタン(送信・停止の候補):")
    counts = collections.Counter(bottom)
    for n, c in counts.most_common():
        print(f"  x{c}  {n}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copilot 系アプリの UIA ツリーを覗いて SELECTORS 候補を出す。")
    parser.add_argument("--title", help="窓のタイトル完全一致(既定: Copilot)",
                        default="Copilot")
    parser.add_argument("--class", dest="cls",
                        help="窓の ClassName で絞る(例: Chrome_WidgetWin_1)")
    parser.add_argument("--any", action="store_true",
                        help="タイトル絞りを外して Chromium 系すべてを列挙する")
    parser.add_argument("--watch", type=int, default=0,
                        help="この秒数だけ下段のボタン変化を眺める")
    parser.add_argument("--top-px", type=int, default=170,
                        help="下端から何px以内を「下段」とみなすか")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    title = None if args.any else args.title
    windows = find_windows(title=title, cls=args.cls)
    if not windows:
        print(f"該当する窓が見つかりません (title={title!r} class={args.cls!r})")
        print("--any で Chromium 系すべてを列挙できます。")
        return 1

    for hwnd, cls, tt in windows:
        render = find_render_child(hwnd)
        try:
            bottom, inputs, total = probe(hwnd, render, top_px=args.top_px)
        except Exception as e:
            print(f"[{tt}] プローブ失敗: {e}")
            continue
        label = f"class={cls!r} title={tt!r} pid={_pid_of(hwnd)}"
        print_report(label, hwnd, render, bottom, inputs, total)
        suggest_selectors(cls, tt, inputs, bottom)
        if args.watch:
            print()
            watch(hwnd, render, args.watch)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
