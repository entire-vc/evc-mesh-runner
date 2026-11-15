"""Operator pauses hold a green merge wait without restarting the Runner."""
import sys

from mesh_runner import op_pause
from test_merge_wait import MergeBench, PROJECT, SHA


class MergePause(MergeBench):
    def test_token_holds_green_merge_until_resumed(self):
        r, a, _, gl = self.waiting()
        gl.mr_rows[a.wait["target"]]["head_pipeline"]["status"] = "success"
        op_pause.take(self.cfg.state_dir, self.agent.name, "maintenance")
        self.check(r, a)
        self.assertEqual(self.runs(), [])
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertEqual(r.store.get(a.id).wait["merge"]["runs"], 0)
        op_pause.release(self.cfg.state_dir, self.agent.name, "maintenance")
        self.check(r, r.store.get(a.id))
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])

    def test_config_pause_holds_merge(self):
        r, a, _, _ = self.waiting()
        self.agent.paused = True
        self.check(r, a)
        self.assertEqual(self.runs(), [])

    def test_pause_arriving_during_environment_preparation_holds_merge(self):
        r, a, _, _ = self.waiting()
        token = self.cfg.state_dir / "pause.d" / self.agent.name / "late"
        # Real environment hook at the subprocess boundary; no replaced Runner methods.
        self.agent.env_command = [sys.executable, "-c",
                                  f"from pathlib import Path;p=Path({str(token)!r});"
                                  "p.parent.mkdir(parents=True,exist_ok=True);p.touch()"]
        self.check(r, a)
        self.assertTrue(token.exists())
        self.assertEqual(self.runs(), [])
        self.assertNotIn(a.id, r._merge_jobs)

