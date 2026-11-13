# editor/highlighter.py
# 本文側の強調表示をする QSyntaxHighlighter。
#
# 2つの働きがある:
#   - 星印行の強調: 指定パターン(既定では行頭に星印 ★☆ がある行)に
#     背景色を付ける(editor/outline.py の DEFAULT_HIGHLIGHT_PATTERN)。
#     パターンの判定は、各行の先頭 MATCH_HEAD_CHARS 字だけを match
#     する(遅い正規表現への制限はアウトラインと共通)。
#   - Markdown の色付け(任意): .md 等のファイルで、見出し・太字・
#     行内コード・コードブロック・リンク・引用・箇条書きの記号に
#     書式を付ける。落ち着いた配色(VS Code / Typora の実例寄り)で、
#     星印行の背景とは重ねても矛盾しないよう、星印の書式を最後に
#     重ねる(星印行が勝つ)。
#
# どちらも見た目だけの書式で、文書の内容は変えない。ハイライタは
# 編集に自動で追随するので、窓側は設定を差し替えるだけでよい。
import re

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

from editor import outline

STAR_BACKGROUND = QColor(255, 243, 176)

# Markdown と判定する拡張子(ドット無し・小文字)。
MARKDOWN_EXTENSIONS = frozenset({"md", "markdown"})

# Markdown の配色の既定(明るい背景前提の、落ち着いた色)。
# 見出しは階層が深いほど背景を薄くする。正本は editor/colors.py の
# 配色の組で、窓が set_markdown_colors で渡す。ここの定数は、組が
# 渡されなかったときの受けに使う(テストからの参照も残してある)。
HEADING_BACKGROUNDS = (
    QColor(217, 226, 243),   # 見出し1
    QColor(226, 234, 246),   # 見出し2
    QColor(234, 240, 249),   # 見出し3
    QColor(239, 244, 251),   # 見出し4
    QColor(243, 247, 252),   # 見出し5
    QColor(246, 249, 253),   # 見出し6
)
CODE_BLOCK_BACKGROUND = QColor(245, 245, 245)
INLINE_CODE_BACKGROUND = QColor(240, 240, 240)
INLINE_CODE_FOREGROUND = QColor(164, 38, 44)
LINK_COLOR = QColor(5, 99, 193)
QUOTE_COLOR = QColor(90, 125, 90)
BULLET_COLOR = QColor(197, 90, 17)

_HEADING_RX = re.compile(r"^(#{1,6})(?:\s|$)")
_FENCE_RX = re.compile(r"^\s*(```|~~~)")
_BOLD_RX = re.compile(r"(\*\*|__)(.+?)\1")
_CODE_SPAN_RX = re.compile(r"`([^`]+)`")
_LINK_RX = re.compile(r"\[[^\]]*\]\([^)]*\)")
_BULLET_RX = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])(\s+)")

# ブロックの状態(コードブロックの内側かどうか)。
_STATE_NORMAL = 0
_STATE_CODE = 1


def _format(background=None, foreground=None, bold=False, underline=False):
    fmt = QTextCharFormat()
    if background is not None:
        fmt.setBackground(background)
    if foreground is not None:
        fmt.setForeground(foreground)
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if underline:
        fmt.setFontUnderline(True)
    return fmt


class LineHighlighter(QSyntaxHighlighter):
    """パターンに当たった行(ブロック)に背景色を付ける。

    pattern はコンパイル済みの正規表現か None(None なら何もしない)。
    各ブロックの先頭で match するものだけを対象にする。
    Markdown の色付けは set_markdown_enabled で切り替える。"""

    def __init__(self, document, pattern=None):
        super().__init__(document)
        self._pattern = pattern
        self._format = QTextCharFormat()
        self._format.setBackground(STAR_BACKGROUND)
        self._markdown_enabled = False
        self._heading_formats = [
            _format(background=color, bold=True)
            for color in HEADING_BACKGROUNDS]
        self._code_block_format = _format(background=CODE_BLOCK_BACKGROUND)
        self._inline_code_format = _format(
            background=INLINE_CODE_BACKGROUND,
            foreground=INLINE_CODE_FOREGROUND)
        self._bold_format = _format(bold=True)
        self._link_format = _format(foreground=LINK_COLOR, underline=True)
        self._quote_format = _format(foreground=QUOTE_COLOR)
        self._bullet_format = _format(foreground=BULLET_COLOR, bold=True)

    def pattern(self):
        return self._pattern

    def set_pattern(self, pattern):
        self._pattern = pattern
        self.rehighlight()

    def set_background(self, color):
        """強調の背景色を差し替える(配色プリセットの適用)。"""
        self._format.setBackground(QColor(color))
        self.rehighlight()

    def set_markdown_colors(self, scheme):
        """Markdown の書式を配色の組(editor/colors.py)で作り直す。

        組に無い項目は、このモジュールの定数(従来の色)に倒す。
        星印行の背景は set_background 側の担当なので、ここでは触らない。"""
        def pick(key, default):
            value = scheme.get(key) if isinstance(scheme, dict) else None
            return QColor(value) if value else QColor(default)

        self._heading_formats = [
            _format(background=pick(f"heading{level}_background",
                                    HEADING_BACKGROUNDS[level - 1]),
                    bold=True)
            for level in range(1, 7)]
        self._code_block_format = _format(
            background=pick("code_block_background", CODE_BLOCK_BACKGROUND))
        self._inline_code_format = _format(
            background=pick("inline_code_background",
                            INLINE_CODE_BACKGROUND),
            foreground=pick("inline_code_foreground",
                            INLINE_CODE_FOREGROUND))
        self._link_format = _format(
            foreground=pick("link_foreground", LINK_COLOR), underline=True)
        self._quote_format = _format(
            foreground=pick("quote_foreground", QUOTE_COLOR))
        self._bullet_format = _format(
            foreground=pick("bullet_foreground", BULLET_COLOR), bold=True)
        self.rehighlight()

    def markdown_enabled(self) -> bool:
        return self._markdown_enabled

    def set_markdown_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self._markdown_enabled:
            return
        self._markdown_enabled = enabled
        self.rehighlight()

    def highlightBlock(self, text):  # noqa: N802  Qt の上書き
        if self._markdown_enabled:
            self._highlight_markdown(text)
        else:
            self.setCurrentBlockState(_STATE_NORMAL)
        if self._pattern is None:
            return
        if self._pattern.match(text[:outline.MATCH_HEAD_CHARS]):
            self.setFormat(0, len(text), self._format)

    def _highlight_markdown(self, text):
        """Markdown の書式を付ける。コードブロックの内外は
        ブロックの状態で前後の行へ引き継ぐ。"""
        in_code = self.previousBlockState() == _STATE_CODE
        if _FENCE_RX.match(text):
            # 区切りの行(``` 等)自体もコード側の背景にする。
            self.setFormat(0, len(text), self._code_block_format)
            self.setCurrentBlockState(
                _STATE_NORMAL if in_code else _STATE_CODE)
            return
        if in_code:
            self.setFormat(0, len(text), self._code_block_format)
            self.setCurrentBlockState(_STATE_CODE)
            return
        self.setCurrentBlockState(_STATE_NORMAL)
        heading = _HEADING_RX.match(text)
        if heading:
            level = len(heading.group(1))
            self.setFormat(0, len(text), self._heading_formats[level - 1])
            return
        if text.lstrip().startswith(">"):
            self.setFormat(0, len(text), self._quote_format)
            return
        bullet = _BULLET_RX.match(text)
        if bullet:
            start = len(bullet.group(1))
            self.setFormat(start, len(bullet.group(2)),
                           self._bullet_format)
        for match in _BOLD_RX.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(),
                           self._bold_format)
        for match in _CODE_SPAN_RX.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(),
                           self._inline_code_format)
        for match in _LINK_RX.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(),
                           self._link_format)
