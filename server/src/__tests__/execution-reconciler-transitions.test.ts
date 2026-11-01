import { randomUUID } from "node:crypto";
import { and, eq } from "drizzle-orm";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  activityLog,
  agentWakeupRequests,
  agents,
  companies,
  createDb,
  heartbeatRuns,
  issueComments,
  issues,
} from "@paperclipai/db";
import {
  getEmbeddedPostgresTestSupport,
  startEmbeddedPostgresTestDatabase,
} from "./helpers/embedded-postgres.js";

import {
  executionReconcilerService,
  parsePendingReviewParticipant,
  reassignReviewParticipant,
  resolveExecutionReconcilerSettings,
  TRANSITION_BLOCKED_DISPOSITION_LEDGER_ACTION,
  TRANSITION_ESCALATION_LEDGER_ACTION,
  TRANSITION_LANE_LIVENESS_LEDGER_ACTION,
  TRANSITION_REAWAKE_LEDGER_ACTION,
  TRANSITION_REVIEWER_REASSIGN_LEDGER_ACTION,
  TRANSITION_REVIEW_NUDGE_LEDGER_ACTION,
  TRANSITION_SHIP_DISPATCH_LEDGER_ACTION,
  TRANSITION_WAKE_DEAD_LEDGER_ACTION,
  type ExecutionReconcilerSettings,
} from "../services/execution-reconciler.js";

const embeddedPostgresSupport = await getEmbeddedPostgresTestSupport();
const describeEmbeddedPostgres = embeddedPostgresSupport.supported ? describe : describe.skip;

function transitionSettings(overrides: Partial<ExecutionReconcilerSettings> = {}): ExecutionReconcilerSettings {
  return {
    ...resolveExecutionReconcilerSettings(),
    enabled: true,
    dryRun: false,
    levelTriggeredEnabled: true,
    inProgressStallMs: 60 * 60 * 1000,
    maxReawakeAttempts: 2,
    inReviewVerdictMs: 60 * 60 * 1000,
    shipDispatchDelayMs: 60 * 60 * 1000,
    blockedDispositionMs: 60 * 60 * 1000,
    wakeReceiptGraceMs: 60 * 60 * 1000,
    fallbackReviewerAgentId: null,
    releaseLaneAgentId: null,
    maxTransitionActionsPerTick: 20,
    disabledCompanies: [],
    ...overrides,
  };
}

describe("pending review stage parsing", () => {
  const policy = {
    mode: "normal",
    stages: [
      {
        type: "review",
        status: "approved",
        participants: [{ type: "agent", id: "reviewer-1", agentId: "reviewer-1" }],
      },
      {
        type: "release",
        status: "pending",
        participants: [{ type: "agent", id: "release-lane", agentId: "release-lane" }],
      },
    ],
  };

  it("finds the last incomplete stage as the current gate", () => {
    const gate = parsePendingReviewParticipant(policy);
    expect(gate?.stageIndex).toBe(1);
    expect(gate?.reviewerAgentId).toBe("release-lane");
    expect(gate?.stageStatus).toBe("pending");
  });

  it("returns null for policies without stages", () => {
    expect(parsePendingReviewParticipant(null)).toBeNull();
    expect(parsePendingReviewParticipant({ mode: "normal" })).toBeNull();
    expect(parsePendingReviewParticipant({ stages: [] })).toBeNull();
  });

  it("reassigns only the pending stage's participant", () => {
    const next = reassignReviewParticipant(policy, "release-lane", "fallback-reviewer");
    expect(next).not.toBeNull();
    const stages = (next as { stages: Array<Record<string, unknown>> }).stages;
    expect(stages[0].participants).toEqual([{ type: "agent", id: "reviewer-1", agentId: "reviewer-1" }]);
    expect(stages[1].participants).toEqual([{ type: "agent", id: "fallback-reviewer", agentId: "fallback-reviewer" }]);
  });

  it("refuses to reassign unknown reviewers", () => {
    expect(reassignReviewParticipant(policy, "nobody", "fallback-reviewer")).toBeNull();
  });
});

describeEmbeddedPostgres("execution reconciler transition deadlines", () => {
  let tempDb: Awaited<ReturnType<typeof startEmbeddedPostgresTestDatabase>> | null = null;
  let db: ReturnType<typeof createDb>;

  beforeAll(async () => {
    tempDb = await startEmbeddedPostgresTestDatabase("paperclip-execution-transitions-");
    db = createDb(tempDb.connectionString);
  }, 30_000);

  afterEach(async () => {
    vi.clearAllMocks();
    await db.delete(activityLog);
    await db.delete(heartbeatRuns);
    await db.delete(issueComments);
    await db.delete(issues);
    await db.delete(agentWakeupRequests);
    await db.delete(agents);
    await db.delete(companies);
  });

  afterAll(async () => {
    await tempDb?.cleanup();
  }, 30_000);

  type Seed = {
    companyId: string;
    ceoId: string;
    ownerId: string;
    reviewerId: string;
    fallbackId: string;
  };

  async function seedCompany(): Promise<Seed> {
    const companyId = randomUUID();
    const ceoId = randomUUID();
    const ownerId = randomUUID();
    const reviewerId = randomUUID();
    const fallbackId = randomUUID();
    const issuePrefix = `XT${companyId.replace(/-/g, "").slice(0, 6).toUpperCase()}`;

    await db.insert(companies).values({
      id: companyId,
      name: "Transition Deadlines Co",
      issuePrefix,
      requireBoardApprovalForNewAgents: false,
      defaultResponsibleUserId: "transition-test-responsible-user",
    });

    await db.insert(agents).values([
      {
        id: ceoId,
        companyId,
        name: "Chief",
        role: "ceo",
        status: "idle",
        adapterType: "codex_local",
        adapterConfig: {},
        runtimeConfig: {},
        permissions: {},
      },
      {
        id: ownerId,
        companyId,
        name: "Executor",
        role: "engineer",
        status: "idle",
        adapterType: "codex_local",
        adapterConfig: {},
        runtimeConfig: { heartbeat: { wakeOnDemand: true } },
        permissions: {},
      },
      {
        id: reviewerId,
        companyId,
        name: "Reviewer",
        role: "qa",
        status: "idle",
        adapterType: "codex_local",
        adapterConfig: {},
        runtimeConfig: { heartbeat: { wakeOnDemand: true } },
        permissions: {},
      },
      {
        id: fallbackId,
        companyId,
        name: "Fallback Reviewer",
        role: "qa",
        status: "idle",
        adapterType: "codex_local",
        adapterConfig: {},
        runtimeConfig: { heartbeat: { wakeOnDemand: true } },
        permissions: {},
      },
    ]);

    return { companyId, ceoId, ownerId, reviewerId, fallbackId };
  }

  async function insertIssue(input: {
    companyId: string;
    title: string;
    status: string;
    number: number;
    assigneeAgentId?: string | null;
    ageHours?: number;
    executionPolicy?: unknown;
    blockedTransitionAt?: Date | null;
    blockedOwnerNotifiedAt?: Date | null;
  }) {
    const id = randomUUID();
    const when = new Date(Date.now() - (input.ageHours ?? 1) * 60 * 60 * 1000);
    const [company] = await db.select().from(companies).where(eq(companies.id, input.companyId));
    await db.insert(issues).values({
      id,
      companyId: input.companyId,
      title: input.title,
      description: "A real deliverable card.",
      status: input.status,
      priority: "medium",
      assigneeAgentId: input.assigneeAgentId ?? null,
      issueNumber: input.number,
      identifier: `${company.issuePrefix}-${input.number}`,
      executionPolicy: (input.executionPolicy ?? null) as Record<string, unknown> | null,
      blockedTransitionAt: input.blockedTransitionAt ?? null,
      blockedOwnerNotifiedAt: input.blockedOwnerNotifiedAt ?? null,
      originKind: "manual",
      createdAt: when,
      updatedAt: when,
    });
    return id;
  }

  async function addComment(input: {
    companyId: string;
    issueId: string;
    authorAgentId?: string | null;
    body?: string;
    ageHours?: number;
  }) {
    await db.insert(issueComments).values({
      companyId: input.companyId,
      issueId: input.issueId,
      authorType: input.authorAgentId ? "agent" : "system",
      authorAgentId: input.authorAgentId ?? null,
      body: input.body ?? "working on it",
      createdAt: new Date(Date.now() - (input.ageHours ?? 0) * 60 * 60 * 1000),
    });
  }

  async function ledgerCount(companyId: string, action: string, issueId: string) {
    const rows = await db
      .select({ action: activityLog.action })
      .from(activityLog)
      .where(and(eq(activityLog.companyId, companyId), eq(activityLog.action, action), eq(activityLog.entityId, issueId)));
    return rows.length;
  }

  async function commentsFor(issueId: string) {
    return db.select({ body: issueComments.body }).from(issueComments).where(eq(issueComments.issueId, issueId));
  }

  type Deps = Parameters<typeof executionReconcilerService>[1];

  function makeReconciler(seed: Seed, settings: ExecutionReconcilerSettings) {
    const wakes: Array<{ agentId: string; opts: Record<string, unknown> }> = [];
    const directWakes: Array<{ agentId: string; opts: Record<string, unknown> }> = [];
    const reconciler = executionReconcilerService(db, {
      enqueueWakeup: async (agentId: string, opts: Record<string, unknown>) => {
        wakes.push({ agentId, opts });
        return { id: randomUUID() };
      },
      wakeAgentDirectly: async (agentId: string, opts: Record<string, unknown>) => {
        directWakes.push({ agentId, opts });
        return { id: randomUUID() };
      },
      getSettings: () => settings,
    } as Deps);
    return { reconciler, wakes, directWakes };
  }

  it("takes no action when the level-triggered scan is disabled by default", async () => {
    const seed = await seedCompany();
    await insertIssue({
      companyId: seed.companyId,
      title: "stalled but scan disabled",
      status: "in_progress",
      number: 1,
      assigneeAgentId: seed.ownerId,
      ageHours: 6,
    });
    const { reconciler, wakes } = makeReconciler(
      seed,
      transitionSettings({ levelTriggeredEnabled: false }),
    );
    const result = await reconciler.reconcileTransitionDeadlines({});
    expect(result.reawakeOwner).toBe(0);
    expect(result.inProgressStalled).toBe(0);
    expect(wakes).toHaveLength(0);
  });

  it("re-wakes a silently-stalled in_progress card within one tick (self-heal)", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "stalled in_progress card",
      status: "in_progress",
      number: 2,
      assigneeAgentId: seed.ownerId,
      ageHours: 6,
    });
    const { reconciler, wakes } = makeReconciler(seed, transitionSettings());
    const result = await reconciler.reconcileTransitionDeadlines({});
    expect(result.inProgressStalled).toBe(1);
    expect(result.reawakeOwner).toBe(1);
    expect(wakes).toHaveLength(1);
    expect(wakes[0].agentId).toBe(seed.ownerId);
    expect(wakes[0].opts.reason).toBe("execution_reconciler_transition_reawake");
    expect((wakes[0].opts.payload as Record<string, unknown>).issueId).toBe(issueId);
    expect(await ledgerCount(seed.companyId, TRANSITION_REAWAKE_LEDGER_ACTION, issueId)).toBe(1);
    const comments = await commentsFor(issueId);
    expect(comments.some((c) => c.body.includes("TRANSITION_REWAKE"))).toBe(true);
  });

  it("does not re-wake cards with recent executor activity", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "recently active card",
      status: "in_progress",
      number: 3,
      assigneeAgentId: seed.ownerId,
      ageHours: 6,
    });
    await addComment({ companyId: seed.companyId, issueId, authorAgentId: seed.ownerId, ageHours: 0 });
    const { reconciler, wakes } = makeReconciler(seed, transitionSettings());
    const result = await reconciler.reconcileTransitionDeadlines({});
    expect(result.reawakeOwner).toBe(0);
    expect(wakes).toHaveLength(0);
  });

  it("bounds re-wakes and escalates once to the CEO loop", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "never progresses",
      status: "in_progress",
      number: 4,
      assigneeAgentId: seed.ownerId,
      ageHours: 48,
    });
    const { reconciler, wakes } = makeReconciler(seed, transitionSettings());
    await reconciler.reconcileTransitionDeadlines({});
    await reconciler.reconcileTransitionDeadlines({});
    expect(wakes).toHaveLength(2);
    const second = await reconciler.reconcileTransitionDeadlines({});
    expect(second.reawakeOwner).toBe(0);
    expect(second.escalatedToCeo).toBe(1);
    expect(wakes).toHaveLength(2);
    expect(await ledgerCount(seed.companyId, TRANSITION_ESCALATION_LEDGER_ACTION, issueId)).toBe(1);
    const [incident] = await db
      .select()
      .from(issues)
      .where(and(eq(issues.companyId, seed.companyId), eq(issues.originKind, "orchestration_incident")));
    expect(incident).toBeDefined();
    expect(incident.assigneeAgentId).toBe(seed.ceoId);
    expect(incident.originId).toBe(`transition:${issueId}:in_progress_stall`);
    const cardComments = await commentsFor(issueId);
    expect(cardComments.some((c) => c.body.includes("CEO_ESCALATION"))).toBe(true);
    const fourth = await reconciler.reconcileTransitionDeadlines({});
    expect(fourth.escalatedToCeo).toBe(0);
  });

  it("re-admits an error lane before touching its cards", async () => {
    const seed = await seedCompany();
    await db.update(agents).set({ status: "error", errorReason: "quota exhausted" }).where(eq(agents.id, seed.ownerId));
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "owned by broken lane",
      status: "in_progress",
      number: 5,
      assigneeAgentId: seed.ownerId,
      ageHours: 6,
    });
    const { reconciler, wakes } = makeReconciler(seed, transitionSettings());
    const result = await reconciler.reconcileTransitionDeadlines({});
    expect(result.lanesReAdmitted).toBe(1);
    expect(result.skippedLaneUnhealthy).toBeGreaterThanOrEqual(1);
    expect(result.reawakeOwner).toBe(0);
    expect(wakes).toHaveLength(1);
    expect(wakes[0].agentId).toBe(seed.ownerId);
    expect(wakes[0].opts.reason).toBe("execution_reconciler_lane_liveness");
    expect(await ledgerCount(seed.companyId, TRANSITION_LANE_LIVENESS_LEDGER_ACTION, issueId)).toBe(1);
  });

  it("nudges an idle reviewer, then reassigns to the fallback reviewer", async () => {
    const seed = await seedCompany();
    const policy = {
      mode: "normal",
      stages: [
        {
          type: "review",
          status: "pending",
          participants: [{ type: "agent", id: seed.reviewerId, agentId: seed.reviewerId }],
        },
      ],
    };
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "review stalled",
      status: "in_review",
      number: 6,
      ageHours: 8,
      executionPolicy: policy,
    });
    const { reconciler, wakes } = makeReconciler(
      seed,
      transitionSettings({ fallbackReviewerAgentId: seed.fallbackId }),
    );
    const first = await reconciler.reconcileTransitionDeadlines({});
    expect(first.reviewerNudged).toBe(1);
    expect(wakes).toHaveLength(1);
    expect(wakes[0].agentId).toBe(seed.reviewerId);
    expect(await ledgerCount(seed.companyId, TRANSITION_REVIEW_NUDGE_LEDGER_ACTION, issueId)).toBe(1);

    const second = await reconciler.reconcileTransitionDeadlines({});
    expect(second.reviewerReassigned).toBe(1);
    expect(await ledgerCount(seed.companyId, TRANSITION_REVIEWER_REASSIGN_LEDGER_ACTION, issueId)).toBe(1);
    const [updated] = await db.select().from(issues).where(eq(issues.id, issueId));
    const gate = parsePendingReviewParticipant(updated.executionPolicy);
    expect(gate?.reviewerAgentId).toBe(seed.fallbackId);
    const comments = await commentsFor(issueId);
    expect(comments.some((c) => c.body.includes("REVIEWER_REASSIGNED"))).toBe(true);
  });

  it("escalates a review stall when no fallback reviewer is configured", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "review stalled no fallback",
      status: "in_review",
      number: 7,
      ageHours: 8,
      executionPolicy: {
        mode: "normal",
        stages: [
          {
            type: "review",
            status: "pending",
            participants: [{ type: "agent", id: seed.reviewerId, agentId: seed.reviewerId }],
          },
        ],
      },
    });
    const { reconciler } = makeReconciler(seed, transitionSettings());
    await reconciler.reconcileTransitionDeadlines({});
    const second = await reconciler.reconcileTransitionDeadlines({});
    expect(second.escalatedToCeo).toBe(1);
    expect(await ledgerCount(seed.companyId, TRANSITION_ESCALATION_LEDGER_ACTION, issueId)).toBe(1);
  });

  it("dispatches a pending release-lane stage once past the ship window", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "approved but not shipped",
      status: "in_review",
      number: 8,
      ageHours: 8,
      executionPolicy: {
        mode: "normal",
        stages: [
          {
            type: "review",
            status: "approved",
            participants: [{ type: "agent", id: seed.reviewerId, agentId: seed.reviewerId }],
          },
          {
            type: "release",
            status: "pending",
            participants: [{ type: "agent", id: "release-lane", agentId: "release-lane" }],
          },
        ],
      },
    });
    const { reconciler, wakes } = makeReconciler(
      seed,
      transitionSettings({ releaseLaneAgentId: "release-lane" }),
    );
    const first = await reconciler.reconcileTransitionDeadlines({});
    expect(first.shipDispatchQueued).toBe(1);
    expect(wakes).toHaveLength(1);
    expect(wakes[0].agentId).toBe("release-lane");
    expect(wakes[0].opts.reason).toBe("execution_reconciler_ship_dispatch");
    expect(await ledgerCount(seed.companyId, TRANSITION_SHIP_DISPATCH_LEDGER_ACTION, issueId)).toBe(1);
    const second = await reconciler.reconcileTransitionDeadlines({});
    expect(second.shipDispatchQueued).toBe(0);
    expect(wakes).toHaveLength(1);
  });

  it("routes a release-lane gate to ship dispatch instead of a review nudge", async () => {
    const seed = await seedCompany();
    await insertIssue({
      companyId: seed.companyId,
      title: "review and ship stall on one card",
      status: "in_review",
      number: 9,
      ageHours: 8,
      executionPolicy: {
        mode: "normal",
        stages: [
          {
            type: "release",
            status: "pending",
            participants: [{ type: "agent", id: "release-lane", agentId: "release-lane" }],
          },
        ],
      },
    });
    const { reconciler, wakes } = makeReconciler(
      seed,
      transitionSettings({ releaseLaneAgentId: "release-lane" }),
    );
    const result = await reconciler.reconcileTransitionDeadlines({});
    // Rule 2 defers to rule 3 when the pending gate's participant is the
    // configured release lane: one corrective action, correctly labeled.
    expect(result.reviewerNudged).toBe(0);
    expect(result.shipDispatchQueued).toBe(1);
    expect(wakes).toHaveLength(1);
    expect(wakes[0].agentId).toBe("release-lane");
    expect(wakes[0].opts.reason).toBe("execution_reconciler_ship_dispatch");
  });

  it("notifies the owner once for blocked cards past the disposition deadline", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "blocked forever",
      status: "blocked",
      number: 10,
      assigneeAgentId: seed.ownerId,
      ageHours: 30,
      blockedTransitionAt: new Date(Date.now() - 30 * 60 * 60 * 1000),
      blockedOwnerNotifiedAt: null,
    });
    const { reconciler, wakes } = makeReconciler(seed, transitionSettings());
    const first = await reconciler.reconcileTransitionDeadlines({});
    expect(first.blockedDisposition).toBe(1);
    expect(wakes).toHaveLength(1);
    expect(wakes[0].opts.reason).toBe("execution_reconciler_blocked_disposition");
    expect(await ledgerCount(seed.companyId, TRANSITION_BLOCKED_DISPOSITION_LEDGER_ACTION, issueId)).toBe(1);
    const [updated] = await db.select().from(issues).where(eq(issues.id, issueId));
    expect(updated.blockedOwnerNotifiedAt).not.toBeNull();
    const second = await reconciler.reconcileTransitionDeadlines({});
    expect(second.blockedDisposition).toBe(0);
    expect(wakes).toHaveLength(1);
  });

  it("marks dead wakes and retries them via the alternate path", async () => {
    const seed = await seedCompany();
    const issueId = await insertIssue({
      companyId: seed.companyId,
      title: "wake receipt test",
      status: "in_progress",
      number: 11,
      assigneeAgentId: seed.ownerId,
      ageHours: 6,
    });
    const wakeId = randomUUID();
    const oldTs = new Date(Date.now() - 2 * 60 * 60 * 1000);
    await db.insert(agentWakeupRequests).values({
      id: wakeId,
      companyId: seed.companyId,
      agentId: seed.ownerId,
      source: "automation",
      triggerDetail: "system",
      reason: "execution_reconciler_transition_reawake",
      payload: { issueId },
      status: "queued",
      requestedByActorType: "system",
      requestedByActorId: "execution-reconciler",
      requestedAt: oldTs,
      createdAt: oldTs,
      updatedAt: oldTs,
    });
    const { reconciler, directWakes } = makeReconciler(seed, transitionSettings());
    const result = await reconciler.reconcileTransitionDeadlines({});
    expect(result.wakeReceiptsDead).toBe(1);
    expect(result.wakeReceiptsRetried).toBe(1);
    expect(directWakes).toHaveLength(1);
    expect(directWakes[0].agentId).toBe(seed.ownerId);
    expect(directWakes[0].opts.reason).toBe("execution_reconciler_wake_receipt_retry");
    const [wake] = await db.select().from(agentWakeupRequests).where(eq(agentWakeupRequests.id, wakeId));
    expect(wake.status).toBe("failed");
    expect((wake.payload as Record<string, unknown>).receiptRetryAt).toBeTruthy();
    expect(await ledgerCount(seed.companyId, TRANSITION_WAKE_DEAD_LEDGER_ACTION, issueId)).toBe(1);
  });

  it("escalates once when the retried wake also produced no receipt", async () => {
    const seed = await seedCompany();
    const wakeId = randomUUID();
    const wakeIssueId = randomUUID();
    const oldTs = new Date(Date.now() - 4 * 60 * 60 * 1000);
    await db.insert(agentWakeupRequests).values({
      id: wakeId,
      companyId: seed.companyId,
      agentId: seed.ownerId,
      source: "automation",
      triggerDetail: "system",
      reason: "execution_reconciler_transition_reawake",
      payload: { issueId: wakeIssueId, receiptRetryAt: oldTs.toISOString() },
      status: "failed",
      requestedByActorType: "system",
      requestedByActorId: "execution-reconciler",
      requestedAt: oldTs,
      createdAt: oldTs,
      updatedAt: oldTs,
    });
    const { reconciler, directWakes } = makeReconciler(seed, transitionSettings());
    const result = await reconciler.reconcileTransitionDeadlines({});
    expect(result.wakeReceiptsDead).toBe(0);
    expect(result.escalatedToCeo).toBe(1);
    expect(directWakes).toHaveLength(0);
    const [incident] = await db
      .select()
      .from(issues)
      .where(and(eq(issues.companyId, seed.companyId), eq(issues.originKind, "orchestration_incident")));
    expect(incident).toBeDefined();
    expect(incident.originId).toBe(`transition:${wakeIssueId}:wake_receipt_loss`);
    expect(incident.assigneeAgentId).toBe(seed.ceoId);
    const second = await reconciler.reconcileTransitionDeadlines({});
    expect(second.escalatedToCeo).toBe(0);
  });

  it("respects the per-tick corrective action budget", async () => {
    const seed = await seedCompany();
    for (let n = 20; n < 26; n += 1) {
      await insertIssue({
        companyId: seed.companyId,
        title: `budget card ${n}`,
        status: "in_progress",
        number: n,
        assigneeAgentId: seed.ownerId,
        ageHours: 6,
      });
    }
    const { reconciler, wakes } = makeReconciler(
      seed,
      transitionSettings({ maxTransitionActionsPerTick: 3 }),
    );
    const result = await reconciler.reconcileTransitionDeadlines({});
    expect(result.reawakeOwner).toBe(3);
    expect(wakes).toHaveLength(3);
  });
});
