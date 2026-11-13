# editor/findall.py
# 今開いている文書の中の「すべて検索」の純粋部分(Qt に依存しない)。
#
# 一致は行単位で、ヒットした行を (行番号, 行の内容) で返す。1行に
# 何回ヒットしても、一覧に出るのはその行1件(grep と同じ数え方)。
# 検索語は文字どおりか正規表現か(検索バーの条件と同じ)を選べ、
# コンパイルは rxfind.compile_search に任せる(大小区別の扱い、
# 不正な式の文言は検索・置換とそろう)。行ごとに探すので、正規
# 表現の ^ と $ は行の頭・末尾に効く(エディタの検索の作法)。
#
# 重くならないための決め:
#   - 結果の件数に上限を付け、達したら打ち切って truncated で知らせる
#     (grep.MAX_RESULTS と同じ 1000 件)
#   - 実行は窓側(findall_dialog)が裏のスレッドで回す。ここ自体は
#     同期の純粋関数
from dataclasses import dataclass, field

from editor import rxfind

MAX_RESULTS = 1000   # 一覧に出す上限(達したら打ち切り)


@dataclass
class FindAllMatch:
    line: int      # 1始まりの行番号
    text: str      # その行の内容(改行なし)


@dataclass
class FindAllResult:
    matches: list = field(default_factory=list)
    truncated: bool = False   # 件数の上限で打ち切ったか


def find_all(text, word, *, case_sensitive=False, regex=False,
             max_results=MAX_RESULTS):
    """text の中を word で探し、ヒットした行を FindAllResult で返す。

    不正な正規表現は rxfind.RegexFault(利用者向けの文)になる。
    空の検索語・空の本文では何も返さない。"""
    result = FindAllResult()
    if not word or not text:
        return result
    rx = rxfind.compile_search(word, regex=regex,
                               case_sensitive=case_sensitive)
    for number, line in enumerate(text.split("\n"), start=1):
        if rx.search(line):
            result.matches.append(FindAllMatch(number, line))
            if len(result.matches) >= max_results:
                result.truncated = True
                return result
    return result
