# Gate 0 RLS evidence

Run from `manager-sonnia-api` on 2026-08-09 UTC:

```text
$ python3 -m pytest -q
........                                                                 [100%]
8 passed, 1 warning in 0.60s

$ npm run test:rls
PASS: PostgreSQL RLS hid Org B call from an Org A database role; API maps zero rows to 404.

$ npm run test:e2e
E2E PASS: registration, secure session, TOTP, signed invite, login, Org A -> Org B 404, and idempotent Telnyx replay (1 event, 1 call)
PASS: FastAPI auth flow and cross-org 404 completed over PostgreSQL wire protocol.

$ .venv/bin/pytest -q
.................                                                        [100%]
17 passed, 1 warning in 0.59s

$ npm run test:feature-data
PASS: feature schema has 30 tenant tables; raw Telnyx dedupe and private recording constraints hold.

$ npm run test:telnyx-ingestion
PASS: raw event stored once; duplicate skipped; replay reused durable payload; one private recording key with a 300-second signed URL.
```

The feature migration is `alembic/versions/20260809_feature_data.sql` with
revision `20260809_feature_data`, immediately after `20260809_gate0`. It adds
companies, contacts/contact phones/contact emails, recordings, transcripts and
segments, summaries/promises/actions/follow-ups/notes, task lists/tasks,
campaigns/briefs/chats/materials/claims, spend ledger/org balance/campaign
reads, test personas/suites/sessions, instructions, activity log,
integrations, security events, and the `telnyx_webhook_events` inbox. Gate 0's
`organizations` and `app_users` remain the canonical organisation and user
tables.

## Telnyx ingestion proof

`app.ingestion.ingest_telnyx_event` commits the raw JSON inbox row first. The
second transaction normalizes call-shaped events and upserts `calls` by
`(org_id, external_call_key)`; recording media goes through the configured
private storage adapter. A failed parser or storage copy marks the durable raw
row `failed`, and `replay_telnyx_event` reuses that raw body. A repeated event
ID returns `duplicate` without applying the call a second time.

The signed recording route queries only the requested recording ID under the
active RLS transaction and returns a signer-generated URL. `recordings` has a
database `CHECK (is_private = true)` and no public URL field; settings reject
signed URL lifetimes above 900 seconds.

## Database-level proof

`tests/rls_isolation.mjs` loads
`alembic/versions/20260809_gate0_rls.sql` directly into a PostgreSQL-WASM
runtime, so the proof runs the same SQL as Alembic. It then:

1. Creates Org A and Org B, each with a silent default department and a call.
2. Verifies `pg_policies` lists a policy for all ten tenant tables.
3. Verifies `pg_class.relrowsecurity=true` and
   `pg_class.relforcerowsecurity=true` for `calls`.
4. Switches to the non-owner `manager_app` database role, grants it only call
   reads, and sets its transaction context to Org A / Department A.
5. Reads Org A's call successfully and receives zero rows for Org B's call.

The FastAPI route deliberately performs `SELECT ... WHERE calls.id = :id`
without an application tenant filter. Its HTTP contract maps the RLS-hidden
result to `404 {"detail":"Call not found"}`; this is independently asserted
in `tests/test_isolation_contract.py`.

## Auth-flow integration proof

`tests/e2e_auth_flow.mjs` starts an ephemeral PostgreSQL-WASM wire server;
`tests/e2e_auth_flow.py` drives the real FastAPI application through its async
SQLAlchemy/asyncpg database path. It verifies: owner registration and its
`Secure; HttpOnly; SameSite=Lax` cookie, TOTP enrollment/verification, Owner
invite issuance, tokenized invite acceptance, member login with a public
organization slug, and the cross-org call request returning 404.

The socket adapter multiplexes a single PostgreSQL-WASM connection, so the
test disables asyncpg prepared-statement caching and sets `manager_app` before
each RLS-scoped query. Native PostgreSQL deployments instead authenticate the
HTTP runtime directly as `manager_app` through `infra/runtime-role.sql`.

The only intentional RLS-bypass surface is the pre-auth,
`SECURITY DEFINER` `app.resolve_login_scope` function. Deployment gives its
`NOLOGIN manager_auth_resolver` owner `BYPASSRLS` and `SELECT` only on
`organizations` and `departments`; its fixed SQL returns scope to the server
from public slugs. The HTTP runtime remains the separate `NOBYPASSRLS`
`manager_app` role.

## Policy inventory

`organizations`, `departments`, `app_users`, `memberships`,
`membership_departments`, `invites`, `user_sessions`, `calls`, and
`active_dials` use the `tenant_isolation` policy:

```sql
org_id = app.current_org_id()
AND department_id = app.current_department_id()
```

`do_not_contacts` intentionally uses company-wide `organization_legal_isolation`
on `org_id` only so a department switch can never bypass a legal
do-not-contact record. Every application table still stores both identifiers.
