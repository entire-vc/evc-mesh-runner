"""`comment_wake = "addressed"`: a stranger agent's remark does not end a pipeline/MR/time/decision/card/merge wait."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from mesh_runner.config import ConfigError, RunnerConfig, load
from mesh_runner.store import iso
from mesh_runner.wake import check, is_fyi, mentions, wakes_on_comment
from fakes import CAT_TO_STATUS, FakeMesh, task
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

    def test_blocking_waits_keep_waking_on_every_comment(self):
        w = wait("blocking", handle="operator")
        self.assertIn("commented", woken(w, [comment()]))

    def test_card_wait_holds_a_foreign_agent_comment_but_wakes_on_a_mention_or_a_person(self):
        w = wait("card", target="deadbeef")
        self.assertFalse(wakes_on_comment(comment(), {"kind": "card"}, "builder"))
        self.assertIn("commented", woken(dict(w), [comment()], mode="all"))
        self.assertIn("commented", woken(dict(w), [comment(body="@builder is this yours?")]))
        self.assertIn("commented", woken(dict(w), [comment("operator", kind="user")]))
        self.assertIn("commented", woken(dict(w), [comment("operator", kind=None)]))

    def test_merge_wait_is_filtered_like_card_wait(self):
        w = {"kind": "merge"}
        self.assertFalse(wakes_on_comment(comment(), w, "builder"))
        self.assertTrue(wakes_on_comment(comment(body="@builder look"), w, "builder"))
        self.assertTrue(wakes_on_comment(comment("operator", kind="user"), w, "builder"))

    def test_an_always_from_author_with_an_fyi_lead_does_not_wake(self):
        fyi = ["FYI: the grant is the same", "**FYI** same grant", "[FYI] same grant", "fyi: same grant",
               "  \n> **FYI** same grant", "FYI из #307566d2 (CI auth contract): the token moved",
               "**Combined handback e7073fa8 (FYI, same grant).**\n\nbody text"]
        for kind in ("time", "pipeline", "mr", "decision", "card", "merge"):
            w = {"kind": kind, "handle": "lead"}
            for body in fyi:
                with self.subTest((kind, body)):
                    self.assertFalse(wakes_on_comment(comment("Coordinator", body), w, "builder", ("coordinator",)))

    def test_an_fyi_lead_with_a_mention_or_from_a_person_still_wakes(self):
        for kind in ("card", "merge", "pipeline"):
            w = {"kind": kind}
            self.assertTrue(wakes_on_comment(comment("Coordinator", "FYI @builder please check"), w, "builder",
                                             ("coordinator",)))
            self.assertTrue(wakes_on_comment(comment("operator", "FYI stop", kind="user"), w, "builder",
                                             ("coordinator",)))

    def test_an_always_from_author_without_an_fyi_lead_still_wakes(self):
        for kind in ("card", "merge", "time"):
            for body in ("please rebase on main", "I have no FYI for you, but rebase", "Summary (see FYI below)"):
                with self.subTest((kind, body)):
                    self.assertTrue(wakes_on_comment(comment("Coordinator", body), {"kind": kind}, "builder",
                                                     ("coordinator",)))

    def test_fyi_from_an_agent_outside_the_always_list_is_held_either_way(self):
        self.assertFalse(wakes_on_comment(comment("stranger", "FYI: note"), {"kind": "card"}, "builder",
                                          ("coordinator",)))

    def test_is_fyi_lead_shapes(self):
        for body in ("FYI", "fyi: x", "**FYI**", "[FYI] x", "## FYI\nx", "  FYI - x",
                     "**Handback (FYI, same grant).**", "# Note (fyi)\nbody"):
            self.assertTrue(is_fyi(body), body)
        for body in ("", "please look", "FYIs are great", "Handback\nsee (FYI) later", "Result (FYI) only in text",
                     "**Done.** FYI in the middle"):
            self.assertFalse(is_fyi(body), body)

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


class CardWaitWiring(RunnerWiring):
    def card_waiting(self, mode="addressed", author_type="agent", body="progress note", author="agent-lead"):
        self.cfg.comment_wake = mode
        self.n = getattr(self, "n", 0) + 1
        t, other = task(self.n), task(self.n + 100)
        mesh = FakeMesh([t, other])
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="waiting", wait={"kind": "card", "target": other["id"][:8], "handle": "", "until": "",
                                                 "since": iso(), "started": iso()})
        c = mesh.add_comment(t["id"], body, author=author,
                             at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5))
        c["author_type"] = author_type
        return r, a

    def test_a_cross_posted_note_does_not_end_a_card_wait(self):
        r, a = self.card_waiting(mode="all")
        self.assertEqual(self.state_after_check(r, a), "ready")
        r, a = self.card_waiting(body="Combined handback (FYI, same grant).")
        self.assertEqual(self.state_after_check(r, a), "waiting")

    def test_a_mention_or_a_person_ends_a_card_wait(self):
        r, a = self.card_waiting(body="@builder the card you wait for moved")
        self.assertEqual(self.state_after_check(r, a), "ready")
        r, a = self.card_waiting(author_type="user")
        self.assertEqual(self.state_after_check(r, a), "ready")

    def test_the_awaited_card_closing_still_ends_the_wait_with_a_held_comment_present(self):
        r, a = self.card_waiting()
        other = [t for t in r.mesh.tasks.values() if t["id"] != a.task_id][0]
        other["status_id"] = CAT_TO_STATUS["done"]
        r._wake_one(a)
        b = r.store.get(a.id)
        self.assertEqual(b.state, "ready")
        self.assertIn("is done", b.wake_text)


if __name__ == "__main__":
    unittest.main()
