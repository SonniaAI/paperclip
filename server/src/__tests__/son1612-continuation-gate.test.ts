import { describe, expect, it } from "vitest";
import { RECOVERY_REASON_KINDS } from "../services/recovery/origins.js";
import {
  CONTINUATION_NO_MATERIAL_BACKOFF_MS,
  CONTINUATION_NO_MATERIAL_BACKOFF_THRESHOLD,
  evaluateContinuationRequeueGate,
  isNoMaterialContinuationRun,
} from "../services/recovery/service.ts";

const companyId = "company-1";
const issueId = "issue-1";

function continuationContext(overrides: Record<string, unknown> = {}) {
  return {
    issueId,
    wakeReason: "issue_continuation_needed",
    retryReason: "issue_continuation_needed",
    source: "issue.continuation_recovery",
    ...overrides,
  };
}

describe("SON-1612 evaluateContinuationRequeueGate", () => {
  const now = new Date("2026-08-31T10:00:00.000Z");
  const finishedAt = new Date("2026-08-31T09:50:00.000Z");
  const base = {
    latestContinuationFinishedAt: finishedAt,
    hasLiveExecutionPath: false,
    allChildrenRouted: true,
    now,
  };

  it("allows below the no-material streak threshold", () => {
    for (const consecutive of [0, 1, CONTINUATION_NO_MATERIAL_BACKOFF_THRESHOLD - 1]) {
      expect(evaluateContinuationRequeueGate({ ...base, consecutiveNoMaterialContinuations: consecutive }))
        .toEqual({ kind: "allow", consecutive });
    }
  });

  it("backs off to the hourly class at the threshold when a live path exists", () => {
    const decision = evaluateContinuationRequeueGate({
      ...base,
      consecutiveNoMaterialContinuations: CONTINUATION_NO_MATERIAL_BACKOFF_THRESHOLD,
      hasLiveExecutionPath: true,
    });
    expect(decision).toEqual({
      kind: "backoff",
      consecutive: CONTINUATION_NO_MATERIAL_BACKOFF_THRESHOLD,
      nextAllowedAt: new Date(finishedAt.getTime() + CONTINUATION_NO_MATERIAL_BACKOFF_MS),
    });
    expect(CONTINUATION_NO_MATERIAL_BACKOFF_MS).toBe(60 * 60 * 1000);
  });

  it("backs off at the threshold when children are not all routed", () => {
    const decision = evaluateContinuationRequeueGate({
      ...base,
      consecutiveNoMaterialContinuations: 5,
      allChildrenRouted: false,
    });
    expect(decision.kind).toBe("backoff");
  });

  it("stops at the threshold once the issue has no live path and all children are routed", () => {
    const decision = evaluateContinuationRequeueGate({
      ...base,
      consecutiveNoMaterialContinuations: 5,
    });
    expect(decision).toEqual({ kind: "stop", consecutive: 5 });
  });

  it("allows again once the backoff window has elapsed", () => {
    const decision = evaluateContinuationRequeueGate({
      ...base,
      consecutiveNoMaterialContinuations: 3,
      hasLiveExecutionPath: true,
      now: new Date(finishedAt.getTime() + CONTINUATION_NO_MATERIAL_BACKOFF_MS + 1),
    });
    expect(decision).toEqual({ kind: "allow", consecutive: 3 });
  });

  it("anchors the backoff window at now when no finishedAt is known", () => {
    const decision = evaluateContinuationRequeueGate({
      ...base,
      latestContinuationFinishedAt: null,
      consecutiveNoMaterialContinuations: 2,
      hasLiveExecutionPath: true,
    });
    expect(decision).toEqual({
      kind: "backoff",
      consecutive: 2,
      nextAllowedAt: new Date(now.getTime() + CONTINUATION_NO_MATERIAL_BACKOFF_MS),
    });
  });
});

describe("SON-1612 isNoMaterialContinuationRun", () => {
  it("counts no_op runs recorded from continuation wakes", () => {
    expect(isNoMaterialContinuationRun({
      status: "no_op",
      contextSnapshot: continuationContext(),
      livenessState: "blocked",
    })).toBe(true);
    expect(isNoMaterialContinuationRun({
      status: "no_op",
      contextSnapshot: continuationContext({ wakeReason: "issue_monitor_due", retryReason: "issue_monitor_due" }),
      livenessState: null,
    })).toBe(true);
  });

  it("counts comment-only liveness-blocked succeeded continuation runs", () => {
    expect(isNoMaterialContinuationRun({
      status: "succeeded",
      contextSnapshot: continuationContext(),
      livenessState: "blocked",
    })).toBe(true);
    expect(isNoMaterialContinuationRun({
      status: "succeeded",
      contextSnapshot: continuationContext({ retryReason: RECOVERY_REASON_KINDS.runLivenessContinuation }),
      livenessState: "blocked",
    })).toBe(true);
  });

  it("ignores non-continuation runs, material liveness states, and failures", () => {
    expect(isNoMaterialContinuationRun({
      status: "succeeded",
      contextSnapshot: { issueId, wakeReason: "issue_assigned" },
      livenessState: "blocked",
    })).toBe(false);
    expect(isNoMaterialContinuationRun({
      status: "no_op",
      contextSnapshot: { issueId, wakeReason: "issue_assigned" },
      livenessState: null,
    })).toBe(false);
    expect(isNoMaterialContinuationRun({
      status: "succeeded",
      contextSnapshot: continuationContext(),
      livenessState: "advanced",
    })).toBe(false);
    expect(isNoMaterialContinuationRun({
      status: "failed",
      contextSnapshot: continuationContext(),
      livenessState: "blocked",
    })).toBe(false);
    expect(isNoMaterialContinuationRun({
      status: "no_op",
      contextSnapshot: null,
      livenessState: null,
    })).toBe(false);
  });
});
