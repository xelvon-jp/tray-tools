"""あっふの動画まわり(QtMultimedia だけで動く。ffmpeg の実行ファイルは要らない)。

- cv_thumb / cv_probe: OpenCV で動画の1コマと情報を取る速い経路(読み取りスレッドで使う。
  取ったサムネは直近の数本を記憶する)。cv2 が無い・取れないときは None。
- VideoProbe: 動画の情報(長さ・解像度・コーデック)と、サムネイル(1コマ)を取る。
  cv_thumb で取れなかったとき・音声のタグのとき用(GUI スレッド)。
  プレビューのビュア状態で、動画の「情報とサムネ」を出すのに使う。
- VideoPlayerView: 内蔵プレーヤ。反対窓いっぱいに出し、YouTube ショートのように
  ↑↓ で前後の動画へ移る。映像は自分で描く(QVideoWidget は別窓になって、上に
  重ねる字や帯が隠れることがあるので使わない)。

どちらも GUI スレッドで使う(QMediaPlayer の都合)。使い終わったら close() で
ファイルを手放す(動画を掴んだままにしない)。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QWidget

PROBE_TIMEOUT_MS = 6000     # サムネが取れないまま待つ上限(読めない形式で固まらない)
SEEK_STEP_MS = 5000         # ←→ の移動量
END_MARGIN_MS = 150         # 終わりのこの手前で止める
HINT_SHOW_MS = 3000         # 再生を始めてから、操作の案内を出しておく時間
ACCENT = "#ff3b30"          # 進み具合の線の色(YouTube ショートの赤)


def format_time(ms: int) -> str:
    """ミリ秒を m:ss / h:mm:ss に。"""
    seconds = max(int(ms), 0) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def is_audio_path(path: str) -> bool:
    import filer_core

    return os.path.splitext(path)[1].lstrip(".").lower() in filer_core.PREVIEW_AUDIO_EXTS


def tag_lines(meta: dict) -> list:
    """音声のタグ(タイトル・アーティスト・アルバム)を、表示用の行にする(取れた項目だけ)。"""
    lines = []
    for key, label in (("title", "タイトル"), ("artist", "アーティスト"), ("album", "アルバム"),
                       ("genre", "ジャンル")):
        if meta.get(key):
            lines.append(f"{label}  {meta[key]}")
    return lines


def describe_meta(meta: dict) -> list:
    """VideoProbe が集めた情報を、表示用の行にする(取れた項目だけ)。"""
    lines = tag_lines(meta)
    if meta.get("duration"):
        lines.append(f"長さ    {format_time(meta['duration'])}")
    size = meta.get("size")
    if size and size[0] > 0 and size[1] > 0:
        lines.append(f"解像度  {size[0]}×{size[1]}")
    video = " ".join(x for x in (meta.get("video_codec"),
                                 f"{meta['fps']:.0f}fps" if meta.get("fps") else "") if x)
    if video:
        lines.append(f"映像    {video}")
    if meta.get("audio_codec"):
        lines.append(f"音声    {meta['audio_codec']}")
    if meta.get("bitrate"):
        lines.append(f"ビットレート  {int(meta['bitrate']) // 1000} kbps")
    return lines


def _enum_name(value) -> str:
    name = getattr(value, "name", None) or str(value)
    return "" if name in ("Unspecified", "None", "-1") else name


def _collect_meta(player) -> dict:
    """QMediaPlayer から、取れるだけ情報を集める。"""
    from PySide6.QtMultimedia import QMediaMetaData

    meta = {"duration": int(player.duration())}
    data = player.metaData()
    try:
        size = data.value(QMediaMetaData.Key.Resolution)
        if isinstance(size, QSize):
            meta["size"] = (size.width(), size.height())
        fps = data.value(QMediaMetaData.Key.VideoFrameRate)
        if fps:
            meta["fps"] = float(fps)
        meta["video_codec"] = _enum_name(data.value(QMediaMetaData.Key.VideoCodec))
        meta["audio_codec"] = _enum_name(data.value(QMediaMetaData.Key.AudioCodec))
        for key, names in (("title", ("Title",)), ("artist", ("ContributingArtist", "Author",
                                                              "AlbumArtist")),
                           ("album", ("AlbumTitle",)), ("genre", ("Genre",))):
            for name in names:
                value = data.value(getattr(QMediaMetaData.Key, name))
                if isinstance(value, (list, tuple)):
                    value = " / ".join(str(v) for v in value)
                if value:
                    meta[key] = str(value)
                    break
        bitrate = data.value(QMediaMetaData.Key.AudioBitRate)
        if bitrate:
            meta["bitrate"] = int(bitrate)
        cover = data.value(QMediaMetaData.Key.CoverArtImage)
        if not isinstance(cover, QImage) or cover.isNull():
            cover = data.value(QMediaMetaData.Key.ThumbnailImage)
        if isinstance(cover, QImage) and not cover.isNull():
            meta["cover"] = cover
    except Exception:  # noqa: BLE001  取れない項目は出さないだけ
        pass
    return meta


# --- OpenCV で1コマだけ取る(速い経路。QMediaPlayer の VideoProbe は取れないときの代わり) ---
# 読み取りスレッドで呼ぶ(GUI スレッドでは呼ばない)。cv2 が無い・開けない・コマが取れないときは
# None を返し、呼び出し側が VideoProbe(従来方式)へ落ちる。
CV_THUMB_MAX = 1280         # 取ったコマの長辺の上限(縮めて持つ。表示側でさらに箱へ収める)
CV_SEEK_SEC = 1.5           # 先頭のコマが真っ黒・真っ平らなときだけ、このあたり(長さの1/4まで)へ進む
CV_DARK_MEAN = 14.0         # 先頭のコマの平均輝度がこれ未満なら「黒い出だし」とみなす
CV_FLAT_STD = 5.0           # 輝度のばらつきがこれ未満なら「真っ平ら」とみなす
THUMB_CACHE_SIZE = 24       # 取ったサムネの記憶(直近何本か)
_FOURCC_NAMES = {
    "hvc1": "H.265 (HEVC)", "hevc": "H.265 (HEVC)", "hev1": "H.265 (HEVC)", "avc1": "H.264", "h264": "H.264",
    "x264": "H.264", "mp4v": "MPEG-4", "vp80": "VP8", "vp90": "VP9", "av01": "AV1",
    "mjpg": "Motion JPEG", "xvid": "MPEG-4", "divx": "MPEG-4", "wmv3": "WMV",
    "mpg1": "MPEG-1", "mpg2": "MPEG-2",
}
_thumb_cache = None         # OrderedDict (パス, 更新時刻, サイズ) -> (QImage, 情報)
_thumb_lock = None
_cv2 = None
_cv2_failed = False


def _load_cv2():
    """cv2 を読む(初回だけ)。ffmpeg の警告(Dolby Vision など)は黙らせる。無ければ None。"""
    global _cv2, _cv2_failed
    if _cv2 is not None or _cv2_failed:
        return _cv2
    os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
    os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
    try:
        import cv2

        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
        except Exception:  # noqa: BLE001  版によっては無い
            pass
        _cv2 = cv2
    except Exception:  # noqa: BLE001  cv2 が無い環境は従来方式
        _cv2_failed = True
    return _cv2


def warm_cv() -> None:
    """cv2 の読み込み(初回は 300ms ほどかかる)を裏で済ませておく。プレビューを開いたときに呼ぶ。"""
    import threading

    if _cv2 is None and not _cv2_failed:
        threading.Thread(target=_load_cv2, daemon=True, name="filer-cv2-warm").start()


def _frame_to_qimage(frame):
    """cv2 のコマ(BGR / 灰色の numpy)を QImage にする(コピーを持つ)。"""
    cv2 = _cv2
    if frame.ndim == 2:
        rgb = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
    elif frame.shape[2] == 4:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
    else:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()


def _is_dull(frame) -> bool:
    """真っ黒・真っ平らなコマか(出だしの黒み・白み)。"""
    gray = _cv2.cvtColor(frame, _cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    mean, std = _cv2.meanStdDev(gray)
    return float(mean[0][0]) < CV_DARK_MEAN or float(std[0][0]) < CV_FLAT_STD


def cv_probe(path: str):
    """OpenCV で動画の1コマと情報を取る。(QImage, 情報の辞書) か、取れなければ None。

    先頭のコマを読み、真っ黒・真っ平らなときだけ長さの1/4(最大 CV_SEEK_SEC 秒)先へ
    進んで取り直す(シークはコマを順に読み進めるので遅い。出だしが普通なら使わない)。
    情報は describe_meta と同じ項目(duration=ミリ秒 / size / fps / video_codec)で、
    取れないものは入れない。VideoCapture は必ず release する。例外は呼び出し側へ。"""
    cv2 = _load_cv2()
    if cv2 is None:
        return None
    cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return None
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000)
        except Exception:  # noqa: BLE001
            pass
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        ok, frame = cap.read()
        if not ok or frame is None or frame.size == 0:
            return None
        duration = count / fps if fps > 0 and count > 0 else 0.0     # 秒
        if duration > 1.5 and fps > 0 and _is_dull(frame):
            target = int(min(duration / 4, CV_SEEK_SEC) * fps)
            if target > 0 and cap.set(cv2.CAP_PROP_POS_FRAMES, target):
                ok2, frame2 = cap.read()
                if ok2 and frame2 is not None and frame2.size and not _is_dull(frame2):
                    frame = frame2
    finally:
        cap.release()
    height, width = frame.shape[:2]
    meta = {"size": (width, height)}
    if duration > 0:
        meta["duration"] = int(duration * 1000)
    if 0 < fps < 1000:
        meta["fps"] = fps
    code = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip("\0 ").lower()
    if code and code.isprintable():
        meta["video_codec"] = _FOURCC_NAMES.get(code, code.upper())
    long_side = max(width, height)
    if long_side > CV_THUMB_MAX:
        scale = CV_THUMB_MAX / long_side
        frame = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))),
                           interpolation=cv2.INTER_AREA)
    image = _frame_to_qimage(frame)
    if image.isNull():
        return None
    return image, meta


def cv_thumb(path: str, use_cache: bool = True):
    """cv_probe にキャッシュを付けたもの(直近 THUMB_CACHE_SIZE 本)。読み取りスレッドから呼ぶ。

    キーはパス+更新時刻+サイズ(中身が変われば取り直す)。戻ったときは開かずに返す。
    取れなければ None(失敗はキャッシュしない)。"""
    global _thumb_cache, _thumb_lock
    import threading
    from collections import OrderedDict

    if _thumb_lock is None:
        _thumb_lock = threading.Lock()
        _thumb_cache = OrderedDict()
    key = None
    if use_cache:
        try:
            st = os.stat(path)
            key = (os.path.normcase(os.path.abspath(path)), st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if key is not None:
            with _thumb_lock:
                hit = _thumb_cache.get(key)
                if hit is not None:
                    _thumb_cache.move_to_end(key)
                    return hit[0], dict(hit[1])
    got = cv_probe(path)
    if got is None:
        return None
    if key is not None:
        with _thumb_lock:
            _thumb_cache[key] = (got[0], dict(got[1]))
            while len(_thumb_cache) > THUMB_CACHE_SIZE:
                _thumb_cache.popitem(last=False)
    return got


def clear_thumb_cache() -> None:
    if _thumb_lock is not None:
        with _thumb_lock:
            _thumb_cache.clear()


class VideoProbe(QObject):
    """動画の情報とサムネイルを非同期に取る。取れたら done(QImage か None, 情報の辞書)。"""

    done = Signal(object, dict)

    def __init__(self, path: str, parent=None, audio: bool = False):
        super().__init__(parent)
        from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

        self.path = path
        self.audio = audio      # 音声: 映像のコマは無いので、読めた時点で(カバー画像があれば添えて)終える
        self._finished = False
        self._seeked = False
        self._meta = {}
        self.player = QMediaPlayer(self)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(lambda *_: self._finish(None))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._finish(None))

    def start(self) -> None:
        self._timer.start(PROBE_TIMEOUT_MS)
        self.player.setSource(QUrl.fromLocalFile(self.path))
        self.player.pause()     # 読み込んで先頭のコマを出させる(音は出ない)

    def _on_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if status == QMediaPlayer.MediaStatus.LoadedMedia and not self._seeked:
            self._meta = _collect_meta(self.player)
            self._seeked = True
            if self.audio:
                self._finish(self._meta.get("cover"))
                return
            duration = self._meta.get("duration") or 0
            if duration > 2000:     # 真っ黒な出だしを避け、長さの1割(最大 5 秒)あたりの絵にする
                self.player.setPosition(min(duration // 10, 5000))
            self.player.pause()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._finish(None)

    def _on_frame(self, frame) -> None:
        if self._finished or not frame.isValid() or not self._seeked:
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._meta = self._meta or _collect_meta(self.player)
        self._finish(image)

    def _finish(self, image) -> None:
        if self._finished:
            return
        self._finished = True
        self._timer.stop()
        meta = self._meta or _collect_meta(self.player)
        self.close()
        self.done.emit(image, meta)

    def close(self) -> None:
        """動画を手放す(別のファイルへ移るとき・閉じるとき)。"""
        self._finished = True
        self._timer.stop()
        try:
            self.player.stop()
            self.player.setSource(QUrl())
        except Exception:  # noqa: BLE001
            pass


class VideoPlayerView(QWidget):
    """内蔵プレーヤ。反対窓いっぱいに映像を出し、上に題名・進み具合を重ねる。

    キー: Space=再生/一時停止  ←→=5 秒戻す/進める  ↑↓=前・次の動画(move)
    Esc=戻る(done)  Ctrl+Enter=拡張子別メニュー(menu)。クリックで一時停止、
    下の線をクリックでその位置へ。終わったら止まる(くり返さない・次へも進まない)。"""

    done = Signal()
    move = Signal(int)
    menu = Signal()

    def __init__(self, font: QFont = None, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self._font = QFont(font) if font is not None else QFont()
        self._frame = None
        self._title = ""
        self._counter = ""
        self._ended = False
        self._error = ""
        self._audio = False         # 音声ファイル(映像が無いので、題名・タグを画面の真ん中に出す)
        self._tags = []
        self._cover = None
        self._since_start = QTimer(self)
        self._since_start.setSingleShot(True)
        self._since_start.timeout.connect(self.update)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(lambda _d: self.update())
        self.player.playbackStateChanged.connect(lambda _s: self.update())
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)
        self.player.metaDataChanged.connect(self._on_meta)

    def _on_meta(self) -> None:
        if not self._audio:
            return
        try:
            meta = _collect_meta(self.player)
            self._tags = tag_lines(meta)
            cover = meta.get("cover")
            self._cover = cover if isinstance(cover, QImage) and not cover.isNull() else None
        except Exception:  # noqa: BLE001
            pass
        self.update()

    # --- 再生 ---
    def play(self, path: str, title: str = "", counter: str = "") -> None:
        """この動画を最初から再生する(前のものは止めて手放す)。"""
        self._title = title or os.path.basename(path)
        self._counter = counter
        self._ended = False
        self._error = ""
        self._frame = None
        self._audio = is_audio_path(path)
        self._tags = []
        self._cover = None
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self._since_start.start(HINT_SHOW_MS)
        self.update()

    def stop(self) -> None:
        """止めて、動画を手放す(ファイルを掴んだままにしない)。"""
        try:
            self.player.stop()
            self.player.setSource(QUrl())
        except Exception:  # noqa: BLE001
            pass
        self._frame = None
        self._cover = None
        self.update()

    def toggle(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if self._ended:                 # 終わっていたら、最初から
            self._ended = False
            self.player.setPosition(0)
            self.player.play()
        elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()
        self.update()

    def seek(self, delta_ms: int) -> None:
        duration = max(int(self.player.duration()), 0)
        target = int(self.player.position()) + delta_ms
        self.player.setPosition(max(0, min(target, max(duration - 1, 0))))
        self._ended = False

    def is_playing(self) -> bool:
        from PySide6.QtMultimedia import QMediaPlayer

        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def _on_position(self, position: int) -> None:
        """終わりの手前で止める。EndOfMedia まで流し切ると、音声の出力が終わりで固まる
        ことがあった(Qt の FFmpeg バックエンド)。最後の 0.15 秒を残して自分で止める。"""
        duration = int(self.player.duration())
        if (duration > 400 and position >= duration - END_MARGIN_MS and not self._ended
                and self.is_playing()):
            self._ended = True
            self.player.pause()
        self.update()

    def _on_frame(self, frame) -> None:
        if frame.isValid():
            self._frame = frame
            self.update()

    def _on_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._ended = True
            self.update()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._error = "この動画は内蔵プレーヤで再生できません（Ctrl+Enter のメニューから別のアプリで開けます）"
            self.update()

    def _on_error(self, _error, message: str = "") -> None:
        self._error = ("再生できません: " + message) if message else \
            "この動画は内蔵プレーヤで再生できません"
        self.update()

    # --- キー・マウス ---
    def keyPressEvent(self, event):  # noqa: N802 (Qt の上書き)
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.ControlModifier)
        if key in (Qt.Key_Return, Qt.Key_Enter) and ctrl:
            self.menu.emit()
        elif key in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Backspace):
            self.done.emit()
        elif key in (Qt.Key_Space, Qt.Key_K):
            self.toggle()
        elif key == Qt.Key_Left:
            self.seek(-SEEK_STEP_MS)
        elif key == Qt.Key_Right:
            self.seek(SEEK_STEP_MS)
        elif key in (Qt.Key_Up, Qt.Key_PageUp):
            self.move.emit(-1)
        elif key in (Qt.Key_Down, Qt.Key_PageDown):
            self.move.emit(1)
        elif key == Qt.Key_Home:
            self.player.setPosition(0)
            self._ended = False
        else:
            event.ignore()
            return
        event.accept()

    def mousePressEvent(self, event):  # noqa: N802
        pos = event.position()
        if pos.y() >= self.height() - 28 and self.player.duration() > 0:
            ratio = max(0.0, min(pos.x() / max(self.width(), 1), 1.0))
            self.player.setPosition(int(self.player.duration() * ratio))
            self._ended = False
        else:
            self.toggle()
        event.accept()

    def wheelEvent(self, event):  # noqa: N802
        self.move.emit(-1 if event.angleDelta().y() > 0 else 1)
        event.accept()

    # --- 描画 ---
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#000000"))
        w, h = self.width(), self.height()
        if self._frame is not None:
            self._paint_frame(painter, w, h)
        elif self._audio and not self._error:
            self._paint_audio(painter, w, h)
        font = QFont(self._font)
        font.setPointSize(max(self._font.pointSize(), 10))
        painter.setFont(font)
        if self._error:
            painter.setPen(QColor("#ffffff"))
            painter.drawText(QRectF(16, 0, w - 32, h), Qt.AlignCenter | Qt.TextWordWrap,
                             self._error)
            painter.end()
            return
        # 下の帯(題名・何本目・時間)。映像の上に薄い黒のグラデーションを重ねる
        band = 96
        gradient = QLinearGradient(0, h - band, 0, h)
        gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
        gradient.setColorAt(1.0, QColor(0, 0, 0, 190))
        painter.fillRect(QRectF(0, h - band, w, band), gradient)
        bold = QFont(font)
        bold.setBold(True)
        painter.setFont(bold)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(QRectF(14, h - 62, w - 28, 24), Qt.AlignLeft | Qt.AlignVCenter,
                         self._title)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 200))
        duration = max(int(self.player.duration()), 0)
        position = min(int(self.player.position()), duration) if duration else 0
        left = f"{format_time(position)} / {format_time(duration)}"
        painter.drawText(QRectF(14, h - 38, w - 28, 20), Qt.AlignLeft | Qt.AlignVCenter, left)
        if self._counter:
            painter.drawText(QRectF(14, h - 38, w - 28, 20), Qt.AlignRight | Qt.AlignVCenter,
                             self._counter)
        # 進み具合の線(いちばん下)
        painter.fillRect(QRectF(0, h - 4, w, 4), QColor(255, 255, 255, 70))
        if duration:
            painter.fillRect(QRectF(0, h - 4, w * position / duration, 4), QColor(ACCENT))
        # 中央の印: 一時停止・終わったとき
        paused = not self.is_playing()
        if paused and (self._frame is not None or self._ended):
            self._paint_center(painter, w, h, "↻" if self._ended else "▶")
        # 操作の案内(始めの数秒と、止まっているとき)
        if paused or self._since_start.isActive():
            small = QFont(font)
            small.setPointSize(max(font.pointSize() - 1, 8))
            painter.setFont(small)
            painter.setPen(QColor(255, 255, 255, 190))
            hint = ("↑↓ 前後の" + ("曲" if self._audio else "動画")
                    + "   ←→ 5秒   Space 一時停止   Esc 戻る   Ctrl+Enter メニュー")
            painter.drawText(QRectF(10, 6, w - 20, 22), Qt.AlignLeft | Qt.AlignVCenter, hint)
        painter.end()

    def _paint_audio(self, painter, w: int, h: int) -> None:
        """音声: カバー画像(あれば)と、題名・タグを中央に出す。"""
        top = 40
        if self._cover is not None:
            side = max(min(w - 40, h - 220, 360), 0)
            if side > 32:
                cover = self._cover.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                painter.drawImage(int((w - cover.width()) / 2), top, cover)
                top += cover.height() + 16
        else:
            painter.setPen(QColor(255, 255, 255, 60))
            note_font = QFont(self._font)
            note_font.setPointSize(48)
            painter.setFont(note_font)
            painter.drawText(QRectF(0, top, w, 90), Qt.AlignCenter, "♪")
            top += 100
        font = QFont(self._font)
        font.setPointSize(max(self._font.pointSize(), 10) + 1)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 230))
        lines = [self._title] + self._tags
        painter.drawText(QRectF(20, top, w - 40, max(h - top - 110, 20)),
                         Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, "\n".join(lines))

    def _paint_frame(self, painter, w: int, h: int) -> None:
        frame = self._frame
        size = frame.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        scale = min(w / size.width(), h / size.height())
        tw, th = size.width() * scale, size.height() * scale
        target = QRectF((w - tw) / 2, (h - th) / 2, tw, th)
        try:
            frame.paint(painter, target, _paint_options())
        except Exception:  # noqa: BLE001  paint が使えない環境は画像に直して描く
            image = frame.toImage()
            if not image.isNull():
                painter.drawImage(target, image)

    @staticmethod
    def _paint_center(painter, w: int, h: int, mark: str) -> None:
        radius = 34
        center = QPointF(w / 2, h / 2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 140))
        painter.drawEllipse(center, radius, radius)
        mark_font = QFont(painter.font())
        mark_font.setPointSize(26)
        painter.setFont(mark_font)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(QRectF(center.x() - radius, center.y() - radius, radius * 2,
                                radius * 2), Qt.AlignCenter, mark)


def _paint_options():
    from PySide6.QtMultimedia import QVideoFrame

    options = QVideoFrame.PaintOptions()
    options.aspectRatioMode = Qt.KeepAspectRatio
    return options


def thumbnail_image(image: QImage, box_w: int, box_h: int) -> QImage:
    """サムネを箱に収める(拡大はしない)。"""
    if image.isNull() or image.width() <= box_w and image.height() <= box_h:
        return image
    return image.scaled(box_w, box_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
