import { randomUUID } from "node:crypto";
import { and, eq } from "drizzle-orm";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  activityLog,
  agents,
  agentRuntimeState,
  agentWakeupRequests,
  companies,
  companySkills,
  documentRevisions,
  documents,
  createDb,
  heartbeatRunEvents,
  heartbeatRuns,
  issueComments,
  issueDocuments,
  issueRelations,
  issueTreeHolds,
  issues,
  workspaceOperations,
} from "@paperclipai/db";
import {
  getEmbeddedPostgresTestSupport,
  startEmbeddedPostgresTestDatabase,
} from "./helpers/embedded-postgres.js";
import { runningProcesses } from "../adapters/index.ts";
import { heartbeatService } from "../services/heartbeat.ts";

const mockAdapterExecute = vi.hoisted(() =>
  vi.fn(async () => ({
    exitCode: 0,
    signal: null,
    timedOut: false,
    errorMessage: null,
    summary: "Acknowledged wake delivery probe.",
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

const embeddedPostgresSupport = await getEmbeddedPostgresTestSupport();
const describeEmbeddedPostgres = embeddedPostgresSupport.supported ? describe : describe.skip;

if (!embeddedPostgresSupport.supported) {
  console.warn(
    `Skipping embedded Postgres heartbeat wake-delivery tests on this host: ${embeddedPostgresSupport.reason ?? "unsupported environment"}`,
  );
}

describeEmbeddedPostgres("heartbeat cross-agent wake delivery (SON-6247)", () => {
  let db!: ReturnType<typeof createDb>;
  let tempDb: Awaited<ReturnType<typeof startEmbeddedPostgresTestDatabase>> | null = null;

  beforeAll(async () => {
    tempDb = await startEmbeddedPostgresTestDatabase("heartbeat-wake-delivery-son6247-");
    db = createDb(tempDb.connectionString);
  }, 60_000);

  afterEach(async () => {
    vi.clearAllMocks();
    runningProcesses.clear();
    await heartbeatService(db).drainActiveRunExecutions();
    await db.delete(activityLog);
    await db.delete(heartbeatRunEvents);
    await db.delete(heartbeatRuns);
    await db.delete(agentWakeupRequests);
    await db.delete(issueComments);
    await db.delete(issueDocuments);
    await db.delete(documentRevisions);
    await db.delete(documents);
    await db.delete(issueRelations);
    await db.delete(issueTreeHolds);
    await db.delete(companySkills);
    await db.delete(issues);
    await db.delete(agentRuntimeState);
    await db.delete(agents);
    await db.delete(companies);
  });

  async function seedAgents() {
    const companyId = randomUUID();
    const assigneeAgentId = randomUUID();
    const addresseeAgentId = randomUUID();
    const issueId = randomUUID();
    const issuePrefix = `T${companyId.replace(/-/g, "").slice(0, 6).toUpperCase()}`;

    await db.insert(companies).values({
      id: companyId,
      name: "Paperclip",
      issuePrefix,
      requireBoardApprovalForNewAgents: false,
    });

    await db.insert(agents).values([
      {
        id: assigneeAgentId,
        companyId,
        name: "Assignee",
        role: "engineer",
        status: "idle",
        adapterType: "process",
        adapterConfig: {},
        runtimeConfig: {},
        permissions: {},
      },
      {
        id: addresseeAgentId,
        companyId,
        name: "Addressee",
        role: "engineer",
        status: "idle",
        adapterType: "process",
        adapterConfig: {},
        runtimeConfig: {},
        permissions: {},
      },
    ]);

    return { companyId, assigneeAgentId, addresseeAgentId, issueId };
  }

  async function seedIssue(input: {
    companyId: string;
    issueId: string;
    assigneeAgentId: string;
    status?: "in_progress" | "blocked";
  }) {
    const issuePrefixRow = await db
      .select({ issuePrefix: companies.issuePrefix })
      .from(companies)
      .where(eq(companies.id, input.companyId))
      .then((rows) => rows[0]);
    const issueNumber = (await db
      .select({ id: issues.id })
      .from(issues)
      .where(eq(issues.companyId, input.companyId))
    ).length + 1;

    await db.insert(issues).values({
      id: input.issueId,
      companyId: input.companyId,
      title: "SON-6247 wake delivery probe",
      status: input.status ?? "in_progress",
      priority: "high",
      assigneeAgentId: input.assigneeAgentId,
      responsibleUserId: "responsible-user",
      issueNumber,
      identifier: `${issuePrefixRow?.issuePrefix ?? "TUNKNOWN"}-${issueNumber}`,
    });
  }

  const interactionProbeId = randomUUID();

  it("delivers an interaction wake to its addressee even when the issue assignee changed", async () => {
    const { companyId, assigneeAgentId, addresseeAgentId, issueId } = await seedAgents();
    await seedIssue({ companyId, issueId, assigneeAgentId });

    const heartbeat = heartbeatService(db);
    const run = await heartbeat.wakeup(addresseeAgentId, {
      source: "automation",
      triggerDetail: "system",
      reason: "issue_commented",
      payload: {
        issueId,
        interactionId: interactionProbeId,
        interactionKind: "request_confirmation",
        interactionStatus: "pending",
        sourceRunId: randomUUID(),
        mutation: "interaction",
      },
      contextSnapshot: {
        issueId,
        taskId: issueId,
        wakeReason: "issue_commented",
        interactionId: interactionProbeId,
        interactionKind: "request_confirmation",
        interactionStatus: "pending",
        source: "issue.interaction.created",
      },
      requestedByActorType: "user",
      requestedByActorId: "responsible-user",
    });
    expect(run).not.toBeNull();
    await heartbeat.drainActiveRunExecutions();

    const runRow = await db
      .select({ errorCode: heartbeatRuns.errorCode, status: heartbeatRuns.status })
      .from(heartbeatRuns)
      .where(eq(heartbeatRuns.id, run!.id))
      .then((rows) => rows[0] ?? null);

    // The addressee is not the issue assignee; the wake must still not be
    // cancelled as stale. Before SON-6247's interaction exemption it was.
    expect(runRow?.errorCode).not.toBe("issue_assignee_changed");

    const wakeRow = await db
      .select({ status: agentWakeupRequests.status, reason: agentWakeupRequests.reason })
      .from(agentWakeupRequests)
      .where(
        and(
          eq(agentWakeupRequests.companyId, companyId),
          eq(agentWakeupRequests.agentId, addresseeAgentId),
        ),
      )
      .then((rows) => rows[0] ?? null);

    expect(wakeRow?.status).not.toBe("skipped");
    expect(wakeRow?.status).not.toBe("cancelled");
  });

  it("wakes the new assignee when a stale queued run is cancelled for an assignee change", async () => {
    const { companyId, assigneeAgentId, addresseeAgentId, issueId } = await seedAgents();
    // Issue lives on the addressee; the stale run belongs to the previous
    // assignee. Reassignment happened after the wake was queued.
    await seedIssue({ companyId, issueId, assigneeAgentId: addresseeAgentId });

    const heartbeat = heartbeatService(db);
    const staleRun = await heartbeat.wakeup(assigneeAgentId, {
      source: "automation",
      triggerDetail: "system",
      reason: "issue_monitor_due",
      payload: { issueId },
      contextSnapshot: {
        issueId,
        taskId: issueId,
        wakeReason: "issue_monitor_due",
        source: "issue.monitor",
      },
      requestedByActorType: "system",
      requestedByActorId: "heartbeat",
    });
    expect(staleRun).not.toBeNull();
    await heartbeat.drainActiveRunExecutions();

    const cancelledRun = await db
      .select({ errorCode: heartbeatRuns.errorCode, status: heartbeatRuns.status })
      .from(heartbeatRuns)
      .where(eq(heartbeatRuns.id, staleRun!.id))
      .then((rows) => rows[0] ?? null);
    expect(cancelledRun?.status).toBe("cancelled");
    expect(cancelledRun?.errorCode).toBe("issue_assignee_changed");

    const handoffWakes = await db
      .select({
        agentId: agentWakeupRequests.agentId,
        reason: agentWakeupRequests.reason,
        payload: agentWakeupRequests.payload,
        status: agentWakeupRequests.status,
      })
      .from(agentWakeupRequests)
      .where(
        and(
          eq(agentWakeupRequests.companyId, companyId),
          eq(agentWakeupRequests.agentId, addresseeAgentId),
        ),
      );
    const handoff = handoffWakes.find(
      (row) =>
        (row.payload as Record<string, unknown> | null)?.mutation === "assignee_handoff",
    );
    expect(handoff).toBeDefined();
    expect(handoff?.reason).toBe("issue_assigned");
    const handoffMarker = (handoff?.payload as Record<string, unknown>).assigneeHandoff as
      | Record<string, unknown>
      | undefined;
    expect(handoffMarker?.fromAgentId).toBe(assigneeAgentId);
    expect(handoffMarker?.depth).toBe(1);
  });

  it("coalesces repeated identical queue-time skips and alerts once per threshold", async () => {
    const { companyId, assigneeAgentId, addresseeAgentId, issueId } = await seedAgents();
    await seedIssue({ companyId, issueId, assigneeAgentId, status: "blocked" });

    // A second issue blocks the target issue; its assignee (the addressee)
    // owns the blocking work.
    const blockerIssueId = randomUUID();
    await seedIssue({ companyId, issueId: blockerIssueId, assigneeAgentId: addresseeAgentId });

    // The addressee's issue blocks the assignee's issue.
    await db.insert(issueRelations).values({
      companyId,
      issueId: blockerIssueId,
      relatedIssueId: issueId,
      type: "blocks",
    });

    const heartbeat = heartbeatService(db);
    for (let attempt = 0; attempt < 12; attempt += 1) {
      const outcome = await heartbeat.wakeup(assigneeAgentId, {
        source: "automation",
        triggerDetail: "system",
        reason: "issue_monitor_due",
        payload: { issueId },
        contextSnapshot: {
          issueId,
          taskId: issueId,
          wakeReason: "issue_monitor_due",
          source: "issue.monitor",
        },
        requestedByActorType: "system",
        requestedByActorId: "heartbeat",
      });
      expect(outcome).toBeNull();
    }

    const skipRows = await db
      .select({
        id: agentWakeupRequests.id,
        coalescedCount: agentWakeupRequests.coalescedCount,
        status: agentWakeupRequests.status,
        reason: agentWakeupRequests.reason,
      })
      .from(agentWakeupRequests)
      .where(
        and(
          eq(agentWakeupRequests.companyId, companyId),
          eq(agentWakeupRequests.agentId, assigneeAgentId),
          eq(agentWakeupRequests.status, "skipped"),
          eq(agentWakeupRequests.reason, "issue_dependencies_blocked"),
        ),
      );
    expect(skipRows).toHaveLength(1);
    expect(skipRows[0]?.coalescedCount).toBe(12);

    const alerts = await db
      .select({ details: activityLog.details })
      .from(activityLog)
      .where(
        and(
          eq(activityLog.companyId, companyId),
          eq(activityLog.action, "wakeup.skip_storm"),
        ),
      );
    expect(alerts).toHaveLength(1);
    expect((alerts[0]?.details as Record<string, unknown>).threshold).toBe(10);
    // The alert records the count at the moment the threshold was crossed.
    expect((alerts[0]?.details as Record<string, unknown>).coalescedCount).toBe(10);
  });
});
