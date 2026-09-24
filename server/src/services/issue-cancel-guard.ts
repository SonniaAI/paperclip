import { and, eq, notInArray } from "drizzle-orm";
import type { Db } from "@paperclipai/db";
import { issues, issueThreadInteractions } from "@paperclipai/db";

// SON-4111 kernel churn guard.
//
// Evidence (2026-09-23 23:38-23:40Z wave): 28+ active cards were cancelled
// within ~70 seconds through bare PATCH status updates (actor-attributed API
// caller, run_id null), including a founder-initiated epic (SON-505) and its
// ready implementation child (SON-2116). Each cancel also wiped the card's
// execution policy, removed reviewers, and expired pending interactions as
// "issue_closed". None of the cancelled cards carried a driver comment naming
// the actor or the rule that ordered the cancellation.
//
// This guard makes that class impossible from the generic issue-update path:
//
//   R1 pending_interaction   - a card with pending thread interactions is
//                              awaiting an answer and is never stale enough
//                              for a bare churn cancel (hard exclusion).
//   R2 active_child          - a card with non-terminal children has active
//                              tree edges; cancelling the parent orphans live
//                              work. Dedicated tree flows handle descendants
//                              explicitly and do not go through this guard.
//   R3 driver_comment_required
//                            - a cancelled active card must always carry an
//                              on-card driver comment naming the actor + the
//                              rule that ordered the cancellation. The comment
//                              surface of the same update request is that
//                              driver; without it the cancel is refused.
//
// "wouldCancel" in evaluations and dry-run reports means "the guard would
// allow this cancellation to proceed": a card with pending interactions must
// always report wouldCancel === false.
//
// Kill switch: PAPERCLIP_CHURN_CANCEL_GUARD=off|0|false disables the guard for
// emergency operator use. Default is on.

export const CHURN_CANCEL_GUARD_DISABLE_ENV = "PAPERCLIP_CHURN_CANCEL_GUARD";

export type ChurnCancelGuardRule =
  | "pending_interaction"
  | "active_child"
  | "driver_comment_required";

export interface ChurnCancelGuardIssue {
  id: string;
  companyId: string;
  status: string;
}

export interface ChurnCancelGuardEvaluation {
  issueId: string;
  wouldCancel: boolean;
  rules: ChurnCancelGuardRule[];
  pendingInteractionIds: string[];
  activeChildIds: string[];
}

export function churnCancelGuardDisabled(
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  const value = (env[CHURN_CANCEL_GUARD_DISABLE_ENV] ?? "").toLowerCase();
  return value === "off" || value === "0" || value === "false";
}

export async function evaluateChurnCancelGuard(
  db: Pick<Db, "select">,
  issue: ChurnCancelGuardIssue,
  input: { driverComment?: string | null },
): Promise<ChurnCancelGuardEvaluation> {
  const evaluation: ChurnCancelGuardEvaluation = {
    issueId: issue.id,
    wouldCancel: true,
    rules: [],
    pendingInteractionIds: [],
    activeChildIds: [],
  };

  // R1 - pending thread interactions are a hard exclusion.
  const pending = await db
    .select({ id: issueThreadInteractions.id })
    .from(issueThreadInteractions)
    .where(
      and(
        eq(issueThreadInteractions.issueId, issue.id),
        eq(issueThreadInteractions.status, "pending"),
      ),
    );
  if (pending.length > 0) {
    evaluation.rules.push("pending_interaction");
    evaluation.pendingInteractionIds = pending.map((row) => row.id);
  }

  // R2 - non-terminal children are active tree edges.
  const children = await db
    .select({ id: issues.id })
    .from(issues)
    .where(
      and(
        eq(issues.companyId, issue.companyId),
        eq(issues.parentId, issue.id),
        notInArray(issues.status, ["done", "cancelled"]),
      ),
    );
  if (children.length > 0) {
    evaluation.rules.push("active_child");
    evaluation.activeChildIds = children.map((row) => row.id);
  }

  // R3 - every cancelled active card needs an on-card driver comment naming
  // the actor + rule. An empty/whitespace comment is not a driver.
  if (!input.driverComment || input.driverComment.trim().length === 0) {
    evaluation.rules.push("driver_comment_required");
  }

  evaluation.wouldCancel = evaluation.rules.length === 0;
  return evaluation;
}

export type ChurnCancelGuardDecision =
  | { ok: true; evaluation: ChurnCancelGuardEvaluation }
  | {
      ok: false;
      code:
        | "churn_guard_pending_interaction"
        | "churn_guard_active_children"
        | "churn_guard_driver_comment_required";
      error: string;
      details: Record<string, unknown>;
    };

export async function assertChurnCancelGuardAllowed(
  db: Pick<Db, "select">,
  input: {
    issue: ChurnCancelGuardIssue;
    driverComment?: string | null;
    env?: NodeJS.ProcessEnv;
  },
): Promise<ChurnCancelGuardDecision> {
  if (churnCancelGuardDisabled(input.env)) {
    return {
      ok: true,
      evaluation: {
        issueId: input.issue.id,
        wouldCancel: true,
        rules: [],
        pendingInteractionIds: [],
        activeChildIds: [],
      },
    };
  }
  const evaluation = await evaluateChurnCancelGuard(db, input.issue, input);
  if (evaluation.rules.includes("pending_interaction")) {
    return {
      ok: false,
      code: "churn_guard_pending_interaction",
      error:
        "Churn guard: this card has pending thread interactions and cannot be cancelled by a bare status update. Resolve or expire the pending interactions first, or use the dedicated restore/tree flows.",
      details: {
        issueId: input.issue.id,
        rules: evaluation.rules,
        pendingInteractionIds: evaluation.pendingInteractionIds,
      },
    };
  }
  if (evaluation.rules.includes("active_child")) {
    return {
      ok: false,
      code: "churn_guard_active_children",
      error:
        "Churn guard: this card has non-terminal children (active tree edges) and cannot be cancelled by a bare status update. Cancel or detach the children first, or use the dedicated tree flows that handle descendants explicitly.",
      details: {
        issueId: input.issue.id,
        rules: evaluation.rules,
        activeChildIds: evaluation.activeChildIds,
      },
    };
  }
  if (evaluation.rules.includes("driver_comment_required")) {
    return {
      ok: false,
      code: "churn_guard_driver_comment_required",
      error:
        "Churn guard: cancelling an active card requires an on-card driver comment naming the actor and the rule that ordered the cancellation. Re-send the status change with a comment body describing who is cancelling and under which rule.",
      details: { issueId: input.issue.id, rules: evaluation.rules },
    };
  }
  return { ok: true, evaluation };
}

// Dry-run report for the churn guard: evaluate a batch of candidate cards
// without mutating anything. Cards with pending interactions must always come
// back wouldCancel === false.
export async function churnCancelDryRunReport(
  db: Pick<Db, "select">,
  candidates: ReadonlyArray<ChurnCancelGuardIssue>,
  input: { driverComment?: string | null } = {},
): Promise<ChurnCancelGuardEvaluation[]> {
  const report: ChurnCancelGuardEvaluation[] = [];
  for (const issue of candidates) {
    report.push(await evaluateChurnCancelGuard(db, issue, input));
  }
  return report;
}
