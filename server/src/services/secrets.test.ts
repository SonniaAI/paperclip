import { describe, expect, it } from "vitest";
import { FALLBACK_ADAPTER_SCHEMA_SECRET_FIELDS } from "./secrets.js";

// The openclaw_gateway adapter declares no config-schema secret fields of its
// own. When this fallback entry is missing (for example after a full image
// rebuild), dispatch resolves none of its binding fields as secrets and sends
// the raw secret_ref binding objects to the gateway, which fails every
// dispatch with an authentication error. This test pins the entry so the
// behavior cannot silently regress again.
describe("FALLBACK_ADAPTER_SCHEMA_SECRET_FIELDS", () => {
  it("covers the secret-bearing openclaw_gateway binding fields", () => {
    expect(FALLBACK_ADAPTER_SCHEMA_SECRET_FIELDS.openclaw_gateway).toEqual([
      "authToken",
      "password",
      "devicePrivateKeyPem",
    ]);
  });
});
