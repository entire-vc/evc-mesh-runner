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


class TimeWaitReason(unittest.TestCase):
    def p(self, text):
        return parse_outcome(text, NOW)

    def test_event_reasons_are_sent_back_with_the_wait_kind_to_use(self):
        for line in ("⏳ WAIT 2h: waiting for the reviewer verdict",
                     "⏳ WAIT 6h: ждём ответ Павла",
                     "⏳ WAIT 2h: @bill should answer",
                     "⏳ WAIT 2h: after #abcdef12 closes",
                     "⏳ WAIT 2026-10-20T06:00Z: decision from the lead",
                     "⏳ WAIT 1h — решение по карточке"):
            o = self.p(line)
            self.assertEqual(o.kind, "unparsed", line)
            self.assertIn("⏳ WAIT card:#<8hex>", o.note)
            self.assertIn("decision:@<agent>", o.note)

    def test_time_as_the_condition_still_parses(self):
        for line in ("⏳ WAIT 2h: soak window", "⏳ WAIT 2h", "⏳ WAIT 2026-10-20T06:00Z: measurement window ends",
                     "⏳ WAIT 24h: traffic needs a full day"):
            self.assertEqual(self.p(line).kind, "time", line)

    def test_poll_of_an_outside_channel_is_a_time_wait(self):
        # the reply comes in a chat outside Mesh; the reason named
        # the answer, the line was refused and the card was given up.
        for line in ("⏳ WAIT 2026-10-26T12:20Z: poll: Telegram inbox; on reply, copy it verbatim",
                     "⏳ WAIT 6h: POLL: mail from the registrar, answer expected",
                     "⏳ WAIT 2h: poll : @alice reply in Telegram"):
            o = self.p(line)
            self.assertEqual(o.kind, "time", line)
            self.assertTrue(o.note.lower().startswith("poll"), o.note)

    def test_poll_further_in_the_reason_does_not_exempt(self):
        o = self.p("⏳ WAIT 2h: answer from @alice, then poll: CI")
        self.assertEqual(o.kind, "unparsed")
        self.assertIn("poll:", o.note)


if __name__ == "__main__":
    unittest.main()
