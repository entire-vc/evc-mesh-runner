"""Counter acceptance uses complete windows and separates retries from turns."""
import datetime as dt
import json
import unittest

from mesh_runner.outcome_readiness import DAY, report, timestamp

NOW = timestamp("2026-11-11T16:00:00Z")


def event(at, source="typed", kind="outcome_source"):
    return at.isoformat(), kind, json.dumps({"source": source})


class Readiness(unittest.TestCase):
    def test_zero_week_and_red_control_for_each_marker_source(self):
        rows = [event(NOW - 8 * DAY), event(NOW - DAY)]
        self.assertTrue(report(rows, NOW)["zero_week"]["ready"])
        for source in ("comment", "final_text"):
            with self.subTest(source=source):
                result = report(rows + [event(NOW - dt.timedelta(seconds=1), source)], NOW)
                self.assertFalse(result["zero_week"]["ready"])
                self.assertEqual(result["zero_week"]["sources"][source], 1)

    def test_old_events_do_not_substitute_for_counter_coverage(self):
        rows = [event(NOW - 8 * DAY, kind="no_outcome"), event(NOW - DAY)]
        result = report(rows, NOW)
        self.assertFalse(result["zero_week"]["ready"])
        self.assertFalse(report([], NOW)["zero_week"]["ready"])

    def test_half_open_windows_and_future_rows(self):
        rows = [event(NOW - 7 * DAY, "comment"), event(NOW, "final_text"), event(NOW + DAY)]
        result = report(rows, NOW)
        self.assertEqual(result["zero_week"]["sources"]["comment"], 1)
        self.assertEqual(result["zero_week"]["sources"]["final_text"], 0)
        self.assertEqual(result["zero_week"]["sources"]["typed"], 0)

    def test_install_comparison_boundary_and_no_growth(self):
        installed = NOW - DAY
        rows = [event(installed - DAY), event(installed - dt.timedelta(seconds=1), "none"),
                event(installed, "none"), event(installed - dt.timedelta(seconds=1), kind="no_outcome"),
                event(installed, kind="no_outcome")]
        result = report(rows, NOW, installed)["post_install"]
        self.assertTrue(result["ready"])
        self.assertEqual(result["before"]["sources"]["none"], 1)
        self.assertEqual(result["after"]["sources"]["none"], 1)
        for extra in (event(installed, "none"), event(installed, kind="no_outcome")):
            self.assertFalse(report(rows + [extra], NOW, installed)["post_install"]["ready"])

    def test_incomplete_day_and_missing_baseline_fail(self):
        rows = [event(NOW - 8 * DAY), event(NOW - DAY)]
        self.assertFalse(report(rows, NOW, NOW - DAY / 2)["post_install"]["ready"])
        self.assertFalse(report(rows[1:], NOW, NOW - DAY)["post_install"]["ready"])

    def test_offsets_normalized_and_invalid_time_or_source_rejected(self):
        self.assertEqual(timestamp("2026-11-11T19:00:00+03:00"), NOW)
        with self.assertRaises(ValueError):
            timestamp("2026-11-11T16:00:00")
        with self.assertRaises(ValueError):
            report([event(NOW - DAY, "new-source")], NOW)
        with self.assertRaises(ValueError):
            report([], NOW, NOW + DAY)
