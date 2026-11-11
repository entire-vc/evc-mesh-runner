"""ChatLane: the one surface chat reads from an agent lane, the same in the runner and in a chat process."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from mesh_runner.chat_lane import ChatLane, chat_lanes, lane_of


class LaneSurface(unittest.TestCase):
    def owner(self, **kw):
        base = dict(agent=NS(name="A", harness="claude"), cfg=NS(state_dir=Path("/s")), pool="POOL",
                    _routed_harness="codex", _read_env=lambda: {"R": "1"},
                    _route_env=lambda raw: (dict(raw), "codex", "p"), adapter_for=lambda h: f"adapter:{h}",
                    _gate=Mock(return_value=("held", {"X": "1"})))
        base.update(kw)
        return NS(**base)

    def test_reads_through_to_the_lane(self):
        lane = ChatLane(self.owner())
        self.assertEqual((lane.name, lane.pool, lane.routed_harness), ("A", "POOL", "codex"))
        self.assertEqual(lane.read_env(), {"R": "1"})
        self.assertEqual(lane.route_env({"R": "1"}), ({"R": "1"}, "codex", "p"))
        self.assertEqual(lane.adapter_for("claude"), "adapter:claude")

    def test_transport_hold_is_the_gate_transport_phase_and_its_reason_only(self):
        owner = self.owner()
        self.assertEqual(ChatLane(owner).transport_hold(), "held")
        owner._gate.assert_called_once_with("transport", "", "chat")

    def test_fallbacks(self):
        lane = ChatLane(NS(agent=NS(name="A", harness="claude")))
        self.assertIsNone(lane.pool)                         # a lane without a pool: chat never leases
        self.assertEqual(lane.routed_harness, "claude")      # never routed yet: the configured harness

    def test_lane_of_keeps_a_lane_and_wraps_anything_else(self):
        lane = ChatLane(self.owner())
        self.assertIs(lane_of(lane), lane)
        owner = self.owner()
        self.assertIs(lane_of(owner).owner, owner)

    def test_two_lanes_over_one_owner_share_the_env_entry(self):
        from mesh_runner.chat_host import _env_cache, routed
        calls = []
        owner = self.owner(_read_env=lambda: calls.append(1) or {"R": "1"})
        _env_cache.clear()
        routed(ChatLane(owner))
        routed(ChatLane(owner))
        routed(owner)
        self.assertEqual(len(calls), 1)                      # one router run: the entry is the lane's, not the wrapper's
        _env_cache.clear()


class ProcessLanes(unittest.TestCase):
    def test_chat_lanes_builds_only_the_chat_agents_and_starts_nothing(self):
        from mesh_runner.config import AgentConfig, RunnerConfig
        state = Path(tempfile.mkdtemp())
        agents = [AgentConfig(name=n, key="k", harness="claude", profile_dir=state, repos=[]) for n in ("A", "B")]
        cfg = RunnerConfig(mesh_url="https://mesh.test", state_dir=state, agents=agents, chat_agents=("B",))
        lanes = chat_lanes(cfg)
        self.assertEqual([lane.agent.name for lane in lanes], ["B"])
        self.assertEqual(lanes[0].store.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")


if __name__ == "__main__":
    unittest.main()
