# manager.sonnia.ai — Gate 0

This repository starts the Phase 1 backend at the non-negotiable tenancy gate.
It is deliberately small: it provides the database isolation, authentication,
and call lookup needed before feature work can begin.

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

## Required verification

```bash
# Create a virtual environment and install this project's Python dependencies.
# Then run the non-database contract tests:
pytest

# Run the real PostgreSQL-WASM RLS proof against the exact Alembic SQL:
npm install
npm run test:rls

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

For a deployed PostgreSQL service, run the Alembic migration with
`DATABASE_URL=postgresql+psycopg://... alembic upgrade head`. Runtime traffic
uses `postgresql+asyncpg://...`. Then run
`infra/runtime-role.sql` as a database administrator and configure the app to
connect as the non-superuser, `NOBYPASSRLS` `manager_app` role. The migration
role must not be used for HTTP traffic. The script also creates a `NOLOGIN`
`manager_auth_resolver` function owner with `BYPASSRLS`, constrained to a
fixed-query, two-column pre-auth slug resolver; it has no login, no membership
in the runtime role, and no access to user, call, invite, or session tables.

## Layout

- `alembic/versions/20260809_gate0_rls.sql` — canonical schema and RLS policy
  statements.
- `app/` — FastAPI, SQLAlchemy 2.0 async models, sessions, TOTP, and invites.
- `tests/rls_isolation.mjs` — database-level RLS evidence.
- `tests/` — security and route-isolation contracts.

No product feature endpoint should be added until both isolation checks remain
green on the target PostgreSQL deployment.
