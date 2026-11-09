# stall_detect.py
# 見張り用:「Muse が宣言したのに動かない」の検知(純関数)。Qt・UIA は使わない。
#
# 【背景】
# Muse は承認の要る操作の直前に「push します」「【承認依頼します】」と宣言して
# ターンを終える作りだが、そのまま操作が実行されないまま止まることがある
# (2026-11-08 に test-env-fix と checklist-update の2回、実際に起きた)。
# チャットはデータ用なので、止まりに気づくのは画面を見ている見張りの仕事。
# この部品は「いま促すべきか」だけを判定し、促しの送信は見張り側が行う。
#
# 【入力】(すべて値で渡す。時刻は epoch 秒)
#   messages             Muse の直近の発言 [{"text": 本文, "at": 時刻}, ...]
#                        (古い順。Message データクラスでもよい)
#   cards                いま出ている承認カードの数
#   heading              見出し(チャットの題名など。「同じ件」の識別に使う)
#   last_screen_change_at 最後に画面が変わった時刻(無ければ None)
#   now                  現在時刻
#   nudges               過去の促し [{"matter": 件の識別子, "at": 時刻}, ...]
#   user_drafting        陽太さんが入力中(下書きがある)か
#   muse_responding      Muse が応答中(入力中表示が出ている)か
#   config               Config(宣言の語彙と時間の閾値)
#
# 【出力】Decision(nudge=促すか, text=促し文, reason=理由, matter=件の識別子)。
# 促した場合は matter を nudges に積んで次回渡すこと(回数と間隔の判定に使う)。

import re
from dataclasses import dataclass

# 承認依頼の合図(運用ルールで固定の文言)。
APPROVAL_SIGNAL = "【承認依頼します】"

# 宣言とみなす語彙の既定。「これから」は「これから〜します」の形を拾うため。
# 足りなければ Config.declarations で足せる(ルール5)。
DEFAULT_DECLARATIONS = (
    "push します",
    "pushします",
    "これから",
    "実行します",
    "送信します",
    "作成します",
    "追加します",
    "更新します",
)

# 促し文。ルール(1)は運用で決めた文言どおり。
TEXT_DECLARATION = "宣言した操作を実行して"
TEXT_SIGNAL = "【承認依頼します】のあと承認カードが出ていません。宣言した操作を実行して"


@dataclass(frozen=True)
class Config:
    declarations: tuple = DEFAULT_DECLARATIONS
    stall_seconds: float = 60.0         # ルール(1): 宣言から動きが無いとみなす時間
    signal_stall_seconds: float = 90.0  # ルール(2): 合図からカードが無いとみなす時間
    max_nudges: int = 3                 # ルール(3): 同じ件への促しの上限
    nudge_interval: float = 120.0       # ルール(3): 促しの最小間隔


@dataclass(frozen=True)
class Message:
    text: str
    at: float


@dataclass(frozen=True)
class Nudge:
    matter: str
    at: float


@dataclass(frozen=True)
class Decision:
    nudge: bool
    text: str = ""
    reason: str = ""
    matter: str = ""


def _get(item, key):
    """dict でもデータクラスでも読めるようにする。"""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def matter_of(heading, text) -> str:
    """「同じ件」の識別子。見出し + 宣言文の先頭(空白を畳む)。"""
    snippet = " ".join((text or "").split())[:60]
    return f"{heading or ''}|{snippet}"


def _is_signal(text) -> bool:
    return APPROVAL_SIGNAL in (text or "")


def _is_declaration(text, config: Config) -> bool:
    if _is_signal(text):
        return True
    body = text or ""
    return any(phrase in body for phrase in config.declarations)


def decide(messages, cards, heading, last_screen_change_at, now,
           nudges=(), user_drafting=False, muse_responding=False,
           config: Config = Config()) -> Decision:
    """促すべきかを判定する。理由は reason に短い名前で入る。"""
    # ルール(4): 人が書いている最中と、Muse が応答している最中は促さない。
    if user_drafting:
        return Decision(False, reason="user_drafting")
    if muse_responding:
        return Decision(False, reason="muse_responding")
    if not messages:
        return Decision(False, reason="no_messages")

    # 最後の発言が宣言でなければ対象外。宣言のあとに別の発言があれば、
    # それは「話が続いている」ので止まりではない。
    last = messages[-1]
    last_text = _get(last, "text") or ""
    if not _is_declaration(last_text, config):
        return Decision(False, reason="no_declaration")
    declared_at = float(_get(last, "at") or 0.0)
    matter = matter_of(heading, last_text)

    # カードが出ていれば、宣言した操作は進んでいる(承認待ちは止まりではない)。
    if cards and int(cards) > 0:
        return Decision(False, reason="card_present", matter=matter)

    # 宣言のあとに画面が変わっていれば、何か起きている最中とみなす。
    if last_screen_change_at is not None and float(last_screen_change_at) > declared_at:
        return Decision(False, reason="screen_active", matter=matter)

    signal = _is_signal(last_text)
    threshold = config.signal_stall_seconds if signal else config.stall_seconds
    if now - declared_at < threshold:
        return Decision(False, reason="waiting", matter=matter)

    # ルール(3): 同じ件への促しは上限まで。間隔も空ける。
    past = [n for n in nudges if _get(n, "matter") == matter]
    if len(past) >= config.max_nudges:
        return Decision(False, reason="capped", matter=matter)
    if past:
        last_nudge_at = max(float(_get(n, "at") or 0.0) for n in past)
        if now - last_nudge_at < config.nudge_interval:
            return Decision(False, reason="interval", matter=matter)

    if signal:
        return Decision(True, text=TEXT_SIGNAL, reason="signal_stall", matter=matter)
    return Decision(True, text=TEXT_DECLARATION, reason="declaration_stall", matter=matter)


# ---------------------------------------------------------------------------
# 状態タグ(2026-11-08、見張りとの取り決め)
#
# Muse は発言の最後の行に状態タグを1つ付ける: 【状態:実行中】 /
# 【状態:承認依頼】 / 【状態:完了】 / 【状態:Claude待ち】 / 【状態:陽太待ち】。
# 見張りはこのタグだけを根拠に「止まってよいか・促すか・何待ちか」を決める
# (文面の推測はしない)。ここはタグを読む側(解析と判定)で、decide() の
# 宣言検知とは別に、タグ単独の判定を返す。組み込みは見張り側。
#
# 形の約束: タグは「最後の行に、タグだけ」で置く。文中に埋めたものや、
# 最後の行より後ろに別の行があるものは、タグとして数えない(厳しめに倒す)。
# 従来の合図【承認依頼します】が最後の行にあれば、承認依頼と同じとみなす。
# コロンは半角・全角のどちらでもよい。
# ---------------------------------------------------------------------------

STATE_TAGS = ("実行中", "承認依頼", "完了", "Claude待ち", "陽太待ち")

# このタグのまま止まってよいもの。
REST_TAGS = ("完了", "Claude待ち", "陽太待ち")

# 何待ちか(完了は誰も待たないので None)。
WAITING_FOR = {"Claude待ち": "claude", "陽太待ち": "user"}

_TAG_RE = re.compile(r"【状態[:：]([^】]+)】")


@dataclass(frozen=True)
class TagVerdict:
    tag: str | None          # 読めた状態タグ(状態名そのもの)。無ければ None
    may_rest: bool           # このタグのまま止まってよいか
    should_nudge: bool       # このタグのまま止まったら促す対象か
    waiting_for: str | None  # "claude" / "user" / None


def parse_state_tag(text) -> str | None:
    """発言の最後の行にある状態タグを読む。無ければ None。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1]
    if last == APPROVAL_SIGNAL:
        return "承認依頼"
    m = _TAG_RE.fullmatch(last)
    if not m:
        return None
    state = m.group(1).strip()
    return state if state in STATE_TAGS else None


def tag_verdict(text) -> TagVerdict:
    """状態タグから、止まってよい/促す/何待ちかを返す。

    タグが無い発言については may_rest=False(止まってよいとは言わない)が、
    should_nudge も False にする。タグが無いこと自体では促さず、
    従来の宣言検知(decide)が拾う範囲に委ねる。
    """
    tag = parse_state_tag(text)
    if tag is None:
        return TagVerdict(None, False, False, None)
    if tag in REST_TAGS:
        return TagVerdict(tag, True, False, WAITING_FOR.get(tag))
    # 実行中・承認依頼は「まだ動いているはず」の申告。止まっていたら促す。
    return TagVerdict(tag, False, True, None)


# ---------------------------------------------------------------------------
# 承認カードの「未表示の疑い」(2026-11-08 の改善案2)
#
# 【背景】2026-11-08 12:24 の push で、承認カードが PC に出ず、陽太さんの
# スマホだけに出た。見張りのログには「出たカードを押した」記録しか
# 残らないため、出なかったことは観測できず、陽太さんの申告で初めて
# 分かった。そこで、Muse の自己申告(承認依頼のまま経過している)と
# 見張りの観測(自側の画面にカードが0件)を突き合わせ、「未表示の疑い」を
# 返す。組み込みは見張り側。
#
# 疑いは断定ではない。呼び出し内部のカード生成待ち(実測 213〜577 秒)
# の間も、見え方はまったく同じになる。だから閾値は、生成待ちの実測
# 最大より大きく取る。閾値を変えるときは実測を取り直してから。
# ---------------------------------------------------------------------------

# 「承認を待っている」申告の語彙。郵便受け status の state と、
# チャットの状態タグの両方を受ける。
WAITING_STATES = ("waiting_approval", "承認依頼")


@dataclass(frozen=True)
class AbsenceConfig:
    suspect_seconds: float = 600.0  # 承認依頼の申告から、この時間カードが
                                    # 見えなければ「疑い」。生成待ちの実測
                                    # 最大(577秒)より少し大きく取る


@dataclass(frozen=True)
class AbsenceVerdict:
    suspect: bool
    reason: str = ""
    waiting_seconds: float | None = None  # 承認依頼の申告からの経過秒数


def card_absence(self_state, self_updated_at, cards, now,
                 config: AbsenceConfig = AbsenceConfig()) -> AbsenceVerdict:
    """承認カードが「PC に出ていない」疑いがあるかを返す。

    self_state       Muse の自己申告の状態("waiting_approval" か
                     状態タグの "承認依頼")
    self_updated_at  その申告の時刻(epoch 秒。無ければ None)
    cards            見張りが自側の画面で観測した承認カードの数
    now              現在時刻(epoch 秒)
    """
    if self_state not in WAITING_STATES:
        return AbsenceVerdict(False, reason="not_waiting")
    if cards and int(cards) > 0:
        return AbsenceVerdict(False, reason="card_present")
    if self_updated_at is None:
        return AbsenceVerdict(False, reason="no_report")
    waiting = float(now) - float(self_updated_at)
    if waiting < config.suspect_seconds:
        # カード生成待ちの実測の範囲内。まだ様子見が正しい。
        return AbsenceVerdict(False, reason="within_generation",
                              waiting_seconds=waiting)
    return AbsenceVerdict(True, reason="suspected_absent",
                          waiting_seconds=waiting)
