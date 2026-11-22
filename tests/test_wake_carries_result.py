"""Ф1: a session woken on a settled pipeline or merge request gets the result the runner already read
(sha, link, failed jobs, the end of the first failed job's log) instead of fetching it again."""
import unittest

from fakes import FakeGitLab, FakeMesh, calls, task, wait_mr, wait_pipeline, write_replies
from test_runner import Loop

from mesh_runner.wake import _trace_tail, ci_result_detail

SHA = "a" * 40
MERGE_SHA = "b" * 40


class ReadingGitLab(FakeGitLab):
    """FakeGitLab plus the three reads the wake detail uses."""
    def __init__(self):
        super().__init__()
        self.traces, self.bridges, self.sha_pipelines, self.reads = {}, {}, {}, []

    def job_trace_tail(self, target, job_id):
        self.reads.append(("trace", job_id))
        return self.traces.get(job_id, "")

    def pipeline_bridges(self, target):
        return list(self.bridges.get(target, []))

    def pipeline_of_sha(self, project, sha):
        return self.sha_pipelines.get((project, sha))


class WakeCarriesResult(Loop):
    def test_a_red_pipeline_wake_names_the_failed_job_and_its_log_end(self):
        t = task(1)
        mesh, gl = FakeMesh([t], max_concurrent=1), ReadingGitLab()
        gl.pipelines["g/app#5"] = "running"
        gl.pipeline_rows["g/app#5"] = {"id": 5, "status": "failed", "sha": SHA, "ref": "feat/x",
                                       "web_url": "https://git.example/g/app/-/pipelines/5"}
        gl.jobs["g/app#5"] = [
            {"id": 41, "name": "unit", "stage": "test", "status": "failed", "failure_reason": "script_failure"},
            {"id": 42, "name": "lint", "stage": "test", "status": "failed", "allow_failure": True},
            {"id": 43, "name": "build", "stage": "build", "status": "success"}]
        gl.traces[41] = ("section_start:1700000000:step_script\r\x1b[0K\x1b[32;1m$ pytest\x1b[0;m\n"
                         + "".join(f"line {i}\n" for i in range(40))
                         + "FAILED tests/test_x.py::test_y - AssertionError\nsection_end:1700000001:step_script\r\x1b[0K\n")
        write_replies(self.replies, {t["id"][:8]: wait_pipeline("g/app", 5),
                                     t["id"][:8] + ":resume": wait_pipeline("g/app", 6)})
        r = self.runner(mesh, gl)
        r.tick()
        self.settle_all(r)
        gl.pipelines["g/app#5"] = "failed"
        r.tick()
        self.settle_all(r)
        prompt = calls(self.calls)[1]["prompt"]
        self.assertIn("The runner already read the result", prompt)
        self.assertIn(f"pipeline g/app#5: failed, sha {SHA[:12]}, ref feat/x, https://git.example/g/app/-/pipelines/5", prompt)
        self.assertIn("failed jobs: unit (stage test, script_failure, job 41)", prompt)
        self.assertIn("failed but allowed to fail: lint", prompt)
        self.assertIn("FAILED tests/test_x.py::test_y - AssertionError", prompt)
        self.assertNotIn("line 10\n", prompt)                  # only the end of the log
        self.assertNotIn("\x1b", prompt)
        self.assertNotIn("section_start", prompt)
        self.assertEqual(gl.reads, [("trace", 41)])           # one log read, the first failed job

    def test_a_merged_mr_wake_carries_the_merge_commit_and_its_pipeline(self):
        t = task(1)
        mesh, gl = FakeMesh([t], max_concurrent=1), ReadingGitLab()
        gl.mr_head["g/app!7"] = "9:running"
        write_replies(self.replies, {t["id"][:8]: wait_mr("g/app", 7),
                                     t["id"][:8] + ":resume": wait_mr("g/app", 8)})
        r = self.runner(mesh, gl)
        r.tick()
        self.settle_all(r)
        gl.mr_state["g/app!7"] = "merged"
        gl.mr_rows["g/app!7"] = {"state": "merged", "sha": SHA, "merge_commit_sha": MERGE_SHA,
                                 "target_branch": "main", "web_url": "https://git.example/g/app/-/merge_requests/7"}
        gl.sha_pipelines[("g/app", MERGE_SHA)] = {"id": 77, "status": "running",
                                                  "web_url": "https://git.example/g/app/-/pipelines/77"}
        r.tick()
        self.settle_all(r)
        prompt = calls(self.calls)[1]["prompt"]
        self.assertIn(f"MR g/app!7: merged, head {SHA[:12]}, https://git.example/g/app/-/merge_requests/7", prompt)
        self.assertIn(f"merged as {MERGE_SHA[:12]} into main", prompt)
        self.assertIn("pipeline on the merge commit: 77 running, https://git.example/g/app/-/pipelines/77", prompt)

    def test_an_unreadable_result_still_wakes_with_the_bare_event(self):
        t = task(1)
        mesh, gl = FakeMesh([t], max_concurrent=1), ReadingGitLab()
        gl.pipelines["g/app#5"] = "running"
        write_replies(self.replies, {t["id"][:8]: wait_pipeline("g/app", 5),
                                     t["id"][:8] + ":resume": wait_pipeline("g/app", 6)})
        r = self.runner(mesh, gl)
        r.tick()
        self.settle_all(r)
        gl.pipelines["g/app#5"] = "success"
        gl.pipeline_jobs = lambda target: (_ for _ in ()).throw(TimeoutError("slow"))
        r.tick()
        self.settle_all(r)
        prompt = calls(self.calls)[1]["prompt"]
        self.assertIn("What changed:", prompt)
        self.assertNotIn("The runner already read the result", prompt)


class Detail(unittest.TestCase):
    def test_green_pipeline_counts_jobs_and_names_downstream(self):
        gl = ReadingGitLab()
        gl.pipeline_rows["g/app#5"] = {"id": 5, "status": "success", "sha": SHA, "ref": "main"}
        gl.jobs["g/app#5"] = [{"id": 1, "status": "success"}, {"id": 2, "status": "success"},
                              {"id": 3, "status": "manual"}]
        gl.bridges["g/app#5"] = [{"name": "deploy", "status": "success",
                                  "downstream_pipeline": {"status": "running"}}]
        text = ci_result_detail(gl, "pipeline", "g/app#5")
        self.assertIn("jobs: 2 success, 1 manual", text)
        self.assertIn("downstream: deploy running", text)
        self.assertEqual(gl.reads, [])                         # a green pipeline reads no log

    def test_other_waits_get_nothing(self):
        self.assertEqual(ci_result_detail(ReadingGitLab(), "card", "abcd1234"), "")
        self.assertEqual(ci_result_detail(None, "pipeline", "g/app#5"), "")

    def test_trace_tail_keeps_the_end_without_terminal_codes(self):
        tail = _trace_tail("".join(f"\x1b[31mrow {i}\x1b[0m\r\n" for i in range(100)))
        self.assertTrue(tail.endswith("row 99"))
        self.assertNotIn("row 74", tail)
        self.assertIn("row 75", tail)
        self.assertNotIn("\x1b", tail)


if __name__ == "__main__":
    unittest.main()
