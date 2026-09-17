import { and, desc, eq, inArray, isNull, ne, notInArray, sql } from "drizzle-orm";
import { alias } from "drizzle-orm/pg-core";
import type { Db } from "@paperclipai/db";
import {
  activityLog,
  agentWakeupRequests,
  approvals,
  heartbeatRuns,
  issueApprovals,
  issueRelations,
  issueThreadInteractions,
  issues,
  routines,
} from "@paperclipai/db";
import type { SuccessfulRunHandoffState } from "@paperclipai/shared";
import { logActivity } from "./activity-log.js";
import { visibleIssueCondition } from "./issue-visibility.js";
import { RECOVERY_ORIGIN_KINDS } from "./recovery/origins.js";
import { successfulRunHandoffSatisfiedByState } from "./recovery/successful-run-handoff.js";

export const SUCCESSFUL_RUN_HANDOFF_LIVE_RUN_STATUSES = ["queued", "running", "scheduled_retry"] as const;
export const SUCCESSFUL_RUN_HANDOFF_LIVE_WAKE_STATUSES = ["queued", "deferred_issue_execution", "claimed"] as const;

const heartbeatRunIssueId = sql<string>`coalesce(
  ${heartbeatRuns.contextSnapshot} ->> 'issueId',
  ${heartbeatRuns.contextSnapshot} ->> 'taskId'
)`;

const wakeRequestIssueId = sql<string>`coalesce(
  ${agentWakeupRequests.payload} ->> 'issueId',
  ${agentWakeupRequests.payload} ->> 'taskId',
  ${agentWakeupRequests.payload} -> '_paperclipWakeContext' ->> 'issueId',
  ${agentWakeupRequests.payload} -> '_paperclipWakeContext' ->> 'taskId'
)`;

export async function hydrateSuccessfulRunHandoffLiveness(
  dbOrTx: any,
  companyId: string,
  states: Map<string, SuccessfulRunHandoffState>,
) {
  const unresolvedIssueIds = [...states.entries()]
    .filter(([, state]) => state.state === "required" || state.state === "escalated")
    .map(([issueId]) => issueId);
  if (unresolvedIssueIds.length === 0) return states;

  const [activeRuns, activeWakes] = await Promise.all([
    dbOrTx
      .select({ id: heartbeatRuns.id, issueId: heartbeatRunIssueId })
      .from(heartbeatRuns)
      .where(and(
        eq(heartbeatRuns.companyId, companyId),
        inArray(heartbeatRuns.status, [...SUCCESSFUL_RUN_HANDOFF_LIVE_RUN_STATUSES]),
        inArray(heartbeatRunIssueId, unresolvedIssueIds),
      )),
    dbOrTx
      .select({ issueId: wakeRequestIssueId })
      .from(agentWakeupRequests)
      .where(and(
        eq(agentWakeupRequests.companyId, companyId),
        inArray(agentWakeupRequests.status, [...SUCCESSFUL_RUN_HANDOFF_LIVE_WAKE_STATUSES]),
        inArray(wakeRequestIssueId, unresolvedIssueIds),
      )),
  ]);

  const liveRunByIssueId = new Map<string, string>();
  for (const row of activeRuns as Array<{ id: string; issueId: string | null }>) {
    if (row.issueId && !liveRunByIssueId.has(row.issueId)) liveRunByIssueId.set(row.issueId, row.id);
  }
  const liveWakeIssueIds = new Set(
    (activeWakes as Array<{ issueId: string | null }>)
      .map((row) => row.issueId)
      .filter((issueId): issueId is string => Boolean(issueId)),
  );

  for (const issueId of unresolvedIssueIds) {
    const state = states.get(issueId);
    if (!state) continue;
    const liveRunId = liveRunByIssueId.get(issueId);
    states.set(issueId, {
      ...state,
      hasLiveContinuation: Boolean(liveRunId || liveWakeIssueIds.has(issueId)),
      ...(liveRunId ? { liveRunId } : {}),
    });
  }

  return states;
}

export async function resolveRequiredSuccessfulRunHandoffOnValidPath(
  db: Db,
  input: {
    companyId: string;
    issueId: string;
    issueIdentifier: string | null;
    agentId: string;
    runId: string;
    skipReason: string;
  },
) {
  const latestHandoff = await db
    .select({ action: activityLog.action, runId: activityLog.runId, details: activityLog.details })
    .from(activityLog)
    .where(and(
      eq(activityLog.companyId, input.companyId),
      eq(activityLog.entityType, "issue"),
      eq(activityLog.entityId, input.issueId),
      inArray(activityLog.action, [
        "issue.successful_run_handoff_required",
        "issue.successful_run_handoff_resolved",
        "issue.successful_run_handoff_escalated",
      ]),
    ))
    .orderBy(desc(activityLog.createdAt), desc(activityLog.id))
    .limit(1)
    .then((rows) => rows[0] ?? null);
  if (latestHandoff?.action !== "issue.successful_run_handoff_required") return false;

  const details = latestHandoff.details && typeof latestHandoff.details === "object"
    ? latestHandoff.details as Record<string, unknown>
    : {};
  const sourceRunId = [details.sourceRunId, details.source_run_id, details.resumeFromRunId]
    .find((value): value is string => typeof value === "string" && value.trim().length > 0)
    ?.trim() ?? latestHandoff.runId;
  await logActivity(db, {
    companyId: input.companyId,
    actorType: "system",
    actorId: "heartbeat",
    agentId: input.agentId,
    runId: input.runId,
    action: "issue.successful_run_handoff_resolved",
    entityType: "issue",
    entityId: input.issueId,
    details: {
      label: "Successful run handoff continuation confirmed",
      sourceRunId,
      resolvedByRunId: input.runId,
      resolvedBySkipReason: input.skipReason,
      issue: { id: input.issueId, identifier: input.issueIdentifier },
    },
  });
  return true;
}

export type SuccessfulRunHandoffFreshIssueRow = Pick<
  typeof issues.$inferSelect,
  | "id"
  | "companyId"
  | "identifier"
  | "status"
  | "assigneeAgentId"
  | "assigneeUserId"
  | "executionState"
  | "monitorNextCheckAt"
  | "originKind"
>;

// SON-2251: fresh-state re-verification for the recovery sweep's exhausted
// successful-run handoff escalation. The enqueue-side judge checks these
// signals right before enqueuing the corrective wake; the sweep must check
// them again (against a fresh issue read) before escalating, because the
// corrective run may have recorded a valid disposition after the sweep's
// snapshot was taken. Signals mirror decideSuccessfulRunHandoff's skips.
export async function detectSuccessfulRunHandoffValidPath(
  dbOrTx: any,
  input: {
    companyId: string;
    issueId: string;
    agentId: string | null;
    excludeRunId?: string | null;
    hasPauseHold: boolean;
  },
): Promise<{
  satisfied: boolean;
  reason: string | null;
  issue: SuccessfulRunHandoffFreshIssueRow | null;
}> {
  const [issueRow] = await dbOrTx
    .select({
      id: issues.id,
      companyId: issues.companyId,
      identifier: issues.identifier,
      status: issues.status,
      assigneeAgentId: issues.assigneeAgentId,
      assigneeUserId: issues.assigneeUserId,
      executionState: issues.executionState,
      monitorNextCheckAt: issues.monitorNextCheckAt,
      originKind: issues.originKind,
    })
    .from(issues)
    .where(and(eq(issues.id, input.issueId), eq(issues.companyId, input.companyId)))
    .limit(1);
  if (!issueRow) return { satisfied: false, reason: null, issue: null };

  const blocker = alias(issues, "blocker");
  const [activeExecutionPath, queuedWake, pendingInteraction, pendingApproval, explicitBlocker, openRecoveryIssue, activeRoutineContinuation] =
    await Promise.all([
      input.agentId
        ? dbOrTx
            .select({ id: heartbeatRuns.id })
            .from(heartbeatRuns)
            .where(and(
              eq(heartbeatRuns.companyId, input.companyId),
              eq(heartbeatRuns.agentId, input.agentId),
              inArray(heartbeatRuns.status, [...SUCCESSFUL_RUN_HANDOFF_LIVE_RUN_STATUSES]),
              inArray(heartbeatRunIssueId, [input.issueId]),
              ...(input.excludeRunId ? [ne(heartbeatRuns.id, input.excludeRunId)] : []),
            ))
            .limit(1)
            .then((rows: Array<{ id: string }>) => rows[0] ?? null)
        : Promise.resolve(null),
      input.agentId
        ? dbOrTx
            .select({ id: agentWakeupRequests.id })
            .from(agentWakeupRequests)
            .where(and(
              eq(agentWakeupRequests.companyId, input.companyId),
              eq(agentWakeupRequests.agentId, input.agentId),
              inArray(agentWakeupRequests.status, [...SUCCESSFUL_RUN_HANDOFF_LIVE_WAKE_STATUSES]),
              eq(wakeRequestIssueId, input.issueId),
            ))
            .limit(1)
            .then((rows: Array<{ id: string }>) => rows[0] ?? null)
        : Promise.resolve(null),
      dbOrTx
        .select({ id: issueThreadInteractions.id })
        .from(issueThreadInteractions)
        .where(and(
          eq(issueThreadInteractions.companyId, input.companyId),
          eq(issueThreadInteractions.issueId, input.issueId),
          eq(issueThreadInteractions.status, "pending"),
        ))
        .limit(1)
        .then((rows: Array<{ id: string }>) => rows[0] ?? null),
      dbOrTx
        .select({ id: issueApprovals.approvalId })
        .from(issueApprovals)
        .innerJoin(approvals, eq(issueApprovals.approvalId, approvals.id))
        .where(and(
          eq(issueApprovals.companyId, input.companyId),
          eq(issueApprovals.issueId, input.issueId),
          inArray(approvals.status, ["pending", "revision_requested"]),
        ))
        .limit(1)
        .then((rows: Array<{ id: string }>) => rows[0] ?? null),
      dbOrTx
        .select({ id: issueRelations.issueId })
        .from(issueRelations)
        .innerJoin(blocker, eq(blocker.id, issueRelations.issueId))
        .where(and(
          eq(issueRelations.companyId, input.companyId),
          eq(issueRelations.relatedIssueId, input.issueId),
          eq(issueRelations.type, "blocks"),
          notInArray(blocker.status, ["done", "cancelled"]),
          isNull(blocker.hiddenAt),
        ))
        .limit(1)
        .then((rows: Array<{ id: string }>) => rows[0] ?? null),
      dbOrTx
        .select({ id: issues.id })
        .from(issues)
        .where(and(
          eq(issues.companyId, input.companyId),
          inArray(issues.originKind, [
            RECOVERY_ORIGIN_KINDS.strandedIssueRecovery,
            RECOVERY_ORIGIN_KINDS.issueGraphLivenessEscalation,
          ]),
          eq(issues.originId, input.issueId),
          visibleIssueCondition(),
          notInArray(issues.status, ["done", "cancelled"]),
        ))
        .limit(1)
        .then((rows: Array<{ id: string }>) => rows[0] ?? null),
      dbOrTx
        .select({ id: routines.id })
        .from(routines)
        .where(and(
          eq(routines.companyId, input.companyId),
          eq(routines.parentIssueId, input.issueId),
          eq(routines.status, "active"),
        ))
        .limit(1)
        .then((rows: Array<{ id: string }>) => rows[0] ?? null),
    ]);

  const verdict = successfulRunHandoffSatisfiedByState({
    status: issueRow.status,
    executionState: issueRow.executionState,
    monitorNextCheckAt: issueRow.monitorNextCheckAt ?? null,
    hasActiveExecutionPath: Boolean(activeExecutionPath),
    hasQueuedWake: Boolean(queuedWake),
    hasPendingInteractionOrApproval: Boolean(pendingInteraction || pendingApproval),
    hasExplicitBlockerPath: Boolean(explicitBlocker),
    hasOpenRecoveryIssue: Boolean(openRecoveryIssue),
    hasPauseHold: input.hasPauseHold,
    hasActiveRoutineContinuation: Boolean(activeRoutineContinuation),
  });
  return {
    satisfied: verdict.satisfied,
    reason: verdict.satisfied ? verdict.reason : null,
    issue: issueRow,
  };
}
