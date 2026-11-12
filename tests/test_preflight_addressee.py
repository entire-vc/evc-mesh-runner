"""A durable repository-access refusal reaches whoever grants access.

Before: one ``[runner] repository preflight: ...; next_actor=operator`` comment. ``operator`` is
nobody in Mesh and the mention path ignores ``[runner``-prefixed bodies, so the signal reached no
one. Now an access reason opens the comment with an ``@<contact>``
line, once per fingerprint, and the contact's runner files a request card from it.
"""
import datetime as dt
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from mesh_runner import config as config_mod
from mesh_runner import repo_access as ra
from mesh_runner import repo_requirements as rr
from mesh_runner.runner import addressed
from mesh_runner.workspace import RepoSpec
from fakes import FakeGitLab, FakeMesh, task, calls
from test_runner import Bench

BASE = "https://git.example"
AT = lambda: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()  # noqa: E731


def link(project, iid=7):
    return {"link_type": "pr", "url": f"{BASE}/{project}/-/merge_requests/{iid}"}


def access(rev, *projects):
    return {"revision": rev, "repositories": [{"provider": "gitlab", "host": "git.example", "project_path": p}
                                              for p in projects]}


class Addressee(Bench):
    def setUp(self):
        super().setUp()
        base = self.agent.repos[0].base
        deploy = RepoSpec("deploy", base, max_slots=2, project=ra.identity("gitlab", "git.example", "g/deploy"))
        self.cfg.host_repositories = [deploy]
        self.grant = None                                  # None = Mesh has no field (legacy)
        self.cfg.repo_access_contacts = ("Ops", "Lead")

    def setup_run(self, t):
        mesh = FakeMesh([t])
        mesh.links = {t["id"]: [link("g/deploy")]}
        plain = mesh.me
        mesh.me = lambda: {**plain(), **({"repository_access": self.grant} if self.grant is not None else {})}
        gl = FakeGitLab()
        gl.base_url = BASE
        return mesh, self.runner(mesh, gl)

    def notes(self, mesh, t):
        return [c["body"] for c in mesh.comments_all(t["id"]) if "repository preflight" in c["body"]]

    def starts(self):
        return [c for c in calls(self.calls) if not c["resume"]]

    def ticks(self, r, n=3):
        for _ in range(n):
            r._task_backoff.clear()
            r.tick()

    def assert_addressed(self, body, contact="Ops"):
        first = body.splitlines()[0]
        self.assertTrue(first.startswith(f"@{contact} "), first)
        self.assertFalse(body.startswith("[runner"))         # the mention path drops runner-prefixed bodies
        self.assertTrue(addressed(body, contact))
        self.assertIn(f"next_actor={contact}", body)
        self.assertNotIn("next_actor=operator", body)
        self.assertIn(f'"next_actor": "{contact}"', body)

    def delivered(self, body, contact="Ops"):
        """The contact's runner reading this comment as a mention files exactly one request card."""
        theirs = task(9, assignee_id="agent-lead")             # the refused lane's card, not the contact's
        mesh = FakeMesh([theirs])
        mesh.mention_rows = [{"comment_id": "c1", "task_id": theirs["id"], "author_id": "agent-lead",
                              "author_name": "lead", "comment_body": body, "extracted_at": AT(),
                              "mentioned_slug": contact}]
        r = self.runner(mesh)
        r.refresh_me()
        r.deliver_mentions()
        return [t for t in mesh.created if "mention-request" in (t.get("labels") or [])]

    # AC1 -----------------------------------------------------------------------------------------
    def test_no_membership_under_mesh_access_addresses_riker_once_with_the_full_action(self):
        self.grant = access(1, "g/app")
        t = task(1)
        mesh, r = self.setup_run(t)
        self.ticks(r)
        notes = self.notes(mesh, t)
        self.assertEqual(len(notes), 1)
        self.assert_addressed(notes[0])
        self.assertIn("no_membership", notes[0].splitlines()[0])
        self.assertIn("g/deploy", notes[0].splitlines()[0])
        self.assertIn("lane builder", notes[0].splitlines()[0])
        self.assertIn(rr.MESH_ACTIONS[rr.NO_MEMBERSHIP][1], notes[0])      # the full action
        self.ticks(r, 4)                                                    # same fingerprint
        self.assertEqual(len(self.notes(mesh, t)), 1)
        self.assertFalse(self.starts())
        made = self.delivered(notes[0])
        self.assertEqual(len(made), 1)
        self.assertIn("@Ops repository access for lane builder", made[0]["title"])
        self.assertIn(rr.MESH_ACTIONS[rr.NO_MEMBERSHIP][1], made[0]["description"])

    # AC2 -----------------------------------------------------------------------------------------
    def test_missing_clone_signals_once_and_a_new_revision_or_repository_once_more(self):
        self.cfg.host_repositories = []
        self.grant = access(1, "g/deploy", "g/pipe")
        t = task(1)
        mesh, r = self.setup_run(t)
        self.ticks(r)
        notes = self.notes(mesh, t)
        self.assertEqual(len(notes), 1)
        self.assert_addressed(notes[0])
        self.assertIn("missing_approved_clone g/deploy", notes[0].splitlines()[0])
        self.assertIn(rr.MESH_ACTIONS[rr.MISSING_CLONE][1], notes[0])
        # a new revision that still leaves the card blocked: one new signal, then quiet
        self.grant = access(2, "g/deploy", "g/pipe", "g/more")
        self.ticks(r)
        notes = self.notes(mesh, t)
        self.assertEqual(len(notes), 2)
        self.assert_addressed(notes[1])
        # another repository: one new signal, then quiet
        mesh.links[t["id"]] = [link("g/pipe")]
        t["updated_at"] = "2026-11-11T00:00:00Z"
        self.ticks(r)
        notes = self.notes(mesh, t)
        self.assertEqual(len(notes), 3)
        self.assert_addressed(notes[2])
        self.assertIn("missing_approved_clone g/pipe", notes[2].splitlines()[0])
        self.assertFalse(self.starts())

    def test_legacy_undeclared_and_no_membership_are_addressed(self):
        t = task(1)
        mesh, r = self.setup_run(t)                                         # no Mesh field: legacy
        with mock.patch.object(rr, "clone_project", lambda b, rem: "g/app"):
            self.ticks(r)
        notes = self.notes(mesh, t)
        self.assertEqual(len(notes), 1)
        self.assert_addressed(notes[0])
        self.assertIn("undeclared_repo g/deploy", notes[0].splitlines()[0])
        self.assertIn(rr.ACTIONS[rr.UNDECLARED][1], notes[0])

    # AC3 -----------------------------------------------------------------------------------------
    def test_runner_side_reasons_address_nobody(self):
        spec = RepoSpec("deploy", self.agent.repos[0].base)
        links = [link("g/deploy")]
        kw = dict(agent_repos=[spec], approved_repos=[spec], project_of=lambda s: "g/deploy")
        slot = rr.check(links, BASE, pool_full=lambda n: True, **kw)
        checkpoint = rr.check(links, BASE, checkpoint_open=True, **kw)
        checkout = rr.check(links, BASE, resuming=True, **kw)
        self.assertEqual((slot.reason, checkpoint.reason, checkout.reason),
                         (rr.NO_SLOT, rr.CHECKPOINT, rr.NO_CHECKOUT))
        self.assertFalse(slot.durable)                                       # no comment at all
        t = task(1)
        mesh, r = self.setup_run(t)
        r.refresh_me()
        for v in (checkpoint, checkout):
            with self.subTest(reason=v.reason):
                self.assertFalse(v.needs_access)
                self.assertNotIn("@", r._repo_preflight_key(v))
                self.assertEqual(v.text(addressee="Ops", lane="builder"), v.text())
                r._repo_preflight_note(t["id"], v)
        bodies = [c["body"] for c in mesh.comments_all(t["id"])]
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            self.assertTrue(body.startswith("[runner]"))
            self.assertFalse(addressed(body, "Ops"))
            self.assertIn("next_actor=runner", body)
            self.assertEqual(self.delivered(body), [])

    def test_an_access_blocker_is_not_hidden_behind_a_checkpoint(self):
        mine = RepoSpec("app", self.agent.repos[0].base)
        v = rr.check([link("g/app"), link("g/deploy", iid=8)], BASE, agent_repos=[mine],
                     approved_repos=[mine], project_of=lambda s: "g/app", checkpoint_open=True)
        self.assertEqual((v.reason, v.project), (rr.UNDECLARED, "g/deploy"))
        self.assertEqual(v.also, ((rr.CHECKPOINT, "g/app"),))
        self.assertTrue(v.needs_access)

    # delivery details ----------------------------------------------------------------------------
    def test_the_contact_lane_itself_addresses_the_next_contact(self):
        # a lane's own mention files no card (author == addressee): Ops's lane addresses Lead
        self.grant = access(1)
        t = task(1)
        mesh, r = self.setup_run(t)
        self.cfg.repo_access_contacts = ("Builder", "Lead")
        self.ticks(r)
        notes = self.notes(mesh, t)
        self.assertEqual(len(notes), 1)
        self.assert_addressed(notes[0], "Lead")
        self.assertFalse(addressed(notes[0], "Builder"))
        self.assertEqual(len(self.delivered(notes[0], "Lead")), 1)

    def test_no_contact_configured_keeps_the_plain_comment(self):
        self.cfg.repo_access_contacts = ()
        self.grant = access(1)
        t = task(1)
        mesh, r = self.setup_run(t)
        self.ticks(r)
        notes = self.notes(mesh, t)
        self.assertEqual(len(notes), 1)
        self.assertIn("next_actor=operator", notes[0])
        self.assertFalse(addressed(notes[0], "Ops"))

    def test_a_card_held_before_addressing_is_addressed_once(self):
        # a card refused by the previous release (bare fingerprint mark) gets the addressed signal once
        self.grant = access(1, "g/app")
        t = task(1)
        mesh, r = self.setup_run(t)
        r.refresh_me()
        v = rr.check(mesh.links[t["id"]], BASE, agent_repos=list(r.lane_repos),
                     approved_repos=[], access=r.access.current)
        r.store.mark(self.agent.name, t["id"], r.REPO_PREFLIGHT, v.fingerprint)
        mesh.add_comment(t["id"], "[runner] " + v.text())                   # the old, unaddressed comment
        self.ticks(r, 4)
        notes = self.notes(mesh, t)
        self.assertEqual(len(notes), 2)
        self.assertFalse(addressed(notes[0], "Ops"))
        self.assert_addressed(notes[1])
        self.assertFalse(self.starts())


class ContactsConfig(unittest.TestCase):
    def load(self, line):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "runner.toml"
        p.write_text(textwrap.dedent(f"""
            [runner]
            mesh_url = "http://mesh"
            state_dir = "{d.name}/state"
            {line}
            [[agents]]
            name = "a"
            key_env = "HOME"
            profile_dir = "{d.name}"
            """))
        return config_mod.load(p)

    def test_default_is_no_contact(self):
        self.assertEqual(self.load("").repo_access_contacts, ())

    def test_ordered_names_are_read(self):
        cfg = self.load('repo_access_contacts = ["Ops", "Lead"]')
        self.assertEqual(cfg.repo_access_contacts, ("Ops", "Lead"))

    def test_not_a_bare_name_is_refused(self):
        for bad in ('"Ops"', '["@Ops"]', '["two words"]', '[1]'):
            with self.subTest(bad=bad), self.assertRaises(config_mod.ConfigError):
                self.load(f"repo_access_contacts = {bad}")
