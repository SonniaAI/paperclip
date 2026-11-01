import { randomUUID } from "node:crypto";
import { and, asc, desc, eq, exists, gte, inArray, lt, ne, isNotNull, isNull, or, sql } from "drizzle-orm";
import type { Db } from "@paperclipai/db";
import {
  activityLog,
  agentWakeupRequests,
  agents,
  companies,
  heartbeatRuns,
  issueComments,
  issues,
  issueThreadInteractions,
  projects,
} from "@paperclipai/db";
import {
  AGENT_DEFAULT_MAX_CONCURRENT_RUNS,
  ISSUE_THREAD_INTERACTION_KINDS,
  isUuidLike,
} from "@paperclipai/shared";
import { isUniqueViolation } from "../db-errors.js";
import { logger } from "../middleware/logger.js";
import { logActivity } from "./activity-log.js";
import { evaluateAgentInvokabilityFromDb } from "./agent-invokability.js";
import { issueService, type IssueDependencyReadiness } from "./issues.js";
import { visibleIssueCondition } from "./issue-visibility.js";

/**
 * Execution reconciler.
 *
 * Paperclip's dispatch pipeline is wake-driven: a run only starts after some
 * actor mints a wake (assignment, comment, timer, dependency resolution). That
 * leaves structural dead zones where eligible work never generates a wake:
 *
 *  - an issue assigned while still in `backlog` never gets an assignment wake
 *    (assignment wakes skip backlog by design);
 *  - an agent whose timer heartbeat is disabled is never woken for assigned
 *    backlog/todo work by anything else;
 *  - unassigned issues have no assignee to wake at all;
 *  - a remote worker that dies silently holds its issue's execution lock
 *    forever, because nothing reaps a "running" run without a local process.
 *
 * This service closes the loop from the system side. Every scheduler tick it
 * reconciles desired execution (dependency-eligible work should be running)
 * with actual execution:
 *
 *   scan non-terminal work -> evaluate dependencies -> route owners ->
 *   promote backlog -> attach verification policy -> dispatch (idempotent) ->
 *   raise program-stall incidents.
 *
 * On top of that, SON-6153 adds the LEVEL-TRIGGERED transition scan
 * (`reconcileTransitionDeadlines`, default OFF): every ~15 min it reads the
 * whole board and enforces transition deadlines — stale in_progress cards get
 * bounded owner re-wakes then one CEO escalation, stalled reviews get a
 * reviewer nudge then a fallback reassignment, approved cards waiting on a
 * release-lane stage get a gated ship dispatch, blocked cards past 24h get an
 * owner disposition receipt, and every reconciler wake must produce a run id
 * within the grace window or it is marked dead and retried via the alternate
 * self-wake path. At most one corrective action per card per tick; receipts
 * are comments on the affected card.
 *
 * Worker-lease recovery is intentionally NOT part of this module on the
 * current schema: the native wake-queue/run-dispatch/active-run-watchdog
 * modules own queued-run and dead-worker recovery.
 *
 * All state changes reuse Paperclip's own primitives: dependency readiness,
 * assignment, `enqueueWakeup`, the issue execution lock, the execution-policy
 * engine, and origin-scoped incident issues. The reconciler never bypasses
 * the checkout CAS: workers still claim their own issues at run start.
 */

export const EXECUTION_RECONCILER_DISPATCH_WAKE_REASON = "execution_reconciler_dispatch";
/** Origin kind for reconciler-raised orchestration incidents. Tracked on the
 *  current source by the literal string plus the
 *  `issues_active_orchestration_incident_uq` partial unique index
 *  (see routes/issues.ts); the older shared-package constant is gone. */
export const ORCHESTRATION_INCIDENT_ORIGIN_KIND = "orchestration_incident";
export const EXECUTION_RECONCILER_LEASE_RETRY_WAKE_REASON = "execution_reconciler_lease_retry";
export const EXECUTION_RECONCILER_VERIFY_WAKE_REASON = "execution_reconciler_verify";
export const EXECUTION_RECONCILER_QUEUED_RETRY_WAKE_REASON = "execution_reconciler_queued_retry";

export const STALL_CLASSES = [
  "unowned",
  "assigned_unrunnable",
  "queued_never_started",
  "stale_running",
  "blocked_dependency",
  "verifier_backlog",
  "missing_acceptance_criteria",
] as const;
export type StallClass = (typeof STALL_CLASSES)[number];
export const WORKER_LEASE_EXPIRED_ERROR_CODE = "worker_lease_expired";
export const BLOCKED_NO_OWNER_REASON = "no_owner";

// --- Level-triggered transition deadlines (SON-6153) -------------------------

export const EXECUTION_RECONCILER_TRANSITION_REAWAKE_REASON = "execution_reconciler_transition_reawake";
export const EXECUTION_RECONCILER_REVIEW_NUDGE_REASON = "execution_reconciler_review_nudge";
export const EXECUTION_RECONCILER_SHIP_DISPATCH_REASON = "execution_reconciler_ship_dispatch";
export const EXECUTION_RECONCILER_LANE_LIVENESS_REASON = "execution_reconciler_lane_liveness";
export const EXECUTION_RECONCILER_BLOCKED_DISPOSITION_REASON = "execution_reconciler_blocked_disposition";
export const EXECUTION_RECONCILER_WAKE_RECEIPT_RETRY_REASON = "execution_reconciler_wake_receipt_retry";

/** Durable attempt ledger for the transition rules: corrective actions are
 *  counted from the activity log, so the rules need no new tables or columns. */
export const TRANSITION_REAWAKE_LEDGER_ACTION = "issue.execution_reconciler_transition_reawake";
export const TRANSITION_REVIEW_NUDGE_LEDGER_ACTION = "issue.execution_reconciler_review_nudge";
export const TRANSITION_REVIEWER_REASSIGN_LEDGER_ACTION = "issue.execution_reconciler_reviewer_reassigned";
export const TRANSITION_SHIP_DISPATCH_LEDGER_ACTION = "issue.execution_reconciler_ship_dispatch";
export const TRANSITION_BLOCKED_DISPOSITION_LEDGER_ACTION = "issue.execution_reconciler_blocked_disposition";
export const TRANSITION_LANE_LIVENESS_LEDGER_ACTION = "issue.execution_reconciler_lane_liveness";
export const TRANSITION_WAKE_DEAD_LEDGER_ACTION = "issue.execution_reconciler_wake_receipt_dead";
export const TRANSITION_ESCALATION_LEDGER_ACTION = "issue.execution_reconciler_transition_escalated";

/** Origin id prefix for transition-deadline escalation incidents, scoped per
 *  card and rule (one CEO escalation per card, ever). */
export const TRANSITION_ESCALATION_ORIGIN_PREFIX = "transition:";

const STAGE_COMPLETE_STATUSES = new Set(["done", "approved", "complete", "completed", "skipped", "cancelled"]);

export function parseExecutionPolicyStages(executionPolicy: unknown): Array<Record<string, unknown>> {
  if (!executionPolicy || typeof executionPolicy !== "object" || Array.isArray(executionPolicy)) return [];
  const stages = (executionPolicy as Record<string, unknown>).stages;
  if (!Array.isArray(stages)) return [];
  return stages.filter(
    (stage): stage is Record<string, unknown> => Boolean(stage) && typeof stage === "object" && !Array.isArray(stage),
  );
}

function participantAgentId(participant: unknown): string | null {
  if (!participant || typeof participant !== "object" || Array.isArray(participant)) return null;
  const record = participant as Record<string, unknown>;
  if (typeof record.agentId === "string" && record.agentId) return record.agentId;
  if ((record.type === "agent" || typeof record.type !== "string") && typeof record.id === "string" && record.id) {
    return record.id;
  }
  return null;
}

/** The board advances execution-policy stages sequentially: a stage is only
 *  reachable once earlier stages completed. The CURRENT gate is therefore the
 *  last stage that is not complete; its first agent participant is the named
 *  reviewer / lane the card is waiting on. Tolerates unknown shapes. */
export function parsePendingReviewParticipant(executionPolicy: unknown): {
  stageIndex: number;
  stageType: string | null;
  stageStatus: string | null;
  reviewerAgentId: string | null;
} | null {
  const stages = parseExecutionPolicyStages(executionPolicy);
  for (let index = stages.length - 1; index >= 0; index -= 1) {
    const stage = stages[index];
    const status = typeof stage.status === "string" ? stage.status.toLowerCase() : null;
    if (status && STAGE_COMPLETE_STATUSES.has(status)) continue;
    const participants = Array.isArray(stage.participants) ? stage.participants : [];
    const reviewerAgentId = participants.map(participantAgentId).find((id): id is string => Boolean(id)) ?? null;
    return {
      stageIndex: index,
      stageType: typeof stage.type === "string" ? stage.type : null,
      stageStatus: status,
      reviewerAgentId,
    };
  }
  return null;
}

/** Swap the named reviewer in the current pending stage for a fallback
 *  reviewer. Returns null when the policy does not match the expectation, so
 *  callers never guess at unknown shapes. */
export function reassignReviewParticipant(
  executionPolicy: unknown,
  fromAgentId: string,
  toAgentId: string,
): Record<string, unknown> | null {
  const pending = parsePendingReviewParticipant(executionPolicy);
  if (!pending || pending.reviewerAgentId !== fromAgentId) return null;
  const stages = parseExecutionPolicyStages(executionPolicy);
  const policy = executionPolicy as Record<string, unknown>;
  const nextStages = stages.map((stage, index) => {
    if (index !== pending.stageIndex || !Array.isArray(stage.participants)) return stage;
    return {
      ...stage,
      participants: stage.participants.map((participant) => {
        if (participantAgentId(participant) !== fromAgentId) return participant;
        if (!participant || typeof participant !== "object" || Array.isArray(participant)) return participant;
        const record = participant as Record<string, unknown>;
        return {
          ...record,
          ...(typeof record.id === "string" ? { id: toAgentId } : {}),
          agentId: toAgentId,
        };
      }),
    };
  });
  return { ...policy, stages: nextStages };
}

/** A blocked issue is dependency-resumable only when it has a real incoming
 * blocker edge and every recorded blocker has completed. With no edges,
 * dependency readiness is vacuously true; that alone is not authority to
 * reopen manually blocked work. */
function hasResolvedDependencyBlockers(readiness: IssueDependencyReadiness | undefined) {
  return Boolean(readiness?.isDependencyReady && readiness.blockerIssueIds.length > 0);
}

function pendingAgentParticipantId(raw: unknown): string | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const state = raw as {
    status?: string;
    currentParticipant?: { type?: string; agentId?: string | null } | null;
  };
  return state.status === "pending" && state.currentParticipant?.type === "agent"
    ? state.currentParticipant.agentId ?? null
    : null;
}

export type ExecutionReconcilerSettings = {
  enabled: boolean;
  dryRun: boolean;
  stallThresholdMs: number;
  stallNoticeDedupWindowMs: number;
  workerLeaseTtlMs: number;
  queuedRunTimeoutMs: number;
  blockedCooldownMs: number;
  maxDispatchesPerTick: number;
  defaultWorkerAgentId: string | null;
  verifierAgentId: string | null;
  disabledCompanies: string[];
  /** Level-triggered transition enforcement (SON-6153). Default OFF: the
   *  transition scans only run when the deployment explicitly enables them,
   *  independent of the legacy reconciler scans above. */
  levelTriggeredEnabled: boolean;
  levelTriggeredIntervalMs: number;
  inProgressStallMs: number;
  maxReawakeAttempts: number;
  inReviewVerdictMs: number;
  shipDispatchDelayMs: number;
  blockedDispositionMs: number;
  wakeReceiptGraceMs: number;
  fallbackReviewerAgentId: string | null;
  releaseLaneAgentId: string | null;
  maxTransitionActionsPerTick: number;
};

export function resolveExecutionReconcilerSettings(): ExecutionReconcilerSettings {
  const env = process.env;
  return {
    enabled: env.EXECUTION_RECONCILER_ENABLED !== "false",
    dryRun: env.EXECUTION_RECONCILER_DRY_RUN === "true",
    stallThresholdMs: Math.max(
      60_000,
      Number(env.EXECUTION_RECONCILER_STALL_THRESHOLD_MS) || 30 * 60 * 1000,
    ),
    stallNoticeDedupWindowMs: Math.max(
      0,
      Number(env.EXECUTION_RECONCILER_STALL_NOTICE_DEDUP_WINDOW_MS) || 24 * 60 * 60 * 1000,
    ),
    workerLeaseTtlMs: Math.max(
      60_000,
      Number(env.EXECUTION_WORKER_LEASE_TTL_MS) || 15 * 60 * 1000,
    ),
    queuedRunTimeoutMs: Math.max(
      300_000,
      Number(env.EXECUTION_QUEUED_RUN_TIMEOUT_MS) || 30 * 60 * 1000,
    ),
    blockedCooldownMs: Math.max(
      60_000,
      Number(env.EXECUTION_RECONCILER_BLOCKED_COOLDOWN_MS) || 10 * 60 * 1000,
    ),
    maxDispatchesPerTick: Math.max(
      1,
      Number(env.EXECUTION_RECONCILER_MAX_DISPATCHES_PER_TICK) || 10,
    ),
    defaultWorkerAgentId: env.EXECUTION_RECONCILER_DEFAULT_WORKER_AGENT_ID?.trim() || null,
    verifierAgentId: env.EXECUTION_RECONCILER_VERIFIER_AGENT_ID?.trim() || null,
    disabledCompanies: (env.EXECUTION_RECONCILER_DISABLED_COMPANIES ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
    levelTriggeredEnabled: env.EXECUTION_RECONCILER_LEVEL_TRIGGERED_ENABLED === "true",
    levelTriggeredIntervalMs: Math.max(
      60_000,
      Number(env.EXECUTION_RECONCILER_LEVEL_TRIGGERED_INTERVAL_MS) || 15 * 60 * 1000,
    ),
    inProgressStallMs: Math.max(
      300_000,
      Number(env.EXECUTION_RECONCILER_IN_PROGRESS_STALL_MS) || 2 * 60 * 60 * 1000,
    ),
    maxReawakeAttempts: Math.max(1, Number(env.EXECUTION_RECONCILER_MAX_REAWAKE_ATTEMPTS ?? 2)),
    inReviewVerdictMs: Math.max(
      300_000,
      Number(env.EXECUTION_RECONCILER_IN_REVIEW_VERDICT_MS) || 4 * 60 * 60 * 1000,
    ),
    shipDispatchDelayMs: Math.max(
      300_000,
      Number(env.EXECUTION_RECONCILER_SHIP_DISPATCH_DELAY_MS) || 2 * 60 * 60 * 1000,
    ),
    blockedDispositionMs: Math.max(
      300_000,
      Number(env.EXECUTION_RECONCILER_BLOCKED_DISPOSITION_MS) || 24 * 60 * 60 * 1000,
    ),
    wakeReceiptGraceMs: Math.max(
      60_000,
      Number(env.EXECUTION_RECONCILER_WAKE_RECEIPT_GRACE_MS) || 30 * 60 * 1000,
    ),
    fallbackReviewerAgentId: env.EXECUTION_RECONCILER_FALLBACK_REVIEWER_AGENT_ID?.trim() || null,
    releaseLaneAgentId: env.EXECUTION_RECONCILER_RELEASE_LANE_AGENT_ID?.trim() || null,
    maxTransitionActionsPerTick: Math.max(
      1,
      Number(env.EXECUTION_RECONCILER_MAX_TRANSITION_ACTIONS_PER_TICK) || 20,
    ),
  };
}

// Origins that exist only because some recovery path created them. The
// reconciler must not "help" by dispatching its own (or other systems')
// escalation artifacts as if they were ordinary product work.
const NON_DISPATCHABLE_ORIGIN_KINDS = new Set([
  "stale_active_run_evaluation",
  "harness_liveness_escalation",
  "stranded_issue_recovery",
  "task_watchdog",
  "task_watchdog_product_bug",
  ORCHESTRATION_INCIDENT_ORIGIN_KIND,
]);

const ADMINISTRATIVE_RECORD_LABELS = new Set([
  "ceodecisionrecord",
  "ceoreceiptcorrection",
  "batchcarryover",
  "decisionshealth",
]);

function normalizeAdministrativeRecordLabel(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function isAdministrativeRecordIssue(title: string, description: string | null) {
  const bracketedTitle = title.trim().match(/^\[([^\]]+)\]/)?.[1];
  if (
    bracketedTitle &&
    ADMINISTRATIVE_RECORD_LABELS.has(normalizeAdministrativeRecordLabel(bracketedTitle))
  ) {
    return true;
  }
  // The decision-queue health record is conventionally a plain title, not a
  // bracketed label. Keep this anchored to the explicit record name so prose
  // about decisions elsewhere cannot suppress normal deliverable review.
  if (/^\s*decisions[\s_-]*health(?:\b|\s*[:—-])/i.test(title)) return true;

  // Some imported/manual records identify their class in a labeled header
  // instead of the title. Do not infer this from generic product/CEO language.
  const descriptionClass = description?.match(
    /^\s*(?:record|task|work)\s*(?:class|kind|type)\s*:\s*([^\r\n]+)\s*$/im,
  )?.[1];
  return Boolean(
    descriptionClass &&
    ADMINISTRATIVE_RECORD_LABELS.has(normalizeAdministrativeRecordLabel(descriptionClass)),
  );
}

/** Pure policy for automatic verification. Only actionable deliverables enter
 * the verification flow; parked work and Paperclip's own administrative
 * records must not recursively create review-lane work. Existing policies are
 * deliberately left untouched. */
export function isAutomaticVerificationPolicyEligible(issue: {
  status: string;
  originKind: string | null;
  title: string;
  description: string | null;
  executionPolicy: unknown;
}) {
  if (issue.executionPolicy != null) return false;
  if (issue.status !== "todo" && issue.status !== "in_progress") return false;
  if (
    issue.originKind === "issue_productivity_review" ||
    NON_DISPATCHABLE_ORIGIN_KINDS.has(issue.originKind ?? "manual")
  ) {
    return false;
  }
  return !isAdministrativeRecordIssue(issue.title, issue.description);
}

export type ExecutionReconcileRouteDecision =
  | { kind: "assignee" }
  | { kind: "routed"; agentId: string; basis: "project_lead" | "creator" | "default_worker" }
  | { kind: "no_owner" }
  | { kind: "assignee_unrunnable"; assigneeId: string };

export type ExecutionReconcileAction =
  | { kind: "dispatched"; issueId: string; agentId: string; wakeRunId: string | null }
  | { kind: "already_covered"; issueId: string; reason: "active_run" | "queued_wake" }
  | { kind: "promoted_to_todo"; issueId: string }
  | { kind: "routed"; issueId: string; agentId: string; basis: string }
  | { kind: "blocked_no_owner"; issueId: string }
  | { kind: "skipped"; issueId: string; reason: string };

export type ExecutionReconcileResult = {
  companiesScanned: number;
  issuesScanned: number;
  eligible: number;
  notReady: number;
  dispatched: number;
  promoted: number;
  routed: number;
  blockedNoOwner: number;
  skipped: number;
  verificationPoliciesAttached: number;
  actions: ExecutionReconcileAction[];
  dryRun: boolean;
};

export type WorkerLeaseRecoveryResult = {
  scanned: number;
  expired: number;
  retried: number;
  escalated: number;
  skipped: number;
  issueIds: string[];
  dispatchedQueuedRetry: number;
};

export type ProgramStallScanResult = {
  programsScanned: number;
  stalled: number;
  incidentsCreated: number;
  incidentsEscalated: number;
  incidentsExisting: number;
  incidentsSuppressed: number;
};

const STALL_FINGERPRINT_PREFIX = "stall:";

// Stable stall signature: the sorted stuck-classification counts, matching the
// "Stuck classification:" line rendered into every notice body.
export type TransitionDeadlineScanResult = {
  companiesScanned: number;
  lanesReAdmitted: number;
  inProgressStalled: number;
  reawakeOwner: number;
  reviewStalled: number;
  reviewerNudged: number;
  reviewerReassigned: number;
  shipDispatchQueued: number;
  blockedDisposition: number;
  wakeReceiptsDead: number;
  wakeReceiptsRetried: number;
  escalatedToCeo: number;
  skippedLaneUnhealthy: number;
  skippedOneActionPerCard: number;
};

function stallSignature(classification: Partial<Record<StallClass, number>>): string {
  const lines = Object.entries(classification)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([cls, n]) => `${cls}=${n}`)
    .join(", ");
  return lines || "none";
}

// Newer notices carry the signature in originFingerprint; legacy notices
// (fingerprint "default") carry it only in the description body. Recovering it
// from the description keeps dedup effective against existing cancelled
// batches created before the fingerprint existed (SON-4797).
function stallNoticeSignatureOf(incident: { originFingerprint: string; description: string | null }): string {
  if (incident.originFingerprint.startsWith(STALL_FINGERPRINT_PREFIX)) {
    return incident.originFingerprint.slice(STALL_FINGERPRINT_PREFIX.length);
  }
  const match = /Stuck classification: (.*?)\./s.exec(incident.description ?? "");
  return match ? match[1].trim() : incident.originFingerprint;
}

export type ExecutionProgramSummary = {
  projectId: string | null;
  epicIssueId: string | null;
  eligibleIssues: number;
  runningIssues: number;
  verifyingIssues: number;
  blockedIssues: number;
  unownedIssues: number;
  staleRuns: number;
  lastMeaningfulTransitionAt: Date | null;
  stalled: boolean;
  /** Classified stall counters: not all stuck work looks like backlog. */
  classification: Partial<Record<StallClass, number>>;
  progressing: boolean;
};

export function executionReconcilerService(
  db: Db,
  deps: {
    enqueueWakeup: (
      agentId: string,
      opts: Record<string, unknown>,
    ) => Promise<{ id: string } | null>;
    getSettings?: () => ExecutionReconcilerSettings;
    /** When provided, runs the server still observes locally (in-memory child
     *  processes / active executions) are never lease-expired: only genuinely
     *  unreachable remote workers lose their lease. */
    isRunLocallyAlive?: (runId: string) => boolean;
    /** A queued run is valid work while its current owner is at concurrency
     *  capacity or inside a configured dispatch blackout. Such rows must not
     *  be failed and recreated merely because the FIFO wait exceeded its age
     *  threshold. */
    getQueuedRunDispatchBlockReason?: (
      agentId: string,
    ) => Promise<"concurrency_limit" | "dispatch_blackout" | null>;
    /** Alternate wake path for dead-wake retries (SON-6153 wake receipts):
     *  the same internal service the interactive `POST /agents/:id/wakeup`
     *  route uses, creating the run directly instead of queueing another
     *  agent_wakeup_requests row the same loop would have to deliver. */
    wakeAgentDirectly?: (
      agentId: string,
      opts: Record<string, unknown>,
    ) => Promise<{ id: string | null } | null>;
  },
) {
  const issuesSvc = issueService(db);
  // Recovery rotates over owners instead of letting one agent's large FIFO
  // queue occupy every bounded scan page. A missing page wraps back to the
  // beginning on the next call.
  let staleQueuedAgentCursor: string | null = null;

  function settings(): ExecutionReconcilerSettings {
    return deps.getSettings?.() ?? resolveExecutionReconcilerSettings();
  }

  async function getAgent(agentId: string) {
    return db
      .select()
      .from(agents)
      .where(eq(agents.id, agentId))
      .then((rows) => rows[0] ?? null);
  }

  async function isAgentInvokable(agent: typeof agents.$inferSelect | null | undefined) {
    if (!agent) return false;
    return (await evaluateAgentInvokabilityFromDb(db, agent)).invokable;
  }

  function reconcilerDisabledForCompany(companyId: string) {
    return settings().disabledCompanies.includes(companyId);
  }

  function maxConcurrentRunsForAgent(agent: typeof agents.$inferSelect) {
    const runtimeConfig =
      agent.runtimeConfig && typeof agent.runtimeConfig === "object" && !Array.isArray(agent.runtimeConfig)
        ? (agent.runtimeConfig as Record<string, unknown>)
        : {};
    const heartbeat =
      runtimeConfig.heartbeat && typeof runtimeConfig.heartbeat === "object" && !Array.isArray(runtimeConfig.heartbeat)
        ? (runtimeConfig.heartbeat as Record<string, unknown>)
        : {};
    const raw = heartbeat.maxConcurrentRuns;
    if (typeof raw !== "number" || !Number.isFinite(raw)) return AGENT_DEFAULT_MAX_CONCURRENT_RUNS;
    return Math.max(1, Math.min(50, Math.floor(raw)));
  }

  async function getNewDispatchBlockReason(
    companyId: string,
    agentId: string,
    reservationsCreatedThisPass: Map<string, number>,
    reservationsPresentAtFirstCheck: Map<string, number>,
  ) {
    const agent = await getAgent(agentId);
    if (!agent || agent.companyId !== companyId) return "agent_unavailable";

    // Keep existing policy (including the configured blackout) authoritative
    // before reserving any new automatic work. Existing queued work is never
    // cancelled here; it remains available to start when capacity returns.
    const policyBlock = await deps.getQueuedRunDispatchBlockReason?.(agentId);
    if (policyBlock) return policyBlock;

    const activeRows = await db
      .select({ count: sql<number>`count(*)` })
      .from(heartbeatRuns)
      .where(
        and(
          eq(heartbeatRuns.companyId, companyId),
          eq(heartbeatRuns.agentId, agentId),
          inArray(heartbeatRuns.status, ["queued", "running"]),
        ),
      );
    const persistedReservations = Number(activeRows[0]?.count ?? 0);
    const firstObservedReservations = reservationsPresentAtFirstCheck.get(agentId);
    if (firstObservedReservations === undefined) {
      reservationsPresentAtFirstCheck.set(agentId, persistedReservations);
    }
    const baselineReservations = firstObservedReservations ?? persistedReservations;
    const inMemoryReservations = reservationsCreatedThisPass.get(agentId) ?? 0;
    // Some callers/tests provide an enqueue stub that does not persist run
    // rows. Count those pass-local reservations on top of the pre-existing
    // baseline, while avoiding double-counting them when the DB has the rows.
    const reservedSlots = Math.max(
      persistedReservations,
      baselineReservations + inMemoryReservations,
    );
    if (reservedSlots >= maxConcurrentRunsForAgent(agent)) return "concurrency_limit";
    return null;
  }

  async function hasQueuedIssueWake(companyId: string, issueId: string, agentId?: string | null) {
    // Only a queued wake that still owns a live (queued/running) run actually
    // covers the issue. Orphaned queued wake rows — the run was cancelled,
    // timed out, or never created — must not park dispatch forever.
    const conditions = [
      eq(agentWakeupRequests.companyId, companyId),
      eq(agentWakeupRequests.status, "queued"),
      sql`${agentWakeupRequests.payload} ->> 'issueId' = ${issueId}`,
      exists(
        db
          .select({ one: sql`1` })
          .from(heartbeatRuns)
          .where(
            and(
              eq(heartbeatRuns.wakeupRequestId, agentWakeupRequests.id),
              inArray(heartbeatRuns.status, ["queued", "running"]),
            ),
          ),
      ),
    ];
    if (agentId) conditions.push(eq(agentWakeupRequests.agentId, agentId));
    const rows = await db
      .select({ id: agentWakeupRequests.id })
      .from(agentWakeupRequests)
      .where(and(...conditions))
      .limit(1);
    return rows.length > 0;
  }

  /**
   * Latest run bound to an issue (by contextSnapshot.issueId), newest first.
   * Used to mirror the stranded-recovery semantics: a succeeded run that
   * left the issue in `todo` means the agent already looked at this work;
   * re-dispatching every tick would hot-loop instead of progressing.
   */
  async function getLatestIssueRunRow(companyId: string, issueId: string) {
    const rows = await db
      .select({ id: heartbeatRuns.id, status: heartbeatRuns.status })
      .from(heartbeatRuns)
      .where(
        and(
          eq(heartbeatRuns.companyId, companyId),
          sql`${heartbeatRuns.contextSnapshot} ->> 'issueId' = ${issueId}`,
        ),
      )
      .orderBy(desc(heartbeatRuns.createdAt))
      .limit(1);
    return rows[0] ?? null;
  }

  async function hasLiveIssueRun(companyId: string, issueId: string, agentId?: string | null) {
    const rows = await db
      .select({ id: heartbeatRuns.id })
      .from(heartbeatRuns)
      .where(
        and(
          eq(heartbeatRuns.companyId, companyId),
          inArray(heartbeatRuns.status, ["queued", "running"]),
          sql`${heartbeatRuns.contextSnapshot} ->> 'issueId' = ${issueId}`,
          agentId ? eq(heartbeatRuns.agentId, agentId) : undefined,
        ),
      )
      .limit(1);
    return rows.length > 0;
  }

  async function resolveOwner(
    issue: typeof issues.$inferSelect,
    projectLeadAgentId: string | null,
  ): Promise<{ agentId: string | null; decision: ExecutionReconcileRouteDecision }> {
    if (issue.assigneeAgentId) {
      const assignee = await getAgent(issue.assigneeAgentId);
      const invokable =
        assignee && assignee.companyId === issue.companyId && (await isAgentInvokable(assignee));
      if (invokable) return { agentId: issue.assigneeAgentId, decision: { kind: "assignee" } };
      return {
        agentId: null,
        decision: { kind: "assignee_unrunnable", assigneeId: issue.assigneeAgentId },
      };
    }

    const tryCandidate = async (
      agentId: string | null | undefined,
      basis: "project_lead" | "creator" | "default_worker",
    ) => {
      if (!agentId) return null;
      const candidate = await getAgent(agentId);
      if (!candidate || candidate.companyId !== issue.companyId) return null;
      if (!(await isAgentInvokable(candidate))) return null;
      return { agentId, basis };
    };

    const conf = settings();
    const routed =
      (await tryCandidate(projectLeadAgentId, "project_lead")) ??
      (await tryCandidate(issue.createdByAgentId, "creator")) ??
      (await tryCandidate(conf.defaultWorkerAgentId, "default_worker"));

    if (routed) {
      return { agentId: routed.agentId, decision: { kind: "routed", agentId: routed.agentId, basis: routed.basis } };
    }
    return { agentId: null, decision: { kind: "no_owner" } };
  }

  /**
   * Assigned-but-unrunnable: reroute to the project lead when they are a
   * DIFFERENT, invokable agent. Returns the new owner or null.
   */
  async function rerouteFromUnrunnable(
    issue: Pick<typeof issues.$inferSelect, "id" | "companyId" | "status">,
    fromAgentId: string,
    projectLeadAgentId: string | null,
  ): Promise<string | null> {
    if (!projectLeadAgentId || projectLeadAgentId === fromAgentId) return null;
    const lead = await getAgent(projectLeadAgentId);
    if (!lead || lead.companyId !== issue.companyId) return null;
    if (!(await isAgentInvokable(lead))) return null;
    const updated = await db
      .update(issues)
      .set({
        assigneeAgentId: lead.id,
        assigneeUserId: null,
        status: issue.status === "backlog" ? "todo" : issue.status,
        updatedAt: new Date(),
      })
      .where(
        and(
          eq(issues.id, issue.id),
          eq(issues.companyId, issue.companyId),
          eq(issues.assigneeAgentId, fromAgentId),
        ),
      )
      .returning({ id: issues.id });
    if (updated.length === 0) return null;
    await addSystemComment(
      issue.companyId,
      issue.id,
      `Execution reconciler: the assigned worker is not runnable; automatically rerouted to the project lead for dispatch.`,
    );
    return lead.id;
  }

  /** Park an issue whose only owner path is an unrunnable agent, visibly. */
  async function markBlockedUnrunnable(
    issue: typeof issues.$inferSelect,
    assigneeId: string,
  ): Promise<boolean> {
    const assignee = await getAgent(assigneeId);
    const reason = `Assigned worker ${assignee?.name ?? assigneeId} is not runnable (paused, terminated, over budget, or broken reporting chain). Resume or replace the worker, or reassign this issue.`;
    const now = new Date();
    const updated = await db
      .update(issues)
      .set({
        status: "blocked",
        blockedTransitionAt: now,
        unblockDescriptor: { owner: "board", action: reason },
        updatedAt: now,
      })
      .where(
        and(
          eq(issues.id, issue.id),
          eq(issues.companyId, issue.companyId),
          inArray(issues.status, ["backlog", "todo", "in_progress"]),
        ),
      )
      .returning({ id: issues.id });
    if (updated.length === 0) return false;
    await addSystemComment(
      issue.companyId,
      issue.id,
      `Execution reconciler: **BLOCKED_WORKER_UNRUNNABLE**. ${reason}`,
    );
    await logActivity(db, {
      companyId: issue.companyId,
      actorType: "system",
      actorId: "system",
      action: "issue.execution_reconciler_blocked_unrunnable",
      entityType: "issue",
      entityId: issue.id,
      issueId: issue.id,
      details: { issueId: issue.id, assigneeId },
    });
    return true;
  }

  async function addSystemComment(companyId: string, issueId: string, body: string) {
    await db.insert(issueComments).values({
      companyId,
      issueId,
      body,
      authorType: "system",
    });
  }

  async function markBlockedNoOwner(issue: typeof issues.$inferSelect): Promise<boolean> {
    const now = new Date();
    const updated = await db
      .update(issues)
      .set({
        status: "blocked",
        blockedTransitionAt: now,
        unblockDescriptor: {
          owner: "board",
          action:
            "Assign an owner so the execution reconciler can dispatch this dependency-eligible work.",
        },
        updatedAt: now,
      })
      .where(
        and(
          eq(issues.id, issue.id),
          eq(issues.companyId, issue.companyId),
          inArray(issues.status, ["backlog", "todo"]),
          isNull(issues.assigneeAgentId),
        ),
      )
      .returning({ id: issues.id });
    if (updated.length === 0) return false;
    await addSystemComment(
      issue.companyId,
      issue.id,
      "Execution reconciler: this issue is dependency-eligible but has no routable owner " +
        "(no assignee, project lead, creator agent, or configured default worker). " +
        "Marked `blocked` as **BLOCKED_NO_OWNER** instead of leaving it looking like ordinary backlog work.",
    );
    await logActivity(db, {
      companyId: issue.companyId,
      actorType: "system",
      actorId: "system",
      action: "issue.execution_reconciler_blocked_no_owner",
      entityType: "issue",
      entityId: issue.id,
      issueId: issue.id,
      details: { issueId: issue.id, reason: BLOCKED_NO_OWNER_REASON },
    });
    return true;
  }

  function extractAcceptanceCriteria(description: string | null): string | null {
    if (!description) return null;
    const match = description.match(/acceptance criteri[aa]\s*:?\n([\s\S]*?)(?:\n#|\n##|$)/i);
    return match && match[1].trim().length > 0 ? match[1].trim() : null;
  }

  function buildVerificationPolicy(verifierAgentId: string, _criteria: string | null) {
    // Schema contract: stage ids are optional GUIDs (a non-GUID string makes
    // every later PATCH of the issue fail validation) and participants must
    // carry type:"agent". The verifier reads the issue's acceptance criteria
    // from its description.
    return {
      mode: "normal",
      commentRequired: true,
      stages: [
        {
          type: "review",
          approvalsNeeded: 1,
          participants: [{ type: "agent", id: verifierAgentId, agentId: verifierAgentId }],
        },
      ],
    };
  }


  /**
   * Attach an independent verification stage (a different worker) to owned
   * non-terminal work that has no execution policy yet. Runs as its own sweep
   * so it covers issues dispatched by ANY path — assignment wakes, dependency
   * wakes, routines, board checkouts — not only this reconciler's dispatches.
   * The update is guarded on executionPolicy being null, so it is idempotent.
   */
  async function attachVerificationPolicy(
    companyId: string,
    issue: Pick<
      typeof issues.$inferSelect,
      "id" | "status" | "title" | "executionPolicy" | "originKind" | "description"
    >,
    ownerAgentId: string,
    conf: ExecutionReconcilerSettings,
    now: Date,
    result: ExecutionReconcileResult,
  ) {
    if (conf.dryRun || !conf.verifierAgentId) return;
    if (conf.verifierAgentId === ownerAgentId) return;
    if (!isAutomaticVerificationPolicyEligible(issue)) return;
    const verifier = await getAgent(conf.verifierAgentId);
    if (!verifier || verifier.companyId !== companyId) return;
    if (!(await isAgentInvokable(verifier))) return;
    const updated = await db
      .update(issues)
      .set({
        executionPolicy: buildVerificationPolicy(verifier.id, extractAcceptanceCriteria(issue.description ?? null)),
        reviewPolicy: "not_creator",
        updatedAt: now,
      })
      .where(
        and(
          eq(issues.id, issue.id),
          eq(issues.companyId, companyId),
          isNull(issues.executionPolicy),
        ),
      )
      .returning({ id: issues.id });
    if (updated.length > 0) result.verificationPoliciesAttached += 1;
  }

  /**
   * Main reconciliation pass. Idempotent by construction: every mutation is a
   * guarded update (status/owner conditions in the WHERE clause) or an
   * enqueueWakeup guarded by the queued-wake/live-run checks, and the pass
   * itself runs serially inside the scheduler's recovery chain. Running it
   * twice must produce zero additional wakes.
   */
  async function reconcileExecutionState(opts?: {
    companyId?: string;
    now?: Date;
  }): Promise<ExecutionReconcileResult> {
    const now = opts?.now ?? new Date();
    const conf = settings();
    const result: ExecutionReconcileResult = {
      companiesScanned: 0,
      issuesScanned: 0,
      eligible: 0,
      notReady: 0,
      dispatched: 0,
      promoted: 0,
      routed: 0,
      blockedNoOwner: 0,
      skipped: 0,
      verificationPoliciesAttached: 0,
      actions: [],
      dryRun: conf.dryRun,
    };
    if (!conf.enabled) return result;

    const companyRows = await db
      .select({ id: companies.id })
      .from(companies)
      .where(
        and(
          opts?.companyId ? eq(companies.id, opts.companyId) : undefined,
          isNull(companies.pausedAt),
        ),
      );

    const reservationsCreatedThisPass = new Map<string, number>();
    const reservationsPresentAtFirstCheck = new Map<string, number>();

    for (const company of companyRows) {
      if (reconcilerDisabledForCompany(company.id)) continue;
      result.companiesScanned += 1;

      const candidates = await db
        .select()
        .from(issues)
        .where(
          and(
            eq(issues.companyId, company.id),
            inArray(issues.status, ["backlog", "todo", "blocked", "in_progress"]),
            isNull(issues.assigneeUserId),
            visibleIssueCondition(),
          ),
        )
        .orderBy(issues.createdAt);

      const dispatchable = candidates.filter(
        (issue) => !NON_DISPATCHABLE_ORIGIN_KINDS.has(issue.originKind ?? "manual"),
      );
      result.issuesScanned += dispatchable.length;
      if (dispatchable.length === 0) continue;

      const readinessMap: Map<string, IssueDependencyReadiness> =
        await issuesSvc.listDependencyReadiness(
          company.id,
          dispatchable.map((issue) => issue.id),
        );

      const projectIds = [
        ...new Set(
          dispatchable
            .map((issue) => issue.projectId)
            .filter((id): id is string => Boolean(id)),
        ),
      ];
      const projectRows = projectIds.length
        ? await db
            .select({ id: projects.id, leadAgentId: projects.leadAgentId })
            .from(projects)
            .where(inArray(projects.id, projectIds))
        : [];
      const projectLeadByProjectId = new Map(
        projectRows.map((row) => [row.id, row.leadAgentId]),
      );

      // Resumable blocked work goes FIRST: dependents whose blockers just
      // closed and pending agent-review participants must not starve behind
      // the oldest-first backlog within the dispatch budget.
      const orderedDispatchable = [
        ...dispatchable.filter(
          (issue) =>
            issue.status === "blocked" &&
            !issue.unblockDescriptor &&
            (hasResolvedDependencyBlockers(readinessMap.get(issue.id)) ||
              Boolean(pendingAgentParticipantId(issue.executionState))),
        ),
        ...dispatchable.filter(
          (issue) =>
            issue.status !== "blocked" ||
            Boolean(issue.unblockDescriptor) ||
            (!hasResolvedDependencyBlockers(readinessMap.get(issue.id)) &&
              !pendingAgentParticipantId(issue.executionState)),
        ),
      ];

    for (const issue of orderedDispatchable) {
        const readiness = readinessMap.get(issue.id);
        if (!readiness || !readiness.isDependencyReady) {
          result.notReady += 1;
          continue;
        }

        // Respect deliberate blocks: an explicit unblock descriptor means a
        // human or a recovery path recorded WHY this is blocked (including
        // this reconciler's own BLOCKED_NO_OWNER and upstream stranded-issue
        // escalations). Dispatching over it would fight the escalation system
        // and hot-loop.
        if (issue.unblockDescriptor) {
          result.eligible += 1;
          result.skipped += 1;
          result.actions.push({ kind: "skipped", issueId: issue.id, reason: "explicit_unblock_descriptor" });
          continue;
        }

        // `isDependencyReady` is also true when there are no blocker edges.
        // A blocked row with no incoming dependency is a manual/interaction
        // hold, not a dependency that the reconciler can declare resolved.
        // Targeted interaction wakes are handled by their verified wake path;
        // do not mint an ordinary model run for broad or unaddressed requests.
        const blockedParticipantAgentId = pendingAgentParticipantId(issue.executionState);
        if (
          issue.status === "blocked" &&
          !hasResolvedDependencyBlockers(readiness) &&
          !blockedParticipantAgentId
        ) {
          result.skipped += 1;
          result.actions.push({
            kind: "skipped",
            issueId: issue.id,
            reason: "blocked_without_resolved_dependencies",
          });
          continue;
        }

        result.eligible += 1;

        // Respect fresh escalations: a recent blocked transition is the
        // stranded-recovery path parking undecidable work; give it the
        // cooldown window before treating stale blocked+ready work as
        // forgotten and re-dispatching it.
        if (
          issue.status === "blocked" &&
          issue.unblockDescriptor &&
          issue.blockedTransitionAt &&
          now.getTime() - issue.blockedTransitionAt.getTime() < conf.blockedCooldownMs
        ) {
          result.skipped += 1;
          result.actions.push({ kind: "skipped", issueId: issue.id, reason: "blocked_cooldown" });
          continue;
        }

        // Never dispatch work that already has a live execution path.
        if (await hasLiveIssueRun(company.id, issue.id)) {
          result.skipped += 1;
          result.actions.push({ kind: "already_covered", issueId: issue.id, reason: "active_run" });
          continue;
        }

        // Zombie checkout: the issue claims to be running but nothing is
        // executing it (dead worker, reaped run, released lock). A dead
        // worker must never leave an issue RUNNING indefinitely. Only
        // re-dispatch after the issue has been quiet for a full worker-lease
        // period; a succeeded last run stays with the stranded sweep.
        if (issue.status === "in_progress") {
          if (now.getTime() - issue.updatedAt.getTime() < conf.workerLeaseTtlMs) {
            result.skipped += 1;
            result.actions.push({ kind: "skipped", issueId: issue.id, reason: "recently_touched" });
            continue;
          }
          const latestRun = await getLatestIssueRunRow(company.id, issue.id);
          if (latestRun?.status === "succeeded") {
            result.skipped += 1;
            result.actions.push({ kind: "skipped", issueId: issue.id, reason: "succeeded_run_no_disposition" });
            continue;
          }
          if (!conf.dryRun) {
            const updated = await db
              .update(issues)
              .set({ status: "todo", updatedAt: now })
              .where(
                and(
                  eq(issues.id, issue.id),
                  eq(issues.companyId, company.id),
                  eq(issues.status, "in_progress"),
                ),
              )
              .returning({ id: issues.id });
            if (updated.length > 0) {
              result.promoted += 1;
              result.actions.push({ kind: "promoted_to_todo", issueId: issue.id });
              await addSystemComment(
                company.id,
                issue.id,
                "Execution reconciler: this issue was `in_progress` with no live worker run " +
                  "(zombie checkout from a dead or reaped worker). Reset to `todo` for re-dispatch.",
              );
            }
          }
        }

        // A succeeded run that left the issue in `todo` means the assignee
        // already processed this work without a disposition; the stranded
        // sweep owns that case. Re-dispatching here would hot-loop.
        if (issue.status === "todo") {
          const latestRun = await getLatestIssueRunRow(company.id, issue.id);
          if (latestRun?.status === "succeeded") {
            result.skipped += 1;
            result.actions.push({
              kind: "skipped",
              issueId: issue.id,
              reason: "succeeded_run_no_disposition",
            });
            continue;
          }
        }

        let owner = await resolveOwner(
          issue,
          issue.projectId ? projectLeadByProjectId.get(issue.projectId) ?? null : null,
        );

        // A pending agent participant is the workflow's current owner even
        // when the issue is still marked blocked and has no dependency edge.
        // This covers agent-review/continuation stages without making an
        // unaddressed human interaction or unexplained manual block runnable.
        if (issue.status === "blocked" && blockedParticipantAgentId) {
          const participant = await getAgent(blockedParticipantAgentId);
          if (
            participant?.companyId === company.id &&
            (await isAgentInvokable(participant))
          ) {
            owner = { agentId: participant.id, decision: { kind: "assignee" } };
          } else {
            // Do not fall back to the issue assignee when the workflow says
            // a different (paused, missing, or otherwise unrunnable) agent
            // owns the pending step. That would execute the wrong stage and
            // leave the workflow stuck in a dispatch/recovery loop.
            owner = {
              agentId: null,
              decision: { kind: "assignee_unrunnable", assigneeId: blockedParticipantAgentId },
            };
          }
        }

        if (
          issue.status === "blocked" &&
          blockedParticipantAgentId &&
          owner.decision.kind === "assignee_unrunnable" &&
          owner.decision.assigneeId === blockedParticipantAgentId
        ) {
          result.skipped += 1;
          result.actions.push({
            kind: "skipped",
            issueId: issue.id,
            reason: "blocked_agent_participant_unrunnable",
          });
          continue;
        }

        if (owner.decision.kind === "no_owner" || owner.decision.kind === "assignee_unrunnable") {
          if (owner.decision.kind === "assignee_unrunnable") {
            // Assigned but the worker cannot run (paused, terminated, broken
            // chain, over budget). Try an automatic reroute to the project
            // lead; otherwise park the issue visibly blocked with the reason
            // instead of silently skipping it forever.
            const reroute = await rerouteFromUnrunnable(
              issue,
              owner.decision.assigneeId,
              issue.projectId ? projectLeadByProjectId.get(issue.projectId) ?? null : null,
            );
            if (reroute) {
              if (conf.dryRun) {
                result.actions.push({ kind: "skipped", issueId: issue.id, reason: "dry_run:rerouted_unrunnable" });
              } else {
                result.routed += 1;
                if (issue.status === "backlog") result.promoted += 1;
                result.actions.push({ kind: "routed", issueId: issue.id, agentId: reroute, basis: "reroute_unrunnable" });
                await logActivity(db, {
                  companyId: company.id,
                  actorType: "system",
                  actorId: "system",
                  action: "issue.execution_reconciler_rerouted_unrunnable",
                  entityType: "issue",
                  entityId: issue.id,
                  issueId: issue.id,
                  agentId: reroute,
                  details: { issueId: issue.id, fromAgentId: owner.decision.assigneeId, toAgentId: reroute },
                });
                issue.assigneeAgentId = reroute;
                issue.status = issue.status === "backlog" ? "todo" : issue.status;
              }
            } else if (!conf.dryRun) {
              const blocked = await markBlockedUnrunnable(issue, owner.decision.assigneeId);
              if (blocked) {
                result.blockedNoOwner += 1;
                result.actions.push({ kind: "blocked_no_owner", issueId: issue.id });
              } else {
                result.skipped += 1;
              }
              continue;
            } else {
              result.actions.push({ kind: "skipped", issueId: issue.id, reason: "dry_run:blocked_unrunnable" });
              continue;
            }
          } else if (conf.dryRun) {
            result.actions.push({ kind: "skipped", issueId: issue.id, reason: "dry_run:blocked_no_owner" });
            continue;
          } else {
            const blocked = await markBlockedNoOwner(issue);
            if (blocked) {
              result.blockedNoOwner += 1;
              result.actions.push({ kind: "blocked_no_owner", issueId: issue.id });
            } else {
              result.skipped += 1;
            }
            continue;
          }
        }

        const agentId = owner.agentId!;

        if (owner.decision.kind === "routed" && !conf.dryRun) {
          const updated = await db
            .update(issues)
            .set({
              assigneeAgentId: agentId,
              assigneeUserId: null,
              status: issue.status === "backlog" || issue.status === "in_progress" ? "todo" : issue.status,
              updatedAt: now,
            })
            .where(
              and(
                eq(issues.id, issue.id),
                eq(issues.companyId, company.id),
                isNull(issues.assigneeAgentId),
                isNull(issues.assigneeUserId),
                inArray(issues.status, ["backlog", "todo", "blocked"]),
              ),
            )
            .returning({ id: issues.id });
          if (updated.length === 0) {
            result.skipped += 1;
            continue;
          }
          result.routed += 1;
          result.actions.push({
            kind: "routed",
            issueId: issue.id,
            agentId,
            basis: owner.decision.basis,
          });
          if (issue.status === "backlog") result.promoted += 1;
          await logActivity(db, {
            companyId: company.id,
            actorType: "system",
            actorId: "system",
            action: "issue.execution_reconciler_routed",
            entityType: "issue",
            entityId: issue.id,
            issueId: issue.id,
            agentId,
            details: { issueId: issue.id, agentId, basis: owner.decision.basis },
          });
        } else if (issue.status === "backlog" && !conf.dryRun) {
          // Assigned-but-backlog work is the structural dead zone: assignment
          // wakes skip backlog, and timer heartbeats only look at todo+ work.
          const updated = await db
            .update(issues)
            .set({ status: "todo", updatedAt: now })
            .where(
              and(
                eq(issues.id, issue.id),
                eq(issues.companyId, company.id),
                eq(issues.status, "backlog"),
                eq(issues.assigneeAgentId, agentId),
              ),
            )
            .returning({ id: issues.id });
          if (updated.length > 0) {
            result.promoted += 1;
            result.actions.push({ kind: "promoted_to_todo", issueId: issue.id });
          }
        }

        // Independent verification is attached by the dedicated sweep below,
        // which covers work dispatched by ANY path (assignment wakes,
        // dependency wakes, routines, this reconciler).

        if (await hasQueuedIssueWake(company.id, issue.id, agentId)) {
          result.skipped += 1;
          result.actions.push({ kind: "already_covered", issueId: issue.id, reason: "queued_wake" });
          continue;
        }

        if (result.dispatched >= conf.maxDispatchesPerTick) {
          result.skipped += 1;
          result.actions.push({ kind: "skipped", issueId: issue.id, reason: "dispatch_budget" });
          continue;
        }

        if (conf.dryRun) {
          result.actions.push({ kind: "skipped", issueId: issue.id, reason: "dry_run:dispatch" });
          continue;
        }

        const dispatchBlock = await getNewDispatchBlockReason(
          company.id,
          agentId,
          reservationsCreatedThisPass,
          reservationsPresentAtFirstCheck,
        );
        if (dispatchBlock) {
          result.skipped += 1;
          result.actions.push({
            kind: "skipped",
            issueId: issue.id,
            reason: dispatchBlock,
          });
          continue;
        }

        const queuedRun = await deps.enqueueWakeup(agentId, {
          source: "automation",
          triggerDetail: "system",
          reason: EXECUTION_RECONCILER_DISPATCH_WAKE_REASON,
          payload: {
            issueId: issue.id,
            mutation: "execution_reconciler_dispatch",
          },
          requestedByActorType: "system",
          requestedByActorId: "execution-reconciler",
          contextSnapshot: {
            issueId: issue.id,
            taskId: issue.id,
            wakeReason: EXECUTION_RECONCILER_DISPATCH_WAKE_REASON,
            source: "execution.reconciler",
          },
          idempotencyKey: `execution-reconciler:${issue.id}:${issue.statusVersion}`,
        });
        reservationsCreatedThisPass.set(
          agentId,
          (reservationsCreatedThisPass.get(agentId) ?? 0) + 1,
        );
        result.dispatched += 1;
        result.actions.push({
          kind: "dispatched",
          issueId: issue.id,
          agentId,
          wakeRunId: queuedRun?.id ?? null,
        });
        await logActivity(db, {
          companyId: company.id,
          actorType: "system",
          actorId: "system",
          action: "issue.execution_reconciler_dispatched",
          entityType: "issue",
          entityId: issue.id,
          issueId: issue.id,
          agentId,
          details: { issueId: issue.id, agentId, runId: queuedRun?.id ?? null },
        });
      }

      // Reviewer dispatch: a pending verification stage must actually START.
      // When work enters in_review with a reviewer participant and no live
      // reviewer run, wake the reviewer automatically — verification being
      // first-class means the verifier lane runs without being told.
      if (!conf.dryRun) {
        const reviewRows = await db
          .select({
            id: issues.id,
            executionState: issues.executionState,
          })
          .from(issues)
          .where(
            and(
              eq(issues.companyId, company.id),
              eq(issues.status, "in_review"),
              visibleIssueCondition(),
            ),
          );
        for (const row of reviewRows) {
          const state = parseExecutionState(row.executionState);
          if (!state || state.status !== "pending") continue;
          const participant = state.currentParticipant;
          const reviewerAgentId =
            participant && participant.type === "agent" ? participant.agentId ?? null : null;
          if (!reviewerAgentId) continue;
          if (await hasLiveIssueRun(company.id, row.id, reviewerAgentId)) continue;
          if (await hasQueuedIssueWake(company.id, row.id, reviewerAgentId)) continue;
          const reviewer = await getAgent(reviewerAgentId);
          if (!reviewer || !(await isAgentInvokable(reviewer))) continue;
          if (result.dispatched >= conf.maxDispatchesPerTick) break;
          const dispatchBlock = await getNewDispatchBlockReason(
            company.id,
            reviewerAgentId,
            reservationsCreatedThisPass,
            reservationsPresentAtFirstCheck,
          );
          if (dispatchBlock) {
            result.skipped += 1;
            result.actions.push({
              kind: "skipped",
              issueId: row.id,
              reason: dispatchBlock,
            });
            continue;
          }
          const queuedRun = await deps.enqueueWakeup(reviewerAgentId, {
            source: "automation",
            triggerDetail: "system",
            reason: EXECUTION_RECONCILER_VERIFY_WAKE_REASON,
            payload: {
              issueId: row.id,
              mutation: "execution_reconciler_verify",
            },
            requestedByActorType: "system",
            requestedByActorId: "execution-reconciler",
            contextSnapshot: {
              issueId: row.id,
              taskId: row.id,
              wakeReason: EXECUTION_RECONCILER_VERIFY_WAKE_REASON,
              source: "execution.reconciler.verify",
            },
            idempotencyKey: `execution-reconciler-verify:${row.id}:${state.lastDecisionId ?? "initial"}`,
          });
          reservationsCreatedThisPass.set(
            reviewerAgentId,
            (reservationsCreatedThisPass.get(reviewerAgentId) ?? 0) + 1,
          );
          result.dispatched += 1;
          result.actions.push({
            kind: "dispatched",
            issueId: row.id,
            agentId: reviewerAgentId,
            wakeRunId: queuedRun?.id ?? null,
          });
          await logActivity(db, {
            companyId: company.id,
            actorType: "system",
            actorId: "system",
            action: "issue.execution_reconciler_verifier_dispatched",
            entityType: "issue",
            entityId: row.id,
            issueId: row.id,
            agentId: reviewerAgentId,
            details: { issueId: row.id, agentId: reviewerAgentId, runId: queuedRun?.id ?? null },
          });
        }
      }

      // Attach verification only to actionable deliverables. Parked work and
      // Paperclip's own administrative records should not recursively create
      // review-lane tasks.
      if (!conf.dryRun && conf.verifierAgentId) {
        const owned = await db
          .select({
            id: issues.id,
            status: issues.status,
            title: issues.title,
            executionPolicy: issues.executionPolicy,
            originKind: issues.originKind,
            assigneeAgentId: issues.assigneeAgentId,
            description: issues.description,
          })
          .from(issues)
          .where(
            and(
              eq(issues.companyId, company.id),
              inArray(issues.status, ["backlog", "todo", "blocked", "in_progress"]),
              isNotNull(issues.assigneeAgentId),
              isNull(issues.executionPolicy),
              visibleIssueCondition(),
            ),
          );
        for (const ownedIssue of owned) {
          if (!ownedIssue.assigneeAgentId) continue;
          await attachVerificationPolicy(company.id, ownedIssue, ownedIssue.assigneeAgentId, conf, now, result);
        }
      }
    }

    if (
      result.dispatched > 0 ||
      result.blockedNoOwner > 0 ||
      result.routed > 0 ||
      result.verificationPoliciesAttached > 0
    ) {
      logger.warn({ ...result, actions: result.actions.length }, "execution reconciler changed program state");
    }
    return result;
  }

  /**
   * Worker-lease recovery. A running run whose lease (controller lease /
   * last output) expired and which the server does not observe locally must
   * not hold its issue hostage: preserve all evidence (comments, artifacts),
   * terminalize the run as failed(worker_lease_expired), release the issue
   * execution lock, and re-dispatch under a bounded retry policy. After the
   * retry limit, move the issue to blocked and escalate.
   */
  async function buildProgramSummary(
    companyId: string,
    projectId: string | null,
  ): Promise<ExecutionProgramSummary> {
    return buildLaneSummary(companyId, projectId, null);
  }

  function parseExecutionState(raw: unknown): { status?: string; lastDecisionId?: string | null; currentParticipant?: { type?: string; agentId?: string | null } | null } | null {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    return raw as { status?: string; currentParticipant?: { type?: string; agentId?: string | null } | null };
  }

  /**
   * Lane-level stall accounting. A "lane" is an epic (parent issue) within a
   * project, the project itself, or the company-level null program. A busy
   * project must not hide a stalled epic: each epic is accounted separately,
   * and every stuck issue is CLASSIFIED so unowned work, unrunnable workers,
   * queued-but-never-started runs, stale running workers, dependency blocks,
   * verifier backlog, and missing acceptance criteria do not all look like
   * ordinary backlog.
   */
  async function buildLaneSummary(
    companyId: string,
    projectId: string | null,
    epicIssueId: string | null,
  ): Promise<ExecutionProgramSummary> {
    const now = new Date();
    const conf = settings();
    const stallBoundary = new Date(now.getTime() - conf.stallThresholdMs);
    const leaseBoundary = new Date(now.getTime() - conf.workerLeaseTtlMs);
    const queuedBoundary = new Date(now.getTime() - conf.queuedRunTimeoutMs);

    const rows = await db
      .select({
        id: issues.id,
        status: issues.status,
        assigneeAgentId: issues.assigneeAgentId,
        originKind: issues.originKind,
        parentId: issues.parentId,
        description: issues.description,
        executionState: issues.executionState,
        executionPolicy: issues.executionPolicy,
        unblockDescriptor: issues.unblockDescriptor,
        updatedAt: issues.updatedAt,
      })
      .from(issues)
      .where(
        and(
          eq(issues.companyId, companyId),
          visibleIssueCondition(),
          projectId ? eq(issues.projectId, projectId) : isNull(issues.projectId),
          inArray(issues.status, ["backlog", "todo", "blocked", "in_progress", "in_review"]),
        ),
      );

    const laneRows = epicIssueId
      ? rows.filter((row) => row.parentId === epicIssueId)
      : rows.filter((row) => row.parentId === null);
    const programRows = laneRows.filter(
      (row) => !NON_DISPATCHABLE_ORIGIN_KINDS.has(row.originKind ?? "manual"),
    );

    const readiness = await issuesSvc.listDependencyReadiness(
      companyId,
      programRows.map((row) => row.id),
    );

    const issueIds = programRows.map((row) => row.id);
    const liveRunRows = issueIds.length
      ? await db
          .select({
            id: heartbeatRuns.id,
            status: heartbeatRuns.status,
            agentId: heartbeatRuns.agentId,
            issueId: sql<string>`${heartbeatRuns.contextSnapshot} ->> 'issueId'`,
            contextSnapshot: heartbeatRuns.contextSnapshot,
            createdAt: heartbeatRuns.createdAt,
            lastOutputAt: heartbeatRuns.lastOutputAt,
            startedAt: heartbeatRuns.startedAt,
          })
          .from(heartbeatRuns)
          .where(
            and(
              eq(heartbeatRuns.companyId, companyId),
              inArray(heartbeatRuns.status, ["queued", "running"]),
              sql`${heartbeatRuns.contextSnapshot} ->> 'issueId' in (${sql.join(
                issueIds.map((id) => sql`${id}`),
                sql`, `,
              )})`,
            ),
          )
      : [];
    const runsByIssue = new Map<string, typeof liveRunRows>();
    for (const run of liveRunRows) {
      const list = runsByIssue.get(run.issueId) ?? [];
      list.push(run);
      runsByIssue.set(run.issueId, list);
    }

    const companyAgents = await db
      .select({
        id: agents.id,
        status: agents.status,
        reportsTo: agents.reportsTo,
        companyId: agents.companyId,
        name: agents.name,
      })
      .from(agents)
      .where(eq(agents.companyId, companyId));
    const invokabilityCache = new Map<string, boolean>();
    const dispatchBlockCache = new Map<string, Promise<"concurrency_limit" | "dispatch_blackout" | null>>();
    const isAssigneeRunnable = async (agentId: string) => {
      const cached = invokabilityCache.get(agentId);
      if (cached !== undefined) return cached;
      const agent = companyAgents.find((candidate) => candidate.id === agentId);
      const invokable = agent
        ? (await evaluateAgentInvokabilityFromDb(db, agent)).invokable
        : false;
      invokabilityCache.set(agentId, invokable);
      return invokable;
    };
    const getDispatchBlockReason = (agentId: string) => {
      let pending = dispatchBlockCache.get(agentId);
      if (!pending) {
        pending = deps.getQueuedRunDispatchBlockReason?.(agentId) ?? Promise.resolve(null);
        dispatchBlockCache.set(agentId, pending);
      }
      return pending;
    };

    const classification: Partial<Record<StallClass, number>> = {};
    const bump = (cls: StallClass) => {
      classification[cls] = (classification[cls] ?? 0) + 1;
    };
    const hasCriteria = (description: string | null) =>
      /acceptance criteri/i.test(description ?? "");

    let eligible = 0;
    let running = 0;
    let verifying = 0;
    let blocked = 0;
    let unowned = 0;
    let progressing = false;
    let lastTransition: Date | null = null;

    for (const row of programRows) {
      const ready = readiness.get(row.id)?.isDependencyReady ?? false;
      const runs = runsByIssue.get(row.id) ?? [];
      const hasRunningRun = runs.some((run) => run.status === "running");
      const hasFreshQueuedRun = runs.some(
        (run) => run.status === "queued" && run.createdAt >= queuedBoundary,
      );
      const staleQueuedRuns = runs.filter(
        (run) => run.status === "queued" && run.createdAt < queuedBoundary,
      );
      const executionState = parseExecutionState(row.executionState);
      const currentWorkOwnerAgentId =
        row.status === "in_review" && executionState?.currentParticipant?.type === "agent"
          ? executionState.currentParticipant.agentId ?? null
          : row.assigneeAgentId;
      const waitingForDispatch = await Promise.all(
        staleQueuedRuns.map(async (run) => {
          const context = run.contextSnapshot as Record<string, unknown> | null;
          const isCurrentIssueOwner = run.agentId === currentWorkOwnerAgentId;
          const interactionId = typeof context?.interactionId === "string" ? context.interactionId : null;
          const interactionKind = typeof context?.interactionKind === "string" ? context.interactionKind : null;
          const interactionMatchesWake =
            context?.wakeReason === "interaction_pending" &&
            context.source === "issue.interaction.created" &&
            context.wakeSource === "automation" &&
            context.wakeTriggerDetail === "system" &&
            context.interactionStatus === "pending" &&
            context.interactionTargetAgentId === run.agentId &&
            Boolean(interactionId && isUuidLike(interactionId)) &&
            Boolean(interactionKind && (ISSUE_THREAD_INTERACTION_KINDS as readonly string[]).includes(interactionKind));
          const isInteractionTarget = interactionMatchesWake && Boolean(
            await db
              .select({ id: issueThreadInteractions.id })
              .from(issueThreadInteractions)
              .where(
                and(
                  eq(issueThreadInteractions.companyId, companyId),
                  eq(issueThreadInteractions.issueId, row.id),
                  eq(issueThreadInteractions.id, interactionId!),
                  eq(issueThreadInteractions.kind, interactionKind!),
                  eq(issueThreadInteractions.status, "pending"),
                  eq(issueThreadInteractions.addresseeAgentId, run.agentId),
                ),
              )
              .limit(1)
              .then((matches) => matches[0]),
          );
          if (!isCurrentIssueOwner && !isInteractionTarget) return false;
          return Boolean(await getDispatchBlockReason(run.agentId));
        }),
      );
      const hasWaitingQueuedRun = waitingForDispatch.some(Boolean);
      const hasStaleQueuedRun = waitingForDispatch.some((waiting) => !waiting);
      const hasStaleRunningRun = runs.some(
        (run) =>
          run.status === "running" &&
          new Date(run.lastOutputAt ?? run.startedAt ?? run.createdAt) < leaseBoundary &&
          !(deps.isRunLocallyAlive?.(run.id) ?? false),
      );

      if (row.status === "in_progress" && hasRunningRun && !hasStaleRunningRun) progressing = true;
      if (row.status === "in_review" && (hasRunningRun || hasFreshQueuedRun || hasWaitingQueuedRun)) progressing = true;

      if (row.status === "in_progress") running += 1;
      else if (row.status === "in_review") verifying += 1;
      else if (row.status === "blocked") blocked += 1;
      else if (ready) eligible += 1;

      if (!row.assigneeAgentId && ready && (row.status === "backlog" || row.status === "todo")) {
        unowned += 1;
        bump("unowned");
      } else if (
        row.assigneeAgentId &&
        (row.status === "todo" || row.status === "in_progress") &&
        !hasRunningRun &&
        !hasFreshQueuedRun &&
        !hasWaitingQueuedRun
      ) {
        if (!(await isAssigneeRunnable(row.assigneeAgentId))) bump("assigned_unrunnable");
      }
      if (hasStaleQueuedRun && !hasRunningRun) bump("queued_never_started");
      if (hasStaleRunningRun) bump("stale_running");
      if (row.status === "blocked" && !ready && !row.unblockDescriptor) bump("blocked_dependency");
      if (row.status === "in_review") {
        const state = parseExecutionState(row.executionState);
        if (state?.status === "pending" && !hasRunningRun && !hasFreshQueuedRun && !hasWaitingQueuedRun) {
          bump("verifier_backlog");
        }
      }
      if (
        row.assigneeAgentId &&
        !row.executionPolicy &&
        !hasCriteria(row.description) &&
        (row.status === "todo" || row.status === "in_progress")
      ) {
        bump("missing_acceptance_criteria");
      }
      if (!lastTransition || row.updatedAt > lastTransition) lastTransition = row.updatedAt;
    }

    const stuckTotal = Object.values(classification).reduce((sum, n) => sum + (n ?? 0), 0);
    const stalled =
      stuckTotal > 0 && !progressing && (lastTransition === null || lastTransition < stallBoundary);

    return {
      projectId,
      epicIssueId,
      eligibleIssues: eligible,
      runningIssues: running,
      verifyingIssues: verifying,
      blockedIssues: blocked,
      unownedIssues: unowned,
      staleRuns: classification.stale_running ?? 0,
      lastMeaningfulTransitionAt: lastTransition,
      stalled,
      classification,
      progressing,
    };
  }

  /**
   * Lane watchdog: an active lane (epic, project, or company program) with
   * classified stuck work and no actually-progressing run is an orchestration
   * failure, not a quiet board. Each stalled lane raises exactly one incident
   * (enforced by the partial unique index, originId = epic issue id, project
   * id, or "company"), carrying the full classification so unowned work,
   * unrunnable workers, queued-never-started runs, stale workers, dependency
   * blocks, verifier backlog, and missing acceptance criteria are
   * distinguishable at a glance. Deliberately NOT gated on scheduling
   * suppression: visibility must remain active while dispatch is paused.
   */
  async function scanProgramStalls(opts?: {
    companyId?: string;
    now?: Date;
  }): Promise<ProgramStallScanResult> {
    const now = opts?.now ?? new Date();
    const conf = settings();
    const result: ProgramStallScanResult = {
      programsScanned: 0,
      stalled: 0,
      incidentsCreated: 0,
      incidentsEscalated: 0,
      incidentsExisting: 0,
      incidentsSuppressed: 0,
    };
    if (!conf.enabled) return result;

    const companyRows = await db
      .select({ id: companies.id })
      .from(companies)
      .where(
        and(
          opts?.companyId ? eq(companies.id, opts.companyId) : undefined,
          isNull(companies.pausedAt),
        ),
      );

    for (const company of companyRows) {
      if (reconcilerDisabledForCompany(company.id)) continue;
      // Stalled-program incidents belong to the company's chief executive,
      // not to nobody: the CEO sweep owns recovery items addressed to the
      // board operator instead of leaving them unassigned (SON-4680).
      const [boardOwner] = await db
        .select({ id: agents.id })
        .from(agents)
        .where(
          and(
            eq(agents.companyId, company.id),
            sql`lower(trim(${agents.role})) = 'ceo'`,
            ne(agents.status, "terminated"),
          ),
        )
        .limit(1);
      const projectRows = await db
        .select({ id: projects.id })
        .from(projects)
        .where(and(eq(projects.companyId, company.id), eq(projects.status, "in_progress")));
      const projectIds = [...projectRows.map((row) => row.id), null];

      for (const projectId of projectIds) {
        // Epic lanes first: a busy project must not hide a stalled epic.
        const epicRows = await db
          .select({ id: issues.id, title: issues.title })
          .from(issues)
          .where(
            and(
              eq(issues.companyId, company.id),
              visibleIssueCondition(),
              projectId ? eq(issues.projectId, projectId) : isNull(issues.projectId),
              isNotNull(issues.parentId),
              inArray(issues.status, ["backlog", "todo", "blocked", "in_progress", "in_review"]),
            ),
          );
        const epicIds = [...new Set(epicRows.map((row) => row.id))];

        const lanes: Array<{ epicIssueId: string | null; label: string; originId: string }> = [
          ...epicIds.map((epicId) => ({
            epicIssueId: epicId,
            label: `epic "${epicRows.find((row) => row.id === epicId)?.title.slice(0, 60) ?? epicId}"`,
            originId: epicId,
          })),
          {
            epicIssueId: null,
            label: projectId ? "project program" : "company program",
            originId: projectId ?? "company",
          },
        ];

        for (const lane of lanes) {
          const summary = await buildLaneSummary(company.id, projectId, lane.epicIssueId);
          result.programsScanned += 1;
          if (!summary.stalled) continue;
          result.stalled += 1;

          const existing = await db
            .select({ id: issues.id })
            .from(issues)
            .where(
              and(
                eq(issues.companyId, company.id),
                eq(issues.originKind, ORCHESTRATION_INCIDENT_ORIGIN_KIND),
                eq(issues.originId, lane.originId),
                inArray(issues.status, ["backlog", "todo", "blocked", "in_progress", "in_review"]),
                visibleIssueCondition(),
              ),
            )
            .limit(1);

          const classLines = stallSignature(summary.classification);
          const body =
            `Orchestration stall detected in ${lane.label}. ` +
            `Eligible: ${summary.eligibleIssues}; running: ${summary.runningIssues}; ` +
            `verifying: ${summary.verifyingIssues}; blocked: ${summary.blockedIssues}. ` +
            `Stuck classification: ${classLines || "none"}. ` +
            `No run in this lane is actually progressing; last meaningful transition: ` +
            `${summary.lastMeaningfulTransitionAt?.toISOString() ?? "never"} ` +
            `(threshold ${Math.round(conf.stallThresholdMs / 60000)} min).`;

          if (existing.length > 0) {
            if (!conf.dryRun) {
              const lastComment = await db
                .select({ createdAt: issueComments.createdAt })
                .from(issueComments)
                .where(eq(issueComments.issueId, existing[0].id))
                .orderBy(desc(issueComments.createdAt))
                .limit(1);
              const lastEscalation = lastComment[0]?.createdAt ?? new Date(0);
              if (now.getTime() - lastEscalation.getTime() >= conf.stallThresholdMs) {
                await addSystemComment(company.id, existing[0].id, `Still stalled. ${body}`);
                result.incidentsEscalated += 1;
              }
            }
            result.incidentsExisting += 1;
            continue;
          }

          // SON-4797: the CEO operating routine cancels these notices as
          // duplicates-of-record, and every scan then found no OPEN incident
          // and recreated an identical card about the unchanged stall.
          // Suppress recreation while a recent notice with the same stall
          // signature exists in any status, including cancelled predecessors.
          if (conf.stallNoticeDedupWindowMs > 0) {
            const [predecessor] = await db
              .select({
                originFingerprint: issues.originFingerprint,
                description: issues.description,
              })
              .from(issues)
              .where(
                and(
                  eq(issues.companyId, company.id),
                  eq(issues.originKind, ORCHESTRATION_INCIDENT_ORIGIN_KIND),
                  eq(issues.originId, lane.originId),
                  gte(issues.createdAt, new Date(now.getTime() - conf.stallNoticeDedupWindowMs)),
                  visibleIssueCondition(),
                ),
              )
              .orderBy(desc(issues.createdAt))
              .limit(1);
            if (predecessor && stallNoticeSignatureOf(predecessor) === classLines) {
              result.incidentsSuppressed += 1;
              continue;
            }
          }

          if (conf.dryRun) continue;
          try {
            const created = await issuesSvc.create(company.id, {
              title: `[orchestration] ${lane.label} stalled: ${classLines === "none" ? "no progress" : classLines}`.slice(0, 180),
              description: body,
              status: "todo",
              priority: "critical",
              projectId: projectId ?? undefined,
              originKind: ORCHESTRATION_INCIDENT_ORIGIN_KIND,
              originId: lane.originId,
              assigneeAgentId: boardOwner?.id ?? undefined,
              originFingerprint: `${STALL_FINGERPRINT_PREFIX}${classLines}`,
            });
            if (created) {
              result.incidentsCreated += 1;
              await logActivity(db, {
                companyId: company.id,
                actorType: "system",
                actorId: "system",
                action: "issue.execution_reconciler_incident_created",
                entityType: "issue",
                entityId: created.id,
                issueId: created.id,
                details: { projectId, epicIssueId: lane.epicIssueId, classification: summary.classification, assigneeAgentId: boardOwner?.id ?? null },
              });
            }
          } catch (error) {
            if (isUniqueViolation(error)) {
              result.incidentsExisting += 1;
            } else {
              throw error;
            }
          }
        }
      }
    }

    if (result.incidentsCreated > 0 || result.incidentsEscalated > 0) {
      logger.warn({ ...result }, "execution reconciler lane watchdog raised incidents");
    }
    return result;
  }

  // ---------------------------------------------------------------------------
  // Level-triggered transition deadlines (SON-6153)
  //
  // The dispatch pipeline is edge-triggered: a missed or failed wake leaves a
  // card asleep with nothing watching. These scans make the deadline structure
  // level-triggered instead: on every tick, read the whole board, compare
  // current vs. expected state, and emit AT MOST ONE corrective action per
  // card. They never bypass the checkout CAS and never create work cards —
  // receipts are comments on the affected card; the single exception is the
  // one-per-card CEO escalation incident.
  // ---------------------------------------------------------------------------

  async function resolveCompanyBoardOwner(companyId: string): Promise<string | null> {
    const [boardOwner] = await db
      .select({ id: agents.id })
      .from(agents)
      .where(
        and(
          eq(agents.companyId, companyId),
          sql`lower(trim(${agents.role})) = 'ceo'`,
          ne(agents.status, "terminated"),
        ),
      )
      .limit(1);
    return boardOwner?.id ?? null;
  }

  async function countLedgerActions(
    companyId: string,
    action: string,
    entityId: string,
  ): Promise<number> {
    const [row] = await db
      .select({ count: sql<number>`count(*)` })
      .from(activityLog)
      .where(
        and(
          eq(activityLog.companyId, companyId),
          eq(activityLog.action, action),
          eq(activityLog.entityType, "issue"),
          eq(activityLog.entityId, entityId),
        ),
      );
    return Number(row?.count ?? 0);
  }

  /** Latest executor-side activity on a card: assignee comments, runs linked
   *  through a wake payload or native issue id, and any board mutation. The
   *  reconciler's own system receipts deliberately do NOT count as executor
   *  activity (that would make the stall timer self-extinguishing). A live
   *  queued/running run for the card counts as activity right now. */
  async function lastExecutorActivityAt(
    issue: Pick<typeof issues.$inferSelect, "id" | "companyId" | "updatedAt" | "assigneeAgentId">,
  ): Promise<Date | null> {
    const stamps: Date[] = [];
    if (issue.updatedAt) stamps.push(new Date(issue.updatedAt));
    if (issue.assigneeAgentId) {
      const [lastAssigneeComment] = await db
        .select({ createdAt: issueComments.createdAt })
        .from(issueComments)
        .where(
          and(
            eq(issueComments.issueId, issue.id),
            isNull(issueComments.deletedAt),
            eq(issueComments.authorAgentId, issue.assigneeAgentId),
          ),
        )
        .orderBy(desc(issueComments.createdAt))
        .limit(1);
      if (lastAssigneeComment?.createdAt) stamps.push(new Date(lastAssigneeComment.createdAt));
    }
    const [lastLinkedRun] = await db
      .select({ startedAt: heartbeatRuns.startedAt })
      .from(heartbeatRuns)
      .innerJoin(agentWakeupRequests, eq(heartbeatRuns.wakeupRequestId, agentWakeupRequests.id))
      .where(
        and(
          eq(heartbeatRuns.companyId, issue.companyId),
          isNotNull(heartbeatRuns.startedAt),
          sql`(${agentWakeupRequests.payload} ->> 'issueId') = ${issue.id}`,
        ),
      )
      .orderBy(desc(heartbeatRuns.startedAt))
      .limit(1);
    if (lastLinkedRun?.startedAt) stamps.push(new Date(lastLinkedRun.startedAt));
    const [lastNativeRun] = await db
      .select({ startedAt: heartbeatRuns.startedAt })
      .from(heartbeatRuns)
      .where(
        and(
          eq(heartbeatRuns.companyId, issue.companyId),
          isNotNull(heartbeatRuns.startedAt),
          eq(heartbeatRuns.nativeIssueId, issue.id),
        ),
      )
      .orderBy(desc(heartbeatRuns.startedAt))
      .limit(1);
    if (lastNativeRun?.startedAt) stamps.push(new Date(lastNativeRun.startedAt));
    const [liveRun] = await db
      .select({ id: heartbeatRuns.id })
      .from(heartbeatRuns)
      .leftJoin(agentWakeupRequests, eq(heartbeatRuns.wakeupRequestId, agentWakeupRequests.id))
      .where(
        and(
          eq(heartbeatRuns.companyId, issue.companyId),
          inArray(heartbeatRuns.status, ["queued", "running"]),
          or(
            sql`(${agentWakeupRequests.payload} ->> 'issueId') = ${issue.id}`,
            eq(heartbeatRuns.nativeIssueId, issue.id),
          ),
        ),
      )
      .limit(1);
    if (liveRun) return new Date();
    return stamps.length > 0 ? new Date(Math.max(...stamps.map((stamp) => stamp.getTime()))) : null;
  }

  type TransitionEscalationTarget = {
    id: string;
    identifier: string | null;
    projectId: string | null;
    parentId: string | null;
    title: string;
  };

  /** ONE CEO-loop escalation per card, ever: deduped by an open incident with
   *  a per-card-and-rule origin id, and permanently by the escalation ledger
   *  entry on the affected card. */
  async function ensureTransitionEscalationIncident(
    companyId: string,
    target: TransitionEscalationTarget,
    rule: string,
    body: string,
  ): Promise<{ created: boolean }> {
    if (settings().dryRun) {
      logger.warn(
        { companyId, issueId: target.id, rule, dryRun: true },
        "execution reconciler transition escalation suppressed (dry run)",
      );
      return { created: false };
    }
    if ((await countLedgerActions(companyId, TRANSITION_ESCALATION_LEDGER_ACTION, target.id)) > 0) {
      return { created: false };
    }
    const originId = `${TRANSITION_ESCALATION_ORIGIN_PREFIX}${target.id}:${rule}`;
    const boardOwnerId = await resolveCompanyBoardOwner(companyId);
    try {
      const created = await issuesSvc.create(companyId, {
        title: `[orchestration] stalled: ${rule} on ${target.identifier ?? target.id}`.slice(0, 180),
        description: body,
        status: "todo",
        priority: "critical",
        projectId: target.projectId ?? undefined,
        originKind: ORCHESTRATION_INCIDENT_ORIGIN_KIND,
        originId,
        assigneeAgentId: boardOwnerId ?? undefined,
        originFingerprint: originId,
      });
      if (!created) return { created: false };
      await logActivity(db, {
        companyId,
        actorType: "system",
        actorId: "execution-reconciler",
        action: TRANSITION_ESCALATION_LEDGER_ACTION,
        entityType: "issue",
        entityId: target.id,
        issueId: created.id,
        details: { rule, escalatedTo: boardOwnerId, originId },
      });
      // The receipt comment lands on the affected card only when that card is
      // a real issue row (agent-scoped targets have no card to comment on;
      // the incident itself already carries the full trail).
      const [targetIssue] = await db
        .select({ id: issues.id })
        .from(issues)
        .where(and(eq(issues.companyId, companyId), eq(issues.id, target.id)))
        .limit(1);
      if (targetIssue) {
        await addSystemComment(
          companyId,
          target.id,
          `Execution reconciler: **CEO_ESCALATION** — ${rule} did not recover after bounded retries; escalated once to the CEO loop (incident \`${created.id}\`). No further automatic re-wakes for this rule.`,
        );
      }
      return { created: true };
    } catch (error) {
      if (isUniqueViolation(error)) return { created: false };
      throw error;
    }
  }

  async function reconcileTransitionDeadlines(opts?: {
    companyId?: string;
    now?: Date;
  }): Promise<TransitionDeadlineScanResult> {
    const conf = settings();
    const result: TransitionDeadlineScanResult = {
      companiesScanned: 0,
      lanesReAdmitted: 0,
      inProgressStalled: 0,
      reawakeOwner: 0,
      reviewStalled: 0,
      reviewerNudged: 0,
      reviewerReassigned: 0,
      shipDispatchQueued: 0,
      blockedDisposition: 0,
      wakeReceiptsDead: 0,
      wakeReceiptsRetried: 0,
      escalatedToCeo: 0,
      skippedLaneUnhealthy: 0,
      skippedOneActionPerCard: 0,
    };
    if (!conf.enabled || !conf.levelTriggeredEnabled) return result;
    const now = opts?.now ?? new Date();
    let budget = conf.maxTransitionActionsPerTick;
    const processedCards = new Set<string>();
    const unhealthyLanes = new Set<string>();

    const companyRows = await db
      .select({ id: companies.id })
      .from(companies)
      .where(
        and(
          opts?.companyId ? eq(companies.id, opts.companyId) : undefined,
          isNull(companies.pausedAt),
        ),
      );

    for (const company of companyRows) {
      if (reconcilerDisabledForCompany(company.id)) continue;
      result.companiesScanned += 1;
      const companyId = company.id;
      const boardOwnerId = await resolveCompanyBoardOwner(companyId);

      // Rule 6 — LANE LIVENESS: a lane in roster status=error while holding
      // open work is itself the defect. Re-admit (wake) the lane BEFORE any
      // card-level corrective action; its cards are skipped this tick.
      const errorLanes = await db
        .select()
        .from(agents)
        .where(and(eq(agents.companyId, companyId), eq(agents.status, "error")))
        .limit(20);
      for (const lane of errorLanes) {
        const [openWork] = await db
          .select({ count: sql<number>`count(*)` })
          .from(issues)
          .where(
            and(
              eq(issues.companyId, companyId),
              eq(issues.assigneeAgentId, lane.id),
              inArray(issues.status, ["backlog", "todo", "blocked", "in_progress", "in_review"]),
              visibleIssueCondition(),
            ),
          );
        if (Number(openWork?.count ?? 0) === 0) continue;
        unhealthyLanes.add(lane.id);
        if (budget <= 0) continue;
        budget -= 1;
        if (!conf.dryRun) {
          await deps.enqueueWakeup(lane.id, {
            reason: EXECUTION_RECONCILER_LANE_LIVENESS_REASON,
            payload: { laneAgentId: lane.id, trigger: "level_triggered_transition_scan" },
            source: "automation",
            triggerDetail: "system",
            requestedByActorType: "system",
            requestedByActorId: "execution-reconciler",
          });
          const [oldestCard] = await db
            .select({ id: issues.id })
            .from(issues)
            .where(
              and(
                eq(issues.companyId, companyId),
                eq(issues.assigneeAgentId, lane.id),
                inArray(issues.status, ["backlog", "todo", "blocked", "in_progress", "in_review"]),
                visibleIssueCondition(),
              ),
            )
            .orderBy(asc(issues.updatedAt))
            .limit(1);
          if (oldestCard) {
            processedCards.add(oldestCard.id);
            await logActivity(db, {
              companyId,
              actorType: "system",
              actorId: "execution-reconciler",
              action: TRANSITION_LANE_LIVENESS_LEDGER_ACTION,
              entityType: "issue",
              entityId: oldestCard.id,
              issueId: oldestCard.id,
              details: {
                laneAgentId: lane.id,
                openWork: Number(openWork?.count ?? 0),
                errorReason: lane.errorReason ?? null,
              },
            });
            await addSystemComment(
              companyId,
              oldestCard.id,
              `Execution reconciler: **LANE_LIVENESS** — owner lane is in roster status \`error\` while holding open work. Re-admission wake sent before any card-level corrective action; this lane's cards are skipped for the remainder of this tick.`,
            );
          } else {
            await logActivity(db, {
              companyId,
              actorType: "system",
              actorId: "execution-reconciler",
              action: TRANSITION_LANE_LIVENESS_LEDGER_ACTION,
              entityType: "agent",
              entityId: lane.id,
              details: {
                laneAgentId: lane.id,
                openWork: Number(openWork?.count ?? 0),
                errorReason: lane.errorReason ?? null,
              },
            });
          }
        }
        result.lanesReAdmitted += 1;
      }

      // Rule 1 — IN_PROGRESS stall: no executor run activity for the stall
      // window → re-wake the owner (max N attempts), then escalate once to
      // the CEO loop.
      const inProgressCutoff = new Date(now.getTime() - conf.inProgressStallMs);
      const inProgressCandidates = await db
        .select()
        .from(issues)
        .where(
          and(
            eq(issues.companyId, companyId),
            eq(issues.status, "in_progress"),
            isNotNull(issues.assigneeAgentId),
            lt(issues.updatedAt, inProgressCutoff),
            visibleIssueCondition(),
          ),
        )
        .orderBy(asc(issues.updatedAt))
        .limit(25);
      for (const issue of inProgressCandidates) {
        if (isAdministrativeRecordIssue(issue.title, issue.description)) continue;
        const assigneeId = issue.assigneeAgentId as string;
        if (unhealthyLanes.has(assigneeId)) {
          result.skippedLaneUnhealthy += 1;
          continue;
        }
        if (processedCards.has(issue.id)) {
          result.skippedOneActionPerCard += 1;
          continue;
        }
        processedCards.add(issue.id);
        const lastActivity = await lastExecutorActivityAt(issue);
        if (lastActivity && now.getTime() - lastActivity.getTime() < conf.inProgressStallMs) continue;
        result.inProgressStalled += 1;
        const attempts = await countLedgerActions(companyId, TRANSITION_REAWAKE_LEDGER_ACTION, issue.id);
        if (attempts < conf.maxReawakeAttempts) {
          if (budget <= 0) continue;
          budget -= 1;
          if (!conf.dryRun) {
            await deps.enqueueWakeup(assigneeId, {
              reason: EXECUTION_RECONCILER_TRANSITION_REAWAKE_REASON,
              payload: { issueId: issue.id, stalledMs: conf.inProgressStallMs, attempt: attempts + 1 },
              source: "automation",
              triggerDetail: "system",
              requestedByActorType: "system",
              requestedByActorId: "execution-reconciler",
            });
            await logActivity(db, {
              companyId,
              actorType: "system",
              actorId: "execution-reconciler",
              action: TRANSITION_REAWAKE_LEDGER_ACTION,
              entityType: "issue",
              entityId: issue.id,
              issueId: issue.id,
              details: { rule: "in_progress_stall", attempt: attempts + 1, maxAttempts: conf.maxReawakeAttempts },
            });
            await addSystemComment(
              companyId,
              issue.id,
              `Execution reconciler: **TRANSITION_REWAKE** (attempt ${attempts + 1}/${conf.maxReawakeAttempts}) — no executor run activity for ${Math.round(conf.inProgressStallMs / 3_600_000)}h. Owner re-woken; a run receipt is expected within ${Math.round(conf.wakeReceiptGraceMs / 60_000)} min (otherwise this wake is marked dead and retried).`,
            );
          }
          result.reawakeOwner += 1;
        } else {
          const escalated = await ensureTransitionEscalationIncident(
            companyId,
            issue,
            "in_progress_stall",
            `Transition deadline: issue \`${issue.identifier ?? issue.id}\` stayed \`in_progress\` with no executor run activity for ${Math.round(conf.inProgressStallMs / 3_600_000)}h. The owner was re-woken ${attempts} time(s) without producing a run receipt. Bounded retries exhausted; the CEO loop owns recovery from here.`,
          );
          if (escalated.created) {
            result.escalatedToCeo += 1;
          }
        }
      }

      // Rule 2 — IN_REVIEW stall: no reviewer verdict for the verdict window
      // → nudge the named reviewer once, then reassign to the configured
      // fallback reviewer, else escalate once.
      const reviewCutoff = new Date(now.getTime() - conf.inReviewVerdictMs);
      const reviewCandidates = await db
        .select()
        .from(issues)
        .where(
          and(
            eq(issues.companyId, companyId),
            eq(issues.status, "in_review"),
            lt(issues.updatedAt, reviewCutoff),
            visibleIssueCondition(),
          ),
        )
        .orderBy(asc(issues.updatedAt))
        .limit(25);
      for (const issue of reviewCandidates) {
        if (isAdministrativeRecordIssue(issue.title, issue.description)) continue;
        const pendingGate = parsePendingReviewParticipant(issue.executionPolicy);
        const reviewerId = pendingGate?.reviewerAgentId ?? null;
        // When the pending gate's participant IS the configured release lane,
        // this card belongs to rule 3 (gated ship dispatch), not to a review
        // nudge — nudging the release lane would mislabel the wait.
        if (conf.releaseLaneAgentId && reviewerId === conf.releaseLaneAgentId) {
          continue;
        }
        if (processedCards.has(issue.id)) {
          result.skippedOneActionPerCard += 1;
          continue;
        }
        processedCards.add(issue.id);
        if (reviewerId && unhealthyLanes.has(reviewerId)) {
          result.skippedLaneUnhealthy += 1;
          continue;
        }
        let lastReviewerSignalAt: Date | null = issue.updatedAt ? new Date(issue.updatedAt) : null;
        if (reviewerId) {
          const [lastReviewerComment] = await db
            .select({ createdAt: issueComments.createdAt })
            .from(issueComments)
            .where(
              and(
                eq(issueComments.issueId, issue.id),
                isNull(issueComments.deletedAt),
                eq(issueComments.authorAgentId, reviewerId),
              ),
            )
            .orderBy(desc(issueComments.createdAt))
            .limit(1);
          if (lastReviewerComment?.createdAt) {
            const commentAt = new Date(lastReviewerComment.createdAt);
            if (!lastReviewerSignalAt || commentAt > lastReviewerSignalAt) lastReviewerSignalAt = commentAt;
          }
        }
        if (lastReviewerSignalAt && now.getTime() - lastReviewerSignalAt.getTime() < conf.inReviewVerdictMs) {
          continue;
        }
        result.reviewStalled += 1;
        const nudges = await countLedgerActions(companyId, TRANSITION_REVIEW_NUDGE_LEDGER_ACTION, issue.id);
        const nudgeTarget = reviewerId ?? conf.fallbackReviewerAgentId ?? boardOwnerId;
        if (nudges === 0 && nudgeTarget) {
          if (budget <= 0) continue;
          budget -= 1;
          if (!conf.dryRun) {
            await deps.enqueueWakeup(nudgeTarget, {
              reason: EXECUTION_RECONCILER_REVIEW_NUDGE_REASON,
              payload: { issueId: issue.id, waitingMs: conf.inReviewVerdictMs, role: reviewerId ? "reviewer" : "fallback_reviewer" },
              source: "automation",
              triggerDetail: "system",
              requestedByActorType: "system",
              requestedByActorId: "execution-reconciler",
            });
            await logActivity(db, {
              companyId,
              actorType: "system",
              actorId: "execution-reconciler",
              action: TRANSITION_REVIEW_NUDGE_LEDGER_ACTION,
              entityType: "issue",
              entityId: issue.id,
              issueId: issue.id,
              details: { rule: "review_stall", nudgedAgentId: nudgeTarget },
            });
            await addSystemComment(
              companyId,
              issue.id,
              `Execution reconciler: **REVIEW_NUDGE** — no reviewer verdict for ${Math.round(conf.inReviewVerdictMs / 3_600_000)}h on the pending review stage. Named reviewer nudged.`,
            );
          }
          result.reviewerNudged += 1;
          continue;
        }
        const fallbackReviewerId = conf.fallbackReviewerAgentId;
        if (
          fallbackReviewerId &&
          reviewerId &&
          fallbackReviewerId !== reviewerId &&
          (await countLedgerActions(companyId, TRANSITION_REVIEWER_REASSIGN_LEDGER_ACTION, issue.id)) === 0
        ) {
          if (budget <= 0) continue;
          budget -= 1;
          const nextPolicy = reassignReviewParticipant(issue.executionPolicy, reviewerId, fallbackReviewerId);
          if (nextPolicy && !conf.dryRun) {
            await db
              .update(issues)
              .set({ executionPolicy: nextPolicy, updatedAt: now })
              .where(and(eq(issues.id, issue.id), eq(issues.companyId, companyId)));
            await logActivity(db, {
              companyId,
              actorType: "system",
              actorId: "execution-reconciler",
              action: TRANSITION_REVIEWER_REASSIGN_LEDGER_ACTION,
              entityType: "issue",
              entityId: issue.id,
              issueId: issue.id,
              details: { rule: "review_stall", from: reviewerId, to: fallbackReviewerId },
            });
            await addSystemComment(
              companyId,
              issue.id,
              `Execution reconciler: **REVIEWER_REASSIGNED** — the nudged reviewer still has not returned a verdict after ${Math.round(conf.inReviewVerdictMs / 3_600_000)}h; the pending review stage now waits on the configured fallback reviewer.`,
            );
          }
          if (nextPolicy) result.reviewerReassigned += 1;
          continue;
        }
        const escalated = await ensureTransitionEscalationIncident(
          companyId,
          issue,
          "review_stall",
          `Transition deadline: issue \`${issue.identifier ?? issue.id}\` stayed \`in_review\` without a reviewer verdict for ${Math.round(conf.inReviewVerdictMs / 3_600_000)}h. The reviewer was nudged${fallbackReviewerId ? " and the fallback-reviewer reassignment was applied or unavailable" : ""}. The CEO loop owns review recovery from here.`,
        );
        if (escalated.created) result.escalatedToCeo += 1;
      }

      // Rule 3 — gated ship dispatch: an approved card waiting on a pending
      // release-lane stage for the ship window → dispatch the release lane.
      // Stage sequencing is the gate: a pending release stage is only
      // reachable once earlier review stages completed. Inert until a release
      // lane agent is configured.
      const releaseLaneId = conf.releaseLaneAgentId;
      if (releaseLaneId) {
        const shipCutoff = new Date(now.getTime() - conf.shipDispatchDelayMs);
        const shipCandidates = await db
          .select()
          .from(issues)
          .where(
            and(
              eq(issues.companyId, companyId),
              eq(issues.status, "in_review"),
              lt(issues.updatedAt, shipCutoff),
              visibleIssueCondition(),
            ),
          )
          .orderBy(asc(issues.updatedAt))
          .limit(25);
        for (const issue of shipCandidates) {
          if (isAdministrativeRecordIssue(issue.title, issue.description)) continue;
          if (processedCards.has(issue.id)) {
            result.skippedOneActionPerCard += 1;
            continue;
          }
          const pendingGate = parsePendingReviewParticipant(issue.executionPolicy);
          if (!pendingGate || pendingGate.reviewerAgentId !== releaseLaneId) continue;
          processedCards.add(issue.id);
          if ((await countLedgerActions(companyId, TRANSITION_SHIP_DISPATCH_LEDGER_ACTION, issue.id)) > 0) continue;
          if (unhealthyLanes.has(releaseLaneId)) {
            result.skippedLaneUnhealthy += 1;
            continue;
          }
          if (budget <= 0) continue;
          budget -= 1;
          if (!conf.dryRun) {
            await deps.enqueueWakeup(releaseLaneId, {
              reason: EXECUTION_RECONCILER_SHIP_DISPATCH_REASON,
              payload: { issueId: issue.id, gatedBy: "execution_policy_release_stage", waitingMs: conf.shipDispatchDelayMs },
              source: "automation",
              triggerDetail: "system",
              requestedByActorType: "system",
              requestedByActorId: "execution-reconciler",
            });
            await logActivity(db, {
              companyId,
              actorType: "system",
              actorId: "execution-reconciler",
              action: TRANSITION_SHIP_DISPATCH_LEDGER_ACTION,
              entityType: "issue",
              entityId: issue.id,
              issueId: issue.id,
              details: { rule: "ship_dispatch", releaseLaneAgentId: releaseLaneId },
            });
            await addSystemComment(
              companyId,
              issue.id,
              `Execution reconciler: **SHIP_DISPATCH** — approved work has waited on the release-lane stage for ${Math.round(conf.shipDispatchDelayMs / 3_600_000)}h; gated ship dispatched to the release lane (founder same-day-ship rule, made mechanical).`,
            );
          }
          result.shipDispatchQueued += 1;
        }
      }

      // Rule 4 — BLOCKED disposition deadline: blocked past the board's 24h
      // policy with no recorded owner notification → notify the owner once
      // (the "unblock it yourself or hand it to the CEO loop" receipt).
      const blockedCutoff = new Date(now.getTime() - conf.blockedDispositionMs);
      const blockedCandidates = await db
        .select()
        .from(issues)
        .where(
          and(
            eq(issues.companyId, companyId),
            eq(issues.status, "blocked"),
            isNotNull(issues.blockedTransitionAt),
            lt(issues.blockedTransitionAt, blockedCutoff),
            isNull(issues.blockedOwnerNotifiedAt),
            visibleIssueCondition(),
          ),
        )
        .orderBy(asc(issues.blockedTransitionAt))
        .limit(25);
      for (const issue of blockedCandidates) {
        if (processedCards.has(issue.id)) {
          result.skippedOneActionPerCard += 1;
          continue;
        }
        const ownerId = issue.assigneeAgentId ?? issue.createdByAgentId ?? boardOwnerId;
        if (!ownerId) continue;
        if (unhealthyLanes.has(ownerId)) {
          result.skippedLaneUnhealthy += 1;
          continue;
        }
        processedCards.add(issue.id);
        if (budget <= 0) continue;
        budget -= 1;
        if (!conf.dryRun) {
          await deps.enqueueWakeup(ownerId, {
            reason: EXECUTION_RECONCILER_BLOCKED_DISPOSITION_REASON,
            payload: { issueId: issue.id, blockedForMs: conf.blockedDispositionMs },
            source: "automation",
            triggerDetail: "system",
            requestedByActorType: "system",
            requestedByActorId: "execution-reconciler",
          });
          await db
            .update(issues)
            .set({ blockedOwnerNotifiedAt: now })
            .where(and(eq(issues.id, issue.id), eq(issues.companyId, companyId)));
          await logActivity(db, {
            companyId,
            actorType: "system",
            actorId: "execution-reconciler",
            action: TRANSITION_BLOCKED_DISPOSITION_LEDGER_ACTION,
            entityType: "issue",
            entityId: issue.id,
            issueId: issue.id,
            details: { rule: "blocked_disposition", notifiedAgentId: ownerId },
          });
          await addSystemComment(
            companyId,
            issue.id,
            `Execution reconciler: **BLOCKED_DISPOSITION** — this card has been blocked for more than ${Math.round(conf.blockedDispositionMs / 3_600_000)}h without a recorded disposition. Owner notified once: unblock it yourself, or hand it to the CEO loop with one comment naming the exact action, the owner who can do it, and what waiting costs.`,
          );
        }
        result.blockedDisposition += 1;
      }

      // Rule 5 — WAKE RECEIPTS: every reconciler wake must produce a run id
      // within the grace window, else it is marked dead and retried once via
      // the alternate self-wake path. A retry that also produces no receipt
      // escalates once to the CEO loop. No receipt = it did not happen.
      const receiptCutoff = new Date(now.getTime() - conf.wakeReceiptGraceMs);
      const deadWakeFilter = (retried: boolean) =>
        and(
          eq(agentWakeupRequests.companyId, companyId),
          eq(agentWakeupRequests.requestedByActorId, "execution-reconciler"),
          lt(agentWakeupRequests.requestedAt, receiptCutoff),
          ne(agentWakeupRequests.status, "cancelled"),
          isNull(agentWakeupRequests.runId),
          sql`NOT EXISTS (SELECT 1 FROM ${heartbeatRuns} WHERE ${heartbeatRuns.wakeupRequestId} = ${agentWakeupRequests.id})`,
          retried
            ? sql`(${agentWakeupRequests.payload} ->> 'receiptRetryAt') IS NOT NULL AND (${agentWakeupRequests.payload} ->> 'receiptRetryAt')::timestamptz < ${receiptCutoff.toISOString()}`
            : sql`(${agentWakeupRequests.payload} ->> 'receiptRetryAt') IS NULL`,
        );
      const deadWakes = await db
        .select()
        .from(agentWakeupRequests)
        .where(deadWakeFilter(false))
        .limit(15);
      for (const wake of deadWakes) {
        result.wakeReceiptsDead += 1;
        if (budget <= 0) continue;
        budget -= 1;
        const wakeIssueId = typeof wake.payload?.issueId === "string" ? wake.payload.issueId : null;
        if (!conf.dryRun) {
          await db
            .update(agentWakeupRequests)
            .set({
              status: "failed",
              error: "wake_receipt_dead: no run id within the grace window",
              payload: { ...(wake.payload ?? {}), receiptRetryAt: now.toISOString() },
              updatedAt: now,
            })
            .where(and(eq(agentWakeupRequests.id, wake.id), ne(agentWakeupRequests.status, "cancelled")));
          await logActivity(db, {
            companyId,
            actorType: "system",
            actorId: "execution-reconciler",
            action: TRANSITION_WAKE_DEAD_LEDGER_ACTION,
            entityType: wakeIssueId ? "issue" : "agent",
            entityId: wakeIssueId ?? wake.agentId,
            issueId: wakeIssueId ?? undefined,
            details: { wakeId: wake.id, reason: wake.reason, agentId: wake.agentId, issueId: wakeIssueId },
          });
          if (deps.wakeAgentDirectly) {
            const retryRun = await deps.wakeAgentDirectly(wake.agentId, {
              source: "on_demand",
              triggerDetail: "system",
              reason: EXECUTION_RECONCILER_WAKE_RECEIPT_RETRY_REASON,
              payload: { ...(wake.payload ?? {}), deadWakeId: wake.id },
              requestedByActorType: "system",
              requestedByActorId: "execution-reconciler",
            });
            if (retryRun?.id) result.wakeReceiptsRetried += 1;
          }
          if (wakeIssueId) {
            const [wakeIssue] = await db
              .select({ identifier: issues.identifier })
              .from(issues)
              .where(and(eq(issues.id, wakeIssueId), eq(issues.companyId, companyId)))
              .limit(1);
            if (wakeIssue && !isAdministrativeRecordIssue(wakeIssue.identifier ?? "", null)) {
              await addSystemComment(
                companyId,
                wakeIssueId,
                `Execution reconciler: **WAKE_RECEIPT_DEAD** — the prior wake produced no run id within ${Math.round(conf.wakeReceiptGraceMs / 60_000)} min, so it was marked dead and retried once via the alternate self-wake path. No receipt means it did not happen.`,
              );
            }
          }
        }
      }
      const lostRetries = await db
        .select()
        .from(agentWakeupRequests)
        .where(deadWakeFilter(true))
        .limit(10);
      for (const wake of lostRetries) {
        const wakeIssueId = typeof wake.payload?.issueId === "string" ? wake.payload.issueId : null;
        const target: TransitionEscalationTarget = wakeIssueId
          ? {
              id: wakeIssueId,
              identifier: wakeIssueId,
              projectId: null,
              parentId: null,
              title: `wake receipt lost for ${wakeIssueId}`,
            }
          : {
              id: `agent:${wake.agentId}`,
              identifier: `agent:${wake.agentId}`,
              projectId: null,
              parentId: null,
              title: `wake receipt lost for agent ${wake.agentId}`,
            };
        const escalated = await ensureTransitionEscalationIncident(
          companyId,
          target,
          "wake_receipt_loss",
          `Transition deadline: wake \`${wake.id}\` (${wake.reason ?? "unknown"}) produced no run id within ${Math.round(conf.wakeReceiptGraceMs / 60_000)} min, and the alternate-path retry also produced no receipt. The CEO loop owns lane recovery from here.`,
        );
        if (escalated.created) result.escalatedToCeo += 1;
      }
    }

    if (
      result.reawakeOwner > 0 ||
      result.reviewerNudged > 0 ||
      result.reviewerReassigned > 0 ||
      result.shipDispatchQueued > 0 ||
      result.blockedDisposition > 0 ||
      result.wakeReceiptsDead > 0 ||
      result.escalatedToCeo > 0 ||
      result.lanesReAdmitted > 0
    ) {
      logger.warn({ ...result }, "execution reconciler transition scan took corrective action");
    }
    return result;
  }

  return {
    reconcileExecutionState,
    scanProgramStalls,
    reconcileTransitionDeadlines,
    buildProgramSummary,
  };
}
