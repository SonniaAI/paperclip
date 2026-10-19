import { randomUUID } from "node:crypto";
import express from "express";
import request from "supertest";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  agents,
  companies,
  createDb,
  heartbeatRuns,
  issues,
} from "@paperclipai/db";
import {
  getEmbeddedPostgresTestSupport,
  startEmbeddedPostgresTestDatabase,
} from "./helpers/embedded-postgres.js";
import { errorHandler } from "../middleware/index.js";
import { issueRoutes } from "../routes/issues.js";

const embeddedPostgresSupport = await getEmbeddedPostgresTestSupport();
const describeEmbeddedPostgres = embeddedPostgresSupport.supported ? describe : describe.skip;

if (!embeddedPostgresSupport.supported) {
  console.warn(
    `Skipping embedded Postgres external-blocker route tests on this host: ${
      embeddedPostgresSupport.reason ?? "unsupported environment"
    }`,
  );
}

type Db = ReturnType<typeof createDb>;

const blocker = (owner: string, note: string) => ({ owner, note });

describeEmbeddedPostgres("issue externalBlocker", () => {
  let tempDb: Awaited<ReturnType<typeof startEmbeddedPostgresTestDatabase>>;
  let db: Db;
  let companyId: string;
  let agentId: string;
  let issueId: string;
  let secondIssueId: string;
  let appMain: express.Express;
  let appSecond: express.Express;

  beforeAll(async () => {
    tempDb = await startEmbeddedPostgresTestDatabase("paperclip-external-blocker-");
    db = createDb(tempDb.connectionString);

    const nonce = randomUUID().slice(0, 8);
    const [company] = await db.insert(companies).values({
      name: `External blocker ${nonce}`,
      issuePrefix: `EB${nonce.slice(0, 4).toUpperCase()}`,
      defaultResponsibleUserId: "board-user",
    }).returning();
    companyId = company!.id;
    const [agent] = await db.insert(agents).values({
      companyId,
      name: "External Blocker Engineer",
      role: "engineer",
      adapterType: "process",
      adapterConfig: {},
      runtimeConfig: {},
      permissions: {},
    }).returning();
    agentId = agent!.id;
    const [issue] = await db.insert(issues).values({
      companyId,
      title: "External blocker probe",
      status: "backlog",
      priority: "medium",
      assigneeAgentId: agentId,
      responsibleUserId: "board-user",
    }).returning();
    issueId = issue!.id;
    const [second] = await db.insert(issues).values({
      companyId,
      title: "External blocker probe two",
      status: "backlog",
      priority: "medium",
      assigneeAgentId: agentId,
      responsibleUserId: "board-user",
    }).returning();
    secondIssueId = second!.id;

    const [runMain] = await db.insert(heartbeatRuns).values({
      companyId,
      agentId,
      status: "running",
      contextSnapshot: { issueId },
    }).returning();
    const [runSecond] = await db.insert(heartbeatRuns).values({
      companyId,
      agentId,
      status: "running",
      contextSnapshot: { issueId: secondIssueId },
    }).returning();

    const makeApp = (run: string) => {
      const actor = {
        type: "agent",
        agentId,
        companyId,
        runId: run,
        source: "agent_jwt",
      };
      const expressApp = express();
      expressApp.use(express.json());
      expressApp.use((req, _res, next) => {
        (req as any).actor = actor;
        next();
      });
      expressApp.use("/api", issueRoutes(db, {} as any));
      expressApp.use(errorHandler);
      return expressApp;
    };
    appMain = makeApp(runMain!.id);
    appSecond = makeApp(runSecond!.id);
  }, 30_000);

  afterAll(async () => {
    await tempDb?.cleanup();
  });

  it("persists externalBlocker set while entering blocked and returns it via GET", async () => {
    const blocked = await request(appMain)
      .patch(`/api/issues/${issueId}`)
      .send({ status: "blocked", externalBlocker: blocker("Arthur (founder)", "waiting on spend decision") });
    expect(blocked.status, JSON.stringify(blocked.body)).toBe(200);
    expect(blocked.body.externalBlocker).toEqual({ owner: "Arthur (founder)", note: "waiting on spend decision" });

    const read = await request(appMain).get(`/api/issues/${issueId}`);
    expect(read.status, JSON.stringify(read.body)).toBe(200);
    expect(read.body.externalBlocker).toEqual({ owner: "Arthur (founder)", note: "waiting on spend decision" });
  });

  it("persists externalBlocker re-set on an already-blocked issue (regression)", async () => {
    const patched = await request(appMain)
      .patch(`/api/issues/${issueId}`)
      .send({ externalBlocker: blocker("droz-box", "operator host offline") });
    expect(patched.status, JSON.stringify(patched.body)).toBe(200);

    const read = await request(appMain).get(`/api/issues/${issueId}`);
    expect(read.status, JSON.stringify(read.body)).toBe(200);
    expect(read.body.externalBlocker).toEqual({ owner: "droz-box", note: "operator host offline" });
  });

  it("accepts externalBlocker as entering-blocked justification", async () => {
    const blocked = await request(appSecond)
      .patch(`/api/issues/${secondIssueId}`)
      .send({ status: "blocked", externalBlocker: blocker("external vendor", "SLA window") });
    expect(blocked.status, JSON.stringify(blocked.body)).toBe(200);
    expect(blocked.body.status).toBe("blocked");
    expect(blocked.body.unblockDescriptor).toBeNull();
  });

  it("rejects externalBlocker with a non-blocked target status", async () => {
    const rejected = await request(appSecond)
      .patch(`/api/issues/${secondIssueId}`)
      .send({ status: "in_progress", externalBlocker: blocker("external vendor", "SLA window") });
    expect(rejected.status).toBe(422);
  });

  it("clears externalBlocker when leaving blocked", async () => {
    const resumed = await request(appSecond)
      .patch(`/api/issues/${secondIssueId}`)
      .send({ status: "in_progress" });
    expect(resumed.status, JSON.stringify(resumed.body)).toBe(200);

    const read = await request(appSecond).get(`/api/issues/${secondIssueId}`);
    expect(read.status, JSON.stringify(read.body)).toBe(200);
    expect(read.body.externalBlocker).toBeNull();
  });
});
