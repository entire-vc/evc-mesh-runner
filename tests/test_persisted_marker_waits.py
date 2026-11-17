"""Waits declared by a marker line before typed-only outcomes keep working after the upgrade.

The wait rows below are copied from the live store (field names and shapes as written by the
marker-era runner; ids replaced). Nothing re-reads ``line`` to decide what is awaited: the
stored ``kind``/``target``/``gmail_reg`` drive the wake, so an upgrade neither drops nor
re-declares them.
"""
import copy
import datetime as dt
from unittest import mock

from fakes import FakeMesh, task
from mesh_runner import gmail_watch as gw
from mesh_runner.store import iso
from test_gmail_watch import MAILBOX, RESUME, SENDER, SRC, T0, THREAD, FakeReader, rec
from test_runner import Bench


def marker_card_wait(target8: str, target_id: str) -> dict:
    # shape of a live marker-era card wait: no card_condition, no mesh_wait_id, the line kept
    return {"kind": "card", "target": target8, "handle": "", "line": f"⏳ WAIT card:#{target8}",
            "until": "", "since": iso(), "started": iso(), "note": "", "fx": True, "errors": 0,
            "target_id": target_id}


def marker_gmail_wait(reg_id: str) -> dict:
    # shape of the live marker-era gmail wait: kind, target and gmail_reg, the line kept
    return {"kind": "gmail", "target": reg_id, "handle": "", "line": f"⏳ WAIT gmail:{reg_id}",
            "until": "", "since": iso(), "started": iso(), "note": "", "gmail_reg": reg_id, "fx": True}


class PersistedMarkerCardWait(Bench):
    def setUp(self):
        super().setUp()
        self.t, self.awaited = task(1), task(2)
        self.r = self.runner(FakeMesh([self.t, self.awaited]))
        self.addCleanup(self.r.store.db.close)
        self.r.refresh_me()
        self.a = self.r.store.create(self.agent.name, self.t["id"], "claude")
        self.wait = marker_card_wait(self.awaited["id"][:8], self.awaited["id"])
        self.r.store.update(self.a, state="waiting", wait=self.wait)

    def test_open_target_keeps_the_wait_as_stored(self):
        before = copy.deepcopy(self.r.store.get(self.a.id).wait)
        for _ in range(3):
            self.r._wake_one(self.r.store.get(self.a.id))
        after = self.r.store.get(self.a.id)
        self.assertEqual(after.state, "waiting", after.wake_text)
        self.assertEqual({k: after.wait.get(k) for k in ("kind", "target", "line")},
                         {k: before[k] for k in ("kind", "target", "line")})
        self.assertFalse(self.calls.exists())                  # no turn was started to re-declare it

    def test_closed_target_wakes_once(self):
        self.r.mesh.tasks[self.awaited["id"]]["status_id"] = "s-done"
        a = self.r.store.get(self.a.id)
        self.r._wake_one(a)
        b = self.r.store.get(self.a.id)
        self.assertEqual(b.state, "ready")
        self.assertIn("done", b.wake_text)
        self.r._wake_one(a)                                    # replay of the stale snapshot
        self.assertEqual(self.r.store.get(self.a.id).wake_text, b.wake_text)


class PersistedMarkerBlockingWait(Bench):
    """A ``❓ Blocking @person`` declared as text: the person's answer still wakes it."""
    def setUp(self):
        super().setUp()
        self.t = task(1)
        self.t["human_gate"] = True
        self.r = self.runner(FakeMesh([self.t]))
        self.addCleanup(self.r.store.db.close)
        self.r.refresh_me()
        self.a = self.r.store.create(self.agent.name, self.t["id"], "claude")
        self.r.store.update(self.a, state="waiting", wait={
            "kind": "blocking", "target": "", "handle": "owner", "line": "❓ Blocking @owner: log in?",
            "until": "", "since": iso(), "started": iso(), "note": "log in?", "fx": True, "rechecks": 0})

    def test_silence_keeps_waiting(self):
        self.r._wake_one(self.r.store.get(self.a.id))
        self.assertEqual(self.r.store.get(self.a.id).state, "waiting")

    def test_the_persons_answer_wakes(self):
        self.r.mesh.add_comment(self.t["id"], "done, logged in", author="owner",
                                at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        self.r._wake_one(self.r.store.get(self.a.id))
        b = self.r.store.get(self.a.id)
        self.assertEqual(b.state, "ready", b.wake_text)
        self.assertIn("owner commented: done, logged in", b.wake_text)


class PersistedMarkerGmailWait(Bench):
    def setUp(self):
        super().setUp()
        self.cfg.gmail_watch_v1 = True
        self.cfg.gmail_watch_reader = ("wrapper",)
        self.mesh_task = task(1)
        self.r = self.runner(FakeMesh([self.mesh_task]))
        self.addCleanup(self.r.store.db.close)
        self.r.refresh_me()
        self.reader = FakeReader([rec("m0000001", T0 - 5000)])
        self.r._gmail_reader_obj = self.reader
        self.reg = gw.register(self.r.store, self.reader, mailbox=MAILBOX, observer_task=self.mesh_task["id"],
                               resume_agent=RESUME, predicate=gw.build_predicate(from_addr=SENDER, thread=THREAD),
                               source_task=SRC)["registration_id"]
        self.a = self.r.store.create(self.agent.name, self.mesh_task["id"], "claude")
        self.r.store.update(self.a, state="waiting", turns=1, wait=marker_gmail_wait(self.reg))

    def scan_at(self, ahead_ms):
        import time as _t
        real = _t.time()
        with mock.patch("time.time", return_value=real + ahead_ms / 1000):
            self.r._scan_gmail()

    def test_no_mail_keeps_waiting(self):
        self.scan_at(200_000)
        self.r._wake_one(self.r.store.get(self.a.id))
        self.assertEqual(self.r.store.get(self.a.id).state, "waiting")

    def test_matching_mail_wakes_the_stored_registration_once(self):
        self.reader.records.append(rec("m0000003", T0 + 1000))
        self.scan_at(200_000)
        a = self.r.store.get(self.a.id)
        self.r._wake_one(a)
        b = self.r.store.get(self.a.id)
        self.assertEqual(b.state, "ready")
        self.assertIn("gmail:%s fired message_id=m0000003" % self.reg, b.wake_text)
        self.r._wake_one(a)
        self.assertEqual(self.r.store.db.execute(
            "SELECT COUNT(*) FROM gmail_outbox WHERE kind='wake' AND state='acked'").fetchone()[0], 1)


if __name__ == "__main__":       # pragma: no cover
    import unittest
    unittest.main()
