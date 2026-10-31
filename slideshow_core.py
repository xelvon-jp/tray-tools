# slideshow_core.py
# 写真のスライドショー(slideshow.py)の、Qt に依存しない部分。
#
#   - 設定(settings.json の slideshow セクション)の読み書き。書くときはファイルを読み直して
#     slideshow の中の該当キーだけ差し替える(CLAUDE.md「設定ファイル」)
#   - 写真の一覧作り(iter_images / scan_into)。裏のスレッドで回し、見つかった分から
#     PhotoPool に足していく。ジャンクション・シンボリックリンクには潜らない。隠し・システムの
#     ファイルとフォルダは除く。読めないフォルダは飛ばす
#   - 選び方(PhotoPool.pick)。まずフォルダを等確率で選び、その中から袋方式(一巡するまで
#     同じ写真を出さない)で1枚。大きいフォルダに小さいフォルダが埋もれないように
#   - 戻る用の履歴(History。件数に上限)
#   - Ken Burns(ゆっくりズーム)の動きと、各時刻の描く矩形(frame_rect)
#   - 情報表示に出す撮影日時(EXIF の DateTimeOriginal。読むのは filer_core)
#
# テストは tests/test_slideshow.py。乱数(random.Random)と時刻は差し替えられるようにしてある。
import json
import os
import random
import threading
import time
from dataclasses import dataclass
from itertools import islice

import filer_core as core

SECTION = "slideshow"

# 対象の拡張子。HEIC / HEIF は PySide6 の Qt に読み込みプラグインが無いので入れない
# (README「スライドショー」。あっふのプレビューと同じ理由)。GIF は1枚目だけ出る。
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"})

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4

ZOOM_MAX = 1.08          # Ken Burns の拡大率の上限(1.00 ↔ 1.08)
HISTORY_LIMIT = 200      # ← で戻れる枚数(パスだけを持つ)
SCAN_CHUNK = 200         # 複数フォルダを交互に走査するときの、1回に読み進める枚数
START_MIN = 8            # これだけ見つかるか、走査が終わるか、START_WAIT 秒経ったら出し始める
START_WAIT = 1.0

INTERVAL_RANGE = (2.0, 86400.0)
FADE_MAX = 10.0
FPS_RANGE = (10, 120)


def defaults() -> dict:
    """settings.py の既定値(DEFAULT_SETTINGS["slideshow"])の写し。"""
    import settings as settings_module
    return json.loads(json.dumps(settings_module.DEFAULT_SETTINGS[SECTION]))


def _number(value, default, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:      # NaN
        return default
    return min(max(number, low), high)


def normalize_folders(value) -> list:
    """folders を [{"path": str, "recursive": bool}] にそろえる。文字列だけの項目は
    サブフォルダ込みとして受ける。空のパスと、同じフォルダの重なり(大小文字・末尾の \\ 違い)は落とす。"""
    result, seen = [], set()
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            path, recursive = item, True
        elif isinstance(item, dict):
            path, recursive = item.get("path"), item.get("recursive", True)
        else:
            continue
        if not isinstance(path, str) or not path.strip():
            continue
        path = path.strip()
        key = folder_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append({"path": path, "recursive": bool(recursive)})
    return result


def folder_key(path: str) -> str:
    """同じフォルダかどうかを比べる鍵。"""
    return os.path.normcase(os.path.normpath(path))


def normalize_config(section) -> dict:
    """設定のセクションを、使える値にそろえた辞書にする(壊れた値は既定値へ)。"""
    base = defaults()
    section = section if isinstance(section, dict) else {}
    interval = _number(section.get("interval_seconds"), base["interval_seconds"], *INTERVAL_RANGE)
    fade = _number(section.get("fade_seconds"), base["fade_seconds"], 0.0, FADE_MAX)
    # フェードは表示秒数の半分まで(重なりが表示より長いと、いつも何かが消えかけになる)
    fade = min(fade, interval / 2)
    fps = int(_number(section.get("fps"), base["fps"], *FPS_RANGE))
    monitor = section.get("monitor", base["monitor"])
    return {
        "folders": normalize_folders(section.get("folders", base["folders"])),
        "interval_seconds": interval,
        "fade_seconds": fade,
        "ken_burns": bool(section.get("ken_burns", base["ken_burns"])),
        "monitor": monitor if isinstance(monitor, str) else "",
        "fps": fps,
    }


def read_section(settings_path) -> dict:
    """settings.json の slideshow セクション(生のまま)。無い・壊れていれば {}。
    load_settings と違い、ファイルが無くても作らない(テストや別プロセスから読むだけのため)。"""
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return {}
    section = stored.get(SECTION) if isinstance(stored, dict) else None
    return section if isinstance(section, dict) else {}


def load_config(settings_path) -> dict:
    return normalize_config(read_section(settings_path) if settings_path else {})


def save_config(settings_path, updates: dict, app_settings=None) -> None:
    """slideshow の中の updates のキーだけを書く。ほかのキー・セクションには触れない。
    app_settings(常駐のメモリ上の設定)があれば、そちらも同じように差し替える。
    書けなければ OSError / ValueError(呼ぶ側で通知する)。"""
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


# ---------------------------------------------------------------
# 写真の一覧作り
# ---------------------------------------------------------------
def _attributes(entry) -> int:
    try:
        return getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return 0


def _is_link(entry) -> bool:
    """ジャンクション・シンボリックリンクか。OneDrive などのクラウドの印(これも
    reparse point)は潜ってよいので、属性の REPARSE_POINT では見ない。"""
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def iter_images(root: str, recursive: bool, stop=None):
    """root の写真のフルパスを見つけた順に返す。recursive ならサブフォルダも
    (ジャンクション・シンボリックリンクには潜らない)。隠し・システムのものは除く。
    読めないフォルダは黙って飛ばす。stop(threading.Event)が立ったらやめる。
    各フォルダでは先にファイルを返してから、サブフォルダへ降りる(最初の数枚を早く出すため)。"""
    stack = [root]
    while stack:
        if stop is not None and stop.is_set():
            return
        folder = stack.pop()
        files, subdirs = [], []
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if _attributes(entry) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if recursive and not _is_link(entry):
                                subdirs.append(entry.path)
                            continue
                        if os.path.splitext(entry.name)[1].lower() in IMAGE_EXTS:
                            files.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue      # 読めない・消えた・ネットワークが切れた
        yield from files
        # 名前順に降りる(stack なので逆順に積む)
        subdirs.sort(key=str.casefold, reverse=True)
        stack.extend(subdirs)


@dataclass
class _Group:
    path: str
    recursive: bool
    found: list
    bag: list
    last: object = None
    reinsert: object = None


class PhotoPool:
    """見つかった写真の入れ物と選び方。走査スレッドが add し、読み込みスレッドが pick する
    (どちらも lock の中で触る)。

    選び方: まず「写真が1枚以上見つかっているフォルダ」から等確率で1つ選び、その中の袋から
    1枚取り出す。袋が空になったら(一巡したら)見つかっている全部で詰め直す。走査中に
    見つかった写真は、いまの袋にもそのまま入る(その周のうちに出る)。詰め直した直後に、
    前の周の最後の1枚がまた出ないよう、それだけは1枚目を取ったあとで袋へ戻す。"""

    def __init__(self, folders, rng=None):
        self.rng = rng or random.Random()
        self.lock = threading.Lock()
        self.groups = [_Group(f["path"], bool(f.get("recursive", True)), [], [])
                       for f in normalize_folders(folders)]
        self.seen = set()
        self.finished = False
        self.missing = []        # 走査の時点で存在しなかったフォルダ

    def add(self, index: int, paths) -> int:
        added = 0
        with self.lock:
            group = self.groups[index]
            for path in paths:
                key = os.path.normcase(path)
                if key in self.seen:
                    continue       # 登録したフォルダが入れ子になっているときの重なり
                self.seen.add(key)
                group.found.append(path)
                group.bag.append(path)
                added += 1
        return added

    def total(self) -> int:
        with self.lock:
            return sum(len(g.found) for g in self.groups)

    def counts(self) -> list:
        with self.lock:
            return [len(g.found) for g in self.groups]

    def mark_finished(self) -> None:
        with self.lock:
            self.finished = True

    def is_finished(self) -> bool:
        with self.lock:
            return self.finished

    def pick(self):
        """次の1枚のパス。まだ1枚も無ければ None。"""
        with self.lock:
            candidates = [g for g in self.groups if g.found]
            if not candidates:
                return None
            group = self.rng.choice(candidates)
            if not group.bag:
                group.bag = list(group.found)
                if len(group.bag) > 1 and group.last in group.bag:
                    group.bag.remove(group.last)
                    group.reinsert = group.last
            i = self.rng.randrange(len(group.bag))
            path = group.bag[i]
            group.bag[i] = group.bag[-1]
            group.bag.pop()
            if group.reinsert is not None and group.reinsert != path:
                group.bag.append(group.reinsert)
            group.reinsert = None
            group.last = path
            return path

    def discard(self, path: str) -> None:
        """読めなかった写真を外す(二度と選ばない)。"""
        with self.lock:
            for group in self.groups:
                if path in group.found:
                    group.found.remove(path)
                    if path in group.bag:
                        group.bag.remove(path)
                    if group.reinsert == path:
                        group.reinsert = None
                    break


def scan_into(pool: PhotoPool, stop=None, chunk: int = SCAN_CHUNK, isdir=os.path.isdir) -> None:
    """pool の各フォルダを走査して足していく(裏のスレッドで呼ぶ)。

    フォルダが複数あるときは chunk 枚ずつ交互に読み進める(先頭の大きなフォルダを
    読み終えるまで、ほかのフォルダの写真が1枚も出ない、にならないように)。"""
    try:
        gens = []
        for index, group in enumerate(pool.groups):
            if stop is not None and stop.is_set():
                return
            if not isdir(group.path):
                with pool.lock:
                    pool.missing.append(group.path)
                continue
            gens.append((index, iter_images(group.path, group.recursive, stop)))
        while gens:
            if stop is not None and stop.is_set():
                return
            for item in list(gens):
                index, gen = item
                batch = list(islice(gen, chunk))
                if batch:
                    pool.add(index, batch)
                if len(batch) < chunk:
                    gens.remove(item)
    finally:
        pool.mark_finished()


def wait_ready(pool: PhotoPool, started: float, now=time.monotonic) -> bool:
    """最初の1枚を出し始めてよいか(START_MIN 枚見つかった・走査が終わった・START_WAIT 秒経った)。
    見つかった最初の1枚に決め打ちにならないよう、少しだけ溜まるのを待つ。"""
    return (pool.total() >= START_MIN or pool.is_finished()
            or now() - started >= START_WAIT)


# ---------------------------------------------------------------
# 履歴(← で戻る)
# ---------------------------------------------------------------
class History:
    """表示した写真のパスの並び。← で戻り、→ で(戻っていたぶんを)進む。件数に上限。"""

    def __init__(self, limit: int = HISTORY_LIMIT):
        self.limit = max(1, int(limit))
        self.items = []
        self.index = -1

    def current(self):
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    def at_end(self) -> bool:
        return self.index >= len(self.items) - 1

    def push(self, path: str) -> None:
        """新しく出した1枚を末尾に足す(戻っている途中なら、先の分は捨てる)。"""
        del self.items[self.index + 1:]
        self.items.append(path)
        if len(self.items) > self.limit:
            del self.items[:len(self.items) - self.limit]
        self.index = len(self.items) - 1

    def back(self):
        if self.index <= 0:
            return None
        self.index -= 1
        return self.items[self.index]

    def forward(self):
        if self.at_end():
            return None
        self.index += 1
        return self.items[self.index]

    def remove_current(self, step: int = -1) -> None:
        """読めなかった1枚を履歴から外す。step は進んでいた向き(+1 で →、-1 で ←)で、
        続けて同じ向きへ forward() / back() すれば、外した1枚の次(前)に着くように位置を置く。"""
        if 0 <= self.index < len(self.items):
            i = self.index
            del self.items[i]
            self.index = i - 1 if step > 0 else i


# ---------------------------------------------------------------
# Ken Burns(ゆっくりズーム)
# ---------------------------------------------------------------
@dataclass(frozen=True)
class Motion:
    z0: float = 1.0
    z1: float = 1.0
    fx0: float = 0.0
    fy0: float = 0.0
    fx1: float = 0.0
    fy1: float = 0.0


STILL = Motion()


def random_motion(rng, enabled: bool = True, zoom_max: float = ZOOM_MAX) -> Motion:
    """1枚ぶんの動き。拡大か縮小かは半々、平行移動の向きはランダム。
    fx / fy は「はみ出しているぶん」に対する位置(-1 で左・上の端、+1 で右・下の端)。"""
    if not enabled:
        return STILL
    import math
    zoom_in = rng.random() < 0.5
    z0, z1 = (1.0, zoom_max) if zoom_in else (zoom_max, 1.0)
    angle = rng.uniform(0, 2 * math.pi)
    reach = rng.uniform(0.6, 1.0)
    fx, fy = math.cos(angle) * reach, math.sin(angle) * reach
    return Motion(z0, z1, -fx, -fy, fx, fy)


def ease(t: float) -> float:
    """フェードの進み(端でゆっくり)。"""
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def frame_rect(img_w, img_h, scr_w, scr_h, motion: Motion = STILL, t: float = 0.0):
    """時刻 t(0〜1)に写真を描く矩形 (x, y, w, h)。画面に収まる大きさ(縦横比維持)を基準に、
    motion の拡大率と平行移動を掛ける。平行移動は、拡大で画面からはみ出したぶんの中だけで
    動く(黒い余白が新たに見えることはない)。"""
    if img_w <= 0 or img_h <= 0 or scr_w <= 0 or scr_h <= 0:
        return 0.0, 0.0, 0.0, 0.0
    t = min(max(t, 0.0), 1.0)
    fit = min(scr_w / img_w, scr_h / img_h)
    zoom = motion.z0 + (motion.z1 - motion.z0) * t
    w, h = img_w * fit * zoom, img_h * fit * zoom
    extra_x = max(0.0, (w - scr_w) / 2)
    extra_y = max(0.0, (h - scr_h) / 2)
    fx = motion.fx0 + (motion.fx1 - motion.fx0) * t
    fy = motion.fy0 + (motion.fy1 - motion.fy0) * t
    cx = scr_w / 2 + fx * extra_x
    cy = scr_h / 2 + fy * extra_y
    return cx - w / 2, cy - h / 2, w, h


# ---------------------------------------------------------------
# 情報表示(I)
# ---------------------------------------------------------------
def format_taken(raw: str) -> str:
    """EXIF の "2023:05:01 12:34:56" → "2023/05/01 12:34"。読めなければ ""。"""
    if not raw or len(raw) < 16:
        return ""
    date, _, clock = raw.partition(" ")
    return f"{date.replace(':', '/')} {clock[:5]}"


def photo_info(path: str) -> dict:
    """情報表示に出すもの(ファイル名・撮影日時・フォルダ)。読み込みスレッドで呼ぶ。"""
    taken = ""
    try:
        taken = format_taken(core.exif_datetime_original(core.exif_tiff_bytes(path)))
    except Exception:  # noqa: BLE001  情報が出ないだけ
        taken = ""
    return {"name": os.path.basename(path), "folder": os.path.dirname(path), "taken": taken}
