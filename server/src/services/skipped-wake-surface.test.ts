import { describe, expect, it, vi } from "vitest";
import {
  DEFAULT_SKIPPED_WAKE_SURFACE_WINDOW_MINUTES,
  SKIPPED_WAKE_COMMENT_PREFIX,
  surfaceSkippedWake,
  type SkippedWakeSurfaceDeps,
} from "./skipped-wake-surface.js";

const NOW = new Date("2026-11-05T08:00:00.000Z");

function buildDeps(overrides: Partial<SkippedWakeSurfaceDeps> = {}) {
  const mocks = {
    issueExists: vi.fn(
      async (_companyId: string, _issueId: string) => true,
    ),
    hasRecentSkippedWakeComment: vi.fn(
      async (_issueId: string, _since: Date) => false,
    ),
    addComment: vi.fn(
      async (
        _issueId: string,
        _body: string,
        _actor: { agentId?: string; runId?: string | null },
      ) => ({}),
    ),
  };
  const deps: SkippedWakeSurfaceDeps = {
    issueExists: overrides.issueExists ?? mocks.issueExists,
    hasRecentSkippedWakeComment:
      overrides.hasRecentSkippedWakeComment ?? mocks.hasRecentSkippedWakeComment,
    addComment: overrides.addComment ?? mocks.addComment,
    windowMinutes: overrides.windowMinutes,
    now: () => NOW,
  };
  return { deps, mocks };
}

const baseInput = {
  companyId: "22222222-2222-4222-8222-222222222222",
  issueId: "33333333-3333-4333-8333-333333333333",
  agentId: "11111111-1111-4111-8111-111111111111",
  agentName: "Platform Engineering",
  agentStatus: "error",
  skipCode: "issue_state_guard_mismatch",
  reason: "Issue status or assignee changed before the wake could be queued",
  wakeSource: "on_demand",
  runId: null as string | null,
};

describe("surfaceSkippedWake", () => {
  it("posts one concise comment with reason, code, source, and the errored-lane recovery hint", async () => {
    const { deps, mocks } = buildDeps();
    const result = await surfaceSkippedWake(deps, baseInput);

    expect(result.commented).toBe(true);
    expect(result.deduped).toBe(false);
    expect(mocks.addComment).toHaveBeenCalledTimes(1);
    const call = mocks.addComment.mock.calls[0]!;
    const [issueId, body, actor] = call;
    expect(issueId).toBe(baseInput.issueId);
    expect(actor).toEqual({ agentId: baseInput.agentId, runId: null });
    expect(body).toContain(`${SKIPPED_WAKE_COMMENT_PREFIX} Platform Engineering`);
    expect(body).toContain(baseInput.reason);
    expect(body).toContain(baseInput.skipCode);
    expect(body).toContain("source: on_demand");
    expect(body).toContain("error status");
    expect(body).toContain("CEO lane can clear/retry it now");
    expect(body).toContain(`${DEFAULT_SKIPPED_WAKE_SURFACE_WINDOW_MINUTES} min`);
  });

  it("omits the escalation line for healthy addressees", async () => {
    const { deps, mocks } = buildDeps();
    await surfaceSkippedWake(deps, { ...baseInput, agentStatus: "idle" });
    const body = mocks.addComment.mock.calls[0]![1];
    expect(body).not.toContain("error status");
    expect(body).toContain(SKIPPED_WAKE_COMMENT_PREFIX);
  });

  it("falls back to the agent id when the name is unknown", async () => {
    const { deps, mocks } = buildDeps();
    await surfaceSkippedWake(deps, { ...baseInput, agentName: null });
    const body = mocks.addComment.mock.calls[0]![1];
    expect(body).toContain(baseInput.agentId);
  });

  it("dedupes repeat notices within the suppression window", async () => {
    const { deps, mocks } = buildDeps();
    mocks.hasRecentSkippedWakeComment.mockImplementation(async () => true);
    const result = await surfaceSkippedWake(deps, baseInput);
    expect(result.commented).toBe(false);
    expect(result.deduped).toBe(true);
    expect(mocks.addComment).not.toHaveBeenCalled();
  });

  it("does nothing when the issue no longer exists", async () => {
    const { deps, mocks } = buildDeps();
    mocks.issueExists.mockImplementation(async () => false);
    const result = await surfaceSkippedWake(deps, baseInput);
    expect(result.commented).toBe(false);
    expect(result.deduped).toBe(false);
    expect(mocks.addComment).not.toHaveBeenCalled();
  });

  it("swallows comment failures so the wake path is never broken by surfacing", async () => {
    const { deps, mocks } = buildDeps();
    mocks.addComment.mockImplementation(async () => {
      throw new Error("db down");
    });
    const result = await surfaceSkippedWake(deps, baseInput);
    expect(result.commented).toBe(false);
  });

  it("honors a custom suppression window", async () => {
    const { deps, mocks } = buildDeps({ windowMinutes: 60 });
    await surfaceSkippedWake(deps, { ...baseInput, agentStatus: "idle" });
    const since = mocks.hasRecentSkippedWakeComment.mock.calls[0]![1];
    expect(since.getTime()).toBe(NOW.getTime() - 60 * 60 * 1000);
    const body = mocks.addComment.mock.calls[0]![1];
    expect(body).toContain("suppressed for 60 min");
  });
});
