import { describe, expect, it } from "vitest";
import {
  buildAgentParams,
  buildReplyDeliveryRequest,
  classifyReplyDelivery,
  resolveClaimedApiKeyPath,
  resolveSessionKey,
} from "./execute.js";

describe("resolveSessionKey", () => {
  it("prefixes run-scoped session keys with the configured agent", () => {
    expect(
      resolveSessionKey({
        strategy: "run",
        configuredSessionKey: null,
        agentId: "meridian",
        runId: "run-123",
        issueId: null,
      }),
    ).toBe("agent:meridian:paperclip:run:run-123");
  });

  it("prefixes issue-scoped session keys with the configured agent", () => {
    expect(
      resolveSessionKey({
        strategy: "issue",
        configuredSessionKey: null,
        agentId: "meridian",
        runId: "run-123",
        issueId: "issue-456",
      }),
    ).toBe("agent:meridian:paperclip:issue:issue-456");
  });

  it("prefixes fixed session keys with the configured agent", () => {
    expect(
      resolveSessionKey({
        strategy: "fixed",
        configuredSessionKey: "paperclip",
        agentId: "meridian",
        runId: "run-123",
        issueId: null,
      }),
    ).toBe("agent:meridian:paperclip");
  });

  it("does not double-prefix an already-routed session key", () => {
    expect(
      resolveSessionKey({
        strategy: "fixed",
        configuredSessionKey: "agent:meridian:paperclip",
        agentId: "meridian",
        runId: "run-123",
        issueId: null,
      }),
    ).toBe("agent:meridian:paperclip");
  });
});

describe("buildAgentParams", () => {
  it("strips root-level paperclip fields from gateway agent params", () => {
    expect(
      buildAgentParams({
        payloadTemplate: {
          text: "old text",
          paperclip: { stale: true },
          keep: "value",
        },
        message: "wake text",
        sessionKey: "agent:meridian:paperclip:issue:issue-456",
        runId: "run-123",
        configuredAgentId: "meridian",
        waitTimeoutMs: 30_000,
      }),
    ).toEqual({
      keep: "value",
      message: "wake text",
      sessionKey: "agent:meridian:paperclip:issue:issue-456",
      idempotencyKey: "run-123",
      agentId: "meridian",
      timeout: 30_000,
    });
  });

  it("preserves an explicit agentId and timeout from the payload template", () => {
    expect(
      buildAgentParams({
        payloadTemplate: {
          agentId: "template-agent",
          timeout: 5_000,
        },
        message: "wake text",
        sessionKey: "paperclip",
        runId: "run-123",
        configuredAgentId: "configured-agent",
        waitTimeoutMs: 30_000,
      }),
    ).toEqual({
      agentId: "template-agent",
      timeout: 5_000,
      message: "wake text",
      sessionKey: "paperclip",
      idempotencyKey: "run-123",
    });
  });
});

describe("resolveClaimedApiKeyPath", () => {
  const DEFAULT_PATH = "~/.openclaw/workspace/paperclip-claimed-api-key.json";

  it("returns the configured per-agent path when set", () => {
    expect(
      resolveClaimedApiKeyPath("~/.openclaw/workspace/paperclip-keys/happy.json"),
    ).toBe("~/.openclaw/workspace/paperclip-keys/happy.json");
  });

  it("falls back to the shared default when value is empty", () => {
    expect(resolveClaimedApiKeyPath("")).toBe(DEFAULT_PATH);
    expect(resolveClaimedApiKeyPath("   ")).toBe(DEFAULT_PATH);
  });

  it("falls back to the shared default when value is missing", () => {
    expect(resolveClaimedApiKeyPath(undefined)).toBe(DEFAULT_PATH);
    expect(resolveClaimedApiKeyPath(null)).toBe(DEFAULT_PATH);
  });

  it("falls back to the shared default when value is not a string", () => {
    expect(resolveClaimedApiKeyPath(42)).toBe(DEFAULT_PATH);
    expect(resolveClaimedApiKeyPath({})).toBe(DEFAULT_PATH);
  });
});

describe("buildReplyDeliveryRequest", () => {
  const CONVERSATION_REF = `conv_${"a".repeat(32)}`;

  it("builds a per-run idempotent conversations.send request", () => {
    expect(
      buildReplyDeliveryRequest({
        runId: "0f0e0d0c-1111-2222-3333-444455556666",
        conversationRef: CONVERSATION_REF,
        agentId: "sonnia",
        message: "Answer for the founder.",
      }),
    ).toEqual({
      agentId: "sonnia",
      conversationRef: CONVERSATION_REF,
      operationId: "paperclip-run-0f0e0d0c-1111-2222-3333-444455556666",
      message: "Answer for the founder.",
    });
  });

  it("rejects a malformed conversation ref", () => {
    expect(
      buildReplyDeliveryRequest({
        runId: "run-1",
        conversationRef: "not-a-conversation-ref",
        agentId: "sonnia",
        message: "hello",
      }),
    ).toBeNull();
    expect(
      buildReplyDeliveryRequest({
        runId: "run-1",
        conversationRef: `conv_${"g".repeat(32)}`,
        agentId: "sonnia",
        message: "hello",
      }),
    ).toBeNull();
  });

  it("rejects a missing or malformed owning agent id", () => {
    expect(
      buildReplyDeliveryRequest({
        runId: "run-1",
        conversationRef: CONVERSATION_REF,
        agentId: null,
        message: "hello",
      }),
    ).toBeNull();
    expect(
      buildReplyDeliveryRequest({
        runId: "run-1",
        conversationRef: CONVERSATION_REF,
        agentId: "Bad Agent Slug",
        message: "hello",
      }),
    ).toBeNull();
  });

  it("rejects an empty reply so the run fails visibly instead of silently", () => {
    expect(
      buildReplyDeliveryRequest({
        runId: "run-1",
        conversationRef: CONVERSATION_REF,
        agentId: "sonnia",
        message: "   ",
      }),
    ).toBeNull();
    expect(
      buildReplyDeliveryRequest({
        runId: "run-1",
        conversationRef: CONVERSATION_REF,
        agentId: "sonnia",
        message: null,
      }),
    ).toBeNull();
  });
});

describe("classifyReplyDelivery", () => {
  it("treats sent and queued as delivered", () => {
    expect(classifyReplyDelivery({ status: "sent", channel: "telegram", messageId: "m1" })).toEqual({
      delivered: true,
      gatewayStatus: "sent",
      channel: "telegram",
      messageId: "m1",
      queueId: null,
    });
    expect(classifyReplyDelivery({ status: "queued", queueId: "q1" }).delivered).toBe(true);
  });

  it("treats suppressed and unknown as refusals", () => {
    expect(classifyReplyDelivery({ status: "suppressed" }).delivered).toBe(false);
    expect(classifyReplyDelivery({ status: "unknown" }).delivered).toBe(false);
    expect(classifyReplyDelivery(null).delivered).toBe(false);
    expect(classifyReplyDelivery(undefined).gatewayStatus).toBe("unknown");
    expect(classifyReplyDelivery({}).gatewayStatus).toBe("unknown");
  });
});
