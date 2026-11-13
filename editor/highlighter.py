# editor/highlighter.py
# 指定パターンに当たる行を本文側で強調表示する QSyntaxHighlighter。
#
# 既定では、行頭(空白の後でもよい)に星印(★☆)がある行が当たる
# (editor/outline.py の DEFAULT_HIGHLIGHT_PATTERN)。ハイライタは
# 編集に自動で追随するので、窓側はパターンを差し替えるだけでよい。
# 背景色は文字の範囲にだけ付く(行の余白までは伸びない)が、選択や
# カーソルの表示とは競合しない。
#
# 遅い正規表現への制限はアウトラインと共通で、各行は先頭
# MATCH_HEAD_CHARS 字だけを match する(outline 側のコメント参照)。
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat

from editor import outline

STAR_BACKGROUND = QColor(255, 243, 176)


class LineHighlighter(QSyntaxHighlighter):
    """パターンに当たった行(ブロック)に背景色を付ける。

    pattern はコンパイル済みの正規表現か None(None なら何もしない)。
    各ブロックの先頭で match するものだけを対象にする。"""

    def __init__(self, document, pattern=None):
        super().__init__(document)
        self._pattern = pattern
        self._format = QTextCharFormat()
        self._format.setBackground(STAR_BACKGROUND)

    def pattern(self):
        return self._pattern

    def set_pattern(self, pattern):
        self._pattern = pattern
        self.rehighlight()

    def highlightBlock(self, text):  # noqa: N802  Qt の上書き
        if self._pattern is None:
            return
        if self._pattern.match(text[:outline.MATCH_HEAD_CHARS]):
            self.setFormat(0, len(text), self._format)
