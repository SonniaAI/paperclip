import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { basename, dirname, join } from "node:path";

export const COUNTER_STATE_VERSION = 1 as const;

export const LEDGER_OUTCOMES = [
  "zero_write",
  "terminal_fail",
  "success_readonly",
  "success",
  "other",
  "running",
] as const;

export type LedgerOutcome = (typeof LEDGER_OUTCOMES)[number];

export const FAILURE_OUTCOMES = ["zero_write", "terminal_fail"] as const;
export type FailureOutcome = (typeof FAILURE_OUTCOMES)[number];

export const SUCCESS_OUTCOMES = ["success_readonly", "success"] as const;

export type RawLedgerRecord = Record<string, unknown>;

export interface NormalizedLedgerRecord {
  run_id: string;
  lane: string;
  outcome: LedgerOutcome;
  finished_at: string | null;
  source_index: number;
}

export interface LedgerIssue {
  source_index: number;
  reason: string;
  value?: unknown;
}

export interface ParsedLedger {
  metadata: Record<string, unknown> | null;
  records: NormalizedLedgerRecord[];
  invalid: LedgerIssue[];
}

export interface OutcomeCounts {
  zero_write: number;
  terminal_fail: number;
  success_readonly: number;
  success: number;
  other: number;
  running: number;
}

export interface FailureCounts {
  zero_write: number;
  terminal_fail: number;
}

export interface LaneCounterState {
  current_streak: number;
  last_success_ts: string | null;
  last_failure_ts: string | null;
  updated_at: string;
  outcome_counts: OutcomeCounts;
  failure_counts: FailureCounts;
}

export interface CounterState {
  version: typeof COUNTER_STATE_VERSION;
  updated_at: string;
  processed_run_ids: string[];
  lanes: Record<string, LaneCounterState>;
}

export interface ApplyOptions {
  now?: () => Date | string;
}

export interface SkippedRecord {
  run_id: string;
  lane: string;
  outcome: LedgerOutcome;
  reason: string;
}

export interface ApplyResult {
  state: CounterState;
  applied: NormalizedLedgerRecord[];
  duplicate_run_ids: string[];
  already_processed_run_ids: string[];
  conflicting_duplicate_run_ids: string[];
  skipped: SkippedRecord[];
}

const OUTCOME_RANK: Record<LedgerOutcome, number> = {
  other: 0,
  success: 1,
  success_readonly: 2,
  terminal_fail: 3,
  zero_write: 4,
  running: -1,
};

const FAILURE_SET = new Set<FailureOutcome>(FAILURE_OUTCOMES);
const SUCCESS_SET = new Set<string>(SUCCESS_OUTCOMES);

function isRecord(value: unknown): value is RawLedgerRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function firstValue(record: RawLedgerRecord, keys: string[]): unknown {
  for (const key of keys) {
    if (key in record) return record[key];
  }
  return undefined;
}

function firstString(record: RawLedgerRecord, keys: string[]): string | null {
  return readString(firstValue(record, keys));
}

function readBoolean(value: unknown): boolean {
  if (value === true || value === 1) return true;
  if (typeof value !== "string") return false;
  const normalized = value.trim().toLowerCase();
  return normalized === "true" || normalized === "1" || normalized === "yes";
}

function truthyField(record: RawLedgerRecord, keys: string[]): boolean {
  return keys.some((key) => key in record && readBoolean(record[key]));
}

function normalizeOutcomeToken(value: unknown): LedgerOutcome | null {
  const raw = readString(value);
  if (!raw) return null;
  const normalized = raw.toLowerCase().replace(/[\s-]+/g, "_");
  switch (normalized) {
    case "zero_write":
    case "zerowrite":
      return "zero_write";
    case "terminal_fail":
    case "terminal_failure":
    case "terminalfail":
      return "terminal_fail";
    case "success_readonly":
    case "success_read_only":
    case "readonly_success":
    case "read_only_success":
      return "success_readonly";
    case "success":
    case "succeeded":
      return "success";
    case "running":
    case "queued":
      return "running";
    case "other":
      return "other";
    default:
      return "other";
  }
}

function normalizeStatus(value: unknown): string | null {
  return readString(value)?.toLowerCase().replace(/[\s-]+/g, "_") ?? null;
}

/**
 * Classify one run using the frozen WP-A precedence:
 * zero_write > terminal_fail > success_readonly > success > other.
 *
 * The explicit outcome is preferred, but boolean signal fields are accepted so
 * the reducer can consume a raw heartbeat-run export. A raw status is used only
 * when no explicit outcome/signal is present.
 */
export function classifyOutcome(record: RawLedgerRecord): LedgerOutcome {
  const explicitOutcome = normalizeOutcomeToken(record.outcome);

  if (
    explicitOutcome === "zero_write" ||
    truthyField(record, ["zero_write", "zeroWrite", "is_zero_write"])
  ) {
    return "zero_write";
  }
  if (
    explicitOutcome === "terminal_fail" ||
    truthyField(record, ["terminal_fail", "terminalFail", "is_terminal_fail"])
  ) {
    return "terminal_fail";
  }
  if (
    explicitOutcome === "success_readonly" ||
    truthyField(record, ["success_readonly", "successReadonly", "is_success_readonly"])
  ) {
    return "success_readonly";
  }
  if (explicitOutcome === "success") return "success";
  if (explicitOutcome === "running") return "running";
  if (explicitOutcome === "other") return "other";
  if ("outcome" in record && record.outcome != null) return "other";

  const status = normalizeStatus(record.status);
  if (status === "running" || status === "queued" || status === "scheduled_retry") {
    return "running";
  }
  if (status === "succeeded" || status === "success") {
    return truthyField(record, ["read_only", "readonly", "success_readonly", "successReadonly"])
      ? "success_readonly"
      : "success";
  }
  if (status === "failed" || status === "timed_out" || status === "cancelled" || status === "interrupted") {
    return "terminal_fail";
  }
  return "other";
}

function readLane(record: RawLedgerRecord): string | null {
  const direct = firstString(record, ["lane", "agent", "lane_id", "laneId", "agent_id", "agentId"]);
  if (direct) return direct;

  const agent = firstValue(record, ["agent", "agent_info"]);
  if (isRecord(agent)) {
    return firstString(agent, ["lane", "name", "id", "shortname"]);
  }
  return null;
}

function normalizeTimestamp(value: unknown): string | null {
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value.toISOString();
  }
  const raw = readString(value);
  if (!raw) return null;
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function normalizeNow(now?: () => Date | string): string {
  const value = now?.() ?? new Date();
  const timestamp = normalizeTimestamp(value);
  if (!timestamp) throw new Error("The supplied clock returned an invalid timestamp");
  return timestamp;
}

export function normalizeLedgerRecord(value: unknown, sourceIndex: number):
  | { record: NormalizedLedgerRecord; issue?: undefined }
  | { record?: undefined; issue: LedgerIssue } {
  if (!isRecord(value)) {
    return { issue: { source_index: sourceIndex, reason: "record is not an object", value } };
  }

  const runId = firstString(value, ["run_id", "runId", "id"]);
  if (!runId) {
    return { issue: { source_index: sourceIndex, reason: "missing run_id", value } };
  }
  const lane = readLane(value);
  if (!lane) {
    return { issue: { source_index: sourceIndex, reason: "missing lane/agent", value } };
  }

  const rawFinishedAt = firstValue(value, ["finished_at", "finishedAt", "completed_at", "completedAt"]);
  const finishedAt = rawFinishedAt == null ? null : normalizeTimestamp(rawFinishedAt);
  if (rawFinishedAt != null && finishedAt == null) {
    return { issue: { source_index: sourceIndex, reason: "invalid finished_at", value: rawFinishedAt } };
  }

  return {
    record: {
      run_id: runId,
      lane,
      outcome: classifyOutcome(value),
      finished_at: finishedAt,
      source_index: sourceIndex,
    },
  };
}

function metadataFromValue(value: RawLedgerRecord): Record<string, unknown> | null {
  return isRecord(value.meta) ? value.meta : null;
}

function entriesFromContainer(value: unknown): { entries: unknown[]; metadata: Record<string, unknown> | null } | null {
  if (Array.isArray(value)) return { entries: value, metadata: null };
  if (!isRecord(value)) return null;

  const metadata = metadataFromValue(value);
  for (const key of ["records", "runs", "data", "rows"]) {
    if (Array.isArray(value[key])) return { entries: value[key], metadata };
  }
  if ("run_id" in value || "runId" in value || "id" in value) {
    return { entries: [value], metadata };
  }
  if (metadata) return { entries: [], metadata };
  return null;
}

function parseEntries(entries: unknown[], metadata: Record<string, unknown> | null, indexOffset = 0): ParsedLedger {
  const records: NormalizedLedgerRecord[] = [];
  const invalid: LedgerIssue[] = [];
  let discoveredMetadata = metadata;

  entries.forEach((entry, entryIndex) => {
    const sourceIndex = indexOffset + entryIndex;
    if (isRecord(entry) && metadataFromValue(entry) && !("run_id" in entry) && !("runId" in entry) && !("id" in entry)) {
      discoveredMetadata = metadataFromValue(entry);
      return;
    }
    const normalized = normalizeLedgerRecord(entry, sourceIndex);
    if (normalized.record) records.push(normalized.record);
    else invalid.push(normalized.issue);
  });

  return { metadata: discoveredMetadata, records, invalid };
}

/** Parse either a JSON array/container or newline-delimited JSON. */
export function parseLedgerText(text: string): ParsedLedger {
  const trimmed = text.replace(/^\uFEFF/, "").trim();
  if (!trimmed) return { metadata: null, records: [], invalid: [] };

  try {
    const parsed = JSON.parse(trimmed) as unknown;
    const container = entriesFromContainer(parsed);
    if (container) return parseEntries(container.entries, container.metadata);
  } catch {
    // A JSONL export is expected to fail whole-document JSON.parse; parse it below.
  }

  const records: NormalizedLedgerRecord[] = [];
  const invalid: LedgerIssue[] = [];
  let metadata: Record<string, unknown> | null = null;
  for (const [lineIndex, line] of trimmed.split(/\r?\n/).entries()) {
    const lineText = line.trim();
    if (!lineText) continue;
    try {
      const value = JSON.parse(lineText) as unknown;
      if (isRecord(value) && metadataFromValue(value) && !("run_id" in value) && !("runId" in value) && !("id" in value)) {
        metadata = metadataFromValue(value);
        continue;
      }
      const normalized = normalizeLedgerRecord(value, lineIndex);
      if (normalized.record) records.push(normalized.record);
      else invalid.push(normalized.issue);
    } catch (error) {
      invalid.push({
        source_index: lineIndex,
        reason: `invalid JSON: ${error instanceof Error ? error.message : String(error)}`,
        value: lineText,
      });
    }
  }
  return { metadata, records, invalid };
}

export async function parseLedgerFile(path: string): Promise<ParsedLedger> {
  return parseLedgerText(await readFile(path, "utf8"));
}

function compareRecords(a: NormalizedLedgerRecord, b: NormalizedLedgerRecord): number {
  if (a.finished_at && b.finished_at) {
    const byTime = a.finished_at.localeCompare(b.finished_at);
    if (byTime !== 0) return byTime;
  } else if (a.finished_at && !b.finished_at) {
    return -1;
  } else if (!a.finished_at && b.finished_at) {
    return 1;
  }
  return a.source_index - b.source_index;
}

function mergeOutcome(a: LedgerOutcome, b: LedgerOutcome): LedgerOutcome {
  return OUTCOME_RANK[a] >= OUTCOME_RANK[b] ? a : b;
}

function latestTimestamp(a: string | null, b: string | null): string | null {
  if (!a) return b;
  if (!b) return a;
  return a >= b ? a : b;
}

function coalesceRecords(records: NormalizedLedgerRecord[]): {
  records: NormalizedLedgerRecord[];
  duplicateRunIds: string[];
  conflictingDuplicateRunIds: string[];
} {
  const byRunId = new Map<string, NormalizedLedgerRecord>();
  const duplicateRunIds = new Set<string>();
  const conflictingDuplicateRunIds = new Set<string>();

  for (const record of records) {
    const existing = byRunId.get(record.run_id);
    if (!existing) {
      byRunId.set(record.run_id, { ...record });
      continue;
    }
    duplicateRunIds.add(record.run_id);
    if (existing.lane !== record.lane) conflictingDuplicateRunIds.add(record.run_id);
    existing.outcome = mergeOutcome(existing.outcome, record.outcome);
    existing.finished_at = latestTimestamp(existing.finished_at, record.finished_at);
    existing.source_index = Math.min(existing.source_index, record.source_index);
  }

  return {
    records: [...byRunId.values()].sort(compareRecords),
    duplicateRunIds: [...duplicateRunIds].sort(),
    conflictingDuplicateRunIds: [...conflictingDuplicateRunIds].sort(),
  };
}

function zeroOutcomeCounts(): OutcomeCounts {
  return {
    zero_write: 0,
    terminal_fail: 0,
    success_readonly: 0,
    success: 0,
    other: 0,
    running: 0,
  };
}

function zeroFailureCounts(): FailureCounts {
  return { zero_write: 0, terminal_fail: 0 };
}

function newLaneState(updatedAt: string): LaneCounterState {
  return {
    current_streak: 0,
    last_success_ts: null,
    last_failure_ts: null,
    updated_at: updatedAt,
    outcome_counts: zeroOutcomeCounts(),
    failure_counts: zeroFailureCounts(),
  };
}

export function createEmptyCounterState(now: Date | string = new Date()): CounterState {
  const updatedAt = normalizeTimestamp(now);
  if (!updatedAt) throw new Error("The supplied timestamp is invalid");
  return {
    version: COUNTER_STATE_VERSION,
    updated_at: updatedAt,
    processed_run_ids: [],
    lanes: {},
  };
}

function nonNegativeInteger(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : fallback;
}

function normalizedTimestampOrNull(value: unknown): string | null {
  if (value == null) return null;
  return normalizeTimestamp(value);
}

/** Validate and fill optional state fields so WP-B has a stable JSON shape. */
export function parseCounterState(value: unknown): CounterState {
  if (!isRecord(value)) throw new Error("Counter state must be a JSON object");
  if (value.version !== COUNTER_STATE_VERSION) {
    throw new Error(`Unsupported counter state version: ${String(value.version)}`);
  }

  const updatedAt = normalizeTimestamp(value.updated_at);
  if (!updatedAt) throw new Error("Counter state has an invalid updated_at");

  const processedRunIds = Array.isArray(value.processed_run_ids)
    ? value.processed_run_ids.filter((entry): entry is string => Boolean(readString(entry))).map((entry) => readString(entry)!)
    : [];
  const lanesValue = value.lanes;
  if (!isRecord(lanesValue)) throw new Error("Counter state lanes must be an object");

  const lanes: Record<string, LaneCounterState> = {};
  for (const [lane, rawLane] of Object.entries(lanesValue)) {
    if (!isRecord(rawLane)) throw new Error(`Counter state lane ${lane} is not an object`);
    const laneUpdatedAt = normalizeTimestamp(rawLane.updated_at);
    if (!laneUpdatedAt) throw new Error(`Counter state lane ${lane} has an invalid updated_at`);
    const rawOutcomeCounts = isRecord(rawLane.outcome_counts)
      ? rawLane.outcome_counts
      : isRecord(rawLane.counts)
        ? rawLane.counts
        : {};
    const rawFailureCounts = isRecord(rawLane.failure_counts) ? rawLane.failure_counts : {};
    lanes[lane] = {
      current_streak: nonNegativeInteger(rawLane.current_streak),
      last_success_ts: normalizedTimestampOrNull(rawLane.last_success_ts),
      last_failure_ts: normalizedTimestampOrNull(rawLane.last_failure_ts),
      updated_at: laneUpdatedAt,
      outcome_counts: {
        zero_write: nonNegativeInteger(rawOutcomeCounts.zero_write),
        terminal_fail: nonNegativeInteger(rawOutcomeCounts.terminal_fail),
        success_readonly: nonNegativeInteger(rawOutcomeCounts.success_readonly),
        success: nonNegativeInteger(rawOutcomeCounts.success),
        other: nonNegativeInteger(rawOutcomeCounts.other),
        running: nonNegativeInteger(rawOutcomeCounts.running),
      },
      failure_counts: {
        zero_write: nonNegativeInteger(rawFailureCounts.zero_write, nonNegativeInteger(rawOutcomeCounts.zero_write)),
        terminal_fail: nonNegativeInteger(rawFailureCounts.terminal_fail, nonNegativeInteger(rawOutcomeCounts.terminal_fail)),
      },
    };
  }

  return {
    version: COUNTER_STATE_VERSION,
    updated_at: updatedAt,
    processed_run_ids: [...new Set(processedRunIds)],
    lanes,
  };
}

function cloneCounterState(state: CounterState): CounterState {
  return parseCounterState(JSON.parse(JSON.stringify(state)) as unknown);
}

function requireFinishedAt(record: NormalizedLedgerRecord): string | null {
  return record.finished_at;
}

/** Apply a chronologically ordered, deduplicated reducer without mutating input state. */
export function applyCounterState(
  initialState: CounterState,
  inputRecords: NormalizedLedgerRecord[],
  options: ApplyOptions = {},
): ApplyResult {
  const state = cloneCounterState(initialState);
  const now = normalizeNow(options.now);
  const coalesced = coalesceRecords(inputRecords);
  const processed = new Set(state.processed_run_ids);
  const applied: NormalizedLedgerRecord[] = [];
  const alreadyProcessedRunIds: string[] = [];
  const skipped: SkippedRecord[] = [];

  for (const record of coalesced.records) {
    if (processed.has(record.run_id)) {
      alreadyProcessedRunIds.push(record.run_id);
      continue;
    }

    if ((FAILURE_SET.has(record.outcome as FailureOutcome) || SUCCESS_SET.has(record.outcome)) && !requireFinishedAt(record)) {
      skipped.push({
        run_id: record.run_id,
        lane: record.lane,
        outcome: record.outcome,
        reason: "failure/success record is missing finished_at",
      });
      continue;
    }

    const laneState = state.lanes[record.lane] ?? newLaneState(now);
    laneState.outcome_counts[record.outcome] += 1;
    laneState.updated_at = now;

    if (FAILURE_SET.has(record.outcome as FailureOutcome)) {
      laneState.current_streak += 1;
      laneState.last_failure_ts = latestTimestamp(laneState.last_failure_ts, record.finished_at);
      laneState.failure_counts[record.outcome as FailureOutcome] += 1;
    } else if (SUCCESS_SET.has(record.outcome)) {
      laneState.current_streak = 0;
      laneState.last_success_ts = latestTimestamp(laneState.last_success_ts, record.finished_at);
    }

    state.lanes[record.lane] = laneState;
    processed.add(record.run_id);
    applied.push(record);
  }

  if (applied.length > 0) state.updated_at = now;
  state.processed_run_ids = [...processed].sort();

  return {
    state,
    applied,
    duplicate_run_ids: coalesced.duplicateRunIds,
    already_processed_run_ids: [...new Set(alreadyProcessedRunIds)].sort(),
    conflicting_duplicate_run_ids: coalesced.conflictingDuplicateRunIds,
    skipped,
  };
}

export function serializeCounterState(state: CounterState): string {
  const normalized = parseCounterState(state);
  const lanes = Object.fromEntries(
    Object.entries(normalized.lanes)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([lane, laneState]) => [lane, laneState]),
  );
  return `${JSON.stringify({ ...normalized, processed_run_ids: [...normalized.processed_run_ids].sort(), lanes }, null, 2)}\n`;
}

export async function readCounterState(path: string, now: Date | string = new Date()): Promise<CounterState> {
  try {
    return parseCounterState(JSON.parse(await readFile(path, "utf8")) as unknown);
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code === "ENOENT") return createEmptyCounterState(now);
    throw error;
  }
}

/** Persist state with a same-directory temp file + rename so readers never see partial JSON. */
export async function writeCounterState(path: string, state: CounterState): Promise<void> {
  const directory = dirname(path);
  await mkdir(directory, { recursive: true });
  const temporaryPath = join(directory, `.${basename(path)}.${process.pid}.${randomUUID()}.tmp`);
  try {
    await writeFile(temporaryPath, serializeCounterState(state), { encoding: "utf8", mode: 0o644 });
    await rename(temporaryPath, path);
  } finally {
    await rm(temporaryPath, { force: true }).catch(() => undefined);
  }
}
