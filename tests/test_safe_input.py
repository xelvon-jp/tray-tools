# tests/test_safe_input.py
# safe_input.py の純ロジック(キー指定・拒否キー・文字数上限・前面/所有者連鎖/座標の判定・
# 引数翻訳・INPUT 配列の組み立て)と、mcp_server の click/key/type の翻訳を確かめる。
# 実際の SendInput は呼ばない(env を偽物にしてモックする)。画面にも触れない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_safe_input.py
import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import mcp_server  # noqa: E402
import safe_input as si  # noqa: E402


def win(hwnd, exe="app.exe", title="t", rect=(100, 100, 400, 300), minimized=False, elevated=False):
    return {
        "hwnd": hwnd, "exe": exe, "pid": hwnd, "title": title, "cls": "C",
        "rect": None if minimized else rect, "monitor": 1, "minimized": minimized, "topmost": False,
        "toolwindow": False, "elevated": elevated, "foreground": False,
    }


def summ(i):
    if i.type == si.INPUT_MOUSE:
        return ("m", i.mi.dwFlags)
    return ("k", i.ki.wVk, i.ki.wScan, i.ki.dwFlags)


class FakeEnv:
    """SendInput をモックした環境。送られた INPUT 配列の要約を sent に溜める。"""

    def __init__(self, fg=1, owners=None, root=1, accept=None, scale=1.0, cursor=(5, 6)):
        self.fg, self.owners, self.root = fg, owners or {}, root
        self.accept, self.scale, self.cursor = accept, scale, cursor
        self.sent = []
        self.cursor_calls = []

    def foreground(self):
        return self.fg

    def owner(self, h):
        return self.owners.get(h, 0)

    def root_at(self, x, y):
        return self.root

    def to_physical(self, x, y, hwnd):
        return int(round(x * self.scale)), int(round(y * self.scale))

    def describe(self, h):
        return f"fake{h}"

    def get_cursor(self):
        return self.cursor

    def set_cursor(self, x, y):
        self.cursor_calls.append((x, y))
        return True

    def send_input(self, array, n):
        assert ctypes.sizeof(array) == n * ctypes.sizeof(si.INPUT)
        self.sent.append([summ(array[i]) for i in range(n)])
        return n if self.accept is None else min(self.accept, n)


def err_of(fn, *a):
    try:
        fn(*a)
    except ValueError as e:
        return str(e)
    raise AssertionError("例外にならなかった")


# --- 構造体 ---
def test_input_size():
    assert ctypes.sizeof(si.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


# --- キー指定 ---
def test_parse_keys_basic():
    assert si.parse_keys("enter") == ([], 0x0D)
    assert si.parse_keys("F5") == ([], 0x74)
    assert si.parse_keys("ctrl+r") == ([si.VK_CONTROL], ord("R"))
    assert si.parse_keys("Ctrl + Shift + T") == ([si.VK_CONTROL, si.VK_SHIFT], ord("T"))
    assert si.parse_keys("alt+left") == ([si.VK_MENU], 0x25)
    assert si.parse_keys("ctrl++") == ([si.VK_CONTROL], 0xBB)
    assert si.parse_keys("ctrl+ctrl+a") == ([si.VK_CONTROL], ord("A"))  # 重複は1つに


def test_parse_keys_rejected():
    for bad in ("win+r", "ctrl+win+d", "lwin", "rwin+e", "meta+a", "super", "cmd+c",
                "ctrl+alt+del", "alt+ctrl+delete", "", "  ", "ctrl+", "+a+", "ctrl", "ctrl+shift",
                "foo", "ctrl+foo", "x+a", "ctrl r"):
        err_of(si.parse_keys, bad)
    # ctrl+alt+del 以外の del は通る
    assert si.parse_keys("ctrl+delete") == ([si.VK_CONTROL], 0x2E)
    assert si.parse_keys("alt+f4") == ([si.VK_MENU], 0x73)


def test_key_events_order_and_extended():
    ev = si.key_events([si.VK_CONTROL, si.VK_SHIFT], ord("T"), scan_of=lambda vk: 0)
    assert [summ(e) for e in ev] == [
        ("k", 0x11, 0, 0), ("k", 0x10, 0, 0), ("k", ord("T"), 0, 0), ("k", ord("T"), 0, 2),
        ("k", 0x10, 0, 2), ("k", 0x11, 0, 2)]
    left = si.key_events([], 0x25, scan_of=lambda vk: 0)
    assert summ(left[0])[3] == si.KEYEVENTF_EXTENDEDKEY
    assert summ(left[1])[3] == si.KEYEVENTF_EXTENDEDKEY | si.KEYEVENTF_KEYUP


# --- type ---
def test_check_text_limits():
    assert si.check_text("a" * 1000)[1] is None
    assert "長すぎ" in si.check_text("a" * 1001)[1]
    assert si.check_text("")[1] and si.check_text(None)[1]
    assert si.check_text("a\x07b")[1]  # 制御文字
    assert si.check_text("a\r\nb\rc")[0] == "a\nb\nc"


def test_text_events():
    s = [summ(e) for e in si.text_events("aあ\n")]
    assert s[0] == ("k", 0, ord("a"), si.KEYEVENTF_UNICODE)
    assert s[1] == ("k", 0, ord("a"), si.KEYEVENTF_UNICODE | si.KEYEVENTF_KEYUP)
    assert s[2][2] == ord("あ")
    assert s[4] == ("k", si.VK_RETURN, 0, 0) and s[5] == ("k", si.VK_RETURN, 0, si.KEYEVENTF_KEYUP)
    pair = [summ(e) for e in si.text_events("\U0001F600")]  # サロゲートペアは2単位
    assert len(pair) == 4 and pair[0][2] == 0xD83D and pair[2][2] == 0xDE00


def test_click_events():
    assert [summ(e) for e in si.click_events()] == [("m", 2), ("m", 4)]
    assert [summ(e) for e in si.click_events("right")] == [("m", 8), ("m", 16)]
    assert len(si.click_events("left", True)) == 4


# --- 判定 ---
def test_owner_chain():
    owners = {30: 20, 20: 10}
    assert si.owner_chain_reaches(10, 10, owners.get)
    assert si.owner_chain_reaches(30, 10, owners.get)
    assert not si.owner_chain_reaches(40, 10, owners.get)
    assert not si.owner_chain_reaches(0, 10, owners.get)
    loop = {1: 2, 2: 1}
    assert not si.owner_chain_reaches(1, 9, loop.get)  # 循環でも止まる


def test_check_foreground():
    owners = {30: 10}
    assert si.check_foreground(10, 10, owners.get, str) is None
    assert si.check_foreground(30, 10, owners.get, str) is None  # 対象のダイアログ
    msg = si.check_foreground(99, 10, owners.get, lambda h: "other.exe")
    assert "前面に出して" in msg and "other.exe" in msg


def test_check_elevated():
    assert si.check_elevated(win(1, elevated=False)) is None
    assert "管理者" in si.check_elevated(win(1, elevated=True))
    assert "判定できません" in si.check_elevated(win(1, elevated=None))


def test_check_click_point_and_under():
    r = (100, 100, 400, 300)
    assert si.check_click_point(100, 100, r) is None
    assert si.check_click_point(499, 399, r) is None
    assert si.check_click_point(500, 200, r)
    assert si.check_click_point(99, 200, r)
    assert si.check_under_point(10, 10, {}.get, str, 1, 1) is None
    assert "overlay" in si.check_under_point(77, 10, {}.get, lambda h: "overlay", 1, 1)
    assert si.check_under_point(0, 10, {}.get, str, 1, 1)


# --- 引数翻訳 ---
def test_parse_input_args():
    p, e = si.parse_input_args("click", ["--window=chrome", "--button=right", "--double", "10", "-20", "--dry"])
    assert e is None and (p["x"], p["y"], p["button"], p["double"], p["dry"]) == (10, -20, "right", True, True)
    assert si.parse_input_args("click", ["10", "20"])[1]                      # window なし
    assert si.parse_input_args("click", ["--window=a", "10"])[1]              # 座標不足
    assert si.parse_input_args("click", ["--window=a", "x", "y"])[1]          # 非整数
    assert si.parse_input_args("click", ["--window=a", "--button=mid", "1", "2"])[1]
    p, e = si.parse_input_args("key", ["--window=a", "ctrl+r"])
    assert e is None and p["vk"] == ord("R") and p["keys"] == "ctrl+r"
    assert si.parse_input_args("key", ["--window=a", "win+r"])[1]
    p, e = si.parse_input_args("type", ["--window=a", "--text=あ\r\nい"])
    assert e is None and p["text"] == "あ\nい"
    assert si.parse_input_args("type", ["--window=a", "--text=" + "x" * 1001])[1]
    p, e = si.parse_input_args("type", ["--window=a", "hello", "world"])
    assert p["text"] == "hello world"
    assert si.parse_input_args("type", ["--window=0x10", "--text=a"])[0]["window"] == "0x10"


def test_mcp_translation():
    c = mcp_server._to_command
    assert c("click", {"window": "chrome", "x": 1, "y": 2}) == (
        "click", ["--window=chrome", "--button=left", "1", "2"])
    assert c("click", {"window": "w", "x": 1, "y": 2, "button": "right", "double": True, "dry": True}) == (
        "click", ["--window=w", "--button=right", "--double", "1", "2", "--dry"])
    assert c("key", {"window": "w", "keys": "ctrl+r"}) == ("key", ["--window=w", "--keys=ctrl+r"])
    assert c("type", {"window": "w", "text": " a\nb ", "dry": True}) == (
        "type", ["--window=w", "--text= a\nb ", "--dry"])
    for name, bad in (("click", {"x": 1, "y": 2}), ("click", {"window": "w", "x": "1", "y": 2}),
                      ("click", {"window": "w", "x": True, "y": 2}),
                      ("click", {"window": "w", "x": 1, "y": 2, "button": "middle"}),
                      ("key", {"window": "w"}), ("key", {"window": "w", "keys": " "}),
                      ("type", {"window": "w", "text": ""}), ("type", {"text": "a"}),
                      ("type", {"window": " ", "text": "a"})):
        err_of(c, name, bad)
    tools = {t["name"]: t for t in mcp_server.TOOLS}
    for n in ("click", "key", "type"):
        assert tools[n]["annotations"]["readOnlyHint"] is False
        assert "窓は動かさ" in tools[n]["description"] and "readOnly ではない" in tools[n]["description"]
    # 往復: 翻訳した引数が常駐側の解釈を通る
    cmd, args = c("click", {"window": "w", "x": -5, "y": 2, "dry": True})
    assert si.parse_input_args(cmd, args)[0]["x"] == -5
    cmd, args = c("type", {"window": "w", "text": "a\nb"})
    assert si.parse_input_args(cmd, args)[0]["text"] == "a\nb"


# --- execute(SendInput はモック) ---
def run(kind, argv, windows, env):
    p, e = si.parse_input_args(kind, argv)
    assert e is None, e
    return si.execute(kind, p, windows, env)


def test_execute_click_ok():
    env = FakeEnv(fg=1, scale=1.5)
    r = run("click", ["--window=app.exe", "150", "200", "--right"], [win(1)], env)
    assert r.ok and r.sent and r.hwnd == 1, r.text
    assert env.sent == [[("m", 8), ("m", 16)]]
    assert env.cursor_calls == [(225, 300), (5, 6)]  # 物理へ変換して移動 → 元へ戻す
    assert "click" in r.log


def test_execute_click_blocked():
    r = run("click", ["--window=1", "150", "200"], [win(1, minimized=True)], FakeEnv())
    assert not r.ok and "最小化" in r.text
    env = FakeEnv(fg=2)  # 前面が別の窓
    r = run("click", ["--window=1", "150", "200"], [win(1), win(2, "o.exe")], env)
    assert not r.ok and "前面に出して" in r.text and env.sent == []
    r = run("key", ["--window=1", "enter"], [win(1, elevated=True)], FakeEnv())
    assert not r.ok and "管理者" in r.text
    r = run("click", ["--window=1", "5", "5"], [win(1)], FakeEnv())  # 矩形の外
    assert not r.ok and "矩形の外" in r.text
    env = FakeEnv(root=9)  # 重なり
    r = run("click", ["--window=1", "150", "200"], [win(1)], env)
    assert not r.ok and "fake9" in r.text and env.sent == [] and env.cursor_calls == []
    r = run("key", ["--window=app.exe", "enter"], [win(1), win(2)], FakeEnv())  # 曖昧は一覧つき
    assert not r.ok and r.text.count("hwnd=") >= 2


def test_execute_dialog_foreground_ok():
    env = FakeEnv(fg=30, owners={30: 1}, root=30)
    r = run("click", ["--window=1", "150", "200"], [win(1)], env)
    assert r.ok and r.sent


def test_execute_dry_does_not_send():
    env = FakeEnv()
    for kind, argv in (("click", ["--window=1", "150", "200", "--dry"]),
                       ("key", ["--window=1", "ctrl+r", "--dry"]),
                       ("type", ["--window=1", "--text=hi", "--dry"])):
        r = run(kind, argv, [win(1)], env)
        assert r.ok and not r.sent and "dry" in r.text
    assert env.sent == [] and env.cursor_calls == []
    r = run("key", ["--window=1", "enter", "--dry"], [win(1)], FakeEnv(fg=2))  # dry でも確認は効く
    assert not r.ok


def test_execute_key_and_type_payload():
    env = FakeEnv()
    r = run("key", ["--window=1", "ctrl+shift+t"], [win(1)], env)
    assert r.ok and len(env.sent[0]) == 6
    assert [x[1] for x in env.sent[0]][:3] == [0x11, 0x10, ord("T")]
    env = FakeEnv()
    r = run("type", ["--window=1", "--text=ab\ncd"], [win(1)], env)
    assert r.ok and "ab" not in r.log and "5文字" in r.log  # 本文はログに出さず文字数だけ
    assert len(env.sent[0]) == 4 + 2 + 4  # a,b 各2 + Enter 2 + c,d 各2


def test_execute_short_sendinput_is_error_and_releases():
    env = FakeEnv(accept=0)
    r = run("key", ["--window=1", "ctrl+r"], [win(1)], env)
    assert not r.ok and "SendInput" in r.text
    assert len(env.sent) == 2  # 本送信 + 後始末の KEYUP
    cleanup = env.sent[1]
    assert all(x[3] & si.KEYEVENTF_KEYUP for x in cleanup) and len(cleanup) == 2
    env = FakeEnv(accept=1)  # click も後始末とカーソル復帰
    r = run("click", ["--window=1", "150", "200"], [win(1)], env)
    assert not r.ok and env.cursor_calls[-1] == (5, 6)
    assert env.sent[1] == [("m", 4)]
    r = run("type", ["--window=1", "--text=a"], [win(1)], FakeEnv(accept=0))
    assert not r.ok


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
