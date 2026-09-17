import { parseObject } from "../../adapters/utils.js";

/**
 * Board write-transport failure classification (SON-1775).
 *
 * A wake whose board writes were refused at the transport layer — cross-issue
 * write denials (`cross_issue_influence*`, `issue_write_*`), agent-token auth
 * flaps ("Agent token did not verify"), or board-API/interaction-route 5xx —
 * failed to DELIVER its output, but the failure says nothing about the issue's
 * real state. Treating such a run as evidence that the issue is stranded
 * fail-closes live work: an issue whose disposition writes were denied looks
 * "unchanged / without a live execution path", and the stranded-work
 * reconciler flips it to `blocked` even though the actual state is, for
 * example, "decision recorded, awaiting execution wake".
 *
 * Recovery must treat this class like any other transport condition: log it,
 * keep retrying per the existing retry policy, surface it as a wake warning —
 * and never derive an issue-level `blocked` status from it.
 *
 * The denial patterns mirror the canonical issue-write denial codes in
 * `packages/shared/src/issue-write-denial.ts` (`ISSUE_WRITE_DENIAL_CODES`),
 * so every boundary the write API can return is recognized here. The shared
 * package is not imported to keep the recovery module dependency-light; keep
 * the two lists byte-equivalent when codes change.
 */

export type WriteTransportFailureKind = "write_denial" | "auth_flap" | "board_api_5xx";

export interface WriteTransportFailure {
  kind: WriteTransportFailureKind;
  /** The exact matched fragment, for logs/activity details. */
  matched: string;
}

interface WriteTransportRunLike {
  id?: string | null;
  errorCode?: string | null;
  error?: string | null;
  resultJson?: unknown;
}

/** Upper bound per evidence field so pathological error payloads stay cheap. */
const MAX_EVIDENCE_SCAN_CHARS = 8_000;

const WRITE_DENIAL_PATTERNS: RegExp[] = [
  /cross_issue_influence[a-z_]*/i,
  /issue_write_not_visible/i,
  /issue_write_actor_class_excluded/i,
  /issue_write_responsible_user_ceiling/i,
  /issue_write_responsible_user_unavailable/i,
  /issue_write_assignee_run_lock/i,
  /issue_write_attribution_spoof_rejected/i,
];

const AUTH_FLAP_PATTERNS: RegExp[] = [
  /agent token did not verify/i,
];

/**
 * Board-API 5xx counts as write-transport failure only when tied to board
 * write context (interaction routes, the paperclip/board API, issue thread
 * writes). Generic provider/upstream 5xx belongs to the existing
 * transient-infra continuation classification, not here.
 */
const BOARD_API_5XX_PATTERN =
  /\b5\d{2}\b[^\n]{0,120}\b(?:interaction|paperclip[ _-]?api|board[ _-]?api|issue[ _-]?(?:write|comment|thread|accept))|`?`?(?:interaction|paperclip[ _-]?api|board[ _-]?api|issue[ _-]?(?:write|comment|thread|accept))`?`?[^\n]{0,120}\b5\d{2}\b/i;

function boundedEvidence(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (trimmed.length === 0) return null;
  return trimmed.slice(0, MAX_EVIDENCE_SCAN_CHARS);
}

function evidenceHaystacks(run: WriteTransportRunLike): string[] {
  const resultJson = parseObject(run.resultJson);
  const stringifiedResult = Object.keys(resultJson).length > 0
    ? boundedEvidence(JSON.stringify(resultJson))
    : null;
  return [
    boundedEvidence(run.errorCode),
    boundedEvidence(run.error),
    stringifiedResult,
  ].filter((value): value is string => value !== null);
}

/**
 * True when the run's recorded failure evidence is a board write-transport
 * failure rather than an execution/issue-level condition. Returns the kind
 * and matched fragment so callers can log and surface the specific cause.
 */
export function classifyWriteTransportFailure(
  run: WriteTransportRunLike | null | undefined,
): WriteTransportFailure | null {
  if (!run) return null;
  for (const haystack of evidenceHaystacks(run)) {
    for (const pattern of WRITE_DENIAL_PATTERNS) {
      const matched = haystack.match(pattern);
      if (matched) return { kind: "write_denial", matched: matched[0] };
    }
    for (const pattern of AUTH_FLAP_PATTERNS) {
      const matched = haystack.match(pattern);
      if (matched) return { kind: "auth_flap", matched: matched[0] };
    }
    const matched = haystack.match(BOARD_API_5XX_PATTERN);
    if (matched) return { kind: "board_api_5xx", matched: matched[0] };
  }
  return null;
}

/** Human-readable warning carried by recovery wakes after a deferral. */
export function writeTransportRecoveryWarning(failure: WriteTransportFailure): string {
  const reason = failure.kind === "write_denial"
    ? "board writes were denied by an issue-write boundary"
    : failure.kind === "auth_flap"
      ? "agent credentials were rejected by the board API"
      : "the board API returned a server error during writes";
  return (
    `Recovery deferred: the latest run could not deliver its board writes (${reason}; ` +
    `matched "${failure.matched}"). The issue status was left unchanged and recovery ` +
    "keeps retrying per the existing policy."
  );
}
