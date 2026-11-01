# Explicit authentication recheck

After an explicit authentication repair, the owner can notify the existing
installed signed ingress using the same configuration and lane:

```sh
python3 -m mesh_runner.cli notify-auth-change --config /absolute/path/runner.toml --agent exact-lane
```

The explicit owner notifier derives the exact profile from that lane's current
configured route, then calls the existing `recheck-auth --profile exact-fingerprint`
producer. The command authenticates the own Mesh actor, reads the exact durable hold epoch,
and sends `auth.recheck` v1 to the configured loopback `/mesh/events`. It cannot
install an extension, choose an event destination, switch an account or repair a
credential. An event triggers proof; its signature is not proof of provider access.

The consumer re-reads the own configured lane environment. Only a direct
Anthropic API-key route has a supported free remote authentication observation:
`GET https://api.anthropic.com/v1/models?limit=1`, with `x-api-key` and
`anthropic-version: 2023-06-01`. Redirects are refused. The bounded response proves
authentication only; Messages access and entitlement to the configured model
remain UNKNOWN. Primary contracts: [Models](https://platform.claude.com/docs/en/api/models)
and [API overview](https://platform.claude.com/docs/en/api/overview).

Native OAuth, mixed auth, custom endpoints, verification-required and legacy
holds without recorded provenance remain held. Local CLI login status and a
credential file change are not restoration proof. Existing operator clear remains
available; this subset does not automate OAuth recovery. Existing successful
turn/usage observations do not bind a new response to a later hold generation.

Workspace, actor, lane, profile, event UUID, freshness and the unique hold epoch
are checked before the probe. The consumer fences replay durably, then CAS-clears
only the same epoch after fresh proof and unchanged environment. The hold clear
and a credential-free wake obligation commit in one SQLite transaction. Concurrent
or replayed events cannot create another obligation for the same epoch.

Signed acceptance schedules that lane immediately. Its ordinary agent loop also
drains an outstanding obligation after restart; it performs no provider recheck.
The obligation is acknowledged only after a successful ordinary tick, fenced to
the value observed before that tick. New failures and old acknowledgements cannot
consume another generation. A process death before acknowledgement may redeliver
the scheduling hint; normal durable attempt admission still prevents a second
writer. This is one logical wake with at-least-once delivery, not an impossible
exactly-once process-notification guarantee. A wake creates no attempt and grants
no rights. Ordinary task events never clear these holds.

No timer invokes the notifier or provider proof. Only an explicit owner repair
completion calls the supported notifier; no automatic native-login-completion
producer is included.
On source rollback, the additional provenance/event marks remain inert. No
credential values or provider bodies belong in event payloads or command output.
