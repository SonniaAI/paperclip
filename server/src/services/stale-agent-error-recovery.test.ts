import { describe, expect, it, vi } from "vitest";
import {
  DEFAULT_STALE_AGENT_ERROR_CLEAR_MINUTES,
  reconcileStaleErroredAgents,
  resolveStaleAgentErrorClearMinutes,
  type ReconcileStaleErroredAgentsDeps,
  type StaleErroredAgentRow,
} from "./stale-agent-error-recovery.js";

const NOW = new Date("2026-11-05T08:00:00.000Z");

function erroredAgent(overrides: Partial<StaleErroredAgentRow> = {}): StaleErroredAgentRow {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    companyId: "22222222-2222-4222-8222-222222222222",
    name: "Platform Engineering",
    status: "error",
    errorReason: "OpenClaw gateway run timed out after 3600000ms",
    lastHeartbeatAt: new Date(NOW.getTime() - 5 * 60 * 60 * 1000),
    ...overrides,
  };
}

function buildDeps(
  overrides: {
    agents?: StaleErroredAgentRow[];
    activeRunAgentIds?: string[];
    clearError?: ReconcileStaleErroredAgentsDeps["clearError"];
    enqueueWakeup?: ReconcileStaleErroredAgentsDeps["enqueueWakeup"];
  } = {},
) {
  const agents = overrides.agents ?? [erroredAgent()];
  const activeRunAgentIds = overrides.activeRunAgentIds ?? [];
  const listErroredAgents = vi.fn(async () => agents);
  const hasActiveRuns = vi.fn(
    async (agentId: string) => activeRunAgentIds.includes(agentId),
  );
  const clearErrorMock = vi.fn(async (_agentId: string) => ({ id: "cleared" }));
  const enqueueWakeupMock = vi.fn(
    async (
      _agentId: string,
      _opts: Parameters<ReconcileStaleErroredAgentsDeps["enqueueWakeup"]>[1],
    ) => ({ id: "run-1", status: "queued" }),
  );
  const logActivity = vi.fn(
    async (_input: Parameters<ReconcileStaleErroredAgentsDeps["logActivity"]>[0]) => ({}),
  );
  const publishLiveEvent = vi.fn(
    (_event: Parameters<ReconcileStaleErroredAgentsDeps["publishLiveEvent"]>[0]) => ({}),
  );
  const deps: ReconcileStaleErroredAgentsDeps = {
    listErroredAgents,
    hasActiveRuns,
    clearError: overrides.clearError ?? clearErrorMock,
    enqueueWakeup: overrides.enqueueWakeup ?? enqueueWakeupMock,
    logActivity,
    publishLiveEvent,
    now: () => NOW,
  };
  return {
    deps,
    mocks: {
      listErroredAgents,
      hasActiveRuns,
      clearError: clearErrorMock,
      enqueueWakeup: enqueueWakeupMock,
      logActivity,
      publishLiveEvent,
    },
  };
}

describe("resolveStaleAgentErrorClearMinutes", () => {
  it("defaults to the 4-hour bounded interval", () => {
    expect(DEFAULT_STALE_AGENT_ERROR_CLEAR_MINUTES).toBe(240);
    expect(resolveStaleAgentErrorClearMinutes({} as NodeJS.ProcessEnv)).toBe(240);
  });

  it("honors a valid override", () => {
    expect(
      resolveStaleAgentErrorClearMinutes({
        PAPERCLIP_STALE_AGENT_ERROR_CLEAR_MINUTES: "30",
      } as NodeJS.ProcessEnv),
    ).toBe(30);
  });

  it("treats invalid or negative values as the default", () => {
    expect(
      resolveStaleAgentErrorClearMinutes({
        PAPERCLIP_STALE_AGENT_ERROR_CLEAR_MINUTES: "not-a-number",
      } as NodeJS.ProcessEnv),
    ).toBe(240);
    expect(
      resolveStaleAgentErrorClearMinutes({
        PAPERCLIP_STALE_AGENT_ERROR_CLEAR_MINUTES: "-5",
      } as NodeJS.ProcessEnv),
    ).toBe(240);
  });

  it("allows explicit disable with 0", () => {
    expect(
      resolveStaleAgentErrorClearMinutes({
        PAPERCLIP_STALE_AGENT_ERROR_CLEAR_MINUTES: "0",
      } as NodeJS.ProcessEnv),
    ).toBe(0);
  });
});

describe("reconcileStaleErroredAgents", () => {
  it("clears a provably stale latch, audits it, publishes the status event, and enqueues one idempotent recovery wake", async () => {
    const { deps, mocks } = buildDeps();
    const result = await reconcileStaleErroredAgents(deps);

    expect(result.scanned).toBe(1);
    expect(result.cleared).toBe(1);
    expect(result.agentIds).toEqual(["11111111-1111-4111-8111-111111111111"]);
    expect(result.wakeQueued).toBe(1);
    expect(mocks.clearError).toHaveBeenCalledWith("11111111-1111-4111-8111-111111111111");
    expect(mocks.logActivity).toHaveBeenCalledTimes(1);
    const activity = mocks.logActivity.mock.calls[0]![0];
    expect(activity.action).toBe("agent.error_auto_cleared");
    expect(activity.actorType).toBe("system");
    expect(activity.details?.previousStatus).toBe("error");
    expect(activity.details?.latchedForMinutes).toBe(300);
    expect(mocks.publishLiveEvent).toHaveBeenCalledTimes(1);
    const event = mocks.publishLiveEvent.mock.calls[0]![0];
    expect(event.type).toBe("agent.status");
    expect(event.payload.status).toBe("idle");
    expect(event.payload.outcome).toBe("stale_error_auto_cleared");
    expect(mocks.enqueueWakeup).toHaveBeenCalledTimes(1);
    const wakeAgentId = mocks.enqueueWakeup.mock.calls[0]![0];
    const wakeOpts = mocks.enqueueWakeup.mock.calls[0]![1];
    expect(wakeAgentId).toBe("11111111-1111-4111-8111-111111111111");
    expect(wakeOpts.source).toBe("automation");
    expect(wakeOpts.reason).toBe("stale_error_recovery");
    expect(wakeOpts.idempotencyKey).toContain(
      "stale_error_recovery:11111111-1111-4111-8111-111111111111:",
    );
  });

  it("leaves a recent latch alone", async () => {
    const { deps, mocks } = buildDeps({
      agents: [
        erroredAgent({
          lastHeartbeatAt: new Date(NOW.getTime() - 10 * 60 * 1000),
        }),
      ],
    });
    const result = await reconcileStaleErroredAgents(deps);
    expect(result.cleared).toBe(0);
    expect(result.skippedRecent).toBe(1);
    expect(mocks.clearError).not.toHaveBeenCalled();
    expect(mocks.enqueueWakeup).not.toHaveBeenCalled();
  });

  it("never dates a latch without lastHeartbeatAt", async () => {
    const { deps, mocks } = buildDeps({
      agents: [erroredAgent({ lastHeartbeatAt: null })],
    });
    const result = await reconcileStaleErroredAgents(deps);
    expect(result.cleared).toBe(0);
    expect(result.skippedRecent).toBe(1);
    expect(mocks.clearError).not.toHaveBeenCalled();
  });

  it("defers to in-flight runs instead of clearing under them", async () => {
    const { deps, mocks } = buildDeps({
      activeRunAgentIds: ["11111111-1111-4111-8111-111111111111"],
    });
    const result = await reconcileStaleErroredAgents(deps);
    expect(result.cleared).toBe(0);
    expect(result.skippedActiveRuns).toBe(1);
    expect(mocks.clearError).not.toHaveBeenCalled();
  });

  it("is a no-op when the sweep is disabled", async () => {
    const { deps, mocks } = buildDeps();
    const result = await reconcileStaleErroredAgents({ ...deps, clearMinutes: 0 });
    expect(result).toEqual({
      scanned: 0,
      cleared: 0,
      wakeQueued: 0,
      agentIds: [],
      skippedActiveRuns: 0,
      skippedRecent: 0,
    });
    expect(mocks.listErroredAgents).not.toHaveBeenCalled();
  });

  it("loses the clear race gracefully and keeps sweeping", async () => {
    const second = erroredAgent({
      id: "33333333-3333-4333-8333-333333333333",
      name: "QA",
      errorReason: "stale too",
    });
    const { deps, mocks } = buildDeps({
      agents: [erroredAgent(), second],
      clearError: vi.fn(async (agentId: string) => {
        if (agentId === "11111111-1111-4111-8111-111111111111") {
          throw new Error("Only agents in error status can have their error cleared");
        }
        return { id: agentId };
      }),
    });
    const result = await reconcileStaleErroredAgents(deps);
    expect(result.cleared).toBe(1);
    expect(result.agentIds).toEqual(["33333333-3333-4333-8333-333333333333"]);
  });

  it("still counts the clear when the recovery wake cannot be enqueued", async () => {
    const { deps, mocks } = buildDeps({
      enqueueWakeup: vi.fn(async () => {
        throw new Error("adapter unavailable");
      }),
    });
    const result = await reconcileStaleErroredAgents(deps);
    expect(result.cleared).toBe(1);
    expect(result.wakeQueued).toBe(0);
  });
});
