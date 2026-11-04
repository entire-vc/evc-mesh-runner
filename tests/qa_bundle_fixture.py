"""A SYNTHETIC frozen bundle for the public suite. The real brief/policy/schema are private and never packaged; the code is
tested against a bundle of the same shape with invented values and its own pins (``use()`` swaps ``qa.PINS`` for the test run,
the production pins stay literal in ``qa_assignment.py`` and a separate test asserts them)."""
import hashlib
import json
import tempfile
from pathlib import Path

UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
SENDER = "dddddddd-0000-4000-8000-00000000000d"
AGENT = SENDER
WORKSPACE = "eeeeeeee-0000-4000-8000-00000000000e"
HOST = "qa-host.invalid"
BRIEF = ("Synthetic QA assignment for the public test suite.\nOnly the existing admitted producer on qa-host.invalid may "
         "consume this.\nNothing here is executed.\n")


def _s(n=128):
    return {"maxLength": n, "minLength": 1, "type": "string"}


def build(dest: Path) -> dict:
    """Write brief.txt / policy.json / schema.json into ``dest`` and return their sha256 pins."""
    text_sha = hashlib.sha256(BRIEF.encode()).hexdigest()
    policy = {"agent_id": AGENT, "allowed_sender_agent_ids": [SENDER], "artifact_id": 7, "candidate": "a" * 40,
              "capability": "host_local_qa_assignment_v1", "default_enabled": False, "hostname": HOST,
              "max_expiry_seconds": 28800, "operation_manifest_sha256": "b" * 64, "qa_package_sha256": "c" * 64,
              "schema_version": 1, "source_task_id": "ffffffff-0000-4000-8000-00000000000f", "text_sha256": text_sha,
              "workspace_id": WORKSPACE}
    pol_raw = json.dumps(policy, indent=2, sort_keys=True).encode() + b"\n"
    pol_sha = hashlib.sha256(pol_raw).hexdigest()
    const = lambda v: {"const": v}
    props = {
        "artifact_id": const(7), "candidate": const("a" * 40), "capability": const("host_local_qa_assignment_v1"),
        "expected_admission_revision": {"minimum": 1, "type": "integer"}, "expires": {"minimum": 1, "type": "integer"},
        "operation_manifest_sha256": const("b" * 64), "policy_sha256": const(pol_sha),
        "producer": {"additionalProperties": False, "type": "object",
                     "properties": {"producer_pid": {"minimum": 1, "type": "integer"}, "producer_started": _s(),
                                    "terminal_session_id": _s(), "transport": const("iterm"),
                                    "tty": {"pattern": "^/dev/ttys[0-9A-Za-z]{1,16}$", "type": "string"}},
                     "required": ["producer_pid", "producer_started", "terminal_session_id", "transport", "tty"]},
        "qa_package_sha256": const("c" * 64), "receiver_id": {"pattern": UUID, "type": "string"},
        "request_id": {"pattern": UUID, "type": "string"}, "schema_version": const(1),
        "source_task_id": const(policy["source_task_id"]),
        "target": {"additionalProperties": False, "type": "object",
                   "properties": {"agent_id": const(AGENT), "host_id": _s(), "hostname": const(HOST),
                                  "runner_id": {"pattern": UUID, "type": "string"}, "session_id": _s(),
                                  "workspace_id": const(WORKSPACE)},
                   "required": ["agent_id", "host_id", "hostname", "runner_id", "session_id", "workspace_id"]},
        "text": const(BRIEF), "text_sha256": const(text_sha)}
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "additionalProperties": False, "properties": props,
              "required": sorted(props), "title": "synthetic sender envelope", "type": "object"}
    sch_raw = json.dumps(schema, indent=2, sort_keys=True).encode() + b"\n"
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in (("brief.txt", BRIEF.encode()), ("policy.json", pol_raw), ("schema.json", sch_raw)):
        (dest / name).write_bytes(data)
    return {"brief.txt": text_sha, "policy.json": pol_sha, "schema.json": hashlib.sha256(sch_raw).hexdigest()}


_dir = tempfile.TemporaryDirectory(prefix="qa-synthetic-bundle-")
DIR = Path(_dir.name) / "bundle"
PINS = build(DIR)


def use(qa) -> None:
    """Point ``qa`` at the synthetic bundle with the synthetic pins (test process only)."""
    qa.PINS = PINS
    qa.configure_bundle(DIR)
