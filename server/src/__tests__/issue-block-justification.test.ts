import express from "express";
import request from "supertest";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HttpError } from "../errors.js";

const issueId = "11111111-1111-4111-8111-111111111111";
const companyId = "22222222-2222-4222-8222-222222222222";
const ownerAgentId = "33333333-3333-4333-8333-333333333333";
const peerAgentId = "44444444-4444-4444-8444-444444444444";
const ownerRunId = "55555555-5555-4555-8555-555555555555";
const recoveryActionId = "77777777-7777-4777-8777-777777777777";

const mockIssueService = vi.hoisted(() => ({
  addComment: vi.fn(),
  assertCheckoutOwner: vi.fn(),
  create: vi.fn(),
  createChild: vi.fn(),
  decomposeAcceptedPlan: vi.fn(),
  getAttachmentById: vi.fn(),
  getByIdentifier: vi.fn(),
  getById: vi.fn(),
  getByIdForUpdate: vi.fn(),
  getComment: vi.fn(),
  getDependencyReadiness: vi.fn(),
  getRelationSummaries: vi.fn(),
  getWakeableParentAfterChildCompletion: vi.fn(),
  list: vi.fn(),
  listAttachments: vi.fn(),
  listComments: vi.fn(),
  listWakeableBlockedDependents: vi.fn(),
  remove: vi.fn(),
  removeAttachment: vi.fn(),
  update: vi.fn(),
  findMentionedAgents: vi.fn(),
}));

const mockAccessService = vi.hoisted(() => ({
  canUser: vi.fn(),
  decide: vi.fn(),
  hasPermission: vi.fn(),
}));

const mockAgentService = vi.hoisted(() => ({
  getById: vi.fn(),
  list: vi.fn(),
  resolveByReference: vi.fn(),
}));

const mockCompanyService = vi.hoisted(() => ({
  getById: vi.fn(),
}));

const mockDocumentService = vi.hoisted(() => ({
  upsertIssueDocument: vi.fn(),
}));

const mockWorkProductService = vi.hoisted(() => ({
  createForIssue: vi.fn(),
  getById: vi.fn(),
  remove: vi.fn(),
  update: vi.fn(),
}));

const mockStorageService = vi.hoisted(() => ({
  provider: "local_disk",
  putFile: vi.fn(),
  getObject: vi.fn(),
  headObject: vi.fn(),
  deleteObject: vi.fn(),
}));
const mockIssueThreadInteractionService = vi.hoisted(() => ({
  expirePendingInteractionsForTerminalIssue: vi.fn(async () => []),
  expireRequestConfirmationsSupersededByComment: vi.fn(async () => []),
  expireStaleRequestConfirmationsForIssueDocument: vi.fn(async () => []),
  expireRequestConfirmationsSupersededByHistoricalComments: vi.fn(async () => []),
  listForIssue: vi.fn(async () => []),
}));
const mockIssueApprovalService = vi.hoisted(() => ({
  link: vi.fn(),
  unlink: vi.fn(),
  listApprovalsForIssue: vi.fn(async () => []),
}));
const mockIssueRecoveryActionService = vi.hoisted(() => ({
  getActiveForIssue: vi.fn(async () => null),
  listActiveForIssues: vi.fn(async () => new Map()),
  resolveActiveForIssue: vi.fn(async () => null),
}));
const mockTaskWatchdogService = vi.hoisted(() => ({
  getActiveForIssue: vi.fn(async () => null),
  revalidateMutationScope: vi.fn(async () => ({
    allowed: true,
    classification: { state: "stopped", stopFingerprint: "task_watchdog_stop:test" },
  })),
  reconcileForIssueAndAncestors: vi.fn(async () => ({
    checked: 0,
    triggered: 0,
    skipped: 0,
    watchdogIssueIds: [],
  })),
  upsertForIssue: vi.fn(),
  disableForIssue: vi.fn(async () => null),
}));
const mockHeartbeatService = vi.hoisted(() => ({
  wakeup: vi.fn(async () => undefined),
  reportRunActivity: vi.fn(async () => undefined),
  getRun: vi.fn(async () => null),
  getActiveRunForAgent: vi.fn(async () => null),
  cancelRun: vi.fn(async () => null),
}));
const mockExternalObjectService = vi.hoisted(() => ({
  getIssueSummaries: vi.fn(async () => new Map()),
  getIssueSummary: vi.fn(async () => ({
    authRequiredCount: 0,
    byLiveness: {},
    byStatusCategory: {},
    highestSeverity: "muted",
    objects: [],
    staleCount: 0,
    total: 0,
    unreachableCount: 0,
  })),
  getProjectSummary: vi.fn(async () => ({
    authRequiredCount: 0,
    byLiveness: {},
    byStatusCategory: {},
    highestSeverity: "muted",
    objects: [],
    staleCount: 0,
    total: 0,
    unreachableCount: 0,
  })),
  listForIssue: vi.fn(async () => []),
  refreshIssueObjects: vi.fn(async () => []),
  syncCommentSafely: vi.fn(async () => undefined),
  syncDocumentSafely: vi.fn(async () => undefined),
  syncIssueSafely: vi.fn(async () => undefined),
}));
const mockLogActivity = vi.hoisted(() => vi.fn(async () => undefined));
const mockObserveCrossIssueInfluence = vi.hoisted(() => vi.fn(async () => null));

function registerRouteMocks() {
  vi.doMock("@paperclipai/shared/telemetry", () => ({
    trackAgentTaskCompleted: vi.fn(),
    trackErrorHandlerCrash: vi.fn(),
  }));

  vi.doMock("../telemetry.js", () => ({
    getTelemetryClient: vi.fn(() => ({ track: vi.fn() })),
  }));

  vi.doMock("../services/access.js", () => ({
    accessService: () => mockAccessService,
  }));

  vi.doMock("../services/agents.js", () => ({
    agentService: () => mockAgentService,
  }));

  vi.doMock("../services/documents.js", () => ({
    documentAnnotationService: () => ({ remapOpenThreadsForDocument: async () => [] }),
    documentService: () => mockDocumentService,
  }));

  vi.doMock("../services/issues.js", () => ({
    issueService: () => mockIssueService,
  }));

  vi.doMock("../services/work-products.js", () => ({
    workProductService: () => mockWorkProductService,
  }));

  vi.doMock("../services/external-objects.js", () => ({
    externalObjectService: () => mockExternalObjectService,
  }));

  vi.doMock("../services/activity-log.js", () => ({
    logActivity: mockLogActivity,
  }));

  vi.doMock("../services/cross-issue-influence-limit.js", () => ({
    observeCrossIssueInfluence: mockObserveCrossIssueInfluence,
    crossIssueInfluenceLimitError: vi.fn(),
    crossIssueInfluenceRunContextError: () => new HttpError(
      403,
      "Agent issue comments and updates require a valid heartbeat run so cross-issue influence can be contained",
      { code: "cross_issue_influence_run_context_required" },
    ),
  }));

  vi.doMock("../services/index.js", () => ({
    ISSUE_LIST_DEFAULT_LIMIT: 100,
    ISSUE_LIST_MAX_LIMIT: 500,
    accessService: () => mockAccessService,
    agentService: () => mockAgentService,
    clampIssueListLimit: (value: number) => Math.min(Math.max(value, 1), 500),
    companySkillService: () => ({
      completeTestRunForIssue: vi.fn(async () => null),
    }),
    companyService: () => mockCompanyService,
    documentAnnotationService: () => ({ remapOpenThreadsForDocument: async () => [] }),
    documentService: () => mockDocumentService,
    executionWorkspaceService: () => ({}),
    feedbackService: () => ({
      listIssueVotesForUser: vi.fn(async () => []),
      saveIssueVote: vi.fn(async () => ({ vote: null, consentEnabledNow: false, sharingEnabled: false })),
    }),
    goalService: () => ({}),
    heartbeatService: () => mockHeartbeatService,
    instanceSettingsService: () => ({
      get: vi.fn(async () => ({
        id: "instance-settings-1",
        general: {
          censorUsernameInLogs: false,
          feedbackDataSharingPreference: "prompt",
        },
      })),
      listCompanyIds: vi.fn(async () => [companyId]),
    }),
    issueApprovalService: () => mockIssueApprovalService,
    issueRecoveryActionService: () => mockIssueRecoveryActionService,
    issueReferenceService: () => ({
      deleteDocumentSource: async () => undefined,
      diffIssueReferenceSummary: () => ({
        addedReferencedIssues: [],
        removedReferencedIssues: [],
        currentReferencedIssues: [],
      }),
      emptySummary: () => ({ outbound: [], inbound: [] }),
      listIssueReferenceSummary: async () => ({ outbound: [], inbound: [] }),
      syncComment: async () => undefined,
      syncDocument: async () => undefined,
      syncIssue: async () => undefined,
    }),
    issueService: () => mockIssueService,
    issueThreadInteractionService: () => mockIssueThreadInteractionService,
    taskWatchdogService: () => mockTaskWatchdogService,
    logActivity: mockLogActivity,
    projectService: () => ({}),
    routineService: () => ({
      syncRunStatusForIssue: vi.fn(async () => undefined),
    }),
    workProductService: () => mockWorkProductService,
  }));
}

function makeIssue(overrides: Record<string, unknown> = {}) {
  return {
    id: issueId,
    companyId,
    status: "in_progress",
    priority: "high",
    projectId: null,
    goalId: null,
    parentId: null,
    assigneeAgentId: ownerAgentId,
    assigneeUserId: null,
    createdByUserId: "board-user",
    identifier: "PAP-1649",
    title: "Owned active issue",
    executionPolicy: null,
    executionState: null,
    hiddenAt: null,
    ...overrides,
  };
}

function makeAgent(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    companyId,
    role: "engineer",
    reportsTo: null,
    permissions: { canCreateAgents: false },
    ...overrides,
  };
}

function createRunContextDb(
  contextSnapshot: Record<string, unknown> = {},
  runAgentOrRows: string | Record<string, unknown>[] = ownerAgentId,
  runId: string = ownerRunId,
) {
  const runRows = Array.isArray(runAgentOrRows)
    ? runAgentOrRows
    : [{
        id: runId,
        companyId,
        agentId: runAgentOrRows,
        agentCompanyId: companyId,
        contextSnapshot,
      }];
  const firstRun = runRows[0] ?? {};
  const runAgentId = typeof firstRun.agentId === "string" ? firstRun.agentId : ownerAgentId;
  const runAgentCompanyId = typeof firstRun.agentCompanyId === "string" ? firstRun.agentCompanyId : companyId;
  const rowsForSelection = (selection: Record<string, unknown>) => {
    const keys = Object.keys(selection);
    if (keys.includes("entityId")) return [];
    if (keys.includes("contextSnapshot")) return runRows;
    if (keys.includes("agentCompanyId")) return runRows;
    return [{ id: runAgentId, companyId: runAgentCompanyId, permissions: {}, role: "engineer", reportsTo: null }];
  };
  const buildQuery = (selection: Record<string, unknown>) => {
    const rows = rowsForSelection(selection);
    const whereResult = {
      orderBy: vi.fn(async () => []),
      limit: vi.fn(() => ({
        then: async (resolve: (limitedRows: unknown[]) => unknown) => resolve(rows),
      })),
      then: async (resolve: (selectedRows: unknown[]) => unknown) => resolve(rows),
    };
    const query = {
      innerJoin: vi.fn(() => query),
      where: vi.fn(() => whereResult),
    };
    return query;
  };
  return {
    transaction: async (callback: (tx: Record<string, never>) => Promise<unknown>) => callback({}),
    select: vi.fn((selection: Record<string, unknown> = {}) => ({
      from: vi.fn(() => buildQuery(selection)),
    })),
  };
}

async function createApp(actor: Record<string, unknown>, db?: unknown) {
  const routeDb = db ?? createRunContextDb(
    {},
    typeof actor.agentId === "string" ? actor.agentId : ownerAgentId,
    typeof actor.runId === "string" ? actor.runId : ownerRunId,
  );
  const [{ errorHandler }, { issueRoutes }] = await Promise.all([
    vi.importActual<typeof import("../middleware/index.js")>("../middleware/index.js"),
    vi.importActual<typeof import("../routes/issues.js")>("../routes/issues.js"),
  ]);
  const app = express();
  app.use(express.json());
  app.use((req, _res, next) => {
    (req as any).actor = actor;
    next();
  });
  app.use("/api", issueRoutes(routeDb as any, mockStorageService as any));
  app.use(errorHandler);
  return app;
}

function peerActor(overrides: Record<string, unknown> = {}) {
  return {
    type: "agent",
    agentId: peerAgentId,
    companyId,
    source: "agent_key",
    runId: "66666666-6666-4666-8666-666666666666",
    ...overrides,
  };
}

function ownerActor() {
  return {
    type: "agent",
    agentId: ownerAgentId,
    companyId,
    source: "agent_key",
    runId: ownerRunId,
  };
}

function boardActor() {
  return {
    type: "board",
    userId: "board-user",
    companyIds: [companyId],
    source: "local_implicit",
    isInstanceAdmin: false,
  };
}

/**
 * SON-3754 / SON-3774 - blocked-entry justification gate (route tests).
 *
 * Covers the enteringBlocked gate in server/src/routes/issues.ts:
 *  - bare {status:"blocked"}                        -> 400 from shared validator (unchanged)
 *  - blockedByIssueIds resolving only to done rows  -> 422 + SON-3754 how-to-fix message
 *  - externalBlocker {owner} alone                  -> accepted, passes gate, persists
 *  - live blocker edge                              -> still accepted (regression guard)
 *
 * Harness scaffold (service mocks, createApp) adapted from
 * issue-agent-mutation-ownership-routes.test.ts; the db stub is table-aware so
 * the gate's pending-interaction / pending-approval / unresolved-blocker
 * queries are deterministic.
 */
import { issueApprovals, issueThreadInteractions, issues as issueRows } from "@paperclipai/db";

function createGateDb(opts: { blockerRows?: Array<Record<string, unknown>> } = {}) {
  const runRows = [{
    id: ownerRunId,
    companyId,
    agentId: ownerAgentId,
    agentCompanyId: companyId,
    contextSnapshot: {},
  }];
  const rowsFor = (selection: Record<string, unknown>, table: unknown) => {
    if (table === issueThreadInteractions) return [];
    if (table === issueApprovals) return [];
    if (table === issueRows) return opts.blockerRows ?? [];
    const keys = Object.keys(selection);
    if (keys.includes("entityId")) return [];
    if (keys.includes("contextSnapshot")) return runRows;
    if (keys.includes("agentCompanyId")) return runRows;
    return [{ id: ownerAgentId, companyId, permissions: {}, role: "engineer", reportsTo: null }];
  };
  const buildQuery = (selection: Record<string, unknown>, table: unknown): Record<string, unknown> => {
    const rows = rowsFor(selection, table);
    const query: Record<string, unknown> = {
      innerJoin: vi.fn(() => query),
      leftJoin: vi.fn(() => query),
      where: vi.fn(() => query),
      orderBy: vi.fn(() => query),
      limit: vi.fn(() => ({ then: (resolve: (limited: unknown[]) => unknown) => resolve(rows) })),
      then: (resolve: (selected: unknown[]) => unknown) => resolve(rows),
    };
    return query;
  };
  return {
    transaction: async (callback: (tx: unknown) => Promise<unknown>) => callback({}),
    select: vi.fn((selection: Record<string, unknown> = {}) => ({
      from: vi.fn((table: unknown) => buildQuery(selection, table)),
    })),
  };
}

describe("PATCH /api/issues/:id blocked-entry justification gate (SON-3754)", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doUnmock("@paperclipai/shared/telemetry");
    vi.doUnmock("../telemetry.js");
    vi.doUnmock("../services/access.js");
    vi.doUnmock("../services/activity-log.js");
    vi.doUnmock("../services/cross-issue-influence-limit.js");
    vi.doUnmock("../services/agents.js");
    vi.doUnmock("../services/documents.js");
    vi.doUnmock("../services/external-objects.js");
    vi.doUnmock("../services/index.js");
    vi.doUnmock("../services/issues.js");
    vi.doUnmock("../services/work-products.js");
    vi.doUnmock("../routes/issues.js");
    vi.doUnmock("../routes/authz.js");
    vi.doUnmock("../middleware/index.js");
    registerRouteMocks();
    vi.clearAllMocks();
    mockAccessService.canUser.mockReset();
    mockAccessService.decide.mockReset();
    mockAccessService.decide.mockImplementation(async (input: { action: string }) => ({
      allowed: [
        "tasks:assign",
        "issue:comment",
        "issue:read",
        "issue:mutate",
        "company_scope:read",
      ].includes(input.action),
      action: input.action,
      reason: "allow_explicit_grant",
      explanation: "Allowed by test boundary default.",
    }));
    mockIssueService.getById.mockResolvedValue(makeIssue({ status: "in_progress" }));
    mockIssueService.getDependencyReadiness.mockResolvedValue({ unresolvedBlockerCount: 0 });
    mockIssueService.getRelationSummaries.mockResolvedValue({ blockedBy: [], blocking: [] });
    mockIssueService.update.mockImplementation(async (_id: string, data: Record<string, unknown>) => ({
      ...makeIssue({ status: "blocked" }),
      ...(data.externalBlocker ? { externalBlocker: data.externalBlocker } : {}),
    }));
  });

  it("still rejects a reason-less blocked transition with the shared-validator 400", async () => {
    const res = await request(await createApp(ownerActor()))
      .patch(`/api/issues/${issueId}`)
      .send({ status: "blocked" });
    expect(res.status).toBe(400);
    expect(JSON.stringify(res.body)).toContain("blocked status requires either blockedByIssueIds or an externalBlocker");
    expect(mockIssueService.update).not.toHaveBeenCalled();
  });

  it("rejects blockedByIssueIds that resolve only to done issues with the how-to-fix 422", async () => {
    const res = await request(await createApp(ownerActor(), createGateDb()))
      .patch(`/api/issues/${issueId}`)
      .send({ status: "blocked", blockedByIssueIds: ["99999999-9999-4999-8999-999999999999"] });
    expect(res.status).toBe(422);
    expect(res.body.error).toContain("Entering blocked requires a named reason");
    expect(res.body.error).toContain("externalBlocker {owner, note}");
    expect(mockIssueService.update).not.toHaveBeenCalled();
  });

  it("accepts a named externalBlocker as sufficient blocked justification", async () => {
    const res = await request(await createApp(ownerActor(), createGateDb()))
      .patch(`/api/issues/${issueId}`)
      .send({ status: "blocked", externalBlocker: { owner: "ops", note: "waiting on vendor rotation" } });
    expect(res.status, JSON.stringify(res.body)).toBe(200);
    expect(mockIssueService.update).toHaveBeenCalledWith(
      issueId,
      expect.objectContaining({
        status: "blocked",
        externalBlocker: expect.objectContaining({ owner: "ops" }),
      }),
      expect.anything(),
    );
  });

  it("still accepts blocked entry backed by a live unresolved blocker edge", async () => {
    const res = await request(await createApp(ownerActor(), createGateDb({
      blockerRows: [{ id: "88888888-8888-4888-8888-888888888888" }],
    })))
      .patch(`/api/issues/${issueId}`)
      .send({ status: "blocked", blockedByIssueIds: ["88888888-8888-4888-8888-888888888888"] });
    expect(res.status, JSON.stringify(res.body)).toBe(200);
    expect(mockIssueService.update).toHaveBeenCalled();
  });
});
