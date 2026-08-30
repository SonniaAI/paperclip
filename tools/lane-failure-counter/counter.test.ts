import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  applyCounterState,
  classifyOutcome,
  createEmptyCounterState,
  parseCounterState,
  parseLedgerText,
  readCounterState,
  serializeCounterState,
  writeCounterState,
  type NormalizedLedgerRecord,
} from "./index.js";

function record(
  runId: string,
  lane: string,
  outcome: NormalizedLedgerRecord["outcome"],
  finishedAt: string | null,
  sourceIndex = 0,
  metadata: Partial<Pick<NormalizedLedgerRecord, "signal_class" | "signal" | "issue_id" | "cause_code">> = {},
): NormalizedLedgerRecord {
  return {
    run_id: runId,
    lane,
    outcome,
    finished_at: finishedAt ? new Date(finishedAt).toISOString() : null,
    source_index: sourceIndex,
    ...metadata,
  };
}

describe("classifyOutcome", () => {
  it("applies zero-write precedence over every other signal", () => {
    expect(
      classifyOutcome({
        outcome: "success",
        zero_write: true,
        terminal_fail: true,
        success_readonly: true,
      }),
    ).toBe("zero_write");
  });

  it("applies terminal-fail, read-only-success, and success precedence in order", () => {
    expect(classifyOutcome({ outcome: "success", terminal_fail: true })).toBe("terminal_fail");
    expect(classifyOutcome({ outcome: "success", success_readonly: true })).toBe("success_readonly");
    expect(classifyOutcome({ status: "succeeded", readonly: true })).toBe("success_readonly");
    expect(classifyOutcome({ status: "failed" })).toBe("terminal_fail");
    expect(classifyOutcome({ status: "running" })).toBe("running");
  });
});

describe("parseLedgerText", () => {
  it("reads the manager JSONL shape and retains metadata without treating it as a run", () => {
    const parsed = parseLedgerText([
      JSON.stringify({ meta: { title: "fixture", contract: ["dedup by run_id"] } }),
      JSON.stringify({ run_id: "run-2", lane: "B", outcome: "success", finished_at: "2026-08-29T00:02:00Z" }),
      JSON.stringify({ run_id: "run-1", agent: "A", outcome: "terminal-fail", finished_at: "2026-08-29T00:01:00Z" }),
    ].join("\n"));

    expect(parsed.invalid).toEqual([]);
    expect(parsed.metadata).toEqual({ title: "fixture", contract: ["dedup by run_id"] });
    expect(parsed.records).toEqual([
      {
        run_id: "run-2",
        lane: "B",
        outcome: "success",
        finished_at: "2026-08-29T00:02:00.000Z",
        source_index: 1,
      },
      {
        run_id: "run-1",
        lane: "A",
        outcome: "terminal_fail",
        finished_at: "2026-08-29T00:01:00.000Z",
        source_index: 2,
      },
    ]);
  });

  it("reports malformed rows while retaining valid rows", () => {
    const parsed = parseLedgerText([
      JSON.stringify({ run_id: "ok", lane: "A", outcome: "success", finished_at: "2026-08-29T00:00:00Z" }),
      JSON.stringify({ lane: "missing-id", outcome: "success" }),
      "not-json",
    ].join("\n"));

    expect(parsed.records).toHaveLength(1);
    expect(parsed.invalid).toHaveLength(2);
    expect(parsed.invalid.map((issue) => issue.reason)).toEqual([
      "missing run_id",
      expect.stringContaining("invalid JSON"),
    ]);
  });
});

describe("applyCounterState", () => {
  it("maintains independent lane streaks, resets on both success forms, and keeps class counts", () => {
    const initial = createEmptyCounterState("2026-08-29T00:00:00Z");
    const result = applyCounterState(
      initial,
      [
        record("a-3", "A", "terminal_fail", "2026-08-29T00:03:00Z", 3),
        record("a-1", "A", "zero_write", "2026-08-29T00:01:00Z", 1),
        record("b-1", "B", "terminal_fail", "2026-08-29T00:01:30Z", 2),
        record("a-2", "A", "success_readonly", "2026-08-29T00:02:00Z", 0),
        record("a-4", "A", "zero_write", "2026-08-29T00:04:00Z", 4),
        record("a-5", "A", "other", null, 5),
        record("a-6", "A", "running", null, 6),
        record("a-7", "A", "success", "2026-08-29T00:07:00Z", 7),
      ],
      { now: () => "2026-08-29T01:00:00Z" },
    );

    expect(result.applied.map((entry) => entry.run_id)).toEqual([
      "a-1",
      "b-1",
      "a-2",
      "a-3",
      "a-4",
      "a-7",
      "a-5",
      "a-6",
    ]);
    expect(result.state.lanes.A).toEqual({
      current_streak: 0,
      last_success_ts: "2026-08-29T00:07:00.000Z",
      last_failure_ts: "2026-08-29T00:04:00.000Z",
      updated_at: "2026-08-29T01:00:00.000Z",
      outcome_counts: {
        zero_write: 2,
        terminal_fail: 1,
        success_readonly: 1,
        success: 1,
        other: 1,
        running: 1,
      },
      failure_counts: { zero_write: 2, terminal_fail: 1 },
    });
    expect(result.state.lanes.B.current_streak).toBe(1);
  });

  it("coalesces duplicate run ids and keeps the nastier classification", () => {
    const result = applyCounterState(
      createEmptyCounterState("2026-08-29T00:00:00Z"),
      [
        record("same", "A", "success", "2026-08-29T00:01:00Z", 0),
        record("same", "A", "terminal_fail", "2026-08-29T00:01:00Z", 1),
        record("same", "A", "zero_write", "2026-08-29T00:01:00Z", 2),
      ],
      { now: () => "2026-08-29T01:00:00Z" },
    );

    expect(result.duplicate_run_ids).toEqual(["same"]);
    expect(result.applied).toHaveLength(1);
    expect(result.applied[0]?.outcome).toBe("zero_write");
    expect(result.state.lanes.A.current_streak).toBe(1);
    expect(result.state.lanes.A.failure_counts).toEqual({ zero_write: 1, terminal_fail: 0 });
  });

  it("is idempotent against a persisted processed-run set", () => {
    const state = createEmptyCounterState("2026-08-29T00:00:00Z");
    const input = [record("same", "A", "terminal_fail", "2026-08-29T00:01:00.000Z")];
    const first = applyCounterState(state, input, { now: () => "2026-08-29T01:00:00Z" });
    const second = applyCounterState(first.state, input, { now: () => "2026-08-29T02:00:00Z" });

    expect(second.applied).toEqual([]);
    expect(second.already_processed_run_ids).toEqual(["same"]);
    expect(second.state).toEqual(first.state);
  });

  it("does not count a failure or success without a terminal timestamp", () => {
    const result = applyCounterState(
      createEmptyCounterState("2026-08-29T00:00:00Z"),
      [record("missing-time", "A", "zero_write", null)],
      { now: () => "2026-08-29T01:00:00Z" },
    );

    expect(result.applied).toEqual([]);
    expect(result.skipped).toEqual([
      {
        run_id: "missing-time",
        lane: "A",
        outcome: "zero_write",
        reason: "failure/success record is missing finished_at",
      },
    ]);
  });
});

describe("counter state persistence", () => {
  it("round-trips inspectable JSON through an atomic state write", async () => {
    const directory = await mkdtemp(join(os.tmpdir(), "son-1536-counter-"));
    const statePath = join(directory, "state.json");
    try {
      const state = applyCounterState(
        createEmptyCounterState("2026-08-29T00:00:00Z"),
        [record("run-1", "A", "zero_write", "2026-08-29T00:01:00.000Z")],
        { now: () => "2026-08-29T01:00:00Z" },
      ).state;
      await writeCounterState(statePath, state);

      const parsed = await readCounterState(statePath);
      expect(parsed).toEqual(state);
      expect(JSON.parse(await readFile(statePath, "utf8"))).toMatchObject({
        version: 1,
        lanes: { A: { current_streak: 1, last_failure_ts: "2026-08-29T00:01:00.000Z" } },
      });
      expect(serializeCounterState(parsed)).toContain('"processed_run_ids": [');
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("rejects unsupported state versions", () => {
    expect(() => parseCounterState({ version: 2, updated_at: "2026-08-29T00:00:00Z", lanes: {} })).toThrow(
      "Unsupported counter state version",
    );
  });
});

describe("WP-B threshold crossing events", () => {
  it("canonicalizes aliases before reduction and keeps Class A and B independent", () => {
    const result = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("a-1", "Product Engineer", "terminal_fail", "2026-08-30T00:01:00Z", 0),
        record("b-1", "zero-write-display", "zero_write", "2026-08-30T00:01:30Z", 1),
        record("a-2", "PE legacy", "terminal_fail", "2026-08-30T00:02:00Z", 2),
        record("b-2", "zero-write-display", "zero_write", "2026-08-30T00:02:30Z", 3),
        record("b-3", "zero-write-display", "zero_write", "2026-08-30T00:03:30Z", 4),
      ],
      {
        now: () => "2026-08-30T01:00:00Z",
        laneAliases: {
          "PE legacy": "Product Engineer",
          "zero-write-display": "lane-b",
        },
        thresholds: { A: 2, B: 3 },
      },
    );

    expect(result.threshold_crossings).toEqual([
      {
        event_type: "threshold_crossing",
        emitted_at: "2026-08-30T01:00:00.000Z",
        lane_id: "Product Engineer",
        class: "A",
        signal: "generic/ws-open",
        count: 2,
        threshold: 2,
        first_failure_ts: "2026-08-30T00:01:00.000Z",
        last_failure_ts: "2026-08-30T00:02:00.000Z",
        run_id: "a-2",
      },
      {
        event_type: "threshold_crossing",
        emitted_at: "2026-08-30T01:00:00.000Z",
        lane_id: "lane-b",
        class: "B",
        signal: "zero_write",
        count: 3,
        threshold: 3,
        first_failure_ts: "2026-08-30T00:01:30.000Z",
        last_failure_ts: "2026-08-30T00:03:30.000Z",
        run_id: "b-3",
      },
    ]);
    expect(result.state.signals["Product Engineer"]?.A).toMatchObject({
      current_streak: 2,
      alert_emitted: true,
      first_failure_ts: "2026-08-30T00:01:00.000Z",
    });
    expect(result.state.signals["Product Engineer"]?.B).toBeUndefined();
    expect(result.state.signals["lane-b"]?.A).toBeUndefined();
    expect(result.state.signals["lane-b"]?.B).toMatchObject({ current_streak: 3, alert_emitted: true });
  });

  it("emits once above threshold, persists the latch, and emits again only after success reset", () => {
    const first = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("f-1", "lane", "terminal_fail", "2026-08-30T00:01:00Z"),
        record("f-2", "lane", "terminal_fail", "2026-08-30T00:02:00Z"),
        record("f-3", "lane", "terminal_fail", "2026-08-30T00:03:00Z"),
      ],
      { now: () => "2026-08-30T00:04:00Z" },
    );
    expect(first.threshold_crossings).toHaveLength(1);

    const aboveThreshold = applyCounterState(
      first.state,
      [record("f-4", "lane", "terminal_fail", "2026-08-30T00:05:00Z")],
      { now: () => "2026-08-30T00:06:00Z" },
    );
    expect(aboveThreshold.threshold_crossings).toEqual([]);
    expect(aboveThreshold.state.signals.lane?.A).toMatchObject({ current_streak: 4, alert_emitted: true });

    const reset = applyCounterState(
      aboveThreshold.state,
      [record("success-1", "lane", "success", "2026-08-30T00:07:00Z")],
      { now: () => "2026-08-30T00:08:00Z" },
    );
    expect(reset.threshold_crossings).toEqual([]);
    expect(reset.state.signals.lane?.A).toMatchObject({
      current_streak: 0,
      first_failure_ts: null,
      alert_emitted: false,
    });

    const recross = applyCounterState(
      reset.state,
      [
        record("f-5", "lane", "terminal_fail", "2026-08-30T00:09:00Z"),
        record("f-6", "lane", "terminal_fail", "2026-08-30T00:10:00Z"),
        record("f-7", "lane", "terminal_fail", "2026-08-30T00:11:00Z"),
      ],
      { now: () => "2026-08-30T00:12:00Z" },
    );
    expect(recross.threshold_crossings).toHaveLength(1);
    expect(recross.threshold_crossings[0]).toMatchObject({
      count: 3,
      first_failure_ts: "2026-08-30T00:09:00.000Z",
      last_failure_ts: "2026-08-30T00:11:00.000Z",
      run_id: "f-7",
    });
  });

  it("retains an explicit threshold through restart and clears the current failure boundary on readonly success", () => {
    const first = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("configured-1", "lane", "terminal_fail", "2026-08-30T00:01:00Z"),
        record("configured-2", "lane", "terminal_fail", "2026-08-30T00:02:00Z"),
      ],
      { now: () => "2026-08-30T00:03:00Z", thresholds: { A: 2 } },
    );
    expect(first.threshold_crossings).toHaveLength(1);
    expect(first.state.signals.lane?.A).toMatchObject({ threshold: 2, current_streak: 2, alert_emitted: true });

    const reset = applyCounterState(
      first.state,
      [record("configured-success", "lane", "success_readonly", "2026-08-30T00:04:00Z")],
      { now: () => "2026-08-30T00:05:00Z" },
    );
    expect(reset.state.signals.lane?.A).toMatchObject({
      threshold: 2,
      current_streak: 0,
      first_failure_ts: null,
      last_failure_ts: null,
      last_success_ts: "2026-08-30T00:04:00.000Z",
      alert_emitted: false,
    });

    const recross = applyCounterState(
      reset.state,
      [
        record("configured-3", "lane", "terminal_fail", "2026-08-30T00:06:00Z"),
        record("configured-4", "lane", "terminal_fail", "2026-08-30T00:07:00Z"),
      ],
      { now: () => "2026-08-30T00:08:00Z" },
    );
    expect(recross.threshold_crossings).toHaveLength(1);
    expect(recross.threshold_crossings[0]).toMatchObject({ count: 2, threshold: 2 });
  });

  it("coalesces alias spellings of one run before incrementing or emitting", () => {
    const result = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("same-run", "legacy lane", "terminal_fail", "2026-08-30T00:01:00Z", 0),
        record("same-run", "canonical lane", "terminal_fail", "2026-08-30T00:01:00Z", 1),
      ],
      {
        now: () => "2026-08-30T00:02:00Z",
        laneAliases: { "legacy lane": "canonical lane" },
        thresholds: { A: 2 },
      },
    );

    expect(result.duplicate_run_ids).toEqual(["same-run"]);
    expect(result.applied).toHaveLength(1);
    expect(result.state.signals["legacy lane"]).toBeUndefined();
    expect(result.state.signals["canonical lane"]?.A).toMatchObject({ current_streak: 1, alert_emitted: false });
    expect(result.threshold_crossings).toEqual([]);
  });

  it("keeps the alert latch across a persisted restart and does not re-emit replayed failures", async () => {
    const directory = await mkdtemp(join(os.tmpdir(), "son-1536-wp-b-"));
    const statePath = join(directory, "state.json");
    try {
      const first = applyCounterState(
        createEmptyCounterState("2026-08-30T00:00:00Z"),
        [
          record("r-1", "lane", "terminal_fail", "2026-08-30T00:01:00Z"),
          record("r-2", "lane", "terminal_fail", "2026-08-30T00:02:00Z"),
          record("r-3", "lane", "terminal_fail", "2026-08-30T00:03:00Z"),
        ],
        { now: () => "2026-08-30T00:04:00Z" },
      );
      await writeCounterState(statePath, first.state);
      const restarted = await readCounterState(statePath);
      const replay = applyCounterState(
        restarted,
        [
          record("r-1", "lane", "terminal_fail", "2026-08-30T00:01:00Z"),
          record("r-2", "lane", "terminal_fail", "2026-08-30T00:02:00Z"),
          record("r-3", "lane", "terminal_fail", "2026-08-30T00:03:00Z"),
          record("r-4", "lane", "terminal_fail", "2026-08-30T00:05:00Z"),
        ],
        { now: () => "2026-08-30T00:06:00Z" },
      );

      expect(replay.already_processed_run_ids).toEqual(["r-1", "r-2", "r-3"]);
      expect(replay.threshold_crossings).toEqual([]);
      expect(replay.state.signals.lane?.A).toMatchObject({ current_streak: 4, alert_emitted: true });
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("merges persisted alias state without splitting the streak or losing an emitted latch", () => {
    const oldName = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("alias-1", "old lane", "terminal_fail", "2026-08-30T00:01:00Z"),
        record("alias-2", "old lane", "terminal_fail", "2026-08-30T00:02:00Z"),
        record("alias-3", "old lane", "terminal_fail", "2026-08-30T00:03:00Z"),
      ],
      { now: () => "2026-08-30T00:04:00Z" },
    ).state;
    const canonicalName = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [record("canonical-1", "canonical lane", "terminal_fail", "2026-08-30T00:05:00Z")],
      { now: () => "2026-08-30T00:06:00Z" },
    ).state;

    const merged = applyCounterState(
      {
        ...createEmptyCounterState("2026-08-30T00:00:00Z"),
        processed_run_ids: [...oldName.processed_run_ids, ...canonicalName.processed_run_ids],
        lanes: { "old lane": oldName.lanes["old lane"]!, "canonical lane": canonicalName.lanes["canonical lane"]! },
        signals: {
          "old lane": oldName.signals["old lane"]!,
          "canonical lane": canonicalName.signals["canonical lane"]!,
        },
      },
      [record("canonical-2", "canonical lane", "terminal_fail", "2026-08-30T00:07:00Z")],
      {
        now: () => "2026-08-30T00:08:00Z",
        laneAliases: { "old lane": "canonical lane" },
      },
    );

    expect(merged.threshold_crossings).toEqual([]);
    expect(merged.state.signals["canonical lane"]?.A).toMatchObject({
      current_streak: 5,
      alert_emitted: true,
    });
    expect(merged.state.signals["old lane"]).toBeUndefined();
  });

  it("supports Class C's immediate, independently routed signal without treating a stranded success as recovery", () => {
    const result = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("c-1", "lane", "success", "2026-08-30T00:01:00Z", 0, {
          signal_class: "C",
          signal: "stranded_assigned_issue",
          issue_id: "issue-1",
          cause_code: "successful_run_missing_state",
        }),
      ],
      { now: () => "2026-08-30T00:02:00Z" },
    );

    expect(result.threshold_crossings).toEqual([
      {
        event_type: "threshold_crossing",
        emitted_at: "2026-08-30T00:02:00.000Z",
        lane_id: "lane",
        class: "C",
        signal: "stranded_assigned_issue",
        count: 1,
        threshold: 1,
        first_failure_ts: "2026-08-30T00:01:00.000Z",
        last_failure_ts: "2026-08-30T00:01:00.000Z",
        run_id: "c-1",
        issue_id: "issue-1",
        cause_code: "successful_run_missing_state",
      },
    ]);
    expect(result.state.lanes.lane).toBeUndefined();
    expect(result.state.signals.lane?.C).toMatchObject({ current_streak: 1, alert_emitted: true });
  });
});

describe("WP-C recovery-clear events", () => {
  it("emits a correlated clear on readonly success and preserves transition history", () => {
    const alerted = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("clear-a-1", "lane", "terminal_fail", "2026-08-30T00:01:00Z"),
        record("clear-a-2", "lane", "terminal_fail", "2026-08-30T00:02:00Z"),
        record("clear-a-3", "lane", "terminal_fail", "2026-08-30T00:03:00Z"),
      ],
      { now: () => "2026-08-30T00:04:00Z" },
    );
    const recovered = applyCounterState(
      alerted.state,
      [record("clear-success", "lane", "success_readonly", "2026-08-30T00:05:00Z")],
      { now: () => "2026-08-30T00:06:00Z" },
    );

    expect(recovered.recovery_clears).toEqual([
      {
        event_type: "recovery_clear",
        emitted_at: "2026-08-30T00:06:00.000Z",
        lane_id: "lane",
        class: "A",
        signal: "generic/ws-open",
        count: 3,
        threshold: 3,
        first_failure_ts: "2026-08-30T00:01:00.000Z",
        last_failure_ts: "2026-08-30T00:03:00.000Z",
        alert_emitted_at: "2026-08-30T00:04:00.000Z",
        run_id: "clear-success",
      },
    ]);
    expect(recovered.state.signals.lane?.A).toMatchObject({
      current_streak: 0,
      first_failure_ts: null,
      last_failure_ts: null,
      last_success_ts: "2026-08-30T00:05:00.000Z",
      alert_emitted: false,
      last_alert_ts: "2026-08-30T00:04:00.000Z",
      last_clear_ts: "2026-08-30T00:06:00.000Z",
      last_transition_ts: "2026-08-30T00:06:00.000Z",
    });
  });

  it("does not emit a clear when success resets a below-threshold streak", () => {
    const belowThreshold = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("below-1", "lane", "terminal_fail", "2026-08-30T00:01:00Z"),
        record("below-2", "lane", "terminal_fail", "2026-08-30T00:02:00Z"),
      ],
      { now: () => "2026-08-30T00:03:00Z" },
    );
    const recovered = applyCounterState(
      belowThreshold.state,
      [record("below-success", "lane", "success", "2026-08-30T00:04:00Z")],
      { now: () => "2026-08-30T00:05:00Z" },
    );

    expect(recovered.recovery_clears).toEqual([]);
    expect(recovered.state.signals.lane?.A).toMatchObject({
      current_streak: 0,
      alert_emitted: false,
      last_alert_ts: null,
      last_clear_ts: null,
      last_transition_ts: null,
    });
  });

  it("clears only the explicitly recovered class and keeps Class B latched", () => {
    const alerted = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("isolation-a", "lane", "terminal_fail", "2026-08-30T00:01:00Z"),
        record("isolation-b", "lane", "zero_write", "2026-08-30T00:02:00Z"),
      ],
      {
        now: () => "2026-08-30T00:03:00Z",
        thresholds: { A: 1, B: 1 },
      },
    );
    const classARecovered = applyCounterState(
      alerted.state,
      [record("isolation-a-recovery", "lane", "success", "2026-08-30T00:04:00Z", 0, { signal_class: "A" })],
      { now: () => "2026-08-30T00:05:00Z", thresholds: { A: 1, B: 1 } },
    );

    expect(classARecovered.recovery_clears).toHaveLength(1);
    expect(classARecovered.recovery_clears[0]).toMatchObject({ class: "A", run_id: "isolation-a-recovery" });
    expect(classARecovered.state.signals.lane?.A).toMatchObject({
      current_streak: 0,
      alert_emitted: false,
      last_clear_ts: "2026-08-30T00:05:00.000Z",
    });
    expect(classARecovered.state.signals.lane?.B).toMatchObject({ current_streak: 1, alert_emitted: true });

    const allRecovered = applyCounterState(
      classARecovered.state,
      [record("isolation-b-recovery", "lane", "success", "2026-08-30T00:06:00Z")],
      { now: () => "2026-08-30T00:07:00Z", thresholds: { A: 1, B: 1 } },
    );
    expect(allRecovered.recovery_clears).toHaveLength(1);
    expect(allRecovered.recovery_clears[0]).toMatchObject({ class: "B", run_id: "isolation-b-recovery" });
  });

  it("does not duplicate a clear after persisted restart and replay", () => {
    const alerted = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("restart-1", "lane", "terminal_fail", "2026-08-30T00:01:00Z"),
        record("restart-2", "lane", "terminal_fail", "2026-08-30T00:02:00Z"),
        record("restart-3", "lane", "terminal_fail", "2026-08-30T00:03:00Z"),
      ],
      { now: () => "2026-08-30T00:04:00Z" },
    );
    const restarted = parseCounterState(JSON.parse(serializeCounterState(alerted.state)) as unknown);
    const recovery = record("restart-success", "lane", "success", "2026-08-30T00:05:00Z");
    const firstRecovery = applyCounterState(restarted, [recovery], { now: () => "2026-08-30T00:06:00Z" });
    const replay = applyCounterState(firstRecovery.state, [recovery], { now: () => "2026-08-30T00:07:00Z" });

    expect(firstRecovery.recovery_clears).toHaveLength(1);
    expect(replay.already_processed_run_ids).toEqual(["restart-success"]);
    expect(replay.recovery_clears).toEqual([]);
    expect(replay.state).toEqual(firstRecovery.state);
  });

  it("merges an active alias alert before clearing it under the canonical lane", () => {
    const oldAliasState = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("alias-clear-1", "old lane", "terminal_fail", "2026-08-30T00:01:00Z"),
        record("alias-clear-2", "old lane", "terminal_fail", "2026-08-30T00:02:00Z"),
        record("alias-clear-3", "old lane", "terminal_fail", "2026-08-30T00:03:00Z"),
      ],
      { now: () => "2026-08-30T00:04:00Z" },
    ).state;
    const canonicalHistory = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [record("canonical-history", "canonical lane", "success", "2026-08-30T00:02:00Z")],
      { now: () => "2026-08-30T00:03:00Z" },
    ).state;
    const merged = applyCounterState(
      {
        ...createEmptyCounterState("2026-08-30T00:00:00Z"),
        processed_run_ids: [...oldAliasState.processed_run_ids, ...canonicalHistory.processed_run_ids],
        lanes: {
          "old lane": oldAliasState.lanes["old lane"]!,
          "canonical lane": canonicalHistory.lanes["canonical lane"]!,
        },
        signals: {
          "old lane": oldAliasState.signals["old lane"]!,
          "canonical lane": canonicalHistory.signals["canonical lane"]!,
        },
      },
      [record("alias-clear-success", "canonical lane", "success_readonly", "2026-08-30T00:05:00Z")],
      {
        now: () => "2026-08-30T00:06:00Z",
        laneAliases: { "old lane": "canonical lane" },
      },
    );

    expect(merged.recovery_clears).toHaveLength(1);
    expect(merged.recovery_clears[0]).toMatchObject({
      lane_id: "canonical lane",
      class: "A",
      alert_emitted_at: "2026-08-30T00:04:00.000Z",
    });
    expect(merged.state.signals["old lane"]).toBeUndefined();
    expect(merged.state.signals["canonical lane"]?.A).toMatchObject({
      current_streak: 0,
      alert_emitted: false,
      last_clear_ts: "2026-08-30T00:06:00.000Z",
    });
  });

  it("clears Class C after an ordinary success while keeping stranding success latched", () => {
    const stranded = applyCounterState(
      createEmptyCounterState("2026-08-30T00:00:00Z"),
      [
        record("class-c-stranded", "lane", "success", "2026-08-30T00:01:00Z", 0, {
          signal_class: "C",
          signal: "successful_run_missing_state",
          issue_id: "issue-c",
          cause_code: "successful_run_missing_state",
        }),
      ],
      { now: () => "2026-08-30T00:02:00Z" },
    );
    const recovered = applyCounterState(
      stranded.state,
      [record("class-c-recovery", "lane", "success_readonly", "2026-08-30T00:03:00Z")],
      { now: () => "2026-08-30T00:04:00Z" },
    );

    expect(stranded.recovery_clears).toEqual([]);
    expect(recovered.recovery_clears).toEqual([
      {
        event_type: "recovery_clear",
        emitted_at: "2026-08-30T00:04:00.000Z",
        lane_id: "lane",
        class: "C",
        signal: "stranded_assigned_issue",
        count: 1,
        threshold: 1,
        first_failure_ts: "2026-08-30T00:01:00.000Z",
        last_failure_ts: "2026-08-30T00:01:00.000Z",
        alert_emitted_at: "2026-08-30T00:02:00.000Z",
        run_id: "class-c-recovery",
      },
    ]);
    expect(recovered.state.signals.lane?.C).toMatchObject({ current_streak: 0, alert_emitted: false });
  });
});
