import { randomUUID } from "node:crypto";
import { and, eq } from "drizzle-orm";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  activityLog,
  agents,
  agentWakeupRequests,
  companies,
  createDb,
  heartbeatRuns,
  issueRecoveryActions,
  issues,
} from "@paperclipai/db";
import {
  getEmbeddedPostgresTestSupport,
  startEmbeddedPostgresTestDatabase,
} from "./helpers/embedded-postgres.js";
import { DISPOSITION_REPAIR_MAX_ATTEMPTS } from "../services/recovery/disposition-repair.js";
import { collectDispositionRepairSourceState } from "../services/recovery/disposition-repair.js";
import { recoveryService } from "../services/recovery/service.js";
import {
  classifyWriteTransportFailure,
  writeTransportRecoveryWarning,
} from "../services/recovery/write-transport-failure.js";

const embeddedPostgresSupport = await getEmbeddedPostgresTestSupport();
const describeEmbeddedPostgres = embeddedPostgresSupport.supported ? describe : describe.skip;

describe("write-transport failure classification", () => {
  it("classifies a cross-issue write denial (403) as write-transport", () => {
    const failure = classifyWriteTransportFailure({
      id: "run-1",
      status: "failed",
      errorCode: null,
      error:
        'Paperclip API request failed: POST /api/issues/SON-1404/comments responded 403 ' +
        '{"error":{"code":"cross_issue_influence","message":"cross-issue influence requires a run context"}}',
      resultJson: null,
    });
    expect(failure).toEqual({ kind: "write_denial", matched: "cross_issue_influence" });
  });

  it("classifies denial codes carried in resultJson as write-transport", () => {
    const failure = classifyWriteTransportFailure({
      id: "run-2",
      status: "failed",
      errorCode: null,
      error: null,
      resultJson: {
        summary: "wake aborted",
        error: "cross_issue_influence_run_context_required: interactive runs cannot write",
      },
    });
    expect(failure).toEqual({
      kind: "write_denial",
      matched: "cross_issue_influence_run_context_required",
    });
  });

  it("classifies issue-write boundary codes in errorCode as write-transport", () => {
    const failure = classifyWriteTransportFailure({
      id: "run-3",
      status: "cancelled",
      errorCode: "issue_write_assignee_run_lock",
      error: "write refused: assignee run lock",
      resultJson: null,
    });
    expect(failure).toEqual({ kind: "write_denial", matched: "issue_write_assignee_run_lock" });
  });

  it("classifies an agent auth flap as write-transport", () => {
    const failure = classifyWriteTransportFailure({
      id: "run-4",
      status: "failed",
      errorCode: null,
      error: "Agent token did not verify; obtain fresh credentials and retry",
      resultJson: null,
    });
    expect(failure).toEqual({ kind: "auth_flap", matched: "Agent token did not verify" });
  });

  it("classifies a board-API/interaction 5xx as write-transport", () => {
    const failure = classifyWriteTransportFailure({
      id: "run-5",
      status: "failed",
      errorCode: null,
      error:
        "failed to resolve interaction on the board API: POST /api/issues/X/interactions/Y/accept " +
        "responded 500",
      resultJson: null,
    });
    expect(failure?.kind).toBe("board_api_5xx");
  });

  it("does not classify provider or configuration failures as write-transport", () => {
    expect(classifyWriteTransportFailure({
      id: "run-6",
      status: "failed",
      errorCode: "adapter_failed",
      error: "You've hit your usage limit for GPT-5. Try again at 12:00 AM (UTC).",
      resultJson: null,
    })).toBeNull();
    expect(classifyWriteTransportFailure({
      id: "run-7",
      status: "failed",
      errorCode: "configuration_incomplete",
      error: "missing api key: credentials were not configured",
      resultJson: null,
    })).toBeNull();
    expect(classifyWriteTransportFailure({
      id: "run-8",
      status: "failed",
      errorCode: "agent_not_invokable",
      error: "agent is not invokable",
      resultJson: null,
    })).toBeNull();
  });

  it("does not classify a generic provider 5xx without board context as write-transport", () => {
    expect(classifyWriteTransportFailure({
      id: "run-9",
      status: "failed",
      errorCode: "codex_transient_upstream",
      error: "provider returned 503 after 3 attempts",
      resultJson: null,
    })).toBeNull();
  });

  it("returns null for a missing run and renders a wake warning", () => {
    expect(classifyWriteTransportFailure(null)).toBeNull();
    const warning = writeTransportRecoveryWarning({
      kind: "write_denial",
      matched: "cross_issue_influence",
    });
    expect(warning).toContain("Recovery deferred");
    expect(warning).toContain("cross_issue_influence");
  });
});

describeEmbeddedPostgres("write-transport recovery deferral (SON-1775)", () => {
  let tempDb: Awaited<ReturnType<typeof startEmbeddedPostgresTestDatabase>> | null = null;
  let db: ReturnType<typeof createDb>;

  beforeAll(async () => {
    tempDb = await startEmbeddedPostgresTestDatabase("paperclip-write-transport-recovery-");
    db = createDb(tempDb.connectionString);
  }, 30_000);

  afterEach(async () => {
    await db.delete(issueRecoveryActions);
    await db.delete(activityLog);
    await db.delete(heartbeatRuns);
    await db.delete(agentWakeupRequests);
    await db.delete(issues);
    await db.delete(agents);
    await db.delete(companies);
  });

  afterAll(async () => {
    await tempDb?.cleanup();
  });

  async function seedCompany() {
    const companyId = randomUUID();
    const managerId = randomUUID();
    const coderId = randomUUID();
    const sourceIssueId = randomUUID();
    const prefix = `WT${companyId.replaceAll("-", "").slice(0, 6).toUpperCase()}`;
    await db.insert(companies).values({
      id: companyId,
      name: "Write Transport Co",
      issuePrefix: prefix,
      requireBoardApprovalForNewAgents: false,
    });
    await db.insert(agents).values([
      {
        id: managerId,
        companyId,
        name: "CTO",
        role: "cto",
        status: "idle",
        adapterType: "codex_local",
        adapterConfig: {},
        runtimeConfig: {},
        permissions: {},
      },
      {
        id: coderId,
        companyId,
        name: "Coder",
        role: "engineer",
        status: "idle",
        reportsTo: managerId,
        adapterType: "codex_local",
        adapterConfig: {},
        runtimeConfig: {},
        permissions: {},
      },
    ]);
    await db.insert(issues).values({
      id: sourceIssueId,
      companyId,
      title: "Awaiting execution wake after recorded decision",
      status: "in_progress",
      priority: "medium",
      assigneeAgentId: coderId,
      issueNumber: 1,
      identifier: `${prefix}-1`,
    });
    return { companyId, managerId, coderId, sourceIssueId };
  }

  async function seedFailedContinuationRun(input: {
    companyId: string;
    agentId: string;
    issueId: string;
    error: string;
    errorCode?: string | null;
    retryReason?: string;
    extraContext?: Record<string, unknown>;
  }) {
    // SON-1775 seeds model a wake that died at the board API before any
    // provider work started. Without the bootstrap executionRecovery
    // evidence the legacy-execution reconciler (not the stranded paths
    // this suite exercises) intercepts the run and creates an
    // active_run_watchdog instead of reaching the escalation paths.
    const runId = randomUUID();
    await db.insert(heartbeatRuns).values({
      id: runId,
      companyId: input.companyId,
      agentId: input.agentId,
      invocationSource: "manual",
      status: "failed",
      error: input.error,
      errorCode: input.errorCode ?? null,
      startedAt: new Date("2026-09-04T18:00:00.000Z"),
      finishedAt: new Date("2026-09-04T18:01:00.000Z"),
      resultJson: {
        executionRecovery: { kind: "bootstrap", providerWorkStarted: false },
      },
      contextSnapshot: {
        issueId: input.issueId,
        retryReason: input.retryReason ?? "issue_continuation_needed",
        ...(input.extraContext ?? {}),
      },
    });
    return runId;
  }

  async function issueStatus(issueId: string) {
    const [row] = await db.select().from(issues).where(eq(issues.id, issueId));
    return row?.status ?? null;
  }

  async function deferralActivityRows(issueId: string) {
    return db
      .select()
      .from(activityLog)
      .where(
        and(
          eq(activityLog.entityType, "issue"),
          eq(activityLog.entityId, issueId),
          eq(activityLog.action, "issue.write_transport_recovery_deferred"),
        ),
      );
  }

  it("does not flip the issue to blocked when retries exhaust on a 403 cross-issue denial", async () => {
    const { companyId, coderId, sourceIssueId } = await seedCompany();
    await seedFailedContinuationRun({
      companyId,
      agentId: coderId,
      issueId: sourceIssueId,
      error:
        'Paperclip API request failed: POST /api/issues/WT-1/comments responded 403 ' +
        '{"error":{"code":"cross_issue_influence"}}',
    });
    const enqueueWakeup = vi.fn(async () => null);
    const scheduleRecoveryRetry = vi.fn(async () => null);
    const recovery = recoveryService(db, { enqueueWakeup, scheduleRecoveryRetry });

    const result = await recovery.reconcileStrandedAssignedIssues();

    expect(await issueStatus(sourceIssueId)).toBe("in_progress");
    expect(await db.select().from(issueRecoveryActions)).toHaveLength(0);
    expect(result.escalated).toBe(0);
    // The deferral keeps the retry policy alive: the failed predecessor is
    // re-driven through the durable retry scheduler.
    expect(scheduleRecoveryRetry).toHaveBeenCalledTimes(1);
    const deferrals = await deferralActivityRows(sourceIssueId);
    expect(deferrals).toHaveLength(1);
    expect(deferrals[0]?.details).toMatchObject({
      source: "recovery.reconcile_stranded_transport_deferred",
      transportFailureKind: "write_denial",
    });

    // A second pass against the same failing run must not duplicate the
    // deferral record (dedupe per failing run).
    await recovery.reconcileStrandedAssignedIssues();
    expect(await deferralActivityRows(sourceIssueId)).toHaveLength(1);
  });

  it("treats an interaction-route 500 as retryable transport failure, not a blocked verdict", async () => {
    const { companyId, coderId, sourceIssueId } = await seedCompany();
    await seedFailedContinuationRun({
      companyId,
      agentId: coderId,
      issueId: sourceIssueId,
      error:
        "failed to resolve interaction on the board API: POST /api/issues/WT-1/interactions/abc/accept responded 500",
    });
    const enqueueWakeup = vi.fn(async () => null);
    const scheduleRecoveryRetry = vi.fn(async () => null);
    const recovery = recoveryService(db, { enqueueWakeup, scheduleRecoveryRetry });

    const result = await recovery.reconcileStrandedAssignedIssues();

    expect(await issueStatus(sourceIssueId)).toBe("in_progress");
    expect(await db.select().from(issueRecoveryActions)).toHaveLength(0);
    expect(result.escalated).toBe(0);
    expect(scheduleRecoveryRetry).toHaveBeenCalledTimes(1);
    const deferrals = await deferralActivityRows(sourceIssueId);
    expect(deferrals).toHaveLength(1);
    expect(deferrals[0]?.details).toMatchObject({
      transportFailureKind: "board_api_5xx",
    });
  });

  it("does not flip to blocked when the stall disposition-repair exhausts on a write denial", async () => {
    const { companyId, coderId, sourceIssueId } = await seedCompany();
    const [issue] = await db.select().from(issues).where(eq(issues.id, sourceIssueId));
    const state = await collectDispositionRepairSourceState(db, { issue: issue! });
    await seedFailedContinuationRun({
      companyId,
      agentId: coderId,
      issueId: sourceIssueId,
      error:
        'Paperclip API request failed: POST /api/issues/WT-1/comments responded 403 ' +
        '{"error":{"code":"cross_issue_influence_run_context_required"}}',
      retryReason: "issue_disposition_repair",
      extraContext: {
        dispositionRepairAttempt: DISPOSITION_REPAIR_MAX_ATTEMPTS,
        dispositionRepairFingerprint: state.fingerprint,
      },
    });
    const enqueueWakeup = vi.fn(async () => null);
    const recovery = recoveryService(db, { enqueueWakeup });

    const result = await recovery.reconcileStrandedAssignedIssues();

    expect(await issueStatus(sourceIssueId)).toBe("in_progress");
    expect(await db.select().from(issueRecoveryActions)).toHaveLength(0);
    expect(result.escalated).toBe(0);
    const deferrals = await deferralActivityRows(sourceIssueId);
    expect(deferrals).toHaveLength(1);
    expect(deferrals[0]?.details).toMatchObject({
      source: "recovery.reconcile_disposition_repair_transport_deferred",
      transportFailureKind: "write_denial",
    });
  });

  it("still flips to blocked for a genuine non-transport continuation failure", async () => {
    const { companyId, coderId, sourceIssueId } = await seedCompany();
    await seedFailedContinuationRun({
      companyId,
      agentId: coderId,
      issueId: sourceIssueId,
      error: "agent is not invokable: adapter disabled",
      errorCode: "agent_not_invokable",
    });
    const enqueueWakeup = vi.fn(async () => null);
    const recovery = recoveryService(db, { enqueueWakeup });

    const result = await recovery.reconcileStrandedAssignedIssues();

    expect(await issueStatus(sourceIssueId)).toBe("blocked");
    expect(result.escalated).toBe(1);
    const [action] = await db.select().from(issueRecoveryActions);
    expect(action).toMatchObject({
      sourceIssueId,
      ownerType: "board",
      cause: "stranded_assigned_issue",
    });
    expect(await deferralActivityRows(sourceIssueId)).toHaveLength(0);
  });
});
