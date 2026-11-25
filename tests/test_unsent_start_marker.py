"""A launch whose prompt never left is not a start: the event stream names it.

Production, 14-24.11: the log bridge turns every ``start``/``resume`` event into a fed start. A launch can
still end before its prompt is pasted (host session cap, the provider's usage-limit screen, a start-up
menu): ``prompt_unsent_reconciled`` puts the attempt back to ``ready`` and the next launch writes one more
``start``. The unsent event carried only the attempt and the *restored* turn count (``turn`` one below
the launch's), so nothing tied it to the launch it cancels and the metric counted both as starts.

Contract: ``start``/``resume`` (events.jsonl) and ``launch`` (store) carry ``delivery`` = the launch's
prompt token; the unsent end carries the same ``delivery``, ``launched_turn`` (the launch's ``turn``) and
``delivered: false``. A consumer drops the start whose ``delivery`` an unsent event names.
"""
import json
import os
import signal
import time

from fakes import FakeMesh, task, write_replies
from mesh_runner import interactive
from test_runner import Bench


class UnsentStartMarker(Bench):
    def rows(self):
        path = self.cfg.state_dir / "events.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []

    def launch_unsent(self):
        """A real launch, then the native process gone with the delivery journal still at ``reserved``:
        the exact prompt was never handed to the CLI."""
        t = task(1)
        write_replies(self.replies, {t["id"][:8]: "SLEEP:30:done"})
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.tick()
        a = r.store.by_state(self.agent.name, ("running",))[0]
        intent = a.wait["interactive_prompt_intent"]
        try:
            os.killpg(a.pid, signal.SIGKILL)        # the whole native group is gone
        except ProcessLookupError:
            pass
        proc = r.procs.get(a.id)
        if proc is not None:
            proc.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not r._turn_over(r.store.get(a.id)) and time.monotonic() < deadline:
            time.sleep(0.05)
        directory = interactive.agent_dir(self.cfg.state_dir, self.agent.name)
        interactive.write_delivery(directory, a.id, {"version": 1, "token": intent["token"],
                                                     "sha256": intent["sha256"], "phase": "reserved"})
        self.assertFalse(r._reconcile_interactive_prompt(r.store.get(a.id)))
        self.assertEqual(r.store.get(a.id).state, "ready")
        return r, a, intent

    def test_the_unsent_end_names_the_launch_it_cancels(self):
        r, a, intent = self.launch_unsent()
        rows = [x for x in self.rows() if x.get("attempt") == a.id]
        starts = [x for x in rows if x["kind"] in ("start", "resume")]
        unsent = [x for x in rows if x["kind"] == "prompt_unsent_reconciled"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(unsent), 1)
        # the launch and its unsent end share the prompt token: the bridge pairs them exactly
        self.assertEqual(starts[0].get("delivery"), intent["token"])
        self.assertEqual(unsent[0].get("delivery"), intent["token"])
        self.assertIs(unsent[0].get("delivered"), False)
        # the unsent row's own ``turn`` is the restored count; the launch's turn is named separately
        self.assertEqual(unsent[0].get("launched_turn"), starts[0]["turn"])
        self.assertEqual(unsent[0]["turn"], starts[0]["turn"] - 1)
        # the durable launch attribution carries the same token
        launches = [json.loads(d) for _, k, d in r.store.events(a.id) if k == "launch"]
        self.assertEqual([x.get("delivery") for x in launches], [intent["token"]])

    def test_the_relaunch_after_an_unsent_end_says_so(self):
        r, a, first = self.launch_unsent()
        r.tick()                                   # the kept prompt is admitted again
        launches = [json.loads(d) for _, k, d in r.store.events(a.id) if k == "launch"]
        self.assertEqual(len(launches), 2)
        self.assertEqual(launches[1]["restart_reason"], "prompt_unsent")
        self.assertNotEqual(launches[1]["delivery"], first["token"])
        relaunched = r.store.get(a.id)
        try:
            os.killpg(relaunched.pid, signal.SIGKILL)   # end the sleeping fake turn; nothing else to settle
        except ProcessLookupError:
            pass

    def test_a_resumed_retry_after_an_unsent_end_says_so_too(self):
        """The kept prompt of a turn with a bound session is retried as a resume: no restart, but still
        the retry of a launch that never sent its prompt."""
        t = task(2)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        a = r.store.create(self.agent.name, t["id"], "claude", t["title"])
        self.assertEqual(r._record_launch(a, True, "tok-1"), "")         # an ordinary resume
        r.store.event(a.id, "prompt_unsent_reconciled", "tok-1")
        r._record_launch(a, True, "tok-2")
        r._record_launch(a, True, "tok-3")                                # the retry was delivered
        launches = [json.loads(d) for _, k, d in r.store.events(a.id) if k == "launch"]
        self.assertEqual([(x["restart_reason"], x["delivery"]) for x in launches],
                         [("", "tok-1"), ("prompt_unsent", "tok-2"), ("", "tok-3")])
