"""Questions: to an agent that can answer, else to the project lead; to a person, hard or soft."""
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakes import FakeMesh, task
from test_runner import Bench

from mesh_runner import ask_audit, asks, cli
from mesh_runner import config as config_mod
from mesh_runner.config import AgentConfig
from mesh_runner.outcome import Outcome
from mesh_runner.wake import check as wake_check

AT = "2026-10-21T00:00:00Z"


class AskBench(Bench):
    def setup_route(self, handle, note="which option?", labels=()):
        self.source = task(1, labels=list(labels))
        self.mesh = FakeMesh([self.source])
        self.agent.lead = "lead"
        self.r = self.runner(self.mesh)
        self.r.refresh_me()
        self.a = self.r.store.create(self.agent.name, self.source["id"], "claude")
        self.handle, self.note = handle, note

    def route(self, kind="decision"):
        self.wait = {"kind": kind, "handle": self.handle, "note": self.note, "line": f"⏳ WAIT decision:@{self.handle}"}
        self.told = self.r._route_ask(self.a, self.source,
                                      Outcome(kind=kind, handle=self.handle, note=self.note), self.wait, AT)
        return self.told

    def agent_cfg(self, name, **kw):
        ag = AgentConfig(name=name, key="k", harness="claude", profile_dir=self.root, repos=[], **kw)
        self.cfg.agents.append(ag)
        return ag

    def bodies(self):
        return [c["body"] for c in self.mesh.comment_log.get(self.source["id"], [])]

    def blocking_comments(self):
        return [b for b in self.bodies() if "❓ Blocking" in b]


class AgentAddressee(AskBench):
    def test_an_active_addressee_gets_the_request_with_priority(self):
        self.setup_route("reviewer")
        self.agent_cfg("reviewer")
        self.assertEqual(self.route(), "")
        self.assertEqual(self.wait["handle"], "reviewer")
        self.assertNotIn("asked_handle", self.wait)
        self.r.effects.on_wait(self.a, self.source, self.wait, self.r.me)
        request = self.mesh.created[0]
        self.assertEqual(request["assignee_id"], "agent-reviewer")
        self.assertEqual(request["priority"], "high")
        self.assertIn("decision-request", request["labels"])

    def test_the_addressee_picks_a_decision_request_before_ordinary_work(self):
        urgent = task(2, priority="urgent")
        request = task(3, priority="low", labels=["decision-request"])
        plain = task(4, priority="high")
        mesh = FakeMesh([urgent, request, plain])
        r = self.runner(mesh)
        r.refresh_me()
        self.assertEqual([t["id"] for t in r.candidates()], [request["id"], urgent["id"], plain["id"]])

    def test_a_paused_addressee_sends_the_request_to_the_lead_with_a_comment(self):
        self.setup_route("reviewer")
        self.agent_cfg("reviewer")
        from mesh_runner import op_pause
        op_pause.take(self.cfg.state_dir, "reviewer", "maintenance")
        self.assertEqual(self.route(), "")
        self.assertEqual((self.wait["kind"], self.wait["handle"], self.wait["asked_handle"]),
                         ("decision", "lead", "reviewer"))
        self.r.effects.on_wait(self.a, self.source, self.wait, self.r.me)
        self.assertEqual(self.mesh.created[0]["assignee_id"], "agent-lead")
        self.assertIn("put to @reviewer", self.mesh.created[0]["description"])
        said = self.bodies()[-1]
        self.assertIn("@lead", said)
        self.assertIn("paused", said)
        self.assertEqual(self.blocking_comments(), [])

    def test_config_pause_not_run_here_and_self_each_send_it_to_the_lead(self):
        for label, prep, handle in (("config pause", lambda s: s.agent_cfg("reviewer", paused=True), "reviewer"),
                                    ("not run by this runner", lambda s: None, "reviewer"),
                                    ("the asker itself", lambda s: None, "builder")):
            with self.subTest(label):
                self.setUp()
                self.setup_route(handle)
                prep(self)
                self.assertEqual(self.route(), "")
                self.assertEqual(self.wait["handle"], "lead")
                self.tmp.cleanup()

    def test_the_answer_of_the_lead_or_of_the_addressee_wakes_the_asker(self):
        self.setup_route("reviewer")
        self.route()
        self.r.effects.on_wait(self.a, self.source, self.wait, self.r.me)
        for who in ("agent-lead", "agent-reviewer"):
            with self.subTest(who):
                self.mesh.comment_log.clear()
                self.mesh.add_comment(self.source["id"], "go with B", author=who,
                                      at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5))
                wait = dict(self.wait, since=AT, started=AT)
                event = wake_check(self.mesh, self.r.cats, None, self.source, wait, self.r.agent_id,
                                   self.mesh.comments(self.source["id"]), dt.datetime.now(dt.timezone.utc))
                self.assertIn("commented", event)

    def test_without_a_lead_it_waits_as_before(self):
        self.setup_route("reviewer")
        self.agent.lead = ""
        self.assertEqual(self.route(), "")
        self.assertEqual(self.wait["handle"], "reviewer")
        self.assertNotIn("asked_handle", self.wait)

    def test_a_question_to_yourself_without_a_lead_is_not_a_wait(self):
        # `WAIT decision:@<their own agent>` has no answerer when the agent has no lead.
        self.setup_route("builder")
        self.agent.lead = ""
        told = self.route()
        self.assertIn("asking agent itself", told)
        self.assertNotEqual(self.wait.get("kind"), "decision-routed")

    def test_the_switch_restores_the_older_behaviour(self):
        self.setup_route("reviewer")
        self.cfg.unanswerable_ask_to_lead = False
        self.assertEqual(self.route(), "")
        self.assertEqual(self.wait["handle"], "reviewer")

    def test_the_lead_is_not_the_unavailable_addressee(self):
        self.setup_route("lead")
        self.agent.lead = "lead"
        self.assertEqual(self.route(), "")
        self.assertEqual(self.wait["handle"], "lead")        # no other lead to go to: stays as asked

    def test_a_test_user_never_answers(self):
        self.setup_route("owner")
        self.cfg.unanswerable_users = ("owner",)
        self.assertEqual(self.route(), "")
        self.assertEqual(self.wait["handle"], "lead")
        self.assertEqual(self.blocking_comments(), [])

    def test_a_roster_row_marked_as_test_never_answers(self):
        self.setup_route("owner")
        self.mesh.recipients = [{"id": "owner", "kind": "user", "slug": "owner", "display_name": "owner",
                                 "is_test": True}]
        self.assertEqual(self.route(), "")
        self.assertEqual(self.wait["handle"], "lead")


class PersonAsk(AskBench):
    def test_a_payment_question_stays_a_human_gate(self):
        self.setup_route("person", "Should we pay for the annual plan?")
        self.route()
        self.assertEqual(len(self.blocking_comments()), 1)
        self.assertTrue(self.wait["person_ask"])
        self.assertEqual(self.wait["handle"], "person")
        self.assertEqual(self.mesh.created, [])

    def test_a_design_question_goes_to_the_lead_without_a_gate(self):
        self.setup_route("person", "Which variant of the settings page, A or B? I suggest A.")
        self.assertEqual(self.route(), "")
        self.assertEqual(self.blocking_comments(), [])
        self.assertEqual((self.wait["kind"], self.wait["handle"], self.wait["asked_handle"]),
                         ("decision", "lead", "person"))
        self.assertNotIn("person_ask", self.wait)
        self.assertIn("Decided by lead, owner may override by commenting here", self.bodies()[-1])
        self.r.effects.on_wait(self.a, self.source, self.wait, self.r.me)
        self.assertEqual(self.mesh.created[0]["assignee_id"], "agent-lead")

    def test_a_later_comment_by_the_person_wakes_the_card(self):
        self.setup_route("person", "Which variant, A or B?")
        self.route()
        self.r.effects.on_wait(self.a, self.source, self.wait, self.r.me)
        self.mesh.add_comment(self.source["id"], "no, take C", author="user-person",
                              at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5))
        wait = dict(self.wait, since=AT, started=AT)
        event = wake_check(self.mesh, self.r.cats, None, self.source, wait, self.r.agent_id,
                           self.mesh.comments(self.source["id"]), dt.datetime.now(dt.timezone.utc))
        self.assertIn("user-person commented", event)

    def test_a_hard_label_makes_a_soft_question_hard(self):
        for label in asks.DEFAULT_HARD_LABELS:
            with self.subTest(label):
                self.setUp()
                self.setup_route("person", "Which variant, A or B?", labels=[label])
                self.route()
                self.assertEqual(len(self.blocking_comments()), 1)
                self.tmp.cleanup()

    def test_an_ask_the_agent_raised_itself_is_not_touched(self):
        self.setup_route("person", "Which variant, A or B?")
        self.route(kind="blocking")
        self.assertEqual(self.wait["handle"], "person")
        self.assertEqual(self.mesh.created, [])

    def test_without_a_lead_a_soft_question_stays_a_gate(self):
        self.setup_route("person", "Which variant, A or B?")
        self.agent.lead = ""
        self.route()
        self.assertEqual(len(self.blocking_comments()), 1)

    def test_an_empty_question_raises_nothing_and_falls_to_the_bounded_recheck(self):
        self.setup_route("person", note="")
        self.route()
        self.assertEqual(self.bodies(), [])                      # no `❓ Blocking`, no notice
        self.assertEqual(self.mesh.created, [])                  # no decision request
        self.assertEqual((self.wait["kind"], self.wait["handle"], self.wait["person_ask"]),
                         ("decision", "person", True))
        self.assertNotIn("asked_handle", self.wait)             # not redirected to the lead
        self.r.effects.on_wait(self.a, self.source, self.wait, self.r.me)
        self.assertEqual(self.mesh.created, [])
        started = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)
        wait = dict(self.wait, since=AT, started=started.isoformat())
        task = dict(self.source, human_gate=False)           # Mesh reports an explicit "no gate"
        event = wake_check(self.mesh, self.r.cats, None, task, wait, self.r.agent_id, [],
                           dt.datetime.now(dt.timezone.utc), first_recheck=dt.timedelta(hours=2))
        self.assertIn("no human gate or agent request stands behind it", event)
        self.assertTrue(wait["recheck_due"])

    def test_the_question_is_posted_to_the_lead_once(self):
        self.setup_route("person", "Which variant, A or B?")
        self.route()
        self.route()
        self.assertEqual(sum("Decided by lead" in b for b in self.bodies()), 1)


class LoginAskCheck(AskBench):
    def fake_run(self, rc, out="registry: session LIVE"):
        return mock.patch("mesh_runner.runner.subprocess.run",
                          return_value=SimpleNamespace(returncode=rc, stdout=out, stderr=""))

    def test_a_login_ask_the_registry_answers_goes_back_to_the_lane_once(self):
        self.setup_route("person", "Sign in to GitHub in Chrome Profile 1, the session expired")
        self.cfg.login_ask_check = ("check",)
        with self.fake_run(10) as run:
            told = self.route()
        self.assertIn("registry already knows this login", told)
        self.assertIn("session LIVE", told)
        self.assertEqual(self.blocking_comments(), [])
        self.assertEqual(run.call_args.kwargs["input"], self.note)
        with self.fake_run(10):                      # the same question declared again passes
            self.assertNotIn("registry already knows", self.route())
        self.assertEqual(len(self.blocking_comments()), 1)

    def test_a_question_the_registry_does_not_answer_is_raised(self):
        self.setup_route("person", "Sign in to my personal Gmail owner@example.com")
        self.cfg.login_ask_check = ("check",)
        with self.fake_run(0, ""):
            self.assertNotIn("registry already knows", self.route())
        self.assertEqual(len(self.blocking_comments()), 1)

    def test_a_broken_check_never_loses_the_question(self):
        self.setup_route("person", "Sign in to GitHub")
        self.cfg.login_ask_check = ("check",)
        with mock.patch("mesh_runner.runner.subprocess.run", side_effect=OSError("gone")):
            self.assertNotIn("registry already knows", self.route())
        self.assertEqual(len(self.blocking_comments()), 1)

    def test_undecodable_check_output_never_loses_the_question(self):
        self.setup_route("person", "Sign in to GitHub")
        self.cfg.login_ask_check = ("check",)
        err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with mock.patch("mesh_runner.runner.subprocess.run", side_effect=err):
            self.assertNotIn("registry already knows", self.route())
        self.assertEqual(len(self.blocking_comments()), 1)

    def test_the_registry_answers_before_a_soft_question_goes_to_the_lead(self):
        self.setup_route("person", "Please sign in to GitHub, which profile is better, A or B?")
        self.cfg.login_ask_check = ("check",)
        with self.fake_run(10):
            told = self.route()
        self.assertIn("registry already knows", told)
        self.assertEqual(self.mesh.created, [])

    def test_off_by_default(self):
        self.setup_route("person", "Sign in to GitHub")
        with self.fake_run(10) as run:
            self.assertNotIn("registry already knows", self.route())
        run.assert_not_called()


class Classifier(unittest.TestCase):
    def test_hard_keywords_in_both_languages(self):
        for text in ("Нужно оплатить домен", "рассылка клиентам", "Удалить таблицу?", "send the invoice",
                     "rotate the API key", "publish the post", "договор с подрядчиком", "drop the prod database",
                     "give me access to the server", "Выдать доступ", "добавлять платный раннер в пул?",
                     "выдай @bender роль Developer", "нажми «Connect» в расширении", "Войди в Glama и напиши «вошёл»",
                     "сделаешь прогон на своём iPhone", "please log in and click Allow",
                     "перетащи #55556666 в Done", "подай плагин с макбука"):
            with self.subTest(text):
                self.assertTrue(asks.classify(text, []).hard, text)

    def test_soft_questions(self):
        for text in ("Which option for the layout, A or B?", "Use sqlite or postgres for the cache?",
                     "Можно закончить рефакторинг в этом MR?", "Назвать функцию load или fetch?", "закрыть обе карточки как заменённые, раз решение в силе?",
                     "подаём в каталог плагин под названием «EVC Team Relay»?"):
            with self.subTest(text):
                self.assertFalse(asks.classify(text, []).hard, text)

    def test_unsure_is_hard(self):
        self.assertTrue(asks.classify("", []).hard)
        self.assertTrue(asks.classify("   ", ["x"]).hard)
        self.assertTrue(asks.classify("Which option?", None).hard)

    def test_the_rule_is_named(self):
        self.assertEqual(asks.classify("Pay the vendor", []).rule, "keyword:pay")
        self.assertEqual(asks.classify("Which option?", ["Security"]).rule, "label:security")
        self.assertEqual(asks.classify("Which option?", []).rule, "no hard rule matched")

    def test_lists_come_from_the_config_and_can_be_replaced(self):
        self.assertTrue(asks.classify("ship the codename", [], ["codename"], ["x"]).hard)
        self.assertFalse(asks.classify("pay the vendor", [], ["codename"], ["x"]).hard)
        self.assertTrue(asks.classify("Which?", ["x"], ["codename"], ["x"]).hard)

    def test_blocking_question_is_read_from_the_newest_ask(self):
        comments = [{"body": "❓ Blocking @person: old one"},
                    {"body": "notes\n❓ **Blocking @anna**: which colour?\nmore context"}]
        self.assertEqual(asks.blocking_question(comments), ("anna", "which colour? more context"))
        self.assertEqual(asks.blocking_question([{"body": "nothing"}]), ("", ""))


class ConfigFlags(unittest.TestCase):
    def load(self, extra=""):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        key = Path(d.name) / "k"
        key.write_text("secret")
        key.chmod(0o600)
        p = Path(d.name) / "runner.toml"
        p.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{d.name}/s"\n{extra}\n'
                     f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{d.name}"\n')
        return config_mod.load(p), p

    def test_defaults(self):
        cfg, _ = self.load()
        self.assertTrue(cfg.unanswerable_ask_to_lead)
        self.assertEqual(cfg.hard_ask_labels, asks.DEFAULT_HARD_LABELS)
        self.assertEqual(cfg.hard_ask_patterns, asks.DEFAULT_HARD_PATTERNS)
        self.assertEqual(cfg.unanswerable_users, ())
        self.assertEqual(cfg.agents[0].pool, "")

    def test_default_state_dir_keeps_a_legacy_home_directory(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            self.assertTrue(config_mod._default_state_dir().endswith("/.local/state/mesh-runner"))
            os.mkdir(os.path.join(home, ".mesh-runner"))
            self.assertEqual(config_mod._default_state_dir(), os.path.join(home, ".mesh-runner"))

    def test_the_retired_lists_are_ignored(self):
        # hard_ask_patterns, hard_ask_labels and unanswerable_users are no longer [runner] options
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            cfg, _ = self.load('hard_ask_patterns = ["codename"]\nhard_ask_labels = ["x"]\n'
                               'unanswerable_users = ["Example"]\n')
        self.assertEqual((cfg.hard_ask_patterns, cfg.hard_ask_labels, cfg.unanswerable_users),
                         (asks.DEFAULT_HARD_PATTERNS, asks.DEFAULT_HARD_LABELS, ()))
        self.assertEqual(len([m for m in logs.output if "is no longer an option" in m]), 3)

    def test_a_bad_value_of_a_retired_list_no_longer_fails_the_load(self):
        with self.assertLogs("mesh_runner", "WARNING"):
            cfg, _ = self.load('hard_ask_patterns = ["(unclosed"]\nhard_ask_labels = "money"\n')
        self.assertEqual(cfg.hard_ask_patterns, asks.DEFAULT_HARD_PATTERNS)


class FakeReader:
    """Only the GET surface: any write would raise AttributeError."""

    def __init__(self, gated, comments):
        self.gated, self.comment_rows, self.calls = gated, comments, []

    def my_tasks(self, cat):
        self.calls.append(("GET", cat))
        return [dict(t) for t in self.gated if t["_cat"] == cat]

    def comments(self, tid):
        self.calls.append(("GET", tid))
        return self.comment_rows.get(tid, [])


class GateClass(unittest.TestCase):
    """asks.gate_class is the class Mesh arms a marker gate with (server markerGateClass,
    internal/service/human_gate_ask_class.go); the cases mirror that file's test tables."""

    def test_live_questions(self):
        cases = [
            ("Утверждаешь план пяти витрин (Stage 1)?", "да, Local Sync с текущим H1", False, "no hard rule matched"),
            ("Отправлять КП Галине (письмо от вашего имени, код в письме) или закрыть заявку без отправки?",
             "закрыть", True, "keyword:кп"),
            ("Одобряете этот текст для WhatsApp при недозвоне и берёте первый звонок Анне?", "да", True,
             "keyword:whatsapp"),
            ("Выставить счет", "да", True, "keyword:счет"),
            ("Ценности команды описать так?", "да", False, "no hard rule matched"),
            ("Repay the tech debt first or ship?", "ship", False, "no hard rule matched"),
        ]
        for question, default, hard, rule in cases:
            with self.subTest(question=question):
                v = asks.gate_class(question, [], default)
                self.assertEqual((v.hard, v.rule), (hard, rule))

    def test_tags_and_defaults(self):
        self.assertEqual(asks.gate_class("Which layout? [hard]", [], "left"), asks.Verdict(True, "tag:hard"))
        self.assertEqual(asks.gate_class("Pay the invoice now? [soft]", [], "Monday"), asks.Verdict(True, "keyword:pay"))
        self.assertEqual(asks.gate_class("Which layout? [soft]", [], ""), asks.Verdict(False, "tag:soft"))
        self.assertEqual(asks.gate_class("Which layout?", [], ""),
                         asks.Verdict(True, "no default stated by the asker"))
        self.assertEqual(asks.gate_class("Which layout?", [], asks.MARKER_FALLBACK_DEFAULT),
                         asks.Verdict(True, "no default stated by the asker"))
        self.assertEqual(asks.gate_class("Which layout?", ["Money"], "left"), asks.Verdict(True, "label:money"))

    def test_gate_class_reads_the_default_with_the_question(self):
        # Without a marker the audit classifies the held gate_reason and recommended_default
        # apart; what applies at the deadline is the default, so its money words count too.
        self.assertEqual(asks.gate_class("Включаем канал бота обратно?", [], "да, оплатить и включить"),
                         asks.Verdict(True, "keyword:оплат"))
        self.assertEqual(asks.gate_class("Включаем канал бота обратно?", [], "да, включить"),
                         asks.Verdict(False, "no hard rule matched"))
        # A live gate's question verbatim: hard on the question alone, with a default that names
        # no money, and with the default the live gate carried.
        q = ("Пополнить общий баланс на $20 через https://openrouter.ai/settings/credits или оставить "
             "этот канал отключённым?")
        self.assertEqual(asks.gate_class(q, [], "да"), asks.Verdict(True, "keyword:пополн"))
        self.assertEqual(asks.gate_class(q, [], "да, пополнить"), asks.Verdict(True, "keyword:пополн"))
        # An amount spelled with a currency word is money too, while a time window is not.
        self.assertEqual(asks.gate_class("Закинуть 20 долларов на OpenRouter?", [], "да"),
                         asks.Verdict(True, "keyword:закинуть 2"))
        self.assertEqual(asks.gate_class("Потратить 50 евро на домен?", [], "да"),
                         asks.Verdict(True, "keyword:евро"))
        self.assertEqual(asks.gate_class("Оставить окно 24 часа для мягких вопросов?", [], "да"),
                         asks.Verdict(False, "no hard rule matched"))
        self.assertEqual(asks.gate_class("Перенести сервер в европейский регион?", [], "да"),
                         asks.Verdict(False, "no hard rule matched"))
        # A missing question fails closed even with a stated default that would read soft.
        for missing in ("", "   ", None, "[soft]"):
            self.assertEqual(asks.gate_class(missing, [], "да, включить"),
                             asks.Verdict(True, "unsure: no question text"))


class SharedFixture(unittest.TestCase):
    """tests/fixtures/ask_class_cases.json is byte-identical to the Mesh server's
    internal/service/testdata/ask_class_cases.json, which the server's markerGateClass test
    runs: one question set, one verdict on both sides."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / "fixtures" / "ask_class_cases.json"
        cls.fx = json.loads(path.read_text(encoding="utf-8"))

    def test_rule_lists_match(self):
        self.assertEqual(list(asks.DEFAULT_HARD_PATTERNS), self.fx["hard_patterns"],
                         "update the shared fixture here and in evc-mesh together")
        self.assertEqual(list(asks.DEFAULT_HARD_LABELS), self.fx["hard_labels"])
        self.assertEqual(asks.MARKER_FALLBACK_DEFAULT, self.fx["fallback_default"])

    def test_cases(self):
        self.assertTrue(self.fx["cases"])
        for tc in self.fx["cases"]:
            with self.subTest(case=tc["name"]):
                self.assertEqual(asks.marker_default(tc["body"], tc["slug"]), tc["default"])
                v = asks.marker_gate_class(tc["body"], tc["slug"], tc["labels"])
                self.assertEqual(("hard" if v.hard else "soft", v.rule), (tc["class"], tc["rule"]))
                handle, body = asks.blocking_ask([{"body": tc["body"]}])
                self.assertEqual((handle, body), (tc["slug"], tc["body"]))


class Audit(unittest.TestCase):
    def rows(self):
        pay = dict(task(1, human_gate=True), title="Pay for the domain", _cat="todo")
        design = dict(task(2, human_gate=True), title="Settings page", _cat="review",
                      human_gate_class="hard", recommended_default="Layout A")
        labelled = dict(task(3, human_gate=True, labels=["legal"]), title="Terms", _cat="todo")
        free = dict(task(4), title="No gate", _cat="todo")
        held_soft = dict(task(5, human_gate=True), title="Plan", _cat="todo",
                         human_gate_class="soft", recommended_default="ship it")
        no_default = dict(task(6, human_gate=True), title="Copy", _cat="todo", human_gate_class="hard",
                          recommended_default=asks.MARKER_FALLBACK_DEFAULT)
        comments = {pay["id"]: [{"body": "❓ Blocking @person: Should we pay for the annual plan?"}],
                    design["id"]: [{"body": "❓ Blocking @person: Layout A or B?\nDefault: Layout A"}],
                    labelled["id"]: [{"body": "❓ Blocking @person: Which wording?"}],
                    held_soft["id"]: [{"body": "❓ Blocking @person: Approve the plan?\nDefault: ship it"}],
                    no_default["id"]: [{"body": "❓ Blocking @person: Which headline?"}]}
        reader = FakeReader([pay, design, labelled, free, held_soft, no_default], comments)
        cfg = mock.Mock(agents=[SimpleNamespace(name="x"), SimpleNamespace(name="y")], mesh_url="u", mesh_basic_auth="",
                        hard_ask_patterns=asks.DEFAULT_HARD_PATTERNS, hard_ask_labels=asks.DEFAULT_HARD_LABELS)
        return cfg, reader, ask_audit.open_gates(cfg, client_for=lambda ag: reader)

    def test_each_open_gate_once_with_verdict_and_rule(self):
        _, reader, rows = self.rows()
        by_task = {r["task"]: r for r in rows}
        self.assertEqual(len(rows), 5)                       # the second agent lists the same tasks
        self.assertEqual((by_task["00000001"]["verdict"], by_task["00000001"]["rule"]), ("hard", "keyword:pay"))
        # the rule calls it soft but Mesh holds it hard: the count the owner wants at zero
        self.assertEqual((by_task["00000002"]["verdict"], by_task["00000002"]["rule"], by_task["00000002"]["held"]),
                         ("soft", "no hard rule matched", "hard"))
        self.assertEqual((by_task["00000003"]["verdict"], by_task["00000003"]["rule"]), ("hard", "label:legal"))
        self.assertEqual((by_task["00000005"]["verdict"], by_task["00000005"]["held"]), ("auto", "soft"))
        self.assertEqual((by_task["00000006"]["verdict"], by_task["00000006"]["rule"]),
                         ("hard", "no default stated by the asker"))
        self.assertEqual(ask_audit.summary(rows), {"gates": 5, "hard": 3, "soft": 1, "auto": 1})
        self.assertTrue(all(c[0] == "GET" for c in reader.calls))

    def test_cli_prints_the_table_and_totals_and_does_not_write(self):
        cfg, _, rows = self.rows()
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / "k"
            key.write_text("secret")
            key.chmod(0o600)
            conf = Path(d) / "r.toml"
            conf.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{d}/s"\n'
                            f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{d}"\n')
            out = io.StringIO()
            with mock.patch.object(ask_audit, "open_gates", return_value=rows), redirect_stdout(out):
                self.assertEqual(cli.main(["asks", "classify", "--config", str(conf)]), 0)
            text = out.getvalue()
            self.assertIn("keyword:pay", text)
            self.assertIn("total 5: hard 3, soft 1, auto 1", text)
            out = io.StringIO()
            with mock.patch.object(ask_audit, "open_gates", return_value=rows), redirect_stdout(out):
                cli.main(["asks", "classify", "--config", str(conf), "--json"])
            self.assertEqual(json.loads(out.getvalue())["summary"]["soft"], 1)
            self.assertNotIn("secret", out.getvalue())

    def test_an_unreadable_comment_list_is_hard(self):
        from mesh_runner.mesh import MeshError

        class Broken(FakeReader):
            def comments(self, tid):
                raise MeshError(503, "down")
        gated = [dict(task(1, human_gate=True), _cat="todo")]
        cfg = mock.Mock(agents=[SimpleNamespace(name="x")], mesh_url="u", mesh_basic_auth="",
                        hard_ask_patterns=asks.DEFAULT_HARD_PATTERNS, hard_ask_labels=asks.DEFAULT_HARD_LABELS)
        rows = ask_audit.open_gates(cfg, client_for=lambda ag: Broken(gated, {}))
        self.assertEqual((rows[0]["verdict"], rows[0]["rule"]), ("hard", "unsure: comments unreadable"))
        self.assertIn("errors", rows[-1])


if __name__ == "__main__":
    unittest.main()
