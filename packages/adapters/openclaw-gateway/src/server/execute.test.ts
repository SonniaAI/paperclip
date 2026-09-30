import { describe, expect, it } from "vitest";
import {
  buildAgentParams,
  fetchSessionUsageSummary,
  resolveClaimedApiKeyPath,
  resolveSessionKey,
  summarizeSessionUsage,
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

describe("summarizeSessionUsage", () => {
  it("maps gateway session-usage field variants onto one aggregate summary", () => {
    const summary = summarizeSessionUsage({
      sessions: [
        {
          key: "agent:meridian:paperclip:run:run-123",
          usage: { input: 100, output: 40, cacheRead: 10, totalCost: 0.5 },
        },
        {
          key: "agent:meridian:paperclip:run:run-123:older",
          usage: { inputTokens: 25, outputTokens: 5, cacheReadTokens: 5, costUsd: 0.25 },
        },
      ],
    });
    expect(summary).toEqual({
      usage: { inputTokens: 125, outputTokens: 45, cachedInputTokens: 15 },
      costUsd: 0.75,
    });
  });

  it("keeps an explicit cachedInputTokens alias and omits the field when zero", () => {
    const withAlias = summarizeSessionUsage({
      sessions: [{ key: "s1", usage: { cachedInputTokens: 4, input: 2, output: 1 } }],
    });
    expect(withAlias?.usage.cachedInputTokens).toBe(4);

    const withoutCache = summarizeSessionUsage({
      sessions: [{ key: "s2", usage: { inputTokens: 3, outputTokens: 2 } }],
    });
    expect(withoutCache?.usage).toEqual({ inputTokens: 3, outputTokens: 2 });
  });

  it("returns null when no session row carries tokens", () => {
    expect(
      summarizeSessionUsage({ sessions: [{ key: "s", usage: { input: 0, output: 0 } }] }),
    ).toBeNull();
    expect(summarizeSessionUsage({ sessions: [] })).toBeNull();
  });

  it("returns null for malformed payloads instead of throwing", () => {
    expect(summarizeSessionUsage(null)).toBeNull();
    expect(summarizeSessionUsage("nope")).toBeNull();
    expect(summarizeSessionUsage({ sessions: "x" })).toBeNull();
  });
});

describe("fetchSessionUsageSummary", () => {
  it("requests the run session usage report and summarizes it", async () => {
    const calls: Array<{ method: string; params: unknown }> = [];
    const client = {
      request: async (method: string, params: unknown) => {
        calls.push({ method, params });
        return {
          sessions: [
            { key: "agent:m:paperclip:run:r", usage: { input: 7, output: 3, cacheRead: 2, totalCost: 0.11 } },
          ],
        };
      },
    };
    const summary = await fetchSessionUsageSummary(client as never, "agent:m:paperclip:run:r", 5_000);
    expect(calls).toEqual([{ method: "sessions.usage", params: { key: "agent:m:paperclip:run:r", limit: 5 } }]);
    expect(summary?.usage).toEqual({ inputTokens: 7, outputTokens: 3, cachedInputTokens: 2 });
    expect(summary?.costUsd).toBeCloseTo(0.11);
  });

  it("returns null when the gateway reports no sessions", async () => {
    const client = {
      request: async () => ({ sessions: [] }),
    };
    expect(await fetchSessionUsageSummary(client as never, "k", 5_000)).toBeNull();
  });

  it("never fails the run when the usage request errors", async () => {
    const client = {
      request: async () => {
        throw new Error("gateway closed (1006)");
      },
    };
    expect(await fetchSessionUsageSummary(client as never, "k", 5_000)).toBeNull();
  });
});
