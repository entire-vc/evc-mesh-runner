import unittest

from mesh_runner.hostmem import Thresholds, decide, parse_meminfo, parse_swapusage

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


if __name__ == "__main__":
    unittest.main()
