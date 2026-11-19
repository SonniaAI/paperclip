import { describe, expect, it } from "vitest";
import {
  INTERACTIVE_WAKE_REPLY_CONVERSATION_REF_PATTERN,
  enrichInteractiveWakeContextFields,
} from "../services/heartbeat.js";

const CONVERSATION_REF = `conv_${"b".repeat(32)}`;

describe("enrichInteractiveWakeContextFields", () => {
  it("maps the interactive user message onto the wake instruction", () => {
    const contextSnapshot: Record<string, unknown> = {};
    enrichInteractiveWakeContextFields(contextSnapshot, {
      interactiveUserMessage: "Arthur asked where the AWS credentials live.",
    });
    expect(contextSnapshot.wakeMessage).toBe(
      "Arthur asked where the AWS credentials live.",
    );
  });

  it("keeps a canonical wakeMessage already on the snapshot", () => {
    const contextSnapshot: Record<string, unknown> = {
      wakeMessage: "coordination instruction",
    };
    enrichInteractiveWakeContextFields(contextSnapshot, {
      interactiveUserMessage: "founder question",
    });
    expect(contextSnapshot.wakeMessage).toBe("coordination instruction");
  });

  it("binds a well-formed reply conversation ref and owning agent", () => {
    const contextSnapshot: Record<string, unknown> = {};
    enrichInteractiveWakeContextFields(contextSnapshot, {
      replyConversationRef: CONVERSATION_REF,
      replyAgentId: "sonnia",
    });
    expect(contextSnapshot.replyConversationRef).toBe(CONVERSATION_REF);
    expect(contextSnapshot.replyAgentId).toBe("sonnia");
    expect(CONVERSATION_REF).toMatch(INTERACTIVE_WAKE_REPLY_CONVERSATION_REF_PATTERN);
  });

  it("rejects a malformed conversation ref without binding a partial pair", () => {
    const contextSnapshot: Record<string, unknown> = {};
    enrichInteractiveWakeContextFields(contextSnapshot, {
      replyConversationRef: "conv-not-hex",
      replyAgentId: "sonnia",
    });
    expect(contextSnapshot.replyConversationRef).toBeUndefined();
    expect(contextSnapshot.replyAgentId).toBeUndefined();
  });

  it("rejects a malformed owning agent id without binding a partial pair", () => {
    const contextSnapshot: Record<string, unknown> = {};
    enrichInteractiveWakeContextFields(contextSnapshot, {
      replyConversationRef: CONVERSATION_REF,
      replyAgentId: "Not A Slug",
    });
    expect(contextSnapshot.replyConversationRef).toBeUndefined();
    expect(contextSnapshot.replyAgentId).toBeUndefined();
  });

  it("ignores payload fields when the snapshot already carries a binding", () => {
    const contextSnapshot: Record<string, unknown> = {
      replyConversationRef: `conv_${"c".repeat(32)}`,
      replyAgentId: "sonnia",
    };
    enrichInteractiveWakeContextFields(contextSnapshot, {
      replyConversationRef: CONVERSATION_REF,
      replyAgentId: "other-agent",
    });
    expect(contextSnapshot.replyConversationRef).toBe(`conv_${"c".repeat(32)}`);
    expect(contextSnapshot.replyAgentId).toBe("sonnia");
  });

  it("tolerates a missing payload", () => {
    const contextSnapshot: Record<string, unknown> = {};
    expect(() =>
      enrichInteractiveWakeContextFields(contextSnapshot, null),
    ).not.toThrow();
    expect(contextSnapshot.wakeMessage).toBeUndefined();
  });
});
