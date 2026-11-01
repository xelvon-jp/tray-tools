# tests/test_photometa.py
# 写真の★の共通パッケージ photometa（xmp / config / filter_expr / writer / export）を確かめる。
#
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe tests\test_photometa.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_slideshow.py と同じ形)。Qt は使わない。
#
# 触るのは tempfile で作った一時フォルダだけ。共通設定と変更記録の置き場所は、
# モジュールの定数(config.CONFIG_PATH / writer.JOURNAL_PATH)を一時フォルダへ差し替える。
# 環境変数 LOCALAPPDATA は差し替えないこと(この PC の python は pymanager 経由で、
# 差し替えると別の Python を勝手に入れにいく)。
#
# writer(ExifTool で実際に書く)のテストは、ExifTool が見つかるときだけ回す:
#   環境変数 PHOTOMETA_EXIFTOOL に exiftool の場所 → PATH 上の exiftool の順に探す。
#   見つからなければ skip と表示して成功扱い(CI のランナーには ExifTool が無い)。
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_TEST_TEMP = tempfile.mkdtemp(prefix="photometa_test_tmp_")

from photometa import config as pc  # noqa: E402
from photometa import filter_expr as fe  # noqa: E402
from photometa import metacache as mc  # noqa: E402
from photometa import writer as pw  # noqa: E402
from photometa import xmp as px  # noqa: E402

# 本物の %LOCALAPPDATA%\traytools を汚さない
pc.CONFIG_PATH = os.path.join(_TEST_TEMP, "data", "photo_ratings.json")
pw.JOURNAL_PATH = os.path.join(_TEST_TEMP, "data", "photometa_journal.jsonl")

PhotoMeta = px.PhotoMeta


def _tmpdir(name):
    d = Path(_TEST_TEMP) / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


# ------------------------------------------------------------------ XMP パケットと画像を作る

def _packet(rating=None, keywords=(), element=False, extra=""):
    attrs, body = "", ""
    if rating is not None:
        if element:
            body += f"<xmp:Rating>{rating}</xmp:Rating>"
        else:
            attrs = f' xmp:Rating="{rating}"'
    if keywords:
        lis = "".join(f"<rdf:li>{k}</rdf:li>" for k in keywords)
        body += f"<dc:subject><rdf:Bag>{lis}</rdf:Bag></dc:subject>"
    return (
        "<?xpacket begin='\ufeff' id='W5M0MpCehiHzreSzNTczkc9d'?>"
        "<x:xmpmeta xmlns:x='adobe:ns:meta/'>"
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
        "<rdf:Description rdf:about='' xmlns:xmp='http://ns.adobe.com/xap/1.0/'"
        f" xmlns:dc='http://purl.org/dc/elements/1.1/'{attrs}>{body}{extra}</rdf:Description>"
        "</rdf:RDF></x:xmpmeta><?xpacket end='w'?>"
    ).encode("utf-8")


def _image():
    from PIL import Image
    return Image.new("RGB", (16, 12), (200, 120, 40))


def _make_jpeg(path, packet=None):
    buf = io.BytesIO()
    _image().save(buf, "JPEG")
    data = buf.getvalue()
    if packet is not None:
        payload = b"http://ns.adobe.com/xap/1.0/\x00" + packet
        seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        # APP0(JFIF) の後ろに差し込む
        assert data[2:4] == b"\xff\xe0"
        app0_len = struct.unpack(">H", data[4:6])[0]
        cut = 4 + app0_len
        data = data[:cut] + seg + data[cut:]
    Path(path).write_bytes(data)
    return str(path)


def _make_png(path, packet=None, compressed=False):
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    if packet is not None:
        info.add_itxt("XML:com.adobe.xmp", packet.decode("utf-8"), zip=compressed)
    _image().save(path, "PNG", pnginfo=info)
    return str(path)


def _make_webp(path, packet=None):
    kw = {"xmp": packet} if packet is not None else {}
    _image().save(path, "WEBP", **kw)
    return str(path)


# ------------------------------------------------------------------ xmp: 読み取り

def test_is_photo():
    for n in ("a.jpg", "A.JPEG", "x.png", "x.webp", "x.heic", "x.HEIF", "x.tif", "x.tiff",
              "x.arw", "x.CR3", "x.nef", "x.dng", "x.orf", "x.rw2", "x.raf", "x.cr2"):
        assert px.is_photo(n), n
    for n in ("a.txt", "a.mp4", "a.xmp", "a.gif", "jpg", "a.jpg.bak"):
        assert not px.is_photo(n), n
    assert px.sidecar_path(r"C:\a\b.ARW") == r"C:\a\b.xmp"


def test_jpeg_attribute_form():
    d = _tmpdir("jpeg_attr")
    p = _make_jpeg(d / "a.jpg", _packet(3, ["みてね済", "旅行"]))
    assert px.read_meta(p) == PhotoMeta(3, ("みてね済", "旅行"))


def test_jpeg_element_form_and_decimal():
    d = _tmpdir("jpeg_elem")
    p = _make_jpeg(d / "a.jpg", _packet("4.0", element=True))
    assert px.read_meta(p) == PhotoMeta(4, ())
    p = _make_jpeg(d / "b.jpg", _packet("3.0"))
    assert px.read_meta(p).rating == 3


def test_jpeg_reject_and_none():
    d = _tmpdir("jpeg_reject")
    assert px.read_meta(_make_jpeg(d / "a.jpg", _packet(-1))) == PhotoMeta(-1, ())
    # XMP 無し → ★なし(0)
    assert px.read_meta(_make_jpeg(d / "b.jpg")) == PhotoMeta(0, ())
    # XMP はあるが Rating が無い
    assert px.read_meta(_make_jpeg(d / "c.jpg", _packet(None, ["x"]))) == PhotoMeta(0, ("x",))
    # 範囲外は丸める
    assert px.read_meta(_make_jpeg(d / "d.jpg", _packet(9))).rating == 5


def test_jpeg_after_sos_is_ignored():
    """SOS より後ろにある XMP らしき文字列は見ない(画像データの途中にたまたま出ても拾わない)。"""
    d = _tmpdir("jpeg_sos")
    p = _make_jpeg(d / "a.jpg")
    data = Path(p).read_bytes()
    Path(p).write_bytes(data[:-2] + _packet(5) + data[-2:])
    assert px.read_meta(p) == PhotoMeta(0, ())


def test_broken_files_are_unknown():
    d = _tmpdir("broken")
    (d / "trunc.jpg").write_bytes(b"\xff\xd8\xff\xe1\x00")
    assert px.read_meta(d / "trunc.jpg") is None
    (d / "garbage.jpg").write_bytes(b"\xff\xd8\x00\x01\x02\x03")
    assert px.read_meta(d / "garbage.jpg") is None
    (d / "empty.heic").write_bytes(b"")
    assert px.read_meta(d / "empty.heic") is None
    assert px.read_meta(d / "missing.jpg") is None
    # XML として壊れている
    bad = _packet(3).replace(b"</rdf:Description>", b"</rdf:Descr>")
    assert px.read_meta(_make_jpeg(d / "badxml.jpg", bad)) is None
    # Rating が数でない
    assert px.read_meta(_make_jpeg(d / "badnum.jpg", _packet("abc"))) is None
    # PNG のチャンクが途中で切れている
    good = Path(_make_png(d / "p.png", _packet(2))).read_bytes()
    (d / "cut.png").write_bytes(good[:40])
    assert px.read_meta(d / "cut.png") is None


def test_png():
    d = _tmpdir("png")
    assert px.read_meta(_make_png(d / "a.png", _packet(4, ["みてね済"]))) == PhotoMeta(4, ("みてね済",))
    assert px.read_meta(_make_png(d / "z.png", _packet(2), compressed=True)) == PhotoMeta(2, ())
    assert px.read_meta(_make_png(d / "n.png")) == PhotoMeta(0, ())


def test_webp():
    d = _tmpdir("webp")
    assert px.read_meta(_make_webp(d / "a.webp", _packet(5, ["k"]))) == PhotoMeta(5, ("k",))
    assert px.read_meta(_make_webp(d / "n.webp")) == PhotoMeta(0, ())


def test_scan_generic_and_mismatched_extension():
    d = _tmpdir("generic")
    # HEIC / TIFF などは先頭から探す簡易法
    (d / "a.heic").write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100 + _packet(4) + b"\x00" * 50)
    assert px.read_meta(d / "a.heic") == PhotoMeta(4, ())
    (d / "n.tif").write_bytes(b"II*\x00" + b"\x00" * 100)
    assert px.read_meta(d / "n.tif") == PhotoMeta(0, ())
    # 末尾にだけある大きいファイル
    big = d / "big.tif"
    with open(big, "wb") as f:
        f.write(b"II*\x00" + b"\x00" * (px.HEAD_BYTES + 10))
        f.write(_packet(2))
    assert px.read_meta(big) == PhotoMeta(2, ())
    # 拡張子が .jpg でも中身が PNG なら PNG として読む
    p = _make_png(d / "x.png", _packet(3))
    shutil.copy(p, d / "really_png.jpg")
    assert px.read_meta(d / "really_png.jpg") == PhotoMeta(3, ())


def test_raw_sidecar():
    d = _tmpdir("raw")
    (d / "a.ARW").write_bytes(b"II*\x00rawdata" + _packet(1))   # 本体の中身は見ない
    assert px.read_meta(d / "a.ARW") == PhotoMeta(0, ())      # サイドカーが無い → ★なし
    (d / "a.xmp").write_bytes(_packet(4, ["x"]))
    assert px.read_meta(d / "a.ARW") == PhotoMeta(4, ("x",))
    assert px.read_meta(d / "missing.nef") is None            # 本体が無い → 不明


def test_parse_xmp_misc():
    assert px.parse_xmp(b"") == PhotoMeta()
    assert px.parse_xmp(_packet(2).decode("utf-8")) == PhotoMeta(2, ())
    # x:xmpmeta の無い rdf:RDF だけのもの
    rdf_only = _packet(3).split(b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>")[1].split(b"</x:xmpmeta>")[0]
    assert px.parse_xmp(rdf_only) == PhotoMeta(3, ())
    # 重複したキーワードは1つに
    assert px.parse_xmp(_packet(0, ["a", "a", "b"])).keywords == ("a", "b")


# ------------------------------------------------------------------ config

def test_config_default_when_missing_or_broken():
    path = pc.CONFIG_PATH
    if os.path.exists(path):
        os.remove(path)
    cfg = pc.load_config()
    assert cfg == pc.default_config()
    assert [lv["min"] for lv in cfg["levels"]] == [5, 4, 3]
    assert cfg["keywords"] == [{"key": "K", "name": "みてね済"}]
    assert [p["expr"] for p in cfg["presets"]] == ["★>=3", "★=5 && !#みてね済", "reject"]
    assert cfg["frame_targets"] == [] and cfg["exiftool"] == ""
    assert cfg["frame_manual_delete"] == []
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text("{ broken", encoding="utf-8")
    assert pc.load_config() == pc.default_config()
    Path(path).write_text("[1, 2]", encoding="utf-8")
    assert pc.load_config() == pc.default_config()


def test_config_normalize_bad_values():
    cfg = pc.normalize({
        "levels": "nope",
        "keywords": [{"key": "K", "name": "済"}, {"key": "K", "name": "重複キー"},
                     {"key": "0", "name": "固定キー"}, {"name": "キー無し"}],
        "presets": [{"name": "a", "expr": "★>=1", "key": "U"}, {"name": "", "expr": "x"}, 3],
        "frame_targets": ["", "  ", r"X:\frame", 5],
        "frame_manual_delete": [None, " ", r"X:\frame"],
        "exiftool": 12,
        "future_option": {"a": 1},
    })
    assert cfg["levels"] == pc.default_config()["levels"]
    assert cfg["keywords"] == [{"key": "K", "name": "済"}]
    assert cfg["presets"] == [{"name": "a", "expr": "★>=1"}]   # U は固定キーなので落とす
    assert cfg["frame_targets"] == [r"X:\frame"]
    assert cfg["frame_manual_delete"] == [r"X:\frame"]
    assert pc.normalize({"frame_manual_delete": "X:\\frame"})["frame_manual_delete"] == []
    assert cfg["exiftool"] == ""
    assert cfg["future_option"] == {"a": 1}                     # 知らないキーは残す
    # 段階：範囲外・重複・固定キーは落とす。全部だめなら既定
    lv = pc.normalize({"levels": [{"key": "9", "name": "a", "min": 9},
                                  {"key": "/", "name": "b", "min": 3},
                                  {"key": "x", "name": "c", "min": 2.0},
                                  {"key": "y", "name": "c", "min": 3}]})["levels"]
    assert lv == [{"key": "X", "name": "c", "min": 2}]
    assert pc.normalize({"levels": []})["levels"] == pc.default_config()["levels"]


def test_config_save_roundtrip_lf():
    cfg = pc.default_config()
    cfg["exiftool"] = r"X:\tools\exiftool.exe"
    cfg["levels"][0]["name"] = "最上"
    pc.save_config(cfg)
    raw = Path(pc.CONFIG_PATH).read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    assert "最上".encode("utf-8") in raw
    back = pc.load_config()
    assert back["exiftool"] == r"X:\tools\exiftool.exe"
    assert back["levels"][0]["name"] == "最上"
    assert not [n for n in os.listdir(os.path.dirname(pc.CONFIG_PATH)) if n.endswith(".tmp")]


def test_config_levels_lookup():
    cfg = pc.default_config()
    assert pc.level_min(cfg, "ベストショット") == 3
    assert pc.level_min(cfg, "みてね用") == 5
    assert pc.level_min(cfg, "無い") is None
    assert pc.level_for_rating(cfg, 4)["name"] == "フォトフレーム用"
    assert pc.level_for_rating(cfg, 2) is None
    assert pc.rating_display(5, ("みてね済",), cfg) == "みてね用 済"
    assert pc.rating_display(2, (), cfg) == "★2"
    assert pc.rating_display(-1) == "❌削除候補"
    assert pc.rating_display(-1, ("みてね済",)) == "❌削除候補 済"
    assert pc.rating_display(0, ("みてね済",)) == "済"           # 未選別で済だけ
    assert pc.rating_display(0) == "" and pc.rating_display(None) == ""


def test_rating_names():
    cfg = pc.default_config()
    assert (pc.RATING_MIN, pc.RATING_MAX) == (-1, 5)
    assert [pc.rating_name(r, cfg) for r in range(-1, 6)] == [
        "削除候補", "未選別", "★1", "★2", "ベストショット", "フォトフレーム用", "みてね用"]
    assert [pc.rating_tag(r, cfg) for r in range(-1, 6)] == [
        "❌削除候補", "", "★1", "★2", "ベストショット", "フォトフレーム用", "みてね用"]
    assert [pc.rating_short(r, cfg) for r in range(-1, 6)] == [
        "❌", "未", "1", "2", "ベスト", "フレーム", "みてね"]
    assert pc.rating_name(4) == "フォトフレーム用"              # config を省くと既定
    # 段階は「しきい値がちょうどその★」のときだけ名前になる
    custom = pc.normalize({"levels": [{"key": "A", "name": "とっておき", "min": 2,
                                       "short": " とっ "},
                                      {"key": "B", "name": "まあまあ", "min": 1}]})
    assert custom["levels"][0]["short"] == "とっ"               # 前後の空白は落とす
    assert "short" not in custom["levels"][1]
    assert [pc.rating_short(r, custom) for r in range(-1, 6)] == [
        "❌", "未", "まあまあ", "とっ", "3", "4", "5"]              # short が無ければ name
    assert pc.rating_name(3, custom) == "★3" and pc.rating_name(2, custom) == "とっておき"
    # short の無い古い設定でも、既定と同じ名前の段階なら既定の短い名前
    old = pc.normalize({"levels": [{"key": "5", "name": "みてね用", "min": 5},
                                   {"key": "3", "name": "ベストショット", "min": 3,
                                    "short": 12}]})
    assert all("short" not in lv for lv in old["levels"])        # 文字列でない short は落とす
    assert pc.rating_short(5, old) == "みてね" and pc.rating_short(3, old) == "ベスト"


def test_rating_badge():
    cfg = pc.default_config()
    assert [lv["icon"] for lv in cfg["levels"]] == ["💌", "🖼️", "⭐"]
    assert [pc.rating_badge(r, cfg) for r in range(-1, 6)] == [
        ("❌", "削除"), None, ("", "★1"), ("", "★2"),
        ("⭐", "ベスト"), ("🖼️", "フレーム"), ("💌", "みてね")]
    assert pc.rating_badge(None) is None and pc.rating_badge(4) == ("🖼️", "フレーム")
    # icon の無い段階: 既定と同じ名前なら既定の絵文字、それ以外は絵文字なし
    custom = pc.normalize({"levels": [{"key": "5", "name": "みてね用", "min": 5},
                                      {"key": "A", "name": "とっておき", "min": 2, "short": "とっ"},
                                      {"key": "B", "name": "まあまあ", "min": 1, "icon": " 🙂 "},
                                      {"key": "C", "name": "数字", "min": 3, "icon": 7}]})
    assert all("icon" not in lv for lv in custom["levels"] if lv["name"] != "まあまあ")
    assert pc.rating_badge(5, custom) == ("💌", "みてね")
    assert pc.rating_badge(2, custom) == ("", "とっ")
    assert pc.rating_badge(1, custom) == ("🙂", "まあまあ")
    assert pc.rating_badge(3, custom) == ("", "数字")


def test_rating_range():
    cfg = pc.default_config()
    assert pc.is_full_range(-1, 5) and not pc.is_full_range(0, 5)
    assert not pc.is_full_range(-1, 4)
    assert pc.range_expr(-1, 5) == ""
    assert pc.range_expr(3, 5) == "★>=3"
    assert pc.range_expr(-1, 0) == "★<=0"
    assert pc.range_expr(1, 2) == "★>=1 && ★<=2"
    assert pc.range_expr(4, 4) == "★>=4 && ★<=4"
    assert pc.range_label(-1, 5, cfg) == "すべて"
    assert pc.range_label(4, 4, cfg) == "フォトフレーム用だけ"
    assert pc.range_label(-1, -1, cfg) == "削除候補だけ"
    assert pc.range_label(3, 5, cfg) == "ベストショット以上"
    assert pc.range_label(-1, 0, cfg) == "未選別まで"
    assert pc.range_label(1, 2, cfg) == "★1〜★2"
    assert pc.range_label(0, 4, cfg) == "未選別〜フォトフレーム用"
    # 式は filter_expr でそのまま読める（★の原子として評価される）
    node = fe.parse(f"(#みてね済) && {pc.range_expr(3, 4)}")
    meta = px.PhotoMeta(4, ("みてね済",))
    assert fe.evaluate(node, fe.meta_atom_fn(meta, cfg)) is True
    assert fe.evaluate(node, fe.meta_atom_fn(px.PhotoMeta(5, ("みてね済",)), cfg)) is False


def test_menu_items():
    cfg = pc.default_config()
    items = pc.menu_items(cfg)
    keys = [m.key for m in items if m.action[0] != "separator"]
    assert keys == ["5", "4", "3", "2", "1", "0", "-", "K", "/"]
    acts = {m.key: m.action for m in items}
    assert acts["5"] == ("rating", 5) and acts["0"] == ("rating", 0)
    assert acts["-"] == ("rating", -1)
    # 段階の無い★2・★1 も付けられる（名前は rating_name）
    by = {m.key: m for m in items}
    assert acts["2"] == ("rating", 2) and acts["1"] == ("rating", 1)
    assert (by["2"].label, by["2"].detail) == ("★2", "2")
    assert (by["1"].label, by["1"].detail) == ("★1", "1")
    assert acts["K"] == ("keyword", "みてね済")
    assert acts["/"] == ("filter_menu", None)
    assert sum(1 for m in items if m is pc.SEPARATOR) == 2
    assert pc.menu_title(3) == "写真の★（3枚）"


def test_menu_items_plain_ratings():
    """★2・★1 の行: 頭の文字 1・2 は段階・キーワードに使えない。段階のしきい値にあればその段階の行だけ。"""
    assert {"1", "2"} <= pc.MENU_FIXED_KEYS
    cfg = pc.normalize({"levels": [{"key": "1", "name": "a", "min": 5},
                                   {"key": "2", "name": "b", "min": 4},
                                   {"key": "4", "name": "c", "min": 4}],
                        "keywords": [{"key": "2", "name": "x"}, {"key": "K", "name": "みてね済"}]})
    assert [lv["key"] for lv in cfg["levels"]] == ["4"]
    assert cfg["keywords"] == [{"key": "K", "name": "みてね済"}]
    # しきい値 2 の段階があれば、★2 はその段階の行（「★2」の行は出さない）。並びは★の高い順
    cfg = pc.normalize({"levels": [{"key": "5", "name": "みてね用", "min": 5},
                                   {"key": "M", "name": "まあまあ", "min": 2},
                                   {"key": "4", "name": "フォトフレーム用", "min": 4}]})
    items = [m for m in pc.menu_items(cfg) if m.action[0] == "rating"]
    assert [(m.key, m.label, m.action[1]) for m in items] == [
        ("5", "みてね用", 5), ("4", "フォトフレーム用", 4), ("M", "まあまあ", 2), ("1", "★1", 1),
        ("0", "★を外す", 0), ("-", "削除候補", -1)]
    keys = [m.key for m in pc.menu_items(cfg, export=True) if m.action[0] != "separator"]
    assert len(set(keys)) == len(keys)


def test_filter_menu_items():
    cfg = pc.default_config()
    items = pc.filter_menu_items(cfg)
    assert [m.key for m in items] == ["B", "F", "M", "D", "U", "E", "A"]
    by = {m.key: m for m in items}
    assert by["B"].action == ("filter", "★>=3")
    assert by["F"].action == ("filter", "★>=4")
    assert by["M"].action == ("filter", "★=5 && !#みてね済")
    assert by["D"].action == ("filter", "reject")
    assert by["U"].action == ("filter", "unrated")
    assert by["E"].action == ("filter_input", None)
    assert by["A"].action == ("filter_clear", None)
    # どの式もパースできる
    for m in items:
        if m.action[0] == "filter":
            fe.parse(m.action[1])
    # key の無いプリセットは Alt+数字と同じ番号
    cfg["presets"].append({"name": "人物", "expr": "たろう && ★>=3"})
    keys = [m.key for m in pc.filter_menu_items(cfg)]
    assert "4" in keys


def test_find_exiftool():
    d = _tmpdir("find_et")
    exe = d / "exiftool(-k).exe"
    exe.write_bytes(b"")
    assert pc.find_exiftool({"exiftool": str(exe)}) == str(exe)
    assert pc.find_exiftool({"exiftool": f'"{exe}"'}) == str(exe)
    assert pc.find_exiftool({"exiftool": str(d)}) == str(exe)
    # 設定が無効なら PATH(テスト環境に依存するので、型だけ確かめる)
    r = pc.find_exiftool({"exiftool": str(d / "none.exe")})
    assert r is None or os.path.isfile(r)


# ------------------------------------------------------------------ filter_expr

def test_filter_new_atoms():
    A = fe.Atom
    assert fe.parse("★>=3") == A("rating", ">=3")
    assert fe.parse("r>=3") == A("rating", ">=3")
    assert fe.parse("RATING >= 3") == A("rating", ">=3")
    assert fe.parse("★==5") == A("rating", "=5")
    assert fe.parse("r = -1") == A("rating", "=-1")
    assert fe.parse("★!=0") == A("rating", "!=0")
    assert fe.parse("★<2") == A("rating", "<2")
    assert fe.parse("★<=4") == A("rating", "<=4")
    assert fe.parse("★>3.0") == A("rating", ">3")
    assert fe.parse("@ベストショット") == A("level", "ベストショット")
    assert fe.parse('@"空白 のある名前"') == A("level", "空白 のある名前")
    assert fe.parse("#みてね済") == A("keyword", "みてね済")
    assert fe.parse('#"a b"') == A("keyword", "a b")
    assert fe.parse("reject") == A("reject")
    assert fe.parse("UNRATED") == A("unrated")
    assert fe.parse("sel") == A("sel")
    assert fe.parse("face") == A("face")
    # 旧い別名
    assert fe.parse("keep") == A("sel")
    assert fe.parse("del") == A("reject")
    # 比較の無い r / rating は人物名(互換)
    assert fe.parse("r") == A("person", "r")
    assert fe.parse("r && rating") == fe.And(A("person", "r"), A("person", "rating"))
    assert fe.parse("r&&!たろう") == fe.And(A("person", "r"), fe.Not(A("person", "たろう")))
    # 組み合わせ
    n = fe.parse("★=5 && !#みてね済")
    assert n == fe.And(A("rating", "=5"), fe.Not(A("keyword", "みてね済")))
    n = fe.parse("(たろう || はなこ) and r>=4 && !del")
    assert n == fe.And(fe.And(fe.Or(A("person", "たろう"), A("person", "はなこ")), A("rating", ">=4")),
                       fe.Not(A("reject")))
    assert fe.needs_meta(n) and fe.needs_faces(n)
    assert not fe.needs_faces(fe.parse("★>=3 && sel"))
    assert not fe.needs_meta(fe.parse("たろう && sel"))
    assert fe.referenced_levels(fe.parse("@a || @b || @a")) == ["a", "b"]
    assert fe.split_rating("!=-1") == ("!=", -1)


def test_filter_errors():
    cases = [
        ("★", 1, "'★' の後に比較"),
        ("★3", 1, "'★' の後に比較"),
        ("★>=", 4, "の後に★の値"),
        ("r >= ", 5, "の後に★の値"),
        ("★>=6", 4, "-1〜5 の整数"),
        ("★>=-2", 4, "-1〜5 の整数"),
        ("★>=2.5", 4, "-1〜5 の整数"),
        ("★>=3x", 5, "余計な文字"),
        ("@", 1, "'@' の後に段階名"),
        ("# && a", 1, "'#' の後にキーワード"),
        ('@"', 2, "閉じていません"),
        ('#""', 2, "キーワードがありません"),
        ("★>=3 &&", 6, "'&&' の後に条件がありません"),
        ("★>=3 ★>=4", 6, "演算子"),
        ("(★>=3", 6, "')' が足りません"),
        ("a & b", 3, "'&' は1つでは使えません"),
    ]
    for expr, pos, part in cases:
        try:
            fe.parse(expr)
        except fe.FilterSyntaxError as e:
            assert e.position + 1 == pos, (expr, e.message)
            assert part in e.message, (expr, e.message)
            assert e.message.startswith(f"{pos}文字目: "), e.message
        else:
            raise AssertionError(f"エラーにならない: {expr}")


def test_filter_roundtrip():
    exprs = [
        "★>=3", "r == 5", "rating != -1", "@ベストショット", '@"a b"', "#みてね済", '#"x y"',
        "reject", "unrated", "sel", "keep", "del", "face", "たろう", '"keep"', '"r>=3"', '"#x"',
        '"@y"', '"★"', "r", "★=5 && !#みてね済", "!(★>=3 || reject) && たろう",
        "a || (b || c)", "(a || b) && c", "not (sel and @x) or #k",
    ]
    for e in exprs:
        n = fe.parse(e)
        t = fe.to_text(n)
        assert fe.parse(t) == n, (e, t)
        assert fe.to_text(fe.parse(t)) == t
    assert fe.to_text(fe.parse("keep && del")) == "sel && reject"
    assert fe.to_text(fe.parse("r==5")) == "★=5"
    assert fe.canonical("★ >= 3 and #みてね済") == "★>=3 && #みてね済"
    assert fe.canonical("たろう and はなこ") == fe.canonical("たろう&&はなこ")
    assert fe.canonical("★>=") == "★>="
    # 人物名の引用
    for name in ("r>=3", "#x", "@y", "★", "sel", "reject", "unrated", "and", "a b", "x(y"):
        q = fe.quote_name(name)
        assert q == f'"{name}"', (name, q)
        assert fe.parse(q) == fe.Atom("person", name)
    assert fe.quote_name("たろう") == "たろう"
    assert fe.quote_name("r") == "r"


def test_filter_negate_and_legacy():
    assert fe.negate_text("★>=3") == "!★>=3"
    assert fe.negate_text("!★>=3") == "★>=3"
    assert fe.negate_text("★=5 && !#みてね済") == "!(★=5 && !#みてね済)"
    assert fe.negate_text("!(sel || reject)") == "sel || reject"
    assert fe.negate_text("(!keep)") == "sel"
    assert fe.negate_text("") == ""
    assert fe.legacy_person_filter_to_text({"person": "たろう", "mode": "without"}) == "!たろう"
    assert fe.legacy_person_filter_to_text({"person": "a b", "mode": "with"}) == '"a b"'
    assert fe.parse("") is None and fe.parse("   ") is None


def _meta_eval(expr, meta, cfg=None, other=None):
    return fe.evaluate(fe.parse(expr), fe.meta_atom_fn(meta, cfg, other))


def test_filter_meta_evaluation():
    m5 = PhotoMeta(5, ("みてね済",))
    m4 = PhotoMeta(4, ())
    mr = PhotoMeta(-1, ())
    m0 = PhotoMeta(0, ())
    assert _meta_eval("★>=3", m4) is True
    assert _meta_eval("★>=5", m4) is False
    assert _meta_eval("★=5 && !#みてね済", m5) is False
    assert _meta_eval("★=5 && !#みてね済", PhotoMeta(5, ())) is True
    assert _meta_eval("#ミテネ済", m5) is False
    assert _meta_eval("#ABC", PhotoMeta(1, ("abc",))) is True     # 大文字小文字は区別しない
    assert _meta_eval("reject", mr) is True and _meta_eval("del", mr) is True
    assert _meta_eval("reject", m0) is False
    assert _meta_eval("unrated", m0) is True and _meta_eval("unrated", m4) is False
    assert _meta_eval("@ベストショット", m4) is True
    assert _meta_eval("@みてね用", m4) is False
    assert _meta_eval("@無い段階", m5) is False
    cfg = pc.default_config()
    cfg["levels"] = [{"key": "9", "name": "ゆるめ", "min": 1}]
    assert _meta_eval("@ゆるめ", PhotoMeta(1, ()), cfg) is True
    assert _meta_eval("★<0", mr) is True and _meta_eval("★!=-1", mr) is False
    # 読めない写真(None)は不明。三値論理
    assert _meta_eval("★>=3", None) is None
    assert _meta_eval("!★>=3", None) is None
    assert _meta_eval("★>=3 && sel", None, other=lambda k, n: False) is False
    assert _meta_eval("★>=3 || sel", None, other=lambda k, n: True) is True
    assert _meta_eval("★>=3 || sel", None, other=lambda k, n: False) is None
    # アプリ側の原子は NotImplemented(meta_atom_fn では other へ。無ければ不明)
    for kind, name in (("person", "たろう"), ("face", None), ("sel", None)):
        assert fe.meta_atom_value(kind, name, m4, None) is NotImplemented
    assert _meta_eval("たろう", m4) is None
    assert _meta_eval("sel && ★>=4", m4, other=lambda k, n: k == "sel") is True


# ------------------------------------------------------------------ 変更記録(ExifTool 不要)

def test_journal_reader():
    jp = os.path.join(_TEST_TEMP, "journal", "j.jsonl")
    os.makedirs(os.path.dirname(jp), exist_ok=True)
    pw.append_journal(r"C:\x\old.jpg", 1, (), jp)
    reader = pw.JournalReader(jp)                 # 今の末尾から
    assert reader.read_new() == []
    pw.append_journal(r"C:\x\a.jpg", 5, ("みてね済",), jp)
    pw.append_journal(r"C:\x\b.jpg", -1, (), jp)
    got = reader.read_new()
    assert [(e["path"], e["rating"], e["keywords"]) for e in got] == [
        (r"C:\x\a.jpg", 5, ["みてね済"]), (r"C:\x\b.jpg", -1, [])]
    assert all(e["pid"] == os.getpid() and e["time"] > 0 for e in got)
    assert reader.read_new() == []
    assert reader.read_new(exclude_pid=os.getpid()) == []
    # 書きかけの行は次回に回す
    with open(jp, "ab") as f:
        f.write(b'{"path": "C:\\\\x\\\\c.jpg", "rati')
    assert reader.read_new() == []
    with open(jp, "ab") as f:
        f.write(b'ng": 3, "keywords": [], "time": 1, "pid": 1}\n' + b"not json\n")
    got = reader.read_new()
    assert [e["path"] for e in got] == [r"C:\x\c.jpg"]
    assert pw.JournalReader(jp, from_start=True).read_new(exclude_pid=os.getpid())[0]["pid"] == 1
    # LF で書く
    assert b"\r\n" not in Path(jp).read_bytes()


def test_journal_rotation():
    jp = os.path.join(_TEST_TEMP, "journal_rot", "j.jsonl")
    os.makedirs(os.path.dirname(jp), exist_ok=True)
    saved = pw.JOURNAL_MAX_BYTES
    try:
        reader = pw.JournalReader(jp)
        for i in range(4):
            pw.append_journal(rf"C:\x\{i}.jpg", 3, (), jp)
        assert len(reader.read_new()) == 4
        assert not os.path.exists(jp + ".old")
        pw.JOURNAL_MAX_BYTES = os.path.getsize(jp) - 1    # 次に書くときに .old へ回る
        for i in range(4, 8):
            pw.append_journal(rf"C:\x\{i}.jpg", 3, (), jp)
        assert os.path.exists(jp + ".old")
        assert Path(jp + ".old").read_bytes().count(b"\n") == 4
        assert Path(jp).read_bytes().count(b"\n") == 4
        # ファイルが替わった(大きさはほぼ同じでも)ので先頭から読み直す
        got = [e["path"] for e in reader.read_new()]
        assert got == [rf"C:\x\{i}.jpg" for i in range(4, 8)], got
        # 縮んだときも先頭から
        pw.JOURNAL_MAX_BYTES = saved
        pw.append_journal(r"C:\x\8.jpg", 3, (), jp)
        reader.read_new()
        Path(jp).write_bytes(b"")
        pw.append_journal(r"C:\x\9.jpg", 3, (), jp)
        assert [e["path"] for e in reader.read_new()] == [r"C:\x\9.jpg"]
    finally:
        pw.JOURNAL_MAX_BYTES = saved


def test_writer_args():
    assert pw.rating_tags(5) == ["-XMP-xmp:Rating=5", "-XMP-microsoft:RatingPercent=99",
                                 "-EXIF:Rating=5", "-EXIF:RatingPercent=99"]
    assert pw.rating_tags(-1) == ["-XMP-xmp:Rating=-1", "-XMP-microsoft:RatingPercent=0",
                                  "-EXIF:Rating=0", "-EXIF:RatingPercent=0"]
    assert pw.rating_tags(3, xmp_only=True) == ["-XMP-xmp:Rating=3", "-XMP-microsoft:RatingPercent=50"]
    assert [pw.rating_percent(r) for r in (-1, 0, 1, 2, 3, 4, 5)] == [0, 0, 1, 25, 50, 75, 99]
    assert pw.keyword_tags("k", True) == ["-XMP-dc:Subject-=k", "-XMP-dc:Subject+=k"]
    assert pw.keyword_tags("k", False) == ["-XMP-dc:Subject-=k"]
    ok, err = pw._parse_output("    1 image files updated\r\n")
    assert ok and err is None
    ok, err = pw._parse_output("    0 image files updated\r\n    1 image files unchanged\r\n")
    assert ok
    ok, err = pw._parse_output("Error: File not found - x.jpg\r\n    0 image files updated\r\n"
                               "    1 files weren't updated due to errors\r\n")
    assert not ok and err.startswith("Error: File not found")
    s = pw.ExifToolSession("unused.exe")
    try:
        s.execute(["a\nb"])
    except ValueError:
        pass
    else:
        raise AssertionError("改行入りの引数を通した")
    for bad in (6, -2, 3.0, True):
        try:
            s.set_rating([], bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"不正な★を通した: {bad!r}")


# ------------------------------------------------------------------ writer(ExifTool で実際に書く)

def _exiftool():
    env = os.environ.get("PHOTOMETA_EXIFTOOL")
    if env:
        return pc.find_exiftool({"exiftool": env})
    return pc.find_exiftool({})


def _heic(path):
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        return None
    _image().save(path, "HEIF")
    return str(path)


def test_writer_exiftool():
    exe = _exiftool()
    if not exe:
        print("skip  test_writer_exiftool: ExifTool が見つかりません(PHOTOMETA_EXIFTOOL か PATH)")
        return
    d = _tmpdir("writer")
    files = [
        _make_jpeg(d / "a.jpg"),
        _make_jpeg(d / "既存★.jpg", _packet(2, ["旅行"])),
        _make_png(d / "p.png"),
        _make_webp(d / "w.webp"),
    ]
    tif = d / "t.tif"
    _image().save(tif, "TIFF")
    files.append(str(tif))
    h = _heic(d / "h.heic")
    if h:
        files.append(h)
    raw = d / "r.ARW"
    raw.write_bytes(b"II*\x00dummy raw")
    files.append(str(raw))
    old = time.time() - 86400 * 30
    for f in files:
        os.utime(f, (old, old))
    mtimes = {f: os.stat(f).st_mtime_ns for f in files}
    raw_bytes = raw.read_bytes()

    jp = os.path.join(_TEST_TEMP, "writer_journal.jsonl")
    with pw.ExifToolSession(exe, journal_path=jp) as et:
        assert et.version()
        reader = pw.JournalReader(jp)
        res = et.set_rating(files, 5)
        for r in res:
            assert r.ok, (r.path, r.error)
            assert r.after == px.read_meta(r.path) and r.after.rating == 5, (r.path, r.after)
        before = {r.path: r.before for r in res}
        assert before[files[1]] == PhotoMeta(2, ("旅行",))
        assert before[files[0]] == PhotoMeta(0, ())
        # 更新日時は変わらない
        for f in files:
            assert os.stat(f).st_mtime_ns == mtimes[f], f
        # RAW 本体は触らず、サイドカーに書く
        assert raw.read_bytes() == raw_bytes
        assert (d / "r.xmp").exists()
        # 既存のキーワードは残る
        assert px.read_meta(files[1]).keywords == ("旅行",)
        # 変更記録
        got = reader.read_new()
        assert [os.path.normcase(e["path"]) for e in got] == \
            [os.path.normcase(os.path.abspath(f)) for f in files]
        assert all(e["rating"] == 5 for e in got)

        # キーワードを付ける(2回付けても1つ)・外す
        et.set_keyword(files, "みてね済", True)
        res = et.set_keyword(files, "みてね済", True)
        for r in res:
            assert r.ok, (r.path, r.error)
            assert px.read_meta(r.path).keywords.count("みてね済") == 1, r.path
        assert px.read_meta(files[1]).keywords == ("旅行", "みてね済")
        res = et.set_keyword(files[:2], "みてね済", False)
        assert all(r.ok for r in res)
        assert px.read_meta(files[0]).keywords == ()
        assert px.read_meta(files[1]).keywords == ("旅行",)
        # 付いていないキーワードを外すのも成功扱い
        assert all(r.ok for r in et.set_keyword(files[:1], "無いキーワード", False))

        # 削除候補(-1)と取り消し
        res = et.set_rating(files[:3], -1)
        assert all(r.ok and px.read_meta(r.path).rating == -1 for r in res)
        back = et.restore(res)
        assert all(r.ok for r in back)
        assert [px.read_meta(f).rating for f in files[:3]] == [5, 5, 5]
        res = et.set_keyword(files[2:3], "みてね済", False)
        et.restore(res)
        assert "みてね済" in px.read_meta(files[2]).keywords

        # ★を外す(0)
        assert all(r.ok for r in et.set_rating(files, 0))
        assert all(px.read_meta(f).rating == 0 for f in files)
        for f in files:
            assert os.stat(f).st_mtime_ns == mtimes[f], f

        # 失敗するもの
        (d / "note.txt").write_text("x", encoding="utf-8")
        r = et.set_rating([d / "note.txt"], 3)[0]
        assert not r.ok and "写真ではありません" in r.error
        r = et.set_rating([d / "missing.jpg"], 3)[0]
        assert not r.ok and "ありません" in r.error
        broken = d / "broken.jpg"
        broken.write_bytes(b"\xff\xd8\xff\xe0garbage")
        r = et.set_rating([broken], 3)[0]
        assert not r.ok and r.error, r
        ro = d / "readonly.jpg"
        _make_jpeg(ro)
        os.chmod(ro, 0o444)
        try:
            r = et.set_rating([ro], 3)[0]
            assert not r.ok and "読み取り専用" in r.error, r
        finally:
            os.chmod(ro, 0o666)
        # 失敗しても ExifTool の一時ファイルを写真の隣に残さない
        assert not [n for n in os.listdir(d) if "_exiftool_tmp" in n], os.listdir(d)
        proc = et._proc
    # with を抜けたら ExifTool は終わっている
    assert proc is not None and proc.poll() is not None


def test_writer_restart_after_exit():
    """ExifTool が途中で終わっていても、次の書き込みで起こし直す。"""
    exe = _exiftool()
    if not exe:
        print("skip  test_writer_restart_after_exit: ExifTool が見つかりません")
        return
    d = _tmpdir("writer_restart")
    f = _make_jpeg(d / "a.jpg")
    et = pw.ExifToolSession(exe, journal_path=os.path.join(_TEST_TEMP, "restart.jsonl"))
    try:
        assert et.set_rating([f], 4)[0].ok
        et._proc.kill()
        et._proc.wait(5)
        r = et.set_rating([f], 3)[0]
        assert r.ok and px.read_meta(f).rating == 3, r
    finally:
        et.close()
    assert et._proc is None


# ------------------------------------------------------------------ export(書き出し)

from photometa import export as pe  # noqa: E402

# 台帳の既定の場所(と、その隣の「手で消す写真の一覧」)も一時フォルダへ
pe.default_ledger_dir = lambda: os.path.join(_TEST_TEMP, "data", "frame_ledger")


def _exif_jpeg(path, size=(3000, 2000), orientation=None, when="2024:09:03 02:23:44",
               gps=True, packet=None, color_split=True):
    """EXIF(向き・撮影日時・GPS)入りの JPEG。左半分が赤・右半分が青。"""
    from PIL import Image
    img = Image.new("RGB", size, (0, 0, 255))
    if color_split:
        img.paste((255, 0, 0), (0, 0, size[0] // 2, size[1]))
    exif = Image.Exif()
    exif[0x010F] = "TestMaker"                 # Make(フレーム用には残さない)
    if orientation:
        exif[0x0112] = orientation
    if when:
        exif[0x0132] = when
        exif.get_ifd(0x8769)[0x9003] = when
    if gps:
        gi = exif.get_ifd(0x8825)
        gi[1] = "N"
        gi[2] = (35.0, 39.0, 29.0)
        gi[3] = "E"
        gi[4] = (139.0, 44.0, 28.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, exif=exif.tobytes())
    data = buf.getvalue()
    if packet is not None:
        payload = b"http://ns.adobe.com/xap/1.0/\x00" + packet
        seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        data = data[:2] + seg + data[2:]
    Path(path).write_bytes(data)
    return str(path)


def _rated(path, rating, keywords=(), **kw):
    return _exif_jpeg(path, size=kw.pop("size", (64, 48)), packet=_packet(rating, keywords), **kw)


def _junction(link, target):
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        return True
    except (ImportError, OSError, AttributeError):
        return False


def test_export_config_and_menu():
    cfg = pc.default_config()
    assert cfg["frame"] == {"max_long_edge": 2560, "quality": 85, "strip_gps": True}
    assert cfg["mitene_dir"] == ""
    bad = pc.normalize({"frame": {"max_long_edge": 10, "quality": "x", "strip_gps": False},
                        "mitene_dir": "  X:\\m  "})
    assert bad["frame"] == {"max_long_edge": 2560, "quality": 85, "strip_gps": False}
    assert bad["mitene_dir"] == "X:\\m"
    assert pc.normalize({"frame": "broken"})["frame"]["quality"] == 85
    assert pc.normalize({"frame": {"max_long_edge": 1920.0}})["frame"]["max_long_edge"] == 1920
    # I メニュー: export=True のときだけ E。既存のキーと重ならない
    keys = [m.key for m in pc.menu_items(cfg, export=True) if m.action[0] != "separator"]
    assert keys == ["5", "4", "3", "2", "1", "0", "-", "K", "/", "E"] and len(set(keys)) == len(keys)
    assert "E" not in [m.key for m in pc.menu_items(cfg)]
    assert pc.menu_items(cfg, export=True)[-1].action == ("export_menu", None)
    # E は段階のキーに使えない
    lv = pc.normalize({"levels": [{"key": "E", "name": "x", "min": 3},
                                  {"key": "3", "name": "y", "min": 3}]})["levels"]
    assert [x["key"] for x in lv] == ["3"]
    items = pc.export_menu_items(cfg)
    assert [m.key for m in items] == ["F", "M", "D", "L"]
    assert [m.action[0] for m in items] == ["frame_sync", "mitene_collect", "mitene_finish",
                                            "frame_manual_list"]
    assert items[-1].label == "手で消す写真の一覧（0枚）"
    # 式
    assert pe.frame_expr(cfg) == "★>=4"
    assert pe.mitene_expr(cfg) == "★=5 && !#みてね済"
    cfg2 = pc.default_config()
    cfg2["presets"] = []
    assert pe.mitene_expr(cfg2) == "@みてね用 && !#みてね済"
    fe.parse(pe.mitene_expr(cfg2))
    cfg2["levels"][1]["min"] = 3
    cfg2["levels"][2]["min"] = 2
    assert pe.frame_expr(cfg2) == "★>=3"
    assert pe.mitene_keyword(cfg) == "みてね済"
    assert pe.mitene_dir(cfg).endswith("みてね用")
    assert pe.mitene_dir({"mitene_dir": "%TEMP%\\mm"}) == os.path.abspath(
        os.path.expandvars("%TEMP%\\mm"))


def test_collect_excludes_and_expr():
    import stat as _stat
    import subprocess
    import threading
    d = _tmpdir("collect")
    root = d / "root"
    for sub in ("sub/deep", "print", "Bestshot", "delete_candidates", "extract_たろう",
                ".hidden", "attr_hidden", "frame_out", "mitene_out"):
        (root / sub).mkdir(parents=True)
    _rated(root / "a5.jpg", 5)
    _rated(root / "b4.jpg", 4, ["みてね済"])
    _rated(root / "c3.jpg", 3)
    _rated(root / "sub" / "deep" / "d4.jpg", 4)
    (root / "sub" / "broken.jpg").write_bytes(b"\xff\xd8\xff\xe1")
    (root / "memo.txt").write_text("x", encoding="utf-8")
    for sub in ("print", "Bestshot", "delete_candidates", "extract_たろう", ".hidden",
                "attr_hidden", "frame_out", "mitene_out"):
        _rated(root / sub / "x5.jpg", 5)
    subprocess.run(["attrib", "+h", str(root / "attr_hidden")], check=False,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    outside = d / "outside"
    outside.mkdir()
    _rated(outside / "j5.jpg", 5)
    has_junction = _junction(root / "link", outside)
    cfg = pc.default_config()
    cfg["frame_targets"] = [str(root / "frame_out")]
    cfg["mitene_dir"] = str(root / "mitene_out")
    seen = []
    res = pe.collect(root, "★>=4", cfg, progress=lambda a, b, n: seen.append((a, b, n)))
    names = [os.path.relpath(p, root) for p in res.paths]
    assert names == ["a5.jpg", "b4.jpg", os.path.join("sub", "deep", "d4.jpg")], names
    assert res.metas[str(root / "a5.jpg")].rating == 5
    assert [os.path.basename(p) for p in res.unknown] == ["broken.jpg"]
    assert res.scanned == 5 and not res.cancelled
    assert seen and all(b == 0 for _a, b, _n in seen)
    if has_junction:
        assert (root / "link" / "j5.jpg").exists()
    # 構文木でも・式なし(全部)でも
    res = pe.collect(root, fe.parse("★=5 && !#みてね済"), cfg)
    assert [os.path.basename(p) for p in res.paths] == ["a5.jpg"]
    res = pe.collect(root, None, cfg)
    assert len(res.paths) == 4
    # 中止
    ev = threading.Event()
    ev.set()
    assert pe.collect(root, "★>=4", cfg, cancel=ev).cancelled
    # 属性の後始末(一時フォルダを消せるように)
    try:
        os.chmod(root / "attr_hidden", _stat.S_IWRITE)
    except OSError:
        pass


def _frame_setup(name):
    d = _tmpdir(name)
    src = d / "photos"
    src.mkdir()
    t1, t2 = d / "frame1", d / "frame2"
    t1.mkdir()
    t2.mkdir()
    ledger = d / "ledger"
    return d, src, t1, t2, ledger


def test_sync_frame_jpeg():
    """縮小・向き補正・GPS 消去・日時保持・名前。"""
    from PIL import Image
    d, src, t1, t2, ledger = _frame_setup("frame_jpeg")
    a = _exif_jpeg(src / "IMG_0001.jpg", size=(3000, 2000), orientation=6)
    small = _exif_jpeg(src / "small.jpg", size=(800, 600), when=None, gps=False)
    old = time.mktime((2023, 1, 2, 3, 4, 5, 0, 0, -1))
    os.utime(small, (old, old))
    res = pe.sync_frame([a, small], [str(t1)], {}, str(ledger))
    assert not res.failed and not res.error, res
    assert sorted(n for _t, n, _s in res.added) == ["20230102_030405_small.jpg",
                                                     "20240903_022344_IMG_0001.jpg"]
    out = t1 / "20240903_022344_IMG_0001.jpg"
    with Image.open(out) as im:
        # 3000x2000 を右に90°回して 2000x3000 → 長辺 2560
        assert im.format == "JPEG" and im.size == (1707, 2560), im.size
        exif = im.getexif()
        assert 0x0112 not in exif                      # 向き情報は付けない
        assert 0x010F not in exif                      # ほかの EXIF も付けない
        assert exif.get(0x0132) == "2024:09:03 02:23:44"
        assert exif.get_ifd(0x8769).get(0x9003) == "2024:09:03 02:23:44"
        assert not exif.get_ifd(0x8825) and 0x8825 not in exif     # GPS なし
        # 元の左(赤)が上に来ている
        top = im.getpixel((im.size[0] // 2, 20))
        bottom = im.getpixel((im.size[0] // 2, im.size[1] - 20))
        assert top[0] > 200 and top[2] < 60, top
        assert bottom[2] > 200 and bottom[0] < 60, bottom
    with Image.open(t1 / "20230102_030405_small.jpg") as im:
        assert im.size == (800, 600)                   # 拡大しない
    # 元の写真は触らない
    with Image.open(a) as im:
        assert im.size == (3000, 2000) and im.getexif().get(0x0112) == 6
    # strip_gps=False なら GPS を残す・品質を変えると作り直し
    res = pe.sync_frame([a], [str(t2)], {"strip_gps": False, "max_long_edge": 1000,
                                         "quality": 70}, str(ledger))
    with Image.open(t2 / "20240903_022344_IMG_0001.jpg") as im:
        assert im.size == (667, 1000)
        assert im.getexif().get_ifd(0x8825).get(1) == "N"
    # 台帳は同期先ごと(t1 の台帳に t2 の分は入らない)
    l1 = pe.load_ledger(str(ledger), str(t1))
    assert set(l1) == {"20240903_022344_IMG_0001.jpg", "20230102_030405_small.jpg"}
    rec = l1["20240903_022344_IMG_0001.jpg"]
    assert rec["source"] == a and rec["out_size"] == out.stat().st_size
    assert rec["settings"]["max_long_edge"] == 2560
    # 一時ファイルを残さない
    assert not [n for n in os.listdir(t1) if n.endswith(".tmp")]


def test_sync_frame_ledger_and_manual_files():
    import threading
    d, src, t1, t2, ledger = _frame_setup("frame_ledger")
    a = _exif_jpeg(src / "a.jpg", size=(400, 300), when="2024:01:01 10:00:00")
    b = _exif_jpeg(src / "b.jpg", size=(400, 300), when="2024:01:02 10:00:00")
    c = _exif_jpeg(src / "c.jpg", size=(400, 300), when="2024:01:03 10:00:00")
    # 手で入れた写真(同期と同じ名前のものも)
    (t1 / "手で入れた.jpg").write_bytes(b"manual photo")
    (t1 / "20240101_100000_a.jpg").write_bytes(b"manual same name")
    targets = [str(t1), str(t2)]
    res = pe.sync_frame([a, b, c], targets, {}, str(ledger))
    assert len(res.added) == 6 and not res.failed, res
    l1 = pe.load_ledger(str(ledger), str(t1))
    name_a1 = next(n for n, r in l1.items() if r["source"] == a)
    assert name_a1 == "20240101_100000_a_2.jpg"        # ぶつかったら連番(手置きは上書きしない)
    assert (t1 / "20240101_100000_a.jpg").read_bytes() == b"manual same name"
    assert (t2 / "20240101_100000_a.jpg").exists()     # t2 ではぶつからない
    # 同じものを全部の同期先へ(作るのは1回)
    assert (t1 / name_a1).read_bytes() == (t2 / "20240101_100000_a.jpg").read_bytes()

    # 2回目: 変更なし
    res = pe.sync_frame([a, b, c], targets, {}, str(ledger))
    assert len(res.unchanged) == 6 and not (res.added or res.updated or res.deleted), res

    # 元の写真が変わったものだけ作り直す
    _exif_jpeg(src / "b.jpg", size=(500, 300), when="2024:01:02 10:00:00")
    res = pe.sync_frame([a, b, c], targets, {}, str(ledger))
    assert sorted(s for _t, _n, s in res.updated) == [b, b] and len(res.unchanged) == 4, res

    # 設定が変わったら全部作り直す
    res = pe.sync_frame([a, b, c], targets, {"quality": 60}, str(ledger))
    assert len(res.updated) == 6, res

    # 手で差し替えられたファイル: 上書きしない(元の写真が変わっても)
    replaced = t2 / "20240103_100000_c.jpg"
    replaced.write_bytes(b"replaced by hand")
    _exif_jpeg(src / "c.jpg", size=(410, 300), when="2024:01:03 10:00:00")
    res = pe.sync_frame([a, b, c], targets, {"quality": 60}, str(ledger))
    assert replaced.read_bytes() == b"replaced by hand"
    assert any(p == str(replaced) for p, _r in res.kept), res.kept
    assert any("手で差し替え" in n for n in res.notes())

    # ★が下がって c が外れた: 台帳にあるものだけ消す。手で差し替えたものは消さない
    res = pe.sync_frame([a, b], targets, {"quality": 60}, str(ledger))
    assert [(os.path.basename(t), n) for t, n, _s in res.deleted] == \
        [("frame1", "20240103_100000_c.jpg")], res.deleted
    assert replaced.exists() and replaced.read_bytes() == b"replaced by hand"
    assert not (t1 / "20240103_100000_c.jpg").exists()
    assert (t1 / "手で入れた.jpg").read_bytes() == b"manual photo"
    assert (t1 / "20240101_100000_a.jpg").read_bytes() == b"manual same name"
    # 差し替えたものは台帳から外れる(以後は手で入れた扱い)
    assert "20240103_100000_c.jpg" not in pe.load_ledger(str(ledger), str(t2))

    # 全部外れても、手で入れたものは残る
    res = pe.sync_frame([], targets, {"quality": 60}, str(ledger))
    assert len(res.deleted) == 4, res
    assert sorted(os.listdir(t1)) == ["20240101_100000_a.jpg", "手で入れた.jpg"]
    assert sorted(os.listdir(t2)) == ["20240103_100000_c.jpg"]

    # scope の外・keep の下の写真は消さない。中止したら消さない
    res = pe.sync_frame([a, b], targets, {}, str(ledger))
    assert len(res.added) == 4
    res = pe.sync_frame([], targets, {}, str(ledger), scope=[str(d / "elsewhere")])
    assert not res.deleted
    res = pe.sync_frame([], targets, {}, str(ledger), scope=[str(src)], keep=[a])
    assert sorted(s for _t, _n, s in res.deleted) == [b, b]
    ev = threading.Event()
    ev.set()
    res = pe.sync_frame([], targets, {}, str(ledger), cancel=ev)
    assert res.cancelled and not res.deleted
    assert len(pe.load_ledger(str(ledger), str(t1))) == 1

    # 同期先から消されていたら置き直す
    name = next(iter(pe.load_ledger(str(ledger), str(t1))))
    os.remove(t1 / name)
    res = pe.sync_frame([a], [str(t1)], {}, str(ledger))
    assert [n for _t, n, _s in res.added] == [name] and (t1 / name).exists()

    # 同期先が空・元の写真が無い
    assert pe.sync_frame([a], [], {}, str(ledger)).error == \
        "同期先がありません（photo_ratings.json の frame_targets）"
    res = pe.sync_frame([str(src / "missing.jpg")], [str(t1)], {}, str(ledger))
    assert res.failed and "ありません" in res.failed[0][1]


def test_sync_frame_manual_delete():
    """削除がクラウドへ反映されない同期先(manual_delete)から消したら「手で消す写真」に残す。
    入れ直したら外れる。manual_delete でない同期先は記録しない。"""
    d, src, t1, t2, ledger = _frame_setup("frame_manual")
    record = d / pe.MANUAL_DELETE_FILE                  # 台帳のフォルダの隣
    assert pe.manual_delete_path(str(ledger)) == str(record)
    a = _exif_jpeg(src / "a.jpg", size=(400, 300), when="2024:01:01 10:00:00")
    b = _exif_jpeg(src / "b.jpg", size=(400, 300), when="2024:01:02 10:00:00")
    targets = [str(t1), str(t2)]
    # 大文字小文字・区切りの違い・末尾の区切りは同じフォルダとして扱う
    manual = [str(t1).upper().replace("\\", "/") + "/"]
    res = pe.sync_frame([a, b], targets, {}, str(ledger), manual_delete=manual)
    assert len(res.added) == 4 and not res.manual_delete
    assert not record.exists()                          # 何も消していなければ書かない
    assert pe.pending_manual_deletes(str(ledger)) == []

    # b が外れた: t1(manual_delete)の分だけ記録。t2 は記録しない
    res = pe.sync_frame([a], targets, {}, str(ledger), manual_delete=manual)
    assert len(res.deleted) == 2
    assert res.manual_delete == [(str(t1), "20240102_100000_b.jpg", b)], res.manual_delete
    items = pe.pending_manual_deletes(str(ledger))
    assert len(items) == 1
    it = items[0]
    assert (it["target"], it["name"], it["source"]) == (str(t1), "20240102_100000_b.jpg", b)
    assert len(it["deleted_at"]) == 19 and it["deleted_at"][10] == "T"
    lines = pe.manual_delete_report(res, str(ledger))
    assert lines == ["Amazon 側（frame1）で手で消す写真：1枚（いま全部で 1枚）",
                     "  20240102_100000_b.jpg"], lines
    assert pe.manual_delete_report(pe.SyncResult(), str(ledger)) == []

    # a も外れた: 足される(同期先ごと・消した日時の順。b は前に消した扱いに書き換えておく)
    data = json.loads(record.read_text(encoding="utf-8"))
    data["items"][0]["deleted_at"] = "2026-01-01T00:00:00"
    record.write_text(json.dumps(data), encoding="utf-8")
    res = pe.sync_frame([], targets, {}, str(ledger), manual_delete=manual)
    assert [n for _t, n, _s in res.manual_delete] == ["20240101_100000_a.jpg"]
    assert [x["name"] for x in pe.pending_manual_deletes(str(ledger))] == \
        ["20240102_100000_b.jpg", "20240101_100000_a.jpg"]
    lines = pe.manual_delete_report(res, str(ledger), limit=0)
    assert lines == ["Amazon 側（frame1）で手で消す写真：1枚（いま全部で 2枚）", "  ほか 1 枚"], lines

    # b をまた入れ直した: 一覧から外れる(クラウドにまだあるので消さなくてよい)
    res = pe.sync_frame([b], targets, {}, str(ledger), manual_delete=manual)
    assert len(res.added) == 2 and not res.manual_delete
    assert [x["name"] for x in pe.pending_manual_deletes(str(ledger))] == ["20240101_100000_a.jpg"]

    # manual_delete を渡さなければ記録しない(ほかの同期先は今までどおり)
    res = pe.sync_frame([], targets, {}, str(ledger))
    assert len(res.deleted) == 2 and not res.manual_delete
    assert len(pe.pending_manual_deletes(str(ledger))) == 1

    # 壊れた項目は読み飛ばす
    data = json.loads(record.read_text(encoding="utf-8"))
    data["items"].append({"target": str(t1), "name": 3})
    data["items"].append("junk")
    record.write_text(json.dumps(data), encoding="utf-8")
    assert len(pe.pending_manual_deletes(str(ledger))) == 1
    record.write_text("{ broken", encoding="utf-8")
    assert pe.pending_manual_deletes(str(ledger)) == []


def test_manual_delete_clear():
    """clear_manual_deletes: 渡した項目だけ外す。None なら全部。省くと既定の場所。"""
    d, src, t1, t2, ledger = _frame_setup("frame_manual_clear")
    a = _exif_jpeg(src / "a.jpg", size=(400, 300), when="2024:01:01 10:00:00")
    b = _exif_jpeg(src / "b.jpg", size=(400, 300), when="2024:01:02 10:00:00")
    targets = [str(t1), str(t2)]
    both = [str(t1), str(t2)]
    pe.sync_frame([a, b], targets, {}, str(ledger), manual_delete=both)
    res = pe.sync_frame([], targets, {}, str(ledger), manual_delete=both)
    assert len(res.manual_delete) == 4
    items = pe.pending_manual_deletes(str(ledger))
    assert [os.path.basename(x["target"]) for x in items] == ["frame1", "frame1", "frame2", "frame2"]
    assert pe.clear_manual_deletes([items[0]], str(ledger)) == 1
    rest = pe.pending_manual_deletes(str(ledger))
    assert rest == items[1:]
    assert pe.clear_manual_deletes([], str(ledger)) == 0
    assert pe.clear_manual_deletes(None, str(ledger)) == 3
    assert pe.pending_manual_deletes(str(ledger)) == []
    assert pe.clear_manual_deletes(None, str(ledger)) == 0
    # ledger_dir を省くと default_ledger_dir() の隣(このテストでは一時フォルダ)
    assert pe.manual_delete_path() == os.path.join(_TEST_TEMP, "data", pe.MANUAL_DELETE_FILE)
    assert pe.pending_manual_deletes() == []
    assert pe.clear_manual_deletes() == 0


def _frame_snapshot(*folders):
    """フォルダの中身（相対パス → 中身と更新日時）。予行で何も変わらないことを確かめる。"""
    out = {}
    for folder in folders:
        folder = Path(folder)
        if not folder.exists():
            continue
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                out[str(p)] = (p.read_bytes(), p.stat().st_mtime_ns)
            else:
                out[str(p)] = None
    return out


def _by_target(items, target):
    return [x for x in items if os.path.normcase(x[0]) == os.path.normcase(str(target))]


def _check_plan_matches(sources, targets, settings, ledger, d, **kw):
    """予行 → 実際に同期して、同期先ごとの数が一致することを確かめる。予行では何も書かない。"""
    before = _frame_snapshot(d)
    plan = pe.plan_frame_sync(sources, targets, settings, str(ledger), **kw)
    assert _frame_snapshot(d) == before                 # 同期先・台帳・手で消す写真の一覧とも変えない
    assert plan.sources == [os.path.abspath(p) for p in dict.fromkeys(sources)]
    res = pe.sync_frame(sources, targets, settings, str(ledger), **kw)
    assert not res.failed and not res.error, res
    assert [pt.target for pt in plan.targets] == [os.path.abspath(t) for t in targets]
    for pt in plan.targets:
        assert len(pt.added) == len(_by_target(res.added, pt.target)), (pt, res)
        assert len(pt.updated) == len(_by_target(res.updated, pt.target)), (pt, res)
        assert len(pt.deleted) == len(_by_target(res.deleted, pt.target)), (pt, res)
        assert len(pt.unchanged) == len(_by_target(res.unchanged, pt.target)), (pt, res)
        assert len(pt.manual_delete) == len(_by_target(res.manual_delete, pt.target)), (pt, res)
        assert sorted(pt.deleted) == sorted((n, s) for _t, n, s in _by_target(res.deleted, pt.target))
    assert plan.changed == bool(res.added or res.updated or res.deleted)
    return plan, res


def test_plan_frame_sync():
    """予行（plan_frame_sync）: 追加・更新・削除・そのまま・手で消す数が sync_frame の結果と一致する。
    予行ではファイルも台帳も手で消す写真の一覧も変わらない。"""
    import threading
    d, src, t1, t2, ledger = _frame_setup("frame_plan")
    a = _exif_jpeg(src / "a.jpg", size=(400, 300), when="2024:01:01 10:00:00")
    b = _exif_jpeg(src / "b.jpg", size=(400, 300), when="2024:01:02 10:00:00")
    c = _exif_jpeg(src / "c.jpg", size=(400, 300), when="2024:01:03 10:00:00")
    (t1 / "手で入れた.jpg").write_bytes(b"manual photo")
    targets = [str(t1), str(t2)]
    manual = [str(t2)]
    kw = {"manual_delete": manual, "scope": [str(src)]}

    # 1回目: 全部追加（重複は1回だけ数える）
    plan, _res = _check_plan_matches([a, b, c, a], targets, {}, ledger, d, **kw)
    assert [len(pt.added) for pt in plan.targets] == [3, 3] and plan.changed
    assert plan.targets[0].summary() == "追加 3／更新 0／削除 0／そのまま 0"
    assert [pt.manual for pt in plan.targets] == [False, True]

    # 2回目: 変化なし
    plan, _res = _check_plan_matches([a, b, c], targets, {}, ledger, d, **kw)
    assert not plan.changed and [len(pt.unchanged) for pt in plan.targets] == [3, 3]

    # b が変わった（更新）・c が外れた（削除。t2 は手で消す）・同期先から消えていた a は置き直す
    _exif_jpeg(src / "b.jpg", size=(500, 300), when="2024:01:02 10:00:00")
    name_a = next(n for n, r in pe.load_ledger(str(ledger), str(t1)).items() if r["source"] == a)
    os.remove(t1 / name_a)
    plan, res = _check_plan_matches([a, b], targets, {}, ledger, d, **kw)
    p1, p2 = plan.targets
    assert (len(p1.added), len(p1.updated), len(p1.deleted), len(p1.unchanged)) == (1, 1, 1, 0)
    assert (len(p2.added), len(p2.updated), len(p2.deleted), len(p2.unchanged)) == (0, 1, 1, 1)
    assert p1.manual_delete == [] and p2.manual_delete == [("20240103_100000_c.jpg", c)]
    assert p2.summary() == "追加 0／更新 1／削除 1（Amazon 側で手で消す 1）／そのまま 1"
    assert len(pe.pending_manual_deletes(str(ledger))) == 1

    # 縮小の設定が変わったら全部作り直し
    plan, _res = _check_plan_matches([a, b], targets, {"quality": 60}, ledger, d, **kw)
    assert [len(pt.updated) for pt in plan.targets] == [2, 2]

    # 手で差し替えられたもの（そのままでも削除でもない）・keep の下は消さない
    replaced = t1 / name_a
    replaced.write_bytes(b"replaced by hand")
    plan, res = _check_plan_matches([b], targets, {"quality": 60}, ledger, d,
                                    keep=[str(src / "nothing.jpg")], **kw)
    assert plan.targets[0].kept == [name_a] and not plan.targets[0].deleted
    assert [len(pt.deleted) for pt in plan.targets] == [0, 1]
    assert replaced.read_bytes() == b"replaced by hand"
    plan = pe.plan_frame_sync([], targets, {"quality": 60}, str(ledger), keep=[b], **kw)
    assert not plan.changed

    # scope の外は消さない。同期先がまだ無くても作らない
    t3 = d / "frame3"
    plan = pe.plan_frame_sync([], targets + [str(t3)], {"quality": 60}, str(ledger),
                              scope=[str(d / "elsewhere")])
    assert not plan.changed and not t3.exists()
    plan = pe.plan_frame_sync([b], [str(t3)], {}, str(ledger))
    assert [len(pt.added) for pt in plan.targets] == [1] and not t3.exists()

    # 元の写真が無い・RAW は数えない。同期先が空ならエラー。中止
    raw = src / "r.ARW"
    raw.write_bytes(b"raw")
    plan = pe.plan_frame_sync([str(src / "missing.jpg"), str(raw)], [str(t3)], {}, str(ledger))
    assert plan.missing == [str(src / "missing.jpg")] and plan.skipped_raw == [str(raw)]
    assert not plan.changed
    assert pe.plan_frame_sync([a], [], {}, str(ledger)).error == \
        "同期先がありません（photo_ratings.json の frame_targets）"
    ev = threading.Event()
    ev.set()
    plan = pe.plan_frame_sync([b], targets, {}, str(ledger), cancel=ev)
    assert plan.cancelled and not plan.changed
    # ledger_dir を省くと既定の場所（このテストでは一時フォルダ）の台帳を読む
    plan = pe.plan_frame_sync([b], [str(t1)], {})
    assert [len(pt.added) for pt in plan.targets] == [1]


def test_sync_frame_heic_raw_and_formats():
    from PIL import Image
    d, src, t1, _t2, ledger = _frame_setup("frame_formats")
    png = src / "p.png"
    Image.new("RGBA", (300, 200), (255, 0, 0, 0)).save(png)
    os.utime(png, (1700000000, 1700000000))
    heic = src / "h.heic"
    heic.write_bytes(b"not really heic")
    raw = src / "r.ARW"
    raw.write_bytes(b"raw")
    saved = list(pe._heif_state)
    pe._heif_state[:] = [False]
    try:
        res = pe.sync_frame([str(png), str(heic), str(raw)], [str(t1)], {}, str(ledger))
    finally:
        pe._heif_state[:] = saved
    assert res.skipped_heic == [str(heic)] and res.skipped_raw == [str(raw)]
    notes = res.notes()
    assert any(n.startswith("HEIC は読めないので飛ばしました（1 枚") for n in notes), notes
    assert len(res.added) == 1
    out = t1 / res.added[0][1]
    assert out.name.endswith("_p.jpg")
    with Image.open(out) as im:
        assert im.mode == "RGB" and im.getpixel((5, 5))[1] > 200   # 透明は白で埋める


def test_mitene_collect_and_finish():
    d = _tmpdir("mitene")
    root = d / "root"
    root.mkdir()
    dest = d / "みてね用"
    a = _rated(root / "a.jpg", 5, when="2024:05:05 05:05:05")
    b = _rated(root / "b.jpg", 5, when=None)
    c = _rated(root / "c.jpg", 5)
    _rated(root / "done.jpg", 5, ["みてね済"])
    _rated(root / "four.jpg", 4)
    old = time.mktime((2022, 2, 2, 2, 2, 2, 0, 0, -1))
    os.utime(b, (old, old))
    cfg = pc.default_config()
    cfg["mitene_dir"] = str(dest)
    found = pe.collect(root, pe.mitene_expr(cfg), cfg)
    assert [os.path.basename(p) for p in found.paths] == ["a.jpg", "b.jpg", "c.jpg"]
    res = pe.collect_mitene(found.paths, dest)
    names = sorted(n for n, _s in res.copied)
    assert names == sorted(["20240505_050505_a.jpg", "20220202_020202_b.jpg",
                            "20240903_022344_c.jpg"]), names
    # そのままコピー(縮小しない)
    assert (dest / "20240505_050505_a.jpg").read_bytes() == Path(a).read_bytes()
    man = pe.load_manifest(str(dest))
    assert sorted(it["source"] for it in man) == sorted([a, b, c])
    # 2回目は飛ばす
    res = pe.collect_mitene(found.paths, dest)
    assert not res.copied and len(res.already) == 3
    # 上げないことにした1枚を集めた先から消しておく
    os.remove(dest / "20240903_022344_c.jpg")

    exe = _exiftool()
    if exe:
        session = pw.ExifToolSession(exe, journal_path=os.path.join(_TEST_TEMP, "mitene.jsonl"))
    else:
        print("      test_mitene_collect_and_finish: ExifTool が無いのでフェイクで書きます")
        session = _FakeKeywordSession(fail={"b.jpg"})
    try:
        if exe:
            os.chmod(b, 0o444)        # 読み取り専用で1枚失敗させる
        try:
            fin = pe.finish_mitene(str(dest), session, "みてね済")
        finally:
            os.chmod(b, 0o666)
    finally:
        if exe:
            session.close()
    assert fin.marked == [a], fin
    assert fin.missing == [c]
    assert [p for p, _r in fin.failed] == [b]
    assert not (dest / "20240505_050505_a.jpg").exists()
    assert (dest / "20220202_020202_b.jpg").exists()      # 失敗したものは残る
    assert [it["source"] for it in pe.load_manifest(str(dest))] == [b]
    if exe:
        assert "みてね済" in px.read_meta(a).keywords
        assert "みてね済" not in px.read_meta(c).keywords
        assert px.read_meta(a).rating == 5
    # 残りを片付けると一覧ファイルも消える
    fin = pe.finish_mitene(str(dest), _FakeKeywordSession(), "みてね済")
    assert fin.marked == [b] and not pe.load_manifest(str(dest))
    assert not (dest / pe.MITENE_MANIFEST).exists()
    assert pe.finish_mitene(str(dest), _FakeKeywordSession(), "").error


class _FakeKeywordSession:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    def set_keyword(self, paths, keyword, on):
        out = []
        for p in paths:
            self.calls.append((p, keyword, on))
            before = px.read_meta(p)
            if os.path.basename(p) in self.fail:
                out.append(pw.WriteResult(p, False, "読み取り専用のファイルです", before=before))
            else:
                after = PhotoMeta(before.rating, before.keywords + (keyword,))
                out.append(pw.WriteResult(p, True, before=before, after=after))
        return out


# ------------------------------------------------------------------ metacache

def test_metacache_hit_does_not_read_file():
    """ヒットしたら読み取り関数（＝ファイルを開く処理）を呼ばない。置き場所も差し替えられる。"""
    d = _tmpdir("cache_hit")
    path = _make_jpeg(d / "a.jpg", _packet(4, ["旅行"]))
    db = d / "cache.sqlite"
    cache = mc.MetaCache(str(db))
    assert cache.db_path == str(db)                 # 置き場所を差し替えられる
    calls = []

    def reader(p):
        calls.append(p)
        return px.read_meta(p)

    assert cache.read_cached(path, reader) == PhotoMeta(4, ("旅行",))
    assert len(calls) == 1 and db.exists()          # DB は指定した場所にできる
    assert cache.read_cached(path, reader) == PhotoMeta(4, ("旅行",))
    assert len(calls) == 1                          # 2回目はファイルを開いていない
    assert cache.get(path) == PhotoMeta(4, ("旅行",))
    # 読めなかったもの（None）は覚えない
    assert cache.read_cached(str(d / "none.jpg"), reader) is None
    assert cache.get(str(d / "none.jpg")) is None


def test_metacache_revalidates_on_change():
    """サイズや更新日時が変わったら読み直す。"""
    d = _tmpdir("cache_change")
    path = _make_jpeg(d / "a.jpg", _packet(1))
    cache = mc.MetaCache(str(d / "cache.sqlite"))
    calls = []

    def reader(p):
        calls.append(p)
        return px.read_meta(p)

    assert cache.read_cached(path, reader).rating == 1
    # 中身を書き換える（サイズも更新日時も変わる）
    _make_jpeg(path, _packet(5, ["山"]))
    assert cache.read_cached(path, reader) == PhotoMeta(5, ("山",))
    assert len(calls) == 2
    # 中身は同じでも更新日時だけ変えたら読み直す
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert cache.read_cached(path, reader).rating == 5
    assert len(calls) == 3


def test_metacache_raw_sidecar():
    """RAW はサイドカーの変化でも読み直す（サイドカーが後から作られた場合も）。"""
    d = _tmpdir("cache_raw")
    raw = d / "r.ARW"
    raw.write_bytes(b"II*\x00dummy raw")
    cache = mc.MetaCache(str(d / "cache.sqlite"))
    assert cache.read_cached(str(raw)) == PhotoMeta()
    (d / "r.xmp").write_bytes(_packet(5))
    assert cache.read_cached(str(raw)) == PhotoMeta(5, ())
    (d / "r.xmp").write_bytes(_packet(3, ["海"]))
    st = os.stat(d / "r.xmp")
    os.utime(d / "r.xmp", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert cache.read_cached(str(raw)) == PhotoMeta(3, ("海",))


def test_metacache_update_overrides_stat_match():
    """書き込み後（更新日時は元に戻る）の値は、update でキャッシュに反映される。"""
    d = _tmpdir("cache_update")
    path = _make_jpeg(d / "a.jpg", _packet(1))
    cache = mc.MetaCache(str(d / "cache.sqlite"))
    assert cache.read_cached(path).rating == 1
    cache.update(path, PhotoMeta(5, ("みてね済",)))
    assert cache.get(path) == PhotoMeta(5, ("みてね済",))
    assert cache.read_cached(path) == PhotoMeta(5, ("みてね済",))   # ファイルは★1のまま
    cache.update(path, None)                        # 読み直せなかったら古い値を消す
    assert cache.get(path) is None


def test_metacache_writer_session_updates():
    """ExifToolSession（metacache 付き）で書くと、キャッシュも新しい値になる。"""
    exe = _exiftool()
    if not exe:
        print("skip  test_metacache_writer_session_updates: ExifTool が見つかりません")
        return
    d = _tmpdir("cache_writer")
    path = _make_jpeg(d / "a.jpg", _packet(1))
    cache = mc.MetaCache(str(d / "cache.sqlite"))
    assert cache.read_cached(path).rating == 1
    jp = os.path.join(_TEST_TEMP, "cache_writer.jsonl")
    with pw.ExifToolSession(exe, journal_path=jp, metacache=cache) as et:
        res = et.set_rating([path], 4)
    assert res[0].ok, res[0].error
    # ファイルの更新日時は書き込み前のまま（writer が戻す）でも、キャッシュは★4
    assert cache.get(path) == PhotoMeta(4, ())
    assert cache.read_cached(path).rating == 4


def test_metacache_two_connections():
    """2つの接続（＝2プロセス相当）から同時に使っても壊れない。"""
    import threading
    d = _tmpdir("cache_two")
    paths = [_make_jpeg(d / f"p{i}.jpg", _packet(i % 6)) for i in range(12)]
    db = str(d / "cache.sqlite")
    errors = []

    def work(cache):
        try:
            for _ in range(10):
                for p in paths:
                    cache.read_cached(p)
                    cache.put(p, PhotoMeta(2, ()))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    caches = [mc.MetaCache(db), mc.MetaCache(db)]
    threads = [threading.Thread(target=work, args=(c,)) for c in caches]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert caches[0].get(paths[0]) == PhotoMeta(2, ())
    assert caches[1].get(paths[3]) == PhotoMeta(2, ())


def test_metacache_read_metas_parallel():
    """read_metas: 並行して読んでも結果が正しく、2回目は全部キャッシュで済む。中止も効く。"""
    import threading
    d = _tmpdir("cache_parallel")
    paths = [_make_jpeg(d / f"p{i}.jpg", _packet(i % 6)) for i in range(10)]
    cache = mc.MetaCache(str(d / "cache.sqlite"))
    results, hits, reads, cancelled = mc.read_metas(paths, cache=cache)
    assert not cancelled and hits == 0 and reads == len(paths)
    assert all(results[p] == PhotoMeta(i % 6, ()) for i, p in enumerate(paths))
    results, hits, reads, cancelled = mc.read_metas(paths, cache=cache)
    assert hits == len(paths) and reads == 0 and not cancelled
    # 中止（最初から立っている）→ 何も読まない
    cancel = threading.Event()
    cancel.set()
    results, hits, reads, cancelled = mc.read_metas(
        paths, cache=mc.MetaCache(str(d / "other.sqlite")), cancel=cancel)
    assert cancelled and reads == 0 and results == {}


# ------------------------------------------------------------------ 実行器

def main():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
            if os.environ.get("GITHUB_ACTIONS"):
                tb = traceback.format_exc().replace("%", "%25").replace("\r", "%0D")
                print(f"::error title={name}::" + tb[-900:].replace("\n", "%0A"))
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice::{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS"):
        try:
            os.dup2(1, 2)
            sys.stderr = sys.stdout
        except Exception:  # noqa: BLE001
            pass
    code = main()
    shutil.rmtree(_TEST_TEMP, ignore_errors=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
