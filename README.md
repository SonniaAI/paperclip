# manager.sonnia.ai — Phase 1 backend

This repository starts the Phase 1 backend at the non-negotiable tenancy gate,
then layers the §15 product model and Telnyx ingestion on top of it.

## What is enforced

- PostgreSQL Row-Level Security (RLS) is enabled **and forced** on every
  application table. Policies use transaction-local `app.org_id` and
  `app.department_id` settings; a route never appends an untrusted tenant
  filter to make access safe.
- Every application table has `org_id` and `department_id`. `organizations`
  uses its own ID as `org_id`; a new organization receives a silent default
  department atomically.
- Department shapes are `silent`, `shared`, and `private`. Visibility is
  granted by membership department, including for Owners.
- Do-not-contact rows and active-dial keys are organization-wide. The active
  dial policy defaults to `warn` and can later be changed to `block` or
  `allow`.
- Sessions are signed, `HttpOnly`, `Secure`, and `SameSite=Lax`; TOTP uses a
  standards-compatible HMAC-SHA1 30-second implementation; invite URLs are
  signed and expiry-limited.
- Customer-facing auth inputs use organization/department slugs, not database
  identifiers. The one fixed-query pre-auth resolver returns scope only to the
  server, then normal RLS starts immediately.
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

`npm run test:rls` creates both organization rows and then switches to a
non-owner database role. It proves that, with Org A's transaction context, a
lookup of Org B's call returns zero rows. The FastAPI call route translates
that absence into a 404 without querying by organization in application code.

`npm run test:e2e` starts an ephemeral PostgreSQL-WASM wire server and verifies
registration, the `Secure`/`HttpOnly` session cookie, TOTP enrollment,
tokenized invite acceptance, login, a real Telnyx transcript-memory write,
idempotent duplicate handling, deterministic-first voice recall with a labeled
Hindsight fallback, and Org A receiving a 404 for Org B's call.

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
`manager_auth_resolver` function owner with `BYPASSRLS`, constrained to a
fixed-query, two-column pre-auth slug resolver; it has no login, no membership
in the runtime role, and no access to user, call, invite, or session tables.

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
