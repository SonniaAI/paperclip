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
   `MANAGER_SESSION_SECRET`, and SMTP credentials. The existing AWS account has
   SES production access in `us-east-1` and a verified `sonnia.ai` identity;
   the intended SMTP host is `email-smtp.us-east-1.amazonaws.com` and the
   verified sender is `sonnia@sonnia.ai`. Store the dedicated SMTP username and
   password in the cluster secret, never in source control or a command log.
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
3. Run `alembic upgrade head` once with the migration role. The new
   `20260811_auth_claims` revision adds only the expiring 2FA challenge state.
4. Set `MANAGER_ENVIRONMENT=production`,
   `MANAGER_PUBLIC_APP_URL=https://manager.sonnia.ai`, and the required SMTP
   variables through the existing deployment secret references.
5. Roll out the immutable image to `sonnia/manager-sonnia-api`, wait for both
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
