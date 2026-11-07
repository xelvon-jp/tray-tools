# test_chara_sprites.py
# ドット絵データとフレーム選びの純関数のテスト(Qt 不要)。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chara_sprites as sp


def test_frame_sizes():
    assert set(sp.CLAWD_FRAMES) == {"base", "blink", "lookL", "lookR", "walkA", "walkB",
                                    "waveA", "waveB", "alert", "happy", "sleep"}
    for name, rows in sp.CLAWD_FRAMES.items():
        assert len(rows) == 8 and all(len(r) == 12 for r in rows), name
        assert set("".join(rows)) <= set(".Oed"), name
    assert len(sp.HAT) == 6 and all(len(r) == 12 for r in sp.HAT)
    assert set(sp.MUSE_FRAMES) == {"idle", "blink", "work-a", "work-b", "wait", "alert",
                                   "happy", "sleep", "look-l", "look-r"}
    for name, rows in sp.MUSE_FRAMES.items():
        assert len(rows) == 10 and all(len(r) == 12 for r in rows), name
        assert set("".join(rows)) <= set(".BPES"), name
        r2 = sp.muse_rows(name)
        assert len(r2) == 10 and all(len(r) == 12 for r in r2), name


def test_clawd_shape():
    base = sp.CLAWD_FRAMES["base"]
    assert base[1][3] == "e" and base[1][8] == "e"          # 目
    assert sp.CLAWD_FRAMES["blink"][1][3] == "d"            # 閉じ目
    assert sp.CLAWD_FRAMES["happy"][1][0] == "O"            # 腕が上がる
    assert base[7][2] == "O" and sp.CLAWD_FRAMES["walkA"][7][4] == "."   # 脚の長短


def test_muse_sleep_z_is_light_blue():
    rows = sp.muse_rows("sleep")
    assert "E" not in "".join(r[9:] for r in rows[:3])
    assert "S" in "".join(r[9:] for r in rows[:3])
    assert rows[5] == sp.MUSE_FRAMES["sleep"][5]            # 目の E は残る
    assert sp.MUSE_FRAMES["sleep"][0].endswith("EE")        # 元データは書き換えない


def test_clawd_frame_cycles():
    assert sp.clawd_frame("watch", 0) == "base"
    assert sp.clawd_frame("watch", 3950) == "blink"
    assert sp.clawd_frame("watch", 3900) != "blink"         # 境界は含まない
    assert sp.clawd_frame("watch", 4200 + 3950) == "blink"  # 周期 4200
    assert sp.clawd_frame("watch", 1600) == "lookL"
    assert sp.clawd_frame("watch", 3400) == "lookR"
    assert sp.clawd_frame("work", 0) == "walkB" and sp.clawd_frame("work", 160) == "walkA"
    assert sp.clawd_frame("work", 320) == "walkB"
    assert sp.clawd_frame("wait", 0) == "waveB" and sp.clawd_frame("wait", 280) == "waveA"
    for a in ("alert", "happy", "sleep"):
        assert sp.clawd_frame(a, 12345) == a
    for a in ("watch", "work", "wait", "alert", "happy", "sleep"):
        for t in range(0, 20000, 37):
            assert sp.clawd_frame(a, t) in sp.CLAWD_FRAMES


def test_muse_frame_cycles():
    assert sp.muse_frame("work", 0) == "look-r" and sp.muse_frame("work", 499) == "look-r"
    assert sp.muse_frame("work", 500) == "work-a" and sp.muse_frame("work", 720) == "work-b"
    assert sp.muse_frame("work", 3000) == "look-r"          # 周期 3000
    assert sp.muse_frame("claude", 0) == "idle"
    assert sp.muse_frame("claude", 5000) == "blink"
    assert sp.muse_frame("claude", 2000) == "look-l" and sp.muse_frame("claude", 4500) == "look-r"
    assert sp.muse_frame("approval", 0) == "look-r" and sp.muse_frame("approval", 300) == "wait"
    assert sp.muse_frame("alert", 5) == "alert" and sp.muse_frame("done", 5) == "happy"
    assert sp.muse_frame("sleep", 5) == "sleep"
    for a in ("work", "claude", "approval", "alert", "done", "sleep"):
        for t in range(0, 20000, 37):
            assert sp.muse_frame(a, t) in sp.MUSE_FRAMES


def test_move_offset():
    assert sp.move_offset("", 100) == (0, 0)
    assert sp.move_offset("bob", 0) == (0, 0) and sp.move_offset("bob", 700) == (0, -2)
    assert sp.move_offset("fast", 200) == (0, -3)
    assert sp.move_offset("shake", 0) == (-2, 0) and sp.move_offset("shake", 100) == (2, 0)
    assert sp.move_offset("jump", 0) == (0, 0) and sp.move_offset("jump", 300) == (0, -14)
    assert sp.move_offset("sleep", 2000) == (0, 1)
    for m in list(sp.CLAWD_MOVE.values()) + list(sp.MUSE_MOVE.values()):
        assert m in ("", "bob", "fast", "shake", "jump", "sleep")
    for t in range(0, 3000, 7):
        assert -14 <= sp.move_offset("jump", t)[1] <= 0


def test_confetti():
    seen = 0
    for t in range(1000, 4000, 50):
        ps = sp.confetti_particles(t, 274)
        assert len(ps) <= 18
        for x, y, color, angle, alpha in ps:
            assert 0 <= alpha <= 1 and 6 <= y <= 6 + 10 + 78 and color.startswith("#")
            seen += 1
    assert seen > 0
    assert sp.confetti_particles(1234, 274) == sp.confetti_particles(1234, 274)   # 決定的


ALL_TESTS = [test_frame_sizes, test_clawd_shape, test_muse_sleep_z_is_light_blue,
             test_clawd_frame_cycles, test_muse_frame_cycles, test_move_offset,
             test_confetti]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
