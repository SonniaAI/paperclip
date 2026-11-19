import { describe, expect, it } from "vitest";
import { wakeAgentSchema } from "./agent.js";

// SON-6308: the interactive-session helper posts the user's request as a
// bare `message`. The schema must carry it through (the wakeup route folds
// it into `payload.interactiveUserMessage`) instead of stripping it
// silently, which is how interactive wakes lost their briefs.
describe("wakeAgentSchema", () => {
  it("accepts a bare interactive message alongside the wake reason", () => {
    const result = wakeAgentSchema.safeParse({
      source: "on_demand",
      triggerDetail: "manual",
      reason: "interactive_request",
      idempotencyKey: "interactive-test",
      message: "Please ship the thing",
    });
    expect(result.success).toBe(true);
    expect(
      result.success && result.data.message,
    ).toBe("Please ship the thing");
  });

  it("keeps payload structured metadata when message is absent", () => {
    const result = wakeAgentSchema.safeParse({
      reason: "interactive_request",
      payload: { interactiveUserMessage: "payload-carried brief" },
    });
    expect(result.success).toBe(true);
    expect(
      result.success && result.data.payload?.interactiveUserMessage,
    ).toBe("payload-carried brief");
    expect(result.success && result.data.message).toBeUndefined();
  });

  it("still strips unknown top-level fields it does not declare", () => {
    const result = wakeAgentSchema.safeParse({
      reason: "interactive_request",
      notARealField: "dropped",
    });
    expect(result.success).toBe(true);
    expect(
      result.success && (result.data as Record<string, unknown>).notARealField,
    ).toBeUndefined();
  });
});
