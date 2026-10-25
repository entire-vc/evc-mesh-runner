import datetime as dt
import unittest

from mesh_runner.outcome import parse_outcome

NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone.utc)


class OutcomeGrammar(unittest.TestCase):
    def p(self, text, self8=""):
        return parse_outcome(text, NOW, self8)

    def test_pipeline_with_jobs_and_note(self):
        o = self.p("done\n⏳ WAIT pipeline:grp/app#123 jobs=[lint, test] — waiting for CI")
        self.assertEqual((o.kind, o.target, o.jobs), ("pipeline", "grp/app#123", ("lint", "test")))
        self.assertEqual(o.note, "waiting for CI")

    def test_mr_card_decision(self):
        self.assertEqual(self.p("⏳ WAIT mr:grp/sub/app!42").target, "grp/sub/app!42")
        self.assertEqual(self.p("⏳ WAIT card:#abcdef12: needs schema").target, "abcdef12")
        self.assertEqual(self.p("⏳ WAIT decision:@lead").handle, "lead")

    def test_card_cannot_wait_for_itself(self):
        self.assertEqual(self.p("⏳ WAIT card:#abcdef12", "abcdef12").kind, "unparsed")

    def test_durations_are_clamped(self):
        self.assertEqual(self.p("⏳ WAIT 1m").until, NOW + dt.timedelta(minutes=5))
        self.assertEqual(self.p("⏳ WAIT 2h").until, NOW + dt.timedelta(hours=2))
        self.assertEqual(self.p("⏳ WAIT 90h").until, NOW + dt.timedelta(hours=24))

    def test_absolute_time(self):
        o = self.p("⏳ WAIT 2026-10-20T06:00Z: data window")
        self.assertEqual(o.until, dt.datetime(2026, 10, 20, 6, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(self.p("⏳ WAIT 2030-01-01").kind, "unparsed")

    def test_next_and_blocking(self):
        self.assertEqual(self.p("↻ NEXT").kind, "next")
        self.assertEqual(self.p("↻ NEXT: tests next").note, "tests next")
        o = self.p("❓ **Blocking @owner**: pick a plan?")
        self.assertEqual((o.kind, o.handle, o.note), ("blocking", "owner", "pick a plan?"))

    def test_last_declaration_wins_and_prose_is_ignored(self):
        self.assertIsNone(self.p("I will WAIT for CI later"))
        self.assertEqual(self.p("⏳ WAIT 2h\nmore text\n↻ NEXT").kind, "next")

    def test_pipe_separated_explanation_and_mr_named_as_pipeline(self):
        o = self.p("⏳ WAIT 8h | 2026-10-17T03:30Z")
        self.assertEqual((o.kind, o.until), ("time", NOW + dt.timedelta(hours=8)))
        self.assertEqual(o.note, "2026-10-17T03:30Z")
        o = self.p("⏳ WAIT 2026-10-17T03:30Z | night run")
        self.assertEqual(o.kind, "time")
        o = self.p("⏳ WAIT pipeline:group/project!158")
        self.assertEqual((o.kind, o.target), ("mr", "group/project!158"))

    def test_unknown_wait_target_is_unparsed(self):
        self.assertEqual(self.p("⏳ WAIT forever").kind, "unparsed")


if __name__ == "__main__":
    unittest.main()
