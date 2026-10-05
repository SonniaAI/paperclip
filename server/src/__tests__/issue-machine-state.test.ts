import { describe, expect, it } from "vitest";
import {
  classifyMachineStatePatchNoop,
  ISSUE_MACHINE_STATE_PATCH_FIELDS,
  isRunAnchoredToIssue,
  MACHINE_STATE_NOOP_REASON,
} from "../routes/issue-machine-state.js";

describe("isRunAnchoredToIssue (SON-1524 B1 predicate)", () => {
  it("allows an actor run matching the issue checkoutRunId", () => {
    expect(isRunAnchoredToIssue({
      actorRunId: "run-1",
      checkoutRunId: "run-1",
      executionRunId: null,
    })).toBe(true);
  });

  it("allows an actor run matching the issue executionRunId", () => {
    expect(isRunAnchoredToIssue({
      actorRunId: "run-2",
      checkoutRunId: "run-1",
      executionRunId: "run-2",
    })).toBe(true);
  });

  it("fails closed when the issue has no run anchors", () => {
    expect(isRunAnchoredToIssue({
      actorRunId: "run-1",
      checkoutRunId: null,
      executionRunId: undefined,
    })).toBe(false);
  });

  it("fails closed on missing or empty actor run id", () => {
    expect(isRunAnchoredToIssue({
      actorRunId: undefined,
      checkoutRunId: "run-1",
      executionRunId: "run-1",
    })).toBe(false);
    expect(isRunAnchoredToIssue({
      actorRunId: "",
      checkoutRunId: "run-1",
      executionRunId: "run-1",
    })).toBe(false);
  });

  it("fails closed on a cross-issue (mismatched) run id", () => {
    expect(isRunAnchoredToIssue({
      actorRunId: "run-9",
      checkoutRunId: "run-1",
      executionRunId: "run-2",
    })).toBe(false);
  });

  it("fails closed when anchors are empty strings (no length-0 match)", () => {
    expect(isRunAnchoredToIssue({
      actorRunId: "",
      checkoutRunId: "",
      executionRunId: "",
    })).toBe(false);
  });
});

describe("classifyMachineStatePatchNoop (SON-1524 B2)", () => {
  it("flags the canonical resumeRequested-only patch with empty changes", () => {
    expect(classifyMachineStatePatchNoop({ requested: { resumeRequested: true }, effectiveChanges: {} }))
      .toEqual({ noop: true, noopReason: MACHINE_STATE_NOOP_REASON });
  });

  it("flags a status-only patch with empty changes", () => {
    expect(classifyMachineStatePatchNoop({ requested: { status: "in_review" }, effectiveChanges: {} }))
      .toEqual({ noop: true, noopReason: MACHINE_STATE_NOOP_REASON });
  });

  it("flags an executionPolicy-only patch with empty changes", () => {
    expect(classifyMachineStatePatchNoop({
      requested: { executionPolicy: { monitor: { nextCheckAt: "2026-08-31T09:00:00Z" } } },
      effectiveChanges: {},
    })).toEqual({ noop: true, noopReason: MACHINE_STATE_NOOP_REASON });
  });

  it("flags when every machine-state field is carried with empty changes", () => {
    const requested = Object.fromEntries(ISSUE_MACHINE_STATE_PATCH_FIELDS.map((f) => [f, true]));
    expect(classifyMachineStatePatchNoop({ requested, effectiveChanges: {} }))
      .toEqual({ noop: true, noopReason: MACHINE_STATE_NOOP_REASON });
  });

  it("flags assignee fields carried with empty changes", () => {
    expect(classifyMachineStatePatchNoop({
      requested: { assigneeAgentId: "agent-1", assigneeUserId: null },
      effectiveChanges: {},
    })).toEqual({ noop: true, noopReason: MACHINE_STATE_NOOP_REASON });
  });

  it("flags blockedByIssueIds carried with empty changes", () => {
    expect(classifyMachineStatePatchNoop({ requested: { blockedByIssueIds: [] }, effectiveChanges: {} }))
      .toEqual({ noop: true, noopReason: MACHINE_STATE_NOOP_REASON });
  });

  it("does not flag a comment-only patch (no machine-state intent)", () => {
    expect(classifyMachineStatePatchNoop({ requested: { comment: "hello" }, effectiveChanges: {} }))
      .toEqual({ noop: false });
  });

  it("does not flag when a machine-state field actually changed", () => {
    expect(classifyMachineStatePatchNoop({
      requested: { status: "in_review" },
      effectiveChanges: { status: { from: "in_progress", to: "in_review" } },
    })).toEqual({ noop: false });
  });

  it("does not flag when a non-machine field changed alongside machine-state intent", () => {
    expect(classifyMachineStatePatchNoop({
      requested: { resumeRequested: true, title: "new title" },
      effectiveChanges: { title: { from: "old", to: "new title" } },
    })).toEqual({ noop: false });
  });

  it("treats null/undefined effectiveChanges as empty", () => {
    expect(classifyMachineStatePatchNoop({ requested: { status: "todo" }, effectiveChanges: null }))
      .toEqual({ noop: true, noopReason: MACHINE_STATE_NOOP_REASON });
    expect(classifyMachineStatePatchNoop({ requested: { status: "todo" }, effectiveChanges: undefined }))
      .toEqual({ noop: true, noopReason: MACHINE_STATE_NOOP_REASON });
  });

  it("exposes a single stable reason literal that discloses no extra state", () => {
    expect(MACHINE_STATE_NOOP_REASON).toBe("machine_state_patch_had_no_effect");
  });
});
