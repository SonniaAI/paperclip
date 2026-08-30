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

/**
 * Signal classes are deliberately separate from ledger outcomes.  In
 * particular, zero-write deaths are Class B and must never be folded into the
 * generic Class A counter.
 */
export const SIGNAL_CLASSES = ["A", "B", "C"] as const;
export type SignalClass = (typeof SIGNAL_CLASSES)[number];

export const DEFAULT_THRESHOLDS: Readonly<Record<SignalClass, number>> = Object.freeze({
  A: 3,
  B: 3,
  C: 1,
});

export type ThresholdConfig = Partial<Record<SignalClass, number>>;

export interface LaneKeyOptions {
  /** Explicit aliases are useful while an agents-table rename is rolling out. */
  laneAliases?: Readonly<Record<string, string>>;
  /** Resolve a display name against the authoritative agents table. */
  canonicalizeLane?: (lane: string) => string;
}

export type RawLedgerRecord = Record<string, unknown>;

export interface NormalizedLedgerRecord {
  run_id: string;
  lane: string;
  outcome: LedgerOutcome;
  finished_at: string | null;
  source_index: number;
  signal_class?: SignalClass;
  signal?: string;
  issue_id?: string;
  cause_code?: string;
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

/** Persisted WP-B state for one canonical lane and one signal class. */
export interface SignalCounterState {
  current_streak: number;
  first_failure_ts: string | null;
  last_failure_ts: string | null;
  last_success_ts: string | null;
  updated_at: string;
  threshold: number;
  alert_emitted: boolean;
}

export type LaneSignalStates = Partial<Record<SignalClass, SignalCounterState>>;

export interface CounterState {
  version: typeof COUNTER_STATE_VERSION;
  updated_at: string;
  processed_run_ids: string[];
  lanes: Record<string, LaneCounterState>;
  /**
   * Additive WP-B state.  Keeping it separate preserves the WP-A `lanes`
   * shape for existing readers while making the per-(lane,class) state
   * inspectable and persistable.
   */
  signals: Record<string, LaneSignalStates>;
}

export interface ApplyOptions extends LaneKeyOptions {
  now?: () => Date | string;
  thresholds?: ThresholdConfig;
}

export interface ParseLedgerOptions extends LaneKeyOptions {}

export interface ThresholdCrossingEvent {
  event_type: "threshold_crossing";
  emitted_at: string;
  lane_id: string;
  class: SignalClass;
  signal: string;
  count: number;
  threshold: number;
  first_failure_ts: string;
  last_failure_ts: string;
  run_id?: string;
  issue_id?: string;
  cause_code?: string;
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
  threshold_crossings: ThresholdCrossingEvent[];
  /** Aliases make the result readable to callers that think in alerts/events. */
  emitted_alerts: ThresholdCrossingEvent[];
  events: ThresholdCrossingEvent[];
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

const CLASS_C_CAUSES = new Set([
  "successful_run_missing_state",
  "stranded_assigned_issue",
]);

const DEFAULT_SIGNAL_LABELS: Record<SignalClass, string> = {
  A: "generic/ws-open",
  B: "zero_write",
  C: "stranded_assigned_issue",
};

function compactToken(value: unknown): string | null {
  return readString(value)?.toLowerCase().replace(/[\s_\-/]+/g, "") ?? null;
}

/** Accept taxonomy letters as well as the wire labels used by the drill. */
export function normalizeSignalClass(value: unknown): SignalClass | null {
  const token = compactToken(value);
  if (!token) return null;
  switch (token) {
    case "a":
    case "classa":
    case "generic":
    case "genericterminalfailure":
    case "genericwsopen":
    case "wsopen":
    case "terminalfail":
      return "A";
    case "b":
    case "classb":
    case "zerowrite":
    case "zerowritedeath":
      return "B";
    case "c":
    case "classc":
    case "stranded":
    case "strandedassignedissue":
    case "successfulrunmissingstate":
    case "successfulrunstranding":
      return "C";
    default:
      return null;
  }
}

function causeCodeFromRecord(record: RawLedgerRecord): string | null {
  return firstString(record, [
    "cause_code",
    "causeCode",
    "stranding_cause",
    "strandingCause",
  ]);
}

function explicitSignalClassFromRecord(record: RawLedgerRecord): SignalClass | null {
  return normalizeSignalClass(
    firstValue(record, [
      "signal_class",
      "signalClass",
      "failure_class",
      "failureClass",
      "class",
    ]),
  );
}

function signalClassFromValues(record: RawLedgerRecord, outcome: LedgerOutcome): SignalClass | null {
  // A zero-write outcome is authoritative for the class boundary.  It cannot
  // accidentally become generic Class A because a producer also sent a broad
  // `class` field.
  if (outcome === "zero_write") return "B";

  const explicit = explicitSignalClassFromRecord(record);
  if (explicit) return explicit;

  const causeCode = causeCodeFromRecord(record)?.toLowerCase();
  if (causeCode && CLASS_C_CAUSES.has(causeCode)) return "C";
  if (outcome === "terminal_fail") return "A";
  return null;
}

function signalLabelFromRecord(record: RawLedgerRecord, signalClass: SignalClass | null): string {
  const explicit = firstString(record, ["signal", "signal_type", "signalType", "failure_kind", "failureKind"]);
  if (explicit) return explicit;
  const causeCode = causeCodeFromRecord(record);
  if (signalClass === "C" && causeCode) return causeCode;
  if (!signalClass) throw new Error("Cannot derive a signal label without a signal class");
  return DEFAULT_SIGNAL_LABELS[signalClass];
}

function issueIdFromRecord(record: RawLedgerRecord): string | null {
  return firstString(record, ["issue_id", "issueId"]);
}

function normalizedSignalClass(record: NormalizedLedgerRecord): SignalClass | null {
  if (record.outcome === "zero_write") return "B";
  if (record.signal_class) return record.signal_class;
  const causeCode = record.cause_code?.toLowerCase();
  if (causeCode && CLASS_C_CAUSES.has(causeCode)) return "C";
  return record.outcome === "terminal_fail" ? "A" : null;
}

/** Return the class used by WP-B for a normalized run. */
export function classifySignalClass(record: NormalizedLedgerRecord): SignalClass | null {
  return normalizedSignalClass(record);
}

function signalLabelForNormalizedRecord(record: NormalizedLedgerRecord, signalClass: SignalClass): string {
  return record.signal ?? (signalClass === "C" && record.cause_code
    ? record.cause_code
    : DEFAULT_SIGNAL_LABELS[signalClass]);
}

function followLaneAlias(lane: string, aliases: Readonly<Record<string, string>> | undefined): string {
  if (!aliases) return lane;
  let current = lane;
  const seen = new Set<string>();
  while (true) {
    if (seen.has(current)) throw new Error(`Lane alias cycle detected at ${current}`);
    seen.add(current);
    const next = readString(aliases[current]);
    if (!next) return current;
    current = next;
  }
}

/**
 * Resolve a display name to the stable lane key used by state and events.
 * The function is intentionally explicit: callers can bind it to the agents
 * table, while a small alias map supports deterministic rename rollouts.
 */
export function canonicalizeLaneKey(lane: string, options: LaneKeyOptions = {}): string {
  let canonical = readString(lane);
  if (!canonical) throw new Error("Lane key must be a non-empty string");
  if (options.canonicalizeLane) {
    canonical = readString(options.canonicalizeLane(canonical));
    if (!canonical) throw new Error("Lane canonicalizer returned an empty key");
  }
  return followLaneAlias(canonical, options.laneAliases);
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

export function normalizeLedgerRecord(
  value: unknown,
  sourceIndex: number,
  options: ParseLedgerOptions = {},
):
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

  let canonicalLane: string;
  try {
    canonicalLane = canonicalizeLaneKey(lane, options);
  } catch (error) {
    return {
      issue: {
        source_index: sourceIndex,
        reason: error instanceof Error ? error.message : "invalid lane key",
        value: lane,
      },
    };
  }

  const rawFinishedAt = firstValue(value, ["finished_at", "finishedAt", "completed_at", "completedAt"]);
  const finishedAt = rawFinishedAt == null ? null : normalizeTimestamp(rawFinishedAt);
  if (rawFinishedAt != null && finishedAt == null) {
    return { issue: { source_index: sourceIndex, reason: "invalid finished_at", value: rawFinishedAt } };
  }

  const outcome = classifyOutcome(value);
  const signalClass = signalClassFromValues(value, outcome);
  const explicitSignalClass = explicitSignalClassFromRecord(value);
  const explicitSignal = firstString(value, ["signal", "signal_type", "signalType", "failure_kind", "failureKind"]);
  const causeCode = causeCodeFromRecord(value);
  const issueId = issueIdFromRecord(value);
  const preserveSignalMetadata = Boolean(explicitSignalClass || explicitSignal || causeCode || issueId);
  const record: NormalizedLedgerRecord = {
      run_id: runId,
      lane: canonicalLane,
      outcome,
      finished_at: finishedAt,
      source_index: sourceIndex,
  };
  if (preserveSignalMetadata && signalClass) record.signal_class = signalClass;
  if (preserveSignalMetadata && signalClass) record.signal = signalLabelFromRecord(value, signalClass);
  if (issueId) record.issue_id = issueId;
  if (causeCode) record.cause_code = causeCode;
  return {
    record,
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

function parseEntries(
  entries: unknown[],
  metadata: Record<string, unknown> | null,
  indexOffset = 0,
  options: ParseLedgerOptions = {},
): ParsedLedger {
  const records: NormalizedLedgerRecord[] = [];
  const invalid: LedgerIssue[] = [];
  let discoveredMetadata = metadata;

  entries.forEach((entry, entryIndex) => {
    const sourceIndex = indexOffset + entryIndex;
    if (isRecord(entry) && metadataFromValue(entry) && !("run_id" in entry) && !("runId" in entry) && !("id" in entry)) {
      discoveredMetadata = metadataFromValue(entry);
      return;
    }
    const normalized = normalizeLedgerRecord(entry, sourceIndex, options);
    if (normalized.record) records.push(normalized.record);
    else invalid.push(normalized.issue);
  });

  return { metadata: discoveredMetadata, records, invalid };
}

/** Parse either a JSON array/container or newline-delimited JSON. */
export function parseLedgerText(text: string, options: ParseLedgerOptions = {}): ParsedLedger {
  const trimmed = text.replace(/^\uFEFF/, "").trim();
  if (!trimmed) return { metadata: null, records: [], invalid: [] };

  try {
    const parsed = JSON.parse(trimmed) as unknown;
    const container = entriesFromContainer(parsed);
    if (container) return parseEntries(container.entries, container.metadata, 0, options);
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
      const normalized = normalizeLedgerRecord(value, lineIndex, options);
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

export async function parseLedgerFile(path: string, options: ParseLedgerOptions = {}): Promise<ParsedLedger> {
  return parseLedgerText(await readFile(path, "utf8"), options);
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
    if (existing.signal_class || record.signal_class) {
      const existingClass = normalizedSignalClass(existing);
      const recordClass = normalizedSignalClass(record);
      const mergedClass = existing.outcome === "zero_write"
        ? "B"
        : existingClass === "C" || recordClass === "C"
          ? "C"
          : existingClass ?? recordClass;
      if (mergedClass) existing.signal_class = mergedClass;
    }
    existing.signal ??= record.signal;
    existing.issue_id ??= record.issue_id;
    existing.cause_code ??= record.cause_code;
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
    signals: {},
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

function positiveInteger(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.floor(value)
    : fallback;
}

function thresholdFor(signalClass: SignalClass, thresholds: ThresholdConfig = {}): number {
  const configured = thresholds[signalClass];
  if (configured === undefined) return DEFAULT_THRESHOLDS[signalClass];
  const threshold = positiveInteger(configured, 0);
  if (threshold === 0) throw new Error(`Threshold for Class ${signalClass} must be a positive integer`);
  return threshold;
}

function effectiveThreshold(
  signalClass: SignalClass,
  existing: SignalCounterState | undefined,
  thresholds: ThresholdConfig = {},
): number {
  // A calibrated threshold survives a restart until the caller explicitly
  // supplies a replacement. This prevents replay from silently changing the
  // configuration that was in force when the state was written.
  if (Object.prototype.hasOwnProperty.call(thresholds, signalClass)) {
    return thresholdFor(signalClass, thresholds);
  }
  return existing?.threshold ?? DEFAULT_THRESHOLDS[signalClass];
}

function newSignalState(updatedAt: string, threshold: number): SignalCounterState {
  return {
    current_streak: 0,
    first_failure_ts: null,
    last_failure_ts: null,
    last_success_ts: null,
    updated_at: updatedAt,
    threshold,
    alert_emitted: false,
  };
}

function parseSignalState(value: unknown, signalClass: SignalClass): SignalCounterState {
  if (!isRecord(value)) throw new Error(`Counter state signal ${signalClass} is not an object`);
  const updatedAt = normalizeTimestamp(value.updated_at);
  if (!updatedAt) throw new Error(`Counter state signal ${signalClass} has an invalid updated_at`);
  return {
    current_streak: nonNegativeInteger(value.current_streak),
    first_failure_ts: normalizedTimestampOrNull(value.first_failure_ts),
    last_failure_ts: normalizedTimestampOrNull(value.last_failure_ts),
    last_success_ts: normalizedTimestampOrNull(value.last_success_ts),
    updated_at: updatedAt,
    threshold: positiveInteger(value.threshold, DEFAULT_THRESHOLDS[signalClass]),
    alert_emitted: readBoolean(value.alert_emitted),
  };
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

  const signalsValue = isRecord(value.signals) ? value.signals : {};
  const signals: Record<string, LaneSignalStates> = {};
  for (const [lane, rawLaneSignals] of Object.entries(signalsValue)) {
    if (!isRecord(rawLaneSignals)) throw new Error(`Counter state signals for lane ${lane} is not an object`);
    const laneSignals: LaneSignalStates = {};
    for (const signalClass of SIGNAL_CLASSES) {
      const rawSignal = rawLaneSignals[signalClass] ?? rawLaneSignals[signalClass.toLowerCase()];
      if (rawSignal !== undefined) laneSignals[signalClass] = parseSignalState(rawSignal, signalClass);
    }
    if (Object.keys(laneSignals).length > 0) signals[lane] = laneSignals;
  }

  return {
    version: COUNTER_STATE_VERSION,
    updated_at: updatedAt,
    processed_run_ids: [...new Set(processedRunIds)],
    lanes,
    signals,
  };
}

function cloneCounterState(state: CounterState): CounterState {
  return parseCounterState(JSON.parse(JSON.stringify(state)) as unknown);
}

function earliestTimestamp(a: string | null, b: string | null): string | null {
  if (!a) return b;
  if (!b) return a;
  return a <= b ? a : b;
}

function mergedCurrentStreak(
  left: { current_streak: number; last_failure_ts: string | null; last_success_ts: string | null },
  right: { current_streak: number; last_failure_ts: string | null; last_success_ts: string | null },
): number {
  const latestSuccess = latestTimestamp(left.last_success_ts, right.last_success_ts);
  const latestFailure = latestTimestamp(left.last_failure_ts, right.last_failure_ts);

  // A success at or after the latest known failure is the reset boundary for
  // both alias spellings. Check this before the zero/non-zero shortcut so an
  // older active snapshot cannot undo a newer reset.
  if (latestSuccess && (!latestFailure || latestSuccess >= latestFailure)) return 0;

  // If one side contains a later reset than the other side's failures, that
  // side owns the live streak. The other side is stale history from before the
  // rename.
  if (left.last_success_ts && right.last_failure_ts && left.last_success_ts >= right.last_failure_ts) {
    return left.current_streak;
  }
  if (right.last_success_ts && left.last_failure_ts && right.last_success_ts >= left.last_failure_ts) {
    return right.current_streak;
  }

  if (left.current_streak === 0) return right.current_streak;
  if (right.current_streak === 0) return left.current_streak;

  // Two snapshots with the same terminal boundary are the same streak, not
  // two batches to add.  Otherwise, if one side contains a later success, its
  // active streak supersedes the older side; disjoint active periods can be
  // joined when an alias was introduced mid-stream.
  if (
    left.last_failure_ts &&
    right.last_failure_ts &&
    left.last_failure_ts === right.last_failure_ts &&
    left.last_success_ts === right.last_success_ts
  ) {
    return Math.max(left.current_streak, right.current_streak);
  }
  if (left.last_failure_ts && right.last_success_ts && left.last_failure_ts <= right.last_success_ts) {
    return right.current_streak;
  }
  if (right.last_failure_ts && left.last_success_ts && right.last_failure_ts <= left.last_success_ts) {
    return left.current_streak;
  }
  return left.current_streak + right.current_streak;
}

function mergeLegacyLaneStates(left: LaneCounterState, right: LaneCounterState): LaneCounterState {
  return {
    current_streak: mergedCurrentStreak(left, right),
    last_success_ts: latestTimestamp(left.last_success_ts, right.last_success_ts),
    last_failure_ts: latestTimestamp(left.last_failure_ts, right.last_failure_ts),
    updated_at: latestTimestamp(left.updated_at, right.updated_at)!,
    outcome_counts: {
      zero_write: left.outcome_counts.zero_write + right.outcome_counts.zero_write,
      terminal_fail: left.outcome_counts.terminal_fail + right.outcome_counts.terminal_fail,
      success_readonly: left.outcome_counts.success_readonly + right.outcome_counts.success_readonly,
      success: left.outcome_counts.success + right.outcome_counts.success,
      other: left.outcome_counts.other + right.outcome_counts.other,
      running: left.outcome_counts.running + right.outcome_counts.running,
    },
    failure_counts: {
      zero_write: left.failure_counts.zero_write + right.failure_counts.zero_write,
      terminal_fail: left.failure_counts.terminal_fail + right.failure_counts.terminal_fail,
    },
  };
}

function mergeSignalStates(left: SignalCounterState, right: SignalCounterState): SignalCounterState {
  const currentStreak = mergedCurrentStreak(left, right);
  const latestSuccess = latestTimestamp(left.last_success_ts, right.last_success_ts);
  const latestFailure = latestTimestamp(left.last_failure_ts, right.last_failure_ts);
  const successClearedAllKnownFailures = Boolean(
    latestSuccess && latestFailure && latestSuccess >= latestFailure,
  );
  const newerState = left.updated_at >= right.updated_at ? left : right;
  return {
    current_streak: currentStreak,
    first_failure_ts: currentStreak > 0
      ? earliestTimestamp(left.first_failure_ts, right.first_failure_ts)
      : null,
    last_failure_ts: latestFailure,
    last_success_ts: latestSuccess,
    updated_at: latestTimestamp(left.updated_at, right.updated_at)!,
    threshold: newerState.threshold,
    alert_emitted: successClearedAllKnownFailures
      ? false
      : left.alert_emitted || right.alert_emitted,
  };
}

function canonicalizeCounterState(state: CounterState, options: LaneKeyOptions): CounterState {
  const lanes: Record<string, LaneCounterState> = {};
  for (const [lane, laneState] of Object.entries(state.lanes)) {
    const canonicalLane = canonicalizeLaneKey(lane, options);
    const existing = lanes[canonicalLane];
    lanes[canonicalLane] = existing ? mergeLegacyLaneStates(existing, laneState) : laneState;
  }

  const signals: Record<string, LaneSignalStates> = {};
  for (const [lane, laneSignals] of Object.entries(state.signals)) {
    const canonicalLane = canonicalizeLaneKey(lane, options);
    const target = signals[canonicalLane] ?? {};
    for (const signalClass of SIGNAL_CLASSES) {
      const signalState = laneSignals[signalClass];
      if (!signalState) continue;
      const existing = target[signalClass];
      target[signalClass] = existing ? mergeSignalStates(existing, signalState) : signalState;
    }
    if (Object.keys(target).length > 0) signals[canonicalLane] = target;
  }

  return { ...state, lanes, signals };
}

function canonicalizeNormalizedRecord(record: NormalizedLedgerRecord, options: LaneKeyOptions): NormalizedLedgerRecord {
  const lane = canonicalizeLaneKey(record.lane, options);
  return lane === record.lane ? record : { ...record, lane };
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
  const state = canonicalizeCounterState(cloneCounterState(initialState), options);
  const now = normalizeNow(options.now);
  const canonicalRecords = inputRecords.map((record) => canonicalizeNormalizedRecord(record, options));
  const coalesced = coalesceRecords(canonicalRecords);
  const processed = new Set(state.processed_run_ids);
  const applied: NormalizedLedgerRecord[] = [];
  const alreadyProcessedRunIds: string[] = [];
  const skipped: SkippedRecord[] = [];
  const thresholdCrossings: ThresholdCrossingEvent[] = [];

  for (const record of coalesced.records) {
    if (processed.has(record.run_id)) {
      alreadyProcessedRunIds.push(record.run_id);
      continue;
    }

    const signalClass = classifySignalClass(record);
    const requiresTerminalTimestamp =
      FAILURE_SET.has(record.outcome as FailureOutcome) ||
      SUCCESS_SET.has(record.outcome) ||
      signalClass !== null;
    if (requiresTerminalTimestamp && !requireFinishedAt(record)) {
      skipped.push({
        run_id: record.run_id,
        lane: record.lane,
        outcome: record.outcome,
        reason: "failure/success record is missing finished_at",
      });
      continue;
    }

    // Keep the WP-A aggregate shape intact for existing readers.  Class C is
    // a distinct stranding signal whose outcome may be `success`, so it must
    // not reset or mutate the legacy failure counter.
    if (signalClass !== "C") {
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
    }

    if (signalClass) {
      const laneSignals = state.signals[record.lane] ?? {};
      const threshold = thresholdFor(signalClass, options.thresholds);
      const signalState = laneSignals[signalClass] ?? newSignalState(now, threshold);
      const previousStreak = signalState.current_streak;
      signalState.threshold = threshold;
      signalState.updated_at = now;
      signalState.current_streak += 1;
      if (previousStreak === 0 || !signalState.first_failure_ts) {
        signalState.first_failure_ts = record.finished_at;
      }
      signalState.last_failure_ts = latestTimestamp(signalState.last_failure_ts, record.finished_at);

      if (signalState.current_streak >= threshold && !signalState.alert_emitted) {
        const firstFailureTs = signalState.first_failure_ts;
        const lastFailureTs = signalState.last_failure_ts;
        if (!firstFailureTs || !lastFailureTs) {
          throw new Error(`Signal ${signalClass} crossed without failure timestamps`);
        }
        const event: ThresholdCrossingEvent = {
          event_type: "threshold_crossing",
          emitted_at: now,
          lane_id: record.lane,
          class: signalClass,
          signal: signalLabelForNormalizedRecord(record, signalClass),
          count: signalState.current_streak,
          threshold,
          first_failure_ts: firstFailureTs,
          last_failure_ts: lastFailureTs,
          run_id: record.run_id,
        };
        if (record.issue_id) event.issue_id = record.issue_id;
        if (record.cause_code) event.cause_code = record.cause_code;
        thresholdCrossings.push(event);
        signalState.alert_emitted = true;
      }
      laneSignals[signalClass] = signalState;
      state.signals[record.lane] = laneSignals;
    } else if (SUCCESS_SET.has(record.outcome)) {
      const laneSignals = state.signals[record.lane];
      if (laneSignals) {
        for (const signalClass of SIGNAL_CLASSES) {
          const signalState = laneSignals[signalClass];
          if (!signalState) continue;
          signalState.current_streak = 0;
          signalState.first_failure_ts = null;
          signalState.last_success_ts = latestTimestamp(signalState.last_success_ts, record.finished_at);
          signalState.updated_at = now;
          signalState.threshold = thresholdFor(signalClass, options.thresholds);
          signalState.alert_emitted = false;
        }
      }
    }

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
    threshold_crossings: thresholdCrossings,
    emitted_alerts: thresholdCrossings,
    events: thresholdCrossings,
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
