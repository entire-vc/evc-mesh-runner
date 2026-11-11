# Operator resolution of an unknown attempt

An attempt goes to `unknown` when the Runner cannot prove whether a prompt reached the harness
(`MANDATORY DELIVERY PENDING` on the card). It keeps the card, its checkout and its worktrees and replays
nothing until native evidence settles the delivery. That is the safe default: a prompt that may have been
accepted is never sent twice.

Sometimes the evidence can never appear: the provider refused the harness, the session is gone, the
restored workspace no longer matches the unsent prompt. Then the attempt stands for ever, and moving the
card to another status does not end it.

## Ending the attempt

Write one file on the Runner host, named by the attempt id, with the reason on its first line:

```
mkdir -p ~/.mesh-runner/resolve.d
echo "Operator: harness refused by the provider, the prompt was never taken" \
  > ~/.mesh-runner/resolve.d/<attempt id>
```

The attempt id is in `status.json` (`attempts[].id` with `prompt_delivery.resolution_required`) and in
`runner.sqlite`.

On its next pass the Runner that owns the attempt:

1. archives the unconsumed prompt intent in the mark `operator_resolved_intent:<attempt id>`;
2. ends the attempt as `completed` with the note `operator resolution: <reason>`, releasing the Mesh
   checkout, the capacity gate and the worktrees;
3. writes `OPERATOR RESOLUTION` with the reason on the card and the event `operator_resolution`;
4. removes the token.

The card, still assigned and in a working status, starts in a new attempt. The pending prompt is not
replayed.

## What a token never does

- It does not end an attempt whose process group is still alive: that turn's own end settles the delivery.
- It does not touch an attempt that is not in `unknown`; the token stays until it applies or is removed.
- A symlink, a directory or an empty file is not a decision.
