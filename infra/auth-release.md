# manager.sonnia.ai authentication release

This is the bounded release envelope for the SON-503 frontend and backend. It
does not change dashboard behaviour or the older `starlight.sonnia.ai`
deployment.

## Verified targets

- CloudFront distribution: `E2WZLJF0RBN066` (`manager.sonnia.ai`).
- SPA bucket: `manager-sonnia-phase1-spabucket-tmafq2lcddjf`.
- API origin: `6u9vphhpk9.execute-api.ap-southeast-1.amazonaws.com`.
- Cluster namespace/deployment: `sonnia/manager-sonnia-api`.
- CloudFront already forwards `/auth/*` to `manager-api` with caching disabled;
  browser routes are rewritten to `index.html` at the SPA origin.

## Release prerequisites

1. An independent reviewer approves the exact frontend and backend revisions.
2. The migration role confirms there are no duplicate case-insensitive emails:

   ```sql
   SELECT lower(email), count(*)
   FROM app_users
   GROUP BY lower(email)
   HAVING count(*) > 1;
   ```

3. The production runtime secret supplies `MANAGER_DATABASE_URL`, a unique
   `MANAGER_SESSION_SECRET`, and the SES SMTP username/password. The existing
   AWS account has SES production access in `us-east-1` and a verified
   `sonnia.ai` identity. Use this configuration, with every placeholder
   resolved only through the existing cluster secret references:

   ```text
   MANAGER_ENVIRONMENT=production
   MANAGER_DATABASE_URL=<manager_app runtime database URL secret reference>
   MANAGER_SESSION_SECRET=<unique signing secret reference>
   MANAGER_PUBLIC_APP_URL=https://manager.sonnia.ai
   MANAGER_SECURE_COOKIES=true
   MANAGER_SMTP_HOST=email-smtp.us-east-1.amazonaws.com
   MANAGER_SMTP_PORT=587
   MANAGER_SMTP_USERNAME=<SES SMTP username secret reference>
   MANAGER_SMTP_PASSWORD=<SES SMTP password secret reference>
   MANAGER_SMTP_FROM_EMAIL=sonnia@sonnia.ai
   MANAGER_SMTP_USE_TLS=true
   ```

   Store credential values only in the cluster secret, never in source control
   or a command log. Production validation rejects a missing credential, an
   insecure public URL/cookie, or SMTP without TLS before the app starts.
4. Capture the current deployment JSON, image digest, replica count, and secret
   references for rollback. Do not change resources in `kube-system`.

## Backend order

1. Run the checked-in verification commands:

   ```bash
   pytest
   npm run test:e2e
   npm run test:e2e-son419
   ruff check app tests
   ```

2. Build and publish the backend image for the cluster architecture under an
   immutable digest.
3. Run `alembic upgrade head` once with the privileged migration role. The new
   `20260811_auth_claims` revision adds only the expiring 2FA challenge state.
4. Run `infra/runtime-role.sql` as the database administrator after the
   migration so the new tables/columns and fixed auth functions have the
   intended grants. Configure HTTP traffic to use only the `NOBYPASSRLS`
   `manager_app` runtime role; never reuse the migration or function-owner role.
5. Apply the production settings above through the existing deployment and
   secret references.
6. Roll out the immutable image to `sonnia/manager-sonnia-api`, wait for both
   replicas to become ready, and confirm `/healthz` returns 200. Confirm
   `/auth/me` now returns an authentication response (401 without a cookie),
   not the pre-release 404.

The schema changes are additive. If runtime rollback is needed, restore the
captured image and deployment configuration; retain the added nullable columns.

## Frontend order

From the approved frontend revision:

```bash
npm ci
npm test
npm run lint:auth-copy
npm run build
aws s3 sync dist/ s3://manager-sonnia-phase1-spabucket-tmafq2lcddjf/ \
  --exclude index.html \
  --cache-control 'public,max-age=31536000,immutable'
aws s3 cp dist/index.html \
  s3://manager-sonnia-phase1-spabucket-tmafq2lcddjf/index.html \
  --content-type text/html \
  --cache-control 'no-store,max-age=0,must-revalidate'
aws cloudfront create-invalidation --distribution-id E2WZLJF0RBN066 --paths '/*'
```

The frontend uses hashed asset names, so the HTML upload is last. Record the
S3 object ETags and CloudFront invalidation ID.

## Browser/API and cookie boundary

The browser uses the single `https://manager.sonnia.ai` origin. CloudFront
serves SPA/browser routes from the private S3 origin and forwards `/auth/*`
unchanged to the `manager-api` origin with caching disabled and viewer cookies
forwarded. The backend sets a host-only `manager_session` cookie with
`Path=/`, `Secure`, `HttpOnly`, and `SameSite=Lax`; frontend JavaScript never
reads or stores the session token. Inspect the verification, login, and reset
responses in the live browser and reject the release if any of those flags is
absent.

## Live acceptance and rollback trigger

Run the eight customer steps in order: company registration, check-email,
single-use verification with automatic sign-in, sign out, email/password sign
in (with no unconditional 2FA field), dashboard, forgot/reset with automatic
sign-in, the `sonnia.ai` back link, and a second `Just me` account. Confirm the
message arrives through live email and verify that replaying each verification,
2FA, and reset token fails.

Rollback the frontend HTML and backend image if any auth route serves the
dashboard shell, `/auth/*` returns 404, live email does not arrive, a token can
be reused, or a dead link appears. Keep the issue open until the live flow and
email receipt are attached.
