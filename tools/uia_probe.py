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
import json
import os
import sys
import time

import comtypes
import comtypes.client

comtypes.CoInitialize()
comtypes.client.GetModule("UIAutomationCore.dll")
import comtypes.gen.UIAutomationClient as UIA  # noqa: E402

CONTROL_BUTTON, CONTROL_EDIT, CONTROL_COMBO, CONTROL_TEXT = 50000, 50003, 50004, 50020
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


def _exe_of(hwnd):
    """窓を持っているプロセスの実行ファイル名(例 'Copilot.exe')。取れなければ ''。

    【なぜ exe 名を見るのか】
    窓のタイトルは表示言語・開いている会話・アプリの更新で変わるが、実行ファイル名は
    まず変わらない。別PC・別バージョンの Copilot を狙うときの手掛かりとして、
    タイトルより桁違いに当てになる。"""
    try:
        import psutil
        return psutil.Process(_pid_of(hwnd)).name()
    except Exception:  # noqa: BLE001  psutil が無い・アクセス拒否・既に終了
        return ""


def find_windows(title=None, cls=None, title_contains=None, exe=None):
    """条件に合う可視のトップレベル窓を [(hwnd, class, title, exe), ...] で返す。

    条件を1つも渡さないと Chromium 系(Chrome_WidgetWin*)だけに絞る。M365 Copilot の
    ように正式なタイトルが分からないときは title_contains か exe で当たりを付ける
    (完全一致だけだと、タイトルに会話名が付くアプリで永久に見つからない)。"""
    hits = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    filtered = any(v is not None for v in (title, cls, title_contains, exe))

    def on(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        t = _title_of(hwnd)
        c = _class_of(hwnd)
        if title is not None and t != title:
            return True
        if title_contains is not None and title_contains.lower() not in t.lower():
            return True
        if cls is not None and c != cls:
            return True
        e = _exe_of(hwnd) if (exe is not None or filtered) else ""
        if exe is not None and e.lower() != exe.lower():
            return True
        if not filtered and "Chrome_WidgetWin" not in c:
            return True
        hits.append((hwnd, c, t, e or _exe_of(hwnd)))
        return True

    user32.EnumWindows(EnumProc(on), None)
    return hits


def survey():
    """可視のトップレベル窓を、タイトルのあるものだけ全部並べる。

    業務PCで最初に走らせる用。M365 Copilot がどのクラス・どの exe なのかを、
    当てずっぽうのタイトル一致に頼らず目で確かめるための一覧。"""
    rows = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def on(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        t = _title_of(hwnd)
        if not t.strip():
            return True
        rows.append((hwnd, _class_of(hwnd), t, _exe_of(hwnd)))
        return True

    user32.EnumWindows(EnumProc(on), None)
    return rows


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
    """WM_GETOBJECT を投げて Chromium にツリーを作らせる。

    レンダラ HWND に投げるのが本筋だが、実測ではメイン窓に投げても起きることが
    ある(Chromium のフレーム側が支援技術の要求として受け取り、レンダラへ回す)。
    レンダラが取れなくても諦めずにメイン窓に投げるとよい。"""
    out = ctypes.c_size_t()
    user32.SendMessageTimeoutW(
        ctypes.c_void_p(hwnd), WM_GETOBJECT, 0, ctypes.c_ssize_t(OBJID_CLIENT),
        SMTO_ABORTIFHUNG, 1000, ctypes.byref(out),
    )


def probe(hwnd_main, hwnd_render, top_px=170, wake_wait=6.0):
    """1回覗いて (下段ボタン, 入力欄候補, 全子孫数, 発言マーカー候補) を返す。

    Chromium は WM_GETOBJECT を受け取ってからツリーを作るまでに数百ミリ秒〜数秒
    かかることがある(バックグラウンドだったタブなら特に)。子孫が 30 個未満なら
    まだ立ち上がりきっていない可能性が高いので、wake_wait 秒までリトライする。
    ツリーが起きていないままだと下段ボタンも入力欄候補も空で返り、原因不明の
    「見つかりません」に見えてしまう。"""
    uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    true_cond = uia.CreateTrueCondition()
    root = uia.ElementFromHandle(ctypes.c_void_p(hwnd_main))

    wake_accessibility(hwnd_render or hwnd_main)
    deadline = time.time() + wake_wait
    desc = root.FindAll(UIA.TreeScope_Descendants, true_cond)
    while desc.Length < 30 and time.time() < deadline:
        time.sleep(0.4)
        # 2回目以降も投げる(1回目を落とすアプリがある。害はない)
        wake_accessibility(hwnd_render or hwnd_main)
        desc = root.FindAll(UIA.TreeScope_Descendants, true_cond)

    win_bottom = root.CurrentBoundingRectangle.bottom
    limit = win_bottom - top_px

    bottom_buttons = []
    input_candidates = []
    text_names = collections.Counter()
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
            # 発言マーカーの候補集め。会話の1往復ごとに同じ短い文字列が現れるので、
            # 「2回以上出てくる短い Text」に絞ると、本文に埋もれず浮かび上がる。
            if t == CONTROL_TEXT and 2 <= len(name) <= 30:
                text_names[name] += 1
        except Exception:
            continue
    markers = [(n, c) for n, c in text_names.most_common(25) if c >= 2]
    return bottom_buttons, input_candidates, desc.Length, markers


def print_report(name, hwnd_main, hwnd_render, bottom, inputs, total, markers):
    print(f"[窓] {name}")
    print(f"  HWND: {hwnd_main}  レンダラ: {hwnd_render}")
    print(f"  子孫: {total} 個")
    if total < 30:
        print("  ⚠ 子孫が少なすぎます。アクセシビリティツリーが起きていません。")
        print("     アプリを一度前面に出してから、もう一度実行してください。")
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
    print()
    print("[発言マーカー候補] 2回以上出てくる短い Text（会話の往復ごとに現れるもの）")
    if not markers:
        print("  (見つかりません — 会話を1往復してから実行すると出ます)")
    for text, count in markers:
        print(f"  x{count:<3} {text!r}")


def watch(hwnd_main, hwnd_render, seconds):
    """指定秒数、下段のボタン集合を眺める。変化したときだけ表示。
    「入力待ち → 送信ボタン → 回答中 → 停止ボタン → 入力待ち」を捉えるため。"""
    print(f"[watch] {seconds}秒、下段のボタン集合を見ます。")
    print("  対象アプリで実際にメッセージを送信して、遷移を捉えてください。")
    last = None
    end = time.time() + seconds
    while time.time() < end:
        try:
            bottom, _inputs, _total, _markers = probe(hwnd_main, hwnd_render)
        except Exception as e:
            print(f"  読み取り失敗: {e}")
            time.sleep(1)
            continue
        key = tuple(sorted(set(bottom)))
        if key != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {list(key)}")
            last = key
        time.sleep(0.5)


def build_profile(cls, title, exe, inputs, bottom, markers):
    """観察結果から、settings.json に貼れるプロファイルの下書きを組み立てる。

    埋まらない項目は空のまま残す。ここを機械が勝手に埋めると、間違った値のまま
    「設定はできている」ように見えてしまい、原因の切り分けが遅くなる。
    人が見て決めるべきところは、空にして候補を隣に並べるだけにしてある。"""
    aid = next((e["automation_id"] for e in inputs if e["automation_id"]), "")
    return {
        "name": (exe or title or "copilot").replace(".exe", "").lower(),
        # 窓の探し方。exe 名がいちばん当てになるので先に置く。
        # title は完全一致ではなく「含む」で見る(会話名が付くアプリがあるため)。
        "process_name": exe,
        "window_class": cls,
        "window_title_contains": title,
        "render_class": "Chrome_RenderWidgetHostHWND",
        "input_automation_id": aid,
        # 状態を読むボタン名。候補を並べておいて「どれかに当てはまれば」で判定する。
        # 1つの文字列の完全一致だと、文言が少し違うだけで全部 idle に見えてしまう。
        "send_button": [],
        "busy_button": [],
        "assistant_marker": "",
        "user_marker": "",
        "input_band_px": 170,
    }


def print_profile(profile, bottom, markers):
    print()
    print("=" * 70)
    print("[プロファイルの下書き] settings.json の \"copilot_profiles\" に貼る用")
    print("=" * 70)
    print(json.dumps({"copilot_profiles": [profile]}, ensure_ascii=False, indent=2))
    print()
    print("--- 空欄を埋めるための手掛かり ---")
    print()
    print("send_button / busy_button に入れる候補（下段のボタン）:")
    if not bottom:
        print("  (いま見えているボタンはありません)")
    for n, c in collections.Counter(bottom).most_common():
        print(f"  x{c}  {n!r}")
    print("  ※ --watch 30 を付けて実際に1往復送信すると、")
    print("     「入力待ち → 送信 → 回答中(停止/割り込み) → 入力待ち」の変化が見えます。")
    print("     送信できるときだけ出る名前 → send_button")
    print("     回答中だけ出る名前         → busy_button")
    print()
    print("assistant_marker / user_marker に入れる候補（発言マーカー）:")
    if not markers:
        print("  (会話を1往復してから実行すると出ます)")
    for text, count in markers[:12]:
        print(f"  x{count:<3} {text!r}")
    print("  ※ AI 側の発言の頭に出るもの → assistant_marker")
    print("     自分の発言の頭に出るもの → user_marker")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copilot 系アプリの UIA ツリーを覗いて SELECTORS 候補を出す。")
    parser.add_argument("--title", help="窓のタイトル完全一致(既定: Copilot)",
                        default="Copilot")
    parser.add_argument("--title-contains", dest="title_contains",
                        help="窓のタイトル部分一致。M365 のように正式名が不明なとき用")
    parser.add_argument("--exe", help="実行ファイル名で絞る(例: Copilot.exe)")
    parser.add_argument("--class", dest="cls",
                        help="窓の ClassName で絞る(例: Chrome_WidgetWin_1)")
    parser.add_argument("--any", action="store_true",
                        help="タイトル絞りを外して Chromium 系すべてを列挙する")
    parser.add_argument("--survey", action="store_true",
                        help="可視の窓を全部並べるだけ。まずこれで対象を見つける")
    parser.add_argument("--watch", type=int, default=0,
                        help="この秒数だけ下段のボタン変化を眺める")
    parser.add_argument("--top-px", type=int, default=170,
                        help="下端から何px以内を「下段」とみなすか")
    parser.add_argument("--out", help="表示内容をこのファイルにも書き出す")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    # --out が指定されたら、画面と同じものをファイルへも流す。業務PCで採った結果を
    # そのまま持ち帰れるようにするため(コンソールからの手選択コピーは取りこぼす)。
    sink = None
    if args.out:
        sink = open(args.out, "w", encoding="utf-8")
        real_stdout = sys.stdout

        class _Tee:
            def write(self, s):
                real_stdout.write(s)
                sink.write(s)

            def flush(self):
                real_stdout.flush()
                sink.flush()

        sys.stdout = _Tee()

    try:
        return _run(args)
    finally:
        if sink is not None:
            sys.stdout = sys.__stdout__
            sink.close()
            print(f"\n書き出しました: {os.path.abspath(args.out)}")


def _run(args) -> int:
    if args.survey:
        print("[可視の窓 一覧] 対象アプリを前面に出した状態で見てください")
        print(f"{'class':<28} {'exe':<24} title")
        print("-" * 100)
        for _hwnd, cls, tt, exe in survey():
            print(f"{cls:<28} {exe:<24} {tt}")
        print()
        print("Copilot らしい行を見つけたら、その exe で絞って調べます:")
        print('  python tools\\uia_probe.py --exe "Copilot.exe" --watch 30')
        return 0

    # 明示的な絞り込みが1つでもあれば、既定のタイトル完全一致は外す
    # (--exe だけ渡したのに title="Copilot" が効いて0件、という事故を防ぐ)。
    explicit = args.title_contains or args.exe or args.cls or args.any
    title = None if explicit else args.title
    windows = find_windows(title=title, cls=args.cls,
                           title_contains=args.title_contains, exe=args.exe)
    if not windows:
        print("該当する窓が見つかりません "
              f"(title={title!r} contains={args.title_contains!r} "
              f"exe={args.exe!r} class={args.cls!r})")
        print()
        print("まず --survey で、可視の窓を全部並べて対象を探してください:")
        print("  python tools\\uia_probe.py --survey")
        return 1

    for hwnd, cls, tt, exe in windows:
        render = find_render_child(hwnd)
        try:
            bottom, inputs, total, markers = probe(hwnd, render, top_px=args.top_px)
        except Exception as e:
            print(f"[{tt}] プローブ失敗: {e}")
            continue
        label = f"class={cls!r} exe={exe!r} title={tt!r} pid={_pid_of(hwnd)}"
        print_report(label, hwnd, render, bottom, inputs, total, markers)
        if args.watch:
            print()
            watch(hwnd, render, args.watch)
            # watch のあとにもう一度見る。1往復してもらった後なら、
            # 発言マーカーが出そろっている。
            try:
                bottom2, inputs2, _t2, markers2 = probe(hwnd, render, top_px=args.top_px)
                bottom, inputs, markers = bottom or bottom2, inputs or inputs2, markers2 or markers
            except Exception:  # noqa: BLE001
                pass
        print_profile(build_profile(cls, tt, exe, inputs, bottom, markers),
                      bottom, markers)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
