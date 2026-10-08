# tests/test_teams_meeting.py
# teams_meeting.py の手順エンジンを、偽の要素ツリーで確かめる。実物の Teams には触らない。
#
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe tests\test_teams_meeting.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_bg_remove.py と同じ形。pytest があればそのまま拾える)。
# 時計も偽物にしてあるので、30秒のタイムアウトも一瞬で通る。
# 会議名の書式で snippets(PySide6)を読むので offscreen にしておく(窓は出さない)。
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import teams_meeting as tm  # noqa: E402


# ---------------------------------------------------------------
# 偽の時計と偽の Teams
# ---------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))


class El:
    """偽の UIA 要素。patterns は持っているパターン(invoke / expand / toggle / value)。"""

    def __init__(self, name="", aid="", ctype="Button", patterns=("invoke",),
                 children=None, on_press=None, value=None):
        self.name, self.aid, self.ctype = name, aid, ctype
        self.patterns = set(patterns)
        self.children = list(children or [])
        self.on_press = on_press
        self.value = value

    def walk(self):
        for child in self.children:
            yield child
            yield from child.walk()


class Win:
    def __init__(self, hwnd, title, children, appear_at=0.0):
        self.hwnd, self.title = hwnd, title
        self.root = El("root", ctype="Window", children=children)
        self.appear_at = appear_at


class FakeBackend:
    """UiaBackend と同じ口を持つ偽物。探し方の規則(aid → name → name_prefix、
    within は中だけ)も本物に合わせてある。"""

    def __init__(self, clock):
        self.clock = clock
        self.wins = []
        self.log = []
        self.launched = 0
        self.on_launch = None
        self.set_hook = None

    def windows(self):
        return [tm.TeamsWindow(w.hwnd, w.title) for w in self.wins
                if w.appear_at <= self.clock.now()]

    def launch(self):
        self.launched += 1
        if self.on_launch:
            self.on_launch()

    def _win(self, hwnd):
        return next((w for w in self.wins if w.hwnd == hwnd
                     and w.appear_at <= self.clock.now()), None)

    def find(self, hwnd, spec):
        win = self._win(hwnd)
        if win is None or not spec:
            return None
        return self._find_in(win.root, spec)

    def _find_in(self, base, spec):
        within = spec.get("within")
        if within:
            base = self._find_in(base, within)
            if base is None:
                return None
        types = tm._as_list(spec.get("control_type"))

        def ok(el):
            return not types or el.ctype in types or (el.ctype == "ComboBox" and "Combo" in types)

        for value in tm._as_list(spec.get("automation_id")):
            for el in base.walk():
                if el.aid == value and ok(el):
                    return el
        for value in tm._as_list(spec.get("name")):
            for el in base.walk():
                if el.name == value and ok(el):
                    return el
        for prefix in tm._as_list(spec.get("name_prefix")):
            for el in base.walk():
                if el.name.startswith(prefix) and ok(el):
                    return el
        return None

    def press(self, el, prefer="invoke"):
        order = ("expand", "invoke", "toggle") if prefer == "expand" else ("invoke", "toggle", "expand")
        for kind in order:
            if kind in el.patterns:
                self.log.append(("press", el.name or el.aid, kind))
                if el.on_press:
                    el.on_press()
                return kind
        raise RuntimeError("押せません")

    def collapse(self, el):
        self.log.append(("collapse", el.name or el.aid))

    def set_value(self, el, text):
        el.value = text
        if self.set_hook:
            self.set_hook(el)
        self.log.append(("set", el.aid, text))

    def get_value(self, el):
        return el.value

    def name_of(self, el):
        return el.name

    def pressed(self):
        return [entry[1] for entry in self.log if entry[0] == "press"]


def build_teams(backend, business=True, camera_on=True, mic_muted=True,
                prejoin_opens=True, invite=True):
    """このPCで実測した画面遷移を真似る。business=True なら業務版の文字起こしも生やす。"""
    state = {"transcribing": False, "joined": False}
    main = Win(1, "チャット | Microsoft Teams", [])
    meeting = Win(2, "", [], appear_at=10 ** 9)   # 会議を開始するまでは居ない

    def camera_button():
        el = El("カメラをオフにします(Ctrl+Shift+O)" if camera_on else
                "カメラをオンにします(Ctrl+Shift+O)")

        def toggle():
            el.name = (el.name.replace("オフ", "オン") if "オフ" in el.name
                       else el.name.replace("オン", "オフ"))
        el.on_press = toggle
        return el

    def mic_button():
        el = El("マイクのミュートを解除(Ctrl+Shift+M)" if mic_muted else
                "マイクをミュート(Ctrl+Shift+M)")

        def toggle():
            el.name = ("マイクをミュート(Ctrl+Shift+M)" if "解除" in el.name
                       else "マイクのミュートを解除(Ctrl+Shift+M)")
        el.on_press = toggle
        return el

    def join():
        state["joined"] = True
        meeting.title = meeting.title.replace("会議への参加 | ", "")
        more = El("その他", aid="callingButtons-showMoreBtn", patterns=("expand",))

        def open_more():
            items = [El("ビデオの効果と設定", ctype="MenuItem"),
                     El("言語と音声", aid="LanguageSpeechMenuControl-id",
                        ctype="MenuItem", patterns=("expand",))]
            if business:
                record = El("録画と文字起こし", ctype="MenuItem", patterns=("expand",))

                def open_record():
                    def start():
                        state["transcribing"] = True
                    record.children = [El("文字起こしの開始", ctype="MenuItem",
                                          on_press=start)]
                record.on_press = open_record
                items.append(record)
            more.children = items
        more.on_press = open_more
        controls = [
            El("閉じる"),     # 窓のタイトルバー。これを押したら会議の窓が閉じる
            El("録画", aid="recording-button"),
            more,
            El("退出します", aid="hangup-button"),
        ]
        if invite:
            dialog = El("会議への参加を求めるユーザーを招待してください", ctype="Window")
            dialog.children = [
                El("会議への参加を求めるユーザーを招待してください", ctype="Text"),
                El("閉じる", on_press=lambda: controls.remove(dialog)),
            ]
            controls.append(dialog)
        meeting.root.children = controls

    def start():
        if not prejoin_opens:
            return
        title = box.value
        meeting.title = f"会議への参加 | {title} | Microsoft Teams"
        meeting.appear_at = backend.clock.now() + 2.0   # 窓が出るまで少しかかる
        meeting.root.children = [
            El("閉じる"),
            camera_button(),
            mic_button(),
            El("コンピューターの音声", ctype="RadioButton"),
            El("今すぐ参加", aid="prejoin-join-button", on_press=join),
            El("キャンセル"),
        ]
        main.root.children.remove(dialog)

    box = El("会議名", aid="meeting-title-input", ctype="ComboBox",
             patterns=("value",), value="nagato yota との会議")
    dialog = El("今すぐ会議を開始する", ctype="Window", children=[
        box,
        El("会議を開始", on_press=start),
        El("共有リンクを取得する"),
        El("閉じる"),
    ])

    def meet_now():
        main.root.children.append(dialog)

    main.root.children = [
        El("閉じる"),
        El("チャット", aid="86fcd49b-61a2-4701-b771-54728cd291fb"),
        El("今すぐ会議", on_press=meet_now),
    ]
    backend.wins = [main, meeting]
    return state, main, meeting, box


def run_engine(backend, clock, steps=None, title="一人会議 10/08 14:30 設計",
               camera="off", mic="on"):
    events = []
    engine = tm.Engine(backend, steps or tm.BUILTIN_STEPS,
                       {"title": title, "camera": camera, "mic": mic},
                       on_event=events.append, poll=0.3,
                       clock=clock.now, sleep=clock.sleep)
    return engine.run(), events


def _status(summary):
    return {s["key"]: s["status"] for s in summary["steps"]}


# ---------------------------------------------------------------
# 会議名
# ---------------------------------------------------------------
NOW = datetime(2026, 10, 8, 14, 30)


def test_title_expands_date_and_topic():
    assert tm.format_title(None, "設計レビュー", NOW) == "一人会議 10/08 14:30 設計レビュー"


def test_title_empty_topic_leaves_no_trailing_space():
    assert tm.format_title(None, "", NOW) == "一人会議 10/08 14:30"
    assert tm.format_title(None, "   ", NOW) == "一人会議 10/08 14:30"


def test_title_empty_topic_in_middle_collapses_spaces():
    assert tm.format_title("{topic} 会議 {time}", "", NOW) == "会議 14:30"
    assert tm.format_title("一人会議 {topic} {date:%H:%M}", "", NOW) == "一人会議 14:30"


def test_title_topic_is_one_line():
    assert tm.format_title(None, "設計\nレビュー  2回目", NOW) == \
        "一人会議 10/08 14:30 設計 レビュー 2回目"


def test_title_default_formats_and_needs_topic():
    assert tm.format_title("{date} {datetime}", "", NOW) == "2026/10/08 2026/10/08 14:30"
    assert tm.needs_topic(None)
    assert not tm.needs_topic("会議 {date}")


# ---------------------------------------------------------------
# 設定の重ね方
# ---------------------------------------------------------------
def test_merge_overrides_only_given_fields():
    steps = tm.merge_steps(tm.BUILTIN_STEPS, [{"key": "meet_now", "timeout": 60}])
    meet = next(s for s in steps if s["key"] == "meet_now")
    assert meet["timeout"] == 60
    assert meet["find"]["name"][0] == "今すぐ会議"        # 書かなかった項目は残る
    assert [s["key"] for s in steps] == [s["key"] for s in tm.BUILTIN_STEPS]
    # 元の BUILTIN_STEPS は書き換わらない
    assert next(s for s in tm.BUILTIN_STEPS if s["key"] == "meet_now")["timeout"] == 30


def test_merge_adds_steps_at_position_and_end():
    steps = tm.merge_steps(tm.BUILTIN_STEPS, [
        {"key": "confirm", "after": "transcript", "action": "invoke"},
        {"key": "first", "before": "teams", "action": "wait"},
        {"key": "tail", "action": "wait"},
        {"key": "lost", "after": "no-such-step", "action": "wait"},
    ])
    keys = [s["key"] for s in steps]
    assert keys[0] == "first"
    assert keys[keys.index("transcript") + 1] == "confirm"
    assert keys[-2:] == ["tail", "lost"]
    assert "after" not in steps[keys.index("confirm")]


def test_merge_accepts_dict_form_disabled_and_ignores_garbage():
    steps = tm.merge_steps(tm.BUILTIN_STEPS, {"transcript": {"disabled": True}})
    assert next(s for s in steps if s["key"] == "transcript")["disabled"] is True
    steps = tm.merge_steps(tm.BUILTIN_STEPS, ["x", {"no_key": 1}, None, {"key": ""}])
    assert len(steps) == len(tm.BUILTIN_STEPS)
    assert len(tm.merge_steps(tm.BUILTIN_STEPS, "壊れた値")) == len(tm.BUILTIN_STEPS)


def test_load_config_defaults_and_bad_values():
    cfg = tm.load_config({})
    assert cfg["camera"] == "off" and cfg["mic"] == "on"
    assert cfg["title_format"] == tm.DEFAULT_TITLE_FORMAT
    assert cfg["app"]["process_name"] == "ms-teams.exe"
    cfg = tm.load_config({"teams_meeting": {"camera": "ON", "mic": "なし",
                                            "poll_seconds": "x",
                                            "app": {"window_class": "TeamsWebView"}}})
    assert cfg["camera"] == "on" and cfg["mic"] == "on"
    assert cfg["poll_seconds"] == tm.DEFAULT_POLL_SECONDS
    assert cfg["app"]["window_class"] == "TeamsWebView"
    assert cfg["app"]["process_name"] == "ms-teams.exe"


# ---------------------------------------------------------------
# エンジン
# ---------------------------------------------------------------
def test_all_steps_succeed_on_business_teams():
    clock = Clock()
    backend = FakeBackend(clock)
    state, main, meeting, box = build_teams(backend, business=True)
    summary, events = run_engine(backend, clock)
    assert summary["ok"], summary
    st = _status(summary)
    assert st["teams"] == "ok"
    assert st["open_chat"] == "skipped"          # 今すぐ会議が既に見えていた
    for key in ("meet_now", "title", "start", "prejoin", "camera", "mic",
                "join", "joined", "close_invite", "transcript"):
        assert st[key] == "ok", (key, summary)
    assert summary["transcript"] == "started" and state["transcribing"]
    assert box.value == "一人会議 10/08 14:30 設計"
    assert meeting.title == "一人会議 10/08 14:30 設計 | Microsoft Teams"
    # タイトルバーの「閉じる」は一度も押していない(押したのはダイアログの中だけ)
    assert backend.pressed().count("閉じる") == 1
    assert not any(isinstance(c, El) and c.name.startswith("会議への参加を求める")
                   for c in meeting.root.children)
    # 「その他」は ExpandCollapse でしか開かない
    assert ("press", "その他", "expand") in backend.log
    # 会議名を入れてから「会議を開始」までに待ちを挟まない
    i_set = next(i for i, e in enumerate(events) if e.get("key") == "title"
                 and e["event"] == "step_end")
    i_start = next(i for i, e in enumerate(events) if e.get("key") == "start"
                   and e["event"] == "step_start")
    assert i_start == i_set + 1
    assert events[0]["event"] == "start" and events[-1]["event"] == "done"
    assert "文字起こしを開始しました" in tm.describe_result(summary)


def test_free_teams_skips_transcript_and_still_succeeds():
    clock = Clock()
    backend = FakeBackend(clock)
    state, *_ = build_teams(backend, business=False)
    summary, _events = run_engine(backend, clock)
    assert summary["ok"]
    assert _status(summary)["transcript"] == "skipped"
    assert summary["transcript"] == "not_found"
    assert not state["transcribing"]
    # 開いた「その他」は畳んでから諦める
    assert ("collapse", "その他") in backend.log
    message = tm.describe_result(summary)
    assert "会議名: 一人会議 10/08 14:30 設計" in message
    assert "手で開始してください" in message


def test_required_step_timeout_stops_and_reports():
    clock = Clock()
    backend = FakeBackend(clock)
    build_teams(backend, prejoin_opens=False)
    summary, events = run_engine(backend, clock)
    assert not summary["ok"]
    assert summary["stopped_at"] == "prejoin"
    assert summary["stopped_label"] == "参加前の画面を待つ"
    assert "30秒以内に見つかりません" in summary["stopped_detail"]
    done_keys = [s["key"] for s in summary["steps"]]
    assert done_keys[-1] == "prejoin" and "join" not in done_keys
    assert summary["transcript"] == "not_reached"
    assert "今すぐ参加" not in backend.pressed()
    message = tm.describe_result(summary)
    assert "「参加前の画面を待つ」で止まりました" in message and "手で続けて" in message
    # タイムアウトは偽の時計で 30 秒ぶん進んだだけ
    assert 30 <= clock.now() < 40


def test_optional_invite_dialog_missing_is_skipped_without_touching_titlebar():
    clock = Clock()
    backend = FakeBackend(clock)
    build_teams(backend, invite=False)
    summary, _ = run_engine(backend, clock)
    assert summary["ok"]
    assert _status(summary)["close_invite"] == "skipped"
    assert "閉じる" not in backend.pressed()     # 窓のタイトルバーの「閉じる」を押さない


def test_camera_and_mic_toggled_only_when_needed():
    # カメラがオン・マイクがミュート → 両方押す
    clock = Clock()
    backend = FakeBackend(clock)
    build_teams(backend, camera_on=True, mic_muted=True)
    summary, _ = run_engine(backend, clock, camera="off", mic="on")
    pressed = backend.pressed()
    assert any(p.startswith("カメラをオフにします") for p in pressed)
    assert any(p.startswith("マイクのミュートを解除") for p in pressed)
    details = {s["key"]: s["detail"] for s in summary["steps"]}
    assert details["camera"] == "オン→オフにしました"
    assert details["mic"] == "オフ→オンにしました"

    # 既に望みどおり → 押さない
    clock = Clock()
    backend = FakeBackend(clock)
    build_teams(backend, camera_on=False, mic_muted=False)
    summary, _ = run_engine(backend, clock, camera="off", mic="on")
    pressed = backend.pressed()
    assert not any(p.startswith("カメラ") or p.startswith("マイク") for p in pressed)
    details = {s["key"]: s["detail"] for s in summary["steps"]}
    assert details["camera"] == "既にオフ" and details["mic"] == "既にオン"

    # keep → 見もしない
    clock = Clock()
    backend = FakeBackend(clock)
    build_teams(backend, camera_on=True, mic_muted=True)
    summary, _ = run_engine(backend, clock, camera="keep", mic="keep")
    assert _status(summary)["camera"] == "skipped"
    assert not any(p.startswith("カメラ") or p.startswith("マイク") for p in backend.pressed())


def test_state_classifier_prefers_longer_prefix():
    on = ["マイクのミュートを解除", "Unmute"]
    off = ["マイクのミュート", "Mute"]
    assert tm._classify_state("マイクのミュートを解除(Ctrl+Shift+M)", on, off) == "off"
    assert tm._classify_state("マイクのミュート(Ctrl+Shift+M)", on, off) == "on"
    assert tm._classify_state("Unmute", on, off) == "off"
    assert tm._classify_state("Mute", on, off) == "on"
    assert tm._classify_state("何か別のボタン", on, off) is None


def test_expand_only_element_is_pressed_with_invoke_preference():
    clock = Clock()
    backend = FakeBackend(clock)
    more = El("その他", aid="callingButtons-showMoreBtn", patterns=("expand",))
    backend.wins = [Win(5, "会議 | Microsoft Teams", [more])]
    steps = [{"key": "more", "action": "invoke", "find": {"name": "その他"}, "timeout": 1}]
    summary, _ = run_engine(backend, clock, steps=steps)
    assert summary["ok"]
    assert backend.log == [("press", "その他", "expand")]


def test_menu_path_falls_back_to_second_route():
    clock = Clock()
    backend = FakeBackend(clock)
    hit = {"done": False}
    more = El("その他", patterns=("expand",))
    record_btn = El("録画", aid="recording-button", patterns=("expand",))

    def open_record():
        record_btn.children = [El("文字起こしを開始", ctype="MenuItem",
                                  on_press=lambda: hit.update(done=True))]
    record_btn.on_press = open_record
    backend.wins = [Win(5, "会議 | Microsoft Teams", [more, record_btn])]
    steps = [{
        "key": "transcript", "label": "文字起こし", "action": "path", "timeout": 2,
        "routes": [
            [{"find": {"name": "その他"}, "action": "expand"},
             {"find": {"name": "録画と文字起こし"}, "action": "expand"}],
            [{"find": {"automation_id": "recording-button"}, "action": "expand"},
             {"find": {"name": ["文字起こしの開始", "文字起こしを開始"]}}],
        ],
    }]
    summary, _ = run_engine(backend, clock, steps=steps)
    assert summary["ok"], summary
    assert hit["done"]
    assert ("collapse", "その他") in backend.log     # 1本目で開いたものは畳んだ
    assert summary["steps"][0]["detail"] == "録画 → 文字起こしを開始"
    assert summary["transcript"] == "started"


def test_menu_path_all_routes_fail_reports_what_was_missing():
    clock = Clock()
    backend = FakeBackend(clock)
    backend.wins = [Win(5, "会議 | Microsoft Teams", [El("その他", patterns=("expand",))])]
    steps = [{"key": "transcript", "action": "path", "timeout": 1,
              "path": [{"find": {"name": "その他"}, "action": "expand"},
                       {"find": {"name": "録画と文字起こし"}}]}]
    summary, _ = run_engine(backend, clock, steps=steps)
    assert not summary["ok"]
    assert "「録画と文字起こし」" in summary["stopped_detail"]


def test_launches_teams_when_no_window_and_waits():
    clock = Clock()
    backend = FakeBackend(clock)
    build_teams(backend)
    main = backend.wins[0]
    main.appear_at = 10 ** 9

    def on_launch():
        main.appear_at = clock.now() + 8.0        # 8秒後に窓が出る
    backend.on_launch = on_launch
    summary, _ = run_engine(backend, clock)
    assert backend.launched == 1
    assert summary["ok"], summary
    assert summary["steps"][0]["detail"] == "起動しました"


def test_launch_timeout_stops():
    clock = Clock()
    backend = FakeBackend(clock)
    summary, _ = run_engine(backend, clock)
    assert backend.launched == 1
    assert summary["stopped_at"] == "teams"
    assert "出ません" in summary["stopped_detail"]


def test_title_is_rewritten_when_stray_keys_slip_in():
    clock = Clock()
    backend = FakeBackend(clock)
    _state, _main, _meeting, box = build_teams(backend)
    calls = {"n": 0}

    def hook(el):
        calls["n"] += 1
        if calls["n"] == 1:
            el.value += "susum"       # 別の窓で打っていた文字が混ざった(実際に起きた)
    backend.set_hook = hook
    summary, _ = run_engine(backend, clock)
    assert summary["ok"]
    assert box.value == "一人会議 10/08 14:30 設計"
    detail = next(s["detail"] for s in summary["steps"] if s["key"] == "title")
    assert "入れ直しました" in detail


def test_open_chat_runs_when_meet_now_is_not_visible():
    clock = Clock()
    backend = FakeBackend(clock)
    build_teams(backend)
    main = backend.wins[0]
    meet_now = next(c for c in main.root.children if c.name == "今すぐ会議")
    main.root.children.remove(meet_now)
    tab_pressed = []
    settings_tab = El("チャット", ctype="TabItem",     # 設定画面の同名タブ。押さない
                      on_press=lambda: tab_pressed.append(1))
    main.root.children.insert(0, settings_tab)
    chat = next(c for c in main.root.children if c.aid.startswith("86fcd49b"))
    chat.on_press = lambda: main.root.children.append(meet_now)
    summary, _ = run_engine(backend, clock)
    assert summary["ok"], summary
    assert _status(summary)["open_chat"] == "ok"
    # 押したのは左端のボタン(aid で当たる)で、設定画面の同名タブではない
    assert backend.log[0] == ("press", "チャット", "invoke")
    assert not tab_pressed and backend.pressed().count("チャット") == 1


def test_meeting_window_found_by_title_and_old_meeting_window_ignored():
    """前の会議の窓(同じ exe)が残っていても、そちらのボタンは押さない。"""
    clock = Clock()
    backend = FakeBackend(clock)
    build_teams(backend)
    old = Win(9, "一人会議 10/08 13:00 前の会議 | Microsoft Teams", [
        El("今すぐ参加", aid="prejoin-join-button"),
        El("退出します", aid="hangup-button"),
    ])
    backend.wins.append(old)
    summary, _ = run_engine(backend, clock)
    assert summary["ok"]
    joined = next(s for s in summary["steps"] if s["key"] == "prejoin")
    assert "一人会議 10/08 14:30 設計" in joined["detail"]


def test_main_window_with_meeting_title_is_not_used_for_meeting_steps():
    """会議を立てるとメイン窓のタイトルにも会議名が入りうる。そこに同名の「その他」が
    居ても、会議の窓のほうを押す。"""
    clock = Clock()
    backend = FakeBackend(clock)
    state, main, meeting, _box = build_teams(backend, business=True)
    decoy = {"pressed": False}
    main.root.children.append(
        El("その他", patterns=("expand",), on_press=lambda: decoy.update(pressed=True)))
    original = main.root.children[2].on_press      # 「今すぐ会議」

    def meet_now_and_rename():
        original()
        main.title = "チャット | 一人会議 10/08 14:30 設計 | Microsoft Teams"
    main.root.children[2].on_press = meet_now_and_rename
    summary, _ = run_engine(backend, clock)
    assert summary["ok"], summary
    assert not decoy["pressed"]
    assert state["transcribing"]


def test_disabled_step_is_not_run_and_crash_message():
    clock = Clock()
    backend = FakeBackend(clock)
    build_teams(backend, business=True)
    steps = tm.merge_steps(tm.BUILTIN_STEPS, [{"key": "transcript", "disabled": True}])
    summary, _ = run_engine(backend, clock, steps=steps)
    assert summary["ok"] and summary["transcript"] == "disabled"
    assert "手で開始" in tm.describe_result(summary)
    assert "落ちました" in tm.describe_result({"crashed": True, "ok": False})


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
