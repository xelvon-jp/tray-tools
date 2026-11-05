# window_layout.py
# 窓の配置: 「この画面に集める」と「配置プリセット」。
#
# 背景: モニタの入力を別の PC へ切り替えても、PC 側にはモニタが接続された
# ままに見えるため Windows の自動再配置が働かず、窓だけが見えない画面に
# 残る。そこで、マウスカーソルのあるモニタへ他のモニタの窓を全部寄せる
# 「集める」を用意した。集約先に元からある窓の配置は触らない。
# 配置プリセットは、選んだ窓の位置と大きさ(と最大化)を名前付きで覚え、
# 手動で選んだときだけ一括でその配置へ戻す。モニタ構成の検知・自動適用は
# しない(意図しないタイミングで窓が動いて驚くため)。
# 仮想デスクトップは扱わない(そもそも使っていない前提)。
#
# 計画(どの窓をどこへ)を立てる部分は純関数で、OS とのやり取りは
# window_tools に寄せてある(このモジュール自体は Qt にも ctypes にも
# 触れないので、計画の部分はどこでも確かめられる)。
import json
import os

SECTION = "window_layout"
CASCADE_STEP = 30     # 集めた窓をずらす間隔(px)
CASCADE_WRAP = 10     # 何段ずらしたら左上へ戻るか

# 動かしてはいけないシェルの窓。2026-11-06 の実機で、2画面目のタスクバー
# (Shell_SecondaryTrayWnd)が「集める」の対象になって1画面目へ動いてしまった
# (ディスプレイの切り替えを何度かするまで戻らなかった)。
SHELL_CLASSES = frozenset({
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
    "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland",
    "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow",
})
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


def is_layout_target(class_name: str, ex_style: int, title: str) -> bool:
    """配置(集める・プリセット)で動かしてよいアプリの窓か(純関数)。

    タスクバーなどのシェルの窓、ツール窓(タスクバーに出ない浮き窓)、
    前面にならない窓、タイトルの無い窓は除く。タスクバーに出る普通の
    アプリの窓だけを相手にする。"""
    if class_name in SHELL_CLASSES:
        return False
    if ex_style & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
        return False
    return bool((title or "").strip())


# ---------------------------------------------------------------
# 矩形の計算(純関数)。矩形は (left, top, right, bottom)、right/bottom は排他
# ---------------------------------------------------------------
def _area(rect) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _intersection(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def window_monitor(rect, monitors):
    """矩形がいちばん大きく重なるモニタの番号。どこにも重ならなければ None
    (画面の外に取り残された窓)。"""
    best, best_area = None, 0
    for index, monitor in enumerate(monitors):
        overlap = _intersection(rect, monitor)
        if overlap and _area(overlap) > best_area:
            best, best_area = index, _area(overlap)
    return best


def monitor_at(point, monitors):
    """点を含むモニタの番号。含むものが無ければ一番近いモニタ。"""
    if not monitors:
        return None
    x, y = point
    for index, (left, top, right, bottom) in enumerate(monitors):
        if left <= x < right and top <= y < bottom:
            return index

    def distance(monitor):
        left, top, right, bottom = monitor
        dx = max(left - x, 0, x - (right - 1))
        dy = max(top - y, 0, y - (bottom - 1))
        return dx * dx + dy * dy

    return min(range(len(monitors)), key=lambda i: distance(monitors[i]))


def plan_gather(windows, monitors, cursor):
    """「この画面に集める」の計画。(移動の並び, 集約先のモニタ番号) を返す。

    windows は {hwnd, title, exe, rect(通常時の矩形), bounds(見た目の矩形),
    zoomed} の並び。rect/bounds は list_layout_windows が作る。
    - 集約先はカーソルのあるモニタ。元からそこにある窓は触らない
    - 最大化の窓は集約先でも最大化で移す(矩形は集約先の作業領域全体)
    - それ以外は大きさを保ち、作業領域を超えるものだけ収まるよう縮める。
      位置は左上から少しずつずらす(重なって互いを隠さないため)
    - どこにも重ならない窓(取り残された窓)も集める対象に含める
    """
    if not monitors:
        return [], None
    target = monitor_at(cursor, monitors) if cursor else 0
    if target is None:
        target = 0
    dest = monitors[target]
    dest_w, dest_h = dest[2] - dest[0], dest[3] - dest[1]
    moves = []
    slot = 0
    for win in windows:
        if window_monitor(win["bounds"], monitors) == target:
            continue
        if win.get("zoomed"):
            moves.append({"hwnd": win["hwnd"], "title": win.get("title", ""),
                          "rect": tuple(dest), "zoomed": True})
            continue
        left, top, right, bottom = win["bounds"]
        width, height = min(right - left, dest_w), min(bottom - top, dest_h)
        offset = CASCADE_STEP * (slot % CASCADE_WRAP)
        slot += 1
        x = min(dest[0] + offset, dest[2] - width)
        y = min(dest[1] + offset, dest[3] - height)
        moves.append({"hwnd": win["hwnd"], "title": win.get("title", ""),
                      "rect": (x, y, x + width, y + height), "zoomed": False})
    return moves, target


# ---------------------------------------------------------------
# 配置プリセット(純関数)
# ---------------------------------------------------------------
def normalize_presets(value) -> list:
    """settings の window_layout.presets を整える。形の違うものは捨てる。

    [{name, items: [{exe, title, rect: [l, t, r, b], zoomed}]}] の形。"""
    presets = []
    if not isinstance(value, list):
        return presets
    for preset in value:
        if not isinstance(preset, dict) or not isinstance(preset.get("name"), str):
            continue
        items = []
        for item in preset.get("items") or []:
            if not isinstance(item, dict):
                continue
            rect = item.get("rect")
            if not (isinstance(rect, list) and len(rect) == 4
                    and all(isinstance(v, (int, float)) for v in rect)):
                continue
            items.append({"exe": str(item.get("exe") or ""),
                          "title": str(item.get("title") or ""),
                          "rect": [int(v) for v in rect],
                          "zoomed": bool(item.get("zoomed"))})
        if preset["name"] and items:
            presets.append({"name": preset["name"], "items": items})
    return presets


def preset_item(window) -> dict:
    """列挙した窓1つから、プリセットに覚える項目を作る。"""
    return {"exe": window.get("exe", ""), "title": window.get("title", ""),
            "rect": [int(v) for v in window["rect"]],
            "zoomed": bool(window.get("zoomed"))}


def _same_app(window, item) -> bool:
    """窓とプリセットの項目が同じ窓か。exe 名(大小無視)を第一の手掛かりにし、
    タイトルは「保存時と今が前方一致する」までを同じとみる(タイトルの末尾は
    文書名や状態で揺れるため、完全一致には賭けない)。"""
    exe = os.path.basename(window.get("exe") or "").casefold()
    want = os.path.basename(item.get("exe") or "").casefold()
    if not want or exe != want:
        return False
    title = (window.get("title") or "").strip()
    saved = (item.get("title") or "").strip()
    if not saved:
        return True
    return title == saved or title.startswith(saved) or saved.startswith(title)


def match_preset(windows, items):
    """プリセットを今の窓へ当てはめる。(移動の並び, 見つからなかった件数)。

    項目の並び順に探し、1つの窓は1回だけ使う。"""
    used = set()
    moves = []
    missing = 0
    for item in items:
        found = None
        for window in windows:
            if window["hwnd"] in used:
                continue
            if _same_app(window, item):
                found = window
                break
        if found is None:
            missing += 1
            continue
        used.add(found["hwnd"])
        moves.append({"hwnd": found["hwnd"], "title": found.get("title", ""),
                      "rect": tuple(item["rect"]), "zoomed": bool(item["zoomed"])})
    return moves, missing


# ---------------------------------------------------------------
# OS とのやり取り(window_tools 経由)
# ---------------------------------------------------------------
def list_layout_windows() -> list:
    """配置の対象になる窓の並び。最小化・クローク中・自前の浮き窓は
    window_tools.list_windows の時点で除かれている。自分のプロセスの窓も
    除く(トレイの浮き窓を集めたり保存したりしても意味が無いため)。"""
    import window_tools

    windows = []
    for hwnd, title, bounds in window_tools.list_windows():
        if window_tools.is_own_window(hwnd):
            continue
        if not is_layout_target(window_tools.get_window_class(hwnd),
                                window_tools.get_ex_style(hwnd), title):
            continue
        placement = window_tools.get_placement(hwnd)
        normal, zoomed = placement if placement else (bounds, window_tools.is_zoomed(hwnd))
        windows.append({"hwnd": hwnd, "title": title,
                        "exe": window_tools.get_window_exe(hwnd),
                        "rect": normal, "bounds": bounds, "zoomed": zoomed})
    return windows


def apply_moves(moves):
    """計画を実行する。(成功件数, 失敗した移動の並び) を返す。

    管理者権限の窓など、権限の壁で動かせないものは失敗に数える(黙って
    成功扱いにしない)。"""
    import window_tools

    ok = 0
    failed = []
    for move in moves:
        try:
            done = window_tools.set_placement(
                move["hwnd"], tuple(move["rect"]), bool(move.get("zoomed")))
        except OSError:
            done = False
        if done:
            ok += 1
        else:
            failed.append(move)
    return ok, failed


def undo_moves(windows_before, moves) -> list:
    """集める前の状態から「元に戻す」移動の並びを作る(純関数)。

    windows_before は集める前に列挙した窓の並び。動かした窓について、
    元の通常時矩形と最大化状態へ戻す移動を作る。"""
    before = {win["hwnd"]: win for win in windows_before}
    undo = []
    for move in moves:
        win = before.get(move["hwnd"])
        if win is None:
            continue
        undo.append({"hwnd": win["hwnd"], "title": win.get("title", ""),
                     "rect": tuple(win["rect"]), "zoomed": bool(win.get("zoomed"))})
    return undo


# ---------------------------------------------------------------
# 設定(settings.json の window_layout.presets だけを差し替える)
# ---------------------------------------------------------------
def save_config(settings_path, updates: dict, app_settings=None) -> None:
    """window_layout の中の updates のキーだけを書く。ほかのキー・セクションには
    触れない(slideshow_core.save_config と同じ作法)。"""
    if isinstance(app_settings, dict):
        section = app_settings.get(SECTION)
        if not isinstance(section, dict):
            section = app_settings[SECTION] = {}
        section.update(json.loads(json.dumps(updates)))
    if not settings_path:
        return
    import settings as settings_module

    stored = {}
    if os.path.exists(settings_path):
        with open(settings_path, "r", encoding="utf-8") as f:
            stored = json.load(f)
    if not isinstance(stored, dict):
        stored = {}
    section = stored.get(SECTION)
    if not isinstance(section, dict):
        section = stored[SECTION] = {}
    section.update(json.loads(json.dumps(updates)))
    settings_module.save_settings(stored, settings_path)
