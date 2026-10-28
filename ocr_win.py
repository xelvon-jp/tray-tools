# ocr_win.py
# Windows 内蔵 OCR(Windows.Media.Ocr)で画像から文字を読む。
#
# 使い方の入口は付箋(capture_window)の右クリック「文字を読み取る（OCR）」。
# ShareX の OCR と同じく、認識エンジンは Windows 標準のものを使う
# (別途モデルを持たず、言語は OS に入っている言語パックに従う)。
#
# WinRT の Python 向けパッケージ群は任意の依存(setup.py の --with-ocr)。
# 入っていない環境では available() が False になり、入口自体が出ない
# (スマホへ送る QR と同じ段階開示)。このモジュールは winrt を
# 関数の内側でだけ import するので、無くても import は通る。
import asyncio
import importlib.util
import re

# 認識に優先する言語(下のタグで始まるものを available から探す)。
_PREFERRED = ("ja",)


def available() -> bool:
    """WinRT の OCR 一式が import できるか。入口を出すかの判定に使う。"""
    try:
        return importlib.util.find_spec("winrt.windows.media.ocr") is not None
    except Exception:
        return False


def choose_language(available_tags, preferred=_PREFERRED):
    """使える言語タグの一覧から、認識に使うものを1つ選ぶ。

    優先する言語(日本語)で始まるものがあればそれ、無ければ先頭。
    一覧が空なら None(OCR エンジンを作れない)。"""
    tags = [str(tag) for tag in (available_tags or []) if tag]
    if not tags:
        return None
    for want in preferred:
        for tag in tags:
            if tag.lower().startswith(want.lower()):
                return tag
    return tags[0]


def needs_invert(mean_luminance: float) -> bool:
    """地が暗ければ反転する。Windows OCR は白地に黒文字が前提に近く、
    暗い背景に白文字の画面(ファイラの一覧など)は壊滅的に化ける。"""
    return mean_luminance < 128.0


def ocr_scale(width: int, height: int, max_dim: int = 10000) -> float:
    """OCR に渡す前の拡大率(1.0 はそのまま)。

    小さい文字は広げた方が読めるので、短辺が 1600px 未満なら最大3倍まで
    広げる。逆に、エンジンの上限(OcrEngine.MaxImageDimension = 10000)を
    超える絵は縮める(超えると認識自体が失敗する)。"""
    if width <= 0 or height <= 0:
        return 1.0
    scale = 1.0
    short = min(width, height)
    if short < 1600:
        scale = min(3.0, 1600.0 / short)
    if max(width, height) * scale > max_dim:
        scale = max_dim / max(width, height)
    return scale


# ひらがな・カタカナ・漢字・和文の約物(全角記号を含む)。
_CJK_CHAR = re.compile(r"[぀-ヿ㐀-鿿豈-﫿＀-｠　-〿]")


def _clean_line(line: str) -> str:
    """1行分の文字間空白を落とす(clean_text の中身。行頭の空白は残す)。"""
    chars = []
    for i, ch in enumerate(line):
        if ch in (" ", "　"):
            prev = line[i - 1] if i > 0 else ""
            nxt = line[i + 1] if i + 1 < len(line) else ""
            if (prev and _CJK_CHAR.match(prev)) or (nxt and _CJK_CHAR.match(nxt)):
                continue
        chars.append(ch)
    return "".join(chars)


def clean_text(text: str) -> str:
    """OCR 結果の空白を整える。

    Windows OCR の日本語エンジンは、文字ごとに空白を挟んで返すことが
    ある(「確 か に 酷 い」)。和文の隣にある空白は落として読み形に戻す。
    英単語どうしの区切り(前後とも英字)はそのまま残す。"""
    lines = [_clean_line(line).strip() for line in (text or "").splitlines()]
    return "\n".join(lines).strip()


def layout_text(lines) -> str:
    """単語の位置から、改行とインデントを再現した文字列を組み立てる。

    lines は行ごとの [(文字列, x, y, 幅, 高さ)]。エンジンの返す
    result.text は全文を空白でつないでしまうので、こちらを使う。
    インデントは「その行の先頭の単語の x − 全体の最小 x」を、文字幅の
    中央値で割って空白の個数にする(上限40)。行間が空いていれば空行も入れる。"""
    rows = []
    all_words = [w for line in lines for w in line]
    if not all_words:
        return ""
    widths = sorted(w[3] / max(1, len(w[0])) for w in all_words if w[0])
    char_w = widths[len(widths) // 2] if widths else 0.0
    if char_w <= 0:
        char_w = 1.0
    heights = sorted(max(w[4] for w in line) for line in lines if line)
    line_h = heights[len(heights) // 2] if heights else char_w
    base_x = min(w[1] for w in all_words)
    for line in lines:
        words = sorted((w for w in line if w[0]), key=lambda w: w[1])
        if not words:
            continue
        top = min(w[2] for w in words)
        bottom = max(w[2] + w[4] for w in words)
        indent = round((min(w[1] for w in words) - base_x) / char_w)
        content = _clean_line(" ".join(w[0] for w in words)).strip()
        rows.append((top, bottom, max(0, min(indent, 40)), content))
    rows.sort(key=lambda r: r[0])
    out = []
    prev_bottom = None
    for top, bottom, indent, content in rows:
        if prev_bottom is not None and top - prev_bottom > line_h * 1.2:
            out.append("")
        out.append(" " * indent + content)
        prev_bottom = bottom
    return "\n".join(out).strip()


def recognize_png(png_bytes: bytes) -> str:
    """PNG のバイト列を Windows OCR で読む。読めた文字列を返す。

    WinRT の非同期 API を asyncio で待つ。OCR のワーカースレッドから
    呼ばれる前提(イベントループはここで回す)。失敗は例外で上げる。"""
    if not png_bytes:
        raise ValueError("画像が空です")
    return asyncio.run(_recognize(png_bytes))


async def _recognize(png_bytes: bytes) -> str:
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import (
        DataWriter,
        InMemoryRandomAccessStream,
    )

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png_bytes)
    await writer.store_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    tags = [language.language_tag
            for language in OcrEngine.available_recognizer_languages]
    tag = choose_language(tags)
    if tag is None:
        raise RuntimeError(
            "OCR で使える言語が Windows に入っていません"
            "（設定→時刻と言語→言語 に日本語などを追加してください）")
    engine = OcrEngine.try_create_from_language(Language(tag))
    if engine is None:
        raise RuntimeError(f"OCR エンジンを作れませんでした（言語: {tag}）")
    result = await engine.recognize_async(bitmap)
    # result.text は全文が空白でつながってしまう。単語の位置から
    # 改行とインデントを再現する(layout_text)。
    lines = []
    for line in result.lines:
        words = []
        for word in line.words:
            rect = word.bounding_rect
            words.append((word.text, rect.x, rect.y, rect.width, rect.height))
        lines.append(words)
    return layout_text(lines)
