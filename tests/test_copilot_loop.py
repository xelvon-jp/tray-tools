# tests/test_copilot_loop.py
# copilot_loop.Copilot の入力欄の照合(_find_input_box)と、読み書きの
# 段つきの失敗(InputStageError)を、fake の要素で確かめる。
# UIA には触らない(要素は Current* 属性を持つだけの偽物)。
#
# 照合を緩めた実機の事情(2026-11-14、Edge の中の Muse): 入力欄は
# AutomationId を持たず、クラスは Tailwind の長い token 列。完全一致に
# 賭けた旧実装は「入力欄が読めませんでした」の一文だけで落ちた。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_copilot_loop.py
import os
import struct
import sys
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import copilot_loop  # noqa: E402

# 実機で PHAL の調査が拾った入力欄のクラス(の形)。状態で token が
# 付いたり外れたりするので、保存した文字列と実物は少しずつずれる。
STORED_CLASS = (
    "text-text-primary block max-h-48 w-full resize-none bg-transparent "
    "p-0 text-[15px] leading-[24px] outline-none "
    "placeholder:text-transparent field-sizing-fixed min-h-6 "
    "overflow-y-auto")


class FakeValuePattern:
    """ValuePattern の偽物。要素の value を読み書きする。"""

    def __init__(self, element):
        self._element = element

    def QueryInterface(self, _interface):
        return self

    @property
    def CurrentValue(self):
        return self._element.value

    def SetValue(self, text):
        self._element.value = text


class FakeElement:
    """UIA 要素の偽物。copilot_loop が読む Current* 属性だけ持つ。"""

    def __init__(self, *, name="", class_name="", automation_id="",
                 control_type=None, focusable=True, has_value=True,
                 value=""):
        self.CurrentName = name
        self.CurrentClassName = class_name
        self.CurrentAutomationId = automation_id
        self.CurrentControlType = (
            copilot_loop.CONTROL_EDIT if control_type is None
            else control_type)
        self.CurrentIsKeyboardFocusable = focusable
        self._has_value = has_value
        self.value = value

    def GetCurrentPattern(self, _pattern_id):
        return FakeValuePattern(self) if self._has_value else None


class FakeDesc:
    """FindAll の結果(子孫の並び)の偽物。"""

    def __init__(self, elements):
        self._elements = list(elements)

    @property
    def Length(self):
        return len(self._elements)

    def GetElement(self, index):
        return self._elements[index]


def _copilot(profile, elements=()):
    """__init__(窓探しと COM 初期化)を飛ばした Copilot。

    _descendants だけ差し替えて、読み書きも fake の木で通せるようにする。"""
    cp = copilot_loop.Copilot.__new__(copilot_loop.Copilot)
    cp.profile = profile
    desc = FakeDesc(elements)
    cp._descendants = lambda heal=True: (None, desc)
    return cp


def _page_and_input(input_class):
    """実機の並び: ページ全体の Document と、本物の入力欄(Edit)。"""
    page = FakeElement(
        name="Chat — Muse", automation_id="RootWebArea",
        control_type=copilot_loop.CONTROL_DOCUMENT, class_name="")
    box = FakeElement(name="メッセージ", class_name=input_class)
    return page, box


def test_name_match_survives_class_drift():
    """名前が合えば、クラスが半分以上ずれていても入力欄と認める。

    実機の失敗の再現: 保存したクラス文字列と実物では、状態で付く
    token が入れ替わっていて重なりが半分未満。それでも名前
    「メッセージ」は安定しているので、名前の段で拾えなければ
    ならない(旧実装はクラス完全一致+名前の AND で落ちた)。"""
    drifted = "w-full resize-none bg-transparent p-0 min-h-6 other-a other-b"
    page, box = _page_and_input(drifted)
    assert copilot_loop.Copilot._class_overlap(STORED_CLASS, drifted) * 2 \
        < len(set(STORED_CLASS.split()))  # 前提: 重なりは半分未満
    cp = _copilot({"name": "phal-ai", "input_name": "メッセージ",
                   "input_class": STORED_CLASS})
    found, candidates = cp._find_input_box(FakeDesc([page, box]))
    assert found is box
    assert len(candidates) == 2  # ページも候補には入る(名前で外れる)


def test_name_tie_break_overlap_then_value_pattern_then_order():
    """同名が複数なら、クラスの重なり → 書き込める物 → 並び順で決める。"""
    profile = {"name": "phal-ai", "input_name": "メッセージ",
               "input_class": STORED_CLASS}
    low = FakeElement(name="メッセージ", class_name="w-full p-0",
                      has_value=True)
    high = FakeElement(name="メッセージ", class_name=STORED_CLASS,
                       has_value=False)
    cp = _copilot(profile)
    found, _ = cp._find_input_box(FakeDesc([low, high]))
    assert found is high  # 重なりが多い方が、書き込めなくても勝つ

    no_value = FakeElement(name="メッセージ", class_name=STORED_CLASS,
                           has_value=False)
    writable = FakeElement(name="メッセージ", class_name=STORED_CLASS,
                           has_value=True)
    found, _ = cp._find_input_box(FakeDesc([no_value, writable]))
    assert found is writable  # 重なりが同じなら書き込める物

    first = FakeElement(name="メッセージ", class_name=STORED_CLASS)
    second = FakeElement(name="メッセージ", class_name=STORED_CLASS)
    found, _ = cp._find_input_box(FakeDesc([first, second]))
    assert found is first  # 全部同じなら木の並び順(先に居る物)


def test_class_only_requires_half_the_tokens():
    """名前が無いプロファイルでは、クラスの重なりが半分以上で拾う。"""
    profile = {"name": "phal-ai", "input_class": "a b c d e f"}
    cp = _copilot(profile)
    half = FakeElement(name="欄", class_name="a b c x")
    found, _ = cp._find_input_box(FakeDesc([half]))
    assert found is half  # 6 token 中 3 = 半分ちょうど

    under = FakeElement(name="欄", class_name="a b x y")
    found, candidates = cp._find_input_box(FakeDesc([under]))
    assert found is None  # 2 では半分に届かない
    assert len(candidates) == 1  # 候補としては見えていた、と理由に残る


def test_not_found_message_lists_up_to_three_names():
    """見つからない段の文には、候補の件数と名前(最大3件)が入る。"""
    candidates = [(None, f"名前{i}", "", True) for i in range(1, 5)]
    message = copilot_loop._input_not_found_message(candidates)
    assert "4 件" in message
    assert "名前1" in message and "名前3" in message
    assert "名前4" not in message  # 4件目以降は出さない
    empty = copilot_loop._input_not_found_message([])
    assert "0 件" in empty


def test_staged_errors_for_read_and_write():
    """読み書きの失敗が、段(stage)と段の文で上がること。

    - 目印に合う要素が無い → not_found(候補の件数と名前つき)
    - 見つけた要素に ValuePattern が無い → 読む段は unreadable、
      書く段は unwritable
    - 読める要素では読み書きがそのまま通る"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = types.SimpleNamespace(
        IUIAutomationValuePattern=object())
    try:
        profile = {"name": "phal-ai", "input_name": "メッセージ"}

        other = FakeElement(name="別の欄")
        cp = _copilot(profile, [other])
        for call in (cp.read_input, lambda: cp.set_input("本文")):
            try:
                call()
            except copilot_loop.InputStageError as e:
                assert e.stage == "not_found"
                assert "1 件" in str(e) and "別の欄" in str(e)
            else:
                raise AssertionError("InputStageError になるはず")

        no_value = FakeElement(name="メッセージ", has_value=False)
        cp = _copilot(profile, [no_value])
        try:
            cp.read_input()
        except copilot_loop.InputStageError as e:
            assert e.stage == "unreadable"
            assert "中身を読めませんでした" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")
        try:
            cp.set_input("本文")
        except copilot_loop.InputStageError as e:
            assert e.stage == "unwritable"
        else:
            raise AssertionError("InputStageError になるはず")

        box = FakeElement(name="メッセージ", value="下書き")
        cp = _copilot(profile, [box])
        assert cp.read_input() == "下書き"
        cp.set_input("本文")
        assert box.value == "本文"
    finally:
        copilot_loop.UIA = saved_uia


class FakeInvokePattern:
    """InvokePattern の偽物。Invoke の呼び出しを要素に記録する。"""

    def __init__(self, element):
        self._element = element

    def QueryInterface(self, _interface):
        return self

    def Invoke(self):
        if self._element.invoke_raises:
            raise RuntimeError("押せない事情")
        self._element.invoked = True


class FakeButtonElement(FakeElement):
    """ボタンの偽物。InvokePattern だけを持ち、値の読み書きは無い。"""

    def __init__(self, *, has_invoke=True, invoke_raises=False, **kwargs):
        kwargs.setdefault("control_type", copilot_loop.CONTROL_BUTTON)
        kwargs.setdefault("has_value", False)
        super().__init__(**kwargs)
        self._has_invoke = has_invoke
        self.invoke_raises = invoke_raises
        self.invoked = False

    def GetCurrentPattern(self, pattern_id):
        if pattern_id == copilot_loop.INVOKE_PATTERN:
            return FakeInvokePattern(self) if self._has_invoke else None
        return None


def _pressable_copilot(profile, elements):
    cp = _copilot(profile, elements)
    cp.wait_awake = lambda timeout=3.0: True  # 木の組み上がり待ちは飛ばす
    return cp


def test_press_send_by_automation_id_first():
    """AutomationId があれば、名前より先にそれで合わせる。"""
    named = FakeButtonElement(name="送信")
    by_id = FakeButtonElement(name="別のボタン", automation_id="sendBtn")
    cp = _pressable_copilot(
        {"name": "phal-ai", "send_automation_id": "sendBtn",
         "send_name": "送信"}, [named, by_id])
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = types.SimpleNamespace(
        IUIAutomationInvokePattern=object())
    try:
        assert cp.press_send_button() is True
    finally:
        copilot_loop.UIA = saved_uia
    assert by_id.invoked and not named.invoked


def test_press_send_by_name_and_class_only():
    """名前の同名はクラスで切り分け、クラスだけでも半分の重なりで拾う。"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = types.SimpleNamespace(
        IUIAutomationInvokePattern=object())
    try:
        low = FakeButtonElement(name="送信", class_name="a x")
        high = FakeButtonElement(name="送信", class_name="a b c")
        cp = _pressable_copilot(
            {"name": "phal-ai", "send_name": "送信",
             "send_class": "a b c d"}, [low, high])
        assert cp.press_send_button() is True
        assert high.invoked and not low.invoked

        half = FakeButtonElement(name="送る", class_name="a b c x")
        cp = _pressable_copilot(
            {"name": "phal-ai", "send_class": "a b c d e f"}, [half])
        assert cp.press_send_button() is True
        assert half.invoked  # 6 token 中 3 = 半分ちょうど

        under = FakeButtonElement(name="送る", class_name="a b x y")
        cp = _pressable_copilot(
            {"name": "phal-ai", "send_class": "a b c d e f"}, [under])
        try:
            cp.press_send_button()
        except copilot_loop.InputStageError as e:
            assert e.stage == "not_found"
        else:
            raise AssertionError("InputStageError になるはず")
    finally:
        copilot_loop.UIA = saved_uia


def test_press_send_staged_errors():
    """送信ボタンの失敗が段つきで上がること。

    - 目印が無い → not_found (設定されていない旨)
    - 合うボタンが無い → not_found (候補の件数と名前つき)
    - InvokePattern が無い → unwritable
    - Invoke が例外 → unwritable"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = types.SimpleNamespace(
        IUIAutomationInvokePattern=object())
    try:
        cp = _pressable_copilot({"name": "phal-ai"}, [])
        try:
            cp.press_send_button()
        except copilot_loop.InputStageError as e:
            assert e.stage == "not_found"
            assert "目印が設定されていません" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")

        other = FakeButtonElement(name="キャンセル")
        cp = _pressable_copilot(
            {"name": "phal-ai", "send_name": "送信"}, [other])
        try:
            cp.press_send_button()
        except copilot_loop.InputStageError as e:
            assert e.stage == "not_found"
            assert "1 件" in str(e) and "キャンセル" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")

        no_invoke = FakeButtonElement(name="送信", has_invoke=False)
        cp = _pressable_copilot(
            {"name": "phal-ai", "send_name": "送信"}, [no_invoke])
        try:
            cp.press_send_button()
        except copilot_loop.InputStageError as e:
            assert e.stage == "unwritable"
            assert "InvokePattern" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")

        raising = FakeButtonElement(name="送信", invoke_raises=True)
        cp = _pressable_copilot(
            {"name": "phal-ai", "send_name": "送信"}, [raising])
        try:
            cp.press_send_button()
        except copilot_loop.InputStageError as e:
            assert e.stage == "unwritable"
            assert "押せませんでした" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")
    finally:
        copilot_loop.UIA = saved_uia


def test_scan_button_candidates_shape():
    """送信ボタンの調査が、目印と「押せるか」を付けて返すこと。"""
    send = FakeButtonElement(
        name="送信", class_name="send-btn", automation_id="sendBtn")
    plain = FakeButtonElement(name="戻る", has_invoke=False)
    box = FakeElement(name="メッセージ")  # ボタンでない物は入らない
    cp = _pressable_copilot({"name": "phal-ai"}, [send, plain, box])
    found = cp.scan_button_candidates()
    assert len(found) == 2
    first = found[0]
    assert first["name"] == "送信"
    assert first["automation_id"] == "sendBtn"
    assert first["class_name"] == "send-btn"
    assert first["invoke_pattern"] is True
    assert found[1]["invoke_pattern"] is False


CARD_CLASS = "group/card block max-w-full w-80 cursor-pointer"


def _card(name, **kwargs):
    """添付カードの偽物 (Button でクラスに group/card を含む)。"""
    return FakeButtonElement(name=name, class_name=CARD_CLASS, **kwargs)


def test_download_ignores_name_only_buttons():
    """名前に拡張子を含むだけのボタンは候補にしない (1-21 の誤検出)。

    サイドチャット一覧のスレッドボタンは、タイトルに ".phal" を
    含んでいても添付カードではない。group/card のカードが無ければ
    候補なしで、後ろの Text (文書名など) も拾わない。"""
    thread = FakeButtonElement(
        name="カスタム拡張子.phalのサポート実装とテスト",
        class_name="flex w-full items-start gap-2 py-2")
    page_text = FakeElement(
        name="Chat — Muse", control_type=copilot_loop.CONTROL_TEXT)
    cp = _copilot({"name": "phal-ai"}, [thread, page_text])
    assert cp.latest_download_candidate() is None


def test_download_picks_last_scripty_card():
    """カードの中で、スクリプトらしいものの最後 (= 最新) を選ぶ。

    木の順の最後に、名前に拡張子を含むだけのスレッドボタンが居ても
    (1-21 の実機の並び)、カードでない物は相手にしない。label と
    summary は、名前のコメント行から抜いた一文になる。"""
    old = _card("#!phal python\n# 1-20: 古いカード\nimport base64")
    new = _card("#!phal python\n# 1-21: 新しいカード\nimport base64")
    thread = FakeButtonElement(
        name="1-21_install_phal.phal の話", class_name="flex w-full")
    cp = _copilot({"name": "phal-ai"}, [old, new, thread])
    candidate = cp.latest_download_candidate()
    assert candidate["name"] == new.CurrentName
    assert candidate["class_name"] == CARD_CLASS
    assert candidate["label"] == "1-21: 新しいカード"
    assert candidate["summary"] == "1-21: 新しいカード"


def test_download_prefers_scripty_over_plain_card():
    """スクリプトらしいカードがあれば、後ろの地のカードより優先する。

    スクリプトらしいものが無ければ、カード全体の最後が候補になる。
    "#!" で始まらない名前からは要約を作らず、label は名前の
    切り詰めになる。"""
    script = _card("#!phal python\n# 1-21: スクリプト\nimport base64")
    plain = _card("photo.png のプレビュー")
    cp = _copilot({"name": "phal-ai"}, [script, plain])
    candidate = cp.latest_download_candidate()
    assert candidate["name"] == script.CurrentName

    first = _card("メモ.txt の中身")
    last = _card("photo.png のプレビュー")
    cp = _copilot({"name": "phal-ai"}, [first, last])
    candidate = cp.latest_download_candidate()
    assert candidate["name"] == last.CurrentName
    assert candidate["summary"] == ""
    assert candidate["label"] == "photo.png のプレビュー"


def test_download_summary_from_flattened_name():
    """名前が1行に潰れていても、コメントの一文を抜ける。

    shebang と同じ行に "# 説明" が続き、その後ろにコードが続く形
    (実機のカード名の形)。コードの始まりで切る。"""
    flat = _card("#!phal python # 1-21: チャット確認とDLの行 "
                 "import base64 import hashlib")
    cp = _copilot({"name": "phal-ai"}, [flat])
    candidate = cp.latest_download_candidate()
    assert candidate["summary"] == "1-21: チャット確認とDLの行"
    assert candidate["label"] == "1-21: チャット確認とDLの行"
    # 切り出しそのもの: コメントが無い名前からは作らない
    assert copilot_loop._download_summary("ただのテキスト") == ""
    assert copilot_loop._download_summary(
        "#!/usr/bin/env python3\nprint('x')") == ""


def _text_el(name):
    """会話の本文 (Text 要素) の偽物。"""
    return FakeElement(name=name,
                       control_type=copilot_loop.CONTROL_TEXT)


def test_diag_marker_scan_finds_latest_request():
    """診断リクエストの目印 (1-43): 最新の発言の中の単独行を拾い、
    同定子が安定すること。前の会話が変われば同定子も変わる。"""
    els = [
        _text_el("前の発言の本文です"),
        _text_el("了解です。状態を見ます。\n"
                 + copilot_loop.DIAG_REQUEST_MARKER + "\n"),
        _text_el("Copilot へメッセージを送る"),
    ]
    cp = _copilot({"name": "phal-ai"}, els)
    scan = cp.latest_chat_scan()
    assert scan["candidate"] is None
    assert scan["marker"] is not None and scan["marker"]["id"]
    # 同じ木をもう一度走査しても、同定子は同じ (消費済みの判定が
    # 安定する根拠)。
    assert cp.latest_chat_scan()["marker"]["id"] \
        == scan["marker"]["id"]
    # 目印より前に別の会話が積み上がれば、別の発言として扱う。
    cp2 = _copilot({"name": "phal-ai"}, [_text_el("別の序文")] + els)
    assert cp2.latest_chat_scan()["marker"]["id"] \
        != scan["marker"]["id"]


def test_diag_marker_key_stable_across_prefix_drift():
    """第2の同定子 key (1-49): 目印より40行以上前に会話が積み
    上がって id が変わっても、目印のある発言そのものが同じなら
    key は同じ。発言の文言が違えば key も違う。"""
    filler = _text_el("\n".join(f"昔の会話{i}" for i in range(45)))
    utter = _text_el("了解です。状態を見ます。\n"
                     + copilot_loop.DIAG_REQUEST_MARKER + "\n")
    els = [filler, utter, _text_el("Copilot へメッセージを送る")]
    cp = _copilot({"name": "phal-ai"}, els)
    first = cp.latest_chat_scan()["marker"]
    assert first is not None and first["id"] and first["key"]
    # 目印より前に別の会話が積み上がる (id は変わる。窓の外)。
    cp2 = _copilot({"name": "phal-ai"}, [_text_el("別の序文")] + els)
    second = cp2.latest_chat_scan()["marker"]
    assert second is not None
    assert second["id"] != first["id"]
    assert second["key"] == first["key"]
    # 目印のある発言の文言が違えば key も違う。
    other = _copilot({"name": "phal-ai"}, [
        filler,
        _text_el("別の返事です。\n"
                 + copilot_loop.DIAG_REQUEST_MARKER + "\n"),
        _text_el("Copilot へメッセージを送る")])
    assert other.latest_chat_scan()["marker"]["key"] != first["key"]


def test_diag_marker_scan_ignores_absent_partial_and_old():
    """目印が無い・行の一部・古い発言の中、は数えない (1-43)。"""
    cp = _copilot({"name": "phal-ai"}, [_text_el("ただの本文")])
    assert cp.latest_chat_scan()["marker"] is None
    partial = _copilot({"name": "phal-ai"}, [
        _text_el("これは " + copilot_loop.DIAG_REQUEST_MARKER
                 + " の説明です")])
    assert partial.latest_chat_scan()["marker"] is None
    long_tail = "あ" * (copilot_loop.DIAG_MARKER_TAIL_LIMIT + 100)
    old = _copilot({"name": "phal-ai"}, [
        _text_el(copilot_loop.DIAG_REQUEST_MARKER),
        _text_el(long_tail)])
    assert old.latest_chat_scan()["marker"] is None


def test_diag_marker_scan_alongside_candidate():
    """候補と目印は、同じ走査で両方取れる (1-43)。"""
    card = _card("#!phal python\n# 1-43: 目印と同時のカード\n"
                 "import base64")
    els = [card, _text_el(copilot_loop.DIAG_REQUEST_MARKER)]
    cp = _copilot({"name": "phal-ai"}, els)
    scan = cp.latest_chat_scan()
    assert scan["candidate"] is not None
    assert scan["candidate"]["name"] == card.CurrentName
    assert scan["marker"] is not None


def test_reset_marker_scan_finds_latest_request():
    """リセットの目印 (1-52): 診断の目印と同じ流儀で、最新の
    発言の中の単独行を拾い、同定子 (id / key) が安定すること。
    診断の目印とは独立に判定する (片方だけでは他方は立たない)。"""
    els = [
        _text_el("前の発言の本文です"),
        _text_el("了解です。開き直します。\n"
                 + copilot_loop.RESET_REQUEST_MARKER + "\n"),
        _text_el("Copilot へメッセージを送る"),
    ]
    cp = _copilot({"name": "phal-ai"}, els)
    scan = cp.latest_chat_scan()
    assert scan["candidate"] is None
    assert scan["marker"] is None  # 診断の目印は立たない
    reset = scan["reset_marker"]
    assert reset is not None and reset["id"] and reset["key"]
    # 同じ木をもう一度走査しても、同定子は同じ (消費済みの判定が
    # 安定する根拠)。
    assert cp.latest_chat_scan()["reset_marker"] == reset
    # 目印より前に別の会話が積み上がれば、別の発言として扱う。
    cp2 = _copilot({"name": "phal-ai"}, [_text_el("別の序文")] + els)
    assert cp2.latest_chat_scan()["reset_marker"]["id"] \
        != reset["id"]
    # 第2の同定子 key (1-49 と同じ作り): 目印より40行以上前に
    # 会話が積み上がって id が変わっても、目印のある発言そのも
    # のが同じなら key は同じ。
    filler = _text_el("\n".join(f"昔の会話{i}" for i in range(45)))
    utter = _text_el("了解です。開き直します。\n"
                     + copilot_loop.RESET_REQUEST_MARKER + "\n")
    base = [filler, utter, _text_el("Copilot へメッセージを送る")]
    first = _copilot({"name": "phal-ai"}, base) \
        .latest_chat_scan()["reset_marker"]
    drifted = _copilot({"name": "phal-ai"},
                       [_text_el("別の序文")] + base) \
        .latest_chat_scan()["reset_marker"]
    assert first is not None and drifted is not None
    assert drifted["id"] != first["id"]
    assert drifted["key"] == first["key"]
    # 診断の目印だけの発言では、リセットの目印は立たない。
    diag_only = _copilot({"name": "phal-ai"}, [
        _text_el("状態を見ます。\n"
                 + copilot_loop.DIAG_REQUEST_MARKER + "\n")])
    scan2 = diag_only.latest_chat_scan()
    assert scan2["marker"] is not None
    assert scan2["reset_marker"] is None


def test_reset_marker_scan_ignores_absent_partial_and_old():
    """目印が無い・行の一部・古い発言の中、は数えない (1-52)。"""
    cp = _copilot({"name": "phal-ai"}, [_text_el("ただの本文")])
    assert cp.latest_chat_scan()["reset_marker"] is None
    partial = _copilot({"name": "phal-ai"}, [
        _text_el("これは " + copilot_loop.RESET_REQUEST_MARKER
                 + " の説明です")])
    assert partial.latest_chat_scan()["reset_marker"] is None
    long_tail = "あ" * (copilot_loop.DIAG_MARKER_TAIL_LIMIT + 100)
    old = _copilot({"name": "phal-ai"}, [
        _text_el(copilot_loop.RESET_REQUEST_MARKER),
        _text_el(long_tail)])
    assert old.latest_chat_scan()["reset_marker"] is None


class FakeExpandPattern:
    """ExpandCollapsePattern の偽物。Expand の呼び出しを要素に記録する。"""

    def __init__(self, element):
        self._element = element

    def QueryInterface(self, _interface):
        return self

    def Expand(self):
        self._element.expanded = True


class FakeOptionsButton(FakeButtonElement):
    """「その他のオプション」の偽物。Invoke は持たず ExpandCollapse で開く
    (実機の全ボタン調査で複数あった形)。"""

    def __init__(self, **kwargs):
        kwargs.setdefault("has_invoke", False)
        super().__init__(name="その他のオプション", **kwargs)
        self.expanded = False

    def GetCurrentPattern(self, pattern_id):
        if pattern_id == copilot_loop.EXPAND_COLLAPSE_PATTERN:
            return FakeExpandPattern(self)
        return super().GetCurrentPattern(pattern_id)


def _menu_item(**kwargs):
    """メニューの「ダウンロード」項目の偽物 (MenuItem で Invoke 持ち)。"""
    kwargs.setdefault("control_type", copilot_loop.CONTROL_MENU_ITEM)
    return FakeButtonElement(name="ダウンロード", **kwargs)


def _download_flow_copilot(before, after, trigger):
    """「…」が押される前と後で木が変わる Copilot。

    trigger (「…」ボタン) が invoked / expanded になったら、after の
    木 (メニューが開いた状態) を返す。"""
    cp = copilot_loop.Copilot.__new__(copilot_loop.Copilot)
    cp.profile = {"name": "phal-ai"}

    def descendants(heal=True):
        opened = getattr(trigger, "invoked", False) or \
            getattr(trigger, "expanded", False)
        return None, FakeDesc(after if opened else before)

    cp._descendants = descendants
    return cp


def _patched_uia():
    return types.SimpleNamespace(
        IUIAutomationInvokePattern=object(),
        IUIAutomationExpandCollapsePattern=object(),
        IUIAutomationSelectionItemPattern=object(),
        IUIAutomationTogglePattern=object(),
        IUIAutomationLegacyIAccessiblePattern=object(),
        TreeScope_Children=2,
        TreeScope_Descendants=4)


def test_press_download_two_steps_invoke():
    """押下は2段: カードは押さず、「…」→「ダウンロード」の順に押す。

    1-22 まではカード自体を Invoke していたが、実機ではプレビューが
    開くだけでダウンロードは始まらなかった (2026-11-15)。"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = _patched_uia()
    try:
        card = _card("#!phal python\n# 1-23: 2段で落とす\nimport base64")
        options = FakeButtonElement(name="その他のオプション")
        item = _menu_item()
        cp = _download_flow_copilot(
            [card, options], [card, options, item], options)
        assert cp.press_download_candidate() is True
        assert options.invoked and item.invoked
        assert not card.invoked  # カード自体は押さない
    finally:
        copilot_loop.UIA = saved_uia


def test_press_download_picks_latest_card_options():
    """押す「…」は、選ばれた (最新の) カードの発言のもの。

    古い発言の「…」がカードより前に居ても、探すのはカードの後ろ
    だけなので、取り違えない。"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = _patched_uia()
    try:
        old = _card("#!phal python\n# 1-20: 古いカード\nimport base64")
        old_options = FakeButtonElement(name="その他のオプション")
        new = _card("#!phal python\n# 1-21: 新しいカード\nimport base64")
        new_options = FakeButtonElement(name="その他のオプション")
        item = _menu_item()
        before = [old, old_options, new, new_options]
        after = before + [item]
        cp = _download_flow_copilot(before, after, new_options)
        assert cp.press_download_candidate() is True
        assert new_options.invoked and not old_options.invoked
        assert item.invoked
    finally:
        copilot_loop.UIA = saved_uia


def test_download_scan_returns_all_scripty_candidates():
    """走査は scripty カード全件を candidates に返す (1-57)。

    candidate は従来どおり最後の1件。地のカード (ファイル名を
    含まない文章) や、名前に拡張子を含むだけのボタンは
    candidates にも入らない。"""
    old = _card("#!phal python\n# 1-20: 古いカード\nimport base64")
    plain = _card("ただの文章のカードです")
    named = FakeButtonElement(
        name="カスタム拡張子.phalのサポート",
        class_name="flex w-full items-start")
    new = _card("run.ps1\n# 1-21: 新しいカード")
    cp = _copilot({"name": "phal-ai"}, [old, plain, named, new])
    scan = cp.latest_chat_scan()
    assert [c["name"] for c in scan["candidates"]] == [
        old.CurrentName, new.CurrentName]
    assert scan["candidate"]["name"] == new.CurrentName


def test_press_download_with_key_picks_that_card():
    """key を渡すと、その候補 (手前のカード) の「…」を押す (1-57)。

    窓側が選んだ候補を押し直す形。照合は名前+クラス名の完全
    一致で、名前だけでは合わせない。"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = _patched_uia()
    try:
        old = _card("#!phal python\n# 1-20: 古いカード\nimport base64")
        old_options = FakeButtonElement(name="その他のオプション")
        new = _card("#!phal python\n# 1-21: 新しいカード\nimport base64")
        new_options = FakeButtonElement(name="その他のオプション")
        item = _menu_item()
        before = [old, old_options, new, new_options]
        after = before + [item]

        def descendants(heal=True):
            opened = old_options.invoked or new_options.invoked
            return None, FakeDesc(after if opened else before)

        cp = copilot_loop.Copilot.__new__(copilot_loop.Copilot)
        cp.profile = {"name": "phal-ai"}
        cp._descendants = descendants
        key = {"name": old.CurrentName,
               "class_name": old.CurrentClassName}
        assert cp.press_download_candidate(key) is True
        assert old_options.invoked and not new_options.invoked
        assert item.invoked
    finally:
        copilot_loop.UIA = saved_uia


def test_press_download_with_unknown_key_falls_back_to_latest():
    """key に一致するカードが無ければ、従来の最新カードに倒す。"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = _patched_uia()
    try:
        card = _card("#!phal python\n# 1-23: 2段で落とす\nimport base64")
        options = FakeButtonElement(name="その他のオプション")
        item = _menu_item()
        cp = _download_flow_copilot(
            [card, options], [card, options, item], options)
        key = {"name": "もう無いカード", "class_name": CARD_CLASS}
        assert cp.press_download_candidate(key) is True
        assert options.invoked and item.invoked
    finally:
        copilot_loop.UIA = saved_uia


def test_press_download_options_via_expand():
    """「…」に Invoke が無ければ ExpandCollapse で開く (実機の形)。"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = _patched_uia()
    try:
        card = _card("#!phal python\n# 1-23: 展開で開く\nimport base64")
        options = FakeOptionsButton()
        item = _menu_item()
        cp = _download_flow_copilot(
            [card, options], [card, options, item], options)
        assert cp.press_download_candidate() is True
        assert options.expanded and item.invoked
    finally:
        copilot_loop.UIA = saved_uia


def test_press_download_not_found_stages():
    """見つからない段: カードが無い / 「…」が無い / 次のカードで打ち切り。"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = _patched_uia()
    try:
        thread = FakeButtonElement(
            name="1-21_install_phal.phal の話", class_name="flex w-full")
        cp = _copilot({"name": "phal-ai"}, [thread])
        try:
            cp.press_download_candidate()
        except copilot_loop.InputStageError as e:
            assert e.stage == "not_found"
        else:
            raise AssertionError("InputStageError になるはず")

        card = _card("#!phal python\n# 1-23: 一人ぼっち\nimport base64")
        cp = _copilot({"name": "phal-ai"}, [card])
        try:
            cp.press_download_candidate()
        except copilot_loop.InputStageError as e:
            assert e.stage == "not_found"
            assert "その他のオプション" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")

        # スクリプトカードの後ろに地のカードが居ると、そこで打ち切る。
        # その先の「…」は地のカードの発言のものなので、押さない。
        plain = _card("photo.png のプレビュー")
        options = FakeButtonElement(name="その他のオプション")
        cp = _copilot({"name": "phal-ai"}, [card, plain, options])
        try:
            cp.press_download_candidate()
        except copilot_loop.InputStageError as e:
            assert e.stage == "not_found"
            assert "その他のオプション" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")
        assert not options.invoked
    finally:
        copilot_loop.UIA = saved_uia


def test_press_download_options_without_pattern():
    """「…」はあるが、押す仕組みがどちらも無ければ unwritable。"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = _patched_uia()
    try:
        card = _card("#!phal python\n# 1-23: 押せない\nimport base64")
        options = FakeButtonElement(
            name="その他のオプション", has_invoke=False)
        cp = _copilot({"name": "phal-ai"}, [card, options])
        try:
            cp.press_download_candidate()
        except copilot_loop.InputStageError as e:
            assert e.stage == "unwritable"
            assert "押す仕組み" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")
    finally:
        copilot_loop.UIA = saved_uia


def test_press_download_menu_never_appears():
    """「…」は押せたが「ダウンロード」が出なければ not_found。

    待ち時間は 0 に潰して確かめる (窓の外の探し直しは、fake には
    uia が無いので、そのまま「無い」に落ちる)。"""
    saved_uia = copilot_loop.UIA
    saved_timeout = copilot_loop.DOWNLOAD_MENU_TIMEOUT
    copilot_loop.UIA = _patched_uia()
    copilot_loop.DOWNLOAD_MENU_TIMEOUT = 0.0
    try:
        card = _card("#!phal python\n# 1-23: メニューが出ない\nimport x")
        options = FakeButtonElement(name="その他のオプション")
        cp = _download_flow_copilot(
            [card, options], [card, options], options)
        try:
            cp.press_download_candidate()
        except copilot_loop.InputStageError as e:
            assert e.stage == "not_found"
            assert "ダウンロード" in str(e) and "メニュー" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")
        assert options.invoked  # 「…」の段までは進んでいる
    finally:
        copilot_loop.UIA = saved_uia
        copilot_loop.DOWNLOAD_MENU_TIMEOUT = saved_timeout


class _FakeChainPattern:
    """押下の代替手順用のパターンの偽物。呼ばれたメソッド名を
    要素の pressed_via に記録する (どの仕組みで押せたかを見る)。"""

    def QueryInterface(self, _interface):
        return self

    def __init__(self, element):
        self._element = element

    def Invoke(self):
        self._element.pressed_via.append("Invoke")

    def Expand(self):
        self._element.pressed_via.append("ExpandCollapse")

    def Collapse(self):
        self._element.pressed_via.append("Collapse")

    def Select(self):
        self._element.pressed_via.append("SelectionItem")

    def Toggle(self):
        self._element.pressed_via.append("Toggle")

    def DoDefaultAction(self):
        self._element.pressed_via.append("LegacyIAccessible")


class FakePressableElement(FakeButtonElement):
    """押す仕組みを好きに付け替えられる要素 (DL 項目の代替手順用)。

    patterns は持っているパターンIDの集合。rect は
    CurrentBoundingRectangle (クリックの落ち先の確認用)。
    parent はデスクトップ探索のメニュー文脈の確認用。"""

    def __init__(self, *, patterns=(), rect=None, parent=None,
                 **kwargs):
        kwargs.setdefault("has_invoke", False)
        super().__init__(**kwargs)
        self._patterns = set(patterns)
        self.pressed_via = []
        self.parent = parent
        if rect is not None:
            self.CurrentBoundingRectangle = types.SimpleNamespace(
                left=rect[0], top=rect[1], right=rect[2],
                bottom=rect[3])

    def GetCurrentPattern(self, pattern_id):
        if pattern_id in self._patterns:
            return _FakeChainPattern(self)
        return None


def _chain_item(**kwargs):
    """「ダウンロード」項目の偽物 (代替手順の確認用)。"""
    kwargs.setdefault("control_type", copilot_loop.CONTROL_MENU_ITEM)
    kwargs.setdefault("name", "ダウンロード")
    return FakePressableElement(**kwargs)


def test_press_download_item_chain_order():
    """仕組みが複数あれば、優先順 (Invoke → … → SelectionItem →
    Toggle → Legacy) の早い方で押す (1-32)。"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = _patched_uia()
    try:
        cases = [
            ({copilot_loop.INVOKE_PATTERN,
              copilot_loop.SELECTION_ITEM_PATTERN}, "Invoke"),
            ({copilot_loop.SELECTION_ITEM_PATTERN,
              copilot_loop.TOGGLE_PATTERN}, "SelectionItem"),
            ({copilot_loop.TOGGLE_PATTERN,
              copilot_loop.LEGACY_ACCESSIBLE_PATTERN}, "Toggle"),
            ({copilot_loop.LEGACY_ACCESSIBLE_PATTERN},
             "LegacyIAccessible"),
        ]
        for patterns, expected in cases:
            card = _card("#!phal python\n# 1-32: 優先順\nimport x")
            options = FakeButtonElement(name="その他のオプション")
            item = _chain_item(patterns=patterns)
            cp = _download_flow_copilot(
                [card, options], [card, options, item], options)
            assert cp.press_download_candidate() is True
            assert item.pressed_via == [expected], (patterns, item.pressed_via)
    finally:
        copilot_loop.UIA = saved_uia


def test_press_download_item_select_only():
    """Invoke が無く SelectionItem だけの項目は Select で押す
    (実機サイクル2の形、1-32)。"""
    saved_uia = copilot_loop.UIA
    copilot_loop.UIA = _patched_uia()
    try:
        card = _card("#!phal python\n# 1-32: Selectだけ\nimport x")
        options = FakeButtonElement(name="その他のオプション")
        item = _chain_item(
            patterns={copilot_loop.SELECTION_ITEM_PATTERN})
        cp = _download_flow_copilot(
            [card, options], [card, options, item], options)
        assert cp.press_download_candidate() is True
        assert item.pressed_via == ["SelectionItem"]
    finally:
        copilot_loop.UIA = saved_uia


def test_press_download_item_click_fallback():
    """仕組みが1つも無い項目は、外接矩形の中心をクリックする
    (最終手段、1-32)。クリック自体は差し替えて座標だけ見る。"""
    saved_uia = copilot_loop.UIA
    saved_click = copilot_loop._click_at
    copilot_loop.UIA = _patched_uia()
    clicks = []
    copilot_loop._click_at = lambda x, y: clicks.append((x, y))
    try:
        card = _card("#!phal python\n# 1-32: クリックで押す\nimport x")
        options = FakeButtonElement(name="その他のオプション")
        item = _chain_item(rect=(100, 200, 180, 240))
        cp = _download_flow_copilot(
            [card, options], [card, options, item], options)
        assert cp.press_download_candidate() is True
        assert clicks == [(140, 220)]  # 矩形の中心
        assert item.pressed_via == []
    finally:
        copilot_loop.UIA = saved_uia
        copilot_loop._click_at = saved_click


def test_press_download_item_unpressable_retries_once():
    """仕組みが無く位置も取れない項目は、見つけ直し+押下を
    1回だけやり直してから unwritable (1-32)。

    旧仕様 (1-23) では「Invoke が無い」時点で即 unwritable だった。
    今は代替手順と再試行を尽くしてから止まる。"""
    saved_uia = copilot_loop.UIA
    saved_wait = copilot_loop.DOWNLOAD_PRESS_RETRY_WAIT
    copilot_loop.UIA = _patched_uia()
    copilot_loop.DOWNLOAD_PRESS_RETRY_WAIT = 0.0
    try:
        card = _card("#!phal python\n# 1-32: どうしても押せない\nimport x")
        options = FakeButtonElement(name="その他のオプション")
        item = _chain_item()  # 仕組みなし・位置なし
        cp = _download_flow_copilot(
            [card, options], [card, options, item], options)
        finds = []
        original_wait = cp._wait_download_item

        def counting_wait():
            finds.append(1)
            return original_wait()

        cp._wait_download_item = counting_wait
        try:
            cp.press_download_candidate()
        except copilot_loop.InputStageError as e:
            assert e.stage == "unwritable"
            assert "クリックも効きませんでした" in str(e)
            assert "窓の木" in str(e)  # 窓の木で見つけた項目だと分かる
        else:
            raise AssertionError("InputStageError になるはず")
        assert len(finds) == 2  # 初回 + 再試行1回
    finally:
        copilot_loop.UIA = saved_uia
        copilot_loop.DOWNLOAD_PRESS_RETRY_WAIT = saved_wait


class _FakeWalker:
    """RawViewWalker の偽物。要素の parent 属性をたどる。"""

    def GetParentElement(self, el):
        return getattr(el, "parent", None)


class _FakeDesktopRoot:
    """デスクトップの root の偽物。FindAll は固定の並びを返す。"""

    def __init__(self, elements):
        self._elements = elements

    def FindAll(self, _scope, _cond):
        return FakeDesc(self._elements)


def _desktop_copilot(before, after, trigger, desktop_elements):
    """窓の木に項目が無く、デスクトップ側にだけ要素がある Copilot。"""
    cp = _download_flow_copilot(before, after, trigger)
    cp.uia = types.SimpleNamespace(
        RawViewWalker=_FakeWalker(),
        GetRootElement=lambda: _FakeDesktopRoot(desktop_elements))
    cp.true_cond = object()
    return cp


def test_press_download_desktop_stray_rejected():
    """デスクトップ側の同名要素がメニューの外 (フライアウト等)
    にあれば、項目とは認めず not_found (1-32)。

    文には、デスクトップに同名要素があったことが残る。"""
    saved_uia = copilot_loop.UIA
    saved_timeout = copilot_loop.DOWNLOAD_MENU_TIMEOUT
    copilot_loop.UIA = _patched_uia()
    copilot_loop.DOWNLOAD_MENU_TIMEOUT = 0.0
    try:
        card = _card("#!phal python\n# 1-32: フライアウト\nimport x")
        options = FakeButtonElement(name="その他のオプション")
        stray = _chain_item(
            control_type=copilot_loop.CONTROL_LIST_ITEM,
            patterns={copilot_loop.INVOKE_PATTERN})
        cp = _desktop_copilot(
            [card, options], [card, options], options, [stray])
        try:
            cp.press_download_candidate()
        except copilot_loop.InputStageError as e:
            assert e.stage == "not_found"
            assert "デスクトップ" in str(e)
            assert "メニュー" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")
        assert stray.pressed_via == []  # 余所の要素は押さない
    finally:
        copilot_loop.UIA = saved_uia
        copilot_loop.DOWNLOAD_MENU_TIMEOUT = saved_timeout


def test_press_download_desktop_menu_item_accepted():
    """デスクトップ側でも、Menu の中にある項目なら使う (1-32)。"""
    saved_uia = copilot_loop.UIA
    saved_timeout = copilot_loop.DOWNLOAD_MENU_TIMEOUT
    copilot_loop.UIA = _patched_uia()
    copilot_loop.DOWNLOAD_MENU_TIMEOUT = 0.0
    try:
        card = _card("#!phal python\n# 1-32: 外のメニュー\nimport x")
        options = FakeButtonElement(name="その他のオプション")
        menu = FakeElement(
            name="", control_type=copilot_loop.CONTROL_MENU,
            has_value=False)
        item = _chain_item(
            patterns={copilot_loop.INVOKE_PATTERN}, parent=menu)
        cp = _desktop_copilot(
            [card, options], [card, options], options, [item])
        assert cp.press_download_candidate() is True
        assert item.pressed_via == ["Invoke"]
    finally:
        copilot_loop.UIA = saved_uia
        copilot_loop.DOWNLOAD_MENU_TIMEOUT = saved_timeout


class FakeTreeElement(FakePressableElement):
    """子を持てる要素 (フライアウト探索用、1-40)。

    プロセス ID と窓ハンドルも持つ。FindAll は
    TreeScope_Children のときだけ子を返す (浅い探索の真似)。"""

    def __init__(self, *, pid=0, hwnd=0, children=(), **kwargs):
        super().__init__(**kwargs)
        self.CurrentProcessId = pid
        self.CurrentNativeWindowHandle = hwnd
        self._children = list(children)

    def FindAll(self, scope, _cond):
        if scope == getattr(copilot_loop.UIA, "TreeScope_Children", 2):
            return FakeDesc(self._children)
        return FakeDesc([])


class _OrderButton(FakeButtonElement):
    """押された順序を共有リストに残すボタン (1-40 の順序確認用)。

    on_press に関数を入れると、Invoke のたびにそれも呼ぶ
    (フライアウトが閉じてデスクトップから消える所まで真似る)。"""

    def __init__(self, *, log, tag, **kwargs):
        super().__init__(**kwargs)
        self._log = log
        self._tag = tag
        self.on_press = None

    def GetCurrentPattern(self, pattern_id):
        pattern = super().GetCurrentPattern(pattern_id)
        if pattern is None:
            return None
        element = self

        class _Recorder:
            def QueryInterface(self, _interface):
                return self

            def Invoke(self):
                element._log.append(element._tag)
                pattern.Invoke()
                if element.on_press is not None:
                    element.on_press()

        return _Recorder()


class _MutableDesktopRoot:
    """中身 (トップレベル要素の並び) が差し替えられる
    デスクトップ root の偽物。フライアウトが閉じて消える動きを
    共有の辞書経由で差し替える。"""

    def __init__(self, holder):
        self._holder = holder

    def FindAll(self, _scope, _cond):
        return FakeDesc(self._holder["tops"])


FLYOUT_PID = 4242


def _flyout_copilot(before, after, trigger, holder, pid=FLYOUT_PID):
    """デスクトップ側にフライアウトの偽物がある Copilot (1-40)。"""
    cp = _download_flow_copilot(before, after, trigger)
    cp.uia = types.SimpleNamespace(
        RawViewWalker=_FakeWalker(),
        GetRootElement=lambda: _MutableDesktopRoot(holder))
    cp.true_cond = object()
    if pid is not None:
        cp._target_process_id = lambda: pid
    return cp


def _flyout_panel(pid, close=None, children=(), **kwargs):
    """フライアウトのパネルの偽物 (Pane で名前「ダウンロード」)。"""
    kids = list(children)
    if close is not None:
        kids.insert(0, close)
    kwargs.setdefault("control_type", copilot_loop.CONTROL_PANE)
    kwargs.setdefault("name", "ダウンロード")
    return FakeTreeElement(pid=pid, children=kids, **kwargs)


def test_press_download_dismisses_flyout_first():
    """フライアウトが開いていれば、カードの「…」より先に
    その「閉じる」を押してから2段に進む (1-40)。"""
    saved_uia = copilot_loop.UIA
    saved_wait = copilot_loop.FLYOUT_DISMISS_WAIT
    copilot_loop.UIA = _patched_uia()
    copilot_loop.FLYOUT_DISMISS_WAIT = 0.0
    try:
        log = []
        holder = {"tops": []}
        close = _OrderButton(name="閉じる", log=log, tag="close")
        close.on_press = lambda: holder.update(tops=[])
        panel = _flyout_panel(FLYOUT_PID, close=close)
        holder["tops"] = [panel]
        card = _card("#!phal python\n# 1-40: フライアウトを閉じる\nimport x")
        options = _OrderButton(name="その他のオプション",
                               log=log, tag="options")
        item = _OrderButton(name="ダウンロード", log=log, tag="item",
                            control_type=copilot_loop.CONTROL_MENU_ITEM)
        cp = _flyout_copilot([card, options], [card, options, item],
                             options, holder)
        assert cp.press_download_candidate() is True
        assert log == ["close", "options", "item"], log
        assert cp._flyout_dismissal == "closed"
    finally:
        copilot_loop.UIA = saved_uia
        copilot_loop.FLYOUT_DISMISS_WAIT = saved_wait


def test_press_download_collapses_flyout_without_close_button():
    """「閉じる」が無いパネルは、畳む仕組み (ExpandCollapse の
    Collapse) で閉じる (1-40)。"""
    saved_uia = copilot_loop.UIA
    saved_wait = copilot_loop.FLYOUT_DISMISS_WAIT
    copilot_loop.UIA = _patched_uia()
    copilot_loop.FLYOUT_DISMISS_WAIT = 0.0
    try:
        panel = _flyout_panel(
            FLYOUT_PID,
            patterns={copilot_loop.EXPAND_COLLAPSE_PATTERN})
        holder = {"tops": [panel]}
        card = _card("#!phal python\n# 1-40: 畳んで閉じる\nimport x")
        options = FakeButtonElement(name="その他のオプション")
        item = _menu_item()
        cp = _flyout_copilot([card, options], [card, options, item],
                             options, holder)
        assert cp.press_download_candidate() is True
        assert panel.pressed_via == ["Collapse"]
        assert options.invoked and item.invoked
    finally:
        copilot_loop.UIA = saved_uia
        copilot_loop.FLYOUT_DISMISS_WAIT = saved_wait


def test_press_download_no_flyout_no_dismissal():
    """フライアウトが無ければ、閉じる操作は一切せず素通りする
    (1-40)。同じプロセスの無関係な窓が居ても触らない。"""
    saved_uia = copilot_loop.UIA
    saved_wait = copilot_loop.FLYOUT_DISMISS_WAIT
    copilot_loop.UIA = _patched_uia()
    copilot_loop.FLYOUT_DISMISS_WAIT = 0.0
    try:
        other = FakeTreeElement(
            name="別の窓", pid=FLYOUT_PID,
            control_type=copilot_loop.CONTROL_WINDOW)
        holder = {"tops": [other]}
        card = _card("#!phal python\n# 1-40: フライアウト無し\nimport x")
        options = FakeButtonElement(name="その他のオプション")
        item = _menu_item()
        cp = _flyout_copilot([card, options], [card, options, item],
                             options, holder)
        assert cp.press_download_candidate() is True
        assert cp._flyout_dismissal == "absent"
        assert options.invoked and item.invoked
    finally:
        copilot_loop.UIA = saved_uia
        copilot_loop.FLYOUT_DISMISS_WAIT = saved_wait


def test_press_download_flyout_other_process_not_dismissed():
    """形がそっくりでも、持ち主のプロセスが違うパネルは
    閉じに行かない (1-40、安全側)。"""
    saved_uia = copilot_loop.UIA
    saved_wait = copilot_loop.FLYOUT_DISMISS_WAIT
    copilot_loop.UIA = _patched_uia()
    copilot_loop.FLYOUT_DISMISS_WAIT = 0.0
    try:
        close = FakeButtonElement(name="閉じる")
        panel = _flyout_panel(FLYOUT_PID + 999, close=close)
        holder = {"tops": [panel]}
        card = _card("#!phal python\n# 1-40: 別プロセスのパネル\nimport x")
        options = FakeButtonElement(name="その他のオプション")
        item = _menu_item()
        cp = _flyout_copilot([card, options], [card, options, item],
                             options, holder)
        assert cp.press_download_candidate() is True
        assert not close.invoked  # 余所のパネルは触らない
        assert cp._flyout_dismissal == "absent"
        assert options.invoked and item.invoked
    finally:
        copilot_loop.UIA = saved_uia
        copilot_loop.FLYOUT_DISMISS_WAIT = saved_wait


class _FakeUser32:
    """PostMessageW の呼び出しを記録する user32 の偽物 (1-67)。

    mode: "ok" (True を返す) / "false" (False を返す) /
    "raise" (例外を投げる)。"""

    def __init__(self, mode="ok"):
        self._mode = mode
        self.calls = []

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        # 呼び側は c_void_p で渡すので、記録は素の値に直す。
        self.calls.append(
            (getattr(hwnd, "value", hwnd), msg, wparam, lparam))
        if self._mode == "raise":
            raise OSError("投げられませんでした")
        return self._mode == "ok"


def test_post_escape_posts_both_messages_in_order():
    """ESC は WM_KEYDOWN → WM_KEYUP の順に、VK_ESCAPE で、
    宛先の HWND へ投げる (1-67)。"""
    saved = copilot_loop.user32
    fake = _FakeUser32()
    copilot_loop.user32 = fake
    try:
        assert copilot_loop._post_escape(0x21234) == "sent"
        assert fake.calls == [
            (0x21234, copilot_loop.WM_KEYDOWN, copilot_loop.VK_ESCAPE,
             0),
            (0x21234, copilot_loop.WM_KEYUP, copilot_loop.VK_ESCAPE,
             0),
        ], fake.calls
        # wParam が VK_ESCAPE (0x1B) であることも確かめる
        assert copilot_loop.VK_ESCAPE == 0x1B
    finally:
        copilot_loop.user32 = saved


def test_post_escape_no_hwnd_makes_no_calls():
    """宛先が無ければ何も投げず "no-hwnd" (1-67)。"""
    saved = copilot_loop.user32
    fake = _FakeUser32()
    copilot_loop.user32 = fake
    try:
        assert copilot_loop._post_escape(None) == "no-hwnd"
        assert copilot_loop._post_escape(0) == "no-hwnd"
        assert fake.calls == []
    finally:
        copilot_loop.user32 = saved


def test_post_escape_failure_is_swallowed():
    """PostMessage の失敗 (False / 例外) は "failed" に畳まれ、
    例外は呼び側へ出ない (1-67)。"""
    saved = copilot_loop.user32
    try:
        copilot_loop.user32 = _FakeUser32(mode="false")
        assert copilot_loop._post_escape(0x1234) == "failed"
        copilot_loop.user32 = _FakeUser32(mode="raise")
        assert copilot_loop._post_escape(0x1234) == "failed"
    finally:
        copilot_loop.user32 = saved


def test_send_escape_uses_main_hwnd():
    """Copilot.send_escape は自分の窓 (hwnd_main) へ投げる。
    hwnd が無ければ "no-hwnd" (1-67)。"""
    saved = copilot_loop.user32
    fake = _FakeUser32()
    copilot_loop.user32 = fake
    try:
        cp = copilot_loop.Copilot.__new__(copilot_loop.Copilot)
        cp.hwnd_main = 0x777
        assert cp.send_escape() == "sent"
        assert [call[0] for call in fake.calls] == [0x777, 0x777]
        cp2 = copilot_loop.Copilot.__new__(copilot_loop.Copilot)
        assert cp2.send_escape() == "no-hwnd"  # hwnd_main 自体が無い
    finally:
        copilot_loop.user32 = saved


def test_press_download_sends_escape_before_dismissal():
    """押下の流れでは、ESC が UIA の解散より先に投げられ、
    結果が控え (_esc_before_press) に残る (1-67)。"""
    saved_uia = copilot_loop.UIA
    saved_wait = copilot_loop.FLYOUT_DISMISS_WAIT
    copilot_loop.UIA = _patched_uia()
    copilot_loop.FLYOUT_DISMISS_WAIT = 0.0
    try:
        order = []
        holder = {"tops": []}
        card = _card("#!phal python\n# 1-67: ESCが先\nimport x")
        options = FakeButtonElement(name="その他のオプション")
        item = _menu_item()
        cp = _flyout_copilot([card, options], [card, options, item],
                             options, holder)
        cp.send_escape = lambda: order.append("esc") or "sent"
        original_dismiss = cp._dismiss_downloads_flyout

        def dismiss():
            order.append("dismiss")
            return original_dismiss()

        cp._dismiss_downloads_flyout = dismiss
        assert cp.press_download_candidate() is True
        assert order == ["esc", "dismiss"], order
        assert cp._esc_before_press == "sent"
    finally:
        copilot_loop.UIA = saved_uia
        copilot_loop.FLYOUT_DISMISS_WAIT = saved_wait


# ---------------------------------------------------------------------------
# 添付 (1.60 で貼り付け方式、1.61 で方式の切り替えとダイアログ復活)
# ---------------------------------------------------------------------------
class _FocusableElement(FakeElement):
    """SetFocus を共有ログへ残す入力欄の偽物。"""

    def __init__(self, *, log, **kwargs):
        super().__init__(**kwargs)
        self._log = log

    def SetFocus(self):
        self._log.append("focus")


def _paste_state(*, clip_text="元のテキスト", paste_plan=()):
    """貼り付け方式の部品一式 (状態は辞書で共有する)。

    paste_plan は Ctrl+V のたびにチップが付くか (True) 付か
    ないか (False) の並び。呼び出しが並びを超えたら最後の
    要素を繰り返す。chip_style は確認の印の出方: "name" は
    ファイル名を含む要素、"remove" は「添付ファイルを削除」。"""
    return {"log": [], "attached_names": [], "remove_count": 0,
            "clip_text": clip_text, "paste_plan": list(paste_plan),
            "paste_calls": 0, "chip_style": "name",
            "pending_name": ""}


def _paste_copilot(state):
    """入力欄とチップを fake の木で真似た Copilot。

    子孫は state を見て組み直す (貼った後にチップが増える
    実機の動き)。"""
    cp = copilot_loop.Copilot.__new__(copilot_loop.Copilot)
    cp.profile = {"name": "phal-ai", "input_name": "メッセージ",
                  "input_class": STORED_CLASS}
    cp.hwnd_main = 424242
    box = _FocusableElement(
        name="メッセージ", class_name=STORED_CLASS, log=state["log"])

    def descendants(heal=True):
        elements = [box]
        if state["chip_style"] == "remove":
            for _ in range(state["remove_count"]):
                elements.append(
                    FakeButtonElement(name="添付ファイルを削除"))
        else:
            for name in state["attached_names"]:
                elements.append(FakeElement(
                    name=name,
                    control_type=copilot_loop.CONTROL_BUTTON,
                    has_value=False))
        return None, FakeDesc(elements)

    cp._descendants = descendants
    return cp


class _PasteRig:
    """貼り付けの境界 (モジュール関数と待ち時間) の差し替え。

    クリップボード・前面化・キー送信は実機でしか動かない
    ので、モジュール関数ごと差し替えて順序と成否を握る。"""

    def __init__(self, state, *, write_file_error=None,
                 confirm_timeout=None):
        self._state = state
        self._write_file_error = write_file_error
        self._confirm_timeout = confirm_timeout
        self._saved = {}

    def _patch(self, name, value):
        self._saved[name] = getattr(copilot_loop, name)
        setattr(copilot_loop, name, value)

    def __enter__(self):
        state = self._state

        def write_file(path):
            state["log"].append(("file", str(path)))
            state["pending_name"] = copilot_loop._file_name_of(path)
            if self._write_file_error is not None:
                raise self._write_file_error

        def send_ctrl_v():
            state["log"].append("paste")
            plan = state["paste_plan"]
            ok = plan[min(state["paste_calls"], len(plan) - 1)] \
                if plan else False
            state["paste_calls"] += 1
            if ok:
                if state["chip_style"] == "remove":
                    state["remove_count"] += 1
                else:
                    state["attached_names"].append(
                        state["pending_name"])

        self._patch("_clipboard_read_text", lambda: state["clip_text"])
        self._patch("_clipboard_write_file", write_file)
        self._patch("_clipboard_write_text",
                    lambda text: state["log"].append(("restore", text)))
        self._patch("_bring_to_front",
                    lambda hwnd: state["log"].append(("front", hwnd))
                    or True)
        self._patch("_send_ctrl_v", send_ctrl_v)
        self._patch("ATTACH_FOREGROUND_WAIT", 0.0)
        self._patch("ATTACH_FOCUS_WAIT", 0.0)
        if self._confirm_timeout is not None:
            self._patch("ATTACH_CONFIRM_TIMEOUT", self._confirm_timeout)
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(copilot_loop, name, value)
        return False


def test_attach_file_paste_happy_path():
    """控える → ファイルを置く → 前面化 → フォーカス → 貼る
    → チップで確認 → テキストを戻す、の順で進む。"""
    state = _paste_state(clip_text="元のテキスト", paste_plan=[True])
    cp = _paste_copilot(state)
    with _PasteRig(state):
        cp.attach_file_paste("C:\\out\\result.csv")
    assert state["log"] == [
        ("file", "C:\\out\\result.csv"), ("front", 424242), "focus",
        "paste", ("restore", "元のテキスト")]
    assert state["attached_names"] == ["result.csv"]


def test_attach_file_confirmed_by_remove_button():
    """チップにファイル名が出なくても、「添付ファイルを削除」
    の増加で確認できる (印は2つの併用)。"""
    state = _paste_state(paste_plan=[True])
    state["chip_style"] = "remove"
    cp = _paste_copilot(state)
    with _PasteRig(state):
        cp.attach_file_paste("C:\\out\\a.txt")
    assert state["remove_count"] == 1


def test_attach_file_without_input_box_is_not_found():
    """入力欄が無ければ not_found。クリップボードには触らない。"""
    state = _paste_state()
    cp = _copilot(
        {"name": "phal-ai", "input_name": "メッセージ",
         "input_class": STORED_CLASS},
        [FakeElement(name="無関係", class_name="x")])
    with _PasteRig(state):
        try:
            cp.attach_file_paste("C:\\out\\a.txt")
        except copilot_loop.InputStageError as e:
            assert e.stage == "not_found"
        else:
            raise AssertionError("InputStageError になるはず")
    assert state["log"] == []


def test_attach_file_unconfirmed_is_unwritable():
    """貼ってもチップが出なければ失敗にする。失敗しても、
    控えたテキストはクリップボードへ戻される。"""
    state = _paste_state(clip_text="元のテキスト", paste_plan=[False])
    cp = _paste_copilot(state)
    with _PasteRig(state, confirm_timeout=0.0):
        try:
            cp.attach_file_paste("C:\\out\\a.txt")
        except copilot_loop.InputStageError as e:
            assert e.stage == "unwritable"
            assert "確かめられませんでした" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")
    assert ("restore", "元のテキスト") in state["log"]


def test_attach_file_clipboard_failure_is_unwritable():
    """クリップボードへ置けなければ unwritable。貼り付けには
    進まない。"""
    state = _paste_state()
    cp = _paste_copilot(state)
    with _PasteRig(state,
                   write_file_error=RuntimeError("置けません")):
        try:
            cp.attach_file_paste("C:\\out\\a.txt")
        except copilot_loop.InputStageError as e:
            assert e.stage == "unwritable"
            assert "クリップボード" in str(e)
        else:
            raise AssertionError("InputStageError になるはず")
    assert "paste" not in state["log"]


def test_attach_files_continues_after_failure():
    """1件目が付かなくても2件目は添付される。戻り値は失敗
    したファイル名 (名前だけ)。"""
    state = _paste_state(paste_plan=[False, True])
    cp = _paste_copilot(state)
    with _PasteRig(state, confirm_timeout=0.0):
        failed = cp.attach_files(
            ["C:\\out\\first.txt", "C:\\out\\second.txt"])
    assert failed == ["first.txt"]
    assert state["attached_names"] == ["second.txt"]


def test_build_dropfiles_data_layout():
    """DROPFILES の頭20バイト + UTF-16LE のパス + NUL 2つ。

    pFiles (先頭4バイト) は頭の大きさ 20、fWide (最後の
    4バイト) は 1。"""
    data = copilot_loop.build_dropfiles_data("C:\\out\\a.txt")
    header, rest = data[:20], data[20:]
    assert len(header) == 20
    assert struct.unpack("<I", header[0:4])[0] == 20
    assert struct.unpack("<I", header[16:20])[0] == 1
    assert rest == "C:\\out\\a.txt".encode("utf-16-le") \
        + b"\x00\x00\x00\x00"


def test_attach_appeared_by_remove_count():
    """「添付ファイルを削除」の増加だけでも付いたとみなす。"""
    assert copilot_loop.attach_appeared([], [], 0, 1, "a.txt")


def test_attach_appeared_by_new_chip_name():
    """ファイル名を含む要素が新たに出れば付いたとみなす。"""
    assert copilot_loop.attach_appeared(
        ["会話"], ["会話", "a.txt"], 0, 0, "a.txt")


def test_attach_appeared_existing_name_is_not_new():
    """同じファイル名を含む要素が元から同じ数だけあれば、
    貼る前からある分なので付いたとはみなさない。"""
    assert not copilot_loop.attach_appeared(
        ["a.txt の話"], ["a.txt の話"], 0, 0, "a.txt")


def test_attach_appeared_name_count_increase():
    """同じ名前でも、数が増えれば付いたとみなす。"""
    assert copilot_loop.attach_appeared(
        ["a.txt"], ["a.txt", "a.txt"], 0, 0, "a.txt")


def test_attach_appeared_nothing_changed():
    """何も変わらなければ付いていない。"""
    assert not copilot_loop.attach_appeared(
        ["会話"], ["会話"], 1, 1, "a.txt")
    assert not copilot_loop.attach_appeared([], [], 0, 0, "")


# ---------------------------------------------------------------------------
# 添付 (1.61、方式の振り分けとダイアログ方式)
# ---------------------------------------------------------------------------
def _dispatch_copilot(method, *, paste_error=None, dialog_error=None):
    """方式の2つを差し替えた Copilot と、呼ばれた順序の控え。

    attach_files の振り分けだけを見る (実体の方式は別の
    テストで確かめる)。"""
    cp = copilot_loop.Copilot.__new__(copilot_loop.Copilot)
    cp.profile = {"name": "phal-ai"}
    if method is not None:
        cp.profile["attach_method"] = method
    calls = []

    def paste(_path):
        calls.append("paste")
        if paste_error is not None:
            raise paste_error

    def dialog(_path):
        calls.append("dialog")
        if dialog_error is not None:
            raise dialog_error

    cp.attach_file_paste = paste
    cp.attach_file_dialog = dialog
    return cp, calls


def test_attach_auto_falls_back_to_dialog():
    """auto: 貼り付けが失敗したファイルだけ、ダイアログでも試す。"""
    cp, calls = _dispatch_copilot(
        "auto",
        paste_error=copilot_loop.InputStageError("unwritable", "貼れず"))
    failed = cp.attach_files(["C:\\out\\a.txt"])
    assert failed == []
    assert calls == ["paste", "dialog"]


def test_attach_auto_both_fail_reports_name():
    """auto: 両方失敗したら、失敗したファイル名が残る。"""
    cp, calls = _dispatch_copilot(
        "auto",
        paste_error=copilot_loop.InputStageError("unwritable", "貼れず"),
        dialog_error=copilot_loop.InputStageError("not_found", "出ず"))
    failed = cp.attach_files(["C:\\out\\a.txt"])
    assert failed == ["a.txt"]
    assert calls == ["paste", "dialog"]


def test_attach_paste_method_never_tries_dialog():
    """paste 指定: 失敗してもダイアログは試さない。"""
    cp, calls = _dispatch_copilot(
        "paste",
        paste_error=copilot_loop.InputStageError("unwritable", "貼れず"))
    failed = cp.attach_files(["C:\\out\\a.txt"])
    assert failed == ["a.txt"]
    assert calls == ["paste"]


def test_attach_dialog_method_never_tries_paste():
    """dialog 指定: 貼り付けは試さない。"""
    cp, calls = _dispatch_copilot("dialog")
    failed = cp.attach_files(["C:\\out\\a.txt"])
    assert failed == []
    assert calls == ["dialog"]


def test_attach_method_of_defaults_to_auto():
    """方式の値が欠け・不正なら auto に倒す。"""
    assert copilot_loop.attach_method_of(None) == "auto"
    assert copilot_loop.attach_method_of({}) == "auto"
    assert copilot_loop.attach_method_of(
        {"attach_method": "bogus"}) == "auto"
    assert copilot_loop.attach_method_of(
        {"attach_method": "dialog"}) == "dialog"
    assert copilot_loop.attach_method_of(
        {"attach_method": "paste"}) == "paste"


def test_attach_test_file_reports_method_and_removes_chip():
    """添付テスト: 成功したら方式を文に入れ、チップを外す。"""
    state = _paste_state(paste_plan=[True])
    state["chip_style"] = "remove"
    cp = _paste_copilot(state)
    cp.profile["attach_method"] = "paste"

    def activate(el):
        if (el.CurrentName or "") == "添付ファイルを削除":
            state["remove_count"] -= 1
        return "Invoke"

    cp._activate_element = activate
    with _PasteRig(state):
        ok, message = cp.attach_test_file("C:\\out\\a.txt")
    assert ok
    assert "貼り付け" in message
    assert "チップも外しました" in message
    assert state["remove_count"] == 0


def test_attach_test_file_failure_reports_stage():
    """添付テスト: 失敗したら、段つきの文がそのまま返る。"""
    state = _paste_state(paste_plan=[False])
    cp = _paste_copilot(state)
    cp.profile["attach_method"] = "paste"
    with _PasteRig(state, confirm_timeout=0.0):
        ok, message = cp.attach_test_file("C:\\out\\a.txt")
    assert not ok
    assert "unwritable" in message


def _dialog_candidate(hwnd, title="開く", pid=999, visible=True,
                      cls="#32770"):
    return {"hwnd": hwnd, "class": cls, "title": title,
            "pid": pid, "visible": visible}


def test_pick_file_dialog_prefers_open_title():
    """タイトル「開く」を優先する。持ち主のプロセスは見ない
    (実機では対象の窓と違う)。"""
    candidates = [
        _dialog_candidate(1, title="名前を付けて保存", pid=79808),
        _dialog_candidate(2, title="開く", pid=59344),
    ]
    assert copilot_loop.pick_file_dialog(candidates) == 2


def test_pick_file_dialog_filters_invisible_and_other_classes():
    """見えていないものと、クラスが違うものは候補にしない。"""
    candidates = [
        _dialog_candidate(1, visible=False),
        _dialog_candidate(2, cls="Other"),
        _dialog_candidate(3),
    ]
    assert copilot_loop.pick_file_dialog(candidates) == 3
    assert copilot_loop.pick_file_dialog([]) is None
    assert copilot_loop.pick_file_dialog(
        [_dialog_candidate(1, visible=False)]) is None


def test_pick_file_dialog_readable_comes_first():
    """部品が読める候補が分かれば、そちらを先に選ぶ (古い
    ダイアログの取り違え防止)。"""
    candidates = [_dialog_candidate(1), _dialog_candidate(2)]
    assert copilot_loop.pick_file_dialog(candidates) == 1
    assert copilot_loop.pick_file_dialog(
        candidates, readable_hwnds={2}) == 2
    assert copilot_loop.order_file_dialogs(
        candidates, readable_hwnds={2}) == [2, 1]


def test_dialog_button_label_strips_accelerator():
    """「開く(&O)」→「開く」。加速キーの表示を落として比べる。"""
    assert copilot_loop._dialog_button_label("開く(&O)") == "開く"
    assert copilot_loop._dialog_button_label("開く (&O)") == "開く"
    assert copilot_loop._dialog_button_label("キャンセル") == "キャンセル"
    assert copilot_loop._dialog_button_label("") == ""


def test_pick_lowest_edit():
    """一番下 (top が最大) を選ぶ。同点は先勝ち、空は None。"""
    assert copilot_loop.pick_lowest_edit([100, 300, 200]) == 1
    assert copilot_loop.pick_lowest_edit([300, 300]) == 0
    assert copilot_loop.pick_lowest_edit([]) is None
    assert copilot_loop.pick_lowest_edit([None, 50]) == 1


def test_attach_dialog_wait_is_ten_seconds():
    """ダイアログの出現待ちは10秒 (陽太さんの指定、2026-11-15)。"""
    assert copilot_loop.ATTACH_DIALOG_WAIT == 10.0


class _DialogEditElement(FakeElement):
    """ファイル名の Edit の偽物。矩形と画面外フラグを持つ。"""

    def __init__(self, *, top, offscreen=False, **kwargs):
        kwargs.setdefault(
            "control_type", copilot_loop.CONTROL_EDIT)
        super().__init__(**kwargs)
        self.CurrentIsOffscreen = offscreen
        self.CurrentBoundingRectangle = types.SimpleNamespace(
            left=10, top=top, right=310, bottom=top + 20)


class _FakeDialogElement:
    """「開く」ダイアログの偽物。FindAll で中身を返す。"""

    def __init__(self, parts, hwnd=777):
        self._parts = parts
        self.CurrentNativeWindowHandle = hwnd

    def FindAll(self, _scope, _cond):
        return FakeDesc(self._parts)


class _ChipOnInvokePattern:
    """「開く」を押したらチップが付く、という実機の動き。"""

    def __init__(self, element, state, chip_name):
        self._element = element
        self._state = state
        self._chip_name = chip_name

    def QueryInterface(self, _interface):
        return self

    def Invoke(self):
        self._element.invoked = True
        self._state["open_invokes"] = \
            self._state.get("open_invokes", 0) + 1
        self._state["attached_names"].append(self._chip_name)


class _OpenButtonElement(FakeButtonElement):
    """「開く」ボタンの偽物。押されたらチップが付く。"""

    def __init__(self, state, chip_name, **kwargs):
        super().__init__(**kwargs)
        self._state = state
        self._chip_name = chip_name

    def GetCurrentPattern(self, pattern_id):
        if pattern_id == copilot_loop.INVOKE_PATTERN:
            return _ChipOnInvokePattern(
                self, self._state, self._chip_name)
        return None


class _DialogRig:
    """ダイアログ方式の境界 (Win32 列挙・UIA・待ち時間・キー
    送信) の差し替え。

    alt_o は、Alt+O が送られたときの実機の動きの真似 (任意の
    関数)。rig.candidates を書き換えれば「ダイアログが閉じ
    た」ことを表せる。"""

    def __init__(self, candidates, alt_o=None):
        self.candidates = list(candidates)
        self._alt_o = alt_o
        self.alt_o_calls = 0
        self.front_hwnds = []
        self._saved = {}

    def _patch(self, name, value):
        self._saved[name] = getattr(copilot_loop, name)
        setattr(copilot_loop, name, value)

    def __enter__(self):
        self._patch("_enum_top_level_windows",
                    lambda: list(self.candidates))
        self._patch("ATTACH_POLL", 0.0)
        self._patch("DIALOG_SUBMIT_WAIT", 0.0)
        self._patch("DIALOG_CLOSE_WAIT", 0.0)

        def send_alt_o():
            self.alt_o_calls += 1
            if self._alt_o is not None:
                self._alt_o(self)

        self._patch("_send_alt_o", send_alt_o)
        self._patch("_bring_to_front",
                    lambda hwnd: self.front_hwnds.append(hwnd)
                    or True)
        self._patch("UIA", types.SimpleNamespace(
            IUIAutomationInvokePattern=object(),
            IUIAutomationValuePattern=object(),
            TreeScope_Descendants=3))
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(copilot_loop, name, value)
        return False


def _dialog_copilot(state):
    """添付ボタンとチップを fake の木で真似た Copilot。"""
    cp = copilot_loop.Copilot.__new__(copilot_loop.Copilot)
    cp.profile = {"name": "phal-ai", "attach_method": "dialog"}
    cp.hwnd_main = 424242
    state["attach_button"] = FakeButtonElement(name="ファイルを添付")

    def descendants(heal=True):
        elements = [state["attach_button"]]
        for name in state["attached_names"]:
            elements.append(FakeElement(
                name=name,
                control_type=copilot_loop.CONTROL_BUTTON,
                has_value=False))
        return None, FakeDesc(elements)

    cp._descendants = descendants
    return cp


def test_dialog_submit_wait_is_two_seconds():
    """決定までの待ちは2秒、閉じるのを見る上限は3秒
    (陽太さんの設計、2026-11-15)。"""
    assert copilot_loop.DIALOG_SUBMIT_WAIT == 2.0
    assert copilot_loop.DIALOG_CLOSE_WAIT == 3.0


def _dialog_parts(state, chip_name="result.csv", edit=None):
    """ダイアログの中身 (欄・開く・キャンセル) の組を返す。"""
    if edit is None:
        edit = _DialogEditElement(top=500)
    open_button = _OpenButtonElement(state, chip_name,
                                     name="開く(&O)")
    cancel_button = FakeButtonElement(name="キャンセル")
    return edit, open_button, cancel_button


def _close_on_alt_o(state, chip_name="result.csv"):
    """Alt+O でダイアログが閉じ、チップが付く実機の動き。"""
    def alt_o(rig):
        rig.candidates.clear()
        state["attached_names"].append(chip_name)
    return alt_o


def test_attach_file_dialog_happy_path():
    """ボタンを押す → ダイアログを捕捉 → 見えている一番下の
    Edit へ書き、読み戻す → Alt+O で決定 → チップで確認する。

    見えていない Edit には書かない (実機の観察どおり)。"""
    state = {"attached_names": []}
    cp = _dialog_copilot(state)
    hidden_edit = _DialogEditElement(top=100, offscreen=True)
    visible_edit, open_button, cancel_button = _dialog_parts(state)
    dialog = _FakeDialogElement(
        [hidden_edit, visible_edit, open_button, cancel_button])
    cp.uia = types.SimpleNamespace(
        ElementFromHandle=lambda _hwnd: dialog)
    cp.true_cond = object()
    with _DialogRig([_dialog_candidate(777)],
                    alt_o=_close_on_alt_o(state)):
        cp.attach_file_dialog("C:\\out\\result.csv")
    assert state["attach_button"].invoked
    assert visible_edit.value == "C:\\out\\result.csv"
    assert hidden_edit.value == ""
    assert state["attached_names"] == ["result.csv"]


class _RevertingDialogEditElement(_DialogEditElement):
    """書いた値が、最初の読み戻しだけ通って、その後は消える
    Edit の偽物。

    ダイアログ自身の初期化で、書いたばかりの欄が戻ってしまう
    実機の動きの真似。2回目の書き込み以降は安定する。"""

    def __init__(self, **kwargs):
        self._stored = ""
        self._writes = 0
        self._reads = 0
        super().__init__(**kwargs)
        self._writes = 0
        self._reads = 0

    @property
    def value(self):
        self._reads += 1
        if self._writes == 1 and self._reads > 1:
            return ""
        return self._stored

    @value.setter
    def value(self, text):
        self._stored = text
        self._writes += 1
        self._reads = 0


def test_attach_file_dialog_rewrites_reverted_field():
    """欄の値が書いた後に戻っていたら、決定の前に書き直す。"""
    state = {"attached_names": []}
    cp = _dialog_copilot(state)
    edit, open_button, cancel_button = _dialog_parts(
        state, edit=_RevertingDialogEditElement(top=500))
    dialog = _FakeDialogElement([edit, open_button, cancel_button])
    cp.uia = types.SimpleNamespace(
        ElementFromHandle=lambda _hwnd: dialog)
    cp.true_cond = object()
    with _DialogRig([_dialog_candidate(777)],
                    alt_o=_close_on_alt_o(state)):
        cp.attach_file_dialog("C:\\out\\result.csv")
    assert edit._writes == 2
    assert edit.value == "C:\\out\\result.csv"
    assert state["attached_names"] == ["result.csv"]


def test_attach_file_dialog_alt_o_without_invoke():
    """Alt+O でダイアログが閉じれば、「開く」の Invoke には
    フォールバックしない。前面化はダイアログの hwnd へ。"""
    state = {"attached_names": []}
    cp = _dialog_copilot(state)
    edit, open_button, cancel_button = _dialog_parts(state)
    dialog = _FakeDialogElement([edit, open_button, cancel_button])
    cp.uia = types.SimpleNamespace(
        ElementFromHandle=lambda _hwnd: dialog)
    cp.true_cond = object()
    with _DialogRig([_dialog_candidate(777)],
                    alt_o=_close_on_alt_o(state)) as rig:
        cp.attach_file_dialog("C:\\out\\result.csv")
    assert rig.alt_o_calls == 1
    assert rig.front_hwnds == [777]
    assert not open_button.invoked
    assert state.get("open_invokes", 0) == 0
    assert state["attached_names"] == ["result.csv"]


def test_attach_file_dialog_invoke_fallback_once():
    """Alt+O の後もダイアログが残れば、「開く」の Invoke へ
    1回だけフォールバックする。"""
    state = {"attached_names": []}
    cp = _dialog_copilot(state)
    edit, open_button, cancel_button = _dialog_parts(state)
    dialog = _FakeDialogElement([edit, open_button, cancel_button])
    cp.uia = types.SimpleNamespace(
        ElementFromHandle=lambda _hwnd: dialog)
    cp.true_cond = object()
    # Alt+O が効かず、ダイアログは開いたまま (候補は残る)。
    with _DialogRig([_dialog_candidate(777)]) as rig:
        cp.attach_file_dialog("C:\\out\\result.csv")
    assert rig.alt_o_calls == 1
    assert state.get("open_invokes", 0) == 1
    assert open_button.invoked
    assert state["attached_names"] == ["result.csv"]


def test_attach_file_dialog_timeout_is_not_found():
    """ダイアログが出なければ not_found (待ちの上限まで待つ)。"""
    state = {"attached_names": []}
    cp = _dialog_copilot(state)
    saved = copilot_loop.ATTACH_DIALOG_WAIT
    copilot_loop.ATTACH_DIALOG_WAIT = 0.0
    try:
        with _DialogRig([]):
            try:
                cp.attach_file_dialog("C:\\out\\a.txt")
            except copilot_loop.InputStageError as e:
                assert e.stage == "not_found"
            else:
                raise AssertionError("InputStageError になるはず")
    finally:
        copilot_loop.ATTACH_DIALOG_WAIT = saved


def test_attach_file_dialog_without_button_is_not_found():
    """添付ボタンが無ければ not_found。ダイアログは待たない。"""
    cp = _copilot(
        {"name": "phal-ai", "attach_method": "dialog"},
        [FakeElement(name="無関係", class_name="x")])
    with _DialogRig([_dialog_candidate(777)]):
        try:
            cp.attach_file_dialog("C:\\out\\a.txt")
        except copilot_loop.InputStageError as e:
            assert e.stage == "not_found"
        else:
            raise AssertionError("InputStageError になるはず")


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
