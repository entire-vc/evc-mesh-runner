import tempfile
import unittest
from pathlib import Path

from mesh_runner.config import load
from mesh_runner.hostmem import (LoadGate, LoadThresholds, MemoryGate, StartRamp, Thresholds, decide,
                                 parse_meminfo, parse_swapusage)

T = Thresholds(hold_free_pct=15, release_free_pct=20, hold_swap_mb=300, release_swap_mb=600)


class HostMem(unittest.TestCase):
    def test_hysteresis(self):
        self.assertTrue(decide(False, 10, None, T))
        self.assertTrue(decide(True, 18, None, T))          # between hold and release: still holding
        self.assertFalse(decide(True, 21, None, T))
        self.assertFalse(decide(False, 18, None, T))

    def test_swap_counts_only_when_memory_is_low(self):
        self.assertFalse(decide(False, 40, 100, T))         # plenty free: swap ignored
        self.assertTrue(decide(False, 25, 100, T))

    def test_no_readings_fail_open_and_zero_threshold_is_off(self):
        self.assertFalse(decide(True, None, None, T))
        self.assertFalse(decide(False, 1, 1, Thresholds()))

    def test_parsers(self):
        self.assertAlmostEqual(parse_swapusage("total = 2048,00M  used = 1536,00M  free = 512,00M  (encrypted)"), 512)
        self.assertIsNone(parse_swapusage("total = 0.00M  used = 0.00M  free = 0.00M"))
        free, swap = parse_meminfo("MemTotal: 1000 kB\nMemAvailable: 250 kB\nSwapTotal: 2048 kB\nSwapFree: 1024 kB\n")
        self.assertEqual((free, swap), (25.0, 1.0))


class SettleAndRamp(unittest.TestCase):
    """A memory hold lifted and came back a few minutes later; a load hold lifted into a burst of starts.
    The sessions started in those gaps never took their prompt."""

    def setUp(self):
        self.now = [0.0]
        self.mem = [(10.0, None)]                     # free %, swap MB: under the 15 % hold
        self.load = [(30.0, 10)]                      # 3 per CPU: open
        clock = lambda: self.now[0]
        self.memgate = MemoryGate(Thresholds(15, 20, 300, 600), reader=lambda: self.mem[0], clock=clock,
                                  settle_seconds=120)
        self.loadgate = LoadGate(LoadThresholds(8.0, 5.0), reader=lambda: self.load[0], clock=clock,
                                 settle_seconds=120)
        self.ramp = StartRamp(20, 300, (self.memgate, self.loadgate), clock=clock)

    def admitted(self) -> bool:
        """The runner's look right before a window: fresh memory, fresh load, then the ramp."""
        return not (self.memgate.check(fresh=True) or self.loadgate.check(fresh=True)) and self.ramp.admit()

    def at(self, t):
        self.now[0] = t

    def test_a_hold_back_47_s_after_its_release_lets_nothing_through(self):
        self.assertFalse(self.admitted())                         # HOLD
        self.at(10); self.mem[0] = (33.0, None)                   # release condition met
        self.assertFalse(self.admitted())
        for t in range(15, 57, 5):
            self.at(t)
            self.assertFalse(self.admitted(), f"{t} s: still inside the settle")
        self.at(57); self.mem[0] = (14.0, None)                   # 47 s later: back under the hold
        self.assertFalse(self.admitted())
        self.at(200)
        self.assertFalse(self.admitted())
        self.assertIsNone(self.memgate.released_at, "the hold never lifted")

    def test_no_start_until_the_release_held_for_the_settle(self):
        self.assertFalse(self.admitted())                         # HOLD
        self.at(10); self.mem[0] = (33.0, None)
        self.assertFalse(self.admitted())
        self.at(129)
        self.assertFalse(self.admitted(), "119 s of release is not 120")
        self.at(130)
        self.assertTrue(self.admitted())

    def test_a_reading_between_the_thresholds_restarts_the_settle(self):
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertFalse(self.admitted())                     # HOLD
            self.at(10); self.mem[0] = (33.0, None)               # release condition met
            self.assertFalse(self.admitted())
            self.at(100); self.mem[0] = (18.0, None)              # between the thresholds: not a release
            self.assertFalse(self.admitted())
            self.at(110); self.mem[0] = (33.0, None)              # a new streak starts here
            self.assertFalse(self.admitted())
            self.at(229)
            self.assertFalse(self.admitted())
            self.at(230)
            self.assertTrue(self.admitted())
        text = "\n".join(logs.output)
        self.assertEqual(text.count("release condition met"), 2)
        self.assertEqual(text.count("host memory: resume new sessions"), 1)

    def test_failed_readings_during_a_hold_do_not_count_toward_the_settle(self):
        def broken():
            raise OSError("vm_stat did not run")
        self.assertFalse(self.admitted())                         # HOLD
        for reading in ((None, None), broken):
            self.memgate._read = reading if callable(reading) else (lambda r=reading: r)
            for t in range(10, 300, 10):
                self.at(t)
                self.assertFalse(self.admitted(), f"{t} s: no reading is no release")
            self.assertTrue(self.memgate.holding)
            self.assertIsNone(self.memgate.released_at)
        self.memgate._read = lambda: self.mem[0]
        self.at(300); self.mem[0] = (33.0, None)                  # a real release reading: the streak starts
        self.assertFalse(self.admitted())
        self.at(400); self.memgate._read = lambda: (None, None)   # a failed reading inside the streak
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertFalse(self.admitted())
        self.assertIn("no reading during the release settle", "\n".join(logs.output))
        self.memgate._read = lambda: self.mem[0]
        self.at(410)
        self.assertFalse(self.admitted(), "the streak starts again at the next real reading")
        self.at(529)
        self.assertFalse(self.admitted())
        self.at(530)
        self.assertTrue(self.admitted())

    def test_a_load_hold_keeps_through_a_failed_load_reading(self):
        self.mem[0] = (50.0, None)
        self.load[0] = (93.0, 10)
        self.assertFalse(self.admitted())                         # HOLD
        self.load[0] = (None, 10)
        for t in range(10, 300, 10):
            self.at(t)
            self.assertFalse(self.admitted(), f"{t} s")
        self.assertTrue(self.loadgate.holding)
        self.at(300); self.load[0] = (49.0, 10)
        self.assertFalse(self.admitted())
        self.at(420)
        self.assertTrue(self.admitted())

    def test_the_ramp_spaces_starts_then_ends(self):
        self.assertFalse(self.admitted())                         # HOLD
        self.at(10); self.mem[0] = (33.0, None)
        self.assertFalse(self.admitted())
        self.at(130)
        self.assertTrue(self.admitted(), "the first start after the settle")
        self.assertFalse(self.admitted(), "a second at the same moment waits")
        self.at(149)
        self.assertFalse(self.admitted())
        self.assertFalse(self.ramp.ready())
        self.at(150)
        self.assertTrue(self.ramp.ready())
        self.assertTrue(self.admitted())
        starts = [150]
        for t in range(151, 430):
            self.at(t)
            if self.admitted():
                starts.append(t)
        self.assertTrue(all(b - a >= 20 for a, b in zip(starts, starts[1:])), starts)
        self.at(430)                                              # 300 s after the release at 130: no ramp
        self.assertTrue(self.admitted())
        self.assertTrue(self.admitted())

    def test_a_load_hold_settles_and_ramps_the_same_way(self):
        self.mem[0] = (50.0, None)
        self.load[0] = (93.0, 10)                                 # 9.3 per CPU
        self.assertFalse(self.admitted())
        self.at(5); self.load[0] = (49.0, 10)                     # 4.9 per CPU: release condition
        self.assertFalse(self.admitted())
        self.at(124)
        self.assertFalse(self.admitted())
        self.at(125)
        self.assertTrue(self.admitted())
        self.at(130)
        self.assertFalse(self.admitted())

    def test_the_load_gate_reads_fresh(self):
        self.at(0)
        self.assertFalse(self.loadgate.check())
        self.load[0] = (93.0, 10)
        self.assertFalse(self.loadgate.check(), "cached for 30 s")
        self.assertTrue(self.loadgate.check(fresh=True), "the look before a start reads the load now")

    def test_zero_settle_and_zero_ramp_are_the_old_gates(self):
        gate = MemoryGate(Thresholds(15, 20, 300, 600), reader=lambda: self.mem[0], clock=lambda: self.now[0])
        ramp = StartRamp(0, 300, (gate,), clock=lambda: self.now[0])
        self.assertTrue(gate.check(fresh=True))
        self.mem[0] = (33.0, None)
        self.assertFalse(gate.check(fresh=True))
        self.assertTrue(ramp.admit() and ramp.admit())
        self.mem[0] = (10.0, None)
        self.assertTrue(gate.check(fresh=True))
        self.mem[0] = (None, None)
        self.assertFalse(gate.check(fresh=True), "without a settle a failed reading still fails open, as before")


class RampByWaitRoot(unittest.TestCase):
    """The ramp's slots go to the heaviest wait root any lane claims, not to the first asker."""

    def setUp(self):
        self.now = [90.0]                                         # the lanes claim during the hold's last tick
        self.gate = type("Gate", (), {"holding": True, "released_at": None})()
        self.ramp = StartRamp(20, 300, (self.gate,), clock=lambda: self.now[0])

    def lift(self, at=100.0):
        self.now[0] = at
        self.gate.holding, self.gate.released_at = False, at

    def test_claims_count_only_while_the_host_holds_or_ramps(self):
        self.assertTrue(self.ramp.claiming(), "holding")
        self.ramp.claim("B", {"root": 6})
        self.lift()
        self.assertTrue(self.ramp.claiming())
        self.assertFalse(self.ramp.ready("light", 0))
        self.now[0] = 400                                         # the ramp is over: no order, no spacing
        self.assertFalse(self.ramp.claiming())
        self.assertTrue(self.ramp.admit("light", 0) and self.ramp.admit("other", 0))

    def test_heavier_claim_keeps_the_slot_equal_weight_shares_it(self):
        self.ramp.claim("B", {"root": 6})
        self.ramp.claim("C", {"small": 2})
        self.lift()
        with self.assertLogs("mesh_runner", "INFO") as logs:
            self.assertFalse(self.ramp.admit("light", 0))
            self.assertFalse(self.ramp.admit("small", 2), "2 waiting is lighter than 6")
            self.assertTrue(self.ramp.ready("root", 6))
            self.assertTrue(self.ramp.admit("root", 6))
        self.assertIn("next start kept for wait root #root (6 waiting behind it, lane B)", "\n".join(logs.output))
        self.assertIn("start #root goes first: a wait root with 6 waiting behind it", "\n".join(logs.output))
        self.assertFalse(self.ramp.admit("small", 2), "one start per interval")
        self.now[0] = 120
        self.assertFalse(self.ramp.admit("light", 0), "the next slot: the next heaviest claim")
        self.assertTrue(self.ramp.admit("other", 2), "an equal weight is not passed over")
        self.now[0] = 140
        self.assertFalse(self.ramp.admit("light", 0))
        self.assertTrue(self.ramp.admit("small", 2))
        self.now[0] = 160
        self.assertTrue(self.ramp.admit("light", 0), "no claim left")

    def test_a_claim_its_lane_stops_renewing_lapses(self):
        self.ramp.claim("B", {"root": 6})
        self.lift()
        self.assertFalse(self.ramp.ready("light", 0))
        self.now[0] = 149
        self.assertFalse(self.ramp.ready("light", 0))
        self.now[0] = 150                                         # 60 s (3 intervals) without a renewal
        self.assertTrue(self.ramp.admit("light", 0))
        self.now[0] = 170
        self.ramp.claim("B", {"root": 6})                         # renewed, then withdrawn by its lane
        self.assertFalse(self.ramp.ready("light", 0))
        self.ramp.claim("B", {})
        self.assertTrue(self.ramp.ready("light", 0))

    def test_a_root_that_never_takes_its_slot_is_passed_once_per_ramp(self):
        self.lift()
        with self.assertLogs("mesh_runner", "INFO") as logs:
            for t in range(100, 160, 10):
                self.now[0] = t
                self.ramp.claim("B", {"root": 6})                 # renewed every tick, never started
                self.assertFalse(self.ramp.admit("light", 0), t)
            self.now[0] = 160
            self.ramp.claim("B", {"root": 6})
            self.assertTrue(self.ramp.admit("light", 0), "kept 60 s without a start: passed")
        self.assertIn("wait root #root (lane B) did not start within 60s", "\n".join(logs.output))
        self.now[0] = 180
        self.ramp.claim("B", {"root": 6})
        self.assertTrue(self.ramp.admit("other", 0), "passed for the rest of this ramp")
        self.gate.holding = True                                  # a new hold and a new ramp: it is heard again
        self.lift(500)
        self.ramp.claim("B", {"root": 6})
        self.assertFalse(self.ramp.admit("light", 0))
        self.assertTrue(self.ramp.admit("root", 6))

    def test_no_claims_is_the_ramp_as_before(self):
        self.lift()
        self.assertTrue(self.ramp.admit())
        self.assertFalse(self.ramp.admit())
        self.assertFalse(self.ramp.open())
        self.now[0] = 120
        self.assertTrue(self.ramp.open() and self.ramp.ready() and self.ramp.admit())


class SettleAndRampConfig(unittest.TestCase):
    def test_runner_toml_keys_default_to_120_300_20(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "k"
            key.write_text("x")
            key.chmod(0o600)
            base = (f'[runner]\nmesh_url = "http://m"\nstate_dir = "{tmp}"\n{{runner}}'
                    f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{tmp}"\n')
            path = Path(tmp) / "r.toml"
            path.write_text(base.format(runner=""))
            cfg = load(path)
            self.assertEqual((cfg.host_release_settle_seconds, cfg.host_ramp_seconds,
                              cfg.host_ramp_start_interval_seconds), (120.0, 300.0, 20.0))
            path.write_text(base.format(runner="host_release_settle_seconds = 60\nhost_ramp_seconds = 0\n"
                                               "host_ramp_start_interval_seconds = 30\n"))
            cfg = load(path)
            self.assertEqual((cfg.host_release_settle_seconds, cfg.host_ramp_seconds,
                              cfg.host_ramp_start_interval_seconds), (60.0, 0.0, 30.0))


if __name__ == "__main__":
    unittest.main()
