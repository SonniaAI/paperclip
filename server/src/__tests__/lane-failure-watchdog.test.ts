import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  alertCommentBody,
  recoveryCommentBody,
  recordFromHeartbeatRun,
  recordFromStrandingActivity,
  resolveLaneWatchdogConfig,
} from "../services/lane-failure-watchdog.js";
import {
  applyCounterState,
  createEmptyCounterState,
  normalizeLedgerRecord,
  type RecoveryClearEvent,
  type ThresholdCrossingEvent,
} from "../vendor/lane-failure-counter/index.js";

const here = dirname(fileURLToPath(import.meta.url));

/** Mirror the service path: raw records go through the reducer's normalizer. */
const normalize = (records: Record<string, unknown>[]) =>
  records
    .map((value, sourceIndex) => normalizeLedgerRecord(value, sourceIndex).record)
    .filter((record) => record != null);

describe("lane-failure-watchdog vendored reducer sync", () => {
  it("vendored reducer is byte-identical to the canonical tool copy", () => {
    const toolCopy = readFileSync(join(here, "../../../tools/lane-failure-counter/index.ts"), "utf8");
    const vendored = readFileSync(join(here, "../vendor/lane-failure-counter/index.ts"), "utf8");
    expect(vendored).toBe(toolCopy);
  });
});

describe("resolveLaneWatchdogConfig", () => {
  it("defaults to enabled, SON-1334 sink, 30-minute cadence, reducer default thresholds", () => {
    const config = resolveLaneWatchdogConfig({});
    expect(config).toEqual({
      enabled: true,
      sinkIdentifier: "SON-1334",
      intervalMs: 30 * 60 * 1000,
      thresholds: {},
    });
  });

  it("honors disable flag, sink identifier, interval floor, and threshold overrides", () => {
    const config = resolveLaneWatchdogConfig({
      PAPERCLIP_LANE_WATCHDOG_DISABLED: "true",
      PAPERCLIP_LANE_WATCHDOG_SINK_IDENTIFIER: " OPS-SINK ",
      PAPERCLIP_LANE_WATCHDOG_INTERVAL_MS: "1000",
      PAPERCLIP_LANE_WATCHDOG_THRESHOLDS: '{"A":4,"b":2,"junk":9}',
    });
    expect(config.enabled).toBe(false);
    expect(config.sinkIdentifier).toBe("OPS-SINK");
    expect(config.intervalMs).toBe(30 * 60 * 1000);
    expect(config.thresholds).toEqual({ A: 4, B: 2 });
  });

  it("ignores malformed threshold JSON instead of throwing", () => {
    expect(resolveLaneWatchdogConfig({ PAPERCLIP_LANE_WATCHDOG_THRESHOLDS: "{not json" }).thresholds).toEqual({});
  });
});

describe("recordFromHeartbeatRun", () => {
  const base = { id: "run-1", agentId: "agent-1", finishedAt: new Date("2026-08-30T10:00:00Z") };

  it("marks a failed run with zero real writes as zero_write (Class B)", () => {
    const record = recordFromHeartbeatRun({ ...base, status: "failed" }, 0);
    expect(record).toMatchObject({ run_id: "run-1", lane: "agent-1", zero_write: true });
  });

  it("marks a failed run with real writes as a generic terminal failure (Class A)", () => {
    const record = recordFromHeartbeatRun({ ...base, status: "timed_out" }, 3);
    expect(record).toMatchObject({ status: "timed_out", zero_write: false });
  });

  it("never marks a succeeded run as zero_write, whatever the write count", () => {
    expect(recordFromHeartbeatRun({ ...base, status: "succeeded" }, 0)["zero_write"]).toBe(false);
    expect(recordFromHeartbeatRun({ ...base, status: "succeeded" }, 7)["zero_write"]).toBe(false);
  });
});

describe("recordFromStrandingActivity", () => {
  const base = {
    id: "activity-1",
    agentId: "agent-1",
    entityId: "issue-1",
    details: null,
    createdAt: new Date("2026-08-30T10:05:00Z"),
  };

  it("maps reconcile strandings for Class C causes to explicit Class C records", () => {
    const record = recordFromStrandingActivity({
      ...base,
      action: "recovery.reconcile_successful_run_handoff_missing_state",
    });
    expect(record).toMatchObject({
      run_id: "activity-1",
      lane: "agent-1",
      outcome: "success",
      signal_class: "C",
      cause_code: "successful_run_missing_state",
      issue_id: "issue-1",
    });
  });

  it("prefers the recorded recoveryCause detail over the action suffix", () => {
    const record = recordFromStrandingActivity({
      ...base,
      action: "recovery.reconcile_stranded_assigned_issue",
      details: { recoveryCause: "stranded_assigned_issue" },
    });
    expect(record).toMatchObject({ signal_class: "C", cause_code: "stranded_assigned_issue" });
  });

  it("returns null for non-stranding reconcile causes and rows without an agent", () => {
    expect(
      recordFromStrandingActivity({ ...base, action: "recovery.reconcile_workspace_validation_failed" }),
    ).toBeNull();
    expect(
      recordFromStrandingActivity({ ...base, agentId: null, action: "recovery.reconcile_stranded_assigned_issue" }),
    ).toBeNull();
  });
});

describe("sink comment bodies", () => {
  const crossing: ThresholdCrossingEvent = {
    event_type: "threshold_crossing",
    emitted_at: "2026-08-30T10:10:00.000Z",
    lane_id: "agent-1",
    class: "B",
    signal: "zero_write",
    count: 3,
    threshold: 3,
    first_failure_ts: "2026-08-30T09:00:00.000Z",
    last_failure_ts: "2026-08-30T10:00:00.000Z",
  };

  it("alert body carries lane, class, count, threshold, timestamps, and routing policy", () => {
    const body = alertCommentBody(crossing, "Engineering Lead");
    expect(body).toContain("Class B (zero_write)");
    expect(body).toContain("Engineering Lead (`agent-1`)");
    expect(body).toContain("**3** (threshold 3)");
    expect(body).toContain("2026-08-30T09:00:00.000Z");
    expect(body).toContain("never route via the failing lane's agent");
  });

  it("alert body includes issue and cause for Class C crossings", () => {
    const body = alertCommentBody({
      ...crossing,
      class: "C",
      signal: "stranded_assigned_issue",
      count: 1,
      threshold: 1,
      issue_id: "issue-9",
      cause_code: "successful_run_missing_state",
    });
    expect(body).toContain("`issue-9`");
    expect(body).toContain("successful_run_missing_state");
  });

  it("recovery body links the clear back to the alert", () => {
    const clear: RecoveryClearEvent = {
      event_type: "recovery_clear",
      emitted_at: "2026-08-30T11:00:00.000Z",
      lane_id: "agent-1",
      class: "B",
      signal: "zero_write",
      count: 4,
      threshold: 3,
      first_failure_ts: null,
      last_failure_ts: "2026-08-30T10:00:00.000Z",
      alert_emitted_at: "2026-08-30T10:10:00.000Z",
      run_id: "run-clear",
    };
    const body = recoveryCommentBody(clear, null);
    expect(body).toContain("cleared (Class B zero_write)");
    expect(body).toContain("cleared streak: 4 (threshold 3)");
    expect(body).toContain("2026-08-30T10:10:00.000Z");
    expect(body).toContain("`run-clear`");
  });
});

describe("reducer integration (vendored module, frozen semantics)", () => {
  const lane = "agent-1";
  const failed = (index: number, minute: number) => ({
    run_id: `run-fail-${index}`,
    lane,
    status: "failed",
    zero_write: false,
    finished_at: `2026-08-30T10:${String(minute).padStart(2, "0")}:00.000Z`,
  });

  it("crosses once at the default Class A threshold, dedups above it, and clears on success", () => {
    let state = createEmptyCounterState("2026-08-30T09:00:00Z");

    const below = applyCounterState(state, normalize([failed(1, 0), failed(2, 5)]));
    expect(below.threshold_crossings).toHaveLength(0);
    state = below.state;

    const crossing = applyCounterState(state, normalize([failed(3, 10)]));
    expect(crossing.threshold_crossings).toHaveLength(1);
    expect(crossing.threshold_crossings[0]).toMatchObject({ class: "A", count: 3, threshold: 3 });
    state = crossing.state;

    const deduped = applyCounterState(state, normalize([failed(4, 15), failed(5, 20)]));
    expect(deduped.threshold_crossings).toHaveLength(0);
    expect(deduped.emitted_alerts).toHaveLength(0);
    state = deduped.state;

    const cleared = applyCounterState(
      state,
      normalize([
        { run_id: "run-ok", lane, status: "succeeded", zero_write: false, finished_at: "2026-08-30T10:30:00.000Z" },
      ]),
    );
    expect(cleared.threshold_crossings).toHaveLength(0);
    expect(cleared.recovery_clears).toHaveLength(1);
    expect(cleared.recovery_clears[0]).toMatchObject({ class: "A", count: 5 });
    expect(cleared.state.signals[lane]?.A?.alert_emitted).toBe(false);
  });

  it("counts zero-write deaths in Class B without touching the Class A counter", () => {
    const result = applyCounterState(
      createEmptyCounterState("2026-08-30T09:00:00Z"),
      normalize([
        { run_id: "zw-1", lane, status: "failed", zero_write: true, finished_at: "2026-08-30T10:00:00.000Z" },
        { run_id: "zw-2", lane, status: "timed_out", zero_write: true, finished_at: "2026-08-30T10:05:00.000Z" },
      ]),
    );
    expect(result.state.signals[lane]?.A).toBeUndefined();
    expect(result.state.signals[lane]?.B?.current_streak).toBe(2);
    expect(result.threshold_crossings).toHaveLength(0);
  });

  it("increments Class C once per stranding (threshold 1) and replays without duplicates", () => {
    const stranding = {
      run_id: "activity-1",
      lane,
      outcome: "success",
      signal_class: "C",
      cause_code: "stranded_assigned_issue",
      issue_id: "issue-1",
      finished_at: "2026-08-30T10:00:00.000Z",
    };
    const first = applyCounterState(
      createEmptyCounterState("2026-08-30T09:00:00Z"),
      normalize([stranding]),
    );
    expect(first.threshold_crossings).toHaveLength(1);
    expect(first.threshold_crossings[0]).toMatchObject({ class: "C", count: 1, threshold: 1 });

    const replay = applyCounterState(first.state, normalize([stranding]));
    expect(replay.already_processed_run_ids).toContain("activity-1");
    expect(replay.threshold_crossings).toHaveLength(0);
  });
});
