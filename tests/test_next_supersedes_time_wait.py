"""A ``↻ NEXT`` text in the agent's own comment no longer supersedes a time or merge wait: the runner reads no
marker lines (only a typed continue_next ends a wait)."""
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


class MarkerNextIgnoredOnTimeWait(unittest.TestCase):
    def test_own_next_marker_comment_does_not_wake_the_wait(self):
        w = wait()
        self.assertEqual(woken(w, [own("did a thing\n↻ NEXT", 5)]), "")
        self.assertFalse(w.get("met"))

    def test_plain_comment_does_not_wake_it_either(self):
        self.assertEqual(woken(wait(), [own("just a note", 6)]), "")


class MarkerNextIgnoredOnMergeWait(unittest.TestCase):
    def event(self, comments):
        from types import SimpleNamespace
        from mesh_runner import runner
        me = SimpleNamespace(mesh=SimpleNamespace(comments=lambda _id: comments), agent_id="me", gitlab=None)
        w = {"kind": "merge", "target": "g/app!5", "since": SINCE, "merge": {"sha": "a" * 40}}
        a = SimpleNamespace(task_id="t", id="x")
        return runner.AgentRunner._merge_event(me, a, w), w

    def test_next_marker_comment_does_not_wake_a_merge_wait(self):
        event, w = self.event([own("↻ NEXT", 5)])
        self.assertEqual(event, "")
        self.assertFalse(w.get("met"))

    def test_without_next_the_merge_wait_stands(self):
        self.assertEqual(self.event([own("note", 5)])[0], "")


if __name__ == "__main__":
    unittest.main()
