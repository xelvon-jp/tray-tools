# claude_progress.py
# Claude Code の処理中のセッションを、画面右下の小さなパネルに進捗バーで並べる。
# トレイアイコンは持たない部品(開閉と中身の受け渡しは feature_screen が行う)。
#
# 中身は claude_status.list_sessions() の結果をそのまま受け取る(feature_screen が
# 3秒ごとに読んでいるものを渡すだけ。こちらでファイルは読まない)。
#   - バーの長さは計画の進み具合(claude_status.plan_fraction)。計画が無いセッションは
#     長さを出せないので、流れる帯で「動いている」ことだけ示す
#   - 色はトレイの輪と同じ(実行中=青・確認待ち=桃)
#   - 完了・失敗したものは数秒だけバーのまま残し、そのあとは1行に折りたたんで残す
#     (少し前にやり取りしていたセッションへ飛びたいことが多いため)。折りたたんだ行は
#     claude_status の DONE_TTL(30分)で消え、何も無くなればパネルごと隠す
#
# 操作: 行をクリック = そのセッションをデスクトップアプリで開く(claude://code/continue)。
#       Ctrl+ドラッグ = 移動(場所を覚える)。Copilot の状態監視バーと同じ作法で、
#       ただのドラッグでは動かさない(押したつもりで動いてしまう事故を避ける)。
# フォーカスは奪わない(WindowDoesNotAcceptFocus + WA_ShowWithoutActivating)。打っている
# 文字がパネルに取られない。
import json
import os
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

import claude_status as cs

PANEL_WIDTH = 300
PAD_X = 12
PAD_Y = 8
ROW_HEIGHT = 48
# 折りたたんだ行(終わったセッション)の高さ。名前と終わった時刻だけ。
COMPACT_HEIGHT = 24
# 折りたたんだ行は新しいものからこの数まで(全部はメニューの「💬 Claude」にある)。
MAX_COMPACT = 5
TRACK_HEIGHT = 6
SCREEN_MARGIN = 12
# 完了・失敗をバーのまま残しておく時間(秒)。終わったことが目に入る程度。過ぎたら折りたたむ。
LINGER_SECONDS = 8
# 計画の無いセッションの帯を動かす間隔(ms)。経過時間の表示もこの間隔で進む。
ANIMATION_MS = 80
SLIDE_SECONDS = 1.6
SLIDE_RATIO = 0.35

BG = QColor(44, 44, 42, 235)
TEXT = QColor(241, 239, 232)
SUBTEXT = QColor(180, 178, 169)
TRACK = QColor(68, 68, 65)
HOVER = QColor(255, 255, 255, 18)
STATE_COLORS = {
    cs.RUNNING: QColor(55, 138, 221),
    cs.ASK: QColor(212, 83, 126),
    cs.DONE: QColor(99, 153, 34),
    cs.FAIL: QColor(226, 75, 74),
}

# 位置と入切。公開リポジトリの外(%LOCALAPPDATA%\traytools)に置く。
STATE_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "traytools" / "claude_progress.json"


def load_state(path=None) -> dict:
    try:
        data = json.loads(Path(path or STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(update: dict, path=None) -> None:
    """読み直して該当キーだけ差し替える(settings.json と同じ作法)。書けなくても諦める。"""
    path = Path(path or STATE_FILE)
    data = load_state(path)
    data.update(update)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def rows_to_show(entries, now, first_seen_done: dict) -> list:
    """パネルに出す行(entry の写しに "_compact" を付けたもの)。

    処理中(実行中・確認待ち)と、終わって LINGER_SECONDS 以内のものはバーの行。
    それより前に終わったもの(entries に残っている＝DONE_TTL 以内)は折りたたんだ1行で、
    バーの行の下に、終わった新しい順に MAX_COMPACT まで並べる。

    first_seen_done は「終わったのを最初に見た時刻」の控え(セッション ID ごと)。ファイルの
    ended はフックが書いた時刻だが、常駐が読むまでに数秒ずれるので、見えた時刻から数える。"""
    full, compact = [], []
    for entry in entries:
        state = entry.get("state")
        key = entry.get("session_id") or entry.get("_path")
        if state in (cs.RUNNING, cs.ASK):
            first_seen_done.pop(key, None)
            full.append(dict(entry, _compact=False))
        elif state in (cs.DONE, cs.FAIL):
            seen = first_seen_done.setdefault(key, now)
            if now - seen < LINGER_SECONDS:
                full.append(dict(entry, _compact=False))
            else:
                compact.append(dict(entry, _compact=True))
    compact.sort(key=lambda e: -(e.get("ended") or e.get("updated") or 0))
    return full + compact[:MAX_COMPACT]


def panel_height(rows) -> int:
    return PAD_Y * 2 + sum(COMPACT_HEIGHT if r.get("_compact") else ROW_HEIGHT for r in rows)


class ClaudeProgressPanel(QWidget):
    def __init__(self, on_open=None, state_path=None):
        super().__init__()
        self.setWindowTitle("Claude の進み具合")
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self._on_open = on_open
        self._state_path = state_path
        self._entries = []
        self._rows = []
        self._first_seen_done = {}
        self._hover = -1
        self._drag_origin = None
        self._manual_pos = None
        pos = load_state(state_path).get("pos")
        if isinstance(pos, list) and len(pos) == 2:
            try:
                self._manual_pos = QPoint(int(pos[0]), int(pos[1]))
            except (TypeError, ValueError):
                self._manual_pos = None
        self._name_font = QFont("Meiryo", 9)
        self._sub_font = QFont("Meiryo", 8)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # -- 中身 ------------------------------------------------------------
    def set_entries(self, entries) -> None:
        """feature_screen から、読んだばかりの一覧を受け取る。"""
        self._entries = list(entries or [])
        self._tick()

    def _tick(self):
        try:
            rows = rows_to_show(self._entries, time.time(), self._first_seen_done)
            if not rows:
                self._rows = []
                self._timer.stop()
                self.hide()
                return
            height = panel_height(rows)
            resize = height != self.height()
            self._rows = rows
            if resize or not self.isVisible():
                self.resize(PANEL_WIDTH, height)
                self._place()
            if not self.isVisible():
                self.show()
            # 動くもの(経過時間・流れる帯・バーの行が折りたたまれる時刻)があるときだけ回す。
            # 折りたたんだ行だけなら止まった絵なので、3秒ごとの set_entries で足りる。
            if any(not r.get("_compact") for r in rows):
                if not self._timer.isActive():
                    self._timer.start(ANIMATION_MS)
            else:
                self._timer.stop()
            self.update()
        except Exception:  # noqa: BLE001  タイマーのスロット。投げ切ると常駐ごと落ちる
            import traceback
            traceback.print_exc()

    def _place(self):
        """覚えた場所(左上。画面内に収める)か、既定はプライマリ画面の右下(下端を基準に
        上へ伸びる。行が増えても画面の外へはみ出さない)。"""
        if self._manual_pos is not None:
            screen = QGuiApplication.screenAt(self._manual_pos + QPoint(10, 10))
            if screen is not None:
                area = screen.availableGeometry()
                x = min(max(self._manual_pos.x(), area.left()), area.right() - self.width())
                y = min(max(self._manual_pos.y(), area.top()), area.bottom() - self.height())
                self.move(x, y)
                return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - SCREEN_MARGIN,
                  area.bottom() - self.height() - SCREEN_MARGIN)

    # -- 描画 ------------------------------------------------------------
    def _row_rect(self, index) -> QRect:
        top = PAD_Y
        for row in self._rows[:index]:
            top += COMPACT_HEIGHT if row.get("_compact") else ROW_HEIGHT
        height = COMPACT_HEIGHT if self._rows[index].get("_compact") else ROW_HEIGHT
        return QRect(PAD_X // 2, top, self.width() - PAD_X, height)

    def paintEvent(self, _event):
        # QWidget の setStyleSheet では背景が描かれない(CLAUDE.md の罠)ので自分で描く。
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(BG)
        p.drawRoundedRect(QRectF(self.rect()), 10, 10)
        now = time.time()
        for index, entry in enumerate(self._rows):
            self._paint_row(p, entry, self._row_rect(index), now, index == self._hover)

    def _paint_row(self, p, entry, rect, now, hover):
        if hover and cs.jump_url(entry):
            p.setPen(Qt.NoPen)
            p.setBrush(HOVER)
            p.drawRoundedRect(QRectF(rect), 6, 6)
        state = entry.get("state")
        color = STATE_COLORS.get(state, STATE_COLORS[cs.RUNNING])
        if entry.get("_compact"):
            self._paint_compact(p, entry, rect, color)
            return
        inner = rect.adjusted(PAD_X // 2, 4, -PAD_X // 2, -4)

        # 1行目: 名前(切り詰め)と、右に経過/状態
        if state == cs.ASK:
            tail = f"確認待ち {cs.format_duration(now - (entry.get('asked') or now))}"
        elif state in (cs.DONE, cs.FAIL):
            tail = cs.LABELS.get(state, "")
        else:
            tail = cs.format_duration(now - (entry.get("started") or now))
        p.setFont(self._name_font)
        fm = QFontMetrics(self._name_font)
        tail_w = fm.horizontalAdvance(tail)
        head = QRect(inner.left(), inner.top(), inner.width(), fm.height())
        p.setPen(color if state != cs.RUNNING else SUBTEXT)
        p.drawText(head, Qt.AlignRight | Qt.AlignVCenter, tail)
        p.setPen(TEXT)
        name = fm.elidedText(cs.display_name(entry, cs.MENU_TITLE_CHARS), Qt.ElideRight,
                             max(0, inner.width() - tail_w - 8))
        p.drawText(head, Qt.AlignLeft | Qt.AlignVCenter, name)

        # バー
        track = QRectF(inner.left(), head.bottom() + 4, inner.width(), TRACK_HEIGHT)
        p.setPen(Qt.NoPen)
        p.setBrush(TRACK)
        p.drawRoundedRect(track, 3, 3)
        fraction = 1.0 if state in (cs.DONE, cs.FAIL) else cs.plan_fraction(entry.get("plan"))
        p.setBrush(color)
        if fraction is None:
            # 計画が無い: 帯を左から右へ流す(長さは意味を持たない)
            phase = (now % SLIDE_SECONDS) / SLIDE_SECONDS
            width = track.width() * SLIDE_RATIO
            left = track.left() - width + (track.width() + width) * phase
            band = QRectF(left, track.top(), width, track.height()).intersected(track)
            if band.width() > 0:
                p.drawRoundedRect(band, 3, 3)
        elif fraction > 0:
            p.drawRoundedRect(QRectF(track.left(), track.top(),
                                     max(track.height(), track.width() * fraction),
                                     track.height()), 3, 3)

        # 2行目: 計画の手順(無ければ何をしているかは分からないと正直に)
        p.setFont(self._sub_font)
        sub_fm = QFontMetrics(self._sub_font)
        plan = cs.plan_text(entry.get("plan"))
        if state == cs.DONE:
            sub = "終わりました"
        elif state == cs.FAIL:
            sub = "止まりました"
        else:
            sub = plan or "計画なし"
        p.setPen(SUBTEXT)
        sub_rect = QRect(inner.left(), int(track.bottom()) + 2, inner.width(), sub_fm.height())
        p.drawText(sub_rect, Qt.AlignLeft | Qt.AlignVCenter,
                   sub_fm.elidedText(sub, Qt.ElideRight, inner.width()))

    def _paint_compact(self, p, entry, rect, color):
        """折りたたんだ行: 状態の色の点・名前・終わった時刻だけ。バーの行より控えめに。"""
        inner = rect.adjusted(PAD_X // 2, 0, -PAD_X // 2, 0)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        dot = 6
        p.drawEllipse(QRectF(inner.left(), inner.center().y() - dot / 2 + 1, dot, dot))
        p.setFont(self._sub_font)
        fm = QFontMetrics(self._sub_font)
        ended = entry.get("ended") or entry.get("updated") or time.time()
        tail = time.strftime("%H:%M", time.localtime(ended))
        if entry.get("state") == cs.FAIL:
            tail = "失敗 " + tail
        tail_w = fm.horizontalAdvance(tail)
        text_rect = inner.adjusted(dot + 6, 0, 0, 0)
        p.setPen(SUBTEXT)
        p.drawText(text_rect, Qt.AlignRight | Qt.AlignVCenter, tail)
        p.setPen(TEXT if cs.jump_url(entry) else SUBTEXT)
        name = fm.elidedText(cs.display_name(entry, cs.MENU_TITLE_CHARS), Qt.ElideRight,
                             max(0, text_rect.width() - tail_w - 8))
        p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, name)

    # -- マウス ----------------------------------------------------------
    def _row_at(self, pos) -> int:
        for index in range(len(self._rows)):
            if self._row_rect(index).contains(pos):
                return index
        return -1

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            return
        hover = self._row_at(event.position().toPoint())
        if hover != self._hover:
            self._hover = hover
            jumpable = 0 <= hover < len(self._rows) and cs.jump_url(self._rows[hover])
            self.setCursor(Qt.PointingHandCursor if jumpable else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, _event):
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if event.modifiers() & Qt.ControlModifier:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.setCursor(Qt.SizeAllCursor)
            return
        index = self._row_at(event.position().toPoint())
        if 0 <= index < len(self._rows) and self._on_open is not None:
            try:
                self._on_open(self._rows[index])
            except Exception:  # noqa: BLE001  スロットの中で投げ切らない
                import traceback
                traceback.print_exc()

    def mouseReleaseEvent(self, _event):
        if self._drag_origin is None:
            return
        self._drag_origin = None
        self.setCursor(Qt.ArrowCursor)
        self._manual_pos = self.pos()
        save_state({"pos": [self._manual_pos.x(), self._manual_pos.y()]}, self._state_path)

    def reset_position(self):
        self._manual_pos = None
        save_state({"pos": None}, self._state_path)
        if self.isVisible():
            self._place()
