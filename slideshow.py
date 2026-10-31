# slideshow.py
# 写真のスライドショー(フォトフレーム)。選んだモニタに全画面・黒地で、写真を順に出す。
# 効果はクロスフェードと、ゆっくりズーム(Ken Burns 風の拡大・平行移動)。
#
#   pythonw slideshow.py                         登録済みのフォルダ(settings.json の slideshow)
#   pythonw slideshow.py --folder D:\写真\2024     そのフォルダだけ(サブフォルダ込み)を一時的に
#   pythonw slideshow.py --settings <path>        読む settings.json を指定(テスト用)
#
# 常駐とは別のプロセスで動く(起こすのは slideshow_launch.py。理由もそちら)。
#
# 作り
#   - 一覧作りは裏のスレッド(slideshow_core.scan_into)。最初の数枚が見つかった時点で出し始める
#   - 次の1枚は裏のスレッドで先読みする(_Loader)。画面の大きさ×ズームのぶんに縮めて読み
#     (image_load.read_image。EXIF の向きも当たる)、QImage のまま UI スレッドへ渡して、
#     QPixmap にするのは UI スレッドで。読めない写真は飛ばす
#   - 描画は QPainter で、古い1枚と新しい1枚を不透明度で重ねる(加算合成で正確なクロスフェード)。
#     QTimer で進め、動いている間(ズーム中・フェード中)だけ fps で描き直す。止まっている間は
#     描き直さない
#   - 再生中は SetThreadExecutionState で画面が消えない・スリープしないようにし、終わるときに
#     必ず解除する(keep_awake.set_keep_awake。UI スレッドから呼ぶ)
#   - 持つ絵は「いまの1枚・消えかけの1枚・先読みの1枚」だけ。履歴はパスだけ(件数に上限)
#
# 操作: Esc・クリックで終わる / → 次 / ← 前(履歴) / Space 一時停止・再開 / I 情報表示
#       マウスカーソルは隠す(動かすとしばらく出る)
import argparse
import json
import os
import queue
import random
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication, QWidget

import image_load
import keep_awake
import slideshow_core as sc
import slideshow_launch

ERROR_LOG_PATH = Path(__file__).resolve().parent / "slideshow_error.log"
DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"

QUICK_FADE = 0.4          # ← / → で手で送ったときのフェード(表示のフェードより長くはしない)
IDLE_TICK_MS = 100        # 何も動いていない間の見回りの間隔(描き直しはしない)
CURSOR_HIDE_MS = 2000     # マウスを動かしてからカーソルを隠すまで
MESSAGE_CLOSE_MS = 5000   # 「写真がありません」を出してから閉じるまで
SEARCHING_MS = 600        # これより長く1枚目が出なければ「写真を探しています…」


def _log_exception(where: str) -> None:
    """pythonw では標準エラーがどこにも出ないので、ファイルにも残す。"""
    text = traceback.format_exc()
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {where} =====\n{text}")
    except OSError:
        pass
    try:
        print(text, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def pick_screen(name: str):
    """名前(QScreen.name())のモニタ。空・見つからない(外れている)ときは主モニタ。"""
    if name:
        for screen in QGuiApplication.screens():
            if screen.name() == name:
                return screen
    return QGuiApplication.primaryScreen()


def load_photo(path: str, box_w: int, box_h: int):
    """(QImage, 情報) を読む(裏のスレッドで呼ぶ)。描くのが速い形式にそろえて返す。
    読めなければ OSError など。"""
    image, _original, _scaled = image_load.read_image(path, box_w, box_h)
    fmt = (QImage.Format_ARGB32_Premultiplied if image.hasAlphaChannel()
           else QImage.Format_RGB32)
    if image.format() != fmt:
        image = image.convertToFormat(fmt)
    return image, sc.photo_info(path)


# ---------------------------------------------------------------
# 裏のスレッド: 走査と先読み
# ---------------------------------------------------------------
class _Bridge(QObject):
    loaded = Signal(object)     # 結果の辞書(裏のスレッド → UI スレッド)


class _Loader:
    """先読みの係。依頼(「次をランダムに」/「このパスを」)を1つずつ順に読み、結果を
    bridge.loaded で UI スレッドへ返す。走査のスレッドもここで持つ。

    結果: {"serial", "kind", "path", "image", "info", "error", "empty", "fade", "step"}"""

    MAX_TRIES = 50       # 続けて読めなかったら諦める回数(全部壊れているフォルダで回り続けない)

    def __init__(self, folders, bridge: _Bridge, rng=None, reader=load_photo,
                 now=time.monotonic, scan=True):
        self.pool = sc.PhotoPool(folders, rng=rng)
        self.bridge = bridge
        self.reader = reader
        self.now = now
        self.stop_event = threading.Event()
        self.requests = queue.Queue()
        self.started = now()
        self.scan_thread = None
        if scan:
            self.scan_thread = threading.Thread(
                target=self._scan, name="slideshow-scan", daemon=True)
            self.scan_thread.start()
        self.thread = threading.Thread(target=self._run, name="slideshow-load", daemon=True)
        self.thread.start()

    def _scan(self) -> None:
        try:
            sc.scan_into(self.pool, self.stop_event)
        except Exception:  # noqa: BLE001
            _log_exception("scan")
            self.pool.mark_finished()

    def request(self, serial: int, kind: str, box, path=None, fade=0.0, step=0) -> None:
        self.requests.put((serial, kind, path, box, fade, step))

    def stop(self) -> None:
        self.stop_event.set()
        self.requests.put(None)

    def _emit(self, result: dict) -> None:
        if not self.stop_event.is_set():
            self.bridge.loaded.emit(result)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            item = self.requests.get()
            if item is None or self.stop_event.is_set():
                return
            serial, kind, path, box, fade, step = item
            result = {"serial": serial, "kind": kind, "path": path, "image": None,
                      "info": None, "error": None, "empty": False, "fade": fade, "step": step}
            try:
                if kind == "path":
                    try:
                        result["image"], result["info"] = self.reader(path, *box)
                    except Exception as e:  # noqa: BLE001  壊れた写真・消えた写真
                        result["error"] = str(e) or type(e).__name__
                else:
                    self._read_random(result, box)
            except Exception as e:  # noqa: BLE001
                _log_exception("load")
                result["error"] = str(e) or type(e).__name__
            self._emit(result)

    def _read_random(self, result: dict, box) -> None:
        failures = 0
        while not self.stop_event.is_set():
            if not sc.wait_ready(self.pool, self.started, self.now):
                time.sleep(0.05)
                continue
            path = self.pool.pick()
            if path is None:
                if self.pool.is_finished():
                    result["empty"] = True
                    return
                time.sleep(0.1)      # まだ見つかっていない(走査中)
                continue
            try:
                result["image"], result["info"] = self.reader(path, *box)
                result["path"] = path
                return
            except Exception:  # noqa: BLE001  読めない写真は外して次へ
                self.pool.discard(path)
                failures += 1
                if failures >= self.MAX_TRIES and self.pool.is_finished():
                    result["empty"] = True
                    return


class _Slide:
    __slots__ = ("pixmap", "path", "info", "motion", "start", "fade")

    def __init__(self, pixmap, path, info, motion):
        self.pixmap = pixmap
        self.path = path
        self.info = info or {}
        self.motion = motion
        self.start = 0.0
        self.fade = 0.0


# ---------------------------------------------------------------
# 窓
# ---------------------------------------------------------------
class SlideshowWindow(QWidget):
    """全画面のスライドショー。作ったあと place_on(画面) → play(folders) で始める。

    テストのために差し替えられるもの: time_source(時刻)・rng(乱数)・reader(読み込み)・
    scan(走査のスレッドを起こすか)・set_awake(画面を消さない設定)。"""

    def __init__(self, config: dict, time_source=time.monotonic, rng=None,
                 reader=load_photo, scan=True, set_awake=None, settings_path=None):
        super().__init__()
        self.setWindowTitle("スライドショー")
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.BlankCursor)
        self.time_source = time_source
        self.rng = rng or random.Random()
        self.reader = reader
        self.scan = scan
        self.set_awake = set_awake or keep_awake.set_keep_awake
        self.settings_path = settings_path
        self.config = sc.normalize_config(config)
        self.bridge = _Bridge()
        self.bridge.loaded.connect(self._on_loaded)
        self.loader = None
        self.current = None
        self.previous = None
        self.pending = None
        self.history = sc.History()
        self.paused = False
        self._pause_started = 0.0
        self._paused_total = 0.0
        self.show_info = False
        self.message = ""
        self.closing = False
        self._awake = False
        self._serial = 0           # 先読みの依頼の通し番号
        self._nav_serial = 0       # ← / → で頼んだ1枚の通し番号
        self._nav_inflight = False
        self._waiting_next = None  # 次が読めていないうちに送ろうとした(そのフェード秒数)
        self._empty = False
        self._last_mouse = None
        self._started_at = time_source()
        self._searching = False
        self._target_size = None    # place_on で決めた画面の大きさ(出る前の先読みの大きさに使う)
        self._target_ratio = 1.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setSingleShot(True)
        self._cursor_timer.timeout.connect(lambda: self._safe(self._hide_cursor))
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self.close)
        self.info_font = QFont()
        self.info_font.setPointSizeF(11)
        self.message_font = QFont()
        self.message_font.setPointSizeF(16)
        self.folders = None         # 再生中の出どころ(None は登録済みのフォルダ)

    # --- 時刻(一時停止を除いた経過) ---
    def clock(self) -> float:
        now = self.time_source()
        if self.paused:
            now = self._pause_started
        return now - self._paused_total

    # --- 再生の始め・終わり ---
    def play(self, folders=None) -> None:
        """再生し直す。folders が None なら設定の登録フォルダ。"""
        self.folders = folders
        self._stop_loader()
        self.current = self.previous = self.pending = None
        self.history = sc.History()
        self._empty = False
        self._waiting_next = None
        self._nav_inflight = False
        if self.paused:
            self.toggle_pause()
        self.message = ""
        self._searching = False
        self._close_timer.stop()
        self._started_at = self.time_source()
        if folders is None:
            source = self.config["folders"]
        else:
            source = [{"path": f, "recursive": True} for f in folders]
        if not source:
            self._finish_with_message(
                "スライドショーのフォルダが登録されていません\n"
                "トレイの「🖼 スライドショー」→「📂 フォルダと表示の設定…」で登録してください")
            return
        self.loader = _Loader(source, self.bridge, rng=self.rng, reader=self.reader,
                              now=self.time_source, scan=self.scan)
        self._request_next()
        self._set_awake(True)
        self.timer.start(IDLE_TICK_MS)

    def apply_config(self, config: dict) -> None:
        """設定(秒数・フェード・ズーム・fps・モニタ)を入れ替える。写真の出どころは変えない。"""
        old_monitor = self.config.get("monitor")
        self.config = sc.normalize_config(config)
        if self.config.get("monitor") != old_monitor and self.isVisible():
            self.place_on(pick_screen(self.config["monitor"]))
        self._kick()

    def _stop_loader(self) -> None:
        if self.loader is not None:
            self.loader.stop()
            self.loader = None

    def _set_awake(self, on: bool) -> None:
        if on == self._awake:
            return
        try:
            self.set_awake(on)
        except Exception:  # noqa: BLE001
            _log_exception("execution state")
        self._awake = on

    def _finish_with_message(self, text: str) -> None:
        self.message = text
        self._set_awake(False)
        self.update()
        self._close_timer.start(MESSAGE_CLOSE_MS)

    def place_on(self, screen) -> None:
        """screen の全面に出す。"""
        if screen is None:
            return
        geometry = screen.geometry()
        self._target_size = geometry.size()
        self._target_ratio = screen.devicePixelRatio()
        if self.isVisible():
            if self.screen() == screen and self.isFullScreen():
                return
            self.showNormal()
        self.setGeometry(geometry)
        self.winId()                # 窓を作っておく(出す前にモニタを決めるため)
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        self.showFullScreen()

    def box(self):
        """先読みの大きさ(物理ピクセル)。画面×ズームのぶん。"""
        if self._target_size is not None:
            size, ratio = self._target_size, self._target_ratio
        else:
            screen = self.screen()
            size, ratio = self.size(), (screen.devicePixelRatio() if screen is not None else 1.0)
        width, height = size.width(), size.height()
        zoom = sc.ZOOM_MAX if self.config["ken_burns"] else 1.0
        return max(16, int(width * ratio * zoom)), max(16, int(height * ratio * zoom))

    # --- 先読み ---
    def _request_next(self) -> None:
        if self.loader is None or self._empty:
            return
        self._serial += 1
        self.loader.request(self._serial, "random", self.box())

    def _request_path(self, path: str, fade: float, step: int) -> None:
        if self.loader is None:
            return
        self._nav_serial += 1
        self._nav_inflight = True
        self.loader.request(self._nav_serial, "path", self.box(), path=path, fade=fade, step=step)

    def _on_loaded(self, result: dict) -> None:
        try:
            self._handle_loaded(result)
        except Exception:  # noqa: BLE001  スロットから例外を出すとプロセスごと落ちる
            _log_exception("loaded")

    def _handle_loaded(self, result: dict) -> None:
        if self.closing or self.loader is None:
            return
        if result["kind"] == "path":
            if result["serial"] != self._nav_serial:
                return
            self._nav_inflight = False
            if result["image"] is None:
                # 履歴の1枚が読めない(消えた・壊れた)。外して同じ向きにもう1つ進む
                self.history.remove_current(result["step"])
                self._navigate(result["step"], result["fade"])
                return
            self._show(self._make_slide(result), result["fade"])
            return
        if result["serial"] != self._serial:
            return
        if result["empty"]:
            self._empty = True
            if self.current is None:
                self._finish_with_message(self._empty_message())
            return
        if result["image"] is None:
            self._request_next()
            return
        slide = self._make_slide(result)
        if self.current is None:
            self.history.push(slide.path)
            self._show(slide, self.config["fade_seconds"])
            self._request_next()
        elif self._waiting_next is not None:
            fade = self._waiting_next
            self._waiting_next = None
            self.history.push(slide.path)
            self._show(slide, fade)
            self._request_next()
        else:
            self.pending = slide

    def _empty_message(self) -> str:
        missing = self.loader.pool.missing if self.loader is not None else []
        lines = ["写真が見つかりません"]
        if missing:
            lines.append("見つからないフォルダ:")
            lines.extend(missing[:5])
        lines.append("(対象: jpg / jpeg / png / bmp / gif / webp)")
        return "\n".join(lines)

    def _make_slide(self, result: dict) -> _Slide:
        pixmap = QPixmap.fromImage(result["image"])
        motion = sc.random_motion(self.rng, self.config["ken_burns"])
        return _Slide(pixmap, result["path"], result["info"], motion)

    def _show(self, slide: _Slide, fade: float) -> None:
        now = self.clock()
        slide.start = now
        # 一時停止中は時刻が進まないので、フェードを掛けると現れないまま止まる。すぐ出す
        slide.fade = 0.0 if self.paused else max(0.0, fade)
        # 消えかけの1枚は手放す(持つ絵を増やさない)。フェード無しなら前の絵も残さない
        self.previous = self.current if slide.fade > 0 and self.current is not None else None
        self.current = slide
        self.message = ""
        self._kick()

    # --- 送る・戻る ---
    def go_next(self, fade: float) -> bool:
        """次へ。戻っている途中なら履歴を進め、末尾なら先読みの1枚を出す。
        先読みが間に合っていなければ、読めた時点で出す(False を返す)。"""
        if self._nav_inflight:
            return False
        if not self.history.at_end():
            return self._navigate(+1, fade)
        if self.pending is not None:
            slide, self.pending = self.pending, None
            self.history.push(slide.path)
            self._show(slide, fade)
            self._request_next()
            return True
        if not self._empty:
            self._waiting_next = fade
        return False

    def go_prev(self, fade: float) -> bool:
        if self._nav_inflight:
            return False
        return self._navigate(-1, fade)

    def _navigate(self, step: int, fade: float) -> bool:
        path = self.history.forward() if step > 0 else self.history.back()
        if path is None:
            if step > 0:
                return self.go_next(fade)
            return False
        self._waiting_next = None
        self._request_path(path, fade, step)
        return True

    def toggle_pause(self) -> None:
        now = self.time_source()
        if self.paused:
            self._paused_total += now - self._pause_started
            self.paused = False
        else:
            self._pause_started = now
            self.paused = True
        self._kick()

    # --- 進める ---
    def animating(self) -> bool:
        """描き直しが要るか(ズーム中・フェード中)。"""
        if self.current is None or self.paused:
            return False
        return self.previous is not None or (
            self.config["ken_burns"] and self.current.motion is not sc.STILL)

    def _kick(self) -> None:
        self._tick()

    def _tick(self) -> None:
        try:
            self.advance()
        except Exception:  # noqa: BLE001
            _log_exception("tick")

    def advance(self) -> None:
        """時刻を見て、フェードの終わり・次の1枚への切り替えを進め、必要なら描き直す。"""
        if self.closing:
            return
        now = self.clock()
        changed = False
        current = self.current
        if current is not None and self.previous is not None and now - current.start >= current.fade:
            self.previous = None          # フェードが終わった。古い絵を手放す
            changed = True
        if (current is not None and not self.paused and self._waiting_next is None
                and not self._nav_inflight
                and now - current.start >= self.config["interval_seconds"]):
            changed = self.go_next(self.config["fade_seconds"]) or changed
        if (self.current is None and not self.message and not self._searching
                and self.loader is not None
                and (self.time_source() - self._started_at) * 1000 >= SEARCHING_MS):
            self._searching = True
            changed = True
        interval = int(1000 / self.config["fps"]) if self.animating() else IDLE_TICK_MS
        if self.timer.interval() != interval or not self.timer.isActive():
            self.timer.start(interval)
        if changed or self.animating():
            self.update()

    # --- 描く ---
    def slide_rect(self, slide: _Slide, now: float) -> QRectF:
        span = self.config["interval_seconds"] + slide.fade
        t = (now - slide.start) / span if span > 0 else 0.0
        size = slide.pixmap.size()
        motion = slide.motion if self.config["ken_burns"] else sc.STILL
        x, y, w, h = sc.frame_rect(size.width(), size.height(), self.width(), self.height(),
                                   motion, t)
        return QRectF(x, y, w, h)

    def fade_progress(self, now: float = None) -> float:
        """いまの1枚の現れ具合(0〜1。フェードが終われば 1)。"""
        if self.current is None:
            return 0.0
        now = self.clock() if now is None else now
        if self.current.fade <= 0:
            return 1.0
        return sc.ease((now - self.current.start) / self.current.fade)

    def paintEvent(self, event) -> None:
        try:
            self._paint()
        except Exception:  # noqa: BLE001
            _log_exception("paint")

    def _paint(self) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), Qt.black)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            now = self.clock()
            alpha = self.fade_progress(now)
            if self.previous is not None and alpha < 1.0:
                painter.setOpacity(1.0 - alpha)
                painter.drawPixmap(self.slide_rect(self.previous, now), self.previous.pixmap,
                                   QRectF(self.previous.pixmap.rect()))
                # 新しい絵は足し込む: 古い×(1-a) ＋ 新しい×a で、途中で暗くならない
                painter.setCompositionMode(QPainter.CompositionMode_Plus)
            if self.current is not None:
                painter.setOpacity(alpha)
                painter.drawPixmap(self.slide_rect(self.current, now), self.current.pixmap,
                                   QRectF(self.current.pixmap.rect()))
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setOpacity(1.0)
            if self.show_info and self.current is not None:
                self._paint_info(painter)
            if self.paused and self.current is not None:
                self._paint_corner(painter, "⏸ 一時停止", right=True)
            text = self.message
            if not text and self.current is None and self._searching:
                text = "写真を探しています…"
            if text:
                painter.setFont(self.message_font)
                painter.setPen(QColor("#d0d0d0"))
                painter.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, text)
        finally:
            painter.end()

    def info_lines(self) -> list:
        info = self.current.info if self.current is not None else {}
        lines = [info.get("name", "")]
        if info.get("taken"):
            lines.append(f"撮影 {info['taken']}")
        if info.get("folder"):
            lines.append(info["folder"])
        return [line for line in lines if line]

    def _paint_info(self, painter: QPainter) -> None:
        self._paint_corner(painter, "\n".join(self.info_lines()), right=False)

    def _paint_corner(self, painter: QPainter, text: str, right: bool) -> None:
        if not text:
            return
        painter.setFont(self.info_font)
        metrics = painter.fontMetrics()
        lines = text.split("\n")
        width = max(metrics.horizontalAdvance(line) for line in lines) + 24
        height = metrics.height() * len(lines) + 16
        margin = 24
        x = self.width() - width - margin if right else margin
        y = self.height() - height - margin
        box = QRectF(x, y, width, height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(QColor("#e8e8e8"))
        painter.drawText(box.adjusted(12, 8, -12, -8), Qt.AlignLeft | Qt.AlignTop, text)

    # --- 操作 ---
    def keyPressEvent(self, event) -> None:
        try:
            self.handle_key(event.key())
        except Exception:  # noqa: BLE001
            _log_exception("key")

    def handle_key(self, key) -> None:
        quick = min(QUICK_FADE, self.config["fade_seconds"])
        if key == Qt.Key_Escape:
            self.close()
        elif key == Qt.Key_Right:
            self.go_next(quick)
        elif key == Qt.Key_Left:
            self.go_prev(quick)
        elif key == Qt.Key_Space:
            self.toggle_pause()
        elif key == Qt.Key_I:
            self.show_info = not self.show_info
            self.update()

    def mousePressEvent(self, event) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001
            _log_exception("click")

    def mouseMoveEvent(self, event) -> None:
        try:
            pos = event.position()
            last, self._last_mouse = self._last_mouse, QPointF(pos)
            # 全画面にした直後などに来る、動いていない移動は数えない
            if last is None or (pos - last).manhattanLength() < 3:
                return
            self.setCursor(Qt.ArrowCursor)
            self._cursor_timer.start(CURSOR_HIDE_MS)
        except Exception:  # noqa: BLE001
            _log_exception("mouse")

    def _hide_cursor(self) -> None:
        self.setCursor(Qt.BlankCursor)

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            _log_exception("timer")

    def closeEvent(self, event) -> None:
        try:
            self.shutdown()
        except Exception:  # noqa: BLE001
            _log_exception("close")
        super().closeEvent(event)

    def shutdown(self) -> None:
        """止める(何度呼んでもよい)。画面を消さない設定は必ず解除する。"""
        self.closing = True
        for timer in (self.timer, self._cursor_timer, self._close_timer):
            try:
                timer.stop()
            except Exception:  # noqa: BLE001
                pass
        self._stop_loader()
        self.current = self.previous = self.pending = None
        self._set_awake(False)


# ---------------------------------------------------------------
# 起動・単一起動
# ---------------------------------------------------------------
ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def acquire_single_instance() -> bool:
    """先着なら True(CLAUDE.md「単一起動の判定」。filer.acquire_single_instance と同じ)。"""
    global _mutex_handle
    import ctypes
    from ctypes import wintypes
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateMutexW
        create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        handle = create(None, False, slideshow_launch.MUTEX_NAME)
        already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    except OSError:
        return True
    if not handle:
        return True
    if already:
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        close(handle)
        return False
    _mutex_handle = handle
    return True


class CommandServer(QObject):
    """slideshow_launch から届く1行の JSON(play / config / quit)を受ける口。"""

    def __init__(self, window: SlideshowWindow, settings_path):
        super().__init__(window)
        self.window = window
        self.settings_path = settings_path
        self.server = QLocalServer(self)
        QLocalServer.removeServer(slideshow_launch.SERVER_NAME)
        if self.server.listen(slideshow_launch.SERVER_NAME):
            self.server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self) -> None:
        try:
            while self.server.hasPendingConnections():
                sock = self.server.nextPendingConnection()
                sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
                sock.disconnected.connect(sock.deleteLater)
                if sock.bytesAvailable():
                    self._on_ready_read(sock)
        except Exception:  # noqa: BLE001  外から叩かれる入口。必ず受ける
            _log_exception("connection")

    def _on_ready_read(self, sock) -> None:
        try:
            payload = slideshow_launch.parse_command(bytes(sock.readAll()))
            if payload is None:
                return
            sock.write(b"ok\n")
            sock.flush()
            self.handle(payload)
        except Exception:  # noqa: BLE001
            _log_exception("command")

    def handle(self, payload: dict) -> None:
        cmd = payload["cmd"]
        if cmd == "quit":
            self.window.close()
            return
        settings_path = payload.get("settings") or self.settings_path
        config = sc.load_config(settings_path)
        if cmd == "config":
            self.window.apply_config(config)
            return
        self.window.config = sc.normalize_config(config)
        self.window.place_on(pick_screen(config["monitor"]))
        self.window.play(payload.get("folders"))
        self.window.activateWindow()


def parse_args(argv):
    parser = argparse.ArgumentParser(description="写真のスライドショー")
    parser.add_argument("--folder", action="append", default=None,
                        help="このフォルダ(サブフォルダ込み)を一時的に再生する。複数指定可")
    parser.add_argument("--settings", default=None, help="読む settings.json")
    return parser.parse_args(argv)


def _forward(payload: bytes) -> bool:
    """先客に依頼を渡す。先客が終わりかけなら少し待って、こちらが引き継ぐ。
    渡せたら True、引き継ぐ(自分が先着になった)なら False。"""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            if slideshow_launch.send(payload):
                return True
        except Exception:  # noqa: BLE001
            _log_exception("forward")
        time.sleep(0.2)
        if acquire_single_instance():
            return False
    return True     # 渡せず引き継げもしない。重ねて出さない


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings_path = args.settings or str(DEFAULT_SETTINGS_PATH)
    app = QApplication(sys.argv[:1])
    if not acquire_single_instance():
        if _forward(slideshow_launch.command_bytes("play", args.folder, settings_path)):
            return 0
    window = None
    try:
        config = sc.load_config(settings_path)
        window = SlideshowWindow(config, settings_path=settings_path)
        CommandServer(window, settings_path)
        window.place_on(pick_screen(config["monitor"]))
        window.play(args.folder)
        window.raise_()
        window.activateWindow()
        return app.exec()
    except Exception:  # noqa: BLE001
        _log_exception("main")
        return 1
    finally:
        # どう終わっても、画面を消さない設定は解除する(スレッドに紐づくので
        # プロセスが終われば OS も外すが、明示しておく)
        try:
            if window is not None:
                window.shutdown()
            keep_awake.set_keep_awake(False)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
