import { randomUUID } from "node:crypto";
import express from "express";
import request from "supertest";
import { describe, expect, it } from "vitest";
import {
  agents,
  companies,
  createDb,
  heartbeatRuns,
  issues,
} from "@paperclipai/db";
import { errorHandler } from "../middleware/index.js";
import { issueRoutes } from "../routes/issues.js";
import { describeEmbeddedPostgres, useEmbeddedPostgres } from "./helpers/route-test-harness.js";

type Db = ReturnType<typeof createDb>;

const pg = useEmbeddedPostgres("paperclip-issue-monitor-anchor-");

// Route-level composition note (SON-1524 B1 correction): the run-anchor grant
// inside assertCanManageIssueMonitor is reachable for NON-assignee agents via
// POST /issues/:id/monitor/check-now (PATCH /issues/:id stops non-assignees
// earlier at the assignee run-lock guard, issue_write_assignee_run_lock).
// These tests drive that wire site with a seeded scheduled monitor so the
// only differentiator between cases is the gate decision itself.
async function seedFixture(
  db: Db,
  opts: { runStatus: string; actorSource: "agent_jwt" | "agent_key"; crossIssueRun?: boolean },
) {
  const nonce = randomUUID().slice(0, 8);
  const [company] = await db.insert(companies).values({
    name: `Monitor Anchor ${nonce}`,
    issuePrefix: `MA${nonce.slice(0, 4).toUpperCase()}`,
    defaultResponsibleUserId: "board-user",
  }).returning();
  const agentSeed = (name: string) => ({
    companyId: company!.id,
    name,
    role: "engineer",
    adapterType: "process",
    adapterConfig: {},
    runtimeConfig: {},
    permissions: {},
  });
  const [assignee] = await db.insert(agents).values(agentSeed(`Assignee ${nonce}`)).returning();
  const [caller] = await db.insert(agents).values(agentSeed(`Caller ${nonce}`)).returning();
  const [run] = await db.insert(heartbeatRuns).values({
    companyId: company!.id,
    agentId: caller!.id,
    status: opts.runStatus,
  }).returning();
  const [unanchoredRun] = await db.insert(heartbeatRuns).values({
    companyId: company!.id,
    agentId: caller!.id,
    status: "running",
  }).returning();
  const [issue] = await db.insert(issues).values({
    companyId: company!.id,
    projectId: null,
    parentId: null,
    title: `Anchor fixture ${nonce}`,
    status: "in_progress",
    priority: "medium",
    assigneeAgentId: assignee!.id,
    responsibleUserId: "board-user",
    checkoutRunId: run!.id,
    executionRunId: null,
    monitorNextCheckAt: new Date(Date.now() + 60 * 60_000),
  }).returning();
  const actor = {
    type: "agent" as const,
    agentId: caller!.id,
    companyId: company!.id,
    runId: opts.crossIssueRun ? unanchoredRun!.id : run!.id,
    source: opts.actorSource,
  };
  const app = express();
  app.use(express.json());
  app.use((req, _res, next) => {
    req.actor = actor;
    next();
  });
  app.use("/api", issueRoutes(db, {} as any));
  app.use(errorHandler);
  return { app, issue };
}

describeEmbeddedPostgres("SON-1524 B1 monitor run-anchor route authz", () => {
  it("allows a non-assignee agent with a live signed-JWT run anchored to the issue to check the monitor now", async () => {
    const { app, issue } = await seedFixture(pg.db, { runStatus: "running", actorSource: "agent_jwt" });
    const res = await request(app).post(`/api/issues/${issue!.id}/monitor/check-now`);
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ ok: true });
  });

  it("rejects an agent_key actor spoofing the live anchored run id via the header (regression)", async () => {
    const { app, issue } = await seedFixture(pg.db, { runStatus: "running", actorSource: "agent_key" });
    const res = await request(app).post(`/api/issues/${issue!.id}/monitor/check-now`);
    expect(res.status).toBe(403);
  });

  it("rejects a signed-JWT actor whose anchored run is no longer live", async () => {
    const { app, issue } = await seedFixture(pg.db, { runStatus: "succeeded", actorSource: "agent_jwt" });
    const res = await request(app).post(`/api/issues/${issue!.id}/monitor/check-now`);
    expect(res.status).toBe(403);
  });

  it("rejects a live run that is not anchored to the issue (cross-issue / post-takeover)", async () => {
    const { app, issue } = await seedFixture(pg.db, { runStatus: "running", actorSource: "agent_jwt", crossIssueRun: true });
    const res = await request(app).post(`/api/issues/${issue!.id}/monitor/check-now`);
    expect(res.status).toBe(403);
  });
});
