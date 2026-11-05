import express from "express";
import request from "supertest";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.unmock("http");
vi.unmock("node:http");

const agentId = "11111111-1111-4111-8111-111111111111";
const ceoAgentId = "44444444-4444-4444-8444-444444444444";
const companyId = "22222222-2222-4222-8222-222222222222";
const runId = "55555555-5555-4555-8555-555555555555";

const baseAgent = {
  id: agentId,
  companyId,
  name: "Platform Engineering",
  urlKey: "platform-engineering",
  role: "engineer",
  title: "Platform Engineering",
  icon: null,
  status: "error",
  reportsTo: ceoAgentId,
  capabilities: null,
  adapterType: "openclaw_gateway",
  adapterConfig: {},
  runtimeConfig: {},
  budgetMonthlyCents: 0,
  spentMonthlyCents: 0,
  pauseReason: null,
  pausedAt: null,
  permissions: { canCreateAgents: false },
  lastHeartbeatAt: new Date("2026-11-05T03:31:11.000Z"),
  metadata: null,
  createdAt: new Date("2026-04-11T00:00:00.000Z"),
  updatedAt: new Date("2026-04-11T00:00:00.000Z"),
};

const ceoAgent = {
  ...baseAgent,
  id: ceoAgentId,
  name: "CEO",
  urlKey: "ceo",
  role: "ceo",
  title: "CEO",
  status: "idle",
  reportsTo: null,
};

const mockAgentService = vi.hoisted(() => ({
  getById: vi.fn(),
  pause: vi.fn(),
  resume: vi.fn(),
  clearError: vi.fn(),
  terminate: vi.fn(),
  remove: vi.fn(),
  listKeys: vi.fn(),
  createApiKey: vi.fn(),
  getKeyById: vi.fn(),
  revokeKey: vi.fn(),
}));

const mockAccessService = vi.hoisted(() => ({
  canUser: vi.fn(),
  decide: vi.fn(),
  hasPermission: vi.fn(),
  getMembership: vi.fn(),
  ensureMembership: vi.fn(),
  listPrincipalGrants: vi.fn(),
  setPrincipalPermission: vi.fn(),
}));

const mockHeartbeatService = vi.hoisted(() => ({
  cancelActiveForAgent: vi.fn(),
  wakeup: vi.fn(),
}));

const mockIssueService = vi.hoisted(() => ({ list: vi.fn() }));
const mockLogActivity = vi.hoisted(() => vi.fn());
const mockGetTelemetryClient = vi.hoisted(() => vi.fn());

vi.mock("@paperclipai/shared/telemetry", () => ({
  trackAgentCreated: vi.fn(),
  trackErrorHandlerCrash: vi.fn(),
}));

vi.mock("../telemetry.js", () => ({
  getTelemetryClient: mockGetTelemetryClient,
}));

vi.mock("../routes/authz.js", async () => {
  const { forbidden, unauthorized } = await vi.importActual<typeof import("../errors.js")>("../errors.js");
  function assertAuthenticated(req: Express.Request) {
    if (req.actor.type === "none") {
      throw unauthorized();
    }
  }

  function assertBoard(req: Express.Request) {
    if (req.actor.type !== "board") {
      throw forbidden("Board access required");
    }
  }

  function assertCompanyAccess(req: Express.Request, expectedCompanyId: string) {
    assertAuthenticated(req);
    if (req.actor.type === "agent" && req.actor.companyId !== expectedCompanyId) {
      throw forbidden("Agent key cannot access another company");
    }
    if (req.actor.type === "board" && req.actor.source !== "local_implicit") {
      const allowedCompanies = req.actor.companyIds ?? [];
      if (!allowedCompanies.includes(expectedCompanyId)) {
        throw forbidden("User does not have access to this company");
      }
    }
  }

  function hasCompanyAccess(req: Express.Request, expectedCompanyId: string): boolean {
    if (req.actor.type === "none") return false;
    if (req.actor.type === "agent") return req.actor.companyId === expectedCompanyId;
    if (req.actor.source === "local_implicit") return true;
    return (req.actor.companyIds ?? []).includes(expectedCompanyId);
  }

  async function getAccessibleResource<T extends { companyId: string }>(
    req: Express.Request,
    res: { status(code: number): { json(body: unknown): unknown } },
    resource: T | null | undefined | Promise<T | null | undefined>,
    notFoundMessage: string,
  ): Promise<T | null> {
    const resolved = await resource;
    if (!resolved || !hasCompanyAccess(req, resolved.companyId)) {
      res.status(404).json({ error: notFoundMessage });
      return null;
    }
    assertCompanyAccess(req, resolved.companyId);
    return resolved;
  }

  function assertInstanceAdmin(req: Express.Request) {
    assertBoard(req);
    if (req.actor.source === "local_implicit" || req.actor.isInstanceAdmin) return;
    throw forbidden("Instance admin access required");
  }

  function getActorInfo(req: Express.Request) {
    assertAuthenticated(req);
    if (req.actor.type === "agent") {
      return {
        actorType: "agent" as const,
        actorId: req.actor.agentId ?? "unknown-agent",
        agentId: req.actor.agentId ?? null,
        runId: req.actor.runId ?? null,
        agentApiKeyId: req.actor.keyId ?? null,
      };
    }
    return {
      actorType: "user" as const,
      actorId: req.actor.userId ?? "board",
      agentId: null,
      runId: req.actor.runId ?? null,
      agentApiKeyId: null,
    };
  }

  return {
    assertAuthenticated,
    assertBoard,
    assertCompanyAccess,
    assertInstanceAdmin,
    getAccessibleResource,
    getActorInfo,
    hasCompanyAccess,
  };
});

vi.mock("../services/index.js", () => ({
  agentService: () => mockAgentService,
  accessService: () => mockAccessService,
  budgetService: () => ({ upsertPolicy: vi.fn() }),
  builtInAgentService: () => ({ ensureCompanyDefaultAgentGrants: vi.fn() }),
  companySkillService: () => ({ listRuntimeSkillEntries: vi.fn(), resolveRequestedSkillKeys: vi.fn() }),
  heartbeatService: () => mockHeartbeatService,
  issueService: () => mockIssueService,
  logActivity: mockLogActivity,
  secretService: () => ({
    normalizeAdapterConfigForPersistence: vi.fn(),
    resolveAdapterConfigForRuntime: vi.fn(),
  }),
  agentInstructionsService: () => ({ materializeManagedBundle: vi.fn() }),
  workspaceOperationService: () => ({}),
  approvalService: () => ({ create: vi.fn(), getById: vi.fn() }),
  issueApprovalService: () => ({ linkManyForApproval: vi.fn() }),
  syncInstructionsBundleConfigFromFilePath: vi.fn((_agent: unknown, config: unknown) => config),
}));

vi.mock("../services/instance-settings.js", () => ({
  instanceSettingsService: () => ({
    getGeneral: vi.fn(async () => ({ censorUsernameInLogs: false })),
  }),
}));

let routeModules:
  | Promise<[
      typeof import("../middleware/index.js"),
      typeof import("../routes/agents.js"),
    ]>
  | null = null;

async function loadRouteModules() {
  routeModules ??= Promise.all([
    import("../middleware/index.js"),
    import("../routes/agents.js"),
  ]);
  return routeModules;
}

async function createApp(actor: Record<string, unknown>) {
  const [{ errorHandler }, { agentRoutes }] = await loadRouteModules();
  const app = express();
  app.use(express.json());
  app.use((req, _res, next) => {
    (req as any).actor = {
      ...actor,
      companyIds: Array.isArray(actor.companyIds) ? [...actor.companyIds] : actor.companyIds,
    };
    next();
  });
  app.use("/api", agentRoutes({} as any));
  app.use(errorHandler);
  return app;
}

async function requestApp(
  app: express.Express,
  buildRequest: (baseUrl: string) => request.Test,
) {
  const { createServer } = await vi.importActual<typeof import("node:http")>("node:http");
  const server = createServer(app);
  try {
    await new Promise<void>((resolve) => {
      server.listen(0, "127.0.0.1", resolve);
    });
    const address = server.address();
    if (!address || typeof address === "string") {
      throw new Error("Expected HTTP server to listen on a TCP port");
    }
    return await buildRequest(`http://127.0.0.1:${address.port}`);
  } finally {
    if (server.listening) {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => {
          if (error) reject(error);
          else resolve();
        });
      });
    }
  }
}

const boardActor = {
  type: "board",
  userId: "user-1",
  source: "local_implicit",
  isInstanceAdmin: false,
  companyIds: [companyId],
};

const ceoAgentActor = {
  type: "agent",
  agentId: ceoAgentId,
  companyId,
  source: "agent_key",
  keyId: "key-ceo",
};

const engineerAgentActor = {
  type: "agent",
  agentId,
  companyId,
  source: "agent_key",
  keyId: "key-eng",
};

function resetMockDefaults() {
  vi.clearAllMocks();
  mockAgentService.getById.mockImplementation(async (id: string) =>
    id === ceoAgentId ? { ...ceoAgent } : { ...baseAgent, id },
  );
  mockAgentService.clearError.mockImplementation(async () => ({
    ...baseAgent,
    status: "idle",
  }));
  mockAccessService.decide.mockImplementation(async () => ({
    allowed: true,
    action: "test",
    reason: "allow_test",
    explanation: "Allowed by test.",
  }));
  mockHeartbeatService.wakeup.mockImplementation(async () => ({
    id: runId,
    status: "queued",
  }));
  mockGetTelemetryClient.mockReturnValue({ track: vi.fn() });
}

beforeEach(() => {
  resetMockDefaults();
});

describe("POST /agents/:id/clear-error — SON-6222 supervised recovery", () => {
  it("board actors keep clearing lane errors", async () => {
    const app = await createApp(boardActor);
    const res = await requestApp(app, (baseUrl) =>
      request(baseUrl).post(`/api/agents/${agentId}/clear-error`).send({}),
    );
    expect(res.status).toBe(200);
    expect(mockAgentService.clearError).toHaveBeenCalledWith(agentId);
    expect(mockLogActivity).toHaveBeenCalledTimes(1);
    const activity = mockLogActivity.mock.calls[0]![1];
    expect(activity.actorType).toBe("user");
    expect(activity.details.clearedBy).toBe("board");
  });

  it("the company CEO-role agent can clear a peer lane error (audit-logged as agent)", async () => {
    const app = await createApp(ceoAgentActor);
    const res = await requestApp(app, (baseUrl) =>
      request(baseUrl).post(`/api/agents/${agentId}/clear-error`).send({}),
    );
    expect(res.status).toBe(200);
    expect(mockAgentService.clearError).toHaveBeenCalledWith(agentId);
    const activity = mockLogActivity.mock.calls[0]![1];
    expect(activity.actorType).toBe("agent");
    expect(activity.agentId).toBe(ceoAgentId);
    expect(activity.details.clearedBy).toBe("ceo_agent");
  });

  it("non-CEO agent actors still get 403 Board access required", async () => {
    const app = await createApp(engineerAgentActor);
    const res = await requestApp(app, (baseUrl) =>
      request(baseUrl).post(`/api/agents/${agentId}/clear-error`).send({}),
    );
    expect(res.status).toBe(403);
    expect(res.body.error).toBe("Board access required");
    expect(mockAgentService.clearError).not.toHaveBeenCalled();
  });

  it("a CEO-role agent from another company is rejected (cross-tenant reads are 404)", async () => {
    const app = await createApp({
      ...ceoAgentActor,
      companyId: "99999999-9999-4999-8999-999999999999",
    });
    const res = await requestApp(app, (baseUrl) =>
      request(baseUrl).post(`/api/agents/${agentId}/clear-error`).send({}),
    );
    expect(res.status).toBe(404);
    expect(mockAgentService.clearError).not.toHaveBeenCalled();
  });
});

describe("agent wakeup peer-invocation — SON-6222 retry path", () => {
  it("the CEO-role agent can wake a peer lane directly", async () => {
    const app = await createApp(ceoAgentActor);
    const res = await requestApp(app, (baseUrl) =>
      request(baseUrl).post(`/api/agents/${agentId}/wakeup`).send({ source: "on_demand" }),
    );
    expect(res.status).toBe(202);
    expect(mockHeartbeatService.wakeup).toHaveBeenCalledTimes(1);
  });

  it("non-CEO agent actors still cannot wake peers", async () => {
    const app = await createApp(engineerAgentActor);
    const res = await requestApp(app, (baseUrl) =>
      request(baseUrl).post(`/api/agents/${ceoAgentId}/wakeup`).send({ source: "on_demand" }),
    );
    expect(res.status).toBe(403);
    expect(res.body.error).toBe("Agent can only invoke itself");
    expect(mockHeartbeatService.wakeup).not.toHaveBeenCalled();
  });

  it("agent self-invocation stays allowed", async () => {
    const app = await createApp(engineerAgentActor);
    const res = await requestApp(app, (baseUrl) =>
      request(baseUrl).post(`/api/agents/${agentId}/wakeup`).send({ source: "on_demand" }),
    );
    expect(res.status).toBe(202);
    expect(mockHeartbeatService.wakeup).toHaveBeenCalledTimes(1);
  });

  it("the legacy heartbeat invoke endpoint allows CEO peers too", async () => {
    const app = await createApp(ceoAgentActor);
    const res = await requestApp(app, (baseUrl) =>
      request(baseUrl).post(`/api/agents/${agentId}/heartbeat/invoke`).send({}),
    );
    expect(res.status).toBe(202);
    expect(mockHeartbeatService.wakeup).toHaveBeenCalledTimes(1);
  });

  it("the legacy heartbeat invoke endpoint still rejects non-CEO peers", async () => {
    const app = await createApp(engineerAgentActor);
    const res = await requestApp(app, (baseUrl) =>
      request(baseUrl).post(`/api/agents/${ceoAgentId}/heartbeat/invoke`).send({}),
    );
    expect(res.status).toBe(403);
    expect(mockHeartbeatService.wakeup).not.toHaveBeenCalled();
  });
});
