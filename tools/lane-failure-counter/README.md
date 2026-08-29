# SON-1536 WP-A — lane failure counter

This is a standalone TypeScript reducer for the WP-A slice. It does not import
the Paperclip server, gateway, adapter, or database packages, and it performs
no database writes.

## Frozen contract

- One increment per `heartbeat_run` record; duplicate `run_id` values are
  coalesced and persisted run IDs are ignored on later runs.
- Outcome precedence is `zero_write > terminal_fail > success_readonly >
  success > other`.
- `zero_write` and `terminal_fail` increment the lane's current streak.
- `success` and `success_readonly` reset the lane's current streak;
  `success_readonly` is counted as context, never as a failure.
- `other` and `running` are recorded but leave the current streak unchanged.
- The JSON state is inspectable by lane and includes the required
  `current_streak`, `last_success_ts`, `last_failure_ts`, and `updated_at`,
  plus per-outcome and per-failure counts.

## Read-only dry run

The CLI is read-only by default and accepts the manager's JSONL export:

```bash
tsx tools/lane-failure-counter/dry-run.ts \
  --ledger /path/to/run-ledger.jsonl \
  --state /path/to/previous-state.json
```

Add `--print-state` to include the complete proposed state in the output. Use
`--write` only when intentionally persisting the resulting state:

```bash
tsx tools/lane-failure-counter/dry-run.ts \
  --ledger /path/to/run-ledger.jsonl \
  --state /path/to/state.json \
  --write
```

The state writer uses a same-directory temporary file and rename, so readers
never observe a partial JSON document.
