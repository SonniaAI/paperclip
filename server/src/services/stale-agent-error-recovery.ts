import { logger } from "../middleware/logger.js";

/**
 * Supervised recovery for latched agent error states (SON-6222).
 *
 * An agent lane enters `status = "error"` when a run fails or times out
 * (see heartbeat finalizeAgentStatus). The latch is deliberate — it stops
 * repeated failed dispatches — but recovery was board-only
 * (`POST /agents/:id/clear-error`), so a lane whose periodic heartbeat is
 * disabled can stay latched forever even after the fault is long gone
 * (e.g. a killed OpenClaw gateway run session).
 *
 * This reconciler clears the latch once the error is provably stale:
 *   - the agent is still in `error` status,
 *   - no heartbeat run is queued / running / scheduled for the agent
 *     (an active run settles the status by itself on completion),
 *   - the latch has been held longer than the bounded stale interval.
 *
 * After clearing, one on-demand wake is enqueued (best effort, idempotent)
 * so wakeOnDemand lanes get an immediate dispatch attempt instead of
 * waiting for their next assignment. All effects are injected so the sweep
 * logic is unit-testable in isolation.
 */

export const DEFAULT_STALE_AGENT_ERROR_CLEAR_MINUTES = 240;

export function resolveStaleAgentErrorClearMinutes(
  env: NodeJS.ProcessEnv = process.env,
): number {
  const raw = env.PAPERCLIP_STALE_AGENT_ERROR_CLEAR_MINUTES;
  if (raw === undefined || raw.trim() === "") {
    return DEFAULT_STALE_AGENT_ERROR_CLEAR_MINUTES;
  }
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return DEFAULT_STALE_AGENT_ERROR_CLEAR_MINUTES;
  }
  return Math.floor(parsed);
}

export interface StaleErroredAgentRow {
  id: string;
  companyId: string;
  name: string;
  status: string;
  errorReason: string | null;
  lastHeartbeatAt: Date | null;
}

export interface StaleErroredAgentRecoveryResult {
  scanned: number;
  cleared: number;
  wakeQueued: number;
  agentIds: string[];
  skippedActiveRuns: number;
  skippedRecent: number;
}

export interface ReconcileStaleErroredAgentsDeps {
  listErroredAgents: () => Promise<StaleErroredAgentRow[]>;
  hasActiveRuns: (agentId: string) => Promise<boolean>;
  clearError: (agentId: string) => Promise<unknown>;
  enqueueWakeup: (agentId: string, opts: {
    source: "automation";
    triggerDetail?: "system" | "manual" | "ping" | "callback";
    reason?: string | null;
    idempotencyKey?: string | null;
    requestedByActorType?: "system" | "agent" | "user";
    requestedByActorId?: string | null;
  }) => Promise<unknown>;
  logActivity: (input: {
    companyId: string;
    actorType: "system";
    actorId: string;
    action: string;
    entityType: string;
    entityId: string;
    details?: Record<string, unknown> | null;
  }) => Promise<unknown>;
  publishLiveEvent: (event: {
    companyId: string;
    type: string;
    payload: Record<string, unknown>;
  }) => void;
  clearMinutes?: number;
  now?: () => Date;
}

export async function reconcileStaleErroredAgents(
  deps: ReconcileStaleErroredAgentsDeps,
): Promise<StaleErroredAgentRecoveryResult> {
  const now = deps.now ?? (() => new Date());
  const clearMinutes = deps.clearMinutes ?? resolveStaleAgentErrorClearMinutes();
  const result: StaleErroredAgentRecoveryResult = {
    scanned: 0,
    cleared: 0,
    wakeQueued: 0,
    agentIds: [],
    skippedActiveRuns: 0,
    skippedRecent: 0,
  };
  // 0 or negative disables the sweep entirely.
  if (clearMinutes <= 0) return result;

  const staleBefore = new Date(now().getTime() - clearMinutes * 60 * 1000);
  const errored = await deps.listErroredAgents();
  result.scanned = errored.length;

  for (const agent of errored) {
    // Only clear provably stale latches. A missing lastHeartbeatAt means we
    // cannot date the latch; leave it to the board path.
    if (!agent.lastHeartbeatAt || agent.lastHeartbeatAt > staleBefore) {
      result.skippedRecent += 1;
      continue;
    }

    if (await deps.hasActiveRuns(agent.id)) {
      result.skippedActiveRuns += 1;
      continue;
    }

    const staleMinutes = Math.round(
      (now().getTime() - agent.lastHeartbeatAt.getTime()) / 60000,
    );
    try {
      await deps.clearError(agent.id);
    } catch (err) {
      // Status flipped concurrently (board clear, run finalization); the
      // sweep is idempotent and will simply find nothing next tick.
      logger.info(
        { err, agentId: agent.id },
        "stale agent error recovery lost the clear race",
      );
      continue;
    }

    result.cleared += 1;
    result.agentIds.push(agent.id);

    try {
      await deps.logActivity({
        companyId: agent.companyId,
        actorType: "system",
        actorId: "stale-error-recovery",
        action: "agent.error_auto_cleared",
        entityType: "agent",
        entityId: agent.id,
        details: {
          reason: "stale_error_auto_recovery",
          previousStatus: "error",
          errorReason: agent.errorReason ?? null,
          latchedForMinutes: staleMinutes,
          staleThresholdMinutes: clearMinutes,
        },
      });
    } catch (err) {
      logger.warn(
        { err, agentId: agent.id },
        "stale agent error recovery failed to log activity",
      );
    }

    try {
      deps.publishLiveEvent({
        companyId: agent.companyId,
        type: "agent.status",
        payload: {
          agentId: agent.id,
          status: "idle",
          lastHeartbeatAt: agent.lastHeartbeatAt.toISOString(),
          outcome: "stale_error_auto_cleared",
        },
      });
    } catch (err) {
      logger.warn(
        { err, agentId: agent.id },
        "stale agent error recovery failed to publish live event",
      );
    }

    // Best-effort dispatch attempt so wakeOnDemand lanes resume immediately.
    try {
      const run = await deps.enqueueWakeup(agent.id, {
        source: "automation",
        triggerDetail: "system",
        reason: "stale_error_recovery",
        idempotencyKey: `stale_error_recovery:${agent.id}:${agent.lastHeartbeatAt.getTime()}`,
        requestedByActorType: "system",
        requestedByActorId: "stale-error-recovery",
      });
      if (run) {
        result.wakeQueued += 1;
      }
    } catch (err) {
      logger.warn(
        { err, agentId: agent.id },
        "stale agent error recovery failed to enqueue recovery wake",
      );
    }

    logger.warn(
      {
        agentId: agent.id,
        companyId: agent.companyId,
        latchedForMinutes: staleMinutes,
        errorReason: agent.errorReason ?? null,
      },
      "stale agent error recovery cleared a latched lane error",
    );
  }

  return result;
}
