# filer_extmenu.py
# あっふの Ctrl+Enter「拡張子ごとのメニュー」(あふｗの AfxExt 方式)の、Qt に依存しない部分。
#
# 何をするか
#   対象の拡張子(フォルダなら folder)のメニュー定義を読み、その PC で使える項目だけを
#   並べる。項目を選んだら、マクロ($MF $P $F $O $E)を展開して外部コマンドを起こすか、
#   内部命令(&EXCD &EDIT &NOP &SHELL &PROPERTY &VIEW &OPEN)を filer.py に返す。
#
# 置き場所(複数の PC で使う前提)
#   - 共有: リポジトリの filer_menus/<名前>.txt(UTF-8)。個人のパスは書かない。
#     ツールは {tool:名前} と書き、実体は PC ごとの settings.json の filer.tools で引く。
#     filer.tools に無い名前は App Paths レジストリと PATH から探す。見つからない
#     ツールの項目は出さない(業務PCに無いツールの項目は自動で消える)。
#   - 同じ中身の拡張子は filer_menus/_aliases.txt で「グループ: 拡張子…」と束ねる
#     (image: jpg jpeg png …)。jpg なら image.txt と jpg.txt(あれば)の両方を、この順で使う。
#   - その PC だけの追加: %LOCALAPPDATA%\traytools\menus\<名前>.txt。共有の後ろに
#     区切りを挟んで付け足す。探し方は共有と同じ(グループ → 拡張子そのもの)。
#
# 書式(あふｗのメニューにほぼ合わせる)
#   "K:表示名" コマンド        1行1項目。K は選択キー(1字)
#   "-----"                     区切り
#   # …                         コメント(空行も無視)
#   afx …                       先頭行のあふｗの見出し。無視する
#
# このファイルは os・標準ライブラリ・filer_core(data_dir)だけで書く。テストは
# tests/test_filer.py が、レジストリや PATH を差し替えて確かめる。
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import filer_core as core

MENU_DIR = Path(__file__).resolve().parent / "filer_menus"
ALIASES_FILE = "_aliases.txt"
LOCAL_SUBDIR = "menus"
FOLDER_KIND = "folder"

APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

# 内部命令。ここに無い & 命令の項目は出さない。
INTERNAL_COMMANDS = frozenset({"EXCD", "EDIT", "NOP", "SHELL", "PROPERTY", "VIEW", "OPEN"})
# 書庫の中でも使える内部命令(書庫の中のパスを扱えるもの)。外部ツール・&SHELL・
# &PROPERTY は実体のファイルが要るので、書庫の中では出さない。
ARCHIVE_OK_COMMANDS = frozenset({"EXCD", "EDIT", "NOP", "VIEW", "OPEN"})

LOCAL_TEMPLATE = (
    "# この PC だけで使うメニュー項目(共有のメニューの後ろに付け足されます)。\n"
    "# 書式: \"K:表示名\" コマンド   (K は選択キー1字)\n"
    "#   \"-----\" で区切り、# で始まる行はコメント\n"
    "# マクロ: $MF 対象のフルパス(複数可) / $P 今の窓のパス / $F カーソルのファイル名 /\n"
    "#         $O 反対窓のパス / $E 拡張子\n"
    "# 内部命令: &EXCD -P<パス> / &EXCD -O<パス> / &EDIT <ファイル> / &SHELL <フォルダ> /\n"
    "#           &PROPERTY <パス> / &VIEW <ファイル> / &OPEN <パス> / &NOP\n"
    "# ツールは {tool:名前} と書くと settings.json の filer.tools(無ければ App Paths・PATH)で探します。\n"
    "# 例: \"V:VLC\" {tool:vlc} $MF\n"
)


@dataclass
class MenuLine:
    key: str = ""
    label: str = ""
    command: str = ""
    separator: bool = False


SEPARATOR = MenuLine(separator=True)

_ITEM_RE = re.compile(r'^"([^"]*)"\s*(.*)$')
_TOOL_RE = re.compile(r"^\{tool:([^{}]+)\}$", re.I)
_UNQUOTED_EXE_RE = re.compile(r"^([^\"$]+?\.(?:exe|com|bat|cmd))(?=\s|$)", re.I)


# ---------------------------------------------------------------
# 書式の読み書き
# ---------------------------------------------------------------
def parse_line(raw: str, first: bool = False):
    """1行を MenuLine にする。項目でない行(空行・コメント・見出し・読めない行)は None。"""
    text = raw.strip()
    if not text or text.startswith("#"):
        return None
    if first and (text.lower() == "afx" or text.lower().startswith("afx ")):
        return None
    match = _ITEM_RE.match(text)
    if not match:
        return None
    head, command = match.group(1), match.group(2).strip()
    if head and set(head) == {"-"}:
        return MenuLine(separator=True)
    if len(head) >= 2 and head[1] == ":":
        key, label = head[0], head[2:]
    else:
        key, label = (head[:1], head)
    if not command:
        return None
    return MenuLine(key=key.upper(), label=label.strip() or head, command=command)


def parse_menu_text(text: str) -> list:
    items = []
    first = True
    for raw in text.splitlines():
        if raw.strip():
            line = parse_line(raw, first=first)
            first = False
            if line is not None:
                items.append(line)
    return items


def format_line(key: str, label: str, command: str) -> str:
    label = label.replace('"', "'")
    return f'"{key}:{label}" {command}'.rstrip()


def read_text(path) -> str:
    """UTF-8(BOM 付きも可)で読む。無ければ None。"""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            return f.read()
    except OSError:
        return None


def read_menu_file(path):
    text = read_text(path)
    return None if text is None else parse_menu_text(text)


def load_aliases(menu_dir=None) -> dict:
    """_aliases.txt の「グループ: 拡張子 …」を {拡張子: グループ} にする。"""
    text = read_text(Path(menu_dir or MENU_DIR) / ALIASES_FILE) or ""
    result = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        group, exts = line.split(":", 1)
        group = group.strip().lower()
        for ext in core.normalize_exts(exts):
            result.setdefault(ext, group)
    return result


def local_menu_dir() -> str:
    return os.path.join(core.data_dir(), LOCAL_SUBDIR)


def menu_kind(entry) -> str:
    """メニューを選ぶ種類。フォルダ(と ..)は folder、ファイルは小文字の拡張子(無ければ "")。"""
    if entry is None:
        return ""
    if entry.is_dir or getattr(entry, "is_parent", False):
        return FOLDER_KIND
    return kind_of_name(entry.name)


def kind_of_name(name: str) -> str:
    base = os.path.basename(name)
    if "." not in base.strip("."):
        return ""
    return base.rsplit(".", 1)[1].lower()


def menu_names(kind: str, aliases: dict) -> list:
    """kind のメニューとして読むファイル名(拡張子なし)の並び。グループ → 拡張子そのもの。"""
    if not kind:
        return []
    names = []
    group = aliases.get(kind)
    if group and group != kind:
        names.append(group)
    names.append(kind)
    return names


def find_menus(kind: str, shared_dir=None, local_dir=None):
    """(共有の項目, この PC の項目)。どちらも無ければ (None, None)。"""
    shared_dir = Path(shared_dir or MENU_DIR)
    local_dir = Path(local_dir or local_menu_dir())
    names = menu_names(kind, load_aliases(shared_dir))
    shared = local = None
    for name in names:
        items = read_menu_file(shared_dir / f"{name}.txt")
        if items is not None:
            shared = (shared or []) + items
        items = read_menu_file(local_dir / f"{name}.txt")
        if items is not None:
            local = (local or []) + items
    return shared, local


# ---------------------------------------------------------------
# コマンドの分け方(Windows の書き方)
# ---------------------------------------------------------------
def split_command(text: str) -> list:
    """コマンド文字列を引数に分ける。空白で区切り、"…" の中の空白は区切らない(引用符は外す)。

    \\ は逃がしの文字として扱わない(あふｗのメニューは "C:\\dir\\" のようにパスの末尾に
    \\ を書くため)。先頭が引用符で囲まれておらず、空白を含む exe のパスで始まるとき
    (C:\\Program Files\\x\\y.exe $MF。あふｗのメニューでよく見る書き方)は、
    .exe までを1つ目の引数とする。"""
    text = (text or "").strip()
    tokens = []
    match = _UNQUOTED_EXE_RE.match(text)
    if match and " " in match.group(1):
        tokens.append(match.group(1))
        text = text[match.end():]
    current = []
    in_token = False
    quoted = False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            in_token = True
        elif ch in " \t" and not quoted:
            if in_token:
                tokens.append("".join(current))
                current = []
                in_token = False
        else:
            current.append(ch)
            in_token = True
    if in_token:
        tokens.append("".join(current))
    return tokens


# ---------------------------------------------------------------
# ツールと exe の探し方
# ---------------------------------------------------------------
def registry_app_path(exe_name: str):
    """App Paths に登録された exe のパス(HKLM → HKCU)。無ければ None。"""
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, f"{APP_PATHS_KEY}\\{exe_name}") as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        if value:
            return os.path.expandvars(str(value)).strip().strip('"')
    return None


class Resolver:
    """exe とツール名を実際のパスにする。レジストリ・PATH・ファイルの有無は差し替えられる
    (テストで本物を見ないため)。1回のメニュー表示の間は結果を覚える。"""

    def __init__(self, tools=None, app_path=registry_app_path, which=shutil.which,
                 exists=os.path.isfile):
        self.tools = tools if isinstance(tools, dict) else {}
        self.app_path = app_path
        self.which = which
        self.exists = exists
        self._cache = {}

    def exe(self, text: str):
        """exe の名前かパスを、実在するフルパスに。見つからなければ None。"""
        key = ("exe", text)
        if key not in self._cache:
            self._cache[key] = self._find_exe(text)
        return self._cache[key]

    def _find_exe(self, text):
        text = os.path.expandvars((text or "").strip().strip('"'))
        if not text:
            return None
        if os.path.isabs(text) or "\\" in text or "/" in text:
            return text if self.exists(text) else None
        name = text if os.path.splitext(text)[1] else text + ".exe"
        try:
            found = self.app_path(name) if self.app_path else None
        except OSError:
            found = None
        if found and self.exists(found):
            return found
        try:
            found = self.which(text) if self.which else None
        except OSError:
            found = None
        return found or None

    def tool(self, name: str):
        """{tool:名前} を ([exe, 引数…], 作業フォルダ or None) に。見つからなければ None。

        settings の filer.tools にあればそれ(文字列「コマンド 引数」か
        {"command": [...] か "...", "cwd": "..."})。無ければ App Paths と PATH から名前で探す。"""
        name = (name or "").strip()
        spec = self.tools.get(name)
        if spec is None:
            lowered = {str(k).lower(): v for k, v in self.tools.items()}
            spec = lowered.get(name.lower())
        cwd = None
        if spec is not None:
            if isinstance(spec, str):
                argv = split_command(spec)
            elif isinstance(spec, dict):
                command = spec.get("command")
                if isinstance(command, list):
                    argv = [str(a) for a in command if isinstance(a, (str, int, float))]
                elif isinstance(command, str):
                    argv = split_command(command)
                else:
                    argv = []
                if isinstance(spec.get("cwd"), str) and spec["cwd"].strip():
                    cwd = os.path.expandvars(spec["cwd"].strip())
            else:
                argv = []
            if not argv:
                return None
            exe = self.exe(argv[0])
            if exe is None:
                return None
            return [exe] + argv[1:], cwd
        exe = self.exe(name)
        return ([exe], None) if exe else None


# ---------------------------------------------------------------
# 項目の出し分けと実行の組み立て
# ---------------------------------------------------------------
def internal_name(command: str):
    """& で始まる内部命令なら、その名前(大文字)。それ以外は None。"""
    text = (command or "").strip()
    if not text.startswith("&"):
        return None
    return text[1:].split(None, 1)[0].upper() if len(text) > 1 else ""


def internal_arg(command: str) -> str:
    parts = command.strip().split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def program_of(command: str, resolver: Resolver):
    """外部コマンドの ([exe, 前置きの引数…], 作業フォルダ, 残りの引数のひな形) か None。"""
    tokens = split_command(command)
    if not tokens:
        return None
    match = _TOOL_RE.match(tokens[0])
    if match:
        found = resolver.tool(match.group(1))
        if found is None:
            return None
        argv, cwd = found
        return argv, cwd, tokens[1:]
    exe = resolver.exe(tokens[0])
    if exe is None:
        return None
    return [exe], None, tokens[1:]


def usable(line: MenuLine, resolver: Resolver, in_archive: bool = False) -> bool:
    """この PC・この場所で出してよい項目か。"""
    if line.separator:
        return True
    name = internal_name(line.command)
    if name is not None:
        if name not in INTERNAL_COMMANDS:
            return False
        return name in ARCHIVE_OK_COMMANDS if in_archive else True
    if in_archive:
        return False
    return program_of(line.command, resolver) is not None


def tidy_separators(lines) -> list:
    """先頭・末尾・連続の区切りを落とす(出さない項目が抜けた跡を詰める)。"""
    result = []
    for line in lines:
        if line.separator and (not result or result[-1].separator):
            continue
        result.append(line)
    while result and result[-1].separator:
        result.pop()
    return result


def visible_items(shared, local, resolver: Resolver, in_archive: bool = False) -> list:
    """共有 → 区切り → この PC の分、の順に、出してよい項目だけを並べる。"""
    lines = list(shared or [])
    if local:
        lines.append(MenuLine(separator=True))
        lines.extend(local)
    return tidy_separators([line for line in lines if usable(line, resolver, in_archive)])


def _quote_join(paths) -> str:
    return " ".join(f'"{p}"' for p in paths)


def expand_text(text: str, ctx: dict, multi: bool = True) -> str:
    """$MF $P $F $O $E を文字列の中で展開する(値は囲まない)。

    $MF が複数のときは各々を "" で囲んで空白で並べる(あふｗと同じ)。multi=False なら
    先頭の1つだけ。"""
    targets = ctx.get("targets") or []

    def repl(match):
        name = match.group(1)
        if name == "MF":
            if not targets:
                return ""
            if multi and len(targets) > 1:
                return _quote_join(targets)
            return targets[0]
        return str(ctx.get(name) or "")

    return re.sub(r"\$(MF|P|F|O|E)", repl, text)


def expand_args(tokens, ctx: dict) -> list:
    """引数のひな形(分けた後)を展開する。

    分けてから展開するので、値に空白があっても1つの引数のまま(subprocess が必要なら
    囲む)。メニュー側で "$MF" と書いても、引用符は分けるときに外れているので二重に
    ならない。引数がまるごと $MF(末尾の \\ は可)なら、マークの数だけ引数に並べる。"""
    targets = ctx.get("targets") or []
    args = []
    for token in tokens:
        match = re.fullmatch(r"\$MF(\\?)", token)
        if match:
            args.extend(t + match.group(1) for t in targets)
            continue
        args.append(expand_text(token, ctx))
    return args


def build_external(line: MenuLine, ctx: dict, resolver: Resolver):
    """外部コマンドの (argv, cwd)。作業フォルダは、ツールの cwd → 今の窓のフォルダ
    (ctx["cwd"]。書庫の中なら書庫のあるフォルダ)。
    exe が見つからなければ None。"""
    found = program_of(line.command, resolver)
    if found is None:
        return None
    argv, cwd, rest = found
    return argv + expand_args(rest, ctx), cwd or ctx.get("cwd") or ctx.get("P") or None


def strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text.replace('"', "")


def parse_excd(arg: str, ctx: dict):
    """&EXCD の引数を (窓 "P"|"O", パス, 中を開くか) にする。

    あふｗと同じく、-P / -O(大文字)は末尾に \\ があればそのフォルダを開き、無ければ
    親を開いてそれにカーソルを合わせる。小文字の -p / -o は常にそのパスを開く。"""
    arg = arg.strip()
    match = re.match(r"^-([PpOo])(.*)$", arg)
    if not match:
        return None
    flag, rest = match.group(1), match.group(2)
    path = strip_quotes(expand_text(strip_quotes(rest), ctx, multi=False))
    if not path:
        return None
    side = flag.upper()
    if flag.islower():
        return side, path.rstrip("\\") or path, True
    if path.endswith("\\") or path.endswith("/"):
        trimmed = path.rstrip("\\/")
        if len(trimmed) == 2 and trimmed[1] == ":":
            trimmed += "\\"
        return side, trimmed or path, True
    return side, path, False


def internal_targets(arg: str, ctx: dict) -> list:
    """&PROPERTY / &VIEW / &OPEN / &SHELL / &EDIT の引数を、パスの並びにする。"""
    tokens = split_command(arg)
    if not tokens:
        return []
    paths = []
    for token in tokens:
        paths.extend(p.rstrip("\\") if len(p.rstrip("\\")) > 2 else p
                     for p in expand_args([token], ctx))
    return [p for p in paths if p]


# ---------------------------------------------------------------
# メニューの編集(この PC の分・共有の分。設定ダイアログと「ツールを追加」)
# ---------------------------------------------------------------
_KEY_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def suggest_key(label: str, used) -> str:
    """選択キーの提案。表示名の先頭の英数字、使われていれば表示名の中の次の英数字、
    それも無ければ空いている英数字。"""
    used = {str(u).upper() for u in used if u}
    chars = [c.upper() for c in (label or "") if c.isascii() and c.isalnum()]
    for c in chars:
        if c not in used:
            return c
    for c in _KEY_CHARS:
        if c not in used:
            return c
    return chars[0] if chars else "A"


def tool_name_from_exe(exe: str) -> str:
    stem = os.path.splitext(os.path.basename(exe or ""))[0].lower()
    stem = re.sub(r"[^a-z0-9_.-]+", "_", stem).strip("_.")
    return stem or "tool"


def _tool_exe_and_cwd(spec):
    if isinstance(spec, str):
        argv = split_command(spec)
        return (argv[0] if argv else ""), "", argv[1:]
    if isinstance(spec, dict):
        command = spec.get("command")
        argv = (list(command) if isinstance(command, list)
                else split_command(command) if isinstance(command, str) else [])
        cwd = spec.get("cwd") if isinstance(spec.get("cwd"), str) else ""
        return (str(argv[0]) if argv else ""), cwd or "", argv[1:]
    return "", "", []


def tool_value(exe: str, cwd: str = "") -> dict:
    value = {"command": [exe]}
    if cwd:
        value["cwd"] = cwd
    return value


def register_tool(tools: dict, exe: str, cwd: str = ""):
    """exe を filer.tools に名前付きで載せる。(名前, 新しく書く値 or None)。

    同じ exe・同じ作業フォルダ・前置きの引数なしのものが既にあれば、その名前を使い回す
    (値は書かない)。名前は exe の名前から作り、別物が使っていれば番号を付ける。"""
    norm = os.path.normcase(os.path.normpath(exe))
    for name, spec in (tools or {}).items():
        t_exe, t_cwd, t_rest = _tool_exe_and_cwd(spec)
        if (t_exe and not t_rest and os.path.normcase(os.path.normpath(t_exe)) == norm
                and os.path.normcase(t_cwd or "") == os.path.normcase(cwd or "")):
            return name, None
    base = tool_name_from_exe(exe)
    name = base
    number = 2
    lowered = {str(k).lower() for k in (tools or {})}
    while name.lower() in lowered:
        name = f"{base}{number}"
        number += 1
    return name, tool_value(exe, cwd)


_PERSONAL_PATH_RE = re.compile(r"(?:^|[\s\"'=,])(?:[A-Za-z]:[\\/]|\\\\[^\\\s])")


def has_personal_path(line: str, home: str = None) -> bool:
    """共有のメニュー行に、PC 固有のパス(ドライブ付き・UNC・ホーム)が入っていないか。"""
    if _PERSONAL_PATH_RE.search(line or ""):
        return True
    home = home if home is not None else os.path.expanduser("~")
    return bool(home) and os.path.normcase(home) in os.path.normcase(line or "")


class MenuFile:
    """メニューの1ファイルを、行のまま持って編集する(コメント・空行・書き方を崩さない)。

    項目の番号は「項目として読める行」の並び。置き換え・削除・入れ替えはその行だけを
    触り、追加は末尾に足す。"""

    def __init__(self, path, text: str = None):
        self.path = Path(path)
        if text is None:
            text = read_text(self.path)
        self.existed = text is not None
        self.lines = (text or "").replace("\r\n", "\n").split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        self.changed = False

    def _rows(self):
        rows = []
        first = True
        for index, raw in enumerate(self.lines):
            if not raw.strip():
                continue
            line = parse_line(raw, first=first)
            first = False
            if line is not None:
                rows.append((index, line))
        return rows

    def items(self) -> list:
        return [line for _i, line in self._rows()]

    def append(self, text: str) -> None:
        self.lines.append(text)
        self.changed = True

    def replace(self, number: int, text: str) -> None:
        index = self._rows()[number][0]
        self.lines[index] = text
        self.changed = True

    def delete(self, number: int) -> None:
        index = self._rows()[number][0]
        del self.lines[index]
        self.changed = True

    def move(self, number: int, step: int) -> bool:
        rows = self._rows()
        target = number + step
        if not (0 <= number < len(rows) and 0 <= target < len(rows)):
            return False
        a, b = rows[number][0], rows[target][0]
        self.lines[a], self.lines[b] = self.lines[b], self.lines[a]
        self.changed = True
        return True

    def text(self) -> str:
        return "\n".join(self.lines) + "\n" if self.lines else ""

    def save(self, backup: bool = False) -> None:
        """UTF-8・LF・末尾改行で書く。backup なら書く前の中身を .bak に1世代残す。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if backup and self.path.exists():
            shutil.copyfile(self.path, str(self.path) + ".bak")
        temp = str(self.path) + ".tmp"
        with open(temp, "w", encoding="utf-8", newline="") as f:
            f.write(self.text())
        os.replace(temp, self.path)
        self.changed = False


def ensure_local_file(kind: str, local_dir=None) -> str:
    """この PC の追加ファイル。無ければ説明だけの雛形を作る。パスを返す。"""
    folder = Path(local_dir or local_menu_dir())
    path = folder / f"{kind}.txt"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(LOCAL_TEMPLATE)
    return str(path)


class MenuEditor:
    """設定ダイアログの「拡張子とツール」と「ツールを追加」が使う編集の写し。

    決定(commit)まではファイルにも settings.json にも書かない。commit は書いたファイルと、
    filer.tools に足す値を返す(settings.json への書き込みは filer.py が、キーだけ差し替えで
    行う)。この PC の分は .bak を1世代残す。共有の分(リポジトリ)は git があるので残さない。"""

    def __init__(self, tools=None, shared_dir=None, local_dir=None):
        self.shared_dir = Path(shared_dir or MENU_DIR)
        self.local_dir = Path(local_dir or local_menu_dir())
        self.tools = dict(tools) if isinstance(tools, dict) else {}
        self.tool_updates = {}
        self._local = {}
        self._shared = {}
        self._extra_kinds = set()

    # --- 読み ---
    def kinds(self) -> list:
        """一覧に出す種類(folder を先頭に、あとは名前順)。"""
        found = set(self._extra_kinds) | set(self._local) | set(self._shared)
        aliases = load_aliases(self.shared_dir)
        found |= set(aliases)
        for folder in (self.shared_dir, self.local_dir):
            try:
                for path in folder.glob("*.txt"):
                    if path.name != ALIASES_FILE and not path.name.startswith("_"):
                        found.add(path.stem.lower())
            except OSError:
                pass
        groups = set(aliases.values())
        found -= {g for g in groups if g not in aliases}   # image などのグループ名は出さない
        found.add(FOLDER_KIND)
        return [FOLDER_KIND] + sorted(k for k in found if k != FOLDER_KIND)

    def add_kind(self, kind: str) -> str:
        exts = core.normalize_exts(kind)
        if not exts:
            return ""
        self._extra_kinds.add(exts[0])
        return exts[0]

    def local_file(self, kind: str) -> MenuFile:
        if kind not in self._local:
            self._local[kind] = MenuFile(self.local_dir / f"{kind}.txt")
        return self._local[kind]

    def shared_file(self, kind: str) -> MenuFile:
        if kind not in self._shared:
            self._shared[kind] = MenuFile(self.shared_dir / f"{kind}.txt")
        return self._shared[kind]

    def readonly_items(self, kind: str) -> list:
        """(出どころ, MenuLine) の並び。共有の分と、この PC のグループの分(編集できない)。"""
        aliases = load_aliases(self.shared_dir)
        rows = []
        for name in menu_names(kind, aliases):
            source = f"共有 {name}.txt"
            if name == kind:
                rows += [(source, line) for line in self.shared_file(kind).items()]
            else:
                rows += [(source, line) for line in read_menu_file(
                    self.shared_dir / f"{name}.txt") or []]
        group = aliases.get(kind)
        if group and group != kind:
            rows += [(f"この PC {group}.txt", line) for line in read_menu_file(
                self.local_dir / f"{group}.txt") or []]
        return rows

    def local_items(self, kind: str) -> list:
        return self.local_file(kind).items()

    def used_keys(self, kind: str) -> set:
        return {line.key for _s, line in self.readonly_items(kind) if line.key} | {
            line.key for line in self.local_items(kind) if line.key}

    # --- 書き(写しの上だけ) ---
    def make_line(self, result: dict):
        """「ツールを追加」の結果からメニュー行を作る。exe はツールとして登録する。

        result: {"label", "key", "program": ("exe", パス) | ("tool", 名前) | ("raw", 文字列),
                 "args", "cwd"}"""
        kind_, value = result.get("program") or ("raw", "")
        cwd = (result.get("cwd") or "").strip()
        if kind_ == "exe":
            name, new_value = register_tool(self.tools, value, cwd)
            if new_value is not None:
                self.tools[name] = new_value
                self.tool_updates[name] = new_value
            program = f"{{tool:{name}}}"
        elif kind_ == "tool":
            program = f"{{tool:{value}}}"
        else:
            program = value.strip()
        args = (result.get("args") or "").strip()
        command = f"{program} {args}".strip()
        return format_line(result.get("key") or "", result.get("label") or "", command)

    def add_item(self, kind: str, result: dict, scope: str = "local") -> str:
        """項目を足す。scope は "local"(この PC)か "shared"(リポジトリ)。足した行を返す。
        共有に個人のパスが入るときは ValueError。"""
        line = self.make_line(result)
        if scope == "shared":
            if has_personal_path(line):
                raise ValueError("共有のメニューに PC 固有のパスは書けません: " + line)
            self.shared_file(kind).append(line)
        else:
            self.local_file(kind).append(line)
        self._extra_kinds.add(kind)
        return line

    def replace_local(self, kind: str, number: int, result: dict) -> str:
        line = self.make_line(result)
        self.local_file(kind).replace(number, line)
        return line

    def delete_local(self, kind: str, number: int) -> None:
        self.local_file(kind).delete(number)

    def move_local(self, kind: str, number: int, step: int) -> bool:
        return self.local_file(kind).move(number, step)

    def commit(self) -> dict:
        """写しをファイルへ書く。{"local": [パス], "shared": [パス], "tools": {名前: 値}}。"""
        written = {"local": [], "shared": [], "tools": dict(self.tool_updates)}
        for menu in self._local.values():
            if menu.changed:
                menu.save(backup=True)
                written["local"].append(str(menu.path))
        for menu in self._shared.values():
            if menu.changed:
                menu.save(backup=False)
                written["shared"].append(str(menu.path))
        self.tool_updates = {}
        return written


def describe_program(command: str) -> tuple:
    """編集のときに、メニュー行のコマンドを (program, 引数) に分け直す。
    program は ("tool", 名前) か ("raw", 1つ目)。"""
    tokens = split_command(command)
    if not tokens:
        return ("raw", ""), ""
    match = _TOOL_RE.match(tokens[0])
    head = command.strip()
    # 引数は元の書き方のまま残す(引用符を崩さない)。
    rest = ""
    if head.startswith('"'):
        end = head.find('"', 1)
        rest = head[end + 1:].strip() if end > 0 else ""
    else:
        exe_match = _UNQUOTED_EXE_RE.match(head)
        if exe_match and " " in exe_match.group(1):
            rest = head[exe_match.end():].strip()
        else:
            parts = head.split(None, 1)
            rest = parts[1] if len(parts) > 1 else ""
    if match:
        return ("tool", match.group(1)), rest
    return ("raw", tokens[0] if " " not in tokens[0] else f'"{tokens[0]}"'), rest


# ---------------------------------------------------------------
# 「この拡張子を開けるアプリ」の候補(レジストリ。COM は使わない)
# ---------------------------------------------------------------
FILE_EXTS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
CANDIDATE_LIMIT = 40


class Registry:
    """読むだけのレジストリの口。テストでは同じ形の偽物に差し替える。
    hive は "HKCU" / "HKCR" / "HKLM"。"""

    def _hive(self, name):
        import winreg
        return {"HKCU": winreg.HKEY_CURRENT_USER, "HKCR": winreg.HKEY_CLASSES_ROOT,
                "HKLM": winreg.HKEY_LOCAL_MACHINE}[name]

    def values(self, hive: str, path: str) -> dict:
        import winreg
        result = {}
        try:
            with winreg.OpenKey(self._hive(hive), path) as key:
                index = 0
                while index < 2000:
                    try:
                        name, value, _kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    result[name] = value
                    index += 1
        except OSError:
            pass
        return result

    def subkeys(self, hive: str, path: str) -> list:
        import winreg
        result = []
        try:
            with winreg.OpenKey(self._hive(hive), path) as key:
                index = 0
                while index < 2000:
                    try:
                        result.append(winreg.EnumKey(key, index))
                    except OSError:
                        break
                    index += 1
        except OSError:
            pass
        return result

    def default(self, hive: str, path: str):
        return self.values(hive, path).get("")


def exe_from_command(command) -> str:
    """shell\\open\\command の値から exe のパスを取り出す。取れなければ ""。"""
    if not isinstance(command, str):
        return ""
    text = os.path.expandvars(command.strip())
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else ""
    match = _UNQUOTED_EXE_RE.match(text)
    if match:
        return match.group(1)
    return text.split()[0]


def open_with_candidates(kind: str, registry=None, exists=os.path.isfile,
                         app_path=registry_app_path, limit: int = CANDIDATE_LIMIT) -> list:
    """kind(拡張子、または folder)を開けるアプリの exe(実在するものだけ。重複なし)。

    拡張子: HKCU の FileExts\\.ext\\OpenWithList(MRUList の順)と OpenWithProgids、
    HKCR\\.ext の既定の ProgId・OpenWithProgids・OpenWithList。ProgId は
    HKCR\\<ProgId>\\shell\\open\\command、exe 名は HKCR\\Applications\\<exe>\\shell\\open\\command
    (無ければ App Paths)。フォルダ: HKCR\\Directory\\shell\\*\\command と Folder\\shell\\*\\command。"""
    registry = registry or Registry()
    found = []
    seen = set()

    def add(exe):
        exe = (exe or "").strip().strip('"')
        if not exe or len(found) >= limit:
            return
        if not os.path.isabs(exe):
            return
        key = os.path.normcase(os.path.normpath(exe))
        if key in seen or not exists(exe):
            return
        seen.add(key)
        found.append(exe)

    def from_progid(progid):
        add(exe_from_command(registry.default("HKCR", f"{progid}\\shell\\open\\command")))

    def from_app(exe_name):
        command = registry.default("HKCR", f"Applications\\{exe_name}\\shell\\open\\command")
        exe = exe_from_command(command)
        if not exe:
            try:
                exe = app_path(exe_name) if app_path else ""
            except OSError:
                exe = ""
        add(exe)

    if kind == FOLDER_KIND:
        for base in ("Directory", "Folder"):
            for verb in registry.subkeys("HKCR", f"{base}\\shell"):
                add(exe_from_command(registry.default("HKCR", f"{base}\\shell\\{verb}\\command")))
        return found

    ext = "." + kind.lower().lstrip(".")
    user = f"{FILE_EXTS_KEY}\\{ext}"
    values = registry.values("HKCU", f"{user}\\OpenWithList")
    order = values.get("MRUList") if isinstance(values.get("MRUList"), str) else ""
    names = [values[c] for c in order if isinstance(values.get(c), str)]
    names += [v for k, v in sorted(values.items())
              if k != "MRUList" and isinstance(v, str) and v not in names]
    for name in names:
        from_app(name)
    for progid in registry.values("HKCU", f"{user}\\OpenWithProgids"):
        if progid:
            from_progid(progid)
    default = registry.default("HKCR", ext)
    if isinstance(default, str) and default:
        from_progid(default)
    for progid in registry.values("HKCR", f"{ext}\\OpenWithProgids"):
        if progid:
            from_progid(progid)
    for name in registry.subkeys("HKCR", f"{ext}\\OpenWithList"):
        from_app(name)
    return found


def file_description(path: str) -> str:
    """exe のバージョン情報の FileDescription(無ければ "")。ctypes の version.dll だけで読む。"""
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.WinDLL("version")
        size_fn = version.GetFileVersionInfoSizeW
        size_fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        size_fn.restype = wintypes.DWORD
        info_fn = version.GetFileVersionInfoW
        info_fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        info_fn.restype = wintypes.BOOL
        query_fn = version.VerQueryValueW
        query_fn.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(wintypes.UINT)]
        query_fn.restype = wintypes.BOOL
        handle = wintypes.DWORD(0)
        size = size_fn(path, ctypes.byref(handle))
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not info_fn(path, 0, size, buffer):
            return ""
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        pairs = []
        if query_fn(buffer, "\\VarFileInfo\\Translation", ctypes.byref(pointer),
                    ctypes.byref(length)) and length.value >= 4:
            count = length.value // 4
            words = (ctypes.c_ushort * (count * 2)).from_address(pointer.value)
            for i in range(count):
                pairs.append((words[i * 2], words[i * 2 + 1]))
        pairs += [(0x0411, 0x04B0), (0x0409, 0x04B0), (0x0409, 0x04E4)]
        for lang, codepage in pairs:
            sub = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
            if query_fn(buffer, sub, ctypes.byref(pointer), ctypes.byref(length)) \
                    and length.value > 1:
                return ctypes.wstring_at(pointer, length.value - 1).strip()
    except (OSError, AttributeError, ValueError):
        return ""
    return ""
