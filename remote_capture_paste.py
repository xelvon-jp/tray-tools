# remote_capture_paste.py
# リモート撮影(remote_capture.py)の貼り付け係。常駐から別プロセスで起動され、
# 撮れた画像を Muse アプリの表示中チャットへ貼って送信する。
#
# 【なぜ別プロセスか】
# UIA / COM を常駐プロセスの中に持ち込むと、GC の解放タイミングでプロセスごと
# 落ちる事故の前歴がある(copilot_loop._ensure_uia の冒頭を参照)。muse_post は
# 元々こういう短命プロセスから使う作りなので、その流儀に合わせる。このプロセス
# が落ちても常駐と保存済みの画像(OneDrive 側)は無事。
#
# 【安全側の決めごと】(muse_post.cmd_post の作法に倣う)
# - 窓のタイトルに "Microsoft Edge" を含むものはブラウザ窓とみなして中止する。
#   Muse アプリは Edge PWA で、ダッシュボードを開いた Edge 窓と exe もクラスも
#   同じため、タイトルでしか区別できない(ゼロ幅文字を除いてから判定する。
#   見張り v16 の実測)。
# - 投稿欄が空でないときは中止する(人の下書きを上書きしない)。
# - 説明文を書いた後、貼り付けの着弾を確認できなければ送信せず、説明文も
#   元に戻してから終わる(下書きを残さない)。
# - 成否は %LOCALAPPDATA%\traytools\remote_capture_last.json と action_log に残す。
import ctypes
import json
import sys
import time
from pathlib import Path

import action_log
from remote_capture import app_data_dir

CAPTION = "リモート撮影（ARiA実験）"
RESULT_NAME = "remote_capture_last.json"

VK_CONTROL = 0x11
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
CF_DIB = 8
GMEM_MOVEABLE = 0x0002
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

PASTE_CONFIRM_TIMEOUT = 8.0
PASTE_RETRY_TIMEOUT = 6.0
POSTED_TIMEOUT = 10.0

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
except AttributeError:  # Windows 以外では import だけ通す
    _user32 = _kernel32 = None

if _user32 is not None:
    # argtypes/restype は必ず指定する(64bit でハンドルが切り詰められるため)。
    _user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    _user32.OpenClipboard.restype = ctypes.c_bool
    _user32.EmptyClipboard.argtypes = []
    _user32.EmptyClipboard.restype = ctypes.c_bool
    _user32.CloseClipboard.argtypes = []
    _user32.CloseClipboard.restype = ctypes.c_bool
    _user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
    _user32.RegisterClipboardFormatW.restype = ctypes.c_uint
    _user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    _user32.SetClipboardData.restype = ctypes.c_void_p
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int
    _kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalUnlock.restype = ctypes.c_bool
    _kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalFree.restype = ctypes.c_void_p

    import mouse_jiggler

    _SendInput = _user32.SendInput
    _SendInput.argtypes = [
        ctypes.c_uint, ctypes.POINTER(mouse_jiggler.INPUT), ctypes.c_int]
    _SendInput.restype = ctypes.c_uint
else:
    mouse_jiggler = None
    _SendInput = None


def _set_clipboard_data(fmt, data) -> bool:
    """data をクリップボード形式 fmt で置く。成功したら所有権は OS へ移る。"""
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    ptr = _kernel32.GlobalLock(handle)
    if not ptr:
        _kernel32.GlobalFree(handle)
        return False
    buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
    ctypes.memmove(ptr, buf, len(data))
    _kernel32.GlobalUnlock(handle)
    if not _user32.SetClipboardData(fmt, handle):
        _kernel32.GlobalFree(handle)
        return False
    return True


def put_image_on_clipboard(png_bytes) -> bool:
    """PNG をクリップボードへ。"PNG" 形式(Chromium が読む)に加え、Pillow が
    あれば CF_DIB も置く(読む側の対応が広い方に合わせる)。"""
    if _user32 is None:
        return False
    if not _user32.OpenClipboard(None):
        return False
    try:
        _user32.EmptyClipboard()
        fmt_png = _user32.RegisterClipboardFormatW("PNG")
        ok = bool(fmt_png) and _set_clipboard_data(fmt_png, png_bytes)
        try:
            import io

            from PIL import Image

            dib = io.BytesIO()
            Image.open(io.BytesIO(png_bytes)).convert("RGB").save(dib, "BMP")
            # BMP の先頭14バイト(BITMAPFILEHEADER)を落としたものが DIB。
            _set_clipboard_data(CF_DIB, dib.getvalue()[14:])
        except Exception:  # noqa: BLE001  DIB は予備。失敗しても PNG があれば続ける
            pass
        return ok
    finally:
        _user32.CloseClipboard()


def _send_inputs(events) -> bool:
    arr = (mouse_jiggler.INPUT * len(events))()
    for i, ev in enumerate(events):
        arr[i] = ev
    sent = _SendInput(len(events), arr, ctypes.sizeof(mouse_jiggler.INPUT))
    return sent == len(events)


def _key_event(vk, up):
    ev = mouse_jiggler.INPUT()
    ev.type = INPUT_KEYBOARD
    ev.ki.wVk = vk
    ev.ki.wScan = 0
    ev.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    ev.ki.time = 0
    ev.ki.dwExtraInfo = 0
    return ev


def press_ctrl_v() -> bool:
    if _SendInput is None:
        return False
    return _send_inputs([
        _key_event(VK_CONTROL, False),
        _key_event(VK_V, False),
        _key_event(VK_V, True),
        _key_event(VK_CONTROL, True),
    ])


def click_point(x, y) -> bool:
    """仮想画面全体を 0..65535 に写した絶対座標でクリックする。"""
    if _SendInput is None:
        return False
    vx = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = max(_user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) - 1, 1)
    vh = max(_user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) - 1, 1)
    nx = (x - vx) * 65535 // vw
    ny = (y - vy) * 65535 // vh
    base = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    def mouse_ev(dw_flags):
        ev = mouse_jiggler.INPUT()
        ev.type = INPUT_MOUSE
        ev.mi.dx = nx
        ev.mi.dy = ny
        ev.mi.mouseData = 0
        ev.mi.dwFlags = dw_flags
        ev.mi.time = 0
        ev.mi.dwExtraInfo = 0
        return ev

    return _send_inputs([
        mouse_ev(MOUSEEVENTF_MOVE | base),
        mouse_ev(MOUSEEVENTF_LEFTDOWN | base),
        mouse_ev(MOUSEEVENTF_LEFTUP | base),
    ])


def _clean_title(title):
    """Edge の窓タイトルに混ざるゼロ幅文字を除く(見張り v16 の実測)。"""
    if not title:
        return ""
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        title = title.replace(ch, "")
    return title


def _same_text(a, b) -> bool:
    return (a or "").strip() == (b or "").strip()


def _attachment_seen(refs, baseline_count, stem) -> bool:
    items = refs.get("items") or []
    if len(items) > baseline_count:
        return True
    for _ctype, name in items:
        if name and (stem in name or name in ("画像", "Image")):
            return True
    return False


def _finish(result, code) -> int:
    result["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        path = app_data_dir() / RESULT_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    except OSError:
        pass
    detail = "成功" if result["ok"] else (result.get("reason") or "失敗")
    action_log.record("リモート撮影 投稿", detail[:80], "remote")
    print(json.dumps(result, ensure_ascii=False))
    return code


def main(argv) -> int:
    result = {"ok": False, "reason": None, "chat_title": None, "steps": []}
    steps = result["steps"]
    if len(argv) < 2:
        result["reason"] = "画像パスが渡されていない"
        return _finish(result, 1)
    image_path = Path(argv[1])
    caption = argv[2] if len(argv) > 2 else CAPTION
    try:
        png_bytes = image_path.read_bytes()
    except OSError as e:
        result["reason"] = f"画像を読めない: {e}"
        return _finish(result, 1)
    if not png_bytes:
        result["reason"] = "画像が空"
        return _finish(result, 1)

    try:
        import muse_post
        from muse_post_logic import is_blank
    except Exception as e:  # noqa: BLE001
        result["reason"] = f"muse_post を読み込めない: {type(e).__name__}: {e}"
        return _finish(result, 1)

    muse = None
    wrote = False
    try:
        try:
            muse = muse_post.Muse()
        except muse_post.MuseNotFound as e:
            result["reason"] = str(e)
            return _finish(result, 1)

        title = _clean_title(muse.window_title())
        if "Microsoft Edge" in title:
            result["reason"] = "ブラウザ窓(ダッシュボード)を掴んだため中止"
            return _finish(result, 1)
        steps.append(f"窓: {title or '(無題)'}")

        muse.wait_awake()
        s, refs = muse.scan()
        result["chat_title"] = s["chat_title"]
        if len(refs["input"]) != 1:
            result["reason"] = f"投稿欄が {len(refs['input'])} 個ある"
            return _finish(result, 1)
        input_el = refs["input"][0]
        if not is_blank(muse.read_value(input_el)):
            result["reason"] = "投稿欄が空でないため中止(下書き保護)"
            return _finish(result, 1)

        muse.set_value(input_el, caption)
        wrote = True
        steps.append("説明文を書いた")
        if not _same_text(muse.read_value(input_el), caption):
            result["reason"] = "説明文を書いた直後に中身が一致しない"
            return _finish(result, 1)

        if not put_image_on_clipboard(png_bytes):
            result["reason"] = "クリップボードへ画像を置けなかった"
            return _finish(result, 1)
        steps.append("クリップボードへ画像を置いた")

        s, refs = muse.scan()
        baseline = len(refs.get("items") or [])
        if len(refs["input"]) == 1:
            input_el = refs["input"][0]

        pasted = False
        for attempt in range(2):
            try:
                input_el.SetFocus()
            except Exception:  # noqa: BLE001  フォーカス失敗時はクリックに任せる
                pass
            time.sleep(0.3)
            if not press_ctrl_v():
                result["reason"] = "Ctrl+V を送れなかった"
                return _finish(result, 1)
            steps.append(f"Ctrl+V を送った({attempt + 1}回目)")
            deadline = time.monotonic() + (
                PASTE_CONFIRM_TIMEOUT if attempt == 0 else PASTE_RETRY_TIMEOUT)
            while time.monotonic() < deadline:
                time.sleep(0.7)
                s, refs = muse.scan()
                if _attachment_seen(refs, baseline, image_path.stem):
                    pasted = True
                    break
            if pasted or attempt == 1:
                break
            # 1回目で着弾が見えない場合だけ、投稿欄をクリックしてから貼り直す。
            try:
                rect = input_el.CurrentBoundingRectangle
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
            except Exception:  # noqa: BLE001
                break
            if not click_point(cx, cy):
                break
            steps.append("投稿欄をクリックして貼り直す")
            time.sleep(0.3)

        if not pasted:
            result["reason"] = "貼り付けの着弾を確認できなかった(送信しない)"
            return _finish(result, 1)
        steps.append("貼り付けを確認した")

        s, refs = muse.scan()
        if len(refs["send"]) != 1:
            result["reason"] = f"送信ボタンが {len(refs['send'])} 個ある"
            return _finish(result, 1)
        muse.invoke(refs["send"][0])
        steps.append("送信を押した")

        s, refs, ok = muse.poll(
            lambda x: x["inputs"] == 1 and x["input_empty"] is True,
            POSTED_TIMEOUT)
        result["chat_title"] = s["chat_title"]
        if not ok:
            result["reason"] = "送信を押したが投稿欄が空に戻らない(送れたか不明)"
            return _finish(result, 1)
        result["ok"] = True
        return _finish(result, 0)
    except Exception as e:  # noqa: BLE001  何があっても結果ファイルに残して終わる
        result["reason"] = f"例外: {type(e).__name__}: {e}"
        return _finish(result, 1)
    finally:
        if muse is not None:
            if wrote and not result["ok"]:
                # 自分が書いた説明文だけを消して戻す(人の文面は消さない)。
                try:
                    s, refs = muse.scan()
                    if len(refs["input"]) == 1 and _same_text(
                            muse.read_value(refs["input"][0]), caption):
                        muse.set_value(refs["input"][0], "")
                        steps.append("送らずに説明文を消した")
                except Exception:  # noqa: BLE001
                    steps.append("説明文を消そうとして失敗した")
            muse.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
