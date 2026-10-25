"""`comment_wake = "addressed"`: a stranger agent's remark does not end a pipeline/MR/time/decision wait."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from mesh_runner.config import ConfigError, RunnerConfig, load
from mesh_runner.store import iso
from mesh_runner.wake import check, mentions
from fakes import FakeMesh, task
from test_runner import Bench

SINCE = "2026-10-18T00:00:00+00:00"
NOW = dt.datetime(2026, 10, 18, 0, 5, tzinfo=dt.timezone.utc)


def wait(kind, **extra):
    base = {"kind": kind, "since": SINCE, "started": SINCE, "target": "", "handle": "", "until": ""}
    if kind == "time":
        base["until"] = "2026-10-19T00:00:00+00:00"
    if kind in ("pipeline", "mr"):
        base["target"] = "g/app#7" if kind == "pipeline" else "g/app!7"
    return {**base, **extra}


def comment(author="stranger", body="progress note", kind="agent", minute=1, **extra):
    return {"id": f"c{minute}", "author_id": author, "author_name": author, "author_type": kind, "body": body,
            "created_at": f"2026-10-18T00:0{minute}:00+00:00", **extra}


def woken(w, comments, mode="addressed", **kw):
    return check(None, None, None, {}, w, "me", comments, NOW, comment_wake=mode, agent_name="builder", **kw)


class AddressedFilter(unittest.TestCase):
    def test_an_unaddressed_agent_comment_is_held_back_only_in_addressed_mode(self):
        for kind in ("time", "pipeline", "mr", "decision"):
            with self.subTest(kind):
                w = wait(kind, handle="lead" if kind == "decision" else "")
                self.assertIn("commented", woken(dict(w), [comment()], mode="all"))
                self.assertEqual(woken(dict(w), [comment()], mode="addressed"), "")

    def test_a_person_always_wakes_and_so_does_an_unstated_author_kind(self):
        for kind in ("time", "pipeline", "mr", "decision"):
            for author_kind in ("user", None):
                with self.subTest((kind, author_kind)):
                    c = comment("operator", kind=author_kind)
                    if author_kind is None:
                        del c["author_type"]
                    self.assertIn("commented", woken(wait(kind, handle="lead"), [c]))

    def test_an_at_mention_of_the_waiting_agent_wakes(self):
        for kind in ("time", "pipeline", "mr", "decision"):
            for body in ("@builder please look", "ping (@Builder), the base moved", "see this\n\nthanks @builder."):
                with self.subTest((kind, body)):
                    self.assertIn("commented", woken(wait(kind, handle="lead"), [comment(body=body)]))

    def test_other_handles_and_lookalikes_do_not_count_as_a_mention(self):
        self.assertTrue(mentions("hi @builder", "builder"))
        self.assertTrue(mentions("hi @Builder.", "builder"))
        for body in ("@builder-2 look", "mail me@builder.dev", "@lead @reviewer", "builder without at"):
            self.assertFalse(mentions(body, "builder"), body)
        self.assertEqual(woken(wait("time"), [comment(body="@builder-2 look")]), "")

    def test_the_addressee_of_a_decision_wait_always_wakes_and_makes_it_met(self):
        w = wait("decision", handle="lead")
        self.assertIn("commented", woken(w, [comment("lead", "ok, go")]))
        self.assertTrue(w.get("met"))
        w = wait("decision", handle="lead", recipient_id="agent-lead")
        self.assertIn("commented", woken(w, [comment("someone-else", "ok", author_id="agent-lead")]))

    def test_an_explicit_answer_marker_wakes_a_decision_wait_from_anyone(self):
        w = wait("decision", handle="lead")
        self.assertIn("commented", woken(w, [comment("bystander", "Decision: go")]))
        self.assertTrue(w.get("met"))
        self.assertEqual(woken(wait("pipeline"), [comment("bystander", "Decision: go")]), "")

    def test_a_bystander_on_a_decision_wait_is_held_back_and_does_not_make_it_met(self):
        w = wait("decision", handle="lead")
        self.assertEqual(woken(w, [comment("bystander", "fyi")]), "")
        self.assertFalse(w.get("met"))

    def test_an_author_on_the_always_list_wakes(self):
        for kind in ("time", "pipeline", "mr", "decision"):
            w = wait(kind, handle="lead")
            self.assertEqual(woken(dict(w), [comment("Coordinator")]), "")
            self.assertIn("commented", woken(dict(w), [comment("Coordinator")],
                                             comment_wake_always_from=("coordinator",)))

    def test_card_and_blocking_waits_keep_waking_on_every_comment(self):
        for kind in ("card", "blocking"):
            with self.subTest(kind):
                w = wait(kind, target="deadbeef" if kind == "card" else "", handle="operator" if kind == "blocking" else "")
                self.assertIn("commented", woken(w, [comment()]))

    def test_a_held_back_comment_does_not_hide_a_later_wakeful_one(self):
        comments = [comment("bystander", "fyi", minute=1), comment("operator", "stop", kind="user", minute=2),
                    comment("bystander", "more notes", minute=3)]
        out = woken(wait("pipeline"), comments)
        self.assertIn("operator commented: stop", out)

    def test_a_system_note_never_wakes_in_either_mode(self):
        self.assertEqual(woken(wait("time"), [comment("sys", kind="system", auto_summary=True)], mode="all"), "")

    def test_the_default_is_the_old_behaviour(self):
        out = check(None, None, None, {}, wait("time"), "me", [comment()], NOW)
        self.assertIn("commented", out)


class Config(unittest.TestCase):
    def read(self, extra):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "runner.toml"
            p.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\n%s\n' % (tmp, extra))
            return load(p)

    def test_defaults_and_values(self):
        d = RunnerConfig(mesh_url="x", state_dir=Path("/tmp"), agents=[])
        self.assertEqual((d.comment_wake, d.comment_wake_always_from), ("all", []))
        self.assertEqual(self.read("").comment_wake, "all")
        c = self.read('comment_wake = "addressed"\ncomment_wake_always_from = [" Coordinator ", "Lead"]')
        self.assertEqual((c.comment_wake, c.comment_wake_always_from), ("addressed", ["Coordinator", "Lead"]))

    def test_bad_values_are_refused(self):
        for extra in ('comment_wake = "mentions"', 'comment_wake = true', 'comment_wake_always_from = "lead"',
                      'comment_wake_always_from = [1]', 'comment_wake_always_from = [""]'):
            with self.subTest(extra), self.assertRaises(ConfigError):
                self.read(extra)


class RunnerWiring(Bench):
    def waiting(self, kind="time", mode="addressed", author_type="agent", body="progress note"):
        self.cfg.comment_wake = mode
        self.n = getattr(self, "n", 0) + 1
        t = task(self.n)
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5)).isoformat()
        r.store.update(a, state="waiting", wait={"kind": kind, "target": "", "handle": "", "until": until,
                                                 "since": iso(), "started": iso()})
        c = mesh.add_comment(t["id"], body, author="agent-lead",
                             at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5))
        c["author_type"] = author_type
        return r, a

    def state_after_check(self, r, a):
        r._wake_one(a)
        return r.store.get(a.id).state

    def test_the_configured_mode_reaches_the_wake_check(self):
        r, a = self.waiting(mode="all")
        self.assertEqual(self.state_after_check(r, a), "ready")
        r, a = self.waiting(mode="addressed")
        self.assertEqual(self.state_after_check(r, a), "waiting")

    def test_a_person_or_a_mention_still_wakes_in_addressed_mode(self):
        r, a = self.waiting(author_type="user")
        self.assertEqual(self.state_after_check(r, a), "ready")
        r, a = self.waiting(body="@builder the base moved")
        self.assertEqual(self.state_after_check(r, a), "ready")
        self.assertIn("the base moved", r.store.get(a.id).wake_text)


if __name__ == "__main__":
    unittest.main()
