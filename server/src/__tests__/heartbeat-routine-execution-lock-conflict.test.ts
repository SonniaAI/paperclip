import { randomUUID } from "node:crypto";
import { eq, inArray } from "drizzle-orm";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  activityLog,
  agentRuntimeState,
  agents,
  agentWakeupRequests,
  budgetPolicies,
  companies,
  companyMemberships,
  companySkills,
  costEvents,
  createDb,
  executionWorkspaces,
  heartbeatRunEvents,
  heartbeatRuns,
  issueComments,
  issueRelations,
  issueTreeHoldMembers,
  issueTreeHolds,
  issues,
  projects,
  projectWorkspaces,
  workspaceOperations,
} from "@paperclipai/db";
import {
  getEmbeddedPostgresTestSupport,
  startEmbeddedPostgresTestDatabase,
} from "./helpers/embedded-postgres.js";

const mockAdapterExecute = vi.hoisted(() =>
  vi.fn(async () => ({
    exitCode: 0,
    signal: null,
    timedOut: false,
    errorMessage: null,
    summary: "Acknowledged routine execution lock conflict claim.",
    provider: "test",
    model: "test-model",
  })),
);

vi.mock("../telemetry.ts", () => ({
  getTelemetryClient: () => ({ track: vi.fn() }),
}));

vi.mock("@paperclipai/shared/telemetry", async () => {
  const actual = await vi.importActual<typeof import("@paperclipai/shared/telemetry")>(
    "@paperclipai/shared/telemetry",
  );
  return {
    ...actual,
    trackAgentFirstHeartbeat: vi.fn(),
  };
});

vi.mock("../adapters/index.ts", async () => {
  const actual = await vi.importActual<typeof import("../adapters/index.ts")>(
    "../adapters/index.ts",
  );
  return {
    ...actual,
    getServerAdapter: vi.fn(() => ({
      supportsLocalAgentJwt: false,
      execute: mockAdapterExecute,
    })),
  };
});

import { heartbeatService } from "../services/heartbeat.ts";
import { runningProcesses } from "../adapters/index.ts";

const embeddedPostgresSupport = await getEmbeddedPostgresTestSupport();
const describeEmbeddedPostgres = embeddedPostgresSupport.supported
  ? describe
  : describe.skip;

if (!embeddedPostgresSupport.supported) {
  console.warn(
    `Skipping embedded Postgres routine execution lock conflict tests on this host: ${embeddedPostgresSupport.reason ?? "unsupported environment"}`,
  );
}

describeEmbeddedPostgres("heartbeat routine execution lock conflict claim", () => {
  let db!: ReturnType<typeof createDb>;
  let tempDb: Awaited<ReturnType<typeof startEmbeddedPostgresTestDatabase>> | null =
    null;

  beforeAll(async () => {
    tempDb = await startEmbeddedPostgresTestDatabase(
      "heartbeat-routine-execution-lock-",
    );
    db = createDb(tempDb.connectionString);
  }, 20_000);

  afterEach(async () => {
    vi.clearAllMocks();
    runningProcesses.clear();
    await heartbeatService(db).drainActiveRunExecutions();
    // Execution writes (activity log, cost events, workspace operations,
    // completion comments, seeded skills, runtime state) reference
    // runs/issues/agents and must be cleared before their parents.
    await db.delete(activityLog);
    await db.delete(heartbeatRunEvents);
    await db.delete(costEvents);
    await db.delete(workspaceOperations);
    await db.delete(issueComments);
    await db.delete(issueTreeHoldMembers);
    await db.delete(issueTreeHolds);
    await db.delete(issueRelations);
    await db.delete(issues);
    await db.delete(executionWorkspaces);
    await db.delete(projectWorkspaces);
    await db.delete(projects);
    await db.delete(agentRuntimeState);
    await db.delete(heartbeatRuns);
    await db.delete(agentWakeupRequests);
    await db.delete(budgetPolicies);
    await db.delete(companyMemberships);
    await db.delete(companySkills);
    await db.delete(agents);
    await db.delete(companies);
  });

  afterAll(async () => {
    await tempDb?.cleanup();
  });

  async function seedRoutineExecutionLockConflict() {
    const companyId = randomUUID();
    const agentId = randomUUID();

    await db.insert(companies).values({
      id: companyId,
      name: "Routine Conflict Co",
      status: "active",
      issuePrefix: `T${companyId.replace(/-/g, "").slice(0, 6).toUpperCase()}`,
      requireBoardApprovalForNewAgents: false,
    });

    await db.insert(agents).values({
      id: agentId,
      companyId,
      name: "Routine Conflict Agent",
      role: "engineer",
      status: "idle",
      adapterType: "codex_local",
      adapterConfig: {},
      runtimeConfig: { heartbeat: { maxConcurrentRuns: 3 } },
    });

    // Two open routine_execution issues sharing the partial unique index
    // tuple (company, 'routine_execution', routine, fingerprint): the
    // carrier card holds the execution lock while a fire's instance is
    // still queued. This is the live production shape observed during the
    // 2026-10-20 dispatch outage.
    const originId = randomUUID();
    const originFingerprint = `fp-${randomUUID()}`;

    const lockRunId = randomUUID();
    await db.insert(heartbeatRuns).values({
      id: lockRunId,
      companyId,
      agentId,
      status: "running",
      startedAt: new Date(),
    });

    const lockIssueId = randomUUID();
    await db.insert(issues).values({
      id: lockIssueId,
      companyId,
      title: "Routine carrier holding the execution lock",
      status: "in_progress",
      priority: "medium",
      assigneeAgentId: agentId,
      issueNumber: 1,
      identifier: `RCL-1-${randomUUID().slice(0, 8)}`,
      originKind: "routine_execution",
      originId,
      originFingerprint,
      executionRunId: lockRunId,
    });

    const conflictIssueId = randomUUID();
    await db.insert(issues).values({
      id: conflictIssueId,
      companyId,
      title: "Routine instance losing the stamp race",
      status: "todo",
      priority: "medium",
      assigneeAgentId: agentId,
      issueNumber: 2,
      identifier: `RCL-2-${randomUUID().slice(0, 8)}`,
      originKind: "routine_execution",
      originId,
      originFingerprint,
    });

    const conflictRunId = randomUUID();
    await db.insert(heartbeatRuns).values({
      id: conflictRunId,
      companyId,
      agentId,
      status: "queued",
      contextSnapshot: {
        issueId: conflictIssueId,
        wakeReason: "routine_schedule",
        responsibleUserId: "seed-responsible-user",
      },
      createdAt: new Date(Date.now() - 60_000),
    });

    // A second queued run behind the conflicting one: the claim sweep must
    // still reach it after the conflicting claim is skipped.
    const followerIssueId = randomUUID();
    await db.insert(issues).values({
      id: followerIssueId,
      companyId,
      title: "Unrelated queued issue behind the conflict",
      status: "todo",
      priority: "medium",
      assigneeAgentId: agentId,
      issueNumber: 3,
      identifier: `RCL-3-${randomUUID().slice(0, 8)}`,
    });

    const followerRunId = randomUUID();
    await db.insert(heartbeatRuns).values({
      id: followerRunId,
      companyId,
      agentId,
      status: "queued",
      contextSnapshot: {
        issueId: followerIssueId,
        wakeReason: "routine_schedule",
        responsibleUserId: "seed-responsible-user",
      },
      createdAt: new Date(Date.now() - 30_000),
    });

    return {
      companyId,
      agentId,
      lockIssueId,
      lockRunId,
      conflictIssueId,
      conflictRunId,
      followerIssueId,
      followerRunId,
    };
  }

  it("claims past the routine execution lock conflict and keeps claiming the queue", async () => {
    const seeded = await seedRoutineExecutionLockConflict();
    const service = heartbeatService(db);

    // Before the deferrable classification, the issues_open_routine_execution_uq
    // violation escaped claimQueuedRun here: the conflicting run was left
    // phantom-running, the follower run was never attempted, and the throw
    // killed the whole periodic recovery chain for every agent on that tick
    // (company-wide dispatch outage, 2026-10-20 11:06-12:07Z).
    await expect(service.resumeQueuedRuns()).resolves.toBeUndefined();

    await heartbeatService(db).drainActiveRunExecutions();

    const runs = await db
      .select()
      .from(heartbeatRuns)
      .where(
        inArray(heartbeatRuns.id, [seeded.conflictRunId, seeded.followerRunId]),
      );
    const runById = new Map(runs.map((run) => [run.id, run]));

    // Both queued runs must have been claimed and dispatched. Under this
    // minimal mock-adapter fixture the execution SETUP of each dispatched
    // run fails (executeRun's auto-checkout stamps execution_run_id and can
    // hit the same unique index - a separate follow-up), so the expected
    // terminal state here is "failed", not "succeeded". The states that
    // indicate the OLD behavior are stuck states: the conflicting run
    // phantom-"running" forever (claimed, never dispatched) and the
    // follower run still "queued" (claim sweep aborted before reaching it).
    expect(runById.get(seeded.conflictRunId)?.status).not.toBe("running");
    expect(runById.get(seeded.conflictRunId)?.status).not.toBe("queued");
    expect(runById.get(seeded.followerRunId)?.status).not.toBe("queued");

    // The conflicting issue must not have stolen or kept a lock it lost the
    // stamp race for, and the holder's lock must be untouched.
    const [lockIssue] = await db
      .select()
      .from(issues)
      .where(eq(issues.id, seeded.lockIssueId));
    expect(lockIssue.executionRunId).toBe(seeded.lockRunId);
  });
});
