# Two-step reasoning effort

A lane runs its first pass at a base reasoning effort and moves one step up only when the card shows a
real signal of difficulty. Off by default: a lane without the two fields launches exactly as before.

```toml
[[agents]]
name = "dev"
args = ["--model", "opus", "--effort", "xhigh"]   # still the fallback when the policy is off
default_effort  = "high"     # every turn, unless a trigger fires
escalate_effort = "xhigh"    # a turn whose card shows a trigger
```

| fields set | first pass | on a trigger |
|---|---|---|
| neither | the `args` (no change) | the `args` (no change) |
| `default_effort` only | `default_effort` | `default_effort` |
| `escalate_effort` only | the `args` | `escalate_effort` |
| both | `default_effort` | `escalate_effort` |

Levels: `low`, `medium`, `high`, `xhigh`, `max`. An unknown level, or an `escalate_effort` not above
`default_effort`, is logged and ignored; the config still loads. An `effort:<level>` label on the card
outranks the policy. The policy applies to the lane's own harness; a turn routed to an `alt` harness
keeps that route's args.

## Triggers

Read at every launch, first start and resume alike; first match wins.

| trigger | fact |
|---|---|
| `do_not_ship` | the newest review verdict on the card is DO-NOT-SHIP (a request listing both verdicts is not one) |
| `ci_failed_again` | two or more distinct pipelines of the card were delivered to the lane as failed (one pipeline seen by two waits counts once); it stays on after a later green pipeline: the card proved hard, not the head |
| `failed_attempt` | an earlier attempt of the lane on the card ran a turn and gave up on the work itself: no readable outcome, or the per-card turn limit. Aborts, provider limits, harness errors, a lost checkout and the daily turn budget do not count |
| `label:<name>` | the card carries the label `security`, `architecture`, `urgent` or `incident` (exact name) |

A fact that cannot be read counts as absent: the turn runs its base level, it is never held.

## Records

Every launched turn of a lane with the policy on leaves one record, in two places:

- the attempt's store event `effort_policy` in `runner.sqlite` (`events.kind`, `events.attempt`);
- a `runner.log` line `<lane>: #<card8> effort <record> log=<turn log file>`.

The record has fixed keys and no free text:

```
turn=2 effort=xhigh base=high escalated=1 trigger=do_not_ship source=policy
```

`effort` is read back from the turn's real command line. `source` says what set it: `policy`, `label`
(an `effort:` label) or `args` (the lane's args: an escalate-only lane before a trigger, or a turn routed
to an `alt` harness); `escalated=1` only with `source=policy`. `base` is `default_effort`, else the
args' level. The chosen level also reaches the
execution view as `resolved_route.effort` (read from the launch command line); the execution report
carries no new field.
