import { randomUUID } from "node:crypto";
import { and, eq, inArray, sql } from "drizzle-orm";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  activityLog,
  agents,
  agentRuntimeState,
  agentTaskSessions,
  agentWakeupRequests,
  costEvents,
  companies,
  companyMemberships,
  companySkills,
  createDb,
  executionWorkspaces,
  heartbeatRunEvents,
  heartbeatRuns,
  issueComments,
  issueRelations,
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
    summary: "Execution reconciler dispatch acknowledged.",
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
  const actual = await vi.importActual<typeof import("../adapters/index.ts")>("../adapters/index.ts");
  return {
    ...actual,
    getServerAdapter: vi.fn(() => ({
      supportsLocalAgentJwt: false,
      execute: mockAdapterExecute,
    })),
  };
});

import { heartbeatService } from "../services/heartbeat.ts";
import {
  executionReconcilerService,
  isAutomaticVerificationPolicyEligible,
  resolveExecutionReconcilerSettings,
  type ExecutionReconcilerSettings,
} from "../services/execution-reconciler.js";
import { runningProcesses } from "../adapters/index.ts";

const embeddedPostgresSupport = await getEmbeddedPostgresTestSupport();
const describeEmbeddedPostgres = embeddedPostgresSupport.supported ? describe : describe.skip;

if (!embeddedPostgresSupport.supported) {
  console.warn(
    `Skipping execution reconciler tests on this host: ${embeddedPostgresSupport.reason ?? "unsupported environment"}`,
  );
}

function testSettings(overrides: Partial<ExecutionReconcilerSettings> = {}): ExecutionReconcilerSettings {
  return {
    ...resolveExecutionReconcilerSettings(),
    enabled: true,
    dryRun: false,
    stallThresholdMs: 60_000,
    workerLeaseTtlMs: 60_000,
    blockedCooldownMs: 60_000,
    maxDispatchesPerTick: 10,
    defaultWorkerAgentId: null,
    verifierAgentId: null,
    disabledCompanies: [],
    ...overrides,
  };
}

describe("automatic verification policy eligibility", () => {
  const baseIssue = {
    status: "todo",
    originKind: "manual",
    title: "Build the onboarding page",
    description: "Ship a customer-facing product deliverable.",
    executionPolicy: null,
  };

  it("selects actionable deliverables while excluding meta-records and parked work", () => {
    expect(isAutomaticVerificationPolicyEligible(baseIssue)).toBe(true);
    expect(
      isAutomaticVerificationPolicyEligible({
        ...baseIssue,
        title: "Repair AWS deployment healthcheck",
        description: "Restore the infrastructure health endpoint.",
      }),
    ).toBe(true);

    for (const title of [
      "[CEOdecisionrecord] Daily disposition",
      "[CEOreceipt/correction] Board response",
      "[Batchcarry-over] Nightly carry-over",
      "[Decisionshealth] Queue status",
      "Decisions health — current queue",
    ]) {
      expect(isAutomaticVerificationPolicyEligible({ ...baseIssue, title })).toBe(false);
    }

    expect(
      isAutomaticVerificationPolicyEligible({
        ...baseIssue,
        description: "Record class: CEO decision record\nRoutine administrative note.",
      }),
    ).toBe(false);
    expect(
      isAutomaticVerificationPolicyEligible({
        ...baseIssue,
        originKind: "issue_productivity_review",
      }),
    ).toBe(false);
    expect(isAutomaticVerificationPolicyEligible({ ...baseIssue, status: "backlog" })).toBe(false);
    expect(isAutomaticVerificationPolicyEligible({ ...baseIssue, status: "blocked" })).toBe(false);
    expect(
      isAutomaticVerificationPolicyEligible({
        ...baseIssue,
        executionPolicy: { mode: "manual", stages: [] },
      }),
    ).toBe(false);
  });

  it("does not infer administrative status from broad product or decision language", () => {
    expect(
      isAutomaticVerificationPolicyEligible({
        ...baseIssue,
        title: "Decisions dashboard for product launch",
        description: "The CEO asked for a real product deliverable.",
      }),
    ).toBe(true);
  });
});

describeEmbeddedPostgres("execution reconciler", () => {
  let tempDb: Awaited<ReturnType<typeof startEmbeddedPostgresTestDatabase>> | null = null;
  let db: ReturnType<typeof createDb>;

  beforeAll(async () => {
    tempDb = await startEmbeddedPostgresTestDatabase("paperclip-execution-reconciler-");
    db = createDb(tempDb.connectionString);
  }, 30_000);

  afterEach(async () => {
    await heartbeatService(db).drainActiveRunExecutions();
    vi.clearAllMocks();
    runningProcesses.clear();
    await db.delete(activityLog);
    await db.delete(heartbeatRunEvents);
    await db.delete(costEvents);
    await db.delete(workspaceOperations);
    await db.delete(issueComments);
    await db.delete(issueRelations);
    await db.delete(issues);
    await db.delete(executionWorkspaces);
    await db.delete(projectWorkspaces);
    await db.delete(projects);
    await db.delete(heartbeatRuns);
    await db.delete(agentTaskSessions);
    await db.delete(agentWakeupRequests);
    await db.delete(agentRuntimeState);
    await db.delete(agents);
    await db.delete(companySkills);
    await db.delete(companyMemberships);
    await db.delete(companies);
  });

  afterAll(async () => {
    await tempDb?.cleanup();
  }, 30_000);

  type Seed = {
    companyId: string;
    managerId: string;
    coderId: string;
  };

  async function seedCompany(): Promise<Seed> {
    const companyId = randomUUID();
    const managerId = randomUUID();
    const coderId = randomUUID();
    const issuePrefix = `XR${companyId.replace(/-/g, "").slice(0, 6).toUpperCase()}`;

    await db.insert(companies).values({
      id: companyId,
      name: "Execution Reconciler Co",
      issuePrefix,
      requireBoardApprovalForNewAgents: false,
      defaultResponsibleUserId: "reconciler-test-responsible-user",
    });

    await db.insert(agents).values([
      {
        id: managerId,
        companyId,
        name: "Lead",
        role: "cto",
        status: "idle",
        adapterType: "codex_local",
        adapterConfig: {},
        runtimeConfig: { heartbeat: { wakeOnDemand: true } },
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
        runtimeConfig: { heartbeat: { wakeOnDemand: true } },
        permissions: {},
      },
    ]);

    return { companyId, managerId, coderId };
  }

  async function insertIssue(input: {
    companyId: string;
    title: string;
    status: string;
    number: number;
    assigneeAgentId?: string | null;
    projectId?: string | null;
    ageHours?: number;
    originKind?: string;
    description?: string | null;
  }) {
    const id = randomUUID();
    const when = new Date(Date.now() - (input.ageHours ?? 1) * 60 * 60 * 1000);
    const [company] = await db.select().from(companies).where(eq(companies.id, input.companyId));
    await db.insert(issues).values({
      id,
      companyId: input.companyId,
      title: input.title,
      description: input.description ?? null,
      status: input.status,
      priority: "medium",
      assigneeAgentId: input.assigneeAgentId ?? null,
      issueNumber: input.number,
      identifier: `${company.issuePrefix}-${input.number}`,
      projectId: input.projectId ?? null,
      originKind: input.originKind ?? "manual",
      createdAt: when,
      updatedAt: when,
    });
    return id;
  }

  async function blocks(companyId: string, blockerIssueId: string, dependentIssueId: string) {
    await db.insert(issueRelations).values({
      companyId,
      issueId: blockerIssueId,
      relatedIssueId: dependentIssueId,
      type: "blocks",
    });
  }

  async function insertStaleQueuedRun(input: {
    companyId: string;
    agentId: string;
    issueId: string;
    createdAt?: Date;
    contextSnapshot?: Record<string, unknown>;
  }) {
    const runId = randomUUID();
    const wakeupRequestId = randomUUID();
    const createdAt = input.createdAt ?? new Date(Date.now() - 60 * 60 * 1000);
    await db.insert(agentWakeupRequests).values({
      id: wakeupRequestId,
      companyId: input.companyId,
      agentId: input.agentId,
      source: "automation",
      triggerDetail: "system",
      reason: "stale_queue_test",
      payload: { issueId: input.issueId },
      status: "queued",
      requestedAt: createdAt,
      createdAt,
      updatedAt: createdAt,
    });
    await db.insert(heartbeatRuns).values({
      id: runId,
      companyId: input.companyId,
      agentId: input.agentId,
      invocationSource: "automation",
      triggerDetail: "system",
      status: "queued",
      wakeupRequestId,
      contextSnapshot: input.contextSnapshot ?? { issueId: input.issueId },
      createdAt,
      updatedAt: createdAt,
    });
    await db
      .update(agentWakeupRequests)
      .set({ runId })
      .where(eq(agentWakeupRequests.id, wakeupRequestId));
    await db
      .update(issues)
      .set({ executionRunId: runId, checkoutRunId: runId })
      .where(eq(issues.id, input.issueId));
    return { runId, wakeupRequestId };
  }

  it("dispatches dependency-eligible assigned-backlog work and promotes it to todo", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "T1 eligible assigned backlog",
      status: "backlog",
      number: 1,
      assigneeAgentId: seed.coderId,
      ageHours: 96,
    });

    const wakes: Array<{ agentId: string; opts: Record<string, unknown> }> = [];
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (agentId, opts) => {
        wakes.push({ agentId, opts });
        return { id: randomUUID() };
      },
      getSettings: () => testSettings(),
    });

    const result = await reconciler.reconcileExecutionState({ companyId: seed.companyId });

    expect(result.eligible).toBe(1);
    expect(result.promoted).toBe(1);
    expect(result.dispatched).toBe(1);
    expect(wakes).toHaveLength(1);
    expect(wakes[0].agentId).toBe(seed.coderId);
    expect((wakes[0].opts.payload as Record<string, unknown>).issueId).toBe(issueId);

    const [issue] = await db.select().from(issues).where(eq(issues.id, issueId));
    expect(issue.status).toBe("todo");
  });

  it("attaches verification only to active deliverables, not meta-records or parked work", async () => {
    const seed = await seedCompany();
    const activeIds = [
      await insertIssue({
        companyId: seed.companyId,
        title: "Ship customer onboarding",
        description: "Add a product flow for new customers.",
        status: "in_progress",
        number: 1,
        assigneeAgentId: seed.coderId,
        ageHours: 0,
      }),
      await insertIssue({
        companyId: seed.companyId,
        title: "Repair AWS deployment healthcheck",
        description: "Restore the infrastructure health endpoint.",
        status: "in_progress",
        number: 2,
        assigneeAgentId: seed.coderId,
        ageHours: 0,
      }),
    ];
    const excludedIds = [
      await insertIssue({
        companyId: seed.companyId,
        title: "Routine review note",
        status: "in_progress",
        number: 3,
        assigneeAgentId: seed.coderId,
        originKind: "issue_productivity_review",
        ageHours: 0,
      }),
      ...await Promise.all([
        "[CEOdecisionrecord] Daily disposition",
        "[CEOreceipt/correction] Board response",
        "[Batchcarry-over] Nightly carry-over",
        "[Decisionshealth] Queue status",
      ].map((title, index) => insertIssue({
        companyId: seed.companyId,
        title,
        status: "in_progress",
        number: 4 + index,
        assigneeAgentId: seed.coderId,
        ageHours: 0,
      }))),
      await insertIssue({
        companyId: seed.companyId,
        title: "Board process note",
        description: "Record class: CEO decision record\nAdministrative context only.",
        status: "in_progress",
        number: 8,
        assigneeAgentId: seed.coderId,
        ageHours: 0,
      }),
      await insertIssue({
        companyId: seed.companyId,
        title: "Blocked deliverable",
        status: "blocked",
        number: 10,
        assigneeAgentId: seed.coderId,
        ageHours: 0,
      }),
    ];
    const promotedId = await insertIssue({
      companyId: seed.companyId,
      title: "Parked deliverable now ready",
      status: "backlog",
      number: 9,
      assigneeAgentId: seed.coderId,
      ageHours: 0,
    });
    const manualPolicyId = await insertIssue({
      companyId: seed.companyId,
      title: "Manual verification configuration",
      status: "in_progress",
      number: 11,
      assigneeAgentId: seed.coderId,
      ageHours: 0,
    });
    const manualPolicy = { mode: "manual", commentRequired: false, stages: [] };
    await db.update(issues).set({ executionPolicy: manualPolicy }).where(eq(issues.id, manualPolicyId));

    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async () => ({ id: randomUUID() }),
      getSettings: () => testSettings({ verifierAgentId: seed.managerId }),
    });
    const result = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    const allIds = [...activeIds, ...excludedIds, promotedId, manualPolicyId];
    const savedRows = await db.select().from(issues).where(inArray(issues.id, allIds));
    const byId = new Map(savedRows.map((row) => [row.id, row]));

    expect(savedRows.filter((row) => row.executionPolicy != null).map((row) => row.title).sort()).toEqual([
      "Manual verification configuration",
      "Parked deliverable now ready",
      "Repair AWS deployment healthcheck",
      "Ship customer onboarding",
    ]);
    expect(result.verificationPoliciesAttached).toBe(3);
    for (const id of activeIds) {
      expect(byId.get(id)?.executionPolicy).toMatchObject({
        mode: "normal",
        commentRequired: true,
        stages: [{ type: "review" }],
      });
    }
    expect(byId.get(promotedId)).toMatchObject({ status: "todo" });
    expect(byId.get(promotedId)?.executionPolicy).toMatchObject({ mode: "normal" });
    for (const id of excludedIds) expect(byId.get(id)?.executionPolicy).toBeNull();
    expect(byId.get(manualPolicyId)?.executionPolicy).toEqual(manualPolicy);
  });

  it("admits work by agent capacity and keeps another owner's work moving", async () => {
    const seed = await seedCompany();
    await db.update(agents)
      .set({ runtimeConfig: { heartbeat: { wakeOnDemand: true, maxConcurrentRuns: 1 } } })
      .where(eq(agents.id, seed.coderId));
    const holderIssueId = await insertIssue({
      companyId: seed.companyId,
      title: "Coder capacity holder",
      status: "in_progress",
      number: 1,
      assigneeAgentId: seed.coderId,
      ageHours: 0,
    });
    const coderIssueId = await insertIssue({
      companyId: seed.companyId,
      title: "Coder waits for a slot",
      status: "todo",
      number: 2,
      assigneeAgentId: seed.coderId,
    });
    const managerIssueId = await insertIssue({
      companyId: seed.companyId,
      title: "Lead-owned independent work",
      status: "todo",
      number: 3,
      assigneeAgentId: seed.managerId,
    });
    const holderRunId = randomUUID();
    await db.insert(heartbeatRuns).values({
      id: holderRunId,
      companyId: seed.companyId,
      agentId: seed.coderId,
      status: "running",
      startedAt: new Date(),
      contextSnapshot: { issueId: holderIssueId },
    });

    const wakes: Array<{ agentId: string; issueId: string }> = [];
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (agentId, opts) => {
        const issueId = String((opts.payload as Record<string, unknown>).issueId);
        wakes.push({ agentId, issueId });
        await insertStaleQueuedRun({ companyId: seed.companyId, agentId, issueId });
        return { id: randomUUID() };
      },
      getSettings: () => testSettings(),
      getQueuedRunDispatchBlockReason: async (agentId) => {
        const [{ count }] = await db
          .select({ count: sql<number>`count(*)` })
          .from(heartbeatRuns)
          .where(and(eq(heartbeatRuns.agentId, agentId), eq(heartbeatRuns.status, "running")));
        const limit = agentId === seed.coderId ? 1 : 20;
        return Number(count ?? 0) >= limit ? "concurrency_limit" : null;
      },
    });

    const first = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    expect(first.dispatched).toBe(1);
    expect(wakes).toEqual([{ agentId: seed.managerId, issueId: managerIssueId }]);
    expect(first.actions).toContainEqual({
      kind: "skipped",
      issueId: coderIssueId,
      reason: "concurrency_limit",
    });

    await db.update(heartbeatRuns)
      .set({ status: "succeeded", finishedAt: new Date(), updatedAt: new Date() })
      .where(eq(heartbeatRuns.id, holderRunId));
    const second = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    const third = await reconciler.reconcileExecutionState({ companyId: seed.companyId });

    expect(second.dispatched).toBe(1);
    expect(third.dispatched).toBe(0);
    expect(wakes).toEqual([
      { agentId: seed.managerId, issueId: managerIssueId },
      { agentId: seed.coderId, issueId: coderIssueId },
    ]);
  });

  it("fills configured concurrency slots without allowing an unbounded queued backlog", async () => {
    const seed = await seedCompany();
    await db.update(agents)
      .set({ runtimeConfig: { heartbeat: { wakeOnDemand: true, maxConcurrentRuns: 3 } } })
      .where(eq(agents.id, seed.coderId));
    const issueIds: string[] = [];
    for (let index = 0; index < 4; index += 1) {
      issueIds.push(await insertIssue({
        companyId: seed.companyId,
        title: `Concurrent work ${index + 1}`,
        status: "todo",
        number: index + 1,
        assigneeAgentId: seed.coderId,
      }));
    }
    const wakes: string[] = [];
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (agentId, opts) => {
        const issueId = String((opts.payload as Record<string, unknown>).issueId);
        wakes.push(issueId);
        await insertStaleQueuedRun({ companyId: seed.companyId, agentId, issueId });
        return { id: randomUUID() };
      },
      getSettings: () => testSettings(),
    });

    const first = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    expect(first.dispatched).toBe(3);
    expect(wakes).toHaveLength(3);
    expect(first.actions).toContainEqual({
      kind: "skipped",
      issueId: issueIds[3],
      reason: "concurrency_limit",
    });

    const second = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    expect(second.dispatched).toBe(0);
    expect(wakes).toHaveLength(3);

    const [releasedRun] = await db
      .select({ id: heartbeatRuns.id, wakeupRequestId: heartbeatRuns.wakeupRequestId })
      .from(heartbeatRuns)
      .where(and(eq(heartbeatRuns.agentId, seed.coderId), eq(heartbeatRuns.status, "queued")))
      .limit(1);
    await db.update(heartbeatRuns)
      .set({ status: "cancelled", finishedAt: new Date(), updatedAt: new Date() })
      .where(eq(heartbeatRuns.id, releasedRun.id));
    if (releasedRun.wakeupRequestId) {
      await db.update(agentWakeupRequests)
        .set({ status: "cancelled", finishedAt: new Date(), updatedAt: new Date() })
        .where(eq(agentWakeupRequests.id, releasedRun.wakeupRequestId));
    }
    await db.update(issues)
      .set({ status: "done", executionRunId: null, checkoutRunId: null })
      .where(eq(issues.id, issueIds[0]));

    const afterCapacityReleased = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    expect(afterCapacityReleased.dispatched).toBe(1);
    expect(wakes).toHaveLength(4);
    const settled = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    expect(settled.dispatched).toBe(0);
    expect(wakes).toHaveLength(4);
  });

  it("adds pass-local reservations on top of existing concurrency when enqueue is not persisted", async () => {
    const seed = await seedCompany();
    await db.update(agents)
      .set({ runtimeConfig: { heartbeat: { wakeOnDemand: true, maxConcurrentRuns: 3 } } })
      .where(eq(agents.id, seed.coderId));
    const holderIssueId = await insertIssue({
      companyId: seed.companyId,
      title: "Existing capacity reservation",
      status: "in_progress",
      number: 1,
      assigneeAgentId: seed.coderId,
      ageHours: 0,
    });
    const issueIds: string[] = [];
    for (let index = 0; index < 3; index += 1) {
      issueIds.push(await insertIssue({
        companyId: seed.companyId,
        title: `Non-persisted enqueue ${index + 1}`,
        status: "todo",
        number: index + 2,
        assigneeAgentId: seed.coderId,
      }));
    }
    await db.insert(heartbeatRuns).values({
      companyId: seed.companyId,
      agentId: seed.coderId,
      status: "running",
      startedAt: new Date(),
      contextSnapshot: { issueId: holderIssueId },
    });
    const wakes: string[] = [];
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (_agentId, opts) => {
        wakes.push(String((opts.payload as Record<string, unknown>).issueId));
        return { id: randomUUID() };
      },
      getSettings: () => testSettings(),
    });

    const result = await reconciler.reconcileExecutionState({ companyId: seed.companyId });

    expect(result.dispatched).toBe(2);
    expect(wakes).toHaveLength(2);
    expect(result.actions).toContainEqual({
      kind: "skipped",
      issueId: issueIds[2],
      reason: "concurrency_limit",
    });
  });

  it("rechecks reviewer capacity, leaves blackout work pending, then wakes it when a slot opens", async () => {
    const seed = await seedCompany();
    const holderIssueId = await insertIssue({
      companyId: seed.companyId,
      title: "Reviewer capacity holder",
      status: "in_progress",
      number: 1,
      assigneeAgentId: seed.coderId,
      ageHours: 0,
    });
    const reviewIssueId = await insertIssue({
      companyId: seed.companyId,
      title: "Verify the customer workflow",
      status: "in_review",
      number: 2,
      assigneeAgentId: seed.coderId,
    });
    await db.update(issues)
      .set({ executionState: { status: "pending", currentParticipant: { type: "agent", agentId: seed.coderId } } })
      .where(eq(issues.id, reviewIssueId));
    const holderRunId = randomUUID();
    await db.insert(heartbeatRuns).values({
      id: holderRunId,
      companyId: seed.companyId,
      agentId: seed.coderId,
      status: "running",
      startedAt: new Date(),
      contextSnapshot: { issueId: holderIssueId },
    });

    let blockReason: "concurrency_limit" | "dispatch_blackout" | null = "concurrency_limit";
    let enqueueCount = 0;
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (agentId, opts) => {
        enqueueCount += 1;
        const issueId = String((opts.payload as Record<string, unknown>).issueId);
        await insertStaleQueuedRun({ companyId: seed.companyId, agentId, issueId });
        return { id: randomUUID() };
      },
      getSettings: () => testSettings(),
      getQueuedRunDispatchBlockReason: async () => blockReason,
    });

    const full = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    expect(full.dispatched).toBe(0);
    expect(full.actions).toContainEqual({
      kind: "skipped",
      issueId: reviewIssueId,
      reason: "concurrency_limit",
    });
    expect(enqueueCount).toBe(0);

    await db.update(heartbeatRuns)
      .set({ status: "succeeded", finishedAt: new Date(), updatedAt: new Date() })
      .where(eq(heartbeatRuns.id, holderRunId));
    blockReason = "dispatch_blackout";
    const blackout = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    expect(blackout.dispatched).toBe(0);
    expect(blackout.actions).toContainEqual({
      kind: "skipped",
      issueId: reviewIssueId,
      reason: "dispatch_blackout",
    });
    expect(enqueueCount).toBe(0);

    blockReason = null;
    const available = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    const repeated = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    expect(available.dispatched).toBe(1);
    expect(repeated.dispatched).toBe(0);
    expect(enqueueCount).toBe(1);
  });

  it("routes unassigned eligible work to the project lead and blocks unroutable work as BLOCKED_NO_OWNER", async () => {
    const seed = await seedCompany();
    const projectId = randomUUID();
    await db.insert(projects).values({
      id: projectId,
      companyId: seed.companyId,
      name: "Tenant Program",
      status: "in_progress",
      leadAgentId: seed.managerId,
    });

    const routableId = await insertIssue({
      companyId: seed.companyId,
      title: "Routable unassigned",
      status: "backlog",
      number: 1,
      projectId,
    });
    const orphanId = await insertIssue({
      companyId: seed.companyId,
      title: "Unroutable unassigned",
      status: "backlog",
      number: 2,
    });

    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async () => ({ id: randomUUID() }),
      getSettings: () => testSettings(),
    });
    const result = await reconciler.reconcileExecutionState({ companyId: seed.companyId });

    expect(result.routed).toBe(1);
    expect(result.blockedNoOwner).toBe(1);

    const [routable] = await db.select().from(issues).where(eq(issues.id, routableId));
    expect(routable.assigneeAgentId).toBe(seed.managerId);
    expect(routable.status).toBe("todo");

    const [orphan] = await db.select().from(issues).where(eq(issues.id, orphanId));
    expect(orphan.status).toBe("blocked");
    expect(orphan.unblockDescriptor).toMatchObject({ owner: "board" });
    expect(orphan.blockedTransitionAt).not.toBeNull();
  });

  it("dispatches only dependency-ready work in an A->{B,C}->D graph", async () => {
    const seed = await seedCompany();
    const aId = await insertIssue({ companyId: seed.companyId, title: "A (done)", status: "done", number: 1, assigneeAgentId: seed.coderId });
    const bId = await insertIssue({ companyId: seed.companyId, title: "B", status: "blocked", number: 2, assigneeAgentId: seed.coderId });
    const cId = await insertIssue({ companyId: seed.companyId, title: "C", status: "blocked", number: 3, assigneeAgentId: seed.coderId });
    const dId = await insertIssue({ companyId: seed.companyId, title: "D", status: "blocked", number: 4, assigneeAgentId: seed.coderId });
    await blocks(seed.companyId, aId, bId);
    await blocks(seed.companyId, aId, cId);
    await blocks(seed.companyId, bId, dId);
    await blocks(seed.companyId, cId, dId);

    const wakes: string[] = [];
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (_agentId, opts) => {
        wakes.push(String((opts.payload as Record<string, unknown>).issueId));
        return { id: randomUUID() };
      },
      getSettings: () => testSettings(),
    });
    const result = await reconciler.reconcileExecutionState({ companyId: seed.companyId });

    expect(result.eligible).toBe(2);
    expect(result.notReady).toBe(1);
    expect(wakes.sort()).toEqual([bId, cId].sort());
    expect(wakes).not.toContain(dId);
  });

  it("is idempotent: a second pass dispatches nothing new once real wakes exist", async () => {
    const seed = await seedCompany();
    await insertIssue({
      companyId: seed.companyId,
      title: "Idempotent dispatch",
      status: "backlog",
      number: 1,
      assigneeAgentId: seed.coderId,
    });

    // Real heartbeat service so enqueueWakeup creates real wake + run rows.
    // The reconciler drives the scan; on the current source the heartbeat
    // service no longer embeds reconcileExecutionState (the native scheduler
    // owns that wiring), so both passes go through the reconciler service
    // with heartbeat.wakeup as the real enqueue path.
    const heartbeat = heartbeatService(db);
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: (agentId: string, opts: Record<string, unknown>) =>
        heartbeat.wakeup(agentId, opts as Parameters<typeof heartbeat.wakeup>[1]) as unknown as Promise<{
          id: string | null;
        } | null>,
      getSettings: () => ({ ...testSettings(), ...resolveExecutionReconcilerSettings() }),
    });
    const first = (await reconciler.reconcileExecutionState({ companyId: seed.companyId })) as {
      dispatched: number;
    };
    expect(first.dispatched).toBe(1);

    const countWakes = () =>
      db
        .select({ id: agentWakeupRequests.id })
        .from(agentWakeupRequests)
        .where(and(eq(agentWakeupRequests.companyId, seed.companyId), sql`${agentWakeupRequests.status} <> 'skipped'`))
        .then((rows) => rows.length);
    const wakesAfterFirst = await countWakes();
    expect(wakesAfterFirst).toBeGreaterThanOrEqual(1);

    // Wait for the dispatched run to settle (mock adapter), then reconcile
    // again: whatever state the issue is in (running, in_progress, or
    // terminal), a second pass must mint zero new wakes.
    await heartbeat.drainActiveRunExecutions();
    const second = (await reconciler.reconcileExecutionState({ companyId: seed.companyId })) as {
      dispatched: number;
    };
    expect(second.dispatched).toBe(0);
    const wakesAfterSecond = await countWakes();
    expect(wakesAfterSecond).toBe(wakesAfterFirst);
  });

  it("dispatches blocked work to its pending agent participant without a dependency edge", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "Agent review continuation",
      status: "blocked",
      number: 1,
      assigneeAgentId: seed.coderId,
    });
    await db
      .update(issues)
      .set({
        executionState: {
          status: "pending",
          currentParticipant: { type: "agent", agentId: seed.coderId },
        },
      })
      .where(eq(issues.id, issueId));

    const wakes: string[] = [];
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (agentId, opts) => {
        wakes.push(`${agentId}:${String((opts.payload as Record<string, unknown>).issueId)}`);
        return { id: randomUUID() };
      },
      getSettings: () => testSettings(),
    });

    const result = await reconciler.reconcileExecutionState({ companyId: seed.companyId });

    expect(result.eligible).toBe(1);
    expect(result.dispatched).toBe(1);
    expect(wakes).toEqual([`${seed.coderId}:${issueId}`]);
  });

  it("does not fall back to a different assignee when the blocked participant is paused", async () => {
    const seed = await seedCompany();
    await db.update(agents).set({ status: "paused" }).where(eq(agents.id, seed.coderId));
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "Blocked continuation owned by a paused participant",
      status: "blocked",
      number: 1,
      assigneeAgentId: seed.managerId,
    });
    await db
      .update(issues)
      .set({
        executionState: {
          status: "pending",
          currentParticipant: { type: "agent", agentId: seed.coderId },
        },
      })
      .where(eq(issues.id, issueId));

    const wakes: string[] = [];
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (agentId, opts) => {
        wakes.push(`${agentId}:${String((opts.payload as Record<string, unknown>).issueId)}`);
        return { id: randomUUID() };
      },
      getSettings: () => testSettings(),
    });

    const result = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    const [issue] = await db.select().from(issues).where(eq(issues.id, issueId));

    expect(result.dispatched).toBe(0);
    expect(result.skipped).toBe(1);
    expect(wakes).toEqual([]);
    expect(issue).toMatchObject({ status: "blocked", assigneeAgentId: seed.managerId });
  });

  it("raises exactly one orchestration incident per stalled program and escalates on persistence", async () => {
    const seed = await seedCompany();
    const projectId = randomUUID();
    await db.insert(projects).values({
      id: projectId,
      companyId: seed.companyId,
      name: "Stalled Program",
      status: "in_progress",
    });
    // Eligible, assigned, quiet for days — the exact Manager-tenant pattern.
    await insertIssue({
      companyId: seed.companyId,
      title: "T1 stalled",
      status: "todo",
      number: 1,
      assigneeAgentId: seed.coderId,
      projectId,
      ageHours: 96,
    });

    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async () => ({ id: randomUUID() }),
      getSettings: () => testSettings({ stallThresholdMs: 60_000 }),
    });

    const summary = await reconciler.buildProgramSummary(seed.companyId, projectId);
    expect(summary.eligibleIssues).toBe(1);
    expect(summary.runningIssues).toBe(0);
    expect(summary.stalled).toBe(true);

    const first = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(first.incidentsCreated).toBe(1);

    const incidents = await db
      .select()
      .from(issues)
      .where(and(eq(issues.companyId, seed.companyId), eq(issues.originKind, "orchestration_incident")));
    expect(incidents).toHaveLength(1);
    expect(incidents[0].originId).toBe(projectId);
    expect(incidents[0].status).toBe("todo");
    expect(incidents[0].assigneeAgentId).toBeNull();

    // Second scan: incident already exists -> escalation comment, no duplicate.
    const second = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(second.incidentsCreated).toBe(0);
    expect(second.incidentsEscalated).toBe(1);
    const incidentComments = await db
      .select()
      .from(issueComments)
      .where(eq(issueComments.issueId, incidents[0].id));
    expect(incidentComments.length).toBe(1);

    // Third scan immediately: still deduped, comment fresh -> no new escalation.
    const third = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(third.incidentsCreated).toBe(0);
    expect(third.incidentsEscalated).toBe(0);
  });

  function stallScanReconciler() {
    return executionReconcilerService(db, {
      enqueueWakeup: async () => ({ id: randomUUID() }),
      getSettings: () => testSettings({ stallThresholdMs: 60_000 }),
    });
  }

  async function seedStalledLane(seed: Seed): Promise<string> {
    const projectId = randomUUID();
    await db.insert(projects).values({
      id: projectId,
      companyId: seed.companyId,
      name: "Stalled Program",
      status: "in_progress",
    });
    await insertIssue({
      companyId: seed.companyId,
      title: "T1 stalled",
      status: "todo",
      number: 1,
      assigneeAgentId: seed.coderId,
      projectId,
      ageHours: 96,
    });
    return projectId;
  }

  function stallIncidentRows(companyId: string) {
    return db
      .select()
      .from(issues)
      .where(and(eq(issues.companyId, companyId), eq(issues.originKind, "orchestration_incident")));
  }

  it("does not recreate a cancelled stall notice for an unchanged stalled program (SON-4797)", async () => {
    const seed = await seedCompany();
    await seedStalledLane(seed);
    const reconciler = stallScanReconciler();

    const first = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(first.incidentsCreated).toBe(1);

    const [incident] = await stallIncidentRows(seed.companyId);
    expect(incident.originFingerprint).toMatch(/^stall:/);
    const signature = incident.originFingerprint.slice("stall:".length);
    expect(incident.description).toContain(`Stuck classification: ${signature}.`);

    // The CEO operating routine cancels the notice as a duplicate-of-record.
    await db
      .update(issues)
      .set({ status: "cancelled", cancelledAt: new Date() })
      .where(eq(issues.id, incident.id));

    // Next scan: unchanged stall -> suppressed, not recreated.
    const second = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(second.incidentsCreated).toBe(0);
    expect(second.incidentsSuppressed).toBe(1);

    const rows = await stallIncidentRows(seed.companyId);
    expect(rows).toHaveLength(1);
  });

  it("dedups a cancelled legacy notice via its description signature (SON-4797)", async () => {
    const seed = await seedCompany();
    await seedStalledLane(seed);
    const reconciler = stallScanReconciler();

    const first = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(first.incidentsCreated).toBe(1);
    const [incident] = await stallIncidentRows(seed.companyId);

    // Legacy cards predate the signature fingerprint.
    await db
      .update(issues)
      .set({ status: "cancelled", cancelledAt: new Date(), originFingerprint: "default" })
      .where(eq(issues.id, incident.id));

    const second = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(second.incidentsCreated).toBe(0);
    expect(second.incidentsSuppressed).toBe(1);
    expect(await stallIncidentRows(seed.companyId)).toHaveLength(1);
  });

  it("creates a fresh notice when the stall signature changes after cancellation (SON-4797)", async () => {
    const seed = await seedCompany();
    await seedStalledLane(seed);
    const reconciler = stallScanReconciler();

    const first = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(first.incidentsCreated).toBe(1);
    const [incident] = await stallIncidentRows(seed.companyId);

    await db
      .update(issues)
      .set({
        status: "cancelled",
        cancelledAt: new Date(),
        originFingerprint: "stall:blocked_dependency=9",
        description: "Stuck classification: blocked_dependency=9. (historical lane state)",
      })
      .where(eq(issues.id, incident.id));

    // The stall signature changed -> the program state actually changed.
    const second = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(second.incidentsCreated).toBe(1);
    expect(second.incidentsSuppressed).toBe(0);
    expect(await stallIncidentRows(seed.companyId)).toHaveLength(2);
  });

  it("recreates the notice once the dedup window passes after cancellation (SON-4797)", async () => {
    const seed = await seedCompany();
    await seedStalledLane(seed);
    const reconciler = stallScanReconciler();

    const first = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(first.incidentsCreated).toBe(1);
    const [incident] = await stallIncidentRows(seed.companyId);

    const stale = new Date(Date.now() - 25 * 60 * 60 * 1000);
    await db
      .update(issues)
      .set({ status: "cancelled", cancelledAt: stale, createdAt: stale })
      .where(eq(issues.id, incident.id));

    // Outside the 24h window the same stall may raise one fresh notice.
    const second = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(second.incidentsCreated).toBe(1);
    expect(second.incidentsSuppressed).toBe(0);
  });
  it("assigns orchestration incidents to the company chief executive when one exists", async () => {
    const seed = await seedCompany();
    const ceoId = randomUUID();
    await db.insert(agents).values({
      id: ceoId,
      companyId: seed.companyId,
      name: "Chief Executive",
      role: "ceo",
      status: "idle",
      adapterType: "codex_local",
      adapterConfig: {},
      runtimeConfig: { heartbeat: { wakeOnDemand: true } },
      permissions: {},
    });
    const projectId = randomUUID();
    await db.insert(projects).values({
      id: projectId,
      companyId: seed.companyId,
      name: "Stalled Program",
      status: "in_progress",
    });
    await insertIssue({
      companyId: seed.companyId,
      title: "T1 stalled",
      status: "todo",
      number: 1,
      assigneeAgentId: seed.coderId,
      projectId,
      ageHours: 96,
    });

    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async () => ({ id: randomUUID() }),
      getSettings: () => testSettings({ stallThresholdMs: 60_000 }),
    });

    const scan = await reconciler.scanProgramStalls({ companyId: seed.companyId });
    expect(scan.incidentsCreated).toBe(1);

    const incidents = await db
      .select()
      .from(issues)
      .where(and(eq(issues.companyId, seed.companyId), eq(issues.originKind, "orchestration_incident")));
    expect(incidents).toHaveLength(1);
    expect(incidents[0].assigneeAgentId).toBe(ceoId);
  });

  it("attaches an independent verifier policy when a verifier agent is configured", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "Needs verification",
      status: "backlog",
      number: 1,
      assigneeAgentId: seed.coderId,
    });

    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async () => ({ id: randomUUID() }),
      getSettings: () => testSettings({ verifierAgentId: seed.managerId }),
    });
    const result = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    expect(result.verificationPoliciesAttached).toBe(1);

    const [issue] = await db.select().from(issues).where(eq(issues.id, issueId));
    const policy = issue.executionPolicy as Record<string, unknown> | null;
    expect(policy).not.toBeNull();
    const stages = policy!.stages as Array<Record<string, unknown>>;
    expect(stages).toHaveLength(1);
    expect(stages[0].type).toBe("review");
    const participants = stages[0].participants as Array<Record<string, string>>;
    expect(participants[0].agentId).toBe(seed.managerId);
    expect(participants[0].agentId).not.toBe(seed.coderId);
    expect(issue.reviewPolicy).toBe("not_creator");
  });

  it("dry run reports decisions without mutating state", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "Dry run target",
      status: "backlog",
      number: 1,
      assigneeAgentId: seed.coderId,
    });

    const wakes: unknown[] = [];
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (_agentId, opts) => {
        wakes.push(opts);
        return { id: randomUUID() };
      },
      getSettings: () => testSettings({ dryRun: true }),
    });
    const result = await reconciler.reconcileExecutionState({ companyId: seed.companyId });

    expect(result.dispatched).toBe(0);
    expect(wakes).toHaveLength(0);
    const [issue] = await db.select().from(issues).where(eq(issues.id, issueId));
    expect(issue.status).toBe("backlog");
  });

  it("does nothing when disabled via settings", async () => {
    const seed = await seedCompany();
    await insertIssue({
      companyId: seed.companyId,
      title: "Disabled",
      status: "backlog",
      number: 1,
      assigneeAgentId: seed.coderId,
    });

    const wakes: unknown[] = [];
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (_agentId, opts) => {
        wakes.push(opts);
        return { id: randomUUID() };
      },
      getSettings: () => testSettings({ enabled: false }),
    });
    const result = await reconciler.reconcileExecutionState({ companyId: seed.companyId });
    expect(result.issuesScanned).toBe(0);
    expect(wakes).toHaveLength(0);
  });
});
