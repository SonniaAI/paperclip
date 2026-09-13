# SON-1575 / SON-1440 WP-C recovery-clear evidence

Run date: 2026-08-30 UTC

This is a focused WP-C probe over the accepted WP-A `state.json` and the
committed WP-B crossing fixture. It exercises the reducer-side recovery
transition only; no alert sink, gateway, adapter, dashboard, or database is
part of this receipt.

## First apply

Command shape:

```bash
node tools/lane-failure-counter/dist/dry-run.js \
  --ledger tools/lane-failure-counter/evidence/wp-b-crossing-fixture.jsonl \
  --state <copy-of-state.json> \
  --lane-alias 'PE display=Product Engineer' \
  --lane-alias 'Zero Write Display=lane-b' \
  --write
```

- Input / normalized: **12 / 12**
- Invalid / skipped: **0 / 0**
- Applied: **11**
- Duplicate run IDs: **1** (`wpb-b-3`, coalesced before reduction)
- Threshold crossings: **3** — Class A at counts 3 and 3, and Class B at
  count 3
- Recovery clears: **1** — Class A on `wpb-a-reset`, the
  `success_readonly` transition after the active four-failure streak

The clear is emitted on canonical lane `Product Engineer` and carries:

```json
{
  "event_type": "recovery_clear",
  "class": "A",
  "signal": "generic/ws-open",
  "count": 4,
  "threshold": 3,
  "first_failure_ts": "2026-08-30T00:01:00.000Z",
  "last_failure_ts": "2026-08-30T00:04:00.000Z",
  "run_id": "wpb-a-reset"
}
```

Its `alert_emitted_at` equals the first Class A crossing's `emitted_at` from
the same reducer invocation. The inspectable signal state immediately after
that clear has `alert_emitted: false`, cleared current-streak timestamps, the
success's `last_success_ts`, and non-null `last_alert_ts`, `last_clear_ts`, and
`last_transition_ts`. The later Class A re-crossing updates the active latch
again without erasing the clear history.

## Read-only replay

Re-running the same fixture against the written state produced:

- Applied: **0**
- Already processed: **11**
- Threshold crossings: **0**
- Recovery clears: **0**
- Invalid / skipped: **0 / 0**

This verifies that the persisted processed-run set and alert latch prevent a
replayed success from emitting a second recovery clear.

## Verification

- Targeted TypeScript check: passed.
- Focused Vitest: **23/23 passed**.
- Scope guard: reducer state/events, tests, CLI output, and evidence only;
  production binding and transport remain outside WP-C.
