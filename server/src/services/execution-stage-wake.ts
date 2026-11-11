import type { IssueExecutionState } from "@paperclipai/shared";

/**
 * Execution-stage wake context shared by the fresh stage dispatch path
 * (routes/issues.ts) and the recovery dispatch paths. Fresh dispatches have
 * always carried it; recovery wakes MUST carry it too so a stage-bound
 * reviewer/approver whose run ended before consuming the stage decision can
 * still satisfy stage completion gates (wakeRole/stageId/currentParticipant/
 * allowedActions) exactly like a fresh dispatch. Without it, evidence-gated
 * stages strand permanently after participant recovery (SON-6211).
 */
export type ExecutionStageWakeContext = {
  wakeRole: "reviewer" | "approver" | "executor";
  stageId: string | null;
  stageType: IssueExecutionState["currentStageType"];
  currentParticipant: IssueExecutionState["currentParticipant"];
  returnAssignee: IssueExecutionState["returnAssignee"];
  reviewRequest: IssueExecutionState["reviewRequest"];
  lastDecisionOutcome: IssueExecutionState["lastDecisionOutcome"];
  allowedActions: string[];
};

export function buildExecutionStageWakeContext(input: {
  state: IssueExecutionState;
  wakeRole: ExecutionStageWakeContext["wakeRole"];
  allowedActions: string[];
}): ExecutionStageWakeContext {
  return {
    wakeRole: input.wakeRole,
    stageId: input.state.currentStageId,
    stageType: input.state.currentStageType,
    currentParticipant: input.state.currentParticipant,
    returnAssignee: input.state.returnAssignee,
    reviewRequest: input.state.reviewRequest ?? null,
    lastDecisionOutcome: input.state.lastDecisionOutcome,
    allowedActions: input.allowedActions,
  };
}

export function executionStageWakeRoleForStageType(
  stageType: IssueExecutionState["currentStageType"],
): "reviewer" | "approver" {
  return stageType === "approval" ? "approver" : "reviewer";
}
