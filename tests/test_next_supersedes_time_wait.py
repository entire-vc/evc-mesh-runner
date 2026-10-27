"""A fresh ``↻ NEXT`` from the agent supersedes an older time WAIT (#2ecb5c65, E2 #cab1113e)."""
import datetime as dt
import unittest

from mesh_runner.wake import check

SINCE = "2026-10-25T12:45:00+00:00"
NOW = dt.datetime(2026, 10, 25, 20, 0, tzinfo=dt.timezone.utc)


def wait():
    return {"kind": "time", "since": SINCE, "started": SINCE, "target": "", "handle": "",
            "until": "2026-10-26T09:45:00+00:00"}


def own(body, minute):
    return {"id": f"c{minute}", "author_id": "me", "author_name": "me", "body": body,
            "created_at": f"2026-10-25T13:{minute:02d}:00+00:00"}


def woken(w, comments):
    return check(None, None, None, {}, w, "me", comments, NOW)


class NextSupersedesTimeWait(unittest.TestCase):
    def test_later_next_wakes_the_wait(self):
        w = wait()
        self.assertIn("superseded", woken(w, [own("did a thing\n↻ NEXT", 5)]))
        self.assertTrue(w["met"])

    def test_a_blocking_ask_to_a_person_is_not_superseded(self):
        w = {**wait(), "kind": "blocking", "handle": "pavel"}
        event = check(None, None, None, {"human_gate": True}, w, "me", [own("↻ NEXT", 5)], NOW)
        self.assertEqual(event, "")
        self.assertFalse(w.get("met"))

    def test_a_later_wait_after_the_next_stands(self):
        self.assertEqual(woken(wait(), [own("↻ NEXT", 5), own("⏳ WAIT 24h: poll", 9)]), "")

    def test_a_next_older_than_the_wait_is_ignored(self):
        old = own("↻ NEXT", 5)
        old["created_at"] = "2026-10-25T12:00:00+00:00"
        self.assertEqual(woken(wait(), [old]), "")

    def test_runner_notes_and_plain_comments_do_not_count(self):
        self.assertEqual(woken(wait(), [own("[runner] ↻ NEXT", 5), own("just a note", 6)]), "")


class NextSupersedesMergeWait(unittest.TestCase):
    def event(self, comments):
        from types import SimpleNamespace
        from mesh_runner import runner
        me = SimpleNamespace(mesh=SimpleNamespace(comments=lambda _id: comments), agent_id="me", gitlab=None)
        w = {"kind": "merge", "target": "g/app!5", "since": SINCE, "merge": {"sha": "a" * 40}}
        a = SimpleNamespace(task_id="t", id="x")
        return runner.AgentRunner._merge_event(me, a, w), w

    def test_next_wakes_a_merge_wait(self):
        event, w = self.event([own("↻ NEXT", 5)])
        self.assertIn("superseded", event)
        self.assertTrue(w["met"])

    def test_without_next_the_merge_wait_stands(self):
        self.assertEqual(self.event([own("note", 5)])[0], "")


if __name__ == "__main__":
    unittest.main()
