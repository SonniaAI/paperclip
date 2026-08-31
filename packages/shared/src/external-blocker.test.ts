import { describe, expect, it } from "vitest";

import { createIssueSchema, updateIssueValidatedSchema } from "./validators/issue.js";

const baseCreate = { title: "t", description: "d" } as const;

describe("blocked status requires an attributable reason", () => {
  it("rejects create with status blocked and no edges or externalBlocker", () => {
    const r = createIssueSchema.safeParse({ ...baseCreate, status: "blocked" });
    expect(r.success).toBe(false);
    if (!r.success) expect(r.error.issues.some((i) => i.path[0] === "status")).toBe(true);
  });

  it("accepts create with status blocked and an externalBlocker", () => {
    const r = createIssueSchema.safeParse({
      ...baseCreate,
      status: "blocked",
      externalBlocker: { owner: "Arthur", note: "needs founder sign-off" },
    });
    expect(r.success).toBe(true);
  });

  it("accepts create with status blocked and dependency edges", () => {
    const r = createIssueSchema.safeParse({
      ...baseCreate,
      status: "blocked",
      blockedByIssueIds: ["00000000-0000-4000-8000-000000000001"],
    });
    expect(r.success).toBe(true);
  });

  it("rejects update to blocked without a reason", () => {
    const r = updateIssueValidatedSchema.safeParse({ status: "blocked" });
    expect(r.success).toBe(false);
  });

  it("rejects update clearing the last reason while staying blocked", () => {
    const r = updateIssueValidatedSchema.safeParse({
      status: "blocked",
      blockedByIssueIds: [],
      externalBlocker: null,
    });
    expect(r.success).toBe(false);
  });

  it("accepts update to blocked with an externalBlocker", () => {
    const r = updateIssueValidatedSchema.safeParse({
      status: "blocked",
      externalBlocker: { owner: "Szonja", note: "voice review" },
    });
    expect(r.success).toBe(true);
  });
});
