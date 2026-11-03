"""Run in canonical Runner CI: real Store/CAS, files and transport state.

Only Mesh and the external harness use the project's existing test boundary.
No Store, controller method, source scanner or ledger function is mocked.
"""
import copy
import dataclasses
import datetime as dt
import json
from uuid import uuid4

from fakes import FakeMesh, task
from test_runner import Bench
from mesh_runner import native_result_resume as resume
from mesh_runner import native_result_wait as wait_adapter
from mesh_runner import native_result_rebind as rebind
from mesh_runner.native_result_ledger import Ledger, Refused, identity
from mesh_runner.native_result_source import ACK_MARKER
from mesh_runner.outcome import parse_line
from mesh_runner.store import Store


class NativeController(Bench):
    def repair(self):
        r, a, card, ledger, old_rid, value, old_receiver = self.context()
        r._wake_one(a)
        r.store.update(a, state="waiting", pid=0, checkout={}, wake_text="",
            wait=dict(a.wait, native_delivery_failed={"owner": r.agent_id, "reason": "test crash"}))
        card.update(checkout_generation=4, checked_out_by=None)
        receiver = dict(old_receiver, generation=4)
        rid = ledger.register(value["binding"], receiver)["registration"]
        request = {"version": 1, "request_id": str(uuid4()), "agent": a.agent,
                   "task": a.task_id, "attempt": a.id, "session": a.session_id,
                   "owner": r.agent_id, "generation": identity(a.wait),
                   "old_registration": old_rid, "registration": rid, "binding": value["binding"],
                   "authority_sha256": "d" * 64, "reason": "TEST explicit reviewed repair",
                   "created": int(dt.datetime.now(dt.timezone.utc).timestamp()),
                   "expires": int(dt.datetime.now(dt.timezone.utc).timestamp()) + 300}
        return r, a, card, ledger, old_rid, rid, request, receiver, value

    def test_explicit_rebind_commits_wait_and_audit_without_consuming_old_event(self):
        r, a, card, ledger, old_rid, rid, request, receiver, value = self.repair()
        before = copy.deepcopy(a.wait)
        self.assertEqual(r._native_rebind_request(request), "applied")
        restored = r.store.get(a.id)
        self.assertEqual(restored.state, "waiting")
        self.assertEqual(restored.wait["target"], rid)
        self.assertEqual(restored.wait["native_receiver"], receiver)
        self.assertNotIn("native_delivery_failed", restored.wait)
        self.assertNotIn("native_grant", restored.wait)
        audit = json.loads(r.store.mark(a.agent, a.task_id, "native_rebind:" + request["request_id"])[0])
        self.assertEqual(audit["previous_wait"], before)
        self.assertEqual(ledger.readback(old_rid)["cursor"], 0)
        self.assertEqual(r._native_rebind_request(request), "applied")
        # Fresh normal observation owns READY. The repair itself never launches.
        r._wake_one(restored)
        self.assertEqual(restored.state, "ready")
        self.assertEqual(restored.wait["native_packet"]["event"], identity(value))

    def test_rebind_spool_survives_controller_restart_and_receipt_retry(self):
        r, a, card, ledger, old_rid, rid, request, receiver, value = self.repair()
        result = rebind.submit(self.cfg.state_dir, request)
        self.assertIsNone(result["receipt"])
        r2 = self.runner(r.mesh)
        self.addCleanup(r2.store.db.close)
        r2.me = dict(r.me)
        from mesh_runner import workspace_requests
        workspace_requests.consume(self.cfg.state_dir, self.agent.name, r2._native_rebind_request,
                                   spool=rebind.SPOOL, check=rebind.validate)
        retry = rebind.submit(self.cfg.state_dir, dict(request, created=request["created"] + 1,
                                                     expires=request["expires"] + 1))
        self.assertEqual(retry["request"], request)
        self.assertEqual(retry["receipt"]["status"], "applied")
        self.assertEqual(r2.store.get(a.id).wait["target"], rid)
        self.assertEqual(ledger.readback(old_rid)["cursor"], 0)

    def test_rebind_fresh_mesh_revocation_and_changed_wait_do_not_clear_hold(self):
        r, a, card, ledger, old_rid, rid, request, receiver, value = self.repair()
        before = copy.deepcopy(a.wait)
        card["human_gate"] = True
        self.assertEqual(r._native_rebind_request(request), "rejected_native_rebind_fence")
        self.assertEqual(r.store.get(a.id).wait, before)
        card["human_gate"] = False
        r.store.update(a, wait=dict(a.wait, changed="new evidence"))
        self.assertEqual(r._native_rebind_request(request), "rejected_native_rebind_fence")
        self.assertIsNone(r.store.mark(a.agent, a.task_id, "native_rebind:" + request["request_id"]))

    def test_rebind_competing_real_store_cas_leaves_audit_and_wait_unchanged(self):
        r, a, card, ledger, old_rid, rid, request, receiver, value = self.repair()
        other = Store(self.root / "state" / "r.sqlite")
        self.addCleanup(other.db.close)
        # Validate a legitimate plan, then a separate real writer changes the WAIT.
        replacement, audit = rebind.plan(ledger, request, dataclasses.asdict(a), card, r.agent_id)
        competing = other.get(a.id)
        other.update(competing, wait=dict(competing.wait, changed="operator event"))
        self.assertFalse(r.store.update_with_marks(a,
            {"native_rebind:" + request["request_id"]: json.dumps(audit)},
            expect="waiting", expect_wait=a.wait, state="waiting", wait=replacement))
        self.assertEqual(other.get(a.id).wait["changed"], "operator event")
        self.assertIsNone(other.mark(a.agent, a.task_id, "native_rebind:" + request["request_id"]))
        self.assertEqual(ledger.readback(old_rid)["cursor"], 0)

    def context(self, publish=True):
        owner = str(uuid4())
        card = task(1, cat="in_progress", assignee_id=owner, assignee_type="agent")
        mesh = FakeMesh([card])
        runner = self.runner(mesh)
        self.addCleanup(runner.store.db.close)
        runner.refresh_me()
        runner.me["id"] = owner
        attempt = runner.store.create(self.agent.name, card["id"], "codex")
        sid = str(uuid4())
        card.update(checkout_session_id=attempt.id, checkout_generation=3, checked_out_by=owner)
        receiver = {"task": card["id"], "attempt": attempt.id, "session": sid, "generation": 3}
        ledger = Ledger.initialize(self.root / "native-outbox")
        self.cfg.native_result_dir = ledger.root
        binding = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "actual023",
                   "package_sha256": "a" * 64, "test": True}
        rid = ledger.register(binding, receiver)["registration"]
        value = {"binding": binding, "sequence": 1, "status": "result", "receipt_sha256": "b" * 64,
                 "owner": owner, "reason": ""}
        if publish:
            ledger.publish(value, binding["producer"], binding["task"])
        runner.store.update(attempt, state="waiting", session_id=sid, checkout={},
            wait={"kind": "native", "target": rid, "native_receiver": receiver})
        return runner, attempt, card, ledger, rid, value, receiver

    def test_strict_parser_and_existing_receipt_contract(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.assertEqual(parse_line("⏳ WAIT native:" + "a" * 64, now).kind, "native")
        for value in ("A" * 64, "a" * 63, "../ledger", "a" * 64 + " until=success"):
            self.assertEqual(parse_line("⏳ WAIT native:" + value, now).kind, "unparsed")
        self.assertEqual(parse_line("⏳ WAIT receipt:#1234abcd", now).kind, "receipt")

    def test_ready_prompt_mark_are_durable_but_cursor_is_not_consumed(self):
        r, a, card, ledger, rid, value, receiver = self.context()
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        self.assertIn(identity(value), a.wake_text)
        self.assertEqual(ledger.readback(rid)["cursor"], 0)
        self.assertEqual(r.store.mark(self.agent.name, card["id"], "native_delivery:" + rid)[0], identity(value))
        disk = Store(self.root / "state" / "r.sqlite")
        self.addCleanup(disk.db.close)
        restored = disk.get(a.id)
        self.assertEqual(restored.wait["native_packet"]["value"], value)
        self.assertEqual(restored.wake_text, a.wake_text)
        # A restart/re-arm of the already delivered event cannot create a turn.
        r.store.update(a, state="waiting", wake_text="")
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertEqual(ledger.delivery(rid, receiver)["event"], identity(value))

    def test_real_competing_store_cas_cannot_install_stale_wake_or_mark(self):
        r, a, card, ledger, rid, value, receiver = self.context()
        other = Store(self.root / "state" / "r.sqlite")
        self.addCleanup(other.db.close)
        fresh = other.get(a.id)
        other.update(fresh, wait=dict(fresh.wait, operator_repair="new fence"))
        r._wake_native(a, card)  # stale object; the real SQL CAS must refuse
        result = other.get(a.id)
        self.assertEqual(result.state, "waiting")
        self.assertEqual(result.wait["operator_repair"], "new fence")
        self.assertEqual(result.wake_text, "")
        self.assertIsNone(other.mark(self.agent.name, card["id"], "native_delivery:" + rid))
        self.assertEqual(ledger.readback(rid)["cursor"], 0)
        self.assertEqual(ledger.delivery(rid, receiver)["event"], identity(value))

    def test_own_comments_and_empty_outbox_cannot_wake_native_wait(self):
        r, a, card, ledger, rid, value, receiver = self.context(publish=False)
        r.mesh.add_comment(card["id"], "↻ NEXT", author=r.agent_id)
        r._wake_one(a)
        self.assertEqual(a.state, "waiting")
        self.assertIsNone(r.store.mark(self.agent.name, card["id"], "native_delivery:" + rid))

    def test_current_access_and_epoch_fail_closed_with_repair_grounds(self):
        r, a, card, ledger, rid, value, receiver = self.context()
        original = copy.deepcopy(card)
        for changes in ({"human_gate": True}, {"start_after": "2099-01-01T00:00:00Z"},
                        {"checkout_generation": 4}, {"checkout_session_id": str(uuid4())},
                        {"assignee_id": str(uuid4())}, {"checked_out_by": str(uuid4())}):
            with self.subTest(changes=changes):
                card.clear()
                card.update(original, **changes)
                r._wake_native(a, card)
                self.assertEqual(a.state, "waiting")
                self.assertEqual(a.wait["native_integration_blocked"]["owner"], r.agent_id)
                self.assertEqual(ledger.readback(rid)["cursor"], 0)
                self.assertIsNone(r.store.mark(self.agent.name, card["id"], "native_delivery:" + rid))
        card.clear()
        card.update(original)
        r._wake_native(a, card)
        self.assertEqual(a.state, "ready")
        self.assertNotIn("native_integration_blocked", a.wait)

    def grant(self):
        r, a, card, ledger, rid, value, receiver = self.context()
        r._wake_one(a)
        r.store.update(a, checkout={"generation": 4, "token": "test-boundary-token"})
        card.update(checkout_generation=4, checked_out_by=r.agent_id)
        home = self.root / "codex"
        root = home / "sessions"
        root.mkdir(parents=True)
        source = root / ("rollout-test-" + a.session_id + ".jsonl")
        source.write_text(json.dumps({"type": "session_meta", "payload": {"id": a.session_id}}) + "\n")
        prompt, grant = resume.prepare_prompt(ledger, dataclasses.asdict(a), card,
                                             r.agent_id, {"CODEX_HOME": str(home)}, a.wake_text)
        self.assertIn(ACK_MARKER, prompt)
        r.store.update(a, state="running", wait=dict(a.wait, native_grant=grant))
        return r, a, card, ledger, rid, grant, source

    @staticmethod
    def append_ack(source, ack, role="assistant"):
        row = {"type": "response_item", "payload": {"type": "message", "role": role,
               "content": [{"type": "output_text", "text": ACK_MARKER + json.dumps(ack)}]}}
        with source.open("a") as stream:
            stream.write(json.dumps(row) + "\n")

    def test_actual_receiver_source_ack_then_store_receipt_on_fresh_epoch(self):
        r, a, card, ledger, rid, grant, source = self.grant()
        self.append_ack(source, grant["ack"])
        self.assertTrue(r._settle_native(a, card))
        self.assertEqual(ledger.readback(rid)["cursor"], 1)
        receipt = json.loads(r.store.mark(self.agent.name, card["id"], "native_ack:" + rid)[0])
        self.assertEqual(receipt["registered_receiver"]["generation"], 3)
        self.assertEqual(receipt["resumed_receiver"]["generation"], 4)
        self.assertNotIn("native_grant", a.wait)

    def test_crash_between_ledger_ack_and_store_receipt_replays_same_source_ack(self):
        r, a, card, ledger, rid, grant, source = self.grant()
        self.append_ack(source, grant["ack"])
        # Real first durable commit, followed by reconstructing the controller.
        result = resume.consume(ledger, grant, dataclasses.asdict(a), card, r.agent_id)
        self.assertFalse(result["ack"]["duplicate"])
        r2 = self.runner(r.mesh)
        self.addCleanup(r2.store.db.close)
        r2.me = dict(r.me)
        restored = r2.store.get(a.id)
        self.assertTrue(r2._settle_native(restored, card))
        receipt = json.loads(r2.store.mark(self.agent.name, card["id"], "native_ack:" + rid)[0])
        self.assertTrue(receipt["ack"]["duplicate"])

    def test_user_frame_cannot_ack_and_held_delivery_cannot_retry_automatically(self):
        r, a, card, ledger, rid, grant, source = self.grant()
        self.append_ack(source, grant["ack"], role="user")
        self.assertFalse(r._settle_native(a, card))  # scan advances, no ACK
        self.assertFalse(r._settle_native(a, card))  # now EOF: explicit hold
        self.assertEqual(a.state, "waiting")
        self.assertIn("native_delivery_failed", a.wait)
        self.assertEqual(ledger.readback(rid)["cursor"], 0)
        self.assertEqual(a.checkout, {})
        r._wake_native(a, card)
        self.assertEqual(a.state, "waiting")
        self.assertIsNone(r.store.mark(self.agent.name, card["id"], "native_ack:" + rid))

    def test_revoked_access_at_settlement_cannot_consume_a_valid_ack(self):
        r, a, card, ledger, rid, grant, source = self.grant()
        self.append_ack(source, grant["ack"])
        card["human_gate"] = True
        self.assertFalse(r._settle_native(a, card))
        self.assertEqual(ledger.readback(rid)["cursor"], 0)
        self.assertEqual(a.state, "waiting")
        self.assertIn("native_delivery_failed", a.wait)

    def test_arm_captures_identity_before_normal_checkout_release(self):
        r, a, card, ledger, rid, value, receiver = self.context()
        r.store.update(a, state="running", checkout={"generation": 3, "token": "test-boundary-token"}, wait={})
        captured = wait_adapter.arm(ledger, rid, dataclasses.asdict(a), card, r.agent_id)
        r.store.update(a, wait={"kind": "native", "target": rid, "native_receiver": captured})
        r._release_checkout(a)
        self.assertEqual(a.checkout, {})
        # Recovery between release and WAIT settlement retains the exact fence.
        self.assertEqual(wait_adapter.arm(ledger, rid, dataclasses.asdict(a), card, r.agent_id), receiver)
        r.store.update(a, state="waiting")
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
