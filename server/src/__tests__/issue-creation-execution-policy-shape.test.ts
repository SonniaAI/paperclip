import { randomUUID } from "node:crypto";
import express from "express";
import request from "supertest";
import { eq, sql } from "drizzle-orm";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import {
  agents,
  companies,
  createDb,
  issues,
} from "@paperclipai/db";
import {
  getEmbeddedPostgresTestSupport,
  startEmbeddedPostgresTestDatabase,
} from "./helpers/embedded-postgres.js";
import { errorHandler } from "../middleware/index.js";
import { issueRoutes } from "../routes/issues.js";
import {
  normalizeIssueExecutionPolicy,
  repairStoredIssueExecutionPolicy,
} from "../services/issue-execution-policy.ts";

const embeddedPostgresSupport = await getEmbeddedPostgresTestSupport();
const describeEmbeddedPostgres = embeddedPostgresSupport.supported ? describe : describe.skip;

const GUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * SON-3901 regression: the issue-creation writer must emit only new-shape
 * executionPolicy participants ({type: "agent" | "user", id: GUID}) and GUID
 * stage ids. Legacy shape — participants keyed {id, agentId} with no `type`
 * discriminator, and literal (non-GUID) stage ids such as "reconciler-verify" —
 * is what wedged every write on affected cards with 422 "Invalid execution
 * policy" (see the vendored execution-reconciler hotfix, patches/
 * son-3707-execution-reconciler). These tests pin the invariant end to end:
 * a created card stores a policy that re-validates as-is with zero legacy
 * participant keys and zero non-GUID stage ids, and legacy request shapes are
 * rejected at creation so the writer can never persist them.
 */
describeEmbeddedPostgres("issue creation execution policy shape (SON-3901)", () => {
  let tempDb: Awaited<ReturnType<typeof startEmbeddedPostgresTestDatabase>> | null = null;
  let db: ReturnType<typeof createDb>;

  beforeAll(async () => {
    tempDb = await startEmbeddedPostgresTestDatabase("paperclip-son3901-policy-shape-");
    db = createDb(tempDb.connectionString);
  }, 30_000);

  afterEach(async () => {
    // One-shot teardown: CASCADE follows FK edges in the referrer direction,
    // so the creation path's wide heartbeat/wake/activity graph cannot race
    // ordered deletes (heartbeat_runs is referenced by run_events, activity_log,
    // watchdogs, decision queues, comments, and more). Fire-and-forget wake
    // writes from the request may land after the first truncate; the settle +
    // second truncate sweeps them before the next test.
    const truncate = () =>
      db.execute(sql`
        truncate table
          issue_comments, issue_inbox_archives, issue_relations,
          heartbeat_run_events, heartbeat_runs, agent_wakeup_requests,
          activity_log, environment_leases, environments,
          issue_watchdogs, decision_queues,
          issues, agents, companies
        cascade
      `);
    await truncate();
    await new Promise((resolve) => setTimeout(resolve, 100));
    await truncate();
  });

  afterAll(async () => {
    await tempDb?.cleanup();
  });

  async function seedCompany() {
    const companyId = randomUUID();
    const coderId = randomUUID();
    const prefix = `S3901${companyId.replaceAll("-", "").slice(0, 4).toUpperCase()}`;
    await db.insert(companies).values({
      id: companyId,
      name: "Policy Shape Co",
      issuePrefix: prefix,
      requireBoardApprovalForNewAgents: false,
    });
    await db.insert(agents).values({
      id: coderId,
      companyId,
      name: "Coder",
      role: "engineer",
      status: "idle",
      adapterType: "codex_local",
      adapterConfig: {},
      runtimeConfig: {},
      permissions: {},
    });
    return { companyId, coderId };
  }

  function createApp(actor: any = { type: "board", source: "local_implicit" }) {
    const app = express();
    app.use(express.json());
    app.use((req, _res, next) => {
      (req as any).actor = actor;
      next();
    });
    app.use("/api", issueRoutes(db, {} as any, {}));
    app.use(errorHandler);
    return app;
  }

  async function countIssues(companyId: string) {
    const rows = await db.select().from(issues).where(eq(issues.companyId, companyId));
    return rows.length;
  }

  function nonGuidStageIds(policy: any): unknown[] {
    return (policy?.stages ?? [])
      .filter((stage: any) => typeof stage?.id !== "string" || !GUID_RE.test(stage.id))
      .map((stage: any) => stage?.id ?? null);
  }

  function legacyParticipantCount(policy: any): number {
    let count = 0;
    for (const stage of policy?.stages ?? []) {
      for (const participant of stage?.participants ?? []) {
        const typeless =
          participant == null
          || typeof participant !== "object"
          || typeof (participant as any).type !== "string";
        // Legacy {id, agentId} shape: principal keyed by agentId with no type.
        if (typeless && participant != null && "agentId" in participant) count += 1;
      }
    }
    return count;
  }

  it("creates a card whose stored executionPolicy validates with zero legacy {id, agentId} participants and zero non-GUID stage ids", async () => {
    const { companyId, coderId } = await seedCompany();
    const app = createApp();

    const res = await request(app)
      .post(`/api/companies/${companyId}/issues`)
      .send({
        title: "Writer shape regression card",
        status: "todo",
        priority: "high",
        assigneeAgentId: coderId,
        executionPolicy: {
          stages: [
            {
              type: "review",
              approvalsNeeded: 1,
              // No stage id and no participant id: the writer must mint GUIDs.
              participants: [{ type: "agent", agentId: coderId }],
            },
          ],
        },
      });
    expect([200, 201]).toContain(res.status);
    expect(res.body?.id).toBeTruthy();

    const [stored] = await db.select().from(issues).where(eq(issues.id, res.body.id));
    expect(stored).toBeTruthy();
    const policy = (stored as any).executionPolicy;
    expect(policy).toBeTruthy();
    expect(policy.stages).toHaveLength(1);

    // Zero non-GUID stage ids: absent stage ids are minted as GUIDs.
    expect(nonGuidStageIds(policy)).toEqual([]);
    for (const stage of policy.stages) {
      expect(GUID_RE.test(stage.id)).toBe(true);
    }

    // Zero legacy {id, agentId} participants: every participant is typed and
    // carries a GUID principal id.
    expect(legacyParticipantCount(policy)).toBe(0);
    for (const participant of policy.stages[0].participants) {
      expect(["agent", "user"]).toContain(participant.type);
      expect(GUID_RE.test(participant.id)).toBe(true);
    }

    // The stored policy re-validates as-is: strict normalization does not
    // throw, and the stored-data repair path reports zero repairs and zero
    // warnings (no legacy key or non-GUID id needed touching).
    expect(() => normalizeIssueExecutionPolicy(policy)).not.toThrow();
    const repair = repairStoredIssueExecutionPolicy(policy);
    expect(repair.policy).not.toBeNull();
    expect(repair.repaired).toBe(false);
    expect(repair.warnings).toEqual([]);

    // What the writer produced is exactly what was stored.
    expect(policy).toEqual(res.body.executionPolicy);
  });

  it("rejects legacy typeless {id, agentId} participants at creation so the writer can never persist them", async () => {
    const { companyId, coderId } = await seedCompany();
    const app = createApp();

    const res = await request(app)
      .post(`/api/companies/${companyId}/issues`)
      .send({
        title: "Legacy participant shape card",
        status: "todo",
        assigneeAgentId: coderId,
        executionPolicy: {
          stages: [
            {
              type: "review",
              // Legacy reconciler shape: {id, agentId} with no type key.
              participants: [{ id: coderId, agentId: coderId }],
            },
          ],
        },
      });
    // Generic body-validation failures keep the long-standing 400.
    expect(res.status).toBe(400);
    expect(await countIssues(companyId)).toBe(0);
  });

  it("rejects literal (non-GUID) stage ids at creation", async () => {
    const { companyId, coderId } = await seedCompany();
    const app = createApp();

    const res = await request(app)
      .post(`/api/companies/${companyId}/issues`)
      .send({
        title: "Literal stage id card",
        status: "todo",
        assigneeAgentId: coderId,
        executionPolicy: {
          stages: [
            {
              id: "reconciler-verify",
              type: "review",
              participants: [{ type: "agent", agentId: coderId }],
            },
          ],
        },
      });
    expect(res.status).toBe(400);
    expect(await countIssues(companyId)).toBe(0);
  });
});
