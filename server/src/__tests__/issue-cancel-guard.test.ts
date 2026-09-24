import { describe, expect, it } from "vitest";
import type { Db } from "@paperclipai/db";
import { issues, issueThreadInteractions } from "@paperclipai/db";
import {
  assertChurnCancelGuardAllowed,
  CHURN_CANCEL_GUARD_DISABLE_ENV,
  churnCancelDryRunReport,
  churnCancelGuardDisabled,
  evaluateChurnCancelGuard,
  type ChurnCancelGuardIssue,
} from "../services/issue-cancel-guard.js";

// Minimal fake of the two drizzle chains the guard runs: a pending-interaction
// count and a non-terminal children count, distinguished by the table passed
// to .from().
interface FakeDbPlan {
  pendingInteractionIds?: string[];
  childRows?: Array<{ id: string }>;
}

function makeFakeDb(plan: FakeDbPlan) {
  const select = () => ({
    from(table: unknown) {
      return {
        where(_condition: unknown) {
          if (table === issueThreadInteractions) {
            return Promise.resolve(
              (plan.pendingInteractionIds ?? []).map((id) => ({ id })),
            );
          }
          if (table === issues) {
            return Promise.resolve(plan.childRows ?? []);
          }
          return Promise.resolve([]);
        },
      };
    },
  });
  return { db: { select } as unknown as Pick<Db, "select"> };
}

function card(overrides: Partial<ChurnCancelGuardIssue> = {}): ChurnCancelGuardIssue {
  return { id: "issue-1", companyId: "company-1", status: "in_progress", ...overrides };
}

const DRIVER = "Platform lead: cancelling per churn-guard rule X";

describe("issue cancel churn guard (SON-4111)", () => {
  it("excludes cards with pending interactions", async () => {
    const { db } = makeFakeDb({ pendingInteractionIds: ["ia-1"] });
    const evaluation = await evaluateChurnCancelGuard(db, card(), {
      driverComment: DRIVER,
    });
    expect(evaluation.wouldCancel).toBe(false);
    expect(evaluation.rules).toContain("pending_interaction");
    expect(evaluation.pendingInteractionIds).toEqual(["ia-1"]);
  });

  it("excludes cards with non-terminal children (active tree edges)", async () => {
    const { db } = makeFakeDb({ childRows: [{ id: "child-1" }] });
    const evaluation = await evaluateChurnCancelGuard(db, card(), {
      driverComment: DRIVER,
    });
    expect(evaluation.wouldCancel).toBe(false);
    expect(evaluation.rules).toContain("active_child");
    expect(evaluation.activeChildIds).toEqual(["child-1"]);
  });

  it("refuses a bare churn cancel without a driver comment", async () => {
    // The 2026-09-23 23:39Z wave shape: no pending interaction, no children,
    // no comment - a silent status->cancelled PATCH.
    const { db } = makeFakeDb({});
    const evaluation = await evaluateChurnCancelGuard(db, card(), {
      driverComment: null,
    });
    expect(evaluation.wouldCancel).toBe(false);
    expect(evaluation.rules).toEqual(["driver_comment_required"]);
  });

  it("refuses a whitespace-only driver comment", async () => {
    const { db } = makeFakeDb({});
    const evaluation = await evaluateChurnCancelGuard(db, card(), {
      driverComment: "   ",
    });
    expect(evaluation.rules).toContain("driver_comment_required");
  });

  it("allows a driven cancel of a clean active card", async () => {
    const { db } = makeFakeDb({});
    const evaluation = await evaluateChurnCancelGuard(db, card(), {
      driverComment: DRIVER,
    });
    expect(evaluation.wouldCancel).toBe(true);
    expect(evaluation.rules).toEqual([]);
  });

  describe("assertChurnCancelGuardAllowed decision codes", () => {
    it("reports churn_guard_pending_interaction first", async () => {
      const { db } = makeFakeDb({
        pendingInteractionIds: ["ia-1"],
        childRows: [{ id: "child-1" }],
      });
      const decision = await assertChurnCancelGuardAllowed(db, {
        issue: card(),
        driverComment: null,
      });
      expect(decision.ok).toBe(false);
      if (!decision.ok) {
        expect(decision.code).toBe("churn_guard_pending_interaction");
        expect(decision.details.pendingInteractionIds).toEqual(["ia-1"]);
      }
    });

    it("reports churn_guard_active_children when only children block", async () => {
      const { db } = makeFakeDb({ childRows: [{ id: "child-1" }] });
      const decision = await assertChurnCancelGuardAllowed(db, {
        issue: card(),
        driverComment: DRIVER,
      });
      expect(decision.ok).toBe(false);
      if (!decision.ok) {
        expect(decision.code).toBe("churn_guard_active_children");
        expect(decision.details.activeChildIds).toEqual(["child-1"]);
      }
    });

    it("reports churn_guard_driver_comment_required for bare cancels", async () => {
      const { db } = makeFakeDb({});
      const decision = await assertChurnCancelGuardAllowed(db, {
        issue: card(),
        driverComment: null,
      });
      expect(decision.ok).toBe(false);
      if (!decision.ok) {
        expect(decision.code).toBe("churn_guard_driver_comment_required");
        expect(decision.error).toMatch(/driver comment/i);
      }
    });

    it("allows a driven cancel and returns the clean evaluation", async () => {
      const { db } = makeFakeDb({});
      const decision = await assertChurnCancelGuardAllowed(db, {
        issue: card(),
        driverComment: DRIVER,
      });
      expect(decision.ok).toBe(true);
    });
  });

  it("kill switch disables the guard", () => {
    expect(churnCancelGuardDisabled({ [CHURN_CANCEL_GUARD_DISABLE_ENV]: "off" })).toBe(true);
    expect(churnCancelGuardDisabled({ [CHURN_CANCEL_GUARD_DISABLE_ENV]: "0" })).toBe(true);
    expect(churnCancelGuardDisabled({ [CHURN_CANCEL_GUARD_DISABLE_ENV]: "false" })).toBe(true);
    expect(churnCancelGuardDisabled({ [CHURN_CANCEL_GUARD_DISABLE_ENV]: "on" })).toBe(false);
    expect(churnCancelGuardDisabled({})).toBe(false);
  });

  it("replay of the SON-505/2116 wave shapes: zero would-cancel", async () => {
    // SON-505 shape: restored epic, blocked on its implementation child,
    // anti-churn interaction re-armed.
    const epic = makeFakeDb({
      pendingInteractionIds: ["anti-churn-505"],
      childRows: [{ id: "son-2116" }],
    }).db;
    // SON-2116 shape: ready implementation child with its own pending
    // interaction.
    const child = makeFakeDb({
      pendingInteractionIds: ["anti-churn-2116"],
    }).db;
    // The wave driver shape: clean active card, silent bare cancel.
    const clean = makeFakeDb({}).db;

    const report = [];
    for (const db of [epic, child, clean]) {
      report.push(
        await evaluateChurnCancelGuard(db, card(), { driverComment: null }),
      );
    }
    expect(report.every((entry) => !entry.wouldCancel)).toBe(true);
    expect(report[0].rules).toEqual([
      "pending_interaction",
      "active_child",
      "driver_comment_required",
    ]);
    expect(report[1].rules).toEqual([
      "pending_interaction",
      "driver_comment_required",
    ]);
    expect(report[2].rules).toEqual(["driver_comment_required"]);
  });

  it("dry-run report shows zero would-cancel for cards with pending interactions", async () => {
    const { db } = makeFakeDb({ pendingInteractionIds: ["ia-1", "ia-2"] });
    const report = await churnCancelDryRunReport(
      db,
      [
        card({ id: "a", status: "in_progress" }),
        card({ id: "b", status: "todo" }),
      ],
      { driverComment: "operator cancel with a stated reason" },
    );
    expect(report).toHaveLength(2);
    expect(report.every((entry) => !entry.wouldCancel)).toBe(true);
    expect(report.flatMap((entry) => entry.rules)).toContain(
      "pending_interaction",
    );
  });
});
