# tests/check_bg_remove_vector_gvml.py
# 「ベクタ化」でクリップボードへ載せたものを、実際の Excel に貼ると、色や大きさを編集
# できる図形(フリーフォーム)として貼れることを確かめる。
#
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe tests\check_bg_remove_vector_gvml.py
#
# 確かめること
#   - 図形(Shape.Type = 5 msoFreeform、またはグループ 6 の中の Freeform)として貼れる
#   - 塗りの色が保たれる(元の絵の色に近い色の図形がある)
#   - アイコンに分けたときはアイコンの数のグループ、分けないときは1つのグループになる
#   - GVML・PNG・image/svg+xml の3つの形式が載っている
#
# - vtracer が要る(任意の依存)。無ければ何もせずに終わる。
# - 実画面のプラットフォームで動かす(offscreen にはクリップボードが無い)。窓は出さない。
# - Excel は Visible=False で起こし、保存せずに閉じて Quit する(画面には出ない)。
# - **本物のクリップボードを一瞬書き換える。** 走らせる前の中身は clipboard_format の
#   退避で取っておき、終わったら書き戻す(check_bg_remove_gvml.py と同じ)。
# - 画像は合成したもの(赤いドーナツ形・白い窓のある緑の四角・青い円)だけを使う。
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

import bg_remove  # noqa: E402
import clipboard_format  # noqa: E402
from check_bg_remove_clipboard import list_formats, pump  # noqa: E402

# 貼った図形を、グループの中まで降りて一覧にする。色は Fill.ForeColor.RGB(R + G*256 + B*65536)。
EXCEL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$out = @{ shapes = @() }
$xl = New-Object -ComObject Excel.Application
$xl.Visible = $false; $xl.DisplayAlerts = $false
try {
  $wb = $xl.Workbooks.Add(); $ws = $wb.Worksheets.Item(1); $ws.Paste()
  foreach ($s in $ws.Shapes) {
    $item = @{ name = $s.Name; type = $s.Type; width = $s.Width; height = $s.Height; children = @() }
    if ($s.Type -eq 6) {
      foreach ($c in $s.GroupItems) {
        $item.children += @{ type = $c.Type; rgb = $c.Fill.ForeColor.RGB; line = $c.Line.Visible }
      }
    } else {
      $item.rgb = $s.Fill.ForeColor.RGB
    }
    $out.shapes += $item
  }
  $wb.Close($false)
} finally {
  $xl.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($xl)
}
$out | ConvertTo-Json -Depth 6 -Compress
"""

COLORS = {"red": (220, 40, 40), "green": (40, 160, 40), "white": (250, 250, 250),
          "blue": (40, 60, 200)}


def make_sheet():
    """3つのアイコン。赤は穴(透明)のあるドーナツ形、緑は上に白い窓が重なる四角。"""
    rgba = np.zeros((160, 420, 4), np.uint8)
    cv2.circle(rgba, (70, 80), 55, COLORS["red"] + (255,), -1)
    cv2.circle(rgba, (70, 80), 22, (0, 0, 0, 0), -1)
    cv2.rectangle(rgba, (160, 25), (270, 135), COLORS["green"] + (255,), -1)
    cv2.rectangle(rgba, (190, 55), (240, 105), COLORS["white"] + (255,), -1)
    cv2.circle(rgba, (350, 80), 50, COLORS["blue"] + (255,), -1)
    return rgba


def run_excel():
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", EXCEL_SCRIPT],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0x08000000)
    deadline = time.time() + 180
    while proc.poll() is None and time.time() < deadline:
        pump(100)  # 貼り付けでデータを取りに来るので、待つ間もイベントを回す
    if proc.poll() is None:
        proc.kill()
        raise TimeoutError("Excel が 180 秒で終わりませんでした")
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode("cp932", "replace"))
    return json.loads(out.decode("cp932", "replace").strip().splitlines()[-1])


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _rgb(value):
    value = int(value)
    return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


def check_once(rgba, vector, split: bool, failures: list) -> None:
    label = "分ける" if split else "分けない"
    icons = bg_remove.split_icons(rgba[..., 3], 12, 16) if split else []
    groups = bg_remove.assign_paths_to_icons(vector["paths"], icons) if icons else None
    gvml = bg_remove.build_vector_gvml(vector["paths"], groups)
    bg_remove.set_clipboard_image(bg_remove.array_to_qimage(rgba), extra={
        bg_remove.GVML_MIME: gvml, bg_remove.SVG_MIME: vector["svg"].encode("utf-8")})
    pump(200)
    names = [name for _fmt, name in list_formats()]
    print(f"[{label}] 載った形式:", ", ".join(names))
    for required in ("Art::GVML ClipFormat", "PNG", "image/svg+xml"):
        if required not in names:
            failures.append(f"[{label}] {required} が載っていない")

    shapes = _as_list(run_excel().get("shapes"))
    expect_groups = len(icons) if split else 1
    print(f"[{label}] Excel に貼った図形: {len(shapes)} 個(期待 {expect_groups} 個のグループ)")
    seen = []
    for shape in shapes:
        children = _as_list(shape.get("children"))
        types = sorted({int(c["type"]) for c in children})
        print(f"  {shape['name']} type={shape['type']} W={shape['width']:.1f} H={shape['height']:.1f}"
              f" 子 {len(children)} 個 type={types}")
        if int(shape["type"]) != 6:
            failures.append(f"[{label}] {shape['name']} がグループ(6)でない: type={shape['type']}")
        if any(int(c["type"]) != 5 for c in children):
            failures.append(f"[{label}] {shape['name']} の中に Freeform(5) でない図形がある")
        if any(c.get("line") not in (0, False) for c in children):
            failures.append(f"[{label}] {shape['name']} の中に線のある図形がある")
        seen.extend(_rgb(c["rgb"]) for c in children)
    if len(shapes) != expect_groups:
        failures.append(f"[{label}] グループの数が {len(shapes)}(期待 {expect_groups})")
    total = sum(len(_as_list(s.get("children"))) for s in shapes)
    if total != len(vector["paths"]):
        failures.append(f"[{label}] 図形の数が {total}(パスは {len(vector['paths'])})")
    print(f"  色: {sorted(set(seen))}")
    for name, target in COLORS.items():
        if not any(max(abs(a - b) for a, b in zip(color, target)) <= 12 for color in seen):
            failures.append(f"[{label}] {name} {target} に近い色の図形が無い")


def main() -> int:
    if not bg_remove.vector_available():
        print("vtracer が入っていないので確かめられません(pip install vtracer)")
        return 2
    app = QGuiApplication([sys.argv[0]])  # noqa: F841
    rgba = make_sheet()
    vector = bg_remove.vectorize(rgba, 20)
    print(f"ベクタ化: {len(vector['paths'])} パス {vector['seconds']:.1f}秒")
    if not clipboard_format.take_snapshot():
        print("いまのクリップボードを退避できないので、書き換えずに中止します")
        return 2
    failures = []
    try:
        check_once(rgba, vector, True, failures)
        check_once(rgba, vector, False, failures)
    finally:
        ok, message = clipboard_format.restore_snapshot()
        pump(200)
        print(f"クリップボードを元に戻す: {message}")
        if not ok:
            failures.append("クリップボードを元に戻せなかった")
    if failures:
        print("FAIL: " + " / ".join(failures))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
