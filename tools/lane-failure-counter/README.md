# SON-1536 WP-A/WP-B — lane failure counter and crossing events

This is a standalone TypeScript reducer for the WP-A/WP-B slices. It does not import
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

## WP-B crossing contract

The additive `signals` section stores state per canonical `(lane, class)`:

- Class A is generic terminal failure (`generic/ws-open`), default threshold
  **3**.
- Class B is zero-write terminal failure (`zero_write`), default threshold
  **3**, and never contributes to Class A.
- Class C is a stranded successful run (`successful_run_missing_state` or
  `stranded_assigned_issue`), default threshold **1**.
- Thresholds are configuration, not constants. Pass `thresholds: { A, B, C }`
  to `applyCounterState` to retune them.
- A `threshold_crossing` event is emitted exactly once when a class reaches its
  threshold. Further failures remain deduplicated while the latch is set.
- Any ordinary successful run resets every existing class state for that
  canonical lane, clears its latch, and permits a later re-crossing.
- The latch is persisted in `signals.<canonical-lane>.<class>.alert_emitted`,
  so restart/replay cannot duplicate an alert.

Events contain the canonical `lane_id`, taxonomy `class`, wire `signal`,
`count`, `threshold`, current-streak `first_failure_ts` and `last_failure_ts`,
plus the UTC ISO `emitted_at`. Class C events also carry `issue_id` and
`cause_code` when present. Transport and sink delivery remain outside WP-B.

Lane names can be resolved at the input boundary with an agents-table-backed
`canonicalizeLane` function or a deterministic `laneAliases` map. Both parsed
records and persisted state are canonicalized before reduction, so a display
name rename does not split a streak or orphan its alert latch.

## Read-only dry run

The CLI is read-only by default and accepts the manager's JSONL export:

```bash
tsx tools/lane-failure-counter/dry-run.ts \
  --ledger /path/to/run-ledger.jsonl \
  --state /path/to/previous-state.json
```

For a rename rollout or calibration run, add repeatable options such as:

```bash
tsx tools/lane-failure-counter/dry-run.ts \
  --ledger /path/to/run-ledger.jsonl \
  --state /path/to/state.json \
  --lane-alias "PE legacy=Product Engineer" \
  --threshold A=4
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
