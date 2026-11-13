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
# 閉じたタブの控え(Ctrl+Shift+T で開き直す分)は、同じ場所に
# editor_closed_tabs.json として別に小さく持つ(直近10件まで、
# 1件ごとと合計の大きさの上限つき、7日より古いものは捨てる)。
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
# 保存のときは、ディスクの控えが「このプロセスが最後に見た印
# (更新時刻とサイズ)」と違うかも確かめる。違えば、別の EDDy や
# 新しい版が、このプロセスの知らない間に先に書いた新しい控えなの
# で、黙って上書きせず、新しい側を別名(editor_session.json.newer、
# あれば連番)へ退避してから書く。退避の結果は save の戻り値で
# 窓へ渡り、ステータスで利用者へ知らせる。自分の書いた控えを
# 自分で更新する通常の保存は、印がその都度追いつくので止まらない。
#
# このモジュールは Qt に依存しない(テストのため)。
import json
import os
import time
from pathlib import Path

DATA_FILENAME = "editor_session.json"
SESSION_VERSION = 1

# 閉じたタブの控えの置き場。セッションの控えと同じ場所に、
# セッションとは別に小さく持つ(editor_closed_tabs.json)。
# Ctrl+Shift+T で開き直すためのもので、直近10件まで残す。
CLOSED_TABS_FILENAME = "editor_closed_tabs.json"
CLOSED_TABS_LIMIT = 10
CLOSED_TABS_VERSION = 1
# 閉じたタブの控えの大きさの上限。未保存の全文を平文で持つので、
# 無制限だと大きな文書を閉じるたびに控えのファイルが膨らみ、
# 毎回まるごと書き直すことになる。大きさは、記録1件を JSON に
# したときの UTF-8 のバイト数で数える(ファイルに書かれる姿
# そのもの)。上限を超える記録は残さない(開き直しの控えを諦める)。
CLOSED_TABS_ENTRY_MAX_BYTES = 2 * 1024 * 1024
CLOSED_TABS_TOTAL_MAX_BYTES = 10 * 1024 * 1024
# 閉じたタブの控えの期限。閉じた日(closed_at)からこれより古い
# 記録は捨てる。closed_at の無い記録(古い版が書いたもの)は、
# 期限では落とさない。
CLOSED_TABS_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

# 退避ファイル(.newer / .broken)は読み戻す口が無く、放っておくと
# 溜まる一方なので、控え1つにつき各この件数まで残し、古いもの
# から消す。
EVACUATION_KEEP = 3


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


# 壊れた控えの退避先(セッションファイルのパス文字列 → 退避先の
# パス文字列。退避できなかったときは空文字列)。読み込みのときに
# 記録し、窓が take_corrupt_notice で受け取って利用者へ知らせる。
_CORRUPT_NOTICES = {}


def _file_stamp(path):
    """セッションファイルの印((更新時刻 ns, サイズ)。無ければ None)。

    「最後に見たときのファイルそのものか」を判定するための印。
    文書ファイルの disk_mtime / disk_size と同じ流儀で、時刻だけ
    では無くサイズも組にする。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _prune_evacuations(path, suffix):
    """退避ファイル(元の名前 + suffix、連番つき)が EVACUATION_KEEP
    件を超えたら、古いものから消す。退避ファイルは誰も読み戻さ
    ないので、溜め続けないための後始末。消すのに失敗しても、
    退避そのものは済んでいるので呼び側へは響かせない。"""
    path = Path(path)
    prefix = path.name + suffix
    try:
        siblings = []
        for item in path.parent.iterdir():
            if item.name == prefix:
                siblings.append(item)
            elif item.name.startswith(prefix + "-") \
                    and item.name[len(prefix) + 1:].isdigit():
                siblings.append(item)
    except OSError:
        return
    if len(siblings) <= EVACUATION_KEEP:
        return

    def _mtime(item):
        try:
            return item.stat().st_mtime_ns
        except OSError:
            return 0

    siblings.sort(key=_mtime)
    for item in siblings[:len(siblings) - EVACUATION_KEEP]:
        try:
            item.unlink()
        except OSError:
            pass


def _evacuate_with_suffix(path, suffix):
    """セッションファイルを「元の名前 + suffix」の別名へ退避し、
    退避先を返す。既にあれば suffix-2、…と連番にする(前の退避も
    上書きしない)。退避できなければ None。退避のたびに、古い
    退避ファイルは EVACUATION_KEEP 件を超えた分を消す。"""
    path = Path(path)
    candidate = path.with_name(path.name + suffix)
    serial = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}{suffix}-{serial}")
        serial += 1
    try:
        os.replace(path, candidate)
    except OSError:
        return None
    _prune_evacuations(path, suffix)
    return str(candidate)


def _evacuate_corrupt(path):
    """壊れたセッションファイルを別名(.broken)へ退避し、退避先を
    返す。次の保存で黙って上書きして、壊れる前の控えごと消さない
    ため。退避できなければ None。"""
    return _evacuate_with_suffix(path, ".broken")


def _evacuate_newer(path):
    """別の EDDy 等が先に書いた新しいセッションファイルを別名
    (.newer)へ退避し、退避先を返す。退避できなければ None。"""
    return _evacuate_with_suffix(path, ".newer")


def take_corrupt_notice(path):
    """壊れた控えの読み直し失敗を1回だけ受け取る。

    戻り値は None(何も無い)か {"backup": 退避先のパス文字列
    (退避できなかったときは None)}。"""
    return _CORRUPT_NOTICES.pop(str(path), None)


def load_session_file(path):
    """セッションファイルを読む。無いときは空の並び。

    ファイルはあるのに読めない・形が崩れているときは、壊れた
    ファイルを別名へ退避してから空の並びを返す(次の保存で黙って
    上書きしないため)。version がこの版より大きい(新しい版の
    EDDy が書いた)控えも、知らないフィールドを落として上書き
    しないよう、別名(.newer)へ退避して空の並びを返す。退避の
    記録は take_corrupt_notice で受け取れる(新しい版の控えは
    "kind": "newer" つき)。形は合っているが中身の値が壊れて
    いるタブは、sanitize_session が使えるものだけ残す(従来どおり)。"""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        raw = None
    data = None
    if raw is not None:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
    if not isinstance(data, dict) \
            or not isinstance(data.get("windows"), list):
        backup = _evacuate_corrupt(path)
        _CORRUPT_NOTICES[str(path)] = {"backup": backup}
        return []
    version = data.get("version")
    if isinstance(version, int) and version > SESSION_VERSION:
        # 新しい版の EDDy が書いた控え。この版の知らないフィールド
        # を落として読み、次の保存で version 1 として上書きすると、
        # 新しい版の控えが黙って消える。壊れた控えと同じく退避し
        # て空で始め、知らせは "kind": "newer" で区別する(窓が
        # 文面を変えるため)。
        backup = _evacuate_newer(path)
        _CORRUPT_NOTICES[str(path)] = {"backup": backup, "kind": "newer"}
        return []
    return sanitize_session(data)


def save_session_file(path, windows):
    """セッションファイルへ原子的に書く。

    途中で落ちても半端な状態にならないよう、一時ファイルに書いて
    から os.replace で置き換える(明日のメモと同じ作法)。"""
    path = Path(path)
    payload = {"version": SESSION_VERSION, "windows": windows}
    text = json.dumps(payload, ensure_ascii=False)
    _atomic_write_text(path, text)


# 閉じたタブの控えの置き場と、プロセス内での持ち回し。
#
# データの形:
#   {"version": 1,
#    "tabs": [タブの記録(セッションのタブと同じ形に、閉じたときの
#              タブの位置 "index" と、閉じた時刻 "closed_at"
#              (UNIX 時刻)を足したもの), ...]}
# 並びは新しい順(先頭が直近に閉じたタブ)。Ctrl+Shift+T は先頭
# から順に開き直す。直近 CLOSED_TABS_LIMIT 件まで残し、古いもの
# から捨てる。大きさの上限(1件ごと・合計)と期限(7日)も、
# 読み書きのときに当てる(prune_closed_tabs)。
#
# セッションの控えと同じく、プロセス内では置き場1つ分の状態を
# _ClosedTabsRegistry で共有する(窓が複数あっても、閉じたタブの
# 並びが食い違わないため)。保存のときは、ディスクの控えが「この
# プロセスが最後に見た印」と違うか確かめ、違えば新しい側を別名
# (.newer、あれば連番)へ退避してから書く(セッションの save と
# 同じ流儀。黙って上書きしないため)。

def _sanitize_closed_tab(value):
    """閉じたタブ1件分の記録を整える。形が違うものは None(捨てる)。

    タブの部分はセッションのタブと同じ(_sanitize_tab)。閉じた
    ときのタブの位置 "index" と、閉じた時刻 "closed_at" を足して
    持つ(closed_at は形の合った数値のときだけ残す)。"""
    tab = _sanitize_tab(value)
    if tab is None:
        return None
    index = value.get("index")
    tab["index"] = index if isinstance(index, int) and index >= 0 else 0
    closed_at = value.get("closed_at")
    if isinstance(closed_at, (int, float)) and closed_at > 0:
        tab["closed_at"] = float(closed_at)
    return tab


def _closed_tab_size(tab):
    """閉じたタブの記録1件の大きさ(JSON にしたときの UTF-8 の
    バイト数)。数えられない記録は、上限を超えたものとして扱う。"""
    try:
        return len(json.dumps(tab, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return CLOSED_TABS_ENTRY_MAX_BYTES + 1


def prune_closed_tabs(tabs, now=None):
    """閉じたタブの並び(新しい順)に、上限と期限を当てる。

    - 件数: CLOSED_TABS_LIMIT 件まで
    - 期限: closed_at が CLOSED_TABS_MAX_AGE_SECONDS より古い
      記録は落とす(closed_at の無い記録は期限では落とさない)
    - 大きさ: 1件が CLOSED_TABS_ENTRY_MAX_BYTES を超える記録は
      落とし、合計が CLOSED_TABS_TOTAL_MAX_BYTES を超える分は
      新しい方を優先して残す(収まらない記録は飛ばす)
    並びの順は変えない。"""
    if now is None:
        now = time.time()
    kept = []
    total = 0
    for tab in tabs:
        if len(kept) >= CLOSED_TABS_LIMIT:
            break
        closed_at = tab.get("closed_at")
        if isinstance(closed_at, (int, float)) \
                and now - closed_at > CLOSED_TABS_MAX_AGE_SECONDS:
            continue
        size = _closed_tab_size(tab)
        if size > CLOSED_TABS_ENTRY_MAX_BYTES:
            continue
        if total + size > CLOSED_TABS_TOTAL_MAX_BYTES:
            continue
        kept.append(tab)
        total += size
    return kept


def sanitize_closed_tabs(data):
    """ファイルから読んだデータを、閉じたタブの並びに整える。

    戻り値は [記録, ...](新しい順)。想定外の要素は黙って捨てる。
    件数・大きさ・期限の上限も当てる(prune_closed_tabs)。"""
    raw_tabs = None
    if isinstance(data, dict):
        raw_tabs = data.get("tabs")
    elif isinstance(data, list):
        raw_tabs = data
    if not isinstance(raw_tabs, list):
        return []
    tabs = []
    for item in raw_tabs:
        tab = _sanitize_closed_tab(item)
        if tab is not None:
            tabs.append(tab)
    return prune_closed_tabs(tabs)


# 新しい版が書いた閉じたタブの控えを退避した記録(ファイルのパス
# 文字列 → {"backup": 退避先のパス文字列})。窓が
# take_closed_tabs_notice で受け取って利用者へ知らせる。
_CLOSED_TABS_NOTICES = {}


def take_closed_tabs_notice(path):
    """閉じたタブの控えの退避を1回だけ受け取る。

    戻り値は None(何も無い)か {"backup": 退避先のパス文字列
    (退避できなかったときは None)}。"""
    return _CLOSED_TABS_NOTICES.pop(str(path), None)


def load_closed_tabs_file(path):
    """閉じたタブのファイルを読み、整えた並びを返す。

    無いとき・読めないとき・形が崩れているときは空の並び
    (セッションと違って、壊れたファイルの退避はしない。閉じた
    タブの控えが壊れても、開いているタブの控えは別ファイルなので
    巻き添えにならないため)。
    ただし version がこの版より大きい(新しい版の EDDy が書いた)
    控えは、セッションと同じく黙って上書きせず、別名(.newer)へ
    退避して空の並びを返す(知らせは take_closed_tabs_notice)。"""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        version = data.get("version")
        if isinstance(version, int) and version > CLOSED_TABS_VERSION:
            backup = _evacuate_newer(path)
            _CLOSED_TABS_NOTICES[str(path)] = {"backup": backup}
            return []
    return sanitize_closed_tabs(data)


def _atomic_write_text(path, text):
    """テキストを一時ファイルに書いてから os.replace で置き換える
    (途中で落ちても半端な状態にならないため。セッションと閉じた
    タブの両方で使う)。"""
    path = Path(path)
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


def save_closed_tabs_file(path, tabs):
    """閉じたタブの並びをファイルへ原子的に書く。

    件数・大きさ・期限の上限を当ててから書く(prune_closed_tabs)。
    セッションの控えとは別ファイルなので、セッションの印(.newer
    の判定)には触れない。"""
    payload = {"version": CLOSED_TABS_VERSION,
               "tabs": prune_closed_tabs(list(tabs))}
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False))


class _ClosedTabsRegistry:
    """閉じたタブのファイル1つ分の、プロセス内での状態。

    entries は閉じたタブの並び(新しい順)。stamp は「このプロセス
    が最後に見たディスクの控え」の印(_file_stamp)。読み込みの
    ときと、自分の保存が成功したときに取り直す。保存のときに
    ディスクの印がこれと違えば、よそが先に書いた新しい控えが
    あると分かる。"""

    def __init__(self):
        self.entries = None     # None はまだ読んでいない
        self.stamp = None

    def ensure_loaded(self, path):
        if self.entries is None:
            self.entries = load_closed_tabs_file(path)
            self.stamp = _file_stamp(path)


_CLOSED_TABS_REGISTRIES = {}


def _closed_tabs_registry_for(path):
    key = str(path)
    registry = _CLOSED_TABS_REGISTRIES.get(key)
    if registry is None:
        registry = _ClosedTabsRegistry()
        _CLOSED_TABS_REGISTRIES[key] = registry
    return registry


def closed_tabs(path):
    """閉じたタブの並び(新しい順)を、プロセス内で共有する並びで返す。

    呼び側はこの並びを直接いじる(先頭へ足す・先頭から取る)。
    変えたら save_closed_tabs でファイルへ書くこと。"""
    registry = _closed_tabs_registry_for(path)
    registry.ensure_loaded(path)
    return registry.entries


def save_closed_tabs(path):
    """閉じたタブの並びをファイルへ書く。

    書く前に、ディスクの控えの印が「このプロセスが最後に見た印」
    と違うか確かめる。違えば、別の EDDy が先に書いた新しい控え
    なので、黙って上書きせず、新しい側を別名(.newer、あれば連番)
    へ退避してから書く。退避したら {"backup": 退避先のパス文字列
    (退避できなかったときは None)} を返し、何も無ければ None を
    返す。"""
    registry = _closed_tabs_registry_for(path)
    registry.ensure_loaded(path)
    # 期限切れや上限超えの記録は、メモリ上の並びからも落として、
    # ファイルと中身をそろえる(落とした記録は開き直しにも出ない)。
    registry.entries[:] = prune_closed_tabs(registry.entries)
    notice = None
    current = _file_stamp(path)
    if current is not None and current != registry.stamp:
        notice = {"backup": _evacuate_newer(path)}
    save_closed_tabs_file(path, registry.entries)
    registry.stamp = _file_stamp(path)
    return notice


# ---------------------------------------------------------------
# プロセス内でのスロットの持ち回し
# ---------------------------------------------------------------
class _PathRegistry:
    """セッションファイル1つ分の、スロットの状態。

    states は窓スロットの並び(中身は窓が capture した dict)。
    live は「いま開いている窓が持っているスロット」の id の組。
    stamp は「このプロセスが最後に見たディスクの控え」の印
    (_file_stamp。ファイルが無かったときは None)。読み込みの
    ときと、自分の保存が成功したときに取り直す。保存のときに
    ディスクの印がこれと違えば、よそが先に書いた新しい控えが
    あると分かる。
    ファイルから読むのは最初の claim のときだけ(遅延)。"""

    def __init__(self):
        self.states = None      # None はまだ読んでいない
        self.live = set()
        self.stamp = None

    def ensure_loaded(self, path):
        if self.states is None:
            self.states = load_session_file(path)
            # 読み込みの直後の姿を印にする(壊れた控えが退避されて
            # ファイルが無くなった場合は None のまま)。
            self.stamp = _file_stamp(path)


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

    中身の無いスロット(タブが1つも無いもの)は書かない。

    書く前に、ディスクの控えの印が「このプロセスが最後に見た印」
    と違うか確かめる。違えば、別の EDDy や新しい版が先に書いた
    新しい控えなので、黙って上書きせず、新しい側を別名
    (editor_session.json.newer、あれば連番)へ退避してから書く。
    退避したら {"backup": 退避先のパス文字列(退避できなかった
    ときは None)} を返し、何も無ければ None を返す(窓が
    ステータスで知らせるための戻り値)。自分の控えを自分で
    更新する通常の保存では、印が保存のたびに追いつくので、
    退避は起きず戻り値も None のまま。"""
    registry = _registry_for(path)
    registry.ensure_loaded(path)
    notice = None
    current = _file_stamp(path)
    if current is not None and current != registry.stamp:
        notice = {"backup": _evacuate_newer(path)}
    windows = [state for state in registry.states if state.get("tabs")]
    save_session_file(path, windows)
    registry.stamp = _file_stamp(path)
    return notice
