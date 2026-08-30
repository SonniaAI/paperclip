# SON-1575 / SON-1440 WP-B reducer evidence

Run date: 2026-08-30 UTC

This is a focused WP-B probe over the accepted WP-A `state.json`. The manager's
1,064-row export is not copied or rewritten by WP-B; its accepted WP-A receipt
records **1,064 normalized, 0 invalid, 1,064 applied, 0 duplicate IDs, and
replay-applies 0**. The committed fixture here exercises the new crossing
state machine and is intentionally a separate, small delta.

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
- Threshold crossings: **3** — Class A at counts 3 and 3 after the
  `success_readonly` reset, and Class B at count 3
- Canonical lanes: `PE display` merged into `Product Engineer`; `Zero Write
  Display` reduced under `lane-b`
- Class A and Class B each retained an independent count-3 latch

The resulting signal state records current-streak timestamps in UTC ISO form:
Class A's re-crossing is `00:06:00Z`–`00:08:00Z`; Class B's crossing is
`00:01:30Z`–`00:03:30Z`.

## Read-only replay

Re-running the same fixture against the written state produced:

- Applied: **0**
- Already processed: **11**
- Threshold crossings: **0**
- Invalid / skipped: **0 / 0**

This verifies that the persisted `alert_emitted` latch and `processed_run_ids`
set prevent duplicate alerts after restart/replay.

## Verification

- Targeted TypeScript check: passed.
- Focused Vitest: **17/17 passed**.
- Scope guard: no alert transport, dashboard/UI, delivery wiring, gateway,
  adapter, or database changes.
