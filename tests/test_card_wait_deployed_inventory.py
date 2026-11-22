"""``wait_for on=card target_condition=deployed`` on a card whose merge request is in a repository the deploy
inventory lists without a live-commit source (``type: none``) is sent back once: Mesh never records that
deploy, so the typed edge would hold the waiter for good. Any other wait is unchanged."""
import json
import tempfile
from pathlib import Path

from fakes import FakeMesh, task, write_replies, wait_card
from mesh_runner import config as config_mod, deploy_inventory
from mesh_runner.mesh import MeshError
from test_card_wait_owned_edge import EDGE, OwnerCheckedMesh, server_edge
from test_runner import Bench
from test_typed_outcome import TypedTurn

INVENTORY = {"repos": {
    "g/ci-defs": {"branch": "main", "components": {"ci": {"source": {
        "type": "none", "note": "CI definitions are read from main when a pipeline starts"}}}},
    "g/app": {"branch": "main", "components": {"api": {"source": {"type": "http_text", "url": "https://x/.build"}}}},
}}


def mr(repo, iid, status="merged", **extra):
    return {"link_type": "pr", "status": status, "url": f"https://gitlab.example/{repo}/-/merge_requests/{iid}", **extra}


class DeployedWaitOnUnmarkedRepo(Bench):
    def flow(self, links, inventory=INVENTORY, x_status="in_progress"):
        t, x = task(1), task(2, status=x_status)
        if inventory is not None:
            path = self.root / "deploy-versions.json"
            path.write_text(inventory if isinstance(inventory, str) else json.dumps(inventory))
            self.cfg.deploy_inventory = path
        mesh = FakeMesh([t, x], max_concurrent=1)
        mesh.links = {x["id"]: links}
        r = self.runner(mesh)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        return r, t, x

    def turn(self, r, t, x, condition="deployed", first=True):
        line = wait_card(x["id"], condition=condition)
        write_replies(self.replies, {t["id"][:8]: line, t["id"][:8] + ":resume": line})
        r.start_new(1) if first else r.resume_ready(1)
        return self.finish_turn(r)

    @staticmethod
    def sent_back(a):
        return a.state == "ready" and "never becomes `deployed`" in a.wake_text

    def test_type_none_repo_is_sent_back_with_the_reason_and_the_way_out(self):
        r, t, x = self.flow([mr("g/ci-defs", 41)])
        with self.assertLogs("mesh_runner", level="INFO") as logs:
            a = self.turn(r, t, x)
        self.assertTrue(self.sent_back(a), (a.state, a.wake_text))
        self.assertIn("g/ci-defs has no live-commit source (type: none: CI definitions", a.wake_text)
        self.assertIn("g/ci-defs!41", a.wake_text)
        self.assertIn("wait_for on=mr project_path=g/ci-defs mr_iid=41 until=merged", a.wake_text)
        # its code is the merge request: never a plain card wait
        self.assertNotIn("wait_for on=card", a.wake_text)
        self.assertIn("stack", a.wake_text)
        line = next(m for m in logs.output if "until=deployed sent back" in m)
        self.assertIn(f"#{x['id'][:8]}", line)
        self.assertIn("never becomes `deployed`: g/ci-defs has no live-commit source", line)

    def test_closed_target_is_checked_too(self):
        # the live case: the target closed, the typed edge stays unmet
        r, t, x = self.flow([mr("g/ci-defs", 41)], x_status="done")
        self.assertTrue(self.sent_back(self.turn(r, t, x)))

    def test_told_once_then_the_same_wait_is_accepted(self):
        r, t, x = self.flow([mr("g/ci-defs", 41)])
        self.assertTrue(self.sent_back(self.turn(r, t, x)))
        a = self.turn(r, t, x, first=False)
        self.assertEqual(a.state, "waiting", a.wake_text)

    def test_plain_card_wait_on_the_same_target_is_accepted(self):
        # task_done: Mesh releases it when the card closes, the ordinary wake
        r, t, x = self.flow([mr("g/ci-defs", 41)])
        self.assertEqual(self.turn(r, t, x, condition="").state, "waiting")

    def test_other_typed_conditions_are_not_this_check(self):
        r, t, x = self.flow([mr("g/ci-defs", 41)])
        self.assertEqual(self.turn(r, t, x, condition="shipped").state, "waiting")

    def test_readable_repo_is_accepted(self):
        r, t, x = self.flow([mr("g/app", 7)])
        self.assertEqual(self.turn(r, t, x).state, "waiting")

    def test_repo_absent_from_the_inventory_is_accepted(self):
        r, t, x = self.flow([mr("g/elsewhere", 3)])
        self.assertEqual(self.turn(r, t, x).state, "waiting")

    def test_closed_or_already_recorded_link_does_not_hold_deployed(self):
        r, t, x = self.flow([mr("g/ci-defs", 40, status="closed"),
                             mr("g/ci-defs", 41, deployed_at="2026-11-12T16:00:00Z"), mr("g/app", 7)])
        self.assertEqual(self.turn(r, t, x).state, "waiting")

    def test_mixed_target_names_only_the_unmarkable_mr(self):
        r, t, x = self.flow([mr("g/app", 7), mr("g/ci-defs", 41, status="opened")])
        a = self.turn(r, t, x)
        self.assertTrue(self.sent_back(a))
        self.assertIn("No deploy is recorded for g/ci-defs!41,", a.wake_text)
        self.assertNotIn("g/app", a.wake_text)

    def test_without_an_inventory_nothing_changes(self):
        r, t, x = self.flow([mr("g/ci-defs", 41)], inventory=None)
        self.assertEqual(self.turn(r, t, x).state, "waiting")

    def test_unreadable_inventory_keeps_the_wait(self):
        r, t, x = self.flow([mr("g/ci-defs", 41)], inventory="{not json")
        self.assertEqual(self.turn(r, t, x).state, "waiting")

    def test_unreadable_links_keep_the_wait(self):
        r, t, x = self.flow([mr("g/ci-defs", 41)])
        def down(tid):
            from mesh_runner.mesh import MeshError
            raise MeshError(503, "down")
        r.mesh.vcs_links = down
        self.assertEqual(self.turn(r, t, x).state, "waiting")


class SentBackWaitLeavesNothing(TypedTurn):
    """The sent-back wait is never armed, so no wake ends it. Its Mesh wait is resolved as cancelled and
    the blocks edge Mesh made for it is removed with the owner-checked delete. Before, both stayed: an
    until=deployed edge on a closed target is never released, so the card kept reading as waiting on
    it. An edge the receipt does not give this wait stays."""

    def flow(self, *, owned=True, owner="w-1", links=None, delete_errors=()):
        source, target = task(1), task(3, status="done")
        path = self.root / "deploy-versions.json"
        path.write_text(json.dumps(INVENTORY))
        self.cfg.deploy_inventory = path
        mesh = OwnerCheckedMesh([source, target])
        mesh.links = {target["id"]: links or [mr("g/ci-defs", 41)]}
        r = self.run_turn(mesh, source)
        self.addCleanup(r.store.db.close)
        mesh.deps[source["id"]] = [server_edge(source, target, "deployed", owner)]
        self.typed(mesh, source, "wait", {"kind": "card", "target_task_id": target["id"],
                                          "target_condition": "deployed", "dependency_id": EDGE,
                                          "dependency_generation": 1, "dependency_owned": owned})
        mesh.wait_delete_errors = list(delete_errors)
        return r, self.finish_turn(r), mesh, source

    def outgoing(self, mesh, source):
        return [e["id"] for e in mesh.dependencies(source["id"])["outgoing"]]

    def test_owned_edge_is_removed_and_the_wait_cancelled(self):
        r, a, mesh, s = self.flow()
        self.assertEqual(a.state, "ready")
        self.assertIn("never becomes `deployed`", a.wake_text)
        self.assertEqual([(w, res) for _t, w, res, _c in getattr(mesh, "resolved", [])], [("w-1", "cancelled")])
        self.assertIn("never becomes `deployed`", mesh.resolved[0][3])
        self.assertEqual(mesh.wait_deletes, [(s["id"], EDGE, 1, "w-1")])
        self.assertEqual((mesh.plain_deletes, self.outgoing(mesh, s)), ([], []))

    def test_edge_of_another_wait_stays(self):
        r, a, mesh, s = self.flow(owned=False, owner="w-other")
        self.assertEqual(a.state, "ready")
        self.assertEqual([(w, res) for _t, w, res, _c in mesh.resolved], [("w-1", "cancelled")])
        self.assertEqual((mesh.wait_deletes, mesh.plain_deletes), ([], []))
        self.assertEqual(self.outgoing(mesh, s), [EDGE])

    def test_a_failed_delete_does_not_hold_the_send_back(self):
        with self.assertLogs("mesh_runner", level="WARNING") as logs:
            r, a, mesh, s = self.flow(delete_errors=[MeshError(503, "unavailable")])
        self.assertEqual(a.state, "ready")
        self.assertIn("never becomes `deployed`", a.wake_text)
        self.assertEqual(len(mesh.wait_deletes), 1)
        self.assertEqual(self.outgoing(mesh, s), [EDGE])
        self.assertTrue(any("unavailable" in m for m in logs.output), logs.output)

    def test_an_accepted_wait_keeps_its_edge_and_its_wait(self):
        # positive control: a readable repo arms the wait, which binds the edge as its own
        r, a, mesh, s = self.flow(links=[mr("g/app", 7)])
        self.assertEqual((a.state, a.wait.get("edge")), ("waiting", EDGE))
        self.assertEqual((getattr(mesh, "resolved", []), mesh.wait_deletes), ([], []))
        self.assertEqual(self.outgoing(mesh, s), [EDGE])


class InventoryRule(Bench):
    def test_never_deployed_mirrors_the_marker(self):
        repos = {
            "a": {"components": {"x": {"source": {"type": "none"}}}},
            "b": {"components": {"x": {"sources": [{"type": "http_text"}, {"type": "none", "note": "n"}]}}},
            "c": {"components": {"x": {}}},                                   # no source: does not read
            "d": {"components": {"x": {"source": {"type": "none"}}, "y": {"source": {"type": "ssh_file"}}}},
            "e": {"components": {}},
        }
        self.assertIn("a has no live-commit source (type: none)", deploy_inventory.never_deployed("a", repos))
        self.assertIn("(type: none: n)", deploy_inventory.never_deployed("b", repos))
        self.assertTrue(deploy_inventory.never_deployed("c", repos))
        self.assertEqual(deploy_inventory.never_deployed("d", repos), "")     # one readable component is enough
        self.assertTrue(deploy_inventory.never_deployed("e", repos))
        self.assertEqual(deploy_inventory.never_deployed("zzz", repos), "")   # not listed: no rule known

    def test_load_rejects_what_is_not_an_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "inv.json"
            self.assertIsNone(deploy_inventory.load(p))                        # missing
            p.write_text('{"repos": []}')
            self.assertIsNone(deploy_inventory.load(p))
            p.write_text(json.dumps(INVENTORY))
            self.assertEqual(set(deploy_inventory.load(p)), {"g/ci-defs", "g/app"})
        self.assertIsNone(deploy_inventory.load(None))

    def test_config_reads_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "runner.toml"
            cfg.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\ndeploy_inventory = "~/inv.json"\n' % tmp)
            self.assertEqual(config_mod.load(cfg).deploy_inventory, Path("~/inv.json").expanduser())
            cfg.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\n' % tmp)
            self.assertIsNone(config_mod.load(cfg).deploy_inventory)
