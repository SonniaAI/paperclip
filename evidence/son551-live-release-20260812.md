# SON-551 controlled live release evidence — 2026-08-12

## Release identity

- Source revision: `df72a454ebf0e7226e9130518094bbec1ef1fc1f`
- Source archive SHA-256: `3d0f6328ef2318bbea2bf158c0c35092ea70dde413ce6a2b1b920dbd8abaf922`
- Immutable runtime image: `public.ecr.aws/q4i7u0q2/manager-sonnia-api@sha256:bc93e0a6adf0576cc181577a9a44438fb09b3a28c30a4f5137dc17aa03b02df8`
- Canonical CodeBuild run: `manager-sonnia-api-arm64-build:776af4d4-eb78-476b-b0f0-56e864a908df` — succeeded at 2026-08-12T18:49:13Z.
- Database migration Job: `son551-db-migrate-1856` — succeeded. It advanced `20260811_phase1_campaign_imports` through `20260812_son551_customer_memory`, `20260812_merge_son551_campaigns`, and `20260812_registration_v2 (head)`.
- Deployment rollout: 2/2 ready on the immutable image above (pods `manager-sonnia-api-76f9f75bd7-gxxv4` and `manager-sonnia-api-76f9f75bd7-r8vt7`). Both reported `/healthz` OK and exposed `POST /api/voice/contacts/{contact_id}/memory-recall` in their OpenAPI documents.

## Controlled synthetic two-call / isolation proof

All inputs used disposable synthetic orgs, synthetic owner sessions, and controlled caller statements; all rows were explicitly cleaned up at the end. Identifiers, cookies, secrets, and caller numbers are omitted.

- **Call A ingestion:** signed Telnyx-shaped `call.hangup` with caller facts “I prefer calls after 3pm” and “installation launch is in September” returned **202 Accepted**.
- **Call B follow-up:** “When should I ring them?” for the same synthetic caller returned **200**, no deterministic hit, `used_hindsight: true`, and `hindsight_status: unavailable`. This is the expected labelled optional-provider fallback because this release has no Hindsight base URL configured; no fabricated memory was returned.
- **CRM-first fallback:** “September launch” for the same caller returned **200**, one deterministic CRM result, `used_hindsight: false`, `hindsight_status: not_needed`.
- **Same-org other caller:** the same Call B query returned **200**, zero deterministic/fuzzy results, with Hindsight unavailable rather than any caller A material.
- **Cross-org caller A ID:** the same query under the second synthetic organization returned **404 Contact not found**.
- Cleanup completed successfully for both synthetic organizations and their dependent rows.

## Limitation / remaining concern

Hindsight is intentionally not configured in the current production workload (`MANAGER_HINDSIGHT_BASE_URL` and API key absent), so live fuzzy recall is safely unavailable rather than returning an AI-assisted result. Deterministic CRM memory is live and isolated; enabling Hindsight later requires its separate credential/configuration rollout and a repeat of the fuzzy-return branch proof, including the public label `AI-assisted recall; verify before use.`
