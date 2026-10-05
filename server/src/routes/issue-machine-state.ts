// SON-1524 B1/B2 — run-scoped service identity (Option B) helpers.
//
// Pure, dependency-free decision logic extracted from routes/issues.ts so the
// authorization/noop semantics added by SON-1524 are unit-testable without
// booting the route stack.
//
// B1: a Paperclip run that is LIVE and anchored to an issue (its checkoutRunId
// or executionRunId) is the service identity executing on that issue. The
// anchor/equality semantics deliberately mirror the pre-existing run-ownership
// predicate used for comment suppression in routes/issues.ts.

export type IssueRunAnchorInput = {
  actorRunId: string | null | undefined;
  checkoutRunId: string | null | undefined;
  executionRunId: string | null | undefined;
};

export function isRunAnchoredToIssue(input: IssueRunAnchorInput): boolean {
  return (
    typeof input.actorRunId === "string"
    && input.actorRunId.length > 0
    && (input.actorRunId === input.checkoutRunId || input.actorRunId === input.executionRunId)
  );
}

// Authorization anchor handed to the monitor-management gate for writes that
// target an EXISTING issue. getRun resolves liveness and ownership from the
// heartbeat run store; only a run whose status is "running" AND whose
// company/agent match the authenticated actor counts as live.
export type IssueMonitorRunRecord = {
  status?: string | null;
  companyId?: string | null;
  agentId?: string | null;
};

export type IssueMonitorRunAnchor = {
  checkoutRunId: string | null | undefined;
  executionRunId: string | null | undefined;
  getRun: (
    runId: string,
  ) => Promise<IssueMonitorRunRecord | null | undefined>
    | IssueMonitorRunRecord
    | null
    | undefined;
};

export const ISSUE_MACHINE_STATE_PATCH_FIELDS = [
  "status",
  "assigneeAgentId",
  "assigneeUserId",
  "blockedByIssueIds",
  "executionPolicy",
  "resumeRequested",
] as const;

export const MACHINE_STATE_NOOP_REASON = "machine_state_patch_had_no_effect" as const;

// B2 — observability only, loosens nothing: when a PATCH that already passed
// auth/validation/recovery/resume guards carries machine-state intent (any
// field above) but the committed write produced an EMPTY effective change set,
// the response says so explicitly instead of a bare 200 + changes:{} (the
// historical resumeRequested-only signature). The reason is a stable literal
// and discloses no extra state.
export function classifyMachineStatePatchNoop(input: {
  requested: Record<string, unknown>;
  effectiveChanges: Record<string, unknown> | null | undefined;
}): { noop: boolean; noopReason?: typeof MACHINE_STATE_NOOP_REASON } {
  const requestedMachineStateFields = ISSUE_MACHINE_STATE_PATCH_FIELDS.filter(
    (field) => input.requested[field] !== undefined,
  );
  if (requestedMachineStateFields.length === 0) return { noop: false };
  if (Object.keys(input.effectiveChanges ?? {}).length > 0) return { noop: false };
  return { noop: true, noopReason: MACHINE_STATE_NOOP_REASON };
}
