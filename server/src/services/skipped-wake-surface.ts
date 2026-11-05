import { logger } from "../middleware/logger.js";

/**
 * Surfacing for silently-skipped wakes (SON-6222).
 *
 * Several enqueue-time guards and dispatch gates finalize a wake request as
 * `skipped` (issue-state guard mismatch, assignee change, stale queued run,
 * dependency gate, daily cap). The wake diagnostics endpoint exposes these,
 * but nothing annotated the affected issue, so assigned work could look
 * in-progress while its collection path was dead.
 *
 * This helper posts one concise system comment on the affected issue and
 * suppresses repeat notices for the same issue within a bounded window so
 * retry loops cannot spam the thread. All effects are injected so the
 * dedupe/escalation logic is unit-testable in isolation.
 */

export const DEFAULT_SKIPPED_WAKE_SURFACE_WINDOW_MINUTES = 360;

export const SKIPPED_WAKE_COMMENT_PREFIX = "Paperclip skipped a wake to";

export interface SkippedWakeSurfaceInput {
  companyId: string;
  issueId: string;
  agentId: string | null;
  agentName: string | null;
  agentStatus: string | null;
  skipCode: string;
  reason: string;
  wakeSource?: string | null;
  runId?: string | null;
}

export interface SkippedWakeSurfaceDeps {
  issueExists: (companyId: string, issueId: string) => Promise<boolean>;
  hasRecentSkippedWakeComment: (
    issueId: string,
    since: Date,
  ) => Promise<boolean>;
  addComment: (
    issueId: string,
    body: string,
    actor: { agentId?: string; runId?: string | null },
  ) => Promise<unknown>;
  windowMinutes?: number;
  now?: () => Date;
}

export interface SkippedWakeSurfaceResult {
  commented: boolean;
  deduped: boolean;
}

export async function surfaceSkippedWake(
  deps: SkippedWakeSurfaceDeps,
  input: SkippedWakeSurfaceInput,
): Promise<SkippedWakeSurfaceResult> {
  const now = deps.now ?? (() => new Date());
  const windowMinutes =
    deps.windowMinutes ?? DEFAULT_SKIPPED_WAKE_SURFACE_WINDOW_MINUTES;

  try {
    if (!(await deps.issueExists(input.companyId, input.issueId))) {
      return { commented: false, deduped: false };
    }

    const windowStart = new Date(now().getTime() - windowMinutes * 60 * 1000);
    if (await deps.hasRecentSkippedWakeComment(input.issueId, windowStart)) {
      return { commented: false, deduped: true };
    }

    const agentLabel = input.agentName ?? input.agentId ?? "the addressee lane";
    const lines = [
      `${SKIPPED_WAKE_COMMENT_PREFIX} ${agentLabel} — it was not delivered.`,
      `Reason: ${input.reason} (${input.skipCode})${input.wakeSource ? ` · source: ${input.wakeSource}` : ""}`,
    ];
    if (input.agentStatus === "error") {
      lines.push(
        "The addressee lane is in error status (latched adapter fault). Recovery: the stale-error sweep clears it automatically after the bounded interval, or the CEO lane can clear/retry it now (audit-logged).",
      );
    }
    lines.push(
      `Repeat notices for this issue are suppressed for ${windowMinutes} min.`,
    );

    await deps.addComment(
      input.issueId,
      lines.join("\n"),
      input.agentId
        ? { agentId: input.agentId, runId: input.runId ?? null }
        : {},
    );
    return { commented: true, deduped: false };
  } catch (err) {
    logger.warn(
      { err, issueId: input.issueId, agentId: input.agentId },
      "failed to surface skipped wake on the affected issue",
    );
    return { commented: false, deduped: false };
  }
}
