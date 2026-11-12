"""`wait_for on=manual` (marker `⏳ WAIT manual:#<this-card>`): a lead-decided passive wait on a card
labelled `wake:manual`.

No observer exists for it, so it has no timer and no age re-check: only a comment by someone
else (or the owner's own NEXT) resumes the session. Without the label, or naming another card,
it is refused like any unreadable outcome, so it never becomes a silent park."""
import datetime as dt
import unittest

from fakes import FakeMesh, calls, task, wait_manual, write_replies
from test_runner import Bench

from mesh_runner.outcome import MANUAL_WAIT_GUIDANCE, TIME_WAIT_GUIDANCE, from_typed, parse_outcome
from mesh_runner.wake import check

NOW = dt.datetime(2026, 11, 10, 18, 0, tzinfo=dt.timezone.utc)
LONG_AGO = "2026-10-01T00:00:00+00:00"


def wait(**extra):
    return {"kind": "manual", "target": "abcdef12", "handle": "", "until": "",
            "since": LONG_AGO, "started": LONG_AGO, **extra}


def comment(author, body, minute=1):
    return {"id": f"c{minute}", "author_id": author, "author_name": author, "author_type": "agent",
            "body": body, "created_at": f"2026-11-10T17:0{minute}:00+00:00"}


class Parse(unittest.TestCase):
    def test_the_line_is_a_manual_wait_on_the_named_card(self):
        o = parse_outcome("⏳ WAIT manual:#abcdef12 — вход: новый attachment-артефакт на этой карточке", NOW)
        self.assertEqual((o.kind, o.target, o.is_wait), ("manual", "abcdef12", True))

    def test_a_malformed_manual_line_gets_its_own_guidance_not_the_time_one(self):
        for line in ("⏳ WAIT manual: someday", "⏳ WAIT manual:#xyz", "⏳ WAIT manual:abcdef12"):
            o = parse_outcome(line, NOW)
            self.assertEqual((o.kind, o.note), ("unparsed", MANUAL_WAIT_GUIDANCE), line)
            self.assertNotEqual(o.note, TIME_WAIT_GUIDANCE, line)


class Typed(unittest.TestCase):
    """The record Mesh writes for `wait_for on=manual` (evc-mesh typed_outcome_command_service.go): kind
    manual, no target, the card itself as task_id, the note required."""
    TASK = "abcdef12-0000-4000-8000-000000000000"

    def outcome(self, **wait):
        row = {"id": "o-1", "kind": "wait", "wait_id": "w-1", "task_id": self.TASK, "note": ""}
        return from_typed(row, {"id": "w-1", "kind": "manual", "task_id": self.TASK, **wait}, NOW, "abcdef12")

    def test_it_is_the_markers_manual_wait_on_this_card(self):
        o = self.outcome(note="вход: новый attachment-артефакт")
        self.assertEqual((o.kind, o.target, o.is_wait, o.note), ("manual", "abcdef12", True,
                                                               "вход: новый attachment-артефакт"))
        self.assertEqual(o.line, "⏳ WAIT manual:#abcdef12: вход: новый attachment-артефакт")
        self.assertEqual(o.typed["wait_kind"], "manual")

    def test_without_a_reason_or_naming_another_card_it_gets_the_manual_guidance(self):
        for wait in ({"note": ""}, {"note": "x", "target_task_id": "12345678-0000-4000-8000-000000000000"}):
            o = self.outcome(**wait)
            self.assertEqual((o.kind, o.note), ("unparsed", MANUAL_WAIT_GUIDANCE), wait)

    def test_the_guidance_names_the_turn_command_not_the_marker(self):
        self.assertIn("wait_for on=manual", MANUAL_WAIT_GUIDANCE)
        self.assertNotIn("⏳", MANUAL_WAIT_GUIDANCE)


class Wake(unittest.TestCase):
    def woken(self, w, comments, mode="all"):
        return check(None, None, None, {}, w, "me", comments, NOW, comment_wake=mode, agent_name="builder")

    def test_age_alone_never_wakes_it(self):
        w = wait()
        self.assertEqual(self.woken(w, []), "")
        self.assertFalse(w.get("recheck_due"))
        self.assertFalse(w.get("met"))

    def test_a_comment_by_someone_else_resumes_it(self):
        self.assertIn("commented", self.woken(wait(), [comment("lead", "артефакт: bridge v1 готов")]))

    def test_the_owners_own_next_supersedes_it(self):
        w = wait()
        self.assertIn("superseded", self.woken(w, [comment("me", "нашёл вход сам\n↻ NEXT")]))
        self.assertTrue(w.get("met"))

    def test_the_owners_own_note_does_not_wake_it(self):
        self.assertEqual(self.woken(wait(), [comment("me", "проверка: входа нет")]), "")


class Settlement(Bench):
    def line(self, t, target=None):
        return wait_manual("вход: новый attachment-артефакт")

    def test_a_labelled_card_sleeps_until_someone_else_writes(self):
        t = task(1, labels=["passive-wait", "wake:manual"])
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: self.line(t), t["id"][:8] + ":resume": self.line(t)})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")
        a = r.store.by_state(self.agent.name, ("waiting",))[0]
        self.assertEqual((a.wait.get("kind"), a.wait.get("target")), ("manual", t["id"][:8]))
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "in_progress")
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))
        for _ in range(3):
            r.tick()
            self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)            # nothing woke it
        mesh.add_comment(t["id"], "артефакт доставлен: bridge v1", author="lead")
        r.tick()
        self.settle_all(r)
        c = calls(self.calls)
        self.assertEqual([x["resume"] for x in c], [False, True])
        self.assertEqual(c[0]["sid"], c[1]["sid"])

    def test_without_the_label_it_is_refused_with_the_guidance(self):
        t = task(1, labels=["passive-wait"])
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: self.line(t)})
        r = self.runner(mesh)
        r.tick()
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome, a.wait), ("ready", 1, {}))
        self.assertIn("wake:manual", a.wake_text)

    def test_a_resumed_attempt_whose_label_was_removed_is_refused_not_parked(self):
        """Once the lead removes `wake:manual`, the same line no longer parks the card: the resumed turn is
        unreadable like any other (ready, counted, told why). The woken wait record stays, as for every kind."""
        t = task(1, labels=["passive-wait", "wake:manual"])
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: self.line(t), t["id"][:8] + ":resume": self.line(t)})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")
        mesh.tasks[t["id"]]["labels"] = ["passive-wait"]
        mesh.add_comment(t["id"], "метка снята, вход есть", author="lead")
        r.tick()                                              # woken -> resumed -> same line, now refused
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("ready", 1))
        self.assertIn("wake:manual", a.wake_text)
        self.assertEqual(r.store.by_state(self.agent.name, ("waiting",)), [])


if __name__ == "__main__":
    unittest.main()
