# tests/test_editor_ai_target.py
# 「AI の投げ先」(editor/ai_target.py の整え、設定画面
# editor/ai_target_dialog.py、窓の送信路の配線)を確かめる。
#
# プロファイルの形の正本は copilot_loop.BUILTIN_PROFILES で、
# 保存したカスタムが copilot_loop.profiles() を往復して同じ
# 辞書で戻ることまで見る。実 UIA の検出は実機でしか確かめ
# られないので、画面には fake の prober / filler を差し込む。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_ai_target.py
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import copilot_io  # noqa: E402
import copilot_loop  # noqa: E402
import editor  # noqa: E402
from editor import ai_target as aitmod  # noqa: E402
import editor.ai_target_dialog as dialogmod  # noqa: E402
from editor.ai_target_dialog import AiTargetDialog  # noqa: E402
import editor.window as windowmod  # noqa: E402

APP = None

FIELDS = {
    "label": "私のClaude",
    "process_name": "Claude.exe",
    "window_class": "Chrome_WidgetWin_1",
    "window_title_contains": "Claude",
    "input_automation_id": "chat-input",
    "app_id_prefix": "",
}


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_window(**kwargs):
    app()
    window = editor.create_window(**kwargs)
    window.show()
    return window


def discard_close(window):
    window.close()
    window.deleteLater()
    app().processEvents()


def wait_send(window, timeout=5.0):
    deadline = time.time() + timeout
    while window._send_thread is not None and time.time() < deadline:
        app().processEvents()
        time.sleep(0.02)
    app().processEvents()
    assert window._send_thread is None, "送信スレッドが終わりません"


def wait_dialog_op(dialog, timeout=5.0):
    deadline = time.time() + timeout
    while dialog._op_thread is not None and time.time() < deadline:
        app().processEvents()
        time.sleep(0.02)
    app().processEvents()
    assert dialog._op_thread is None, "画面の裏処理が終わりません"


# ---------------------------------------------------------------------
# プロファイルの整え (Qt 非依存の部分)
# ---------------------------------------------------------------------
def test_custom_profile_shape_matches_builtins():
    profile = aitmod.build_custom_profile(FIELDS)
    builtin_keys = set(copilot_loop.BUILTIN_PROFILES[0])
    # 組み込みのキー一式に、表示名とアプリIDの頭だけが増える。
    assert set(profile) == builtin_keys | {"label", "app_id_prefix"}
    assert profile["name"] == aitmod.CUSTOM_PROFILE_NAME
    assert profile["process_name"] == "Claude.exe"
    assert profile["window_class"] == "Chrome_WidgetWin_1"
    assert profile["window_title_contains"] == "Claude"
    assert profile["input_automation_id"] == "chat-input"
    assert profile["app_id_prefix"] == ""
    # 測っていない部分は安全な既定 (入力欄の帯・送信ボタンの候補)。
    assert profile["input_band_px"] == 170
    assert "送信" in profile["send_button"]
    assert profile["render_class"] == "Chrome_RenderWidgetHostHWND"


def test_custom_profile_roundtrip_through_copilot_loop():
    profile = aitmod.build_custom_profile(FIELDS)
    merged = copilot_loop.profiles({"copilot_profiles": [profile]})
    assert merged[0] == profile
    assert [p["name"] for p in merged[1:]] == ["mscopilot", "m365"]


def test_custom_profile_same_name_overrides_builtin():
    override = aitmod.build_custom_profile(
        {**FIELDS, "label": "差し替え"})
    override["name"] = "mscopilot"
    merged = copilot_loop.profiles({"copilot_profiles": [override]})
    assert merged[0]["process_name"] == "Claude.exe"
    assert [p["name"] for p in merged].count("mscopilot") == 1


def test_fields_from_window_row_trims_title():
    row = {"title": "雑談の続き - Claude",
           "window_class": "Chrome_WidgetWin_1",
           "process_name": "Claude.exe"}
    fields = aitmod.fields_from_window_row(row)
    assert fields["window_title_contains"] == "Claude"
    assert fields["label"] == "Claude"
    assert fields["process_name"] == "Claude.exe"
    fields = aitmod.fields_from_window_row(
        {"title": "区切りの無いタイトル", "window_class": "C",
         "process_name": "p.exe"})
    assert fields["window_title_contains"] == "区切りの無いタイトル"
    long = aitmod.fields_from_window_row(
        {"title": "あ" * 100, "window_class": "", "process_name": ""})
    assert len(long["window_title_contains"]) == 60


def test_sanitize_and_upsert_profiles():
    custom = aitmod.build_custom_profile(FIELDS)
    got = aitmod.sanitize_profiles(
        [custom, {"name": ""}, "文字列", None, {"noname": 1}])
    assert got == [custom]
    replaced = aitmod.upsert_profile(
        got, aitmod.build_custom_profile(
            {**FIELDS, "process_name": "Other.exe"}))
    assert len(replaced) == 1
    assert replaced[0]["process_name"] == "Other.exe"
    assert aitmod.sanitize_selection("mscopilot") == "mscopilot"
    assert aitmod.sanitize_selection(None) == ""
    assert aitmod.sanitize_selection(123) == ""


def test_display_name_and_fields_roundtrip():
    assert aitmod.display_name({"name": "mscopilot"}) == "Copilot"
    assert aitmod.display_name({"name": "m365"}) == \
        "Microsoft 365 Copilot"
    assert aitmod.display_name(
        {"name": "x", "label": "表示名"}) == "表示名"
    fields = aitmod.fields_from_profile(
        aitmod.build_custom_profile(FIELDS))
    assert fields == FIELDS
    # AutomationId が並びのときは、先頭を欄へ出す。
    profile = dict(aitmod.build_custom_profile(FIELDS))
    profile["input_automation_id"] = ["first-id", "second-id"]
    assert aitmod.fields_from_profile(profile)[
        "input_automation_id"] == "first-id"


# ---------------------------------------------------------------------
# 設定画面 (fake の prober / filler)
# ---------------------------------------------------------------------
def make_dialog(selection="", custom=None, **overrides):
    app()
    kwargs = {
        "enumerate_windows": lambda: [],
        "prober": lambda profile: copilot_io.ProbeResult(
            True, "入力欄の候補が 1 件見つかりました",
            [{"automation_id": "found-id", "control_type": "Edit",
              "name": ""}]),
        "filler": lambda text, profile: copilot_io.SendResult(
            True, "入力欄に入れました（送信はしていません）"),
    }
    kwargs.update(overrides)
    dialog = AiTargetDialog(
        copilot_loop.profiles(), selection, custom, **kwargs)
    dialog.show()
    app().processEvents()
    return dialog


def test_dialog_custom_group_follows_selection():
    dialog = make_dialog()
    try:
        assert dialog.selection_name() == ""
        assert not dialog._custom_group.isEnabled()
        index = dialog._target_combo.findData(aitmod.CUSTOM_PROFILE_NAME)
        dialog._target_combo.setCurrentIndex(index)
        assert dialog.selection_name() == aitmod.CUSTOM_PROFILE_NAME
        assert dialog._custom_group.isEnabled()
    finally:
        dialog.close()


def test_dialog_loads_stored_custom():
    custom = aitmod.build_custom_profile(FIELDS)
    dialog = make_dialog(selection=aitmod.CUSTOM_PROFILE_NAME,
                         custom=custom)
    try:
        assert dialog.current_fields() == FIELDS
        assert not dialog.custom_dirty  # 初期値の流し込みは変更でない
    finally:
        dialog.close()


def test_dialog_picker_fills_fields():
    row = {"title": "雑談 - Claude",
           "window_class": "Chrome_WidgetWin_1",
           "process_name": "Claude.exe"}

    class FakePicker:
        def __init__(self, rows, parent=None):
            self._rows = rows

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_row(self):
            return self._rows[0] if self._rows else None

    real = dialogmod.WindowPickerDialog
    dialogmod.WindowPickerDialog = FakePicker
    try:
        dialog = make_dialog(enumerate_windows=lambda: [row])
        try:
            dialog._pick_button.click()
            app().processEvents()
            assert dialog.selection_name() == aitmod.CUSTOM_PROFILE_NAME
            fields = dialog.current_fields()
            assert fields["process_name"] == "Claude.exe"
            assert fields["window_class"] == "Chrome_WidgetWin_1"
            assert fields["window_title_contains"] == "Claude"
            assert dialog.custom_dirty
        finally:
            dialog.close()
    finally:
        dialogmod.WindowPickerDialog = real


def test_dialog_probe_fills_automation_id():
    seen = []

    def prober(profile):
        seen.append(profile)
        return copilot_io.ProbeResult(
            True, "入力欄の候補が 1 件見つかりました",
            [{"automation_id": "", "control_type": "Edit", "name": ""},
             {"automation_id": "found-id", "control_type": "Edit",
              "name": ""}])

    dialog = make_dialog(prober=prober)
    try:
        dialog._process_edit.setText("Claude.exe")
        dialog._detect_button.click()
        wait_dialog_op(dialog)
        # 検出は編集中のカスタムに対して走り、空でない最初の
        # AutomationId が欄に入る。
        assert seen and seen[0]["name"] == aitmod.CUSTOM_PROFILE_NAME
        assert seen[0]["process_name"] == "Claude.exe"
        assert dialog._aid_edit.text() == "found-id"
        assert "found-id" in dialog._status_label.text()
    finally:
        dialog.close()


def test_dialog_probe_failure_reports_reason():
    dialog = make_dialog(prober=lambda profile: copilot_io.ProbeResult(
        False, "投げ先の窓が見つかりませんでした"))
    try:
        dialog._detect_button.click()
        wait_dialog_op(dialog)
        assert dialog._status_label.text() == \
            "投げ先の窓が見つかりませんでした"
        assert dialog._aid_edit.text() == ""
    finally:
        dialog.close()


def test_dialog_test_button_passes_selection_and_never_retries():
    calls = []

    def filler(text, profile):
        calls.append((text, profile))
        return copilot_io.SendResult(
            False, "入力欄に書きかけの文があります。"
            "テストでも上書きはしません", needs_confirm=True)

    dialog = make_dialog(filler=filler)
    try:
        # 自動: profile は None、テスト文は3行の固定文。
        dialog._test_button.click()
        wait_dialog_op(dialog)
        assert calls == [(aitmod.TEST_TEXT, None)]
        assert aitmod.TEST_TEXT.count("\n") == 2
        # 書きかけがあっても、確認に進まず文だけ出して終わる。
        assert "上書きはしません" in dialog._status_label.text()
        # 組み込み: 名前の文字列で渡る。
        index = dialog._target_combo.findData("m365")
        dialog._target_combo.setCurrentIndex(index)
        dialog._test_button.click()
        wait_dialog_op(dialog)
        assert calls[-1] == (aitmod.TEST_TEXT, "m365")
        # カスタム: 欄から組み立てた辞書で渡る。
        index = dialog._target_combo.findData(aitmod.CUSTOM_PROFILE_NAME)
        dialog._target_combo.setCurrentIndex(index)
        dialog._process_edit.setText("Claude.exe")
        dialog._test_button.click()
        wait_dialog_op(dialog)
        assert calls[-1][1]["name"] == aitmod.CUSTOM_PROFILE_NAME
        assert calls[-1][1]["process_name"] == "Claude.exe"
    finally:
        dialog.close()


def test_dialog_accept_requires_custom_identity():
    dialog = make_dialog()
    try:
        index = dialog._target_combo.findData(aitmod.CUSTOM_PROFILE_NAME)
        dialog._target_combo.setCurrentIndex(index)
        for edit in dialog._field_edits():
            edit.setText("")
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert "どれかが必要です" in dialog._status_label.text()
        dialog._process_edit.setText("Claude.exe")
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        dialog.close()


# ---------------------------------------------------------------------
# 窓の送信路と設定の保存
# ---------------------------------------------------------------------
def test_send_with_builtin_profile_passes_name():
    calls = []

    def fake_sender(text, force=False, profile=None):
        calls.append((text, force, profile))
        return copilot_io.SendResult(True, "送信できました(テスト)")

    window = make_window(
        sender=fake_sender, editor_settings={"ai_target": "mscopilot"})
    try:
        window._edit.setPlainText("送る範囲")
        window._edit.selectAll()
        window.send_selection()
        wait_send(window)
        assert calls == [("送る範囲", False, "mscopilot")]
    finally:
        discard_close(window)


def test_send_auto_keeps_legacy_call_shape():
    calls = []

    def fake_sender(text, force=False):  # profile を受けない従来の形
        calls.append((text, force))
        if not force:
            return copilot_io.SendResult(
                False, "書きかけがあります", needs_confirm=True)
        return copilot_io.SendResult(True, "送信できました(テスト)")

    window = make_window(sender=fake_sender)
    try:
        window.ask_overwrite_input = lambda: True
        window._edit.setPlainText("送る範囲")
        window._edit.selectAll()
        window.send_selection()
        wait_send(window)
        # 自動のときは profile を渡さない (呼び方が従来と同一)。
        assert calls == [("送る範囲", False), ("送る範囲", True)]
    finally:
        discard_close(window)


def test_send_with_custom_profile_passes_dict_and_guards_draft():
    custom = aitmod.build_custom_profile(FIELDS)
    calls = []

    def fake_sender(text, force=False, profile=None):
        calls.append((force, profile))
        if not force:
            return copilot_io.SendResult(
                False, "書きかけがあります", needs_confirm=True)
        return copilot_io.SendResult(True, "送信できました(テスト)")

    window = make_window(
        sender=fake_sender,
        editor_settings={"ai_target": aitmod.CUSTOM_PROFILE_NAME,
                         "copilot_profiles": [custom]})
    try:
        window.ask_overwrite_input = lambda: True
        window._edit.setPlainText("送る範囲")
        window._edit.selectAll()
        window.send_selection()
        wait_send(window)
        # 書きかけの確認 (最初の送信) も、送り直しも、同じ
        # カスタム辞書が付いて行く。
        assert calls == [(False, custom), (True, custom)]
    finally:
        discard_close(window)


def test_ai_target_settings_persist_and_reload():
    changes = []
    window = make_window(
        on_settings_change=lambda diff: changes.append(diff))
    try:
        changes.clear()  # 起動時に流れる別件の差分は見ない
        custom = aitmod.build_custom_profile(FIELDS)
        window.apply_ai_target(aitmod.CUSTOM_PROFILE_NAME, custom)
        assert changes == [{
            "ai_target": aitmod.CUSTOM_PROFILE_NAME,
            "copilot_profiles": [custom]}]
        assert window.ai_target_selection() == aitmod.CUSTOM_PROFILE_NAME
        assert window.ai_target_profile() == custom
        # 変わらない適用では差分を流さない。
        window.apply_ai_target(aitmod.CUSTOM_PROFILE_NAME)
        assert len(changes) == 1
    finally:
        discard_close(window)
    # 保存された節で開き直すと、選択とカスタムが戻る。
    reloaded = make_window(editor_settings={
        "ai_target": aitmod.CUSTOM_PROFILE_NAME,
        "copilot_profiles": [custom]})
    try:
        assert reloaded.ai_target_selection() == \
            aitmod.CUSTOM_PROFILE_NAME
        assert reloaded.ai_target_profile() == custom
    finally:
        discard_close(reloaded)


def test_show_ai_target_settings_wires_dialog_result():
    class FakeDialog:
        def __init__(self, builtins, selection, custom, parent=None,
                     **kwargs):
            FakeDialog.seen = (builtins, selection, custom, kwargs)
            self.custom_dirty = True

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selection_name(self):
            return aitmod.CUSTOM_PROFILE_NAME

        def custom_profile(self):
            return aitmod.build_custom_profile(FIELDS)

    real_dialog = windowmod.AiTargetDialog
    real_list = copilot_io.list_profiles
    windowmod.AiTargetDialog = FakeDialog
    copilot_io.list_profiles = lambda timeout=0: copilot_loop.profiles()
    changes = []
    try:
        window = make_window(
            on_settings_change=lambda diff: changes.append(diff))
        try:
            changes.clear()  # 起動時に流れる別件の差分は見ない
            window.show_ai_target_settings()
            builtins, selection, custom, kwargs = FakeDialog.seen
            assert [p["name"] for p in builtins] == ["mscopilot", "m365"]
            assert selection == "" and custom is None
            assert callable(kwargs["prober"])
            assert callable(kwargs["filler"])
            assert changes and \
                changes[0]["ai_target"] == aitmod.CUSTOM_PROFILE_NAME
            saved = changes[0]["copilot_profiles"][0]
            assert saved["process_name"] == "Claude.exe"
            # 保存された形は copilot_loop.profiles() を往復できる。
            assert copilot_loop.profiles(
                {"copilot_profiles": [saved]})[0] == saved
        finally:
            discard_close(window)
    finally:
        windowmod.AiTargetDialog = real_dialog
        copilot_io.list_profiles = real_list


def test_default_sender_passes_force_as_keyword():
    """既定の送信役は copilot_io.send_text へ force をキーワードで
    渡す。位置で渡すと transport に化けて、書きかけ確認のあとの
    送り直しが必ず失敗する (実物で踏んだ不具合の固定)。"""
    recorded = {}

    def fake_send_text(text, transport=None, force=False, profile=None):
        recorded.update(text=text, transport=transport, force=force,
                        profile=profile)
        return copilot_io.SendResult(True, "ok")

    real = copilot_io.send_text
    copilot_io.send_text = fake_send_text
    try:
        window = make_window()  # sender を差し替えない = 既定の役
        try:
            result = window._sender("本文", True)
            assert result.ok
            assert recorded == {"text": "本文", "transport": None,
                                "force": True, "profile": None}
        finally:
            discard_close(window)
    finally:
        copilot_io.send_text = real


def test_ai_menu_has_target_settings_item():
    window = make_window()
    try:
        ai_items = None
        for action in window.menuBar().actions():
            menu = action.menu()
            if menu is not None and action.text().startswith("AI"):
                ai_items = [item.text() for item in menu.actions()]
        assert ai_items is not None
        assert "投げ先の設定(&T)…" in ai_items
    finally:
        discard_close(window)


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
