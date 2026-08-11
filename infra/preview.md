# Managed preview envelope

## Deployment target

Phase 1 uses an AWS Lambda Function URL for the preview. It is a managed,
HTTPS-only service with a small idle footprint, while the included Dockerfile
keeps the same FastAPI app portable to a future long-running runtime service.

The public preview exposes only `/healthz`; customer routes continue to require
the signed session and RLS transaction context. A production PostgreSQL service
is intentionally a separate approval because RLS cannot be safely approximated
with a local or SQLite substitute.

## Private media pattern

- The storage bucket has Block Public Access enabled, Bucket Owner Enforced
  object ownership, and default SSE-S3 encryption.
- The Lambda role receives `s3:GetObject` and `s3:PutObject` only below
  `recordings/*` and `transcripts/*`; it cannot make an object public or
  delete one.
- The application copies a provider recording into that bucket, records its
  private key in the RLS-protected database, and issues short-lived URLs only
  after the caller passes that row's RLS lookup.
- Signatures expire in five minutes by default (configurable only from 1 to
  900 seconds). Bucket names and object keys are never customer API data.

Configure the preview with:

```text
MANAGER_ENVIRONMENT=production
MANAGER_SESSION_SECRET=<random value stored outside source control>
MANAGER_PUBLIC_APP_URL=https://manager.sonnia.ai
MANAGER_STORAGE_BUCKET=<private bucket name>
MANAGER_STORAGE_REGION=us-east-1
MANAGER_RECORDING_SIGNED_URL_TTL_SECONDS=300
MANAGER_SMTP_HOST=<transactional SMTP host>
MANAGER_SMTP_PORT=587
MANAGER_SMTP_USERNAME=<SMTP username>
MANAGER_SMTP_PASSWORD=<secret value stored outside source control>
MANAGER_SMTP_FROM_EMAIL=<verified sender address>
MANAGER_SMTP_USE_TLS=true
```

When SMTP is absent in development, verification and reset URLs are logged and
returned only for the explicit development banner. Production configuration
validation requires both `MANAGER_SMTP_HOST` and `MANAGER_SMTP_FROM_EMAIL`, so
the service exits before accepting traffic if live email is not configured.

## CI and billing affordance

GitHub Actions runs Ruff, Python tests, a wheel/sdist build, the real PGLite
RLS proof, the FastAPI/PGLite auth-flow proof, and a Docker build. Phase 1
uses authenticated `GET /api/billing/top-up` for the deliberately
non-functional `Add credit → contact us` affordance; no payment-provider
integration is configured.
