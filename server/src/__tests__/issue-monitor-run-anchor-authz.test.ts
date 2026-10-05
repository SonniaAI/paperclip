import type { Request } from "express";
import { afterEach, describe, expect, it, vi } from "vitest";
import { logger } from "../middleware/logger.js";
import { assertCanManageIssueMonitor } from "../routes/issues.js";
import type { IssueMonitorRunAnchor } from "../routes/issue-machine-state.js";

const COMPANY = "11111111-1111-4111-8111-111111111111";
const AGENT_ASSIGNEE = "22222222-2222-4222-8222-222222222222";
const AGENT_CALLER = "33333333-3333-4333-8333-333333333333";
const RUN_LIVE = "44444444-4444-4444-8444-444444444444";
const RUN_STALE = "55555555-5555-4555-8555-555555555555";

function accessSvc(decision: { allowed: boolean; explanation?: string } = { allowed: true }) {
  return { decide: async () => decision } as unknown as ReturnType<
    typeof import("../services/authorization.js").accessService
  >;
}

function req(actor: Record<string, unknown>): Request {
  return { actor } as unknown as Request;
}

function anchor(overrides: Partial<IssueMonitorRunAnchor> = {}): IssueMonitorRunAnchor {
  return {
    checkoutRunId: RUN_LIVE,
    executionRunId: null,
    getRun: async (runId: string) =>
      runId === RUN_LIVE
        ? { status: "running", companyId: COMPANY, agentId: AGENT_CALLER }
        : null,
    ...overrides,
  };
}

function gate(
  actor: Record<string, unknown>,
  runAnchor: IssueMonitorRunAnchor | null = anchor(),
  monitorChanged = true,
  access = accessSvc(),
) {
  return assertCanManageIssueMonitor(access, req(actor), COMPANY, AGENT_ASSIGNEE, monitorChanged, runAnchor);
}

function httpStatus(err: unknown): number | undefined {
  const e = err as { status?: number; statusCode?: number } | undefined;
  return e?.status ?? e?.statusCode;
}

async function deniedStatus(promise: Promise<unknown>): Promise<number | undefined> {
  try {
    await promise;
    return undefined;
  } catch (err) {
    return httpStatus(err);
  }
}

describe("SON-1524 B1 monitor run-anchor authorization (gate level)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("board actors manage monitors without touching the anchor", async () => {
    const getRun = vi.fn();
    await expect(
      gate(
        { type: "board", userId: "u1", companyIds: [COMPANY] },
        { checkoutRunId: RUN_LIVE, executionRunId: null, getRun } as IssueMonitorRunAnchor,
      ),
    ).resolves.toBeUndefined();
    expect(getRun).not.toHaveBeenCalled();
  });

  it("assignee agent keeps monitor control via the agent_key path with no run identity", async () => {
    await expect(
      gate({ type: "agent", agentId: AGENT_ASSIGNEE, companyId: COMPANY, source: "agent_key", runId: undefined }),
    ).resolves.toBeUndefined();
  });

  it("unchanged monitor policy skips the gate entirely", async () => {
    await expect(
      gate(
        { type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_key", runId: RUN_LIVE },
        anchor(),
        false,
      ),
    ).resolves.toBeUndefined();
  });

  it("runtime:manage denial still rejects before the anchor is consulted", async () => {
    expect(
      await deniedStatus(
        gate(
          { type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_jwt", runId: RUN_LIVE },
          anchor(),
          true,
          accessSvc({ allowed: false, explanation: "denied" }),
        ),
      ),
    ).toBe(403);
  });

  it("allows a non-assignee agent whose SIGNED JWT run is live and anchored to the issue", async () => {
    await expect(
      gate({ type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_jwt", runId: RUN_LIVE }),
    ).resolves.toBeUndefined();
  });

  it("rejects an agent_key actor spoofing the SAME live run id in the header (regression)", async () => {
    const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
    expect(
      await deniedStatus(
        gate({ type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_key", runId: RUN_LIVE }),
      ),
    ).toBe(403);
    expect(warn).toHaveBeenCalledWith(
      expect.objectContaining({ reason: "untrusted_run_identity_source" }),
      "monitor_run_anchor_denied",
    );
  });

  it("fails closed when the actor source is missing", async () => {
    const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
    expect(
      await deniedStatus(
        gate({ type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, runId: RUN_LIVE }),
      ),
    ).toBe(403);
    expect(warn).toHaveBeenCalledWith(
      expect.objectContaining({ reason: "untrusted_run_identity_source" }),
      "monitor_run_anchor_denied",
    );
  });

  it("rejects a live run that is not anchored to the issue (cross-issue)", async () => {
    const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
    expect(
      await deniedStatus(
        gate(
          { type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_jwt", runId: "99999999-9999-4999-8999-999999999999" },
        ),
      ),
    ).toBe(403);
    expect(warn).toHaveBeenCalledWith(
      expect.objectContaining({ reason: "run_not_anchored_to_issue" }),
      "monitor_run_anchor_denied",
    );
  });

  it("rejects a signed JWT actor with no run id", async () => {
    await expect(deniedStatus(
      gate({ type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_jwt", runId: undefined }),
    )).resolves.toBe(403);
  });

  it("rejects a stale (not running) anchored run", async () => {
    const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
    const staleAnchor = anchor({
      checkoutRunId: RUN_STALE,
      getRun: async () => ({ status: "succeeded", companyId: COMPANY, agentId: AGENT_CALLER }),
    });
    expect(
      await deniedStatus(
        gate(
          { type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_jwt", runId: RUN_STALE },
          staleAnchor,
        ),
      ),
    ).toBe(403);
    expect(warn).toHaveBeenCalledWith(
      expect.objectContaining({ reason: "run_not_live" }),
      "monitor_run_anchor_denied",
    );
  });

  it("rejects when the run store has no row for the anchored id", async () => {
    await expect(deniedStatus(
      gate(
        { type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_jwt", runId: RUN_LIVE },
        anchor({ getRun: async () => null }),
      ),
    )).resolves.toBe(403);
  });

  it("probe failure is fail-closed AND observable", async () => {
    const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
    const boomAnchor = anchor({ getRun: async () => { throw new Error("store down"); } });
    expect(
      await deniedStatus(
        gate(
          { type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_jwt", runId: RUN_LIVE },
          boomAnchor,
        ),
      ),
    ).toBe(403);
    expect(warn).toHaveBeenCalledWith(
      expect.objectContaining({ reason: "run_probe_failed" }),
      "monitor_run_anchor_denied",
    );
  });

  it("rejects a live anchored run owned by ANOTHER agent (defense-in-depth)", async () => {
    const warn = vi.spyOn(logger, "warn").mockImplementation(() => {});
    const foreignAnchor = anchor({
      getRun: async () => ({ status: "running", companyId: COMPANY, agentId: AGENT_ASSIGNEE }),
    });
    expect(
      await deniedStatus(
        gate(
          { type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_jwt", runId: RUN_LIVE },
          foreignAnchor,
        ),
      ),
    ).toBe(403);
    expect(warn).toHaveBeenCalledWith(
      expect.objectContaining({ reason: "run_owner_mismatch" }),
      "monitor_run_anchor_denied",
    );
  });

  it("rejects a live anchored run from ANOTHER company (defense-in-depth)", async () => {
    const foreignAnchor = anchor({
      getRun: async () => ({ status: "running", companyId: "88888888-8884-4888-8888-888888888888", agentId: AGENT_CALLER }),
    });
    expect(
      await deniedStatus(
        gate(
          { type: "agent", agentId: AGENT_CALLER, companyId: COMPANY, source: "agent_jwt", runId: RUN_LIVE },
          foreignAnchor,
        ),
      ),
    ).toBe(403);
  });
});
