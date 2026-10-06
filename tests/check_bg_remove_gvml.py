# tests/check_bg_remove_gvml.py
# 「アイコンに分ける」でクリップボードへ載せたものを、実際の Excel に貼ると
# アイコンの数だけ別々の図(Shape.Type = 13 msoPicture)になり、位置関係とアルファが
# 保たれることを確かめる。
#
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe tests\check_bg_remove_gvml.py
#
# - 実画面のプラットフォームで動かす(offscreen にはクリップボードが無い)。窓は出さない。
# - Excel は Visible=False で起こし、保存せずに閉じて Quit する(画面には出ない)。
#   貼った結果のアルファは、一時フォルダへ xlsx で保存し、中の画像が RGBA のままかで見る。
# - **本物のクリップボードを一瞬書き換える。** 走らせる前の中身は clipboard_format の
#   退避で取っておき、終わったら書き戻す(check_bg_remove_clipboard.py と同じ)。
# - Excel が貼り付けでデータを取りに来る間、このプロセスはクリップボードの持ち主として
#   応答しなければならない。PowerShell を待つ間も Qt のイベントを回し続けている。
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

import bg_remove  # noqa: E402
import clipboard_format  # noqa: E402
from check_bg_remove_clipboard import list_formats, pump  # noqa: E402

EXCEL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$out = @{ shapes = @() }
$xl = New-Object -ComObject Excel.Application
$xl.Visible = $false; $xl.DisplayAlerts = $false
try {
  $wb = $xl.Workbooks.Add(); $ws = $wb.Worksheets.Item(1); $ws.Paste()
  foreach ($s in $ws.Shapes) {
    $out.shapes += @{ name = $s.Name; type = $s.Type; left = $s.Left; top = $s.Top;
                      width = $s.Width; height = $s.Height }
  }
  $wb.SaveAs('__XLSX__', 51)
  $wb.Close($false)
} finally {
  $xl.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($xl)
}
$out | ConvertTo-Json -Depth 4 -Compress
"""


def make_sheet():
    """3つのアイコン(1つは半透明の縁付き)を並べた透過画像。"""
    rgba = np.zeros((220, 400, 4), np.uint8)
    rgba[20:120, 20:120] = (220, 40, 40, 255)
    rgba[40:140, 160:260] = (40, 160, 40, 255)
    rgba[40:140, 160:164, 3] = 128            # 半透明の縁
    rgba[100:200, 290:380] = (40, 40, 200, 255)
    return rgba


def run_excel(xlsx_path):
    script = EXCEL_SCRIPT.replace("__XLSX__", str(xlsx_path))
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0x08000000)
    deadline = time.time() + 120
    while proc.poll() is None and time.time() < deadline:
        pump(100)  # 貼り付けでデータを取りに来るので、待つ間もイベントを回す
    if proc.poll() is None:
        proc.kill()
        raise TimeoutError("Excel が 120 秒で終わりませんでした")
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode("cp932", "replace"))
    return json.loads(out.decode("cp932", "replace").strip().splitlines()[-1])


def main() -> int:
    app = QGuiApplication([sys.argv[0]])  # noqa: F841
    if not clipboard_format.take_snapshot():
        print("いまのクリップボードを退避できないので、書き換えずに中止します")
        return 2
    failures = []
    workdir = Path(tempfile.mkdtemp(prefix="bg_remove_gvml_"))
    xlsx = workdir / "pasted.xlsx"
    try:
        rgba = make_sheet()
        icons = bg_remove.split_icons(rgba[..., 3], 12, 16)
        print("アイコン:", [i["box"] for i in icons])
        gvml = bg_remove.gvml_from_rgba(rgba, icons)
        bg_remove.set_clipboard_image(bg_remove.array_to_qimage(rgba),
                                      extra={bg_remove.GVML_MIME: gvml})
        pump(200)
        names = [name for _fmt, name in list_formats()]
        print("載った形式:", ", ".join(names))
        for required in ("Art::GVML ClipFormat", "PNG", "CF_DIB"):
            if required not in names:
                failures.append(f"{required} が載っていない")

        result = run_excel(xlsx)
        shapes = result.get("shapes") or []
        if isinstance(shapes, dict):
            shapes = [shapes]
        print(f"Excel に貼った図: {len(shapes)} 個")
        for shape in shapes:
            print("  {name} type={type} L={left} T={top} W={width} H={height}".format(**shape))
        if len(shapes) != len(icons):
            failures.append(f"図の数が {len(shapes)}(期待 {len(icons)})")
        if any(int(s["type"]) != 13 for s in shapes):
            failures.append("msoPicture(13) でない図がある")
        # 位置関係: px を pt(0.75 倍)にして、左上の図からの相対位置が合っているか
        if len(shapes) == len(icons):
            base_l = min(s["left"] for s in shapes)
            base_t = min(s["top"] for s in shapes)
            left0 = min(i["box"][0] for i in icons)
            top0 = min(i["box"][1] for i in icons)
            for shape, icon in zip(sorted(shapes, key=lambda s: s["name"]), icons):
                x0, y0, x1, y1 = icon["box"]
                expect = ((x0 - left0) * 0.75, (y0 - top0) * 0.75, (x1 - x0) * 0.75, (y1 - y0) * 0.75)
                got = (shape["left"] - base_l, shape["top"] - base_t, shape["width"], shape["height"])
                if max(abs(a - b) for a, b in zip(expect, got)) > 1.0:
                    failures.append(f"{shape['name']} の位置/大きさが {got}(期待 {expect})")

        with zipfile.ZipFile(xlsx) as archive:
            media = [n for n in archive.namelist() if n.startswith("xl/media/")]
            partial = False
            for name in media:
                image = cv2.imdecode(np.frombuffer(archive.read(name), np.uint8),
                                     cv2.IMREAD_UNCHANGED)
                channels = image.shape[2] if image.ndim == 3 else 1
                alpha = image[..., 3] if channels == 4 else None
                print(f"  {name}: {image.shape[1]}x{image.shape[0]} ch={channels}"
                      + (f" alpha min/max={alpha.min()}/{alpha.max()}" if alpha is not None else ""))
                if channels != 4:
                    failures.append(f"{name} にアルファが無い")
                elif ((alpha > 0) & (alpha < 255)).any():
                    partial = True
            if not partial:
                failures.append("半透明の画素が残っていない")
    finally:
        ok, message = clipboard_format.restore_snapshot()
        pump(200)
        print(f"クリップボードを元に戻す: {message}")
        if not ok:
            failures.append("クリップボードを元に戻せなかった")
        try:
            xlsx.unlink(missing_ok=True)
            workdir.rmdir()
        except OSError:
            pass

    if failures:
        print("FAIL: " + " / ".join(failures))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
