import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from mesh_runner import queue_order
from mesh_runner.effects import DECISION_REQUEST_LABEL
from mesh_runner.runner import AgentRunner


def task(i, prio="medium", created="2026-11-01T00:00:00Z", **kw):
    return {"id": i * 8 + "-0000-0000-0000-000000000000", "priority": prio, "created_at": created,
            "version": 1, "workspace_id": "w", "project_id": "p", "title": "SECRET TITLE", "description": "BODY",
            "labels": [], **kw}


class Helper:
    """A python helper script that records its stdin and does what ``mode`` says."""
    def __init__(self, tmp, mode):
        self.dir = Path(tmp)
        self.log = self.dir / "calls"
        self.path = self.dir / "h.py"
        self.path.write_text(f"""
import json, sys, time
raw = sys.stdin.read()
open({str(self.log)!r}, "a").write(raw + "\\n")
req = json.loads(raw)
ids = [c["id"] for c in req["candidates"]]
mode = {mode!r}
if mode == "reverse": print(json.dumps({{"version": 1, "order": ids[::-1], "mode": "x"}}))
elif mode == "exit": sys.exit(3)
elif mode == "flood":
    while True: sys.stdout.write("x" * 65536); sys.stdout.flush()
elif mode == "hang": time.sleep(30)
elif mode == "garbage": print("not json")
elif mode == "v2": print(json.dumps({{"version": 2, "order": ids[::-1]}}))
elif mode == "missing": print(json.dumps({{"version": 1, "order": ids[:-1]}}))
elif mode == "extra": print(json.dumps({{"version": 1, "order": ids + ["zzzz"]}}))
elif mode == "dup": print(json.dumps({{"version": 1, "order": [ids[0]] * len(ids)}}))
""")
        self.argv = [sys.executable, str(self.path)]

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


def runner(argv=None, waiting=(), **kw):
    r = AgentRunner.__new__(AgentRunner)
    r.agent = NS(name="A", queue_order_command=argv or [], queue_order_timeout_seconds=kw.get("timeout", 3.0),
                 queue_order_cache_seconds=kw.get("cache", 60.0))
    r.cfg = NS()
    r.store = NS(marked=lambda agent, kind: list(waiting))
    return r


class QueueOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = [task("a", "high"), task("b"), task("c", "low")]
        self.own = [t["id"] for t in self.work]

    def ids(self, r, work=None):
        return [t["id"] for t in r._work_order(work or self.work)]

    def test_off_is_byte_identical_and_spawns_nothing(self):
        h = Helper(self.tmp.name, "reverse")
        r = runner()                       # no key
        self.assertEqual(self.ids(r), self.own)
        self.assertFalse(hasattr(r, "_queue_order"))
        self.assertEqual(h.calls(), [])
        r2 = runner(argv=[])               # explicit empty
        self.assertEqual(self.ids(r2, list(reversed(self.work))), self.own)

    def test_on_applies_order_to_ordinary_class_only(self):
        h = Helper(self.tmp.name, "reverse")
        dec = task("d", "low", labels=[DECISION_REQUEST_LABEL])
        cap = task("e", "critical")
        r = runner(h.argv, waiting=[cap["id"]])
        got = self.ids(r, self.work + [dec, cap])
        # decision first, CAP_WAIT last, ordinary reversed
        self.assertEqual(got, [dec["id"]] + self.own[::-1] + [cap["id"]])
        sent = json.loads(h.calls()[0])
        self.assertEqual([c["id"] for c in sent["candidates"]], self.own)   # only ordinary, runner order

    def test_stdin_has_no_title_body_or_labels(self):
        h = Helper(self.tmp.name, "reverse")
        self.ids(runner(h.argv))
        raw = h.calls()[0]
        for bad in ("SECRET TITLE", "BODY", "labels", "title", "description"):
            self.assertNotIn(bad, raw)
        sent = json.loads(raw)
        self.assertEqual(sent["version"], 1)
        self.assertEqual(sent["agent"], "A")
        self.assertEqual(set(sent["candidates"][0]), set(queue_order.FIELDS))

    def test_single_ordinary_card_does_not_call(self):
        h = Helper(self.tmp.name, "reverse")
        r = runner(h.argv)
        self.assertEqual(self.ids(r, [self.work[0]]), [self.work[0]["id"]])
        dec = task("d", labels=[DECISION_REQUEST_LABEL])
        self.ids(r, [self.work[0], dec])
        self.assertEqual(h.calls(), [])

    def test_fallbacks_keep_own_order(self):
        for mode in ("exit", "garbage", "v2", "missing", "extra", "dup"):
            with self.subTest(mode=mode), self.assertLogs("mesh_runner", "WARNING") as cm:
                h = Helper(self.tmp.name, mode)
                self.assertEqual(self.ids(runner(h.argv)), self.own)
                self.assertEqual(len(cm.records), 1)

    def test_timeout_falls_back_and_kills_group(self):
        h = Helper(self.tmp.name, "hang")
        with self.assertLogs("mesh_runner", "WARNING") as cm:
            self.assertEqual(self.ids(runner(h.argv, timeout=0.5)), self.own)
        self.assertIn("timeout", cm.output[0])

    def test_flooding_helper_is_cut_at_the_cap(self):
        h = Helper(self.tmp.name, "flood")
        with self.assertLogs("mesh_runner", "WARNING") as cm:
            self.assertEqual(self.ids(runner(h.argv, timeout=10)), self.own)
        self.assertIn("reply too large", cm.output[0])

    def test_missing_binary_falls_back(self):
        with self.assertLogs("mesh_runner", "WARNING"):
            self.assertEqual(self.ids(runner(["/nonexistent/helper"])), self.own)

    def test_cache_hit_and_miss(self):
        h = Helper(self.tmp.name, "reverse")
        r = runner(h.argv)
        self.ids(r)
        self.ids(r)
        self.assertEqual(len(h.calls()), 1)                       # hit
        changed = [dict(self.work[0], version=2)] + self.work[1:]
        self.ids(r, changed)
        self.assertEqual(len(h.calls()), 2)                       # version changed: miss
        self.ids(r, self.work[:2])
        self.assertEqual(len(h.calls()), 3)                       # set changed: miss
        r2 = runner(h.argv, cache=0)
        self.ids(r2), self.ids(r2)
        self.assertEqual(len(h.calls()), 5)                       # ttl 0: always asks

    def test_fallback_logged_on_edge_and_recovery(self):
        h = Helper(self.tmp.name, "exit")
        r = runner(h.argv, cache=0)
        with self.assertLogs("mesh_runner", "INFO") as cm:
            self.ids(r), self.ids(r)
            h.path.write_text(h.path.read_text().replace("mode = 'exit'", "mode = 'reverse'"))
            self.ids(r)
        text = "\n".join(cm.output)
        self.assertEqual(text.count("falling back"), 1)
        self.assertEqual(text.count("recovered"), 1)
        self.assertIn("queue order: A first #cccccccc (own #aaaaaaaa)", text)

    def test_first_card_line(self):
        h = Helper(self.tmp.name, "reverse")
        with self.assertLogs("mesh_runner", "INFO") as cm:
            self.ids(runner(h.argv))
        self.assertEqual(cm.output[0].split(":", 2)[2].strip(), "queue order: A first #cccccccc (own #aaaaaaaa)")

    def test_reply_parser(self):
        ids = ["x", "y"]
        self.assertEqual(queue_order.parse_reply('{"version":1,"order":["y","x"]}', ids), ["y", "x"])
        for bad in ('[]', '{"version":true,"order":["x","y"]}', '{"version":1,"order":["x","x"]}',
                    '{"version":1,"order":["x",1]}', '{"version":1}'):
            with self.assertRaises(ValueError):
                queue_order.parse_reply(bad, ids)


class ConfigTest(unittest.TestCase):
    def test_validation(self):
        from mesh_runner import config
        self.assertEqual(config._qo_argv(None, "a"), [])
        self.assertEqual(config._qo_argv(["x", "y"], "a"), ["x", "y"])
        with self.assertRaises(ValueError):
            config._qo_argv("x y", "a")
        with self.assertRaises(ValueError):
            config._qo_seconds(0, "queue_order_timeout_seconds", "a")
        self.assertEqual(config._qo_seconds(0, "queue_order_cache_seconds", "a", zero=True), 0.0)


if __name__ == "__main__":
    unittest.main()
