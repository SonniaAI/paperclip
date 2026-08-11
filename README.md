# manager.sonnia.ai — Phase 1 backend

This repository starts the Phase 1 backend at the non-negotiable tenancy gate,
then layers the §15 product model and Telnyx ingestion on top of it.

## What is enforced

- PostgreSQL Row-Level Security (RLS) is enabled **and forced** on every
  application table. Policies use transaction-local `app.org_id` and
  `app.department_id` settings; a route never appends an untrusted tenant
  filter to make access safe.
- Every application table has `org_id` and `department_id`. `organizations`
  uses its own ID as `org_id`; a new organisation receives a silent default
  department atomically.
- Department shapes are `silent`, `shared`, and `private`. Visibility is
  granted by membership department, including for Owners.
- Do-not-contact rows and active-dial keys are organisation-wide. The active
  dial policy defaults to `warn` and can later be changed to `block` or
  `allow`.
- Sessions are signed, `HttpOnly`, `Secure`, and `SameSite=Lax`; TOTP uses a
  standards-compatible HMAC-SHA1 30-second implementation; invite URLs are
  signed and expiry-limited.
- Customer-facing sign-in accepts email and password only. Email is globally
  unique; a fixed-query pre-auth resolver returns only the user's primary scope
  and ID, then normal RLS starts immediately. Organisation and department slugs
  remain internal routing data and are never customer-entered credentials.
- Registration creates an individual or business organisation, silent default
  department, Owner membership, and 24-hour single-use verification token in
  one transaction. Password-reset tokens last 1 hour, reset revokes all prior
  sessions, and login locks an email/IP hash pair for 15 minutes after five
  consecutive failures. TOTP is requested only after a correct password for an
  account that has it enabled.
- `20260809_feature_data` adds the complete §15 feature model: companies,
  contacts and contact methods, recordings, transcripts/segments, call
  intelligence, tasks, campaigns/materials/claims, spend/balance, test
  fixtures, instructions, activity, integrations, and security events. The
  existing Gate 0 `organizations` and `app_users` tables are the canonical
  organization/user entities.
- `telnyx_webhook_events` is a durable inbox. It stores canonical raw JSON and
  its SHA-256 before normalization, deduplicates globally on provider event ID,
  and exposes `replay_telnyx_event` for failed/transient processing.
- Telnyx calls upsert on the provider call key. Recordings are copied through
  the private object-storage adapter; the database has no public URL column and
  `GET /api/recordings/{id}/url` issues only a configured signed URL with a
  maximum 15-minute lifetime.
- `app.crm.CRMAdapter` keeps the canonical model independent of any connector;
  no provider-specific CRM is installed.
- Contact facts and preferences are written to tenant-scoped PostgreSQL rows
  first.  A durable outbox then sends a redacted, idempotent transcript/fact
  copy to the optional Hindsight service.  Processed and duplicate Telnyx
  transcript events invoke that post-commit dispatch path, using explicit SDK
  replacement mode on retries.  The authenticated voice-memory route uses
  deterministic rows first and receives explicitly labeled Hindsight results
  only as a fuzzy fallback; fuzzy results never overwrite the contact book.
- `POST /api/materials/extract` accepts an authenticated pitch material upload
  and returns a ranked, source-linked cold-call brief through the deterministic
  offline fallback. See [`docs/materials-strategy.md`](docs/materials-strategy.md)
  and [`evidence/materials-strategy-sample.json`](evidence/materials-strategy-sample.json).

## Required verification

```bash
# Create a virtual environment and install this project's Python dependencies.
# Then run the non-database contract tests:
pytest

# Run the real PostgreSQL-WASM RLS proof against the exact Alembic SQL:
npm install
npm run test:rls

# Verify the full §15 feature schema, forced RLS, raw-event dedupe, and
# private-recording constraint:
npm run test:feature-data

# Run the async SQLAlchemy/PGlite replay proof, including private recordings:
npm run test:telnyx-ingestion

# Run the FastAPI + async SQLAlchemy auth, Telnyx transcript-memory, and
# deterministic-first voice recall flow against PostgreSQL wire protocol:
npm run test:e2e
```

`npm run test:rls` creates both organisation rows and then switches to a
non-owner database role. It proves that, with Org A's transaction context, a
lookup of Org B's call returns zero rows. The FastAPI call route translates
that absence into a 404 without querying by organisation in application code.

`npm run test:e2e` starts an ephemeral PostgreSQL-WASM wire server and verifies
business and individual registration, single-use email verification, the
`Secure`/`HttpOnly` session cookie, conditional TOTP, invite acceptance,
email-only login, password reset with session invalidation, five-attempt rate
limiting, a real Telnyx transcript-memory write, idempotent duplicate handling,
deterministic-first voice recall with a labelled Hindsight fallback, and Org A
receiving a 404 for Org B's call.

`npm run test:feature-data` applies Gate 0 plus the feature migration to
PostgreSQL-WASM and verifies all 30 new tables carry both tenant keys, forced
RLS, globally idempotent raw-event inserts, and the database-level private
recording constraint.

`npm run test:telnyx-ingestion` drives the real ingestion service through the
PostgreSQL wire adapter and verifies one durable raw event, one call after a
duplicate and a replay, one private recording object key, and a 300-second
signed URL.

For a deployed PostgreSQL service, run the Alembic migrations with
`DATABASE_URL=postgresql+psycopg://... alembic upgrade head`. Runtime traffic
uses `postgresql+asyncpg://...`. Then run
`infra/runtime-role.sql` as a database administrator and configure the app to
connect as the non-superuser, `NOBYPASSRLS` `manager_app` role. The migration
role must not be used for HTTP traffic. The script also creates a `NOLOGIN`
`manager_auth_resolver` function owner with `BYPASSRLS`, constrained to fixed
email-to-scope lookup and all-session-revocation functions. It has no login or
membership in the runtime role and cannot run arbitrary statements.

Transactional email uses standard SMTP when `MANAGER_SMTP_HOST` and
`MANAGER_SMTP_FROM_EMAIL` are configured. Without SMTP, development logs the
verification/reset URL and the frontend shows an explicitly development-only
banner. Production refuses to start without both SMTP settings, so it cannot
silently expose an account flow whose verification email will never arrive.
Development fallback URLs are never exposed in a production browser response.

## Layout

- `alembic/versions/20260809_gate0_rls.sql` — Gate 0 schema and RLS policy
  statements.
- `alembic/versions/20260809_feature_data.sql` — §15 schema, inbox, indexes,
  and forced RLS policies.
- `alembic/versions/20260809_son419.sql` — SON-419 layer: versioned
  instructions (slug/version/effective_from/superseded_at + partial unique
  index), personal-or-team task lists, tenant-scoped forced-RLS contact
  imports, a privacy flag on activity entries, and organisation onboarding
  state.
- `alembic/versions/20260810_hindsight_memory.sql` — tenant-scoped
  deterministic contact-memory batches/entries and retryable Hindsight outbox.
- `alembic/versions/20260811_auth_flow.sql` — email-first identity lookup,
  verification/reset state, global email uniqueness, and hashed login-rate
  limits.
- `alembic/versions/20260811_auth_claims.sql` — persisted, expiring 2FA
  challenge digests for atomic single-use completion under concurrent replay.
- `app/` — FastAPI, async SQLAlchemy, ingestion, private storage, CRM seam,
  sessions, TOTP, invites, and the SON-419 service layer (`app/son419.py`)
  and routes (`/api/instructions`, `/api/tasks`, `/api/task-lists`,
  `/api/activity`, `/api/settings/*`, `/api/contacts/import*`, `/api/onboarding`).
- `app/contact_memory.py` — deterministic-first contact-memory write, outbox
  dispatch, redaction, and voice-agent recall seam; `app/transcript_memory.py`
  is the post-commit Telnyx transcript dispatcher; `app/hindsight.py` wraps the
  optional maintained Hindsight SDK.
- `tests/rls_isolation.mjs`, `tests/feature_data.mjs`,
  `tests/telnyx_ingestion.mjs` — database-level and wire proofs.
- `tests/son419_migration.mjs` — full-chain migration proof for the SON-419
  layer (versioning cycle, forced-RLS imports, privacy flag, onboarding state).
- `tests/` — security and route-isolation contracts.
