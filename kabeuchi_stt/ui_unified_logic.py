# -*- coding: utf-8 -*-
"""統合 GUI（S3 3行窓＋F3 引き出し）の判定ロジック（純関数・Qt/COM 非依存）。表示は ui_unified / ui_mic_chip / ui_drawer。

- マイク状態チップ: 8状態（＋判定前の「確認中」）。既存の判定（mic_check の Issue・meter_state の未録音の赤・
  session の monitor_error）の結果を、1つの状態へまとめるだけ。新しい判定はしない。
- 録音ボタン・保留ボタン・確定ボタンの文言。
- 喋っている長さの表示（「喋っています 0:12」）と、録音全体の清書の進み（「清書中 35%（あと約3分）」）。
"""
from __future__ import annotations

import ui_dialogue_logic as L

# ---- マイク状態チップ ---------------------------------------------------------------------------------
# key: (チップの文言, tone, 詳細の説明, 対処)
CHIP_STATES: dict[str, tuple[str, str, str, str]] = {
    "ok": ("入力OK", "good", "マイクの音量は良好です。", "そのまま話してください。"),
    "low": ("音が小さい", "warn", "声が小さめです。認識が欠けることがあります。", "マイクに近づくか、入力音量を上げてください。"),
    "clip": ("音割れ", "bad", "音量が大きすぎて割れています。", "マイクから離れるか、入力音量を下げてください。"),
    "noise": ("ノイズ大（要相談）", "noise", "雑音が声に近い大きさです。", "静かな場所に移るか、入力のブーストを確認してください。"),
    "none": ("音が入っていない", "none", "数秒間、音が届いていません。", "マイクの選択・ミュート・接続を確認してください。"),
    "busy": ("マイク使用中", "busy", "別のアプリがマイクを使っています。", "会議アプリなどを閉じてから、もう一度試してください。"),
    "idle": ("未録音（話しています）", "idle", "録音していませんが声を検出しています。", "録音ボタンを押すと記録を始めます。"),
    "paused": ("一時停止中（話しています）", "paused", "一時停止中ですが声を検出しています。", "再開を押すと続きから記録します。"),
    # 判定前（入力を開いていない・統計がまだ無い）。画面案の8状態には無い、中立の表示
    "wait": ("マイク確認中", "wait", "マイクの状態をまだ測っていません。", "録音を始めるか、少し話してみてください。"),
}
CHIP_ORDER = ("ok", "low", "clip", "noise", "none", "busy", "idle", "paused", "wait")
CHIP_LABELS = tuple(CHIP_STATES[k][0] for k in CHIP_ORDER)

_ISSUE_STATE = {"silent": "none", "clipping": "clip", "low": "low", "noisy": "noise"}
_ISSUE_RANK = {"none": 0, "clip": 1, "low": 2, "noise": 3}   # 小さいほど優先


def chip_state(*, recording: bool, paused: bool = False, issues=(), monitoring: bool = False, no_audio: bool = False,
               busy: bool = False, speaking_unrecorded: bool = False, measured: bool = True) -> str:
    """マイク状態チップの状態キー。優先順: マイク使用中 > 未録音/一時停止中の話し声 > 音が入っていない > 音量の指摘 > 入力OK。
    - busy: 待機中モニターが他のアプリのためにマイクを開けない（録音中は対象外）
    - speaking_unrecorded: 録音されていない（待機中・一時停止中）のに話しているとき（メーターが赤になる条件と同じ）
    - no_audio: 数秒間、音が届いていない
    - issues: mic_check.Issue（code: silent / low / clipping / noisy）
    - monitoring: 録音中、または待機中モニターが入力を開いている / measured: 統計が1つでもある"""
    if busy and not recording:
        return "busy"
    if speaking_unrecorded:
        return "paused" if (recording and paused) else "idle"
    if no_audio:
        return "none"
    found = [_ISSUE_STATE[getattr(i, "code", "")] for i in (issues or ()) if getattr(i, "code", "") in _ISSUE_STATE]
    if found:
        return min(found, key=lambda k: _ISSUE_RANK[k])
    if not recording and not (monitoring and measured):
        return "wait"
    return "ok"


def chip_detail(key: str, issues=()) -> tuple[str, str, str]:
    """押したときの詳細 (見出し, 説明, 対処)。指摘が出ているときは、その具体的な文言（Issue.message / advice）を使う。"""
    label, _tone, text, advice = CHIP_STATES.get(key, CHIP_STATES["wait"])
    mine = [i for i in (issues or ()) if _ISSUE_STATE.get(getattr(i, "code", "")) == key]
    if mine:
        text = " / ".join(i.message for i in mine)
        advice = " ".join(i.advice for i in mine)
    return label, text, advice


# ---- 録音ボタン・確定ボタン -------------------------------------------------------------------------------
def rec_button_view(dialogue_active: bool, ending: bool = False, dialogue_ok: bool = True) -> tuple[str, str, bool, str]:
    """録音ボタンの (文言, 種類 "rec"/"stop", 押せるか, ツールチップ)。録音（＝セッション開始）/ 停止（＝セッション終了）。"""
    if dialogue_active:
        return "停止", "stop", not ending, "セッションを終える（録音を止める。録音全体の清書は、設定でONなら自動で始まります）"
    return "録音", "rec", bool(dialogue_ok), ("セッションを始める（録音と文字起こしを始める。確定するまで入力先へは書きません）"
                                             if dialogue_ok else "設定で AI対話モードを有効にしてください")


def commit_button_text(press_act: str, waiting: int = 0) -> str:
    """確定ボタンの文字。確定のとき、清書待ちの発話を待っているあいだ（waiting 件）は「（認識待ち N件…）」を添える。"""
    if press_act == L.PRESS_RETRY:
        return "未配達を再試行"
    return f"確定して届ける（認識待ち {int(waiting)}件…）" if int(waiting or 0) > 0 else "確定して届ける"


def pause_button_view(dialogue_active: bool, held: bool) -> tuple[bool, bool, str]:
    """保留ボタンの (押せるか, 保留中か, ツールチップ)。保留は、取り込みを止める（確定済みのものには影響しない）。"""
    return dialogue_active, held, ("保留を解除（取り込みを再開する）" if held
                                   else "保留（取り込みを止める。確定済みのものには影響しません）")


def fmt_clock(sec: float | None) -> str:
    """経過時間の表示。録音していないとき（None）は --:--。1時間未満は mm:ss、以上は h:mm:ss。"""
    if sec is None:
        return "--:--"
    s = max(0, int(sec))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


# ---- 喋っている長さ ----------------------------------------------------------------------------------------
def fmt_speaking(seconds: float | None) -> str:
    """喋っている途中の長さ。「喋っています 0:12」。喋っていない（None）ときは「—」。"""
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    return f"喋っています {s // 60}:{s % 60:02d}"


# ---- 録音全体の清書の進み（メイン画面の通知の行） -------------------------------------------------------------
def fmt_remaining(sec: float) -> str:
    """残り時間の言い方。1分未満は10秒単位、1時間未満は分、それ以上は時間と分。「あと約3分」。"""
    s = max(0.0, float(sec))
    if s < 60:
        return f"あと約{max(10, int(round(s / 10.0)) * 10)}秒"
    m = int(-(-s // 60))                       # 切り上げ
    if m < 60:
        return f"あと約{m}分"
    h, mm = divmod(m, 60)
    return f"あと約{h}時間" + (f"{mm}分" if mm else "")


GATED_TEXT = "清書待ち（録音が終わったら始めます）"


def finalize_progress_text(progress: float | None, elapsed: float, waiting: int = 0, running: bool = True,
                           gated: bool = False) -> str:
    """メイン画面の通知の行に出す「清書中 35%（あと約3分）」。待機があれば「（待機 1 件）」を添える。何も無いときは空文字。
    progress: 実行中の清書の進み 0..1（実行中でなければ running=False）。elapsed: その清書が始まってからの秒数。
    残り時間は、進み具合と経過時間から単純に見積もる（進みが小さい・経過が短いときは見積もれないので「あと約…」を省く）。"""
    wait = f"（待機 {int(waiting)} 件）" if waiting else ""
    if not running:
        if gated and waiting:        # セッション中は、録音全体の清書を始めない
            return GATED_TEXT
        return f"清書の待機 {int(waiting)} 件" if waiting else ""
    p = max(0.0, min(1.0, float(progress or 0.0)))
    left = ""
    if p >= 0.03 and float(elapsed) >= 5.0 and p < 1.0:
        left = "（" + fmt_remaining(float(elapsed) * (1.0 - p) / p) + "）"
    return f"清書中 {int(p * 100)}%{left}{wait}"


def draft_lines_height(line_h: int, wave_h: int, margin: int, lines: int = 3) -> int:
    """S3 の「直近3行」の高さ（px）。確定の行（lines-1 行）＋波形の行（帯の高さ）＋上下の余白。"""
    return int(2 * margin + max(0, lines - 1) * line_h + wave_h + 8)


# ---- 状態バッジ・無音カウントダウン・モデル表示・停止中の発話警告（メモ 1・2・6・8・11） -------------------------------------
SPEAK_SILENCE_SEC = 0.3      # 区切り器が「発話の途中」でも、最後の声からこれ以上の無音なら「いま話している」とは言わない
DONE_SHOW_SEC = 1.5          # 配達が成功した直後に「完了」を出す秒数
ALERT_TEXT = "録音していません。録音ボタンを押してください"
ALERT_START_SEC = 1.0         # 録音していないのに、これだけ続けて喋ると認識したら、警告（点滅）を始める。喋り終わるまで続く
ALERT_SEC = 8.0              # 警告を出している秒数（繰り返さない間隔は設定 idle_prompt_cooldown_sec。既定 60 秒）
ALERT_BLINK_PERIOD_SEC = 0.5          # 点滅の1周期（明⇄暗で1周）
ALERT_BLINK_ON = ("#ffffff", "#e5484d")      # (文字色, 背景色) 明るい側
ALERT_BLINK_OFF = ("#ffb3b6", "#5a1d20")     # 暗い側（文字は薄い赤、背景は暗い赤）
# 一時停止（保留）中に喋り続けたときの警告（アンバー）。録音されているものと誤解して喋り続ける事故を防ぐ
HOLD_ALERT_TEXT = "一時停止中です。いま喋っても取り込まれません"
ALERT_BLINK_ON_AMBER = ("#2a1a00", "#f5a623")
ALERT_BLINK_OFF_AMBER = ("#ffd58a", "#5a3d00")


def finalize_badge(finalize: str) -> tuple[str, str] | None:
    """録音全体の清書の進み文言（finalize_progress_text）を、バッジの (文字, 色) にする。空なら None。
    実行中（「清書中 …」）は橙、待機・待ち（「清書待ち」「清書の待機 N 件」）は黄「清書待ち」。"""
    t = (finalize or "").strip()
    if not t:
        return None
    if t.startswith("清書中"):
        return t, "busy"
    return "清書待ち", "warn"


def status_view(snap: dict | None, done: bool = False, alert: bool = False, finalize: str = "") -> tuple[str, str]:
    """状態バッジの (文字, 色の種類)。色の種類: good=緑 / warn=黄 / busy=橙 / info=青 / stop=灰 / alert=赤。
    優先順: 停止中の発話警告 > 送信中 > 認識中 > 保留中 > 録音中 > 完了（直後 DONE_SHOW_SEC） > 無音待機中。
    セッションをしていない（停止中）あいだは、警告が無く清書の仕事があれば「清書中 35%（あと約3分）」（橙）／「清書待ち」（黄）、
    なければ「停止中」。
    done: 配達が成功した直後か。alert: 停止中の発話警告の表示中（停止中を赤にする）。finalize: 清書の進み文言。"""
    s = snap or {}
    if not s.get("active"):
        if alert:
            return "停止中（話しています）", "alert"
        return finalize_badge(finalize) or ("停止中", "stop")
    if s.get("delivering"):
        return "送信中", "info"
    n = int(s.get("pending_recognitions") or 0)
    if n > 0:
        return f"認識中（{n}件）", "busy"
    if s.get("held"):
        return "保留中", "warn"
    if s.get("speaking") and float(s.get("silence_sec") or 0.0) < SPEAK_SILENCE_SEC:
        return "録音中", "good"
    if done:
        return "✓ 完了", "good"
    return "無音待機中", "warn"


def silence_countdown_text(snap: dict | None, gap_sec: float) -> str:
    """黙ってから、認識が始まる区切りまでの残り。「無音 0.5秒（あと0.7秒で認識開始）」。
    話している・発話が無い・保留中・停止中は空文字。「送信」は自動では行わないので、文言に使わない。"""
    s = snap or {}
    if not s.get("active") or s.get("held") or not s.get("speaking"):
        return ""
    sil = float(s.get("silence_sec") or 0.0)
    left = float(gap_sec) - sil
    if sil < SPEAK_SILENCE_SEC or left <= 0:
        return ""
    return f"無音 {sil:.1f}秒（あと{left:.1f}秒で認識開始）"


def models_text(recog: str, final: str, finalize_on: bool = True) -> str:
    """使用中モデルの常時表示。「認識 small ／ 清書 medium」。録音全体の清書をしないときは「清書 なし」。"""
    return f"認識 {recog or '-'} ／ 清書 {(final or '-') if finalize_on else 'なし'}"


def alert_blink_colors(elapsed: float, kind: str = "stop") -> tuple[str, str]:
    """警告の点滅の (文字色, 背景色)。elapsed は警告を出してからの秒数。明るい側で始まり、半周期ごとに入れ替わる。
    kind: "stop"（停止中の発話。赤）／"hold"（一時停止中の発話。アンバー）。"""
    phase = int(max(0.0, float(elapsed)) / (ALERT_BLINK_PERIOD_SEC / 2.0)) % 2
    if kind == "hold":
        return ALERT_BLINK_ON_AMBER if phase == 0 else ALERT_BLINK_OFF_AMBER
    return ALERT_BLINK_ON if phase == 0 else ALERT_BLINK_OFF


def idle_alert_due(*, run_sec: float, now: float, last_at: float | None, enabled: bool, monitoring: bool,
                   session_active: bool, cooldown_sec: float = 60.0, min_speech_sec: float = 1.5,
                   window_active: bool = True) -> bool:
    """停止中（セッションなし）にマイクが声を拾っているとき、警告を出すか。待機中のマイク確認が無効・窓が隠れている・
    セッション中・クールダウン中は出さない。"""
    if session_active or not monitoring:
        return False
    from idle_speech import should_prompt
    return should_prompt(run_sec, now, last_at, enabled=enabled, min_speech_sec=min_speech_sec,
                         cooldown_sec=cooldown_sec, window_active=window_active, recording=False)


def recognizing_label(model: str, eta) -> str:
    """下書き欄の「認識中」の行に出す文言。例「認識中（small・あと約12秒）」。見積もりが無い・ほぼ終わりのときは「あと」を省く。"""
    try:
        sec = float(eta)
    except Exception:
        sec = 0.0
    if sec >= 1.0:
        return f"認識中（{model}・あと約{int(round(sec))}秒）"
    return f"認識中（{model}・もうすぐ）"
