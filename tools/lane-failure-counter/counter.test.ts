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
): NormalizedLedgerRecord {
  return {
    run_id: runId,
    lane,
    outcome,
    finished_at: finishedAt ? new Date(finishedAt).toISOString() : null,
    source_index: sourceIndex,
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
