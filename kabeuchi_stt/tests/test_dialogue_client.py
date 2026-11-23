# -*- coding: utf-8 -*-
"""AI対話モード: 本体側のワーカー管理（プロトコル・タイムアウト・死活・再起動）。
サブプロセスは起こさない（spawn を差し替えたプロセスもどきで確かめる）。実機には触らない。
"""
import json
import queue
import threading
import time
import unittest

import _common as C  # noqa: F401
import dialogue_client as dc
import dialogue_deliver as dd
import dialogue_worker as dw
from test_dialogue_deliver import FakeOps, PROFILE, ROOT


class _Stdin:
    def __init__(self, proc):
        self.proc = proc
        self.buf = b""
        self.closed = False

    def write(self, data):
        if self.closed or self.proc.dead:
            raise BrokenPipeError("pipe closed")
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self.proc.on_request(line)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _Out:
    def __init__(self):
        self.q = queue.Queue()

    def readline(self):
        return self.q.get()          # EOF は b""


class FakeProc:
    """handler(req_dict, proc) -> 出力する行（bytes）のリスト。"die" ならプロセス終了、"hang" なら無応答。"""

    def __init__(self, handler):
        self.handler = handler
        self.dead = False
        self.killed = False
        self.requests = []
        self.stdin = _Stdin(self)
        self.stdout = _Out()
        self.stderr = _Out()

    def on_request(self, line):
        req = json.loads(line.decode("utf-8"))
        self.requests.append(req)
        out = self.handler(req, self)
        if out == "die":
            self.die()
        elif out == "hang" or out is None:
            pass
        else:
            for o in out:
                self.stdout.q.put(o)

    def die(self):
        self.dead = True
        self.stdout.q.put(b"")
        self.stderr.q.put(b"")

    def poll(self):
        return 1 if self.dead else None

    def kill(self):
        self.killed = True
        self.die()

    def wait(self, timeout=None):
        return 0


def ok(req, **result):
    return dd.encode_line({"id": req["id"], "ok": True, "result": result})


class Spawner:
    def __init__(self, *handlers):
        self.handlers = list(handlers)
        self.procs = []

    def __call__(self, cmd):
        h = self.handlers[min(len(self.procs), len(self.handlers) - 1)]
        p = FakeProc(h)
        p.cmd = cmd
        self.procs.append(p)
        return p


def echo(req, proc):
    return [ok(req, op=req["op"], echo=req.get("text"))]


def make(*handlers, **kw):
    sp = Spawner(*handlers or (echo,))
    kw.setdefault("grant", lambda: None)
    c = dc.WorkerClient(spawn=sp, **kw)
    return c, sp


class ProtocolTest(unittest.TestCase):
    def test_roundtrip_and_request_shape(self):
        c, sp = make()
        r = c.call("ping")
        self.assertEqual(r["op"], "ping")
        req = sp.procs[0].requests[0]
        self.assertEqual((req["id"], req["op"]), (1, "ping"))
        c.call("ping")
        self.assertEqual(sp.procs[0].requests[1]["id"], 2)
        self.assertEqual(len(sp.procs), 1)                       # 使い回す
        c.shutdown()

    def test_command_line(self):
        c, sp = make(worker_python="X:/py/python.exe")
        c.call("ping")
        cmd = sp.procs[0].cmd
        self.assertEqual(cmd[0], "X:/py/python.exe")
        self.assertTrue(cmd[-1].endswith("dialogue_worker.py"))
        self.assertNotIn("-I", cmd)                              # 同階層の import ができなくなる
        c.shutdown()

    def test_unknown_op_rejected_locally(self):
        c, _sp = make()
        with self.assertRaises(ValueError):
            c.call("rm_rf")

    def test_stale_and_foreign_responses_are_skipped(self):
        def h(req, proc):
            return [ok({"id": req["id"] - 1}, stale=True),                 # 遅れて届いた前の応答
                    dd.encode_line({"hello": "no id"}),
                    ok(req, fresh=True)]
        c, _sp = make(h)
        self.assertEqual(c.call("ping"), {"fresh": True})
        c.shutdown()

    def test_bad_json_lines_are_skipped_and_recorded(self):
        def h(req, proc):
            return [b"warning: something\n", b"\xff\xfe\n", ok(req, fine=1)]
        c, _sp = make(h)
        self.assertEqual(c.call("ping"), {"fine": 1})
        self.assertEqual(len(c.bad_lines), 2)
        c.shutdown()

    def test_error_response_raises_op_error_and_keeps_worker(self):
        def h(req, proc):
            return [dd.encode_line({"id": req["id"], "ok": False, "error": "RuntimeError: x"})]
        c, sp = make(h)
        with self.assertRaises(dc.WorkerOpError) as cm:
            c.call("ping")
        self.assertIn("RuntimeError", str(cm.exception))
        self.assertTrue(c.is_alive())
        self.assertEqual(len(sp.procs), 1)
        c.shutdown()

    def test_non_dict_result_is_wrapped(self):
        c, _sp = make(lambda req, p: [dd.encode_line({"id": req["id"], "ok": True, "result": 7})])
        self.assertEqual(c.call("ping"), {"value": 7})
        c.shutdown()


class FailureTest(unittest.TestCase):
    def test_timeout_kills_and_next_call_restarts(self):
        c, sp = make(lambda req, p: "hang", lambda req, p: [ok(req, back=True)])
        t0 = time.monotonic()
        with self.assertRaises(dc.WorkerTimeout):
            c.call("deliver", {"profile": PROFILE, "text": "x"}, timeout=0.3)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertTrue(sp.procs[0].killed)
        self.assertFalse(c.is_alive())
        self.assertEqual(c.call("ping"), {"back": True})          # 起こし直した
        self.assertEqual(c.starts, 2)
        c.shutdown()

    def test_late_reply_of_timed_out_request_is_ignored(self):
        """タイムアウトで殺しても、遅れて届いた応答が次の頼みの結果に混ざらない（id が違う）。"""
        c, sp = make(lambda req, p: "hang")
        with self.assertRaises(dc.WorkerTimeout):
            c.call("ping", timeout=0.2)
        c2, _sp2 = make(lambda req, p: [ok({"id": 1}, late=True), ok(req, now=True)])
        c2._next_id = 1
        self.assertEqual(c2.call("ping"), {"now": True})
        c2.shutdown()

    def test_idempotent_op_retries_once_after_death(self):
        c, sp = make(lambda req, p: "die", lambda req, p: [ok(req, second=True)])
        self.assertEqual(c.call("ping"), {"second": True})
        self.assertEqual(c.starts, 2)
        c.shutdown()

    def test_idempotent_op_gives_up_after_second_death(self):
        c, sp = make(lambda req, p: "die")
        with self.assertRaises(dc.WorkerDied):
            c.call("resolve", {"profile": PROFILE})
        self.assertEqual(len(sp.procs), 2)

    def test_deliver_is_not_retried_after_death(self):
        c, sp = make(lambda req, p: "die", lambda req, p: [ok(req, status="delivered")])
        with self.assertRaises(dc.WorkerDied):
            c.deliver(PROFILE, "x")
        self.assertEqual(len(sp.procs), 1)                        # 二重配達を避けて自動再試行しない
        self.assertEqual(c.deliver(PROFILE, "x")["status"], "delivered")   # 次の呼び出しで起こし直す
        self.assertEqual(c.starts, 2)
        c.shutdown()

    def test_write_to_dead_worker(self):
        c, sp = make(echo, echo)
        c.call("ping")
        sp.procs[0].dead = True
        self.assertFalse(c.is_alive())
        self.assertEqual(c.call("ping")["op"], "ping")            # 死んでいれば起こし直してから頼む
        self.assertEqual(len(sp.procs), 2)
        c.shutdown()

    def test_broken_pipe_on_write(self):
        c, sp = make(echo, echo)
        c.call("ping")
        sp.procs[0].stdin.closed = True                           # プロセスは生きているが書けない
        with self.assertRaises(dc.WorkerDied):
            c.call("deliver", {"profile": PROFILE, "text": "x"})
        self.assertTrue(sp.procs[0].killed)
        c.shutdown()

    def test_spawn_failure(self):
        def boom(cmd):
            raise FileNotFoundError("no python")
        c = dc.WorkerClient(spawn=boom, grant=lambda: None)
        with self.assertRaises(dc.WorkerDied):
            c.call("ping")

    def test_missing_python_is_reported_before_spawn(self):
        c = dc.WorkerClient(worker_python="X:/nope/python.exe", exists=lambda p: False, grant=lambda: None)
        with self.assertRaises(dc.WorkerDied) as cm:
            c.call("ping")
        self.assertIn("python", str(cm.exception))

    def test_ensure_alive(self):
        c, sp = make(echo, echo)
        self.assertTrue(c.ensure_alive())
        sp.procs[0].die()
        self.assertTrue(c.ensure_alive())                         # 死んでいたら起こし直す
        self.assertEqual(c.starts, 2)
        c.shutdown()
        self.assertFalse(c.ensure_alive())                        # 終了後は起こさない

    def test_stderr_is_collected(self):
        c, sp = make(echo)
        c.call("ping")
        sp.procs[0].stderr.q.put("診断メッセージ\n".encode("utf-8"))
        for _ in range(50):
            if c.stderr_tail:
                break
            time.sleep(0.02)
        self.assertEqual(list(c.stderr_tail), ["診断メッセージ"])
        c.shutdown()


class ShutdownTest(unittest.TestCase):
    def test_shutdown_sends_op_and_stops(self):
        seen = []

        def h(req, proc):
            seen.append(req["op"])
            return [ok(req, bye=True)]
        c, sp = make(h)
        c.call("ping")
        c.shutdown()
        self.assertEqual(seen, ["ping", "shutdown"])
        self.assertTrue(sp.procs[0].killed or sp.procs[0].stdin.closed)
        self.assertFalse(c.is_alive())
        c.shutdown()                                              # 二重でも安全
        with self.assertRaises(dc.WorkerError):
            c.call("ping")                                        # 勝手に起こし直さない
        c.start()
        self.assertEqual(c.call("ping"), {"bye": True})           # 明示すれば再開できる
        c.shutdown()

    def test_shutdown_when_never_started(self):
        c, sp = make()
        c.shutdown()
        self.assertEqual(sp.procs, [])

    def test_shutdown_with_hung_worker_kills(self):
        c, sp = make(lambda req, p: "hang", timeouts={"shutdown": 0.2})
        c.start()
        c.shutdown()
        self.assertTrue(sp.procs[0].killed)


class OpsTest(unittest.TestCase):
    def test_deliver_grants_foreground_then_uses_held_hwnd(self):
        events = []

        def h(req, proc):
            events.append(("req", req["op"], req.get("hwnd")))
            if req["op"] == "resolve":
                return [ok(req, status="ok", hwnd=4242)]
            return [ok(req, status="delivered")]
        c, _sp = make(h, grant=lambda: events.append(("grant",)))
        c.resolve(PROFILE)
        self.assertEqual(c.held_hwnd, 4242)
        c.deliver(PROFILE, "やあ", options={"existing_text": "abort"})
        self.assertEqual(events[-2:], [("grant",), ("req", "deliver", 4242)])
        req = _sp.procs[0].requests[-1]
        self.assertEqual((req["text"], req["options"]), ("やあ", {"existing_text": "abort"}))
        c.deliver(PROFILE, "やあ", grant_foreground=False)
        self.assertEqual(events.count(("grant",)), 1)
        c.shutdown()

    def test_grant_failure_does_not_block_deliver(self):
        def bad():
            raise RuntimeError("x")
        c, _sp = make(lambda req, p: [ok(req, status="delivered")], grant=bad)
        self.assertEqual(c.deliver(PROFILE, "x")["status"], "delivered")
        c.shutdown()

    def test_resolve_not_ok_clears_held(self):
        c, _sp = make(lambda req, p: [ok(req, status="ambiguous", hwnd=0)])
        c.held_hwnd = 5
        c.resolve(PROFILE)
        self.assertEqual(c.held_hwnd, 0)
        c.shutdown()

    def test_pick_timeout_has_margin_and_args(self):
        got = {}

        def h(req, proc):
            got.update(req)
            return [ok(req, status="picked")]
        c, _sp = make(h)
        c.pick("宛先", timeout=12, exclude_roots=[7])
        self.assertEqual((got["name"], got["timeout"], got["exclude_roots"]), ("宛先", 12.0, [7]))
        c.shutdown()

    def test_pick_is_not_cut_short_by_default_timeouts(self):
        def h(req, proc):
            time.sleep(0.4)
            return [ok(req, status="picked")]
        c, _sp = make(h, timeouts={"pick": 0.1})
        self.assertEqual(c.pick(timeout=1)["status"], "picked")
        c.shutdown()

    def test_cleanup(self):
        c, sp = make()
        self.assertEqual(c.cleanup(), {"released": True})          # 起きていなければ起こさない
        self.assertEqual(sp.procs, [])
        c.call("ping")
        c.held_hwnd = 5
        c.cleanup()
        self.assertEqual(c.held_hwnd, 0)
        c.shutdown()

    def test_concurrent_calls_are_serialized(self):
        active, peak = [0], [0]

        def h(req, proc):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            active[0] -= 1
            return [ok(req, n=req["id"])]
        c, _sp = make(h)
        results = []
        ts = [threading.Thread(target=lambda: results.append(c.call("ping")["n"])) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        self.assertEqual(sorted(results), [1, 2, 3, 4])
        self.assertEqual(peak[0], 1)
        c.shutdown()


class WorkerResolveTest(unittest.TestCase):
    def test_default_python_path(self):
        p = dc.default_worker_python(home="H")
        self.assertTrue(p.replace("\\", "/").endswith(".venvs/tray-tools/Scripts/python.exe"))
        self.assertTrue(p.startswith("H"))

    def test_resolve_configured_and_default(self):
        path, found = dc.resolve_worker_python("X:/custom/python.exe", exists=lambda p: p == "X:/custom/python.exe")
        self.assertEqual((path, found), ("X:/custom/python.exe", True))
        path, found = dc.resolve_worker_python("", exists=lambda p: False, home="H")
        self.assertEqual(path, dc.default_worker_python("H"))
        self.assertFalse(found)

    def test_allow_foreground_is_exposed(self):
        self.assertTrue(callable(dc.allow_foreground))           # 実際には呼ばない（実機操作の前段）


class LoopbackTest(unittest.TestCase):
    """クライアント ↔ WorkerService（フェイク ops）を JSON 行でつないだ通しの確認。"""

    def test_ping_resolve_deliver_shutdown(self):
        ops = FakeOps(uia_effective=False, front_works={"uia_set_focus"})
        svc = dw.WorkerService(ops)

        def handler(req, proc):
            resp = svc.handle(req)
            return [dd.encode_line(resp)]
        c, _sp = make(handler)
        self.assertTrue(c.ping()["pong"])
        r = c.deliver(PROFILE, "こんにちは\n二行目")
        self.assertEqual((r["status"], r["stage"]), ("delivered", "paste"))
        self.assertEqual(ops.text, "こんにちは\n二行目")
        self.assertEqual(r["window"], ROOT)
        c.shutdown()
        self.assertTrue(svc.stop)


if __name__ == "__main__":
    unittest.main()
