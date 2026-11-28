# filer_formats.py
# あっふのプレビュー用に、画像・動画以外の形式を読む部品(Qt に依存するのはレンダリングだけ)。
#
#   - xlsx: 標準の zipfile + xml.etree だけで読み、シート名の一覧と先頭シートの先頭部分を
#           HTML の表にする(openpyxl は使わない)
#   - stl / 3mf: numpy で三角形を読み、QPainter で斜め上からのフラットシェーディングの絵にする
#   - shd(Shade 3D): 独自形式でサムネも無いので、ヘッダから版だけ取る
#
# 読み取りスレッドから呼ぶ(UI を固めないよう、重い所は件数・サイズで諦める)。
# 失敗は例外(FormatError / OSError)で返す。呼び出し側(filer_preview.load_preview)が
# filer_error.log に残して info 表示へ落とす。
import datetime
import html
import math
import os
import re
import struct
import zipfile
import xml.etree.ElementTree as ET

XLSX_MAX_ROWS = 200
XLSX_MAX_COLS = 30
XLSX_MAX_BYTES = 200 * 1024 * 1024       # この大きさを超える部品(展開後)は読まない
XLSX_MAX_SHARED = 300_000                # 共有文字列はこの件数まで

MODEL_MAX_BYTES = 100 * 1024 * 1024      # これより大きい STL / 3MF は読み込みを諦める
MODEL_MAX_DRAW = 200_000                 # これを超えたら三角形を間引いて描く
MODEL_BACKGROUND = (38, 41, 50)          # 暗めの落ち着いた背景
MODEL_BASE_COLOR = (132, 176, 222)


class FormatError(Exception):
    """形式として読めない(壊れている・非対応)。メッセージは画面に出せる日本語。"""


# ---------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _col_index(ref: str) -> int:
    """"AB12" → 列番号(0 始まり)。"""
    n = 0
    for ch in ref:
        if "A" <= ch <= "Z":
            n = n * 26 + (ord(ch) - 64)
        else:
            break
    return n - 1


def col_letters(index: int) -> str:
    s = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        s = chr(65 + rem) + s
    return s


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > XLSX_MAX_BYTES:
        raise FormatError(f"{name} が大きすぎます（{info.file_size // (1024 * 1024)}MB）")
    return zf.read(name)


def _shared_strings(zf: zipfile.ZipFile) -> list:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    out = []
    info = zf.getinfo("xl/sharedStrings.xml")
    if info.file_size > XLSX_MAX_BYTES:
        return out
    with zf.open("xl/sharedStrings.xml") as f:
        for _ev, el in ET.iterparse(f, events=("end",)):
            if _local(el.tag) == "si":
                parts = []
                for child in el:       # ふりがな(rPh)は読みなので飛ばす
                    cn = _local(child.tag)
                    if cn == "t":
                        parts.append(child.text or "")
                    elif cn == "r":
                        parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
                out.append("".join(parts))
                el.clear()
                if len(out) >= XLSX_MAX_SHARED:
                    break
    return out


def _date_style_flags(zf: zipfile.ZipFile) -> list:
    """cellXfs の各スタイル(index)が日付の表示形式か。"""
    if "xl/styles.xml" not in zf.namelist():
        return []
    root = ET.fromstring(_read_member(zf, "xl/styles.xml"))
    custom = {}
    for el in root.iter():
        if _local(el.tag) == "numFmt":
            custom[int(el.get("numFmtId", "0"))] = (el.get("formatCode") or "").lower()
    builtin = set(range(14, 23)) | {27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 45, 46, 47, 50, 51,
                                    52, 53, 54, 55, 56, 57, 58}
    flags = []
    for xfs in root:
        if _local(xfs.tag) != "cellXfs":
            continue
        for xf in xfs:
            fid = int(xf.get("numFmtId", "0") or 0)
            if fid in custom:
                code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", custom[fid])
                flags.append(bool(re.search(r"[ymdhs]", code)))
            else:
                flags.append(fid in builtin)
    return flags


def _format_number(text: str) -> str:
    try:
        v = float(text)
    except ValueError:
        return text
    if v != v or v in (float("inf"), float("-inf")):
        return text
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return f"{v:.10g}"


def _excel_date(text: str) -> str:
    try:
        v = float(text)
        base = datetime.datetime(1899, 12, 30)
        dt = base + datetime.timedelta(days=v)
    except (ValueError, OverflowError):
        return text
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and v == int(v):
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _sheet_targets(zf: zipfile.ZipFile):
    """[(シート名, zip 内のパス)] をブックの順で。"""
    wb = ET.fromstring(_read_member(zf, "xl/workbook.xml"))
    rels = {}
    if "xl/_rels/workbook.xml.rels" in zf.namelist():
        for el in ET.fromstring(_read_member(zf, "xl/_rels/workbook.xml.rels")):
            rels[el.get("Id")] = el.get("Target") or ""
    sheets = []
    for el in wb.iter():
        if _local(el.tag) == "sheet":
            rid = None
            for k, v in el.attrib.items():
                if _local(k) == "id":
                    rid = v
            target = rels.get(rid, "")
            if target.startswith("/"):
                path = target.lstrip("/")
            else:
                path = "xl/" + target
            sheets.append((el.get("name") or "?", os.path.normpath(path).replace("\\", "/")))
    return sheets


def read_xlsx(path: str):
    """(HTML の文字列, 注記)。壊れていれば FormatError。"""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise FormatError(f"xlsx として開けません（{e}）") from e
    with zf:
        names = zf.namelist()
        if "xl/workbook.xml" not in names:
            raise FormatError("xlsx ではありません（xl/workbook.xml が無い）")
        try:
            sheets = _sheet_targets(zf)
            if not sheets:
                raise FormatError("シートがありません")
            shared = _shared_strings(zf)
            date_flags = _date_style_flags(zf)
            first_name, first_path = sheets[0]
            if first_path not in names:
                raise FormatError(f"シート本体が見つかりません（{first_path}）")
            rows, truncated, max_col = _read_sheet(zf, first_path, shared, date_flags)
        except ET.ParseError as e:
            raise FormatError(f"xlsx の XML が壊れています（{e}）") from e
    return _rows_to_html(sheets, rows, truncated, max_col), f"xlsx {len(sheets)}シート"


def _read_sheet(zf, sheet_path, shared, date_flags):
    rows = []                     # [(行番号, {列: 文字列})]
    max_col = 0
    truncated = False
    with zf.open(sheet_path) as f:
        row_cells = None
        row_no = 0
        for ev, el in ET.iterparse(f, events=("start", "end")):
            tag = _local(el.tag)
            if ev == "start":
                if tag == "row":
                    row_cells = {}
                    row_no = int(el.get("r") or (rows[-1][0] + 1 if rows else 1))
                continue
            if tag == "c" and row_cells is not None:
                ref = el.get("r") or ""
                col = _col_index(ref) if ref else len(row_cells)
                if col < XLSX_MAX_COLS:
                    row_cells[col] = _cell_text(el, shared, date_flags)
                    max_col = max(max_col, col + 1)
                else:
                    truncated = True
                el.clear()
            elif tag == "row":
                if row_cells:
                    rows.append((row_no, row_cells))
                row_cells = None
                el.clear()
                if len(rows) >= XLSX_MAX_ROWS:
                    truncated = True
                    break
    return rows, truncated, max_col


def _cell_text(el, shared, date_flags) -> str:
    t = el.get("t") or "n"
    value = ""
    inline = []
    for child in el:
        name = _local(child.tag)
        if name == "v":
            value = child.text or ""
        elif name == "is":
            inline = [x.text or "" for x in child.iter() if _local(x.tag) == "t"]
    if t == "inlineStr":
        return "".join(inline)
    if t == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if t == "b":
        return "TRUE" if value.strip() == "1" else "FALSE"
    if t in ("str", "e"):
        return value
    if not value:
        return ""
    try:
        style = int(el.get("s") or 0)
    except ValueError:
        style = 0
    if style < len(date_flags) and date_flags[style]:
        return _excel_date(value)
    return _format_number(value)


def _rows_to_html(sheets, rows, truncated, max_col) -> str:
    esc = html.escape
    out = ["<html><body>"]
    out.append("<p><b>シート</b>: " + " ／ ".join(
        (f"<b>[{esc(n)}]</b>" if i == 0 else esc(n)) for i, (n, _p) in enumerate(sheets)) + "</p>")
    if not rows:
        out.append("<p>（先頭のシートは空です）</p></body></html>")
        return "".join(out)
    out.append('<table border="1" cellspacing="0" cellpadding="3">')
    out.append("<tr><th></th>" + "".join(f"<th>{col_letters(c)}</th>" for c in range(max_col))
               + "</tr>")
    for row_no, cells in rows:
        out.append(f"<tr><th>{row_no}</th>" + "".join(
            f"<td>{esc(cells.get(c, ''))}</td>" for c in range(max_col)) + "</tr>")
    out.append("</table>")
    if truncated:
        out.append(f"<p>…（先頭 {XLSX_MAX_ROWS} 行 × {XLSX_MAX_COLS} 列まで）</p>")
    out.append("</body></html>")
    return "".join(out)


# ---------------------------------------------------------------
# STL / 3MF
# ---------------------------------------------------------------
def _np():
    import numpy as np

    return np


def load_stl(path: str):
    """STL(バイナリ / ASCII)を (三角形 (N,3,3) float32, 三角形の総数) で返す。"""
    np = _np()
    size = os.path.getsize(path)
    if size > MODEL_MAX_BYTES:
        raise FormatError(f"ファイルが大きすぎるため読み込みません（{size // (1024 * 1024)}MB。"
                          f"上限 {MODEL_MAX_BYTES // (1024 * 1024)}MB）")
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if count > 0 and 84 + 50 * count == len(data):
            dt = np.dtype([("n", "<f4", 3), ("v", "<f4", 9), ("a", "<u2")])
            arr = np.frombuffer(data, dtype=dt, count=count, offset=84)
            return arr["v"].reshape(-1, 3, 3).astype(np.float32), int(count)
    found = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
    if not found or len(found) % 3:
        raise FormatError("STL として読めません（三角形が見つからない・壊れている）")
    try:
        verts = np.array(found, dtype=np.float32)
    except ValueError as e:
        raise FormatError(f"STL の座標を読めません（{e}）") from e
    return verts.reshape(-1, 3, 3), len(verts) // 3


_UNIT_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4,
            "foot": 304.8, "meter": 1000.0}


def _parse_transform(text):
    """3MF の transform(12 個の数)→ 4x3 行列。無ければ None。"""
    if not text:
        return None
    np = _np()
    try:
        vals = [float(x) for x in text.split()]
    except ValueError:
        return None
    if len(vals) != 12:
        return None
    return np.array(vals, dtype=np.float64).reshape(4, 3)


def load_3mf(path: str):
    """3MF を (三角形 (N,3,3) float32 [mm], 三角形の総数) で返す。"""
    np = _np()
    size = os.path.getsize(path)
    if size > MODEL_MAX_BYTES:
        raise FormatError(f"ファイルが大きすぎるため読み込みません（{size // (1024 * 1024)}MB。"
                          f"上限 {MODEL_MAX_BYTES // (1024 * 1024)}MB）")
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise FormatError(f"3MF として開けません（{e}）") from e
    with zf:
        model_name = None
        if "_rels/.rels" in zf.namelist():
            try:
                for el in ET.fromstring(zf.read("_rels/.rels")):
                    if (el.get("Type") or "").endswith("3dmodel"):
                        model_name = (el.get("Target") or "").lstrip("/")
                        break
            except ET.ParseError:
                pass
        if not model_name or model_name not in zf.namelist():
            model_name = next((n for n in zf.namelist()
                               if n.lower().endswith(".model")), None)
        if not model_name:
            raise FormatError("3MF の 3D/3dmodel.model が見つかりません")
        info = zf.getinfo(model_name)
        if info.file_size > 4 * MODEL_MAX_BYTES:
            raise FormatError(f"モデルが大きすぎます（展開後 {info.file_size // (1024 * 1024)}MB）")
        try:
            root = ET.fromstring(zf.read(model_name))
        except ET.ParseError as e:
            raise FormatError(f"3MF の XML が壊れています（{e}）") from e
    unit = _UNIT_MM.get((root.get("unit") or "millimeter").lower(), 1.0)
    objects = {}          # id → ("mesh", verts, tris) か ("comp", [(id, 変換)])
    build = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "object":
            oid = el.get("id")
            mesh = comps = None
            for ch in el.iter():
                cn = _local(ch.tag)
                if cn == "mesh":
                    mesh = ch
                elif cn == "components":
                    comps = ch
            if mesh is not None:
                v_el = next((c for c in mesh if _local(c.tag) == "vertices"), None)
                t_el = next((c for c in mesh if _local(c.tag) == "triangles"), None)
                if v_el is None or t_el is None:
                    continue
                verts = np.array([[float(v.get("x", 0)), float(v.get("y", 0)),
                                   float(v.get("z", 0))] for v in v_el], dtype=np.float64)
                tris = np.array([[int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))]
                                 for t in t_el], dtype=np.int64)
                objects[oid] = ("mesh", verts, tris)
            elif comps is not None:
                objects[oid] = ("comp", [(c.get("objectid"), _parse_transform(c.get("transform")))
                                         for c in comps if _local(c.tag) == "component"])
        elif name == "item":
            build.append((el.get("objectid"), _parse_transform(el.get("transform"))))
    if not build:
        build = [(oid, None) for oid, o in objects.items() if o[0] == "mesh"]
    chunks = []

    def emit(oid, matrix, depth):
        obj = objects.get(oid)
        if obj is None or depth > 8:
            return
        if obj[0] == "mesh":
            verts, tris = obj[1], obj[2]
            if len(tris) == 0 or tris.max() >= len(verts) or tris.min() < 0:
                return
            v = verts
            if matrix is not None:
                v = verts @ matrix[:3] + matrix[3]
            chunks.append((v[tris] * unit).astype(np.float32))
        else:
            for child, tf in obj[1]:
                combined = _compose(tf, matrix)
                emit(child, combined, depth + 1)

    for oid, tf in build:
        emit(oid, tf, 0)
    if not chunks:
        raise FormatError("3MF に三角形のメッシュがありません")
    tri = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    return tri, len(tri)


def _compose(inner, outer):
    """行ベクトル規約: 点 → inner → outer の順に当てる 4x3 行列。"""
    np = _np()
    if inner is None:
        return outer
    if outer is None:
        return inner
    m1 = np.hstack([inner, np.array([[0], [0], [0], [1.0]])])
    m2 = np.hstack([outer, np.array([[0], [0], [0], [1.0]])])
    return (m1 @ m2)[:, :3]


def load_model(path: str):
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext == "stl":
        return load_stl(path)
    if ext == "3mf":
        return load_3mf(path)
    raise FormatError("3D モデルの形式ではありません")


def model_dimensions(tris):
    """(幅 x, 奥行 y, 高さ z) の外寸。"""
    np = _np()
    flat = tris.reshape(-1, 3)
    lo, hi = flat.min(axis=0), flat.max(axis=0)
    d = (hi - lo).astype(float)
    return float(d[0]), float(d[1]), float(d[2])


def render_model(tris, box, yaw_deg=35.0, elev_deg=28.0):
    """三角形 (N,3,3) を QImage にする(読み取りスレッドで呼んでよい)。

    Z が上。カメラは少し斜め上(等角風)。三角形は遠い順に塗る(画家のアルゴリズム)。
    法線と固定の光の向きでフラットシェーディング(裏表は区別しない)。"""
    np = _np()
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF

    bw, bh = max(int(box[0]), 64), max(int(box[1]), 64)
    img = QImage(bw, bh, QImage.Format_RGB32)
    img.fill(QColor(*MODEL_BACKGROUND))
    if len(tris) == 0:
        return img
    center = (tris.reshape(-1, 3).min(axis=0) + tris.reshape(-1, 3).max(axis=0)) / 2.0
    p = (tris - center).astype(np.float64)
    yaw, elev = math.radians(yaw_deg), math.radians(elev_deg)
    ca, sa, ce, se = math.cos(yaw), math.sin(yaw), math.cos(elev), math.sin(elev)
    # ワールド → 視点座標(sx: 右, sy: 上, sd: 奥ほど大きい)
    xr = p[..., 0] * ca - p[..., 1] * sa
    yr = p[..., 0] * sa + p[..., 1] * ca
    z = p[..., 2]
    sx = xr
    sy = z * ce + yr * se
    sd = yr * ce - z * se
    # 法線(視点座標)
    e1 = p[:, 1] - p[:, 0]
    e2 = p[:, 2] - p[:, 0]
    n = np.cross(e1, e2)
    nx = n[:, 0] * ca - n[:, 1] * sa
    ny = n[:, 0] * sa + n[:, 1] * ca
    nz = n[:, 2]
    nsx, nsy, nsd = nx, nz * ce + ny * se, ny * ce - nz * se
    length = np.sqrt(nsx ** 2 + nsy ** 2 + nsd ** 2)
    length[length == 0] = 1.0
    light = np.array([-0.4, 0.55, -0.75])
    light /= np.linalg.norm(light)
    lam = np.abs((nsx * light[0] + nsy * light[1] + nsd * light[2]) / length)
    shade = 0.28 + 0.72 * lam
    # 画面に収める
    min_x, max_x = sx.min(), sx.max()
    min_y, max_y = sy.min(), sy.max()
    span_x, span_y = max(max_x - min_x, 1e-9), max(max_y - min_y, 1e-9)
    margin = 14
    scale = min((bw - 2 * margin) / span_x, (bh - 2 * margin) / span_y)
    off_x = (bw - span_x * scale) / 2 - min_x * scale
    off_y = (bh + span_y * scale) / 2 + min_y * scale     # y は下向きに反転
    px = sx * scale + off_x
    py = off_y - sy * scale
    order = np.argsort(-sd.mean(axis=1))        # 奥から手前へ
    base = np.array(MODEL_BASE_COLOR, dtype=np.float64)
    colors = np.clip(base[None, :] * shade[:, None], 0, 255).astype(np.int32)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing, True)
    last = None
    pen = QPen()
    pen.setWidthF(0.6)
    px_l, py_l = px.tolist(), py.tolist()
    col_l = colors.tolist()
    for i in order.tolist():
        c = col_l[i]
        key = (c[0] >> 2, c[1] >> 2, c[2] >> 2)
        if key != last:
            qc = QColor(c[0], c[1], c[2])
            painter.setBrush(qc)
            pen.setColor(qc)
            painter.setPen(pen)
            last = key
        a, b = px_l[i], py_l[i]
        painter.drawPolygon(QPolygonF([QPointF(a[0], b[0]), QPointF(a[1], b[1]),
                                       QPointF(a[2], b[2])]))
    painter.end()
    return img


def model_preview(path: str, box):
    """(QImage, 注記)。三角形が多すぎれば間引いて描く。"""
    np = _np()
    tris, total = load_model(path)
    if total == 0:
        raise FormatError("三角形がありません")
    w, d, h = model_dimensions(tris)
    shown = tris
    note = f"{total:,} 三角形 / {w:.1f}x{d:.1f}x{h:.1f} mm"
    if total > MODEL_MAX_DRAW:
        idx = np.linspace(0, total - 1, MODEL_MAX_DRAW).astype(np.int64)
        shown = tris[idx]
        note += f" / {MODEL_MAX_DRAW:,} に間引き表示"
    return render_model(shown, box), note


# ---------------------------------------------------------------
# shd(Shade 3D)
# ---------------------------------------------------------------
def shd_header(path: str):
    """(ヘッダの文字列, 版) 。先頭の ASCII から 'Shade 3D ... ver.x.y.z' を探す。"""
    with open(path, "rb") as f:
        head = f.read(256)
    m = re.search(rb"Shade[ -~]{0,80}", head)
    if not m:
        raise FormatError("Shade 3D のヘッダが見つかりません")
    text = m.group(0).decode("ascii", errors="replace").strip()
    vm = re.search(r"ver\.?\s*([0-9][0-9.]*)", text, re.IGNORECASE)
    return text, (vm.group(1).rstrip(".") if vm else "")
