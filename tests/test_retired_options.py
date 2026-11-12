"""Options removed from the loader are ignored when an older config file still carries them, and the
[runner] table keeps a fixed, small set of operator options."""
import dataclasses
import os
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mesh_runner import config as config_mod
from mesh_runner import native_result_config

# removed in 0.2.1: the behaviour each one switched on is unconditional
RETIRED = ("mention_requests", "turn_cap_continue_on_progress", "queue_wait_freshness_v1", "recovery_wait_v1",
           "lifecycle_wait_v1", "stall_wait_v1", "requeue_on_cap", "cap_wake_next_day",
           "soft_person_asks_to_lead", "merge_follow_head", "checkpoint_gc")

# The operator options of [runner]. A new one must be a decision: add it here and stay within the cap.
MAX_RUNNER_OPTIONS = 40
RUNNER_OPTIONS = frozenset("""
announce_controllers archive_require_mount archive_root basic_auth_file cardless_question_lanes comment_wake
comment_wake_always_from daily_next_budget daily_start_cap daily_step_budget daily_wait_budget
decision_request_metadata_only_recipients
gmail_watch_reader gmail_watch_v1 hold_labels interactive_idle_minutes interactive_max_sessions load_hold_per_cpu
load_release_per_cpu login_ask_check max_compactions memory_hold_free_pct memory_hold_swap_mb
memory_release_free_pct memory_release_swap_mb merge_command merge_command_cwd merge_rerun_minutes mesh_url
min_free_gb poll_seconds repo_access_contacts review_own_hold_hours
reviewer_select_command state_dir time_wait_ignore_names tmux_binary turn_first_output_minutes turn_idle_minutes
workspace_wait_release_minutes
""".split())
# Not operator options: the native-result adoption protocol reads and CAS-checks these two keys in the
# file itself (native_result_config.py, docs/native-result-config.md), so the loader keeps reading them.
PROTOCOL_KEYS = frozenset(native_result_config.FIELDS)

RECIPIENT = "66666666-7777-8888-9999-000000000000"


def _settable(tmp: str) -> dict:
    """Every key the loader reads -> (TOML value, RunnerConfig field, value it must land as)."""
    auth = Path(tmp) / "basic.auth"
    auth.write_text("user:pw")
    auth.chmod(0o600)
    return {
        "announce_controllers": ("true", "announce_controllers", True),
        "archive_require_mount": ('"/vol/mnt"', "archive_require_mount", Path("/vol/mnt")),
        "archive_root": ('"/vol/mnt/archive"', "archive_root", Path("/vol/mnt/archive")),
        "basic_auth_file": (f'"{auth}"', "mesh_basic_auth", "user:pw"),
        "cardless_question_lanes": ('["lead"]', "cardless_question_lanes", ["lead"]),
        "comment_wake": ('"addressed"', "comment_wake", "addressed"),
        "comment_wake_always_from": ('["Lead"]', "comment_wake_always_from", ["Lead"]),
        "daily_next_budget": ("7", "daily_next_budget", 7),
        "daily_start_cap": ("2", "daily_start_cap", 2),
        "daily_step_budget": ("25", "daily_step_budget", 25),
        "daily_wait_budget": ("9", "daily_wait_budget", 9),
        "decision_request_metadata_only_recipients": (f'["{RECIPIENT}"]', "decision_request_metadata_only_recipients",
                                                      [RECIPIENT]),
        "gmail_watch_reader": ('["reader", "--json"]', "gmail_watch_reader", ("reader", "--json")),
        "gmail_watch_v1": ("true", "gmail_watch_v1", True),
        "hold_labels": ('["awaiting-lead"]', "hold_labels", ("awaiting-lead",)),
        "interactive_idle_minutes": ("61.5", "interactive_idle_minutes", 61.5),
        "interactive_max_sessions": ("21", "interactive_max_sessions", 21),
        "load_hold_per_cpu": ("5.5", "load_hold_per_cpu", 5.5),
        "load_release_per_cpu": ("1.5", "load_release_per_cpu", 1.5),
        "login_ask_check": ('["check"]', "login_ask_check", ("check",)),
        "repo_access_contacts": ('["infra", "lead"]', "repo_access_contacts", ("infra", "lead")),
        "max_compactions": ("2", "max_compactions", 2),
        "memory_hold_free_pct": ("10", "memory_hold_free_pct", 10.0),
        "memory_hold_swap_mb": ("200", "memory_hold_swap_mb", 200.0),
        "memory_release_free_pct": ("20", "memory_release_free_pct", 20.0),
        "memory_release_swap_mb": ("100", "memory_release_swap_mb", 100.0),
        "merge_command": ('["gate", "{iid}"]', "merge_command", ("gate", "{iid}")),
        "merge_command_cwd": ('"/srv/tools"', "merge_command_cwd", Path("/srv/tools")),
        "merge_rerun_minutes": ("6.5", "merge_rerun_minutes", 6.5),
        "mesh_url": ('"http://mesh.other"', "mesh_url", "http://mesh.other"),
        "min_free_gb": ("1.5", "min_free_gb", 1.5),
        "poll_seconds": ("21", "poll_seconds", 21),
        "review_own_hold_hours": ("2.5", "review_own_hold_hours", 2.5),
        "reviewer_select_command": ('["select", "{author}"]', "reviewer_select_command", ("select", "{author}")),
        "state_dir": (f'"{tmp}/state"', "state_dir", Path(tmp) / "state"),
        "time_wait_ignore_names": ('["nightly"]', "time_wait_ignore_names", ["nightly"]),
        "tmux_binary": ('"/usr/bin/tmux"', "tmux_binary", "/usr/bin/tmux"),
        "turn_first_output_minutes": ("20", "turn_first_output_minutes", 20.0),
        "turn_idle_minutes": ("45", "turn_idle_minutes", 45.0),
        "workspace_wait_release_minutes": ("5", "workspace_wait_release_minutes", 5.0),
        "native_result_dir": ('"/private/native/journal"', "native_result_dir", Path("/private/native/journal")),
        "native_result_channels_dir": ('"/private/native/channels"', "native_result_channels_dir",
                                       Path("/private/native/channels")),
    }


def _other(default) -> str:
    """A TOML value that differs from a field default (the loader must not apply it)."""
    if isinstance(default, bool):
        return "false" if default else "true"
    if isinstance(default, int):
        return str(default + 7)
    if isinstance(default, float):
        return str(default + 7.5)
    if isinstance(default, str):
        return '"something-else"'
    if isinstance(default, tuple):
        return '["something-else"]'
    return '"/somewhere/else"'      # a path that is None by default


class _Recording(dict):
    """The [runner] table that records which keys the loader asks for."""

    def __init__(self, *args):
        super().__init__(*args)
        self.asked = set()

    def get(self, key, *default):
        self.asked.add(key)
        return super().get(key, *default)

    def __getitem__(self, key):
        self.asked.add(key)
        return super().__getitem__(key)


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = d.name
        key = Path(d.name) / "k"
        key.write_text("secret")
        key.chmod(0o600)
        self.key = key

    def load(self, extra=""):
        p = Path(self.tmp) / "runner.toml"
        p.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{self.tmp}/s"\n{extra}\n'
                     f'[[agents]]\nname = "a"\nkey_file = "{self.key}"\nprofile_dir = "{self.tmp}"\n')
        return config_mod.load(p)


class RetiredOptions(Base):
    def test_an_older_file_loads_whatever_the_retired_keys_hold(self):
        plain = self.load("daily_start_cap = 5\n")
        for value in ("true", "false", '"yes"', "7"):
            with self.subTest(value=value):
                cfg = self.load("".join(f"{name} = {value}\n" for name in RETIRED) + "daily_start_cap = 5\n")
                self.assertEqual(dataclasses.replace(cfg, state_dir=plain.state_dir, agents=plain.agents,
                                                     config_path=plain.config_path), plain)

    def test_the_loader_has_no_such_fields(self):
        fields = {f.name for f in dataclasses.fields(config_mod.RunnerConfig)}
        self.assertEqual(fields & set(RETIRED), set())


class RunnerOptions(Base):
    """The [runner] keys the loader reads are exactly RUNNER_OPTIONS (+ the two protocol keys)."""

    def full(self) -> tuple:
        values = _settable(self.tmp)
        text = "[runner]\n" + "".join(f"{k} = {v[0]}\n" for k, v in values.items())
        text += f'[[agents]]\nname = "a"\nkey_file = "{self.key}"\nprofile_dir = "{self.tmp}"\n'
        p = Path(self.tmp) / "full.toml"
        p.write_text(text)
        return p, values

    def test_the_keys_the_loader_asks_for_are_exactly_the_options(self):
        p, _ = self.full()
        # also carry every retired key: the loader must not ask for any of them
        p.write_text(p.read_text().replace("[[agents]]", "".join(
            f"{k} = 1\n" for k in config_mod.RETIRED_RUNNER_OPTIONS) + "[[agents]]", 1))
        tables = []

        def loads(text):
            raw = tomllib.loads(text)
            raw["runner"] = _Recording(raw["runner"])
            tables.append(raw["runner"])
            return raw

        with mock.patch.object(config_mod, "tomllib", SimpleNamespace(loads=loads)), \
                self.assertLogs("mesh_runner", "WARNING"):
            config_mod.load(p)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].asked, RUNNER_OPTIONS | PROTOCOL_KEYS)

    def test_the_source_reads_no_other_runner_key(self):
        source = Path(config_mod.__file__).read_text()
        read = set(re.findall(r'\br\.get\("([a-z0-9_]+)"', source)) | set(re.findall(r'\br\["([a-z0-9_]+)"\]', source))
        self.assertEqual(read, RUNNER_OPTIONS | PROTOCOL_KEYS)

    def test_every_option_lands_on_its_field(self):
        p, values = self.full()
        cfg = config_mod.load(p)
        defaults = config_mod.RunnerConfig(mesh_url="", state_dir=Path(), agents=[])
        self.assertEqual(set(values), RUNNER_OPTIONS | PROTOCOL_KEYS)
        for key, (_, name, want) in values.items():
            with self.subTest(key=key):
                self.assertEqual(getattr(cfg, name), want)
                if key not in ("mesh_url", "state_dir"):     # required / derived: no dataclass default
                    self.assertNotEqual(getattr(defaults, name), want, "the test value must not be the default")

    def test_the_option_count_is_capped(self):
        self.assertLessEqual(len(RUNNER_OPTIONS), MAX_RUNNER_OPTIONS)
        self.assertEqual(len(RUNNER_OPTIONS), 40)

    def test_retired_and_settable_do_not_overlap(self):
        retired = set(config_mod.RETIRED_RUNNER_OPTIONS)
        self.assertEqual(len(retired), 63)
        self.assertEqual(retired & (RUNNER_OPTIONS | PROTOCOL_KEYS), set())
        fields = {f.name for f in dataclasses.fields(config_mod.RunnerConfig)}
        # Most retired keys keep fixed fields; removed observers have no runtime fields.
        self.assertEqual(retired - fields, {"basic_auth_env", "ops_review_wait_registry",
                                          "ops_review_wait_notifier_id"})


class RetiredRunnerOptions(Base):
    def test_each_retired_key_loads_warns_once_and_keeps_its_fixed_value(self):
        plain = self.load()
        defaults = {f.name: f.default for f in dataclasses.fields(config_mod.RunnerConfig)}
        for key, reason in sorted(config_mod.RETIRED_RUNNER_OPTIONS.items()):
            if key not in defaults:
                continue
            with self.subTest(key=key):
                with self.assertLogs("mesh_runner", "WARNING") as logs:
                    cfg = self.load(f"{key} = {_other(defaults[key])}\n")
                warned = [m for m in logs.output if "no longer an option" in m]
                self.assertEqual(warned, [f"WARNING:mesh_runner:[runner] {key} is no longer an option and is "
                                          f"ignored ({reason})"])
                self.assertEqual(getattr(cfg, key), defaults[key])
                self.assertEqual(getattr(cfg, key), getattr(plain, key))
                # nothing else changed either
                self.assertEqual(dataclasses.replace(cfg, config_path=plain.config_path), plain)

    def test_a_retired_key_with_a_value_its_old_validator_refused_still_loads(self):
        for line in ('loop_streak_limit = -1', 'dead_slot_hours = -1', 'launch_mode = "tui"',
                     'checkpoint_exclude_names = [".git"]', 'review_verdict_pattern = "("',
                     'merge_max_runs = 0', 'wake_budget_seconds = 0',
                     'ops_review_wait_registry = "relative"', 'ops_review_wait_notifier_id = false'):
            with self.subTest(line=line), self.assertLogs("mesh_runner", "WARNING"):
                self.load(line + "\n")

    def test_removed_ops_review_fields_warn_and_ignore_arbitrary_values(self):
        plain = self.load()
        for value in ('true', 'false', '7', '"relative"', '[]', '{}'):
            for key in ('ops_review_wait_registry', 'ops_review_wait_notifier_id'):
                with self.subTest(key=key, value=value), self.assertLogs("mesh_runner", "WARNING") as logs:
                    cfg = self.load(f"{key} = {value}\n")
                self.assertEqual(len(logs.output), 1)
                self.assertIn(f"{key} is no longer an option and is ignored", logs.output[0])
                self.assertFalse(hasattr(cfg, key))
                self.assertEqual(dataclasses.replace(cfg, config_path=plain.config_path), plain)

    def test_basic_auth_env_is_ignored(self):
        with mock.patch.dict(os.environ, {"MESH_BASIC_FOR_TEST": "user:pw"}):
            with self.assertLogs("mesh_runner", "WARNING") as logs:
                cfg = self.load('basic_auth_env = "MESH_BASIC_FOR_TEST"\n')
        self.assertEqual(cfg.mesh_basic_auth, "")
        self.assertIn("[runner] basic_auth_env is no longer an option and is ignored (set basic_auth_file instead)",
                      "\n".join(logs.output))

    def test_a_file_without_retired_keys_logs_nothing_about_them(self):
        with self.assertNoLogs("mesh_runner", "WARNING"):
            self.load("daily_start_cap = 5\n")

    def test_all_retired_keys_at_once_warn_once_each(self):
        defaults = {f.name: f.default for f in dataclasses.fields(config_mod.RunnerConfig)}
        text = "".join(f"{k} = {_other(defaults.get(k, ''))}\n" for k in config_mod.RETIRED_RUNNER_OPTIONS)
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            cfg = self.load(text)
        warned = [m for m in logs.output if "no longer an option" in m]
        self.assertEqual(len(warned), len(config_mod.RETIRED_RUNNER_OPTIONS))
        self.assertEqual(dataclasses.replace(cfg, config_path=self.load().config_path), self.load())


if __name__ == "__main__":
    unittest.main()
