import {
  and,
  asc,
  eq,
  gt,
  gte,
  inArray,
  isNotNull,
  like,
  ne,
  notInArray,
  notLike,
  or,
  sql,
} from "drizzle-orm";
import {
  activityLog,
  agents,
  heartbeatRuns,
  issueComments,
  issues,
  laneFailureWatchdogState,
} from "@paperclipai/db";
import type { Db } from "@paperclipai/db";
import {
  applyCounterState,
  createEmptyCounterState,
  normalizeLedgerRecord,
  normalizeSignalClass,
  parseCounterState,
  type CounterState,
  type NormalizedLedgerRecord,
  type RecoveryClearEvent,
  type ThresholdConfig,
  type ThresholdCrossingEvent,
} from "../vendor/lane-failure-counter/index.js";
import { logger } from "../middleware/logger.js";
import { logActivity } from "./activity-log.js";

/**
 * Fleet lane-failure watchdog — SON-1440 WP-B sink delivery.
 *
 * Consumes terminal `heartbeat_runs` (plus Class C reconcile-stranding
 * activity rows) through the pure SON-1536 lane-failure-counter reducer and
 * posts exactly-once threshold-crossing alerts and correlated recovery clears
 * to the Operations sink issue (default `SON-1334`). Routing policy per
 * SON-1317: alerts land with Operations on the sink stream — never via the
 * failing lane's agent; these comments are authored by the server itself.
 *
 * Delivery semantics: emissions are inserted BEFORE the reducer state +
 * cursor persist, so a crash between insert and persist re-emits on the next
 * tick (at-least-once). The reducer's persisted crossing latch dedups every
 * other case (restart, replay, duplicate run ids).
 *
 * Zero-write deaths (Class B) are counted as their own signal class and never
 * folded into generic Class A: a terminal non-succeeded run with zero
 * activity_log rows beyond `environment.lease*` / `heartbeat.invoked` is the
 * "died before any write" archaeology-cost shape.
 */

const CLASS_C_CAUSES = new Set(["successful_run_missing_state", "stranded_assigned_issue"]);

/**
 * Reconcile action names drift from the canonical stranding cause codes
 * (e.g. the handoff reconciler action carries an extra "handoff" segment);
 * map the known suffix aliases onto the frozen Class C causes.
 */
const CAUSE_FROM_ACTION_ALIAS: Record<string, string> = {
  successful_run_handoff_missing_state: "successful_run_missing_state",
};

const RECONCILE_ACTION_PATTERN = "recovery.reconcile_%";

/** Run statuses that never classify as failures and are never counted. */
const ACTIVE_RUN_STATUSES = ["queued", "running"];

const DEFAULT_SINK_IDENTIFIER = "SON-1334";
const DEFAULT_INTERVAL_MS = 30 * 60 * 1000;
const MIN_INTERVAL_MS = 60 * 1000;
const MAX_BATCH = 500;
const COMPANY_LOOKBACK_MS = 7 * 24 * 60 * 60 * 1000;

export interface LaneWatchdogConfig {
  enabled: boolean;
  sinkIdentifier: string;
  intervalMs: number;
  thresholds: ThresholdConfig;
}

export interface LaneWatchdogTickResult {
  enabled: boolean;
  companiesChecked: number;
  runsProcessed: number;
  strandingsProcessed: number;
  alertsEmitted: number;
  clearsEmitted: number;
}

interface CursorPosition {
  ts: string;
  id: string;
}

interface WatchdogCursor {
  runs?: CursorPosition;
  strandings?: CursorPosition;
}

export function resolveLaneWatchdogConfig(env: NodeJS.ProcessEnv = process.env): LaneWatchdogConfig {
  const disabled = /^(1|true|yes)$/i.test((env.PAPERCLIP_LANE_WATCHDOG_DISABLED ?? "").trim());
  const thresholds: ThresholdConfig = {};
  const rawThresholds = (env.PAPERCLIP_LANE_WATCHDOG_THRESHOLDS ?? "").trim();
  if (rawThresholds) {
    try {
      const parsed = JSON.parse(rawThresholds) as Record<string, unknown>;
      for (const [key, value] of Object.entries(parsed)) {
        const signalClass = normalizeSignalClass(key);
        const threshold = typeof value === "number" ? value : Number(value);
        if (signalClass && Number.isInteger(threshold) && threshold > 0) {
          thresholds[signalClass] = threshold;
        }
      }
    } catch {
      logger.warn({ raw: rawThresholds }, "lane watchdog: invalid PAPERCLIP_LANE_WATCHDOG_THRESHOLDS; using defaults");
    }
  }
  const rawInterval = Number(env.PAPERCLIP_LANE_WATCHDOG_INTERVAL_MS);
  return {
    enabled: !disabled,
    sinkIdentifier: (env.PAPERCLIP_LANE_WATCHDOG_SINK_IDENTIFIER ?? "").trim() || DEFAULT_SINK_IDENTIFIER,
    intervalMs:
      Number.isFinite(rawInterval) && rawInterval >= MIN_INTERVAL_MS ? rawInterval : DEFAULT_INTERVAL_MS,
    thresholds,
  };
}

/**
 * Build one reducer ledger record from a terminal heartbeat run.
 * `realWriteCount` excludes `environment.lease*` / `heartbeat.invoked`
 * activity rows. Zero-write is only asserted for non-succeeded runs — a
 * succeeded run with zero writes is an ordinary success (it resets streaks).
 */
export function recordFromHeartbeatRun(
  run: { id: string; agentId: string; status: string; finishedAt: Date | null },
  realWriteCount: number,
): Record<string, unknown> {
  return {
    run_id: run.id,
    lane: run.agentId,
    status: run.status,
    zero_write: run.status !== "succeeded" && realWriteCount === 0,
    finished_at: run.finishedAt ? run.finishedAt.toISOString() : null,
  };
}

/**
 * Build a Class C ledger record from a reconcile-stranding activity row, or
 * return null when the row is not a lane-failure stranding signal.
 */
export function recordFromStrandingActivity(row: {
  id: string;
  agentId: string | null;
  action: string;
  entityId: string;
  details: Record<string, unknown> | null;
  createdAt: Date;
}): Record<string, unknown> | null {
  if (!row.agentId) return null;
  const detailsCause =
    row.details && typeof row.details.recoveryCause === "string" ? row.details.recoveryCause : null;
  const actionSuffix = row.action.replace(/^recovery\.reconcile_/, "");
  const cause = detailsCause ?? CAUSE_FROM_ACTION_ALIAS[actionSuffix] ?? actionSuffix;
  if (!CLASS_C_CAUSES.has(cause)) return null;
  return {
    run_id: row.id,
    lane: row.agentId,
    outcome: "success",
    signal_class: "C",
    cause_code: cause,
    issue_id: row.entityId,
    finished_at: row.createdAt.toISOString(),
  };
}

export function alertCommentBody(event: ThresholdCrossingEvent, laneName?: string | null): string {
  const lane = laneName ? `${laneName} (\`${event.lane_id}\`)` : `\`${event.lane_id}\``;
  const lines = [
    `**fleet-watchdog alert — Class ${event.class} (${event.signal}) tripped on lane ${lane}**`,
    "",
    `- consecutive failures: **${event.count}** (threshold ${event.threshold})`,
    `- first failure: ${event.first_failure_ts} · last failure: ${event.last_failure_ts}`,
  ];
  if (event.issue_id) lines.push(`- stranded issue: \`${event.issue_id}\``);
  if (event.cause_code) lines.push(`- cause: ${event.cause_code}`);
  if (event.run_id) lines.push(`- crossing run: \`${event.run_id}\``);
  lines.push(
    "",
    `Producer: lane-failure-watchdog v1 (SON-1440 WP-B) · emitted ${event.emitted_at}.`,
    "Triage per sink protocol: informational-first; confirm the lane's assigned work isn't stranded; ping the lane's owning lead — never route via the failing lane's agent.",
    "This alert dedups while the streak persists; any successful run on the lane auto-clears it (recovery notice follows).",
  );
  return lines.join("\n");
}

export function recoveryCommentBody(event: RecoveryClearEvent, laneName?: string | null): string {
  const lane = laneName ? `${laneName} (\`${event.lane_id}\`)` : `\`${event.lane_id}\``;
  const lines = [
    `**fleet-watchdog recovery — lane ${lane} cleared (Class ${event.class} ${event.signal})**`,
    "",
    `- cleared streak: ${event.count} (threshold ${event.threshold})`,
    `- alert emitted: ${event.alert_emitted_at ?? "unknown"} · cleared: ${event.emitted_at}`,
  ];
  if (event.run_id) lines.push(`- clearing run: \`${event.run_id}\``);
  if (event.issue_id) lines.push(`- related stranded issue: \`${event.issue_id}\``);
  lines.push("", `Producer: lane-failure-watchdog v1 (SON-1440 WP-C) · emitted ${event.emitted_at}.`);
  return lines.join("\n");
}

let tickInFlight = false;

/**
 * Normalize raw ledger-shaped records through the reducer's own entry point so
 * classification precedence (zero_write > terminal_fail > success_readonly >
 * success > other) stays frozen. Records without run_id/lane are dropped and
 * counted as invalid.
 */
function toNormalizedRecords(raw: Record<string, unknown>[]): {
  records: NormalizedLedgerRecord[];
  invalid: number;
} {
  const records: NormalizedLedgerRecord[] = [];
  let invalid = 0;
  for (const [sourceIndex, value] of raw.entries()) {
    const normalized = normalizeLedgerRecord(value, sourceIndex);
    if (normalized.record) records.push(normalized.record);
    else invalid += 1;
  }
  return { records, invalid };
}

/**
 * One watchdog pass over all companies with recent terminal runs. Safe to
 * call on an interval: re-entrant ticks are dropped, and every failure is
 * contained (the tick must never break the caller's loop).
 */
export async function tickLaneFailureWatchdog(db: Db, now: Date = new Date()): Promise<LaneWatchdogTickResult> {
  const config = resolveLaneWatchdogConfig();
  const result: LaneWatchdogTickResult = {
    enabled: config.enabled,
    companiesChecked: 0,
    runsProcessed: 0,
    strandingsProcessed: 0,
    alertsEmitted: 0,
    clearsEmitted: 0,
  };
  if (!config.enabled || tickInFlight) return result;
  tickInFlight = true;
  try {
    const since = new Date(now.getTime() - COMPANY_LOOKBACK_MS);
    const companies = await db
      .selectDistinct({ companyId: heartbeatRuns.companyId })
      .from(heartbeatRuns)
      .where(and(isNotNull(heartbeatRuns.finishedAt), gte(heartbeatRuns.finishedAt, since)));
    for (const { companyId } of companies) {
      try {
        const done = await tickCompanyWatchdog(db, companyId, config, now);
        result.companiesChecked += 1;
        result.runsProcessed += done.runsProcessed;
        result.strandingsProcessed += done.strandingsProcessed;
        result.alertsEmitted += done.alertsEmitted;
        result.clearsEmitted += done.clearsEmitted;
      } catch (err) {
        logger.error({ err, companyId }, "lane watchdog: company tick failed");
      }
    }
  } finally {
    tickInFlight = false;
  }
  return result;
}

/**
 * SON-2066: lower bound for the terminal-run window. A persisted run cursor
 * continues strictly after it; a fresh state row starts at the 7-day lookback
 * instead of genesis, so first boot or state-row loss cannot blind current
 * alerts behind a months-long backlog crawl or replay stale crossings at the
 * sink.
 */
export function runWindowFloor(cursor: WatchdogCursor, now: Date): Date | null {
  return cursor.runs ? null : new Date(now.getTime() - COMPANY_LOOKBACK_MS);
}

async function tickCompanyWatchdog(
  db: Db,
  companyId: string,
  config: LaneWatchdogConfig,
  now: Date,
): Promise<Omit<LaneWatchdogTickResult, "enabled" | "companiesChecked">> {
  const empty = { runsProcessed: 0, strandingsProcessed: 0, alertsEmitted: 0, clearsEmitted: 0 };
  const sinkIssueId = await resolveSinkIssueId(db, companyId, config.sinkIdentifier);
  if (!sinkIssueId) {
    logger.warn({ companyId, sink: config.sinkIdentifier }, "lane watchdog: sink issue not found; skipping company");
    return empty;
  }

  const [stateRow] = await db
    .select({ state: laneFailureWatchdogState.state, cursor: laneFailureWatchdogState.cursor })
    .from(laneFailureWatchdogState)
    .where(eq(laneFailureWatchdogState.companyId, companyId))
    .limit(1);
  let state: CounterState;
  if (stateRow?.state) {
    try {
      state = parseCounterState(stateRow.state);
    } catch (err) {
      logger.warn({ err, companyId }, "lane watchdog: unreadable persisted state; starting fresh");
      state = createEmptyCounterState(now);
    }
  } else {
    state = createEmptyCounterState(now);
  }
  const cursor = (stateRow?.cursor ?? {}) as WatchdogCursor;

  const runConds = [
    eq(heartbeatRuns.companyId, companyId),
    isNotNull(heartbeatRuns.finishedAt),
    notInArray(heartbeatRuns.status, ACTIVE_RUN_STATUSES),
  ];
  if (cursor.runs) {
    runConds.push(
      or(
        gt(heartbeatRuns.finishedAt, new Date(cursor.runs.ts)),
        and(
          eq(heartbeatRuns.finishedAt, new Date(cursor.runs.ts)),
          gt(heartbeatRuns.id, cursor.runs.id),
        ),
      )!,
    );
  }
  // SON-2066: with no persisted run cursor (first boot, rolled or lost state
  // row), bound the first pass to the lookback window instead of genesis —
  // otherwise the cursor crawls at MAX_BATCH per tick, current failures stay
  // unseen for hours, and stale crossings replay at the sink.
  const runFloor = runWindowFloor(cursor, now);
  if (runFloor) {
    runConds.push(gte(heartbeatRuns.finishedAt, runFloor));
  }
  const runs = await db
    .select({
      id: heartbeatRuns.id,
      agentId: heartbeatRuns.agentId,
      status: heartbeatRuns.status,
      finishedAt: heartbeatRuns.finishedAt,
    })
    .from(heartbeatRuns)
    .where(and(...runConds))
    .orderBy(asc(heartbeatRuns.finishedAt), asc(heartbeatRuns.id))
    .limit(MAX_BATCH);

  if (!cursor.runs && runs.length >= MAX_BATCH) {
    logger.warn(
      { companyId, runFloor: runFloor?.toISOString() },
      "lane watchdog: first tick saturated MAX_BATCH within the 7-day lookback; cursor drains on subsequent ticks",
    );
  }

  let runRecords: Record<string, unknown>[] = [];
  if (runs.length > 0) {
    const writeRows = await db
      .select({ runId: activityLog.runId, writes: sql<number>`count(*)`.mapWith(Number) })
      .from(activityLog)
      .where(
        and(
          inArray(
            activityLog.runId,
            runs.map((run) => run.id),
          ),
          notLike(activityLog.action, "environment.lease%"),
          ne(activityLog.action, "heartbeat.invoked"),
        ),
      )
      .groupBy(activityLog.runId);
    const writeCounts = new Map(writeRows.map((row) => [row.runId, row.writes]));
    runRecords = runs.map((run) => recordFromHeartbeatRun(run, writeCounts.get(run.id) ?? 0));
  }

  const strandingConds = [eq(activityLog.companyId, companyId), like(activityLog.action, RECONCILE_ACTION_PATTERN)];
  if (cursor.strandings) {
    strandingConds.push(
      or(
        gt(activityLog.createdAt, new Date(cursor.strandings.ts)),
        and(
          eq(activityLog.createdAt, new Date(cursor.strandings.ts)),
          gt(activityLog.id, cursor.strandings.id),
        ),
      )!,
    );
  }
  const strandingRows = await db
    .select({
      id: activityLog.id,
      agentId: activityLog.agentId,
      action: activityLog.action,
      entityId: activityLog.entityId,
      details: activityLog.details,
      createdAt: activityLog.createdAt,
    })
    .from(activityLog)
    .where(and(...strandingConds))
    .orderBy(asc(activityLog.createdAt), asc(activityLog.id))
    .limit(MAX_BATCH);
  const strandingRecords = strandingRows
    .map((row) => recordFromStrandingActivity(row))
    .filter((record): record is Record<string, unknown> => record !== null);

  if (runRecords.length === 0 && strandingRecords.length === 0) return empty;

  const normalizedRuns = toNormalizedRecords(runRecords);
  const normalizedStrandings = toNormalizedRecords(strandingRecords);
  if (normalizedRuns.invalid > 0 || normalizedStrandings.invalid > 0) {
    logger.warn(
      { companyId, invalidRuns: normalizedRuns.invalid, invalidStrandings: normalizedStrandings.invalid },
      "lane watchdog: dropped malformed ledger records",
    );
  }
  const runApply = applyCounterState(state, normalizedRuns.records, { thresholds: config.thresholds });
  const strandingApply = applyCounterState(runApply.state, normalizedStrandings.records, {
    thresholds: config.thresholds,
  });
  const crossings = [...runApply.threshold_crossings, ...strandingApply.threshold_crossings];
  const clears = [...runApply.recovery_clears, ...strandingApply.recovery_clears];

  const affectedLanes = [
    ...new Set([
      ...runRecords.map((record) => String(record.lane)),
      ...strandingRecords.map((record) => String(record.lane)),
    ]),
  ];
  const laneRows = affectedLanes.length
    ? await db.select({ id: agents.id, name: agents.name }).from(agents).where(inArray(agents.id, affectedLanes))
    : [];
  const laneNames = new Map(laneRows.map((row) => [row.id, row.name]));

  let alertsEmitted = 0;
  let clearsEmitted = 0;
  for (const event of crossings) {
    await db.insert(issueComments).values({
      companyId,
      issueId: sinkIssueId,
      body: alertCommentBody(event, laneNames.get(event.lane_id) ?? null),
    });
    alertsEmitted += 1;
    await logActivity(db, {
      companyId,
      actorType: "system",
      actorId: "lane-failure-watchdog",
      action: "lane_watchdog.alert_emitted",
      entityType: "issue",
      entityId: sinkIssueId,
      agentId: event.lane_id,
      details: {
        lane_id: event.lane_id,
        class: event.class,
        signal: event.signal,
        count: event.count,
        threshold: event.threshold,
        issue_id: event.issue_id ?? null,
        cause_code: event.cause_code ?? null,
      },
    });
  }
  for (const event of clears) {
    await db.insert(issueComments).values({
      companyId,
      issueId: sinkIssueId,
      body: recoveryCommentBody(event, laneNames.get(event.lane_id) ?? null),
    });
    clearsEmitted += 1;
    await logActivity(db, {
      companyId,
      actorType: "system",
      actorId: "lane-failure-watchdog",
      action: "lane_watchdog.recovery_cleared",
      entityType: "issue",
      entityId: sinkIssueId,
      agentId: event.lane_id,
      details: {
        lane_id: event.lane_id,
        class: event.class,
        signal: event.signal,
        count: event.count,
        threshold: event.threshold,
      },
    });
  }

  const nextCursor: WatchdogCursor = {
    runs:
      runs.length > 0
        ? {
            ts: runs[runs.length - 1].finishedAt!.toISOString(),
            id: runs[runs.length - 1].id,
          }
        : cursor.runs,
    strandings:
      strandingRows.length > 0
        ? {
            ts: strandingRows[strandingRows.length - 1].createdAt.toISOString(),
            id: strandingRows[strandingRows.length - 1].id,
          }
        : cursor.strandings,
  };
  await db
    .insert(laneFailureWatchdogState)
    .values({
      companyId,
      state: strandingApply.state as unknown as Record<string, unknown>,
      cursor: nextCursor as unknown as Record<string, unknown>,
      updatedAt: now,
    })
    .onConflictDoUpdate({
      target: laneFailureWatchdogState.companyId,
      set: {
        state: strandingApply.state as unknown as Record<string, unknown>,
        cursor: nextCursor as unknown as Record<string, unknown>,
        updatedAt: now,
      },
    });

  return {
    runsProcessed: runRecords.length,
    strandingsProcessed: strandingRecords.length,
    alertsEmitted,
    clearsEmitted,
  };
}

async function resolveSinkIssueId(db: Db, companyId: string, sinkIdentifier: string): Promise<string | null> {
  const [sink] = await db
    .select({ id: issues.id })
    .from(issues)
    .where(and(eq(issues.companyId, companyId), eq(issues.identifier, sinkIdentifier)))
    .limit(1);
  return sink?.id ?? null;
}
