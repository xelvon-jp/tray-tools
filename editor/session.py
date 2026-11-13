# editor/session.py
# EDDy のセッション(タブと控え)の置き場と、プロセス内での持ち回し。
#
# 仕様(Nagato が文章で確認済み。メモ帳式):
#   - 編集した内容は、控え(ドラフト)を自動で取り続ける。ファイル
#     自体は、明示的に保存するまで書き換わらない
#   - 窓やタブを閉じても「保存しますか」は出さない。閉じても内容は
#     無くならない
#   - 次に EDDy を開くと、前回のタブ・未保存の内容・カーソル位置が
#     そのまま戻る。無題の文書も同じく戻る
#
# 置き場はリポジトリの外(%LOCALAPPDATA%\traytools\editor_session.json。
# 明日のメモ tomorrow_memo.py と同じ流儀で、環境変数
# TRAYTOOLS_DATA_DIR があればそこを使う)。settings.json には混ぜない
# (開いているタブの中身まで設定ファイルに書くと、差分保存の作法と
# ぶつかるうえ、本文が大きいときに設定全体が重くなるため)。
#
# データの形:
#   {"version": 1,
#    "windows": [{"active": タブの番号,
#                 "tabs": [{"path": 保存先(None は無題),
#                           "dirty": 控えが要るか(未保存の変更があるか),
#                           "text": 本文(dirty のときだけ。改行は "\n"),
#                           "encoding" / "bom" / "newline": 文書の形式
#                             (dirty のときだけ),
#                           "disk_mtime" / "disk_size": 控えを取った
#                             時点でのディスクの印(dirty のときだけ。
#                             戻したときに外部変更を見分けるため),
#                           "cursor": カーソル位置(QTextCursor の位置),
#                           "anchor": 選択の起点(カーソルと同じ単位),
#                           "split": 分割の向き("horizontal" は左右、
#                             "vertical" は上下。分割していなければ無い),
#                           "split_cursor" / "split_anchor": 2枚目の
#                             ペインのカーソル位置と選択の起点,
#                           "split_sizes": スプリッタの幅(高さ)の並び},
#                          ...]},
#                ...]}
# 変更の無いタブは path とカーソルだけを持つ(戻すときはディスクから
# 読み直すので、本文を控える必要が無い)。
#
# プロセス内では、開いている窓が「スロット」(windows の要素1つ分)を
# 1つずつ持つ。窓が作られるときに、前回の残り(未使用のスロット)が
# あれば先頭から引き取る(claim)。窓が閉じたらスロットは未使用に
# 戻り、次に開く窓が引き取る。保存(save)は、生きているスロットと
# 未使用のスロットを全部まとめてファイルへ書く(原子的。一時ファイル
# + os.replace)。だから、窓を閉じても・プロセスが落ちても、最後に
# 書き出された控えからタブが戻る。
#
# このモジュールは Qt に依存しない(テストのため)。
import json
import os
from pathlib import Path

DATA_FILENAME = "editor_session.json"
SESSION_VERSION = 1


def data_dir() -> Path:
    """アプリデータの置き場(%LOCALAPPDATA% の下の traytools フォルダ)。

    tomorrow_memo.data_dir と同じ作法。環境変数 TRAYTOOLS_DATA_DIR が
    あればそこを使う(テストが一時フォルダへ向けるため)。"""
    override = os.environ.get("TRAYTOOLS_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "traytools"


def default_session_path() -> Path:
    return data_dir() / DATA_FILENAME


# ---------------------------------------------------------------
# 読んだデータの整形(壊れた値で窓が開かなくならないため)
# ---------------------------------------------------------------
def _sanitize_tab(value):
    """タブ1件分の記録を整える。形が違うものは None(捨てる)。"""
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    if path is not None and not isinstance(path, str):
        return None
    tab = {"path": path, "dirty": bool(value.get("dirty"))}
    cursor = value.get("cursor")
    tab["cursor"] = cursor if isinstance(cursor, int) and cursor >= 0 else 0
    anchor = value.get("anchor")
    tab["anchor"] = anchor if isinstance(anchor, int) and anchor >= 0 \
        else tab["cursor"]
    split = value.get("split")
    if split in ("horizontal", "vertical"):
        tab["split"] = split
        for key in ("split_cursor", "split_anchor"):
            position = value.get(key)
            tab[key] = position \
                if isinstance(position, int) and position >= 0 else 0
        sizes = value.get("split_sizes")
        if (isinstance(sizes, list) and len(sizes) == 2
                and all(isinstance(size, int) and size > 0
                        for size in sizes)):
            tab["split_sizes"] = list(sizes)
    if tab["dirty"]:
        text = value.get("text")
        if not isinstance(text, str):
            return None  # 控えが要るのに本文が無い記録は使えない
        tab["text"] = text
        encoding = value.get("encoding")
        tab["encoding"] = encoding if isinstance(encoding, str) \
            else "utf-8"
        tab["bom"] = bool(value.get("bom"))
        newline = value.get("newline")
        tab["newline"] = newline if newline in ("\r\n", "\n") else "\r\n"
        for key in ("disk_mtime", "disk_size"):
            stamp = value.get(key)
            tab[key] = stamp if isinstance(stamp, (int, float)) else None
    return tab


def sanitize_session(data):
    """ファイルから読んだデータを、窓スロットの並びに整える。

    戻り値は [{"tabs": [...], "active": int}, ...]。想定外の要素は
    黙って捨てる(手で書き換えられた・壊れたファイルでも、使える
    タブだけは戻せるように)。"""
    windows = []
    if not isinstance(data, dict):
        return windows
    raw_windows = data.get("windows")
    if not isinstance(raw_windows, list):
        return windows
    for raw in raw_windows:
        if not isinstance(raw, dict):
            continue
        tabs = []
        raw_tabs = raw.get("tabs")
        if isinstance(raw_tabs, list):
            for item in raw_tabs:
                tab = _sanitize_tab(item)
                if tab is not None:
                    tabs.append(tab)
        if not tabs:
            continue
        active = raw.get("active")
        if not isinstance(active, int) or not 0 <= active < len(tabs):
            active = 0
        windows.append({"tabs": tabs, "active": active})
    return windows


def load_session_file(path):
    """セッションファイルを読む。無い・壊れているときは空の並び。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sanitize_session(data)


def save_session_file(path, windows):
    """セッションファイルへ原子的に書く。

    途中で落ちても半端な状態にならないよう、一時ファイルに書いて
    から os.replace で置き換える(明日のメモと同じ作法)。"""
    path = Path(path)
    payload = {"version": SESSION_VERSION, "windows": windows}
    text = json.dumps(payload, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------
# プロセス内でのスロットの持ち回し
# ---------------------------------------------------------------
class _PathRegistry:
    """セッションファイル1つ分の、スロットの状態。

    states は窓スロットの並び(中身は窓が capture した dict)。
    live は「いま開いている窓が持っているスロット」の id の組。
    ファイルから読むのは最初の claim のときだけ(遅延)。"""

    def __init__(self):
        self.states = None      # None はまだ読んでいない
        self.live = set()

    def ensure_loaded(self, path):
        if self.states is None:
            self.states = load_session_file(path)


_REGISTRIES = {}


def _registry_for(path):
    key = str(path)
    registry = _REGISTRIES.get(key)
    if registry is None:
        registry = _PathRegistry()
        _REGISTRIES[key] = registry
    return registry


def claim(path):
    """窓スロットを1つ引き取る。

    前回の残り(未使用のスロット)があれば先頭のものを、無ければ
    空のスロットを新しく作って返す。戻り値の dict は窓が所有し、
    capture のたびに中身を差し替える。"""
    registry = _registry_for(path)
    registry.ensure_loaded(path)
    for state in registry.states:
        if id(state) not in registry.live:
            registry.live.add(id(state))
            return state
    state = {"tabs": [], "active": 0}
    registry.states.append(state)
    registry.live.add(id(state))
    return state


def release(path, slot):
    """窓が閉じるときにスロットを手放す。

    スロットの中身はファイルに残る(次に開く窓が claim で引き取り、
    タブが戻る)。手放しただけでは書かないので、呼び側は閉じる前に
    capture して save すること。"""
    registry = _registry_for(path)
    registry.live.discard(id(slot))


def save(path):
    """いまのスロット全部(使用中・未使用とも)をファイルへ書く。

    中身の無いスロット(タブが1つも無いもの)は書かない。"""
    registry = _registry_for(path)
    registry.ensure_loaded(path)
    windows = [state for state in registry.states if state.get("tabs")]
    save_session_file(path, windows)
