// SON-4680 restore bootstrap, extended for SON-6153: standalone
// execution-reconciler driver with level-triggered transition deadlines.
//
// The Oct-9..12 control-plane image rebuilds dropped the execution-reconciler
// module from the deployed runtime while the Sonnia heartbeat graph and the
// EXECUTION_RECONCILER_* deployment env survived. This module restores the
// reconciler additively: it constructs the service with database-backed hooks
// and drives the legacy scans plus the SON-6153 level-triggered transition
// scan on their own cadences, without touching the deployed heartbeat module
// graph. It is loaded in production via NODE_OPTIONS=--import (deployment env
// change) or an explicit index import; importing it twice is safe (global
// guard), and it is inert under test.
//
// SON-6153 DEFAULT-OFF: startup now requires an explicit deployment switch —
// EXECUTION_RECONCILER_BOOTSTRAP_ENABLED=true (the new canonical switch) or
// the legacy EXECUTION_RECONCILER_AUTOBOOTSTRAP=true. Nothing starts by
// default when the module merely ships in an image. The level-triggered
// transition scans additionally require
// EXECUTION_RECONCILER_LEVEL_TRIGGERED_ENABLED=true, so each behavior class
// is enabled deliberately after verification.
//
// r6 fix (2026-10-26, hang bisect evidence): the r5 canary and the on-pod leg-7
// forensics proved the compiled bootstrap DEADLOCKS when preloaded via
// `--import` before main: its top-level static import graph reaches
// @paperclipai/db and @paperclipai/shared, whose package exports point at TS
// source, so the tsx loader must translate TS during Node's pre-main --import
// phase; the loader/worker handshake then sleeps forever on a futex (state S,
// wchan futex_do_wait, zero output, rc=124/137). Dynamic import of the same
// graph AFTER boot is clean. This revision therefore keeps the compiled entry
// free of top-level runtime imports (only erased `import type` lines) and
// defers every app import to a setImmediate-scheduled dynamic boot. Under
// `node -e 'process.exit(0)'` canaries the deferred boot never fires (same
// semantics the r6 Dockerfile already documented for unref'd timers); a real
// server boot always fires it.
//
// Hook fidelity notes (vs the pre-Oct-9 in-heartbeat wiring):
// - enqueueWakeup inserts/coallesces agent_wakeup_requests rows directly; the
//   live heartbeat wake loop still claims and dispatches them.
// - getQueuedRunDispatchBlockReason implements the concurrency guard; the
//   dispatch-blackout check is omitted (module absent from the deployed
//   runtime) and non-invokable owners return null exactly like the deployed
//   wiring did (invokability recovery owns those agents).
// - isRunLocallyAlive is omitted (in-memory live-run set only exists inside
//   the heartbeat factory); lease TTL gating alone decides recovery, as it
//   did before the workspace-parallel overlay added the fast path.

import type { and as AndFn, desc, eq, sql } from "drizzle-orm";
import type {
  agentWakeupRequests,
  agents,
  createDb,
  heartbeatRuns,
  Db,
} from "@paperclipai/db";
import type { ExecutionReconcilerSettings } from "./execution-reconciler.js";

type Logger = { info: (o: unknown, m?: string) => void; error: (o: unknown, m?: string) => void; warn: (o: unknown, m?: string) => void };

function readNonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

type BootModules = {
  drizzle: typeof import("drizzle-orm");
  db: typeof import("@paperclipai/db");
  shared: typeof import("@paperclipai/shared");
  logger: Logger;
  evaluateAgentInvokabilityFromDb: (db: Db, agent: unknown) => Promise<{ invokable: boolean }>;
  executionReconcilerService: (
    db: Db,
    hooks: Record<string, unknown>,
  ) => {
    reconcileExecutionState: (o: Record<string, unknown>) => Promise<unknown>;
    scanProgramStalls: (o: Record<string, unknown>) => Promise<unknown>;
    reconcileTransitionDeadlines: (o: Record<string, unknown>) => Promise<unknown>;
  };
  heartbeatService?: (
    db: Db,
    options?: Record<string, unknown>,
  ) => {
    wakeup: (
      agentId: string,
      opts: Record<string, unknown>,
    ) => Promise<{ id?: string } | null | unknown>;
  };
  resolveExecutionReconcilerSettings: () => ExecutionReconcilerSettings;
};

/** Faithful last-mile of heartbeat's enqueueWakeup for reconciler wakes:
 *  validate the agent, coalesce an equivalent still-queued wake, else insert
 *  a queued row the live heartbeat wake loop will claim. */
async function enqueueReconcilerWakeup(
  mods: BootModules,
  db: Db,
  agentId: string,
  opts: Record<string, unknown>,
): Promise<{ id: string } | null> {
  const { and, desc, eq, sql } = mods.drizzle;
  const { agentWakeupRequests, agents } = mods.db;
  const getAgentRow = async () =>
    db.select().from(agents).where(eq(agents.id, agentId)).then((rows) => rows[0] ?? null);
  const agent = await getAgentRow();
  if (!agent) {
    mods.logger.warn({ agentId }, "reconciler bootstrap wake skipped: agent not found");
    return null;
  }
  const payload = (opts.payload ?? null) as Record<string, unknown> | null;
  const reason = readNonEmptyString(opts.reason) ?? "execution_reconciler_wake";
  const issueId = readNonEmptyString(payload?.issueId);
  if (issueId) {
    const pending = await db
      .select({ id: agentWakeupRequests.id, coalescedCount: agentWakeupRequests.coalescedCount })
      .from(agentWakeupRequests)
      .where(
        and(
          eq(agentWakeupRequests.agentId, agentId),
          eq(agentWakeupRequests.reason, reason),
          eq(agentWakeupRequests.status, "queued"),
          sql`(${agentWakeupRequests.payload} ->> 'issueId') = ${issueId}`,
        ),
      )
      .orderBy(desc(agentWakeupRequests.requestedAt))
      .limit(1)
      .then((rows) => rows[0] ?? null);
    if (pending) {
      await db
        .update(agentWakeupRequests)
        .set({ coalescedCount: (pending.coalescedCount ?? 0) + 1, updatedAt: new Date() })
        .where(eq(agentWakeupRequests.id, pending.id));
      return { id: pending.id };
    }
  }
  const inserted = await db
    .insert(agentWakeupRequests)
    .values({
      companyId: (agent as { companyId: string }).companyId,
      agentId,
      source: readNonEmptyString(opts.source) ?? "automation",
      triggerDetail: readNonEmptyString(opts.triggerDetail) ?? "system",
      reason,
      payload,
      status: "queued",
      requestedByActorType: readNonEmptyString(opts.requestedByActorType) ?? "system",
      requestedByActorId: readNonEmptyString(opts.requestedByActorId) ?? "execution-reconciler",
    })
    .returning({ id: agentWakeupRequests.id })
    .then((rows) => rows[0] ?? null);
  return inserted ? { id: inserted.id } : null;
}

/** Concurrency guard protecting queued runs from premature recycle while
 *  their owner is at capacity. Blackout omitted (absent module). */
async function getQueuedRunDispatchBlockReason(
  mods: BootModules,
  db: Db,
  agentId: string,
): Promise<"concurrency_limit" | "dispatch_blackout" | null> {
  const { and, eq, sql } = mods.drizzle;
  const { agentWakeupRequests: _w, agents, heartbeatRuns } = mods.db;
  const { AGENT_DEFAULT_MAX_CONCURRENT_RUNS } = mods.shared;
  const getAgentRow = async () =>
    db.select().from(agents).where(eq(agents.id, agentId)).then((rows) => rows[0] ?? null);
  const agent = await getAgentRow();
  if (!agent) return null;
  const invokability = await mods.evaluateAgentInvokabilityFromDb(db, agent);
  if (!invokability.invokable) return null;
  const runtimeConfig = (agent as { runtimeConfig?: unknown }).runtimeConfig ?? {};
  const heartbeatPolicy = ((runtimeConfig as Record<string, unknown>).heartbeat ?? {}) as Record<string, unknown>;
  const configuredMax = Number(heartbeatPolicy.maxConcurrentRuns);
  const maxConcurrentRuns = Number.isFinite(configuredMax) && configuredMax > 0
    ? configuredMax
    : AGENT_DEFAULT_MAX_CONCURRENT_RUNS;
  const [{ count }] = await db
    .select({ count: sql<number>`count(*)` })
    .from(heartbeatRuns)
    .where(and(eq(heartbeatRuns.agentId, agentId), eq(heartbeatRuns.status, "running")));
  return Number(count ?? 0) >= maxConcurrentRuns ? "concurrency_limit" : null;
}

async function bootExecutionReconciler(mods: BootModules): Promise<void> {
  const settings = mods.resolveExecutionReconcilerSettings();
  const databaseUrl = process.env.DATABASE_URL ?? null;
  if (settings.enabled === false) {
    mods.logger.info("execution reconciler bootstrap: disabled by EXECUTION_RECONCILER_ENABLED; not starting");
    return;
  }
  const masterSwitch =
    process.env.EXECUTION_RECONCILER_BOOTSTRAP_ENABLED === "true" ||
    process.env.EXECUTION_RECONCILER_AUTOBOOTSTRAP === "true";
  if (!masterSwitch) {
    mods.logger.info(
      "execution reconciler bootstrap: default-off (set EXECUTION_RECONCILER_BOOTSTRAP_ENABLED=true to start)",
    );
    return;
  }
  if (!databaseUrl) {
    mods.logger.error("execution reconciler bootstrap: DATABASE_URL missing; not starting");
    return;
  }

  const db: Db = mods.db.createDb(databaseUrl);
  let heartbeat: ReturnType<NonNullable<BootModules["heartbeatService"]>> | null = null;
  const reconciler = mods.executionReconcilerService(db, {
    enqueueWakeup: (agentId: string, opts: Record<string, unknown>) =>
      enqueueReconcilerWakeup(mods, db, agentId, opts),
    getQueuedRunDispatchBlockReason: (agentId: string) =>
      getQueuedRunDispatchBlockReason(mods, db, agentId),
    wakeAgentDirectly: async (agentId: string, opts: Record<string, unknown>) => {
      // SON-6153 wake receipts: the alternate path is the same internal
      // service the interactive POST /agents/:id/wakeup route uses. The
      // heartbeat module is already loaded by a real server boot, so this
      // dynamic import resolves to the cached instance.
      if (!mods.heartbeatService) return null;
      if (!heartbeat) heartbeat = mods.heartbeatService(db);
      const run = await heartbeat.wakeup(agentId, {
        ...opts,
        source: typeof opts.source === "string" ? opts.source : "on_demand",
      });
      if (run && typeof run === "object" && "id" in (run as Record<string, unknown>)) {
        return { id: ((run as Record<string, unknown>).id as string) ?? null };
      }
      return null;
    },
  });

  const intervalMs = Math.max(
    30_000,
    Number(process.env.EXECUTION_RECONCILER_TICK_INTERVAL_MS) || 60_000,
  );
  let ticking = false;
  const tick = async () => {
    if (ticking) return;
    ticking = true;
    try {
      await reconciler.reconcileExecutionState({});
    } catch (error) {
      mods.logger.error({ error: String(error) }, "reconciler bootstrap: reconcileExecutionState failed");
    }
    // Worker-lease recovery is intentionally NOT driven here: the current
    // native scheduler (wake-queue/run-dispatch/active-run-watchdog) owns
    // queued-run and dead-worker recovery, and the legacy lease columns no
    // longer exist on the qualified schema.
    try {
      await reconciler.scanProgramStalls({});
    } catch (error) {
      mods.logger.error({ error: String(error) }, "reconciler bootstrap: scanProgramStalls failed");
    }
    ticking = false;
  };

  let transitioning = false;
  const transitionTick = async () => {
    if (transitioning) return;
    transitioning = true;
    try {
      await reconciler.reconcileTransitionDeadlines({});
    } catch (error) {
      mods.logger.error({ error: String(error) }, "reconciler bootstrap: reconcileTransitionDeadlines failed");
    }
    transitioning = false;
  };

  const transitionIntervalMs = Math.max(60_000, settings.levelTriggeredIntervalMs);
  mods.logger.info(
    {
      intervalMs,
      dryRun: settings.dryRun,
      verifierAgentId: settings.verifierAgentId,
      levelTriggered: settings.levelTriggeredEnabled,
      transitionIntervalMs,
    },
    "execution reconciler bootstrap: starting tick loop",
  );
  setTimeout(() => {
    void tick();
  }, 20_000).unref();
  const timer = setInterval(() => {
    void tick();
  }, intervalMs);
  timer.unref();
  if (settings.levelTriggeredEnabled) {
    setTimeout(() => {
      void transitionTick();
    }, 45_000).unref();
    const transitionTimer = setInterval(() => {
      void transitionTick();
    }, transitionIntervalMs);
    transitionTimer.unref();
  }
}

export function startExecutionReconcilerBootstrap(): void {
  const globalRef = globalThis as { __son4680ReconcilerBootstrapStarted?: boolean };
  if (globalRef.__son4680ReconcilerBootstrapStarted) return;
  globalRef.__son4680ReconcilerBootstrapStarted = true;

  // r6 fix: schedule the app-graph load for after main. The --import pre-main
  // phase must not translate TS (tsx loader/worker futex deadlock); a plain
  // JS registration here is safe (r5 leg A), and a real server's event loop
  // always reaches this immediate.
  setImmediate(() => {
    void (async () => {
      const [drizzle, db, shared, loggerMod, invokability, reconcilerMod, heartbeatMod] = await Promise.all([
        import("drizzle-orm"),
        import("@paperclipai/db"),
        import("@paperclipai/shared"),
        import("../middleware/logger.js"),
        import("./agent-invokability.js"),
        import("./execution-reconciler.js"),
        import("./heartbeat.js"),
      ]);
      const mods: BootModules = {
        drizzle: drizzle as unknown as BootModules["drizzle"],
        db: db as unknown as BootModules["db"],
        shared: shared as unknown as BootModules["shared"],
        logger: (loggerMod as unknown as { logger: Logger }).logger,
        evaluateAgentInvokabilityFromDb: (invokability as unknown as {
          evaluateAgentInvokabilityFromDb: BootModules["evaluateAgentInvokabilityFromDb"];
        }).evaluateAgentInvokabilityFromDb,
        executionReconcilerService: (reconcilerMod as unknown as {
          executionReconcilerService: BootModules["executionReconcilerService"];
        }).executionReconcilerService,
        heartbeatService: (heartbeatMod as unknown as {
          heartbeatService: BootModules["heartbeatService"];
        }).heartbeatService,
        resolveExecutionReconcilerSettings: (reconcilerMod as unknown as {
          resolveExecutionReconcilerSettings: BootModules["resolveExecutionReconcilerSettings"];
        }).resolveExecutionReconcilerSettings,
      };
      await bootExecutionReconciler(mods);
    })().catch((error) => {
      // Pre-logger failures can only use console; keep them visible either way.
      console.error("[son4680-bootstrap] deferred boot failed:", error);
    });
  });
}

// SON-6153 default-off: when the module merely ships in an image (e.g. via a
// NODE_OPTIONS=--import preload), it must not even load the app graph — the
// master switch is checked before any import. Set
// EXECUTION_RECONCILER_BOOTSTRAP_ENABLED=true (canonical) or the legacy
// EXECUTION_RECONCILER_AUTOBOOTSTRAP=true to actually start the tick loops.
const bootstrapEnabled =
  process.env.EXECUTION_RECONCILER_BOOTSTRAP_ENABLED === "true" ||
  process.env.EXECUTION_RECONCILER_AUTOBOOTSTRAP === "true";
if (bootstrapEnabled && process.env.NODE_ENV !== "test") {
  startExecutionReconcilerBootstrap();
}
