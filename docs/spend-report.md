# Spend report

After each turn the runner reports the turn's spend on the agent's Mesh record
(`POST /agents/me/sessions/report`, `[runner] session_reports`, on by default): `model`, `tokens_in`,
`tokens_out` and `estimated_cost`, the provider's price or else the model's list tariff (`[prices]`).
A measure the turn has no figure for is named in `unknown`, never sent as 0. The report is keyed by its
turn (`<attempt>:<turn>`, see [repeat-safe writes](idempotent-writes.md)).

## Turn fields

```toml
[spend_report]
turn_fields = true            # default false

[spend_report.account_refs]   # optional
"/var/lib/codex/ops" = "codex/ops"
```

With `turn_fields = true` the report of a turn also carries:

| field | value |
|---|---|
| `turn_id` | the turn's report id, the same on every resend |
| `started_at` | when the runner launched the turn (RFC 3339, UTC) |
| `ended_at` | the last write of the turn's log |
| `provider` | `anthropic`, `openai`, `zai`, `moonshot`: from the model the harness recorded, else from a harness that talks to one provider only (Codex, Kimi, ZCode) |
| `account_ref` | the account of the turn's login, `<harness>/<account>` |
| `billing_mode` | `subscription` for a turn on a login, `api` for a turn on an API key |
| `cache_read`, `cache_write` | the parts of `tokens_in` read from and written to the cache |
| `cache_write_1h` | the part of `cache_write` written with the 1 h lifetime |

`tokens_in` keeps its meaning: the whole input, cache included, so the input outside the cache is
`tokens_in − cache_read − cache_write`. Claude's figures are `cache_read_input_tokens` and
`cache_creation_input_tokens` (the 1 h part only when the turn reports the 5 m / 1 h split and it adds
up). A Codex turn's `cache_read` is `cached_input_tokens`; its `cache_write` is 0, because OpenAI bills
no cache write apart from plain input. A cache figure the turn did not report is named in `unknown`.

A label or time the runner cannot establish is left out, and Mesh stores it as null:

- `account_ref` is the login directory's name (`CLAUDE_CONFIG_DIR=~/.claude-accounts/ops-team` →
  `claude/ops-team`), the same name a quota observation gives that account's pools
  (`claude/ops-team/5h`), so turns and limits join on one key. The harness's default directory
  (`~/.claude`, `~/.codex`) names an account only through `[spend_report.account_refs]`
  ({absolute login directory: account_ref}). A turn on an API key has no account, except one on the
  runner's own API route, which reports that route's account. A Claude turn served by another endpoint
  (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, a cloud provider) reports neither account nor billing
  mode. Kimi and ZCode keep their own logins, which the runner does not read.
- `started_at` and the account come from the record the runner writes when it launches the turn, never
  from the lane's later route; a turn without its own launch record (one started by an older runner)
  sends neither.

The fields of a turn are kept on their first send: a resend of the same report (after a lost answer)
carries the same body, which Mesh needs to apply it once.

## Order of rollout

A Mesh that does not know the fields refuses the whole report (a cache name in `unknown`), and with
it the turn's tokens and cost. Install the Mesh that accepts them first, then set
`turn_fields = true`. To stop sending them, remove the `[spend_report]` table (or set
`turn_fields = false`) and restart the runner. A runner older than this table ignores it, so a
downgrade needs no config change.
