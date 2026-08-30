import { jsonb, pgTable, timestamp, uuid } from "drizzle-orm/pg-core";
import { companies } from "./companies.js";

/**
 * SON-1440 WP-B/WP-C — persisted reducer state for the fleet lane-failure
 * watchdog (producer of the SON-1334 Operations alert sink).
 *
 * One row per company. `state` holds the verbatim JSON produced by the pure
 * lane-failure-counter reducer (tools/lane-failure-counter, vendored copy in
 * server/src/vendor/lane-failure-counter) so the exactly-once threshold
 * crossing latch survives restarts. `cursor` tracks the last processed
 * (finished_at, id) positions for terminal heartbeat runs and reconcile
 * stranding activity rows so ticks are incremental and replay-safe.
 */
export const laneFailureWatchdogState = pgTable("lane_failure_watchdog_state", {
  companyId: uuid("company_id")
    .primaryKey()
    .references(() => companies.id),
  state: jsonb("state").$type<Record<string, unknown>>().notNull(),
  cursor: jsonb("cursor").$type<Record<string, unknown>>(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});
