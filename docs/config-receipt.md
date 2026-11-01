# Config receipt in status.json

`status.json` (`<state_dir>/status.json`, written atomically every tick) has a `receipt` object:

| field | meaning |
|---|---|
| `pid`, `started_unix` | process that produced the snapshot and its start |
| `release`, `source` | `mesh_runner.__version__` and the directory the running code was imported from |
| `status_unix` | equals the snapshot's `unix`: the receipt belongs to that completed snapshot |
| `cfg_sha256` | sha256 of the stable JSON (sorted keys, compact) of the in-memory `RunnerConfig` |
| `scope` | text of what the digest covers |
| `error` | only on a failure; then `cfg_sha256` is absent |

Scope: every dataclass field of `RunnerConfig` and nested `AgentConfig`/`RepoSpec`/`Policy`, after
`load()` resolved defaults and overrides. Excluded: fields declared `repr=False` (agent keys, tokens,
env containers, basic auth, `source`) and fields whose name looks like a secret. Opaque objects
contribute their type name only.

Reload: the Runner has no hot reload of the held cfg; a restart (launchd) replaces it. Editing
`runner.toml` on disk changes nothing in the receipt until then, so a consumer compares
`cfg_sha256` with the digest of the cfg it expects (`config_receipt.digest(load(path))`), never with a
disk read alone. A consumer must also check `status_unix` is fresh and `pid` is the live unit.
