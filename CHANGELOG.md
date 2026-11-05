
# Changelog

## Unreleased

## 0.1.37

- Keep provider quota within account admission; reject quota-driven task schedules and preserve same-session recovery.
- Require fresh capacity confirmation for reserve and admit, then launch with the selected account environment.

## 0.1.36

- Enrolled reviewer-receipt source for the native result channel (merged to main after 0.1.35 was published) is now
  packaged; retained controls pin the exact A1-issued packet vector. Receipt delivery is data, never admission.

## 0.1.35

- A chat turn that fails before its native process starts now leaves a durable, secret-free diagnosis (runner log and
  a separate durable diagnostics file): stage, exception class, an allowlisted reason (env command/file failure, barrier refusal,
  workspace error, OS error) and the own-package file/function/line. No exception text, traceback, environment,
  arguments, prompt or full paths. Terminal error codes, guards and retry are unchanged.
- Provider-qualified `mr:` / `pipeline:` waits: `mr:github:org/repo!N`, `mr:gitlab:group/project!N`,
  `pipeline:github:org/repo#N` (`mr:github:` supports plain and `until=merged`; its `until=change|success` is refused, use `pipeline:github:…#run`). An explicit
  provider always wins over the `[github]` allowlist, so a GitHub repo no longer has to be listed (and thereby capture the
  same-path GitLab project); it is never read from the other provider, and a provider that is not configured fails closed.
  Observation/dedupe keys carry the provider. Bare targets keep the allowlist inference. `merge:` has no qualified form
  and is rejected with the supported spelling. `[github]` with only a token now builds the GitHub reader.
- `host_local_qa_assignment_v1`: the frozen brief/policy/schema bytes are no longer packaged. They stay private and are
  read only from the explicit local opt-in `bundle_dir`, verified against the unchanged sha256 pins before any registration,
  credential read or state change (fail closed); absent/empty/`enabled = false` still never reads them. The disable path
  (`qa-revoke`, `revoke`, `status`) works without the bundle. Only `conformance.json` ships.

## 0.1.34

- Add default-OFF `host_local_qa_assignment_v1` to the existing-session receiver: a separate opt-in section, scoped
  credential, server-side admission and routes (`/runner/host-local-qa/v1/...`), a strict sender envelope pinned to the
  frozen `schema.json`/`policy.json`/`brief.txt` bytes, an authoritative backend `/check` before the paste and again before
  the Enter (iTerm paste is split into stage/submit), and a host-local guard (`session-receiver qa-check`) for the operation
  owner. One lock, journal and injector with the metadata receiver; legacy routes, admission and ledger are unchanged.
  See `docs/host-local-qa-assignment.md`.

## 0.1.33

- Add opt-in `stall_wait_v1`: `⏳ WAIT stall:#<this-card-8hex>` on an operator-armed registration
  (`mesh-runner stall-wait arm|inspect|disarm`) sleeps until the first NEW own-author
  `[mesh-stall-control:<uuid>]` result comment on that card, with a durable baseline and exactly one
  wake of the same session. See `docs/stall-wait.md`.

## 0.1.32

- Add opt-in `recovery_wait_v1`: `⏳ WAIT recovery:#<this-card-8hex>` on an operator-armed registration
  of a FUTURE shared recovery card (marker + project + recovery owner + receipt author). The registration
  may precede or follow the card; Runner reconciles the actual Mesh cards, binds one, and delivers one
  wake when it is `done` and carries a `carrier-receipt: <ref>` comment by the registered author. Evidence
  only, never a security clearance. See `docs/recovery-wait.md`.

## 0.1.31

- Add opt-in `lifecycle_wait_v1`: one bounded wait on the first new, correlated native SessionStart
  lifecycle event (startup/resume/compact, plus hook failure) of the implementation-pinned profile (`lifecycle_wait.PROFILE`), with durable
  cursors, dedup and owner/generation-checked delivery. Disabled until an operator arms it.

## 0.1.30

- Add opt-in durable native-result delivery with enrolled source lineage, exact receiver ACK,
  restart-safe cursors, explicit terminal ownership and guarded receiver rebinding.
- Keep native-result observation disabled until an operator enrolls the source and receiver.

## 0.1.29

- Durable launch intent: the exact prompt and refs to mandatory events are journaled and persisted before any native
  process exists (journal first, then the DB intent). PID, birth and turn are registered before stdin; a CAS or commit
  refusal keeps the native behind the EOF gate. Queues and wake are consumed only on a matched acknowledgement.
- A native prompt accepted before the driver died is recovered from native hooks/transcript bound to session, turn and
  the exact prompt (exact text compare: paste wrapper, line endings and edges only). No new paste, no second writer;
  an unprovable state stays UNKNOWN (`prompt_delivery_unknown`) with a resolution owner. A fresh Codex session, which
  has no id before its first input, is bound to the journal at the exact native acceptance.
- Equivalent headless contract for both harnesses (`headless_acceptance`, `spawn_gate`), `mesh-runner recheck-delivery`.
- Upgrade/rollback guard `mesh-runner-guarded-launch` (`launch_guard`): the service must start through it; it refuses a
  target without the capability and any target/config/tree change while a pending intent or UNKNOWN row exists.
  README "Updating the runner" lists the supported launch path; an unguarded old binary is unsupported.

## 0.1.28

- `reviewer-merge --apply`: the merge-only card is created with the live Todo `status_id` read from the project's
  statuses (every status row is validated: id, known category; any malformed row refuses before the POST). The status
  list is read only on the create path, so a repeat apply or a repair of an existing card does not depend on it.
  New in this version: `docs/reviewer-merge.md` "Positive check after install" (dry-run, apply, same-id repeat).

## 0.1.27

- `archive_root` / `archive_require_mount`: slot archives go to an explicit root on a mounted volume (fail closed
  when the volume is not the mounted one, no fallback to the internal disk); source files move by same-device atomic
  rename into a durable quarantine with manifest and restore, no auto-purge. See README "External archive root".

## 0.1.26

- `session-receiver`: opt-in `transport = "iterm"` for a native producer already running in iTerm2: exact terminal session
  (`unique id` + tty) and producer pid/start/foreground group proved before each injection, bound with host/runner/session
  ids into the ledger pin and admission record; fail closed, one injector, tmux unchanged. See `docs/session-receiver.md`.

## 0.1.25

- `mesh-runner session-receiver`: the existing-session metadata receiver as a separate opt-in process (pinned producer,
  shared ledger and lock with the controller receiver, fail closed). See `docs/session-receiver.md`.
- `session-receiver revoke`: disable/rollback revokes only this receiver's own recorded admission (`DELETE /receivers/:id`),
  fail closed on unknown/mismatched identity, never reports a failed revoke as success; ledger and producer untouched.

## 0.1.24

- WAIT observation runs under one shared deadline (`wake_budget_seconds`, default 45); a failed read keeps
  the wait untouched and records `wake_read_failed` with kind, stage, elapsed and retries. See
  `docs/wait-observation-budget.md`.

## 0.1.23

- Repo admission compares the declared agent env, not the session env a turn applied, so a normal turn no longer causes a false `rejected_config`.
- `status.json` carries `receipt`: PID, release, source dir, `status_unix` of the same snapshot and
  `cfg_sha256` over the stable serialization of the in-memory `RunnerConfig` (resolved defaults; fields
  declared `repr=False` and secret-named fields excluded). A disk edit does not move it until the held
  cfg is replaced by a restart; a serialization failure writes `receipt.error` with no digest.
  Scope and reproduction: `docs/config-receipt.md`.

## 0.1.22

- Preserve reviewed WAIT intent and access recovery in one integrated path.
- Hold unlaunchable sessions before spawning them.
- Persist the auth-recovery wake so it survives a restart.
- Resume the native session on a proven same-path workspace restore.

## 0.1.21

- Add the opt-in `receipt_wait_v1` receipt WAIT (`⏳ WAIT receipt:#<card8>`): a durable operator-armed registration with per-journal cursors wakes the card once on the first new accepted correlated Telegram text receipt; `mesh-runner receipt-wait arm|inspect|disarm|rebaseline` (docs/receipt-wait.md).
- Accept Git numeric collision admin names for exactly owned legacy worktree slots while preserving ownership and reciprocal path checks.
- Reconcile newer corroborated owner card WAIT intent without waking a stale target or taking over its dependency.
- Preserve durable capability holds and release them only through authenticated exact-binding access recovery with fresh own probes; support the configured native permission-grant notifier.
