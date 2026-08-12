# SON-570 route availability verification — 2026-08-12

## Deployed source and immutable runtime

- Source revision: `df72a454ebf0e7226e9130518094bbec1ef1fc1f`.
- Source archive SHA-256: `3d0f6328ef2318bbea2bf158c0c35092ea70dde413ce6a2b1b920dbd8abaf922`.
- Canonical CodeBuild run: `manager-sonnia-api-arm64-build:776af4d4-eb78-476b-b0f0-56e864a908df`, succeeded at `2026-08-12T18:49:13Z`.
- Immutable image: `public.ecr.aws/q4i7u0q2/manager-sonnia-api@sha256:bc93e0a6adf0576cc181577a9a44438fb09b3a28c30a4f5137dc17aa03b02df8` (registry tag `son551-df72a454ebf0-20260812T184506Z-arm64`).
- Live deployment `sonnia/manager-sonnia-api` observed generation 26 with 2/2 updated, ready, and available replicas. Both live pods reported that same immutable image digest.
- The migration job `son551-db-migrate-1856` succeeded before the rollout; the release receipt records migration head `20260812_registration_v2`.

The deployed source includes `GET /api/dashboard/overview` in `app/main.py` and registers `/api/imports` and `/api/campaigns` from `app/phase1_contracts.py` using the app's authenticated context dependency. The frontend calls the same paths and consumes dashboard, paginated imports, and paginated campaigns payloads.

## Fresh production route-reachability probes

At both `https://manager.sonnia.ai` and direct API origin `https://6u9vphhpk9.execute-api.ap-southeast-1.amazonaws.com`, without a session:

| Path | Result |
| --- | --- |
| `GET /api/dashboard/overview?period=today` | `401 {"detail":"Not authenticated"}` |
| `GET /api/imports` | `401 {"detail":"Not authenticated"}` |
| `GET /api/campaigns` | `401 {"detail":"Not authenticated"}` |
| `GET /healthz` | `200 {"status":"ok"}` |

This confirms the three paths are present in the deployed application and fail at authentication rather than route matching. It supersedes the earlier missing-route `404` observation.

## Local contract and isolation verification

Fresh commands against the source revision completed successfully:

```text
PYTHONPATH=. .venv/bin/pytest -q tests/test_dashboard_route.py tests/test_son419_routes.py tests/test_isolation_contract.py
19 passed, 1 warning in 0.35s

PYTHON=.venv/bin/python PYTHONPATH=. npm run test:e2e
PASS: FastAPI auth flow and cross-org 404 completed over PostgreSQL wire protocol.

PYTHON=.venv/bin/python PYTHONPATH=. npm run test:e2e-son419
PASS: SON-419 routes verified end to end over PostgreSQL wire protocol.

PYTHONPATH=. .venv/bin/ruff check app tests
All checks passed!

.venv/bin/python -m build
Successfully built manager_sonnia_api-0.1.0.tar.gz and manager_sonnia_api-0.1.0-py3-none-any.whl
```

The end-to-end flow creates a disposable authenticated tenant A import and campaign, then switches to tenant B. Tenant B receives an empty imports collection and `404` for tenant A's import contacts and campaign. This uses the real FastAPI app over its PostgreSQL wire path; the authenticated context configures the transaction's organisation and department scope, and the route deliberately maps RLS-hidden records to `404`.

## Browser observation

The available browser profile had no approved session. Opening `/dashboard` displayed the session check and redirected to the sign-in form; no route-missing page was encountered. It is evidence of normal signed-out behavior only, not proof that the authenticated dashboard, imports, or campaigns screens render. No credentials or customer data were used.

## Rollback receipt

The prior immutable image retained in the registry is `public.ecr.aws/q4i7u0q2/manager-sonnia-api@sha256:179bcedbea3120e8d8aa817eafbce227a1f98ae3c64c113330c140aaf4641ae0` (tag `son447-c7bae42-calls-contacts-20260812T134138Z-arm64`). It is the recorded pre-release rollback candidate. A rollback would restore that digest through the existing `sonnia/manager-sonnia-api` rollout mechanism, wait for both replicas, then repeat `/healthz` and signed-out boundary probes.

## Remaining limitation

No approved disposable live tenant session was available in the browser profile, so fresh production authorized payloads and browser screens could not be exercised without inventing credentials. Local authenticated end-to-end coverage proves the route payload and cross-tenant behavior; SON-475 should independently repeat the authorized live check when an approved disposable session is available.
