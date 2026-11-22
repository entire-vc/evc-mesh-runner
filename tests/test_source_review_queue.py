"""An external review is runnable without handing its delivery card to the reviewer."""
import copy
import hashlib
import json
import sys
from unittest import mock

from fakes import AGENT_ID, FakeMesh, task, calls, write_replies
from test_runner import Bench
from mesh_runner.config import RepoSpec


def queued_job(source):
    job = dict(version=1, scope="external-source-refresh", purpose="delta-review", source_task_id=source["id"],
               project_id=source["project_id"], delivery_owner_id=source["assignee_id"],
               reviewer_id=AGENT_ID, reviewer_lane="builder", binding_sha256="b" * 64,
               contract_sha256="c" * 64, mr_url="https://git.example.test/g/external/-/merge_requests/8",
               head_sha="a" * 40, package_sha256="d" * 64, criteria_sha256="e" * 64,
               manifest_sha256="f" * 64, source_context_sha256="0" * 64,
               inputs={k: "/immutable/input/" + k for k in ("contract", "package", "files_root", "brief")})
    job["job_id"] = hashlib.sha256(json.dumps(job, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return job


class SourceReviewQueueTest(Bench):
    def runner(self, mesh, gitlab=None):
        runner = super().runner(mesh, gitlab)
        runner.refresh_me()
        return runner

    def enable_queue(self, jobs, *, check="ready", result="verified", **reply):
        path = self.root / "queue.py"
        path.write_text("import json,sys\nrequest=json.load(sys.stdin)\n" +
            "with open(" + repr(str(self.root / "helper-calls.jsonl")) + ", 'a') as f:\n" +
            " f.write(json.dumps(request)+'\\n')\n" +
            "if request['phase']=='list':\n print(" + repr(json.dumps(dict(version=1, jobs=jobs, **reply))) + ")\n" +
            "else:\n print(json.dumps(dict(version=1, job_id=request['unit']['job_id'], " +
            "status=" + repr(result) + " if request['phase']=='result' else " + repr(check) + ", note_id=51254)))\n")
        self.agent.source_review_command = [sys.executable, str(path)]
        self.agent.source_review_timeout_seconds = 1

    def test_verified_external_subject_enters_selected_reviewers_queue_with_original_owner(self):
        source = task(1, cat="in_progress", assignee_id="delivery-owner", human_gate=True,
                      description="Delivered code MR is already merged")
        own = task(2)
        mesh = FakeMesh([source, own])
        before = copy.deepcopy(mesh.tasks)
        job = queued_job(source)
        self.enable_queue([job])
        r = self.runner(mesh)
        got = r.candidates()
        self.assertEqual([t["id"] for t in got], [source["id"], own["id"]])
        self.assertEqual(got[0]["_mode"], "source-review")
        self.assertEqual(got[0]["_source_review"], job)
        self.assertEqual(mesh.tasks, before)
        self.assertEqual(mesh.created, [])

    def test_disabled_keeps_ordinary_queue(self):
        source, own = task(1, assignee_id="delivery-owner"), task(2)
        r = self.runner(FakeMesh([source, own]))
        self.assertEqual([t["id"] for t in r.candidates()], [own["id"]])

    def test_disabled_helper_is_not_called_by_candidates_or_plan(self):
        own = task(2)
        mesh = FakeMesh([own])
        r = self.runner(mesh)
        with mock.patch("mesh_runner.source_review.run_helper") as helper:
            self.assertEqual([t["id"] for t in r.candidates()], [own["id"]])
            self.assertEqual([t["task"] for t in r.plan()["tasks"]], [own["id"][:8]])
        helper.assert_not_called()
        self.assertEqual(mesh.checkouts, {})

    def test_unavailable_identity_skips_external_feed_and_keeps_ordinary_candidates(self):
        source, own = task(1, assignee_id="delivery-owner"), task(2)
        self.enable_queue([queued_job(source)])
        r = self.runner(FakeMesh([source, own]))
        for identity in (None, {}, {"id": ""}, {"id": None}):
            with self.subTest(identity=identity):
                r.me = identity
                self.assertEqual([t["id"] for t in r.candidates()], [own["id"]])
        del r.me
        self.assertEqual([t["id"] for t in r.candidates()], [own["id"]])
        self.assertFalse((self.root / "helper-calls.jsonl").exists())

    def test_changed_owner_drops_external_unit_and_preserves_ordinary_work(self):
        source, own = task(1, assignee_id="delivery-owner"), task(2)
        job = queued_job(source)
        source["assignee_id"] = "new-owner"
        self.enable_queue([job])
        r = self.runner(FakeMesh([source, own]))
        self.assertEqual([t["id"] for t in r.candidates()], [own["id"]])

    def test_author_cannot_run_own_external_unit(self):
        source = task(1)
        self.enable_queue([queued_job(source)])
        r = self.runner(FakeMesh([source]))
        self.assertEqual([t["_mode"] for t in r.candidates()], ["work"])

    def test_changed_subject_pins_cannot_enter_queue(self):
        source, own = task(1, assignee_id="delivery-owner"), task(2)
        job = queued_job(source)
        job["head_sha"] = "1" * 40
        self.enable_queue([job])
        r = self.runner(FakeMesh([source, own]))
        self.assertEqual([t["id"] for t in r.candidates()], [own["id"]])

    def test_source_now_terminal_cannot_be_fed(self):
        source, own = task(1, cat="done", assignee_id="delivery-owner"), task(2)
        self.enable_queue([queued_job(source)])
        r = self.runner(FakeMesh([source, own]))
        self.assertEqual([t["id"] for t in r.candidates()], [own["id"]])

    def test_completed_source_can_verify_existing_admission_without_reopening(self):
        from mesh_runner.source_review import job_id
        source = task(1, cat="done", assignee_id="delivery-owner", human_gate=True)
        unit = queued_job(source)
        unit.update(purpose="verify-admission", existing_note_id=51254)
        unit["job_id"] = job_id(unit)
        self.enable_queue([unit])
        mesh = FakeMesh([source])
        r = self.runner(mesh)
        self.assertEqual([t["id"] for t in r.candidates()], [source["id"]])
        self.assertTrue(r._start_one(r.candidates()[0]))
        a = self.finish_turn(r)
        self.assertEqual(a.state, "completed")
        self.assertEqual(mesh.tasks[source["id"]]["status_id"], source["status_id"])
        self.assertTrue(mesh.tasks[source["id"]]["human_gate"])

    def test_real_harness_runs_pinned_unit_once_without_taking_delivery_ownership(self):
        source = task(1, assignee_id="delivery-owner", human_gate=True)
        before = copy.deepcopy(source)
        unit = queued_job(source)
        self.enable_queue([unit])
        mesh = FakeMesh([source])
        r = self.runner(mesh)
        self.assertTrue(r._start_one(r.candidates()[0]))
        a = self.finish_turn(r)
        self.assertEqual(a.state, "completed")
        self.assertEqual(a.mode, "source-review")
        self.assertEqual(a.workspaces, [])
        self.assertEqual(mesh.tasks[source["id"]], before)
        self.assertEqual(mesh.created, [])
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertIn(unit["head_sha"], calls(self.calls)[0]["prompt"])
        self.assertEqual(r.candidates(), [])
        phases = [json.loads(line)["phase"] for line in (self.root / "helper-calls.jsonl").read_text().splitlines()]
        self.assertEqual(phases.count("result"), 1)
        self.assertGreaterEqual(phases.count("check"), 3)

    def test_failed_worker_or_unverified_result_is_not_replayed(self):
        source = task(1, assignee_id="delivery-owner")
        self.enable_queue([queued_job(source)], result="refused")
        r = self.runner(FakeMesh([source]))
        self.assertTrue(r._start_one(r.candidates()[0]))
        self.assertEqual(self.finish_turn(r).state, "failed")
        self.assertEqual(r.candidates(), [])
        self.assertEqual(len(calls(self.calls)), 1)

    def test_helper_refusal_never_launches_a_worker(self):
        source = task(1, assignee_id="delivery-owner")
        self.enable_queue([queued_job(source)], check="refused")
        r = self.runner(FakeMesh([source]))
        self.assertFalse(r._start_one(r.candidates()[0]))
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(r.candidates(), [])

    def test_worker_receives_exact_descriptor_and_helper_uses_own_lane_key(self):
        source = task(1, assignee_id="delivery-owner")
        unit = queued_job(source)
        self.enable_queue([unit])
        helper = self.root / "queue.py"
        helper.write_text(helper.read_text().replace(
            "else:\n", "else:\n import os\n assert os.environ['MESH_AGENT_KEY']=='k'\n" +
            " assert os.environ['MESH_RUNNER_LANE']=='builder'\n"))
        worker = self.root / "agent.py"
        capture = self.root / "worker-unit.json"
        worker.write_text("import os\nfrom pathlib import Path\nPath(" + repr(str(capture)) +
                          ").write_text(os.environ['MESH_RUNNER_SOURCE_REVIEW'])\n" + worker.read_text())
        r = self.runner(FakeMesh([source]))
        with mock.patch.dict("os.environ", {"MESH_AGENT_KEY": "wrong-ambient"}):
            self.assertTrue(r._start_one(r.candidates()[0]))
            self.assertEqual(self.finish_turn(r).state, "completed")
        self.assertEqual(json.loads(capture.read_text()), unit)

    def test_foreign_checkout_prevents_spawn_without_changing_delivery_card(self):
        source = task(1, assignee_id="delivery-owner")
        self.enable_queue([queued_job(source)])
        mesh = FakeMesh([source])
        mesh.refuse_checkout.add(source["id"])
        before = copy.deepcopy(mesh.tasks)
        r = self.runner(mesh)
        self.assertFalse(r._start_one(r.candidates()[0]))
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(mesh.tasks, before)

    def test_fresh_helper_refusal_after_checkout_releases_fence_without_spawning(self):
        source = task(1, assignee_id="delivery-owner")
        self.enable_queue([queued_job(source)])
        helper = self.root / "queue.py"
        helper.write_text(helper.read_text().replace(
            "else:\n", "else:\n assert open(" + repr(str(self.root / "helper-calls.jsonl")) +
            ").read().count('\\\"phase\\\": \\\"check\\\"') == 1\n"))
        mesh = FakeMesh([source])
        r = self.runner(mesh)
        self.assertFalse(r._start_one(r.candidates()[0]))
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(r.candidates(), [])

    def test_interrupted_unit_never_resumes_as_delivery_owner_turn(self):
        source = task(1, assignee_id="delivery-owner", human_gate=True)
        self.enable_queue([queued_job(source)])
        mesh = FakeMesh([source])
        r = self.runner(mesh)
        self.assertTrue(r._start_one(r.candidates()[0]))
        a = r.store.by_state(self.agent.name, ("running",))[0]
        r._kill(a)
        r.procs.pop(a.id, None)
        r._resume_one(a)
        self.assertEqual(r.store.get(a.id).state, "failed")
        self.assertEqual(r.candidates(), [])
        self.assertEqual(mesh.tasks[source["id"]], source)

    def test_crash_before_launch_intent_does_not_automatically_requeue_unit(self):
        source = task(1, assignee_id="delivery-owner")
        unit = queued_job(source)
        self.enable_queue([unit])
        r = self.runner(FakeMesh([source]))
        a = r.store.create(self.agent.name, source['id'], self.agent.harness,
                           'Interrupted external review', mode='source-review')
        r.store.update(a, wait={'source_review': unit})
        r._resume_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'failed')
        self.assertEqual(r.candidates(), [])

    def test_tick_starts_external_review_with_no_free_repository_slots(self):
        source = task(1, assignee_id="delivery-owner", human_gate=True)
        self.enable_queue([queued_job(source)])
        self.agent.repos[0] = RepoSpec("app", self.agent.repos[0].base, max_slots=0)
        r = self.runner(FakeMesh([source]))
        r.tick()
        active = r.store.by_state(self.agent.name, ("running",))
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].workspaces, [])
        self.assertEqual(self.finish_turn(r).state, "completed")

    def test_tick_preserves_delivery_owners_runtime_state(self):
        source = task(1, assignee_id="delivery-owner", human_gate=True)
        self.enable_queue([queued_job(source)])
        self.cfg.mesh_effects = True
        mesh = FakeMesh([source])
        owner_state = dict(kind="waiting", lane="delivery-owner", ref="existing-gate")
        mesh.runtime[source["id"]] = copy.deepcopy(owner_state)
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(mesh.runtime[source["id"]], owner_state)
        self.assertEqual(self.finish_turn(r).state, "completed")
        r.tick()
        self.assertEqual(mesh.runtime[source["id"]], owner_state)
        self.assertEqual(mesh.comment_log, {})

    def test_plan_reports_external_subject_without_claiming_delivery_task(self):
        source = task(1, assignee_id="delivery-owner", human_gate=True)
        self.enable_queue([queued_job(source)])
        mesh = FakeMesh([source])
        r = self.runner(mesh)
        plan = r.plan()
        self.assertEqual(plan["would_start"], 1)
        self.assertEqual(plan["tasks"][0]["mode"], "source-review")
        self.assertEqual(mesh.checkouts, {})
