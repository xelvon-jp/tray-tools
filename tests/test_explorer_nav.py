# explorer_nav のテスト。実機(Windows + エクスプローラ)が要る部分は
# 「取れない環境では静かに False / None に倒れる」契約だけを確かめる。
import explorer_nav


def test_activate_window_quiet_without_user32():
    # VM など user32 が無い環境では、例外なく False(前面化は任意の上乗せ)
    if explorer_nav._user32 is None:
        assert explorer_nav.activate_window(1234) is False
    assert explorer_nav.activate_window(0) is False
    assert explorer_nav.activate_window(None) is False


def test_navigate_rejects_empty_path():
    assert explorer_nav.navigate(1234, "") is False
    assert explorer_nav.navigate(1234, None) is False
    assert explorer_nav.navigate_new_tab(1234, "") is False
    assert explorer_nav.navigate_new_tab(1234, None) is False


def test_last_active_quiet_without_explorer():
    # 取れない環境では 0(呼び出し側は新規窓に倒す)
    assert explorer_nav.last_active_hwnd() in (0,)


ALL_TESTS = [test_activate_window_quiet_without_user32,
             test_navigate_rejects_empty_path,
             test_last_active_quiet_without_explorer]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
