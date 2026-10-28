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
    return result.text or ""
