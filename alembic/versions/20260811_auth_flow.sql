-- Email-first account access for manager.sonnia.ai. Existing users are marked
-- verified so the migration does not strand already-created accounts.

ALTER TABLE organizations
    ADD COLUMN account_type varchar(10) NOT NULL DEFAULT 'business',
    ADD COLUMN country text,
    ADD CONSTRAINT organizations_account_type_check
        CHECK (account_type IN ('individual', 'business'));

ALTER TABLE app_users
    ADD COLUMN email_verified_at timestamptz DEFAULT now(),
    ADD COLUMN email_verification_token_digest char(64),
    ADD COLUMN email_verification_expires_at timestamptz,
    ADD COLUMN password_reset_token_digest char(64),
    ADD COLUMN password_reset_expires_at timestamptz;

ALTER TABLE app_users ALTER COLUMN email_verified_at DROP DEFAULT;

-- Email-only sign-in is unambiguous across every organisation.
CREATE UNIQUE INDEX app_users_global_email_key ON app_users (lower(email));

-- Authentication throttling deliberately stores irreversible identifiers, not
-- raw email addresses or IP addresses. It is infrastructure state rather than
-- tenant product data and therefore lives in the private app schema.
CREATE TABLE app.auth_login_attempts (
    email_hash char(64) NOT NULL,
    ip_hash char(64) NOT NULL,
    failure_count integer NOT NULL DEFAULT 0,
    last_failed_at timestamptz NOT NULL DEFAULT now(),
    blocked_until timestamptz,
    PRIMARY KEY (email_hash, ip_hash)
);

-- A fixed, SECURITY DEFINER lookup replaces the old customer-entered slugs.
-- It returns only identifiers needed to establish RLS before reading a user.
CREATE FUNCTION app.resolve_auth_scope(requested_email text)
RETURNS TABLE (org_id uuid, department_id uuid, user_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT account.org_id, account.department_id, account.id
    FROM public.app_users AS account
    WHERE lower(account.email) = lower(btrim(requested_email));
$$;
REVOKE ALL ON FUNCTION app.resolve_auth_scope(text) FROM PUBLIC;

-- Password reset must revoke sessions across historical department scopes.
CREATE FUNCTION app.revoke_user_sessions(requested_user_id uuid, requested_org_id uuid)
RETURNS void
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    UPDATE public.user_sessions
    SET revoked_at = now()
    WHERE user_id = requested_user_id
      AND org_id = requested_org_id
      AND revoked_at IS NULL;
$$;
REVOKE ALL ON FUNCTION app.revoke_user_sessions(uuid, uuid) FROM PUBLIC;
