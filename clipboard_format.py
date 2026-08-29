# clipboard_format.py
# クリップボードに載っている「書式」(HTML)を、段階を選んで落とす部品。
# トレイアイコンは持たない普通のモジュールで、UIは定型文ピッカー(snippets.py)に相乗りする。
#
# 何を解決するのか
# ----------------
# M365 Copilot の出力を PowerPoint に貼ると改行が消える。原因は改行コードではない。
# コピー元は次の2つをクリップボードへ載せている:
#
#   CF_UNICODETEXT : 'iPhoneと簡易的にデータの'
#   HTML Format    : '<html>\r\n<body>\r\n<!--StartFragment--><p data-pm-slice="1 1 []">…</p>…'
#
# PowerPoint はテキストより HTML Format を優先するため、<p> の段落解釈で改行が潰れる。
# そこで「HTML を捨てる/削る」ことで、貼り先のテーマに従わせる。
#
# CF_HTML の形式
# --------------
# HTML Format(CF_HTML)は先頭にバイトオフセットのヘッダを持つ。
#
#   Version:0.9
#   StartHTML:0000000105     ← <html> が始まるバイト位置
#   EndHTML:0000000259       ← 文書の終端
#   StartFragment:0000000131 ← <!--StartFragment--> の直後
#   EndFragment:0000000164   ← <!--EndFragment--> の直前
#   <html><body><!--StartFragment-->…<!--EndFragment--></body></html>
#
# **数えるのは文字数ではなく UTF-8 のバイト数**。中身を書き換えたらここを再計算しないと、
# 貼り先が壊れた位置を読んで「一部しか貼られない」という分かりにくい不具合になる。
# build_cf_html() / parse_cf_html() がその計算を持つ。
#
# クリップボードの読み書きについて
# --------------------------------
# 実際の読み書きは Qt(QClipboard / QMimeData)に任せ、ctypes で Win32 を直に叩かない。
# Qt の Windows 実装(qwindowsmimeregistry.cpp)は
#   - 読み: CF_HTML の StartHTML〜EndHTML を切り出して text/html として返す
#   - 書き: text/html から CF_HTML を組み立て、4つのオフセットを UTF-8 バイトで再計算する
# を既に行っており、自前で GlobalAlloc / SetClipboardData を触る理由が無い
# (ハンドルの restype 指定漏れでアクセス違反を出した実績があるので、増やさない)。
#
# ただし読み取り側は「ヘッダ付きの生の CF_HTML が来る」経路もありうるので、
# parse_cf_html() で両方受けられるようにしてある。
#
# 検証時の落とし穴(実機の不具合ではない)
# --------------------------------------
# QT_QPA_PLATFORM=offscreen で setMimeData() を使うと、プロセス終了時に
# セグメンテーション違反が出る。offscreen にはクリップボードの実装が無く、
# 基底の QPlatformClipboard が Q_GLOBAL_STATIC に QMimeData を抱えるため、
# Python の終了処理が終わった後の静的デストラクタで delete が走るのが原因
# (faulthandler が何も出さないのがその証拠)。Windows では QWindowsClipboard が
# mimeData()/setMimeData() を両方 override していてこの静的変数を使わないので、
# 常駐アプリ側にこの経路は無い。offscreen で終了コード139が出ても本体とは無関係。
import re
import sys
from html import escape
from html.parser import HTMLParser

from PySide6.QtCore import QMimeData
from PySide6.QtGui import QGuiApplication

# ---------------------------------------------------------------------------
# 段階
# ---------------------------------------------------------------------------
# 軸は「構造は残し、見た目は貼り先のテーマに従わせる」。PowerPoint に貼るときに
# Copilot 側のフォントや色を持ち込みたい場面はほぼ無い。
LEVEL_STRUCTURE = "structure"
LEVEL_CLEAN = "clean"
LEVEL_TEXT = "text"

# (キー, 表示名, 説明) の並び。この順でピッカーに出る。既定(先頭)は「構造だけ残す」。
LEVELS = (
    (
        LEVEL_STRUCTURE,
        "構造だけ残す",
        "段落・改行・箇条書き・強調・見出し・表だけ残す。属性は全部落とす",
    ),
    (
        LEVEL_CLEAN,
        "見た目だけ捨てる",
        "リンクや取り消し線なども残す。色・フォント・サイズの指定だけ削る",
    ),
    (
        LEVEL_TEXT,
        "テキストのみ",
        "HTMLを丸ごと捨てる。いちばん確実",
    ),
)

LEVEL_LABELS = {key: label for key, label, _desc in LEVELS}
LEVEL_KEYS = tuple(key for key, _label, _desc in LEVELS)
DEFAULT_LEVEL = LEVELS[0][0]

# ---------------------------------------------------------------------------
# HTML の加工
# ---------------------------------------------------------------------------
# 中身ごと捨てるタグ。閉じタグまで読み飛ばす(空要素はここに入れない。閉じタグが
# 来ないので読み飛ばしが終わらなくなる)。
_DROP_WITH_CONTENT = frozenset(
    {
        "script",
        "style",
        "head",
        "title",
        "noscript",
        "template",
        "svg",
        "math",
        "object",
        "applet",
        "iframe",
        "frameset",
        "form",
        "select",
        "textarea",
        "button",
    }
)

# 閉じタグを持たない要素。開始タグだけ出して開きっぱなしの一覧には積まない。
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

# 「構造だけ残す」で残すタグ。
#
# 依頼の一覧(p / br / ul ol li / b strong i em / h1-h6)に div と表を足してある。
#   - div … 行を div で組む書き出し元がある。落とすと行が繋がってしまい、
#           まさに直したかった「改行が消える」を自分で作ることになる。
#   - 表  … セルを落とすと全部のセルが1行に繋がる。罫線や色は属性を捨てる時点で
#           消えるので、残るのは構造だけ。
_STRUCTURE_TAGS = frozenset(
    {
        "p",
        "br",
        "div",
        "ul",
        "ol",
        "li",
        "b",
        "strong",
        "i",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
    }
)

# 「見た目だけ捨てる」で追加で残すタグ。
_CLEAN_EXTRA_TAGS = frozenset(
    {
        "a",
        "img",
        "span",
        "hr",
        "blockquote",
        "pre",
        "code",
        "kbd",
        "samp",
        "var",
        "tt",
        "u",
        "s",
        "strike",
        "del",
        "ins",
        "sub",
        "sup",
        "small",
        "mark",
        "abbr",
        "cite",
        "q",
        "dl",
        "dt",
        "dd",
        "figure",
        "figcaption",
        "col",
        "colgroup",
    }
)
_CLEAN_TAGS = _STRUCTURE_TAGS | _CLEAN_EXTRA_TAGS

# 「見た目だけ捨てる」で残す属性。どのタグでも許すものと、タグごとに許すもの。
# class / id / data-* / on* は一律で落ちる(残しても貼り先で意味を持たないうえ、
# data-pm-slice のような書き出し元固有の印が付いて回るため)。
_CLEAN_GLOBAL_ATTRS = frozenset({"style", "title", "lang", "dir"})
_CLEAN_TAG_ATTRS = {
    "a": frozenset({"href", "target", "rel"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "td": frozenset({"colspan", "rowspan", "headers"}),
    "th": frozenset({"colspan", "rowspan", "scope", "headers"}),
    "ol": frozenset({"start", "type", "reversed"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "q": frozenset({"cite"}),
    "blockquote": frozenset({"cite"}),
}

# style 属性から削る宣言。依頼の線引きは「色・フォント・サイズ」。
# font-weight / font-style は残す(太字・斜体は見た目ではなく強調の情報で、
# <b> / <i> を残すのと揃わなくなるため)。
_STYLE_DROP_EXACT = frozenset(
    {
        "color",
        "background",
        "background-color",
        "background-image",
        "background-position",
        "background-repeat",
        "background-size",
        "background-attachment",
        "background-clip",
        "background-origin",
        "font",
        "font-family",
        "font-size",
        "font-size-adjust",
        "font-stretch",
        "line-height",
        "letter-spacing",
        "word-spacing",
        "text-shadow",
        "box-shadow",
        "opacity",
        "filter",
        "border-color",
        "border-top-color",
        "border-right-color",
        "border-bottom-color",
        "border-left-color",
        "outline-color",
        "caret-color",
        "text-decoration-color",
        "column-rule-color",
        "text-fill-color",
        "-webkit-text-fill-color",
        "-webkit-text-stroke",
        "-webkit-text-stroke-color",
        "-webkit-text-stroke-width",
    }
)
# Word / Outlook が撒く mso-* と、ベンダー接頭辞付きのフォント指定、CSS変数。
_STYLE_DROP_PREFIX = ("mso-", "--", "-webkit-font", "-moz-font", "-ms-font")

# href / src に許すスキーム。javascript: と(imgのdata:image以外の)data: は落とす。
_ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto", "tel", "ftp", "ftps", "file", "cid"})
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

START_FRAGMENT = "<!--StartFragment-->"
END_FRAGMENT = "<!--EndFragment-->"


def _is_safe_url(value: str, tag: str) -> bool:
    """href / src に残してよい URL か。スキームが無いもの(相対・#)は許す。"""
    value = (value or "").replace("\x00", "").strip()
    if not value:
        return False
    match = _URL_SCHEME_RE.match(value)
    if match is None:
        return True
    scheme = match.group(0)[:-1].lower()
    if scheme in _ALLOWED_URL_SCHEMES:
        return True
    # 貼り付けた画像は data:image/... で載ることがあるので img だけ通す。
    return scheme == "data" and tag == "img" and value[:11].lower() == "data:image/"


def _filter_style(style: str) -> str:
    """style 属性から色・フォント・サイズの宣言だけを削る。残りはそのまま。"""
    kept = []
    for declaration in (style or "").split(";"):
        declaration = declaration.strip()
        if not declaration or ":" not in declaration:
            continue
        prop = declaration.split(":", 1)[0].strip().lower()
        if not prop or prop in _STYLE_DROP_EXACT or prop.startswith(_STYLE_DROP_PREFIX):
            continue
        kept.append(declaration)
    return "; ".join(kept)


class _HtmlCleaner(HTMLParser):
    """許したタグ以外を「中身は残してタグだけ外す」HTMLパーサ。

    html.parser は壊れたHTMLでも例外を投げずに読み進む。閉じタグの対応が取れない
    場合も出力側の入れ子だけは self._open で必ず閉じるので、貼り先に半端な
    入れ子を渡さない。"""

    def __init__(self, keep_tags, keep_attrs: bool):
        # convert_charrefs=True なので handle_data には実体参照を解いた文字列が来る。
        # 出力するときに escape() で戻す(&amp; の二重エスケープは起きない)。
        super().__init__(convert_charrefs=True)
        self._keep_tags = keep_tags
        self._keep_attrs = keep_attrs
        self._out = []
        self._open = []  # 出力済みでまだ閉じていないタグ
        self._dropping = []  # 中身ごと捨てている最中のタグ

    # --- 出力 ---
    def result(self) -> str:
        parts = list(self._out)
        for tag in reversed(self._open):
            parts.append(f"</{tag}>")
        return "".join(parts)

    # --- タグ ---
    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if tag in _DROP_WITH_CONTENT:
            self._dropping.append(tag)
            return
        if self._dropping:
            return
        if tag not in self._keep_tags:
            return  # タグだけ外して中身は残す
        rendered = self._render_attrs(tag, attrs) if self._keep_attrs else ""
        if tag == "img" and " src=" not in rendered:
            # src を落とした(javascript: や data:text/html だった)画像。残すと
            # 貼り先に「壊れた画像」の枠だけが出るので、タグごと捨てる。
            return
        self._out.append(f"<{tag}{rendered}>")
        if tag not in _VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag, attrs):
        # <br/> のような自己完結タグ。空要素でなければ開いてすぐ閉じる。
        self.handle_starttag(tag, attrs)
        if (tag or "").lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if self._dropping:
            if self._dropping[-1] == tag:
                self._dropping.pop()
            return
        if tag in _VOID_TAGS or tag not in self._keep_tags:
            return
        if tag not in self._open:
            return  # 対応する開始タグが無い。壊れたHTMLでも出力は壊さない
        while self._open:
            open_tag = self._open.pop()
            self._out.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    # --- 中身 ---
    def handle_data(self, data):
        if self._dropping or not data:
            return
        self._out.append(escape(data, quote=False))

    def handle_comment(self, data):
        # StartFragment/EndFragment を含め、コメントは全部捨てる。目印は
        # build_cf_html() 側で改めて付け直す。
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    # --- 属性 ---
    def _render_attrs(self, tag: str, attrs) -> str:
        allowed = _CLEAN_GLOBAL_ATTRS | _CLEAN_TAG_ATTRS.get(tag, frozenset())
        parts = []
        for name, value in attrs or ():
            name = (name or "").lower()
            if name not in allowed:
                continue
            if name == "style":
                value = _filter_style(value or "")
                if not value:
                    continue
            elif name in ("href", "src"):
                if not _is_safe_url(value or "", tag):
                    continue
            if value is None:
                parts.append(f" {name}")
            else:
                parts.append(f' {name}="{escape(value, quote=True)}"')
        return "".join(parts)


# テキスト化で行の区切りとして扱うタグ。
_TEXT_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "table",
        "blockquote",
        "pre",
        "dt",
        "dd",
        "section",
        "article",
        "header",
        "footer",
        "figure",
        "figcaption",
        "caption",
    }
)


class _TextExtractor(HTMLParser):
    """HTMLから素のテキストを起こす。CF_UNICODETEXT が無いときの保険。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out = []
        self._dropping = []

    def result(self) -> str:
        text = "".join(self._out)
        lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        text = "\n".join(lines)
        # 空行が3つ以上続いても意味が増えないので2つまでに詰める。
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text.strip()

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if tag in _DROP_WITH_CONTENT:
            self._dropping.append(tag)
            return
        if self._dropping:
            return
        if tag == "br":
            self._out.append("\n")
        elif tag in _TEXT_BLOCK_TAGS:
            self._out.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if self._dropping:
            if self._dropping[-1] == tag:
                self._dropping.pop()
            return
        if tag in ("td", "th"):
            self._out.append("\t")
        elif tag in _TEXT_BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data):
        if self._dropping or not data:
            return
        self._out.append(data)


def sanitize_fragment(html_text: str, level: str) -> str:
    """フラグメント(またはHTML文書)から、その段階で残すHTMLだけを取り出す。

    html.parser は壊れたHTMLで例外を投げない作りだが、想定外の入力で落ちても
    ピッカーごと道連れにしないよう受けておく(失敗したらテキストとして扱う)。"""
    if level == LEVEL_STRUCTURE:
        cleaner = _HtmlCleaner(_STRUCTURE_TAGS, keep_attrs=False)
    elif level == LEVEL_CLEAN:
        cleaner = _HtmlCleaner(_CLEAN_TAGS, keep_attrs=True)
    else:
        raise ValueError(f"未知の段階です: {level}")
    try:
        cleaner.feed(html_text or "")
        cleaner.close()
        return cleaner.result()
    except Exception as e:
        print(f"[tray-tools] HTMLを整形できません: {e}", file=sys.stderr)
        return escape(html_to_text(html_text), quote=False)


def html_to_text(html_text: str) -> str:
    """HTMLから素のテキストを起こす。失敗したらタグを雑に外した文字列を返す。"""
    extractor = _TextExtractor()
    try:
        extractor.feed(html_text or "")
        extractor.close()
        return extractor.result()
    except Exception as e:
        print(f"[tray-tools] HTMLをテキストにできません: {e}", file=sys.stderr)
        return re.sub(r"<[^>]*>", "", html_text or "").strip()


def wrap_document(fragment: str) -> str:
    """フラグメントを CF_HTML が期待する形の文書に包む。

    <html><body> で包み、貼り先が読む位置の目印(StartFragment/EndFragment)を付ける。
    既に目印が入っているものは触らない。"""
    if START_FRAGMENT in (fragment or "") and END_FRAGMENT in (fragment or ""):
        return fragment
    return f"<html><body>{START_FRAGMENT}{fragment or ''}{END_FRAGMENT}</body></html>"


# ---------------------------------------------------------------------------
# CF_HTML (HTML Format) のヘッダ
# ---------------------------------------------------------------------------
# 数字は必ず10桁ゼロ詰め。桁数が変わるとヘッダ長まで変わってオフセットが自分自身に
# 依存してしまうため、固定長にしてヘッダ長を定数にできるようにしている。
_CF_HTML_HEADER = (
    "Version:0.9\r\n"
    "StartHTML:{start_html:010d}\r\n"
    "EndHTML:{end_html:010d}\r\n"
    "StartFragment:{start_fragment:010d}\r\n"
    "EndFragment:{end_fragment:010d}\r\n"
)
# ヘッダは常にこの長さ(105バイト)。文書はこの直後から始まる。
CF_HTML_HEADER_SIZE = len(
    _CF_HTML_HEADER.format(start_html=0, end_html=0, start_fragment=0, end_fragment=0).encode("ascii")
)

_CF_HTML_NUMBER_RE = re.compile(rb"(?im)^(StartHTML|EndHTML|StartFragment|EndFragment)\s*:\s*(-?\d+)")


def build_cf_html(document: str) -> bytes:
    """HTML文書から CF_HTML のバイト列を組み立てる。オフセットは UTF-8 のバイト数。

    文字数で数えると、日本語が入った途端に全部ずれる(「あ」は1文字だがUTF-8で3バイト)。
    ずれた CF_HTML を渡すと、貼り先は文書の途中から読み始めて「一部しか貼られない」
    という分かりにくい壊れ方をする。"""
    document = wrap_document(document)
    body = document.encode("utf-8")
    start_mark = START_FRAGMENT.encode("ascii")
    end_mark = END_FRAGMENT.encode("ascii")

    start_html = CF_HTML_HEADER_SIZE
    start_fragment = start_html + body.index(start_mark) + len(start_mark)
    end_fragment = start_html + body.index(end_mark)
    end_html = start_html + len(body)

    header = _CF_HTML_HEADER.format(
        start_html=start_html,
        end_html=end_html,
        start_fragment=start_fragment,
        end_fragment=end_fragment,
    )
    return header.encode("ascii") + body


def parse_cf_html(payload):
    """CF_HTML(bytes/str)から (文書, フラグメント, オフセットの辞書) を取り出す。

    ヘッダが無い(=Qtが既に切り出した)文字列を渡してもそのまま扱えるようにしてある。
    オフセットが壊れていたら <!--StartFragment--> の目印で拾い直す。目印も無ければ
    文書全体をフラグメントとみなす(貼り元が横着なだけで、中身は使えるため)。"""
    if isinstance(payload, str):
        data = payload.encode("utf-8", "replace")
    elif isinstance(payload, (bytes, bytearray, memoryview)):
        data = bytes(payload)
    else:
        data = str(payload).encode("utf-8", "replace")

    offsets = {}
    header_end = data.find(b"<")
    if header_end > 0:
        for match in _CF_HTML_NUMBER_RE.finditer(data[:header_end]):
            try:
                offsets[match.group(1).decode("ascii")] = int(match.group(2))
            except ValueError:
                pass

    start = offsets.get("StartHTML", -1)
    end = offsets.get("EndHTML", -1)
    # 数字が読めても実体と食い違っていることがある(手書きのヘッダ・途中で中身だけ
    # 差し替えられたもの)。切り出した先頭が '<' でなければ信用せず、最初の '<' に戻す。
    if 0 <= start < end <= len(data) and data[start : start + 1] == b"<":
        document_bytes = data[start:end]
    elif header_end > 0:
        document_bytes = data[header_end:]
    else:
        document_bytes = data

    document = document_bytes.decode("utf-8", "replace")

    # フラグメントは目印(<!--StartFragment-->)を先に探す。目印は中身と一緒に動くので
    # ずれようが無いのに対し、ヘッダの数字は書き換え漏れでずれる。目印が無い書き出し元
    # のためにヘッダの数字も見るが、順番はこちらが後。
    fragment = None
    head = document.find(START_FRAGMENT)
    tail = document.rfind(END_FRAGMENT)
    if head >= 0 and tail > head:
        fragment = document[head + len(START_FRAGMENT) : tail]
    else:
        frag_start = offsets.get("StartFragment", -1)
        frag_end = offsets.get("EndFragment", -1)
        if 0 <= frag_start < frag_end <= len(data):
            fragment = data[frag_start:frag_end].decode("utf-8", "replace")
        else:
            fragment = document
    return document, fragment, offsets


def transform_cf_html(payload, level: str) -> bytes:
    """生の CF_HTML を受け取り、整形した CF_HTML を返す(オフセットは再計算済み)。

    Windows では Qt が CF_HTML の組み立てを持っているので常用の経路ではないが、
    「ヘッダ付きのまま受け取って、ヘッダ付きのまま返す」形をここに閉じ込めておく。"""
    _document, fragment, _offsets = parse_cf_html(payload)
    return build_cf_html(wrap_document(sanitize_fragment(fragment, level)))


# ---------------------------------------------------------------------------
# クリップボード
# ---------------------------------------------------------------------------
# Qt が Windows の生フォーマットを素通しするときの名前。Windows + Qt では
# CF_HTML は text/html として扱われるためここには来ないが、来た場合に備えて先に見る
# (来ればヘッダ込みで取れるので、こちらの方が情報が多い)。
RAW_CF_HTML_MIME = 'application/x-qt-windows-mime;value="HTML Format"'


def _clipboard(clipboard=None):
    if clipboard is not None:
        return clipboard
    return QGuiApplication.clipboard()


def read_clipboard(clipboard=None):
    """クリップボードから (文書, フラグメント, テキスト) を読む。

    HTMLが載っていなければ文書とフラグメントは None。読むだけで中身は変えない。"""
    try:
        board = _clipboard(clipboard)
        if board is None:
            return None, None, ""
        mime = board.mimeData()
        if mime is None:
            return None, None, ""

        text = mime.text() or ""

        payload = None
        if mime.hasFormat(RAW_CF_HTML_MIME):
            raw = mime.data(RAW_CF_HTML_MIME)
            payload = bytes(raw.data()) if hasattr(raw, "data") else bytes(raw)
        elif mime.hasHtml():
            html_text = mime.html() or ""
            if html_text.strip():
                payload = html_text
        if not payload:
            return None, None, text

        document, fragment, _offsets = parse_cf_html(payload)
        if not (fragment or "").strip():
            return None, None, text
        return document, fragment, text
    except Exception as e:
        # クリップボードは他アプリが掴んでいると読めないことがある。Qtのスロットから
        # 呼ばれるので、ここで止めないと常駐ごと落ちる。
        print(f"[tray-tools] クリップボードを読めません: {e}", file=sys.stderr)
        return None, None, ""


def clipboard_has_html(clipboard=None) -> bool:
    """クリップボードに HTML Format が載っているか。ピッカーへ項目を出す条件。"""
    _document, fragment, _text = read_clipboard(clipboard)
    return fragment is not None


def transform(fragment: str, text: str, level: str):
    """(新しい文書 or None, 新しいテキスト) を返す。文書が None なら HTML を載せない。"""
    if level == LEVEL_TEXT:
        plain = text if (text or "").strip() else html_to_text(fragment)
        return None, plain
    document = wrap_document(sanitize_fragment(fragment, level))
    # テキスト側は元の CF_UNICODETEXT をそのまま使う。コピー元が用意した平文が
    # 一番素直で、HTMLから起こし直すと箇条書きの記号などが余計に付く。
    plain = text if (text or "").strip() else html_to_text(document)
    return document, plain


def apply_level(level: str, clipboard=None):
    """クリップボードの中身をその段階で整形して置き換える。(成否, 通知文) を返す。

    貼り付けは行わない(キー送信はしない)。整形した結果を載せるところまで。"""
    try:
        board = _clipboard(clipboard)
        if board is None:
            return False, "クリップボードを扱えません"
        document, fragment, text = read_clipboard(board)
        if fragment is None:
            return False, "HTMLが載っていません"

        new_document, new_text = transform(fragment, text, level)

        mime = QMimeData()
        mime.setText(new_text or "")
        if new_document is not None:
            # Qt が text/html から CF_HTML を組み立て、4つのオフセットを
            # UTF-8 バイトで数え直して載せる。
            mime.setHtml(new_document)
        board.setMimeData(mime)

        label = LEVEL_LABELS.get(level, level)
        if new_document is None:
            return True, f"{label}\nHTMLを捨てました"
        size = len(build_cf_html(new_document))
        return True, f"{label}\nHTMLを整形しました（{size} バイト）"
    except Exception as e:
        print(f"[tray-tools] クリップボードを整形できません: {e}", file=sys.stderr)
        return False, f"整形できませんでした\n{e}"


# プレビューで長い1行を読ませないための改行位置。表示専用で、クリップボードへ
# 載せるHTMLには手を入れない(ピッカーのプレビューは折り返さない作りのため)。
_PREVIEW_BREAK_RE = re.compile(r"(?=<(?:/?(?:p|div|li|ul|ol|tr|table|h[1-6]|blockquote|br)\b))", re.IGNORECASE)


def _pretty_for_preview(html_text: str) -> str:
    return _PREVIEW_BREAK_RE.sub("\n", html_text or "").strip("\n")


def preview_text(level: str, clipboard=None) -> str:
    """ピッカーのプレビューに出す文字列。選ぶ前に「何に置き換わるか」を見せる。"""
    try:
        _document, fragment, text = read_clipboard(clipboard)
        if fragment is None:
            return "クリップボードにHTMLがありません。"

        label = LEVEL_LABELS.get(level, level)
        description = next((desc for key, _l, desc in LEVELS if key == level), "")
        new_document, new_text = transform(fragment, text, level)

        lines = [f"■ {label} … {description}", "", "■ 貼り付く文字", new_text or "(空)"]
        if new_document is None:
            lines += ["", "■ HTML", "載せません（テキストだけになります）"]
        else:
            payload = build_cf_html(new_document)
            _doc, _frag, offsets = parse_cf_html(payload)
            lines += [
                "",
                f"■ 残るHTML（CF_HTML {len(payload)} バイト / "
                f"StartFragment:{offsets.get('StartFragment')} "
                f"EndFragment:{offsets.get('EndFragment')}）",
                _pretty_for_preview(new_document),
            ]
        return "\n".join(lines)
    except Exception as e:
        print(f"[tray-tools] 整形のプレビューを作れません: {e}", file=sys.stderr)
        return f"(プレビューを作れませんでした: {e})"
