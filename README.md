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

# Run the FastAPI + async SQLAlchemy auth flow against PostgreSQL wire protocol:
npm run test:e2e
```

`npm run test:rls` creates both organization rows and then switches to a
non-owner database role. It proves that, with Org A's transaction context, a
lookup of Org B's call returns zero rows. The FastAPI call route translates
that absence into a 404 without querying by organization in application code.

`npm run test:e2e` starts an ephemeral PostgreSQL-WASM wire server and verifies
registration, the `Secure`/`HttpOnly` session cookie, TOTP enrollment,
tokenized invite acceptance, login, and Org A receiving a 404 for Org B's call.

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
- `app/` — FastAPI, async SQLAlchemy, ingestion, private storage, CRM seam,
  sessions, TOTP, and invites.
- `tests/rls_isolation.mjs`, `tests/feature_data.mjs`,
  `tests/telnyx_ingestion.mjs` — database-level and wire proofs.
- `tests/` — security and route-isolation contracts.
