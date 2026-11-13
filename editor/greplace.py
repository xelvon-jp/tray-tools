# editor/greplace.py
# フォルダ内の横断置換(grep して置換)の純粋部分(Qt に依存しない)。
#
# 流れは2段で、どちらも「まず確かめてから書く」安全側の決め:
#   1. preview_replace … 対象ファイルと置換件数を数えるだけ(書かない)。
#      置換後の本文がそのファイルの文字コードで書けるかまで確かめ、
#      書けないファイルは対象から外して理由ごと返す
#   2. execute_replace … プレビューで数えたファイルだけを書き換える。
#      書き込む前にディスクの印(更新時刻・大きさ)がプレビュー時点と
#      同じか確かめ、変わっていたファイルは飛ばす(プレビュー後に誰か
#      が触ったものを、古い計算で上書きしないため)
#
# 対象ファイルの歩き方(拡張子・大きさ・歩かないフォルダ)は grep と
# 共用(editor/grep.py)。読み書きは文書モデル(editor/document.py)に
# 任せるので、文字コードと改行は保たれ、原子的な書き込みも手動保存
# と同じになる。改行は、混ざったファイルでも置換していない行の
# 種別を変えない(対話の保存の「混在のまま」と同じ扱い。
# document.replaced_line_newlines で置換後の種別の並びを作る)。
# .bak は手動保存と違い、連番で逃がして上書きしない(同じファイル
# への2回目の置換でも、最初の控えが残る形)。
#
# 対象から外すもの(理由は結果に分けて返す):
#   - EDDy で開いているファイル(呼び側が exclude_paths で渡す。
#     未保存の控えがあるタブと二重に書き換えないため)
#   - 拡張子の無いファイル(既定。Makefile や LICENSE のような
#     文章ファイルもあるが、実行形式まで入ってしまうため。
#     プレビュー側の除外の扱いに揃えて件数と理由を返す)
#   - リンクのファイル(シンボリックリンク／ハードリンク。書き込み
#     が os.replace による差し替えなので、リンク自体が通常ファイル
#     に置き換わってしまう(シンボリックリンクは消え、ハードリンク
#     は切れる)ため。件数を数えて外す)
#   - 読めなかったファイル
#   - バイナリらしいファイル(読み替えが発生したもの、本文に
#     U+0000 を含むもの)
#   - 置換後の本文に、そのファイルの文字コードで書けない文字が
#     あるファイル(黙って化けさせない)
#
# 検索と置換の実体は rxfind(コンパイルと replace_all_text)に任せる
# ので、条件(文字どおり／正規表現、大小区別)と置換文字列の
# グループ参照の扱いは、検索バーの置換とそろう。
import os
from dataclasses import dataclass, field

from editor import document as docmod
from editor import grep as grepmod
from editor import rxfind

# プレビューに載せる対象ファイルの上限(達したら打ち切って
# truncated で知らせる。grep の件数上限と同じ考え方)。
MAX_PREVIEW_FILES = 500


def normalize_path(path):
    """開いているファイルとの照合用の正規化(窓の _same_path と同じ)。"""
    return os.path.normcase(os.path.abspath(path))


@dataclass
class ReplaceTarget:
    path: str
    count: int        # 置換の件数(マッチした回数)
    first_line: int   # 最初のヒットの行(1始まり)
    mtime: float      # プレビュー時点のディスクの印(更新時刻)
    size: int         # プレビュー時点のディスクの印(大きさ)


@dataclass
class ReplacePreview:
    targets: list = field(default_factory=list)   # ReplaceTarget の並び
    files_scanned: int = 0          # 歩いて確かめたファイルの数
    total_count: int = 0            # 置換の合計件数
    skipped_open: list = field(default_factory=list)        # パス
    skipped_no_extension: list = field(default_factory=list)  # パス
    skipped_link: list = field(default_factory=list)        # パス
    skipped_unreadable: list = field(default_factory=list)  # パス
    skipped_binary: list = field(default_factory=list)      # パス
    skipped_unencodable: list = field(default_factory=list)  # (パス, 書けない文字)
    truncated: bool = False         # 対象ファイルの上限で打ち切ったか


# 実行結果の状態(窓の表示とテストが使う)。
STATUS_DONE = "done"                # 置換して書いた
STATUS_UNCHANGED = "unchanged"      # 件数はあったが内容は変わらなかった
STATUS_CHANGED = "changed"          # プレビュー後に変わっていた
STATUS_OPEN = "open"                # 実行時点で EDDy で開かれていた
STATUS_UNREADABLE = "unreadable"    # 読めなかった
STATUS_BINARY = "binary"            # バイナリらしい
STATUS_LINK = "link"                # リンクのファイルのため飛ばした
STATUS_UNENCODABLE = "unencodable"  # 書けない文字がある
STATUS_FAILED = "failed"            # 書き込み等で失敗した


@dataclass
class ReplaceOutcome:
    path: str
    status: str
    count: int = 0      # 実際に置換した件数(done / unchanged のとき)
    detail: str = ""    # 飛ばした・失敗した理由(利用者向けの文)


@dataclass
class ReplaceRunResult:
    outcomes: list = field(default_factory=list)   # ReplaceOutcome の並び

    @property
    def files_written(self) -> int:
        return sum(1 for o in self.outcomes if o.status == STATUS_DONE)

    @property
    def total_replaced(self) -> int:
        return sum(o.count for o in self.outcomes
                   if o.status in (STATUS_DONE, STATUS_UNCHANGED))

    def by_status(self, status) -> list:
        return [o for o in self.outcomes if o.status == status]


def _replace_all_collecting(text, rx, repl, regex):
    """rxfind.replace_all_text と同じ置換をして、置換の並びも返す。

    戻り値は (新しい本文, 件数, [(開始, 終了, 置き換えた文字列), …])
    で、位置は置換前の本文でのもの。置換後の行ごとの改行種別を
    組み立てる(document.replaced_line_newlines)ために集める。"""
    spans = []

    def _sub(match):
        expanded = rxfind.expand_replacement(match, repl, regex=regex)
        spans.append((match.start(), match.end(), expanded))
        return expanded

    new_text = rx.sub(_sub, text)
    return new_text, len(spans), spans


def _is_binary_like(doc) -> bool:
    """バイナリらしい文書か(読み替えが起きた、または本文に U+0000)。

    U+0000 の判定はデコード後の本文で見るので、UTF-16 のファイル
    (バイト列には 0 が並ぶが本文には現れない)を誤って外さない。"""
    return doc.lossy_decode or "\x00" in doc.text


def _is_link(path) -> bool:
    """リンクのファイルか(シンボリックリンク、またはハードリンク
    で他にも同じ中身の名前があるもの)。

    書き込みは os.replace による差し替えなので、リンク自体が通常
    ファイルに置き換わってしまう(シンボリックリンクは消え、ハード
    リンクは切れて別の中身になる)。読まずに確かめられる。消えて
    いたら False(読み込みの段で読めないものとして扱う)。"""
    try:
        return os.path.islink(path) or os.stat(path).st_nlink > 1
    except OSError:
        return False


def preview_replace(root, word, repl, *, case_sensitive=False, regex=False,
                    exclude_paths=(), extensions=None,
                    max_bytes=grepmod.MAX_FILE_BYTES,
                    max_files=MAX_PREVIEW_FILES, progress=None,
                    exclude_no_extension=True):
    """root の下を word で探し、置換の対象と件数を ReplacePreview で返す。

    書くことは一切しない。不正な正規表現・置換文字列は
    rxfind.RegexFault(利用者向けの文)になる。progress は
    (調べたファイル数, パス) を受ける関数で、grep と同じ間隔で
    呼ばれる(例外を上げても走査は止めない)。拡張子の無いファイルは
    既定で対象から外し、skipped_no_extension に入れて返す
    (exclude_no_extension=False で含める)。"""
    preview = ReplacePreview()
    if not word or not root or not os.path.isdir(root):
        return preview
    rx = rxfind.compile_search(word, regex=regex,
                               case_sensitive=case_sensitive)
    excluded = {normalize_path(p) for p in exclude_paths if p}
    for path in grepmod.iter_target_files(root, extensions, max_bytes):
        preview.files_scanned += 1
        if (progress is not None
                and preview.files_scanned % grepmod.PROGRESS_EVERY == 1):
            try:
                progress(preview.files_scanned, path)
            except Exception:  # noqa: BLE001  進捗の失敗で走査を止めない
                pass
        if normalize_path(path) in excluded:
            preview.skipped_open.append(path)
            continue
        if _is_link(path):
            # リンクは置換の書き込みでリンク自体が置き換わって
            # しまうので、読む前に対象から外す(件数と理由を残す)。
            preview.skipped_link.append(path)
            continue
        if (exclude_no_extension
                and not os.path.splitext(path)[1]):
            # 拡張子の無いファイルは、文章ファイルも実行形式も
            # 混ざっているので既定で対象から外す(読む前に外し、
            # 他の除外と同じく件数と理由を残す)。
            preview.skipped_no_extension.append(path)
            continue
        try:
            doc = docmod.load_document(path)
        except OSError:
            preview.skipped_unreadable.append(path)
            continue
        if _is_binary_like(doc):
            preview.skipped_binary.append(path)
            continue
        new_text, count = rxfind.replace_all_text(
            doc.text, rx, repl, regex=regex)
        if count == 0:
            continue
        bad = docmod.find_unencodable(new_text, doc.encoding)
        if bad:
            preview.skipped_unencodable.append((path, "".join(bad)))
            continue
        match = rx.search(doc.text)
        first_line = (doc.text.count("\n", 0, match.start()) + 1
                      if match is not None else 1)
        preview.targets.append(ReplaceTarget(
            path=path, count=count, first_line=first_line,
            mtime=doc.disk_mtime, size=doc.disk_size))
        preview.total_count += count
        if len(preview.targets) >= max_files:
            preview.truncated = True
            return preview
    return preview


def execute_replace(preview, word, repl, *, case_sensitive=False,
                    regex=False, exclude_paths=(), progress=None):
    """preview の対象だけを置換し、ReplaceRunResult で結果を返す。

    1ファイルごとに、実行の直前に開いているかと、ディスクの印が
    プレビュー時点と同じかを確かめる。どちらかで外れたファイルは
    書かずに理由を結果へ残す。書き込みは docmod.save_document
    (.bak と原子的な置き換えはそちらが担う)。
    progress は (済んだファイル数, パス) を受ける関数。"""
    rx = rxfind.compile_search(word, regex=regex,
                               case_sensitive=case_sensitive)
    excluded = {normalize_path(p) for p in exclude_paths if p}
    result = ReplaceRunResult()
    done = 0
    for target in preview.targets:
        done += 1
        if progress is not None:
            try:
                progress(done, target.path)
            except Exception:  # noqa: BLE001  進捗の失敗で実行を止めない
                pass
        path = target.path
        if normalize_path(path) in excluded:
            result.outcomes.append(ReplaceOutcome(
                path, STATUS_OPEN,
                detail="EDDy で開いているため対象外にしました"))
            continue
        if _is_link(path):
            # プレビューの後にリンクになったものも、ここで止める
            # (プレビューだけの判定では、間の変化を拾えないため)。
            result.outcomes.append(ReplaceOutcome(
                path, STATUS_LINK,
                detail="リンクのファイルのため飛ばしました"))
            continue
        try:
            doc = docmod.load_document(path)
        except OSError as e:
            result.outcomes.append(ReplaceOutcome(
                path, STATUS_UNREADABLE, detail=f"読めませんでした（{e}）"))
            continue
        if (doc.disk_mtime, doc.disk_size) != (target.mtime, target.size):
            result.outcomes.append(ReplaceOutcome(
                path, STATUS_CHANGED,
                detail="プレビューの後に変わっていたため飛ばしました"))
            continue
        if _is_binary_like(doc):
            result.outcomes.append(ReplaceOutcome(
                path, STATUS_BINARY,
                detail="バイナリらしいため飛ばしました"))
            continue
        try:
            new_text, count, spans = _replace_all_collecting(
                doc.text, rx, repl, regex)
        except rxfind.RegexFault as e:
            result.outcomes.append(ReplaceOutcome(
                path, STATUS_FAILED, detail=str(e)))
            continue
        if count == 0:
            result.outcomes.append(ReplaceOutcome(
                path, STATUS_CHANGED,
                detail="置換する箇所が無くなっていました"))
            continue
        bad = docmod.find_unencodable(new_text, doc.encoding)
        if bad:
            result.outcomes.append(ReplaceOutcome(
                path, STATUS_UNENCODABLE,
                detail=("その文字コードで書けない文字があります"
                        f"（{''.join(bad)}）")))
            continue
        if new_text == doc.text:
            result.outcomes.append(ReplaceOutcome(
                path, STATUS_UNCHANGED, count=count,
                detail="内容は変わりませんでした"))
            continue
        # 置換していない行の改行を変えないため、置換後の行ごとの
        # 改行種別を作って渡す(渡さないと文書の改行設定で全体が
        # そろってしまう)。控えは連番で逃がし、最初の .bak を守る。
        new_kinds = docmod.replaced_line_newlines(
            doc.text, doc.line_newlines, spans, doc.newline)
        doc.text = new_text
        try:
            docmod.save_document(doc, line_newlines=new_kinds,
                                 numbered_backup=True)
        except (OSError, UnicodeEncodeError) as e:
            result.outcomes.append(ReplaceOutcome(
                path, STATUS_FAILED, detail=f"書けませんでした（{e}）"))
            continue
        result.outcomes.append(ReplaceOutcome(path, STATUS_DONE, count=count))
    return result
