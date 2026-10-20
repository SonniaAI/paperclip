import { randomUUID } from "node:crypto";
import express from "express";
import request from "supertest";
import { eq } from "drizzle-orm";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  agents,
  companies,
  createDb,
  heartbeatRuns,
  issues,
  routineRuns,
  routines,
} from "@paperclipai/db";
import {
  getEmbeddedPostgresTestSupport,
  startEmbeddedPostgresTestDatabase,
} from "./helpers/embedded-postgres.js";
import { errorHandler } from "../middleware/index.js";
import { issueRoutes } from "../routes/issues.js";

const embeddedPostgresSupport = await getEmbeddedPostgresTestSupport();
const describeEmbeddedPostgres = embeddedPostgresSupport.supported ? describe : describe.skip;

// SON-5458 regression: closing one open routine-execution carrier from the
// assignee agent's live run 500s when a SIBLING open carrier of the same
// routine era already holds issues_open_routine_execution_uq.
//
// Live mechanism (proven from prod logs + activity log, 2026-10-20):
//  - carriers of one routine-definition era share the index tuple
//    (company, 'routine_execution', originId, originFingerprint);
//  - the PATCH close on a carrier whose checkout is unowned (its dispatch run
//    ended and released the lock) routes through assertAgentIssueMutationAllowed
//    -> assertCheckoutOwner -> adoptUnownedCheckoutRun, whose claim-stamp
//    UPDATE sets execution_run_id and re-inserts the target into the index;
//  - the sibling holds the tuple -> 23505 -> unclassified -> HTTP 500, and the
//    close rolls back (card stays in_progress).
// Prod failures: SON-5449 14:18:21-14:18:40Z (run 30111727; tuple held by
// SON-5453), SON-5453 15:12:41-15:13:10Z (run 140a9b3a; tuple held by
// SON-5457). Prod successes explained by the same mechanism: non-assignee
// closer with checkout-management override skips assertCheckoutOwner entirely
// (f3762124 -> SON-5453), and a same-run lock resolves before adoption
// (140a9b3a -> SON-5457).

describeEmbeddedPostgres("SON-5458 routine carrier status transition", () => {
  let db!: ReturnType<typeof createDb>;
  let tempDb: Awaited<ReturnType<typeof startEmbeddedPostgresTestDatabase>> | null = null;
  let companyId!: string;
  let agentA!: string; // watch routine assignee (routine-dispatch caller class)
  let agentB!: string; // cross-company-style closer with override handled upstream
  let routineId!: string;
  let fingerprint!: string;
  let carrierUnowned!: string; // open carrier, checkout/execution released (NULL)
  let carrierSibling!: string; // open carrier holding the era tuple
  let liveRunId!: string;

  beforeAll(async () => {
    tempDb = await startEmbeddedPostgresTestDatabase("paperclip-son5458-");
    db = createDb(tempDb.connectionString);

    companyId = randomUUID();
    agentA = randomUUID();
    agentB = randomUUID();
    routineId = randomUUID();
    fingerprint = "fp-era-" + randomUUID();

    await db.insert(companies).values({
      id: companyId,
      name: "Paperclip",
      issuePrefix: `T${companyId.replace(/-/g, "").slice(0, 6).toUpperCase()}`,
      requireBoardApprovalForNewAgents: false,
    });
    for (const [id, name] of [[agentA, "WatchAgent"], [agentB, "OtherAgent"]] as const) {
      await db.insert(agents).values({
        id,
        companyId,
        name,
        role: "engineer",
        status: "active",
        adapterType: "codex_local",
        adapterConfig: {},
        runtimeConfig: {},
        permissions: {},
      });
    }
    await db.insert(routines).values({
      id: routineId,
      companyId,
      title: "Hourly company-wide watch",
      description: "watch",
      status: "active",
      priority: "high",
      assigneeAgentId: agentA,
      responsibleUserId: "owner-user",
      createdByAgentId: null,
      createdByUserId: "owner-user",
      concurrencyPolicy: "coalesce",
    } as never);

    // The caller's live run, bound to the sibling carrier (dispatch shape:
    // each fire's run carries its own carrier's id in the context snapshot).
    liveRunId = randomUUID();
    await db.insert(heartbeatRuns).values({
      id: liveRunId,
      companyId,
      agentId: agentA,
      invocationSource: "assignment",
      triggerDetail: "system",
      status: "running",
      startedAt: new Date(),
    });

    const makeCarrier = async (opts: { executionRunId: string | null }) => {
      const runId = randomUUID();
      const issueId = randomUUID();
      await db.insert(issues).values({
        id: issueId,
        companyId,
        title: "Hourly watch carrier",
        description: "carrier",
        status: "in_progress",
        priority: "high",
        assigneeAgentId: agentA,
        responsibleUserId: "owner-user",
        originKind: "routine_execution",
        originId: routineId,
        originRunId: runId,
        originFingerprint: fingerprint,
        checkoutRunId: null,
        executionRunId: opts.executionRunId,
        executionLockedAt: opts.executionRunId ? new Date() : null,
      } as never);
      await db.insert(routineRuns).values({
        id: runId,
        companyId,
        routineId,
        source: "schedule",
        status: "completed",
        triggeredAt: new Date(Date.now() - 3_600_000),
        dispatchFingerprint: fingerprint,
        linkedIssueId: issueId,
        responsibleUserId: "owner-user",
        completedAt: new Date(Date.now() - 1_800_000),
      });
      return issueId;
    };

    // Sibling carrier from the same era holds the index tuple: its dispatch
    // run claimed it (execution_run_id = the caller's live run id, exactly the
    // 15:0xZ state where SON-5457 held execution_run_id = 140a9b3a).
    carrierSibling = await makeCarrier({ executionRunId: liveRunId });
    // Target carrier: its own dispatch run already ended and released the
    // checkout/execution lock, so both are NULL (SON-5453 at 15:12Z).
    carrierUnowned = await makeCarrier({ executionRunId: null });

    await db.update(heartbeatRuns).set({
      contextSnapshot: { issueId: carrierSibling },
    }).where(eq(heartbeatRuns.id, liveRunId));
  }, 30_000);

  afterAll(async () => {
    await tempDb?.cleanup();
  });

  function appFor(actor: Record<string, unknown>) {
    const app = express();
    app.use(express.json());
    app.use((req, _res, next) => {
      (req as any).actor = actor;
      next();
    });
    app.use("/api", issueRoutes(db, {} as never));
    app.use(errorHandler);
    return app;
  }

  it("assignee's live run closes the sibling-era carrier without a 500", async () => {
    const app = appFor({ type: "agent", agentId: agentA, companyId, runId: liveRunId });
    const res = await request(app)
      .patch(`/api/issues/${carrierUnowned}`)
      .set("x-paperclip-run-id", liveRunId)
      .send({ status: "done" });
    console.error("[son5458-repro] stale-era close ->", res.status, JSON.stringify(res.body).slice(0, 200));
    expect(res.status).toBe(200);
  });

  it("close actually lands (readback done, no rollback)", async () => {
    const row = await db.select({ status: issues.status })
      .from(issues).where(eq(issues.id, carrierUnowned)).then(rows => rows[0]);
    expect(row?.status).toBe("done");
  });

  it("sibling carrier still open and holding its execution stamp", async () => {
    const row = await db.select({ status: issues.status, exec: issues.executionRunId })
      .from(issues).where(eq(issues.id, carrierSibling)).then(rows => rows[0]);
    expect(row?.status).toBe("in_progress");
    expect(row?.exec).toBe(liveRunId);
  });
});
