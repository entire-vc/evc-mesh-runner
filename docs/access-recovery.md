
# Authenticated capability recovery

This source protocol is opt-in through the trusted `start_gate_command`. No
installed runtime or operator policy is enabled by adding this code. A legacy
hook without structured output keeps its existing behavior.

A task-local exit 76 may emit `ACCESS_HOLD=<JSON>`. The exact v1 fields are
`version`, `task_id`, `lane`, `mesh_actor`, `gitlab_actor`, `project`, `capability`,
`head`, and `route_sha256`. The latter binds the trusted producer's complete
route, including MR, policy and review context. No token or API response belongs
in this record. Runner persists its own random hold epoch in existing SQLite
marks. Task changes, process restarts and time do not invalidate it.

On an explicit credential-refresh or permission-restored notification, the owner
can invoke:

```sh
mesh-runner recheck-access --config /absolute/path/runner.toml --agent exact-lane --task full-task-uuid
```

This command reads the same named lane hook and invokes only `access-recheck`.
The hook must freshly authenticate the own actor and observe its project and
capability, returning `ACCESS_ALLOW` with the identical binding on exit 0.
Generic/unbound routes must not return this proof. The command cannot supply a
proof, choose another actor or select an arbitrary event destination. It sends a
signed `access.restored` v1 event to the configured loopback `/mesh/events`, using
the existing installed extension secret. The consumer verifies workspace,
freshness, exact binding/epoch and a persistent single-use event UUID, invokes
the own hook again, and CAS-clears only the original hold. UNKNOWN or denial
retains it. A successful response reports SQLite readback, not merely HTTP 202.

A wake only schedules normal admission; it never creates a run, reserves
capacity, bypasses a fresh ledger or grants GitLab permissions. No timer invokes
this command. There is no new event bus or cache. The existing extension must
already be installed; this command cannot install or rekey it.

A compatible external gate hook is required for explicitly configured
v2 independent-account publication routes. It must implement the hold/recheck
protocol described above; Runner installation does not supply this hook or enable
an owner configuration. Other provider-auth, quota and filesystem-permission
holds keep their existing mechanisms. A changed review route/policy/context
intentionally cannot release an older hold: the route owner must reconcile that
provenance; this command does not rewrite it. Rolling back to the previous Runner
leaves the new mark keys inert and preserves their data.

Operators must separately pin and review the external hook and permission-grant
integration source, including cross-component contract tests, and verify their
activation alongside the accepted Runner release. A source hash alone does not
prove activation. No specific external implementation is distributed here.

The explicit producer authenticates its selected lane via the normal Mesh
`GET /agents/me` before invoking its hook, and requires that identity to match
the persisted hold. Failed or mismatched identity retains the epoch without
executing the capability probe. It does not run a normal agent tick or adopt a
session to obtain identity.


## Native permission-grant notifier

Automatic permission-grant notification requires an external grant wrapper;
Runner does not install one. Such an integration must perform a post-success
effective-role readback bound to the native user ID and canonical project path,
then invoke the real Runner command on the owner host:

```sh
mesh-runner notify-access-change --config /absolute/path/runner.toml --gitlab-actor 123 --project group/project
```

Here `123` and `group/project` are example values to replace with the authenticated
native user ID and canonical project path. An external wrapper can use the
following local configuration contract at
`~/.config/mesh-runner/access-recovery-owner.json` (this is an integration
example, not a configuration file read by Runner itself):

```json
{"version":1,"runner_config":"/absolute/local/runner.toml","runner_command":["/absolute/python3.12","-m","mesh_runner.cli"],"runner_pythonpath":"/absolute/installed-release/src"}
```

The config contains only local paths and the explicit module invocation. These
must select the same accepted release and config as the local running Runner; no SSH hop or remote owner is inferred. A standalone installed executable can use a one-item `runner_command` and empty
`runner_pythonpath`. In a wrapper implementing this example, per-call `--runner-config` and
`--runner-bin` may select that executable form; these are wrapper options, not
Runner CLI options. The only environment override is named `PYTHONPATH`; no
credential environment is configured or emitted. A malformed config
refuses before the grant. Missing/unavailable owner or failed own probes is
reported separately as `access_recovery.status=notification_pending`: the grant
may already have succeeded and must not be blindly repeated to repair delivery.
An already-satisfied grant still has an authenticated role readback and can notify.

The notifier enumerates configured lanes' existing durable holds only, selecting
exact native actor/project and `independent-account-note-label/v1`. It does not
create a hold or task, and refuses more than 16 matching holds. It calls the
existing explicit producer for each selected hold and reports selected/released
counts. A zero-match acknowledgement means no eligible hold, not a wake. An integration following this contract must impose a 300-second bounded
delivery timeout; timeout is pending, not proof
of rollback or release. No timer retries it. After delivery failure the owner can
invoke the notifier directly for the exact actor/project, without reissuing a grant.

This covers only grants through that configured native wrapper, not arbitrary
GitLab web/API mutations or Mesh membership changes. Key writes and key-issued
or revoked events are deliberately unconnected: they cannot prove that the
running Runner adopted a credential. Existing signed ingress, fresh own checks,
replay rejection and exact epoch CAS remain unchanged.
