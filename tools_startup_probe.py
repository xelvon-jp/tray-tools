# 付箋プロセスの起動が何にかかっているかを測る。
#   pythonw ではなく python で実行すること(出力を見るため)。
#
#   & "C:\Users\yotan\.venvs\tray-tools\Scripts\python.exe" R:\claude\tray-tools\tools_startup_probe.py
#
# 付箋プロセスは1枚出すたびにこの一式を通るので、ここが遅い環境では
# 「範囲を選んでから付箋が出るまで」がそのぶん延びる。
import sys
import time

T0 = time.perf_counter()


def lap(label):
    print("  %-34s %7.1f ms" % (label, (time.perf_counter() - T0) * 1000))


print("Python:", sys.version.split()[0])
print("実行ファイル:", sys.executable)
print()
print("=== 累計時間 ===")
lap("インタプリタ起動〜ここまで")

from PySide6.QtCore import QPoint  # noqa: E402
lap("PySide6.QtCore の import")

from PySide6.QtGui import QImage  # noqa: E402
lap("PySide6.QtGui の import")

from PySide6.QtWidgets import QApplication  # noqa: E402
lap("PySide6.QtWidgets の import")

app = QApplication([])
lap("QApplication の生成")

sys.path.insert(0, r"R:\claude\tray-tools")
import capture_window  # noqa: E402,F401
lap("capture_window の import")

import tempfile, pathlib  # noqa: E402
d = pathlib.Path(tempfile.mkdtemp())
src = d / "probe.png"
QImage(1600, 900, QImage.Format_RGB32).save(str(src), "PNG")
lap("1600x900のPNGを書き出す")

img = QImage(str(src))
lap("同じPNGを読み込む")

w = capture_window.CaptureWindow(
    img, QPoint(80, 80), {"save_dir": str(d), "history_days": 0},
    settings_path=str(d / "settings.json"))
lap("CaptureWindow の生成")

w.show()
app.processEvents()
lap("show() して1回描画")

print()
print("=== 参考 ===")
print("  この環境の合計: %.0f ms" % ((time.perf_counter() - T0) * 1000))
print("  開発機での実測: 364〜450 ms")
w.close()
