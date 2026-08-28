# mirror_notes.py
# 画面ミラー(screen_mirror.py)の「カンペ」——発表者の手元にだけ出すメモの、ファイル側の部品。
#
# 窓そのものは screen_mirror.MirrorNotesWindow が持つ。ここに窓を置かないのは、あちらが
# _TopmostWindow(枠なし・最前面・前面を奪わない)を継承する必要があり、こちらから import
# すると循環するため。snippets.py(ファイルの面倒を見る)と picker.py(窓)の分け方に揃えてある。
#
# カンペは notes/ フォルダに置いた *.md / *.txt で、ファイル名(拡張子を除く)がそのまま
# 表示名になる。定型文(snippets/)と同じ流儀で、専用の編集UIは持たない——発表の直前に
# 書き足すものなので、使い慣れたエディタで書けるほうがよい。ここが持つのは「一覧を作る」
# 「読む」「無ければ作ってエディタへ渡す」だけ。
#
# 通知(toast)をここから出さないのは意図的。toast.py はカーソルのある場所の近くに出るので、
# ミラーの撮影範囲に重なるとそのまま共有側へ映る。カンペは「手元だけに見えるもの」なので、
# 失敗したことも手元だけに出さなければ意味が無い。エラーは戻り値と stderr で返し、
# 画面に出すのは窓側(カンペのパネルの中。撮影範囲の外にある)が受け持つ。
import os
import subprocess
import sys
from pathlib import Path

NOTES_DIR = Path(__file__).resolve().parent / "notes"

# 読む対象。先頭が「新しく作るときの拡張子」でもある。Markdown を既定にするのは、
# 見出し(#)でジャンプする機能がそもそも Markdown の見出しを当てにしているため。
NOTE_SUFFIXES = (".md", ".txt")

# カンペが1つも無いときに作るファイル名。
DEFAULT_NOTE_NAME = "カンペ"

# Windowsのファイル名に使えない文字(snippets.INVALID_NAME_CHARS と同じ)。
INVALID_NAME_CHARS = '\\/:*?"<>|'

# 新規作成したときに入れておく雛形。空のファイルを開かせると「何をどう書けば
# ジャンプできるのか」が分からない。# と ## を1組ずつ含めて、開いた瞬間に
# 前後ボタンが効く状態にしておく。
NEW_NOTE_TEMPLATE = """# 1. つかみ

## 話すこと

- ここに書いたことは手元にだけ出ます（共有側には映りません）

# 2. 本題

## 話すこと

- ◀ ▶ で「#」の見出しを行き来できます
- 「≡」で目次が出ます

# 3. まとめ

## 話すこと

- このファイルを書き換えて「⟳」を押すと読み直します
"""

# notes/ が空のときにパネルへ出す案内。カンペのファイルと同じく Markdown として描くので、
# 見出しの大きさもそのまま確かめられる。
EMPTY_GUIDE = """# カンペがありません

## 作り方

「✎」を押すと `notes` フォルダにファイルを作って、
お使いのエディタで開きます。

## 書き方

`# 見出し` で章、`## 見出し` で小見出しになります。
`#` の章は ◀ ▶ でワンクリックで行き来できます。
"""


def _ensure_dir() -> bool:
    """notes/ フォルダを用意する。作れたか(既にあるか)を返す。

    初回起動でも「入れる場所」が見えるように、読むだけのときも作る
    (snippets.load_templates と同じ)。"""
    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as e:
        print(f"[tray-tools] notes フォルダを作成できません: {e}", file=sys.stderr)
        return False


def list_notes() -> list:
    """notes/ 配下のカンペの表示名を、ファイル名順で返す。"""
    if not _ensure_dir():
        return []
    names = []
    try:
        entries = sorted(NOTES_DIR.iterdir(), key=lambda p: p.name)
    except OSError as e:
        print(f"[tray-tools] notes フォルダを読めません: {e}", file=sys.stderr)
        return []
    for path in entries:
        try:
            if not path.is_file() or path.suffix.lower() not in NOTE_SUFFIXES:
                continue
        except OSError:
            continue
        names.append(path.stem)
    return names


def note_path(name: str):
    """表示名から実ファイルを探す。見つからなければ None。

    表示名は拡張子を落としたファイル名なので、対応拡張子を順に当ててみるしかない
    (snippets.template_path と同じ)。"""
    if not name:
        return None
    for suffix in NOTE_SUFFIXES:
        path = NOTES_DIR / f"{name}{suffix}"
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def read_note(name: str):
    """カンペの中身を返す。読めなければ None。

    BOM付きUTF-8で保存されることがあるので utf-8-sig で読む(snippets と同じ)。
    エディタの文字コードが Shift_JIS だったときに黙って空になるのは困るので、
    読めなかったことは呼び元へ返して画面に出させる。"""
    path = note_path(name)
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        print(f"[tray-tools] カンペを読めません ({name}): {e}", file=sys.stderr)
        return None


def resolve_name(wanted: str = "") -> str:
    """設定に書かれた名前を、実在するカンペへ寄せる。無ければ先頭、それも無ければ空。

    カンペのファイルは外部で消されたり名前を変えられたりする。設定に残った名前を
    そのまま信じると「何も出ないパネル」になるので、開くたびに引き直す。"""
    names = list_notes()
    if not names:
        return ""
    if wanted and wanted in names:
        return wanted
    return names[0]


def new_note_path(name: str = ""):
    """新しく作るときの置き場所。名前を省略すると DEFAULT_NOTE_NAME。"""
    name = (name or DEFAULT_NOTE_NAME).strip()
    if not name or any(char in name for char in INVALID_NAME_CHARS):
        name = DEFAULT_NOTE_NAME
    return NOTES_DIR / f"{name}{NOTE_SUFFIXES[0]}"


def ensure_note(name: str = ""):
    """カンペの実ファイルを返す。無ければ雛形を入れて作る。作れなければ None。

    既にあるものは絶対に書き換えない。上書きすると、書き溜めたカンペを名前の
    打ち間違いひとつで消すことになる(snippets.create_template と同じ判断)。"""
    path = note_path(name)
    if path is not None:
        return path
    if not _ensure_dir():
        return None
    path = new_note_path(name)
    if path.exists():
        # 拡張子違いの取りこぼしなど。あるものには触らない。
        return path
    try:
        # newline="" を指定しないと Python が "\n" を "\r\n" に書き換える。雛形は
        # LF で持っているので、指定しないと改行コードが混ざる。
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(NEW_NOTE_TEMPLATE)
    except OSError as e:
        print(f"[tray-tools] カンペを作成できません ({path}): {e}", file=sys.stderr)
        return None
    return path


def _launch(path) -> None:
    """関連付けアプリで開く。外部プログラムを起こすのはここだけ(検証で差し替える)。

    os.startfile は関連付けが無い・パスが消えたなどで例外(OSError)を投げる。呼び元が
    必ず受けること——Qtのスロット内で投げ切ると常駐アプリごと落ちる。"""
    os.startfile(str(path))


def _launch_notepad(path) -> None:
    """関連付けが無かったときの逃げ道。メモ帳は Windows に必ずある。

    os.startfile では「開くプログラム」を指定できないので subprocess で起こす。
    .md に関連付けが無いPCは珍しくない(その場合 os.startfile は
    「このファイルを開く方法を選んでください」ではなく例外になる)。"""
    subprocess.Popen(["notepad.exe", str(path)])


def open_in_editor(name: str = ""):
    """カンペをエディタで開く。無ければ作ってから開く。開いたファイルを返す(失敗は None)。

    「その場で編集はしない」と決めたぶん、書き足す手段はここだけになる。作ってから
    開くまでを1つにしてあるのは、カンペが1つも無い状態からでも押せば書き始められる
    ようにするため(押しても何も起きないボタンを作らない)。"""
    path = ensure_note(name)
    if path is None:
        return None
    try:
        _launch(path)
        return path
    except OSError as e:
        print(f"[tray-tools] カンペを開けません ({path}): {e}", file=sys.stderr)
    try:
        _launch_notepad(path)
        return path
    except (OSError, ValueError) as e:
        print(f"[tray-tools] メモ帳でも開けません ({path}): {e}", file=sys.stderr)
        return None


def open_folder():
    """notes/ フォルダをエクスプローラで開く。開いたパスを返す(失敗は None)。

    カンペが1件も無い状態からでも辿り着けるように、無ければ作ってから開く。"""
    if not _ensure_dir():
        return None
    try:
        _launch(NOTES_DIR)
        return NOTES_DIR
    except OSError as e:
        print(f"[tray-tools] notes フォルダを開けません: {e}", file=sys.stderr)
        return None
