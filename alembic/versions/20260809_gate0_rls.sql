-- Gate 0 canonical PostgreSQL schema. This file is executed by the Alembic
-- revision and is also the exact input to the PostgreSQL-WASM RLS proof.

CREATE SCHEMA IF NOT EXISTS app;

CREATE FUNCTION app.current_org_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.org_id', true), '')::uuid;
$$;

CREATE FUNCTION app.current_department_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.department_id', true), '')::uuid;
$$;

CREATE TABLE organizations (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL UNIQUE,
    department_id uuid NOT NULL,
    name text NOT NULL,
    login_slug text NOT NULL UNIQUE,
    duplicate_call_protection varchar(8) NOT NULL DEFAULT 'warn',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (org_id = id),
    CHECK (duplicate_call_protection IN ('block', 'warn', 'allow'))
);

CREATE TABLE departments (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id) DEFERRABLE INITIALLY DEFERRED,
    department_id uuid NOT NULL,
    name text NOT NULL,
    login_slug text NOT NULL,
    shape varchar(10) NOT NULL,
    is_silent boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, id),
    UNIQUE (org_id, login_slug),
    CHECK (department_id = id),
    CHECK (shape IN ('silent', 'shared', 'private')),
    CHECK ((shape = 'silent') = is_silent)
);

ALTER TABLE organizations
    ADD CONSTRAINT organizations_default_department_fkey
    FOREIGN KEY (org_id, department_id)
    REFERENCES departments (org_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE app_users (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL,
    email text NOT NULL,
    display_name text NOT NULL,
    password_hash text NOT NULL,
    totp_secret text,
    totp_enabled boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, org_id),
    UNIQUE (org_id, email),
    FOREIGN KEY (org_id, department_id)
        REFERENCES departments (org_id, id)
);

CREATE TABLE memberships (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role varchar(10) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, org_id),
    UNIQUE (user_id, org_id),
    FOREIGN KEY (user_id, org_id) REFERENCES app_users (id, org_id),
    FOREIGN KEY (org_id, department_id)
        REFERENCES departments (org_id, id),
    CHECK (role IN ('owner', 'admin', 'member', 'viewer'))
);

CREATE TABLE membership_departments (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL,
    membership_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (membership_id, department_id),
    FOREIGN KEY (membership_id, org_id) REFERENCES memberships (id, org_id),
    FOREIGN KEY (org_id, department_id)
        REFERENCES departments (org_id, id)
);

CREATE TABLE invites (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL,
    email text NOT NULL,
    role varchar(10) NOT NULL,
    token_digest char(64) NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    accepted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (org_id, department_id)
        REFERENCES departments (org_id, id),
    CHECK (role IN ('owner', 'admin', 'member', 'viewer'))
);

CREATE TABLE user_sessions (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL,
    user_id uuid NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (user_id, org_id) REFERENCES app_users (id, org_id),
    FOREIGN KEY (org_id, department_id)
        REFERENCES departments (org_id, id)
);

CREATE TABLE calls (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL,
    external_call_key text NOT NULL,
    subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, external_call_key),
    FOREIGN KEY (org_id, department_id)
        REFERENCES departments (org_id, id)
);

-- The department ID records the source department, while policy is always
-- company-wide so legal do-not-contact enforcement cannot be bypassed by
-- switching departments.
CREATE TABLE do_not_contacts (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL,
    phone_e164 text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, phone_e164),
    FOREIGN KEY (org_id, department_id)
        REFERENCES departments (org_id, id)
);

-- The unique company-level index detects concurrent/duplicate dialing without
-- exposing another department's record. Product policy decides block/warn/allow.
CREATE TABLE active_dials (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL,
    phone_e164 text NOT NULL,
    call_id uuid,
    started_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (org_id, department_id)
        REFERENCES departments (org_id, id),
    FOREIGN KEY (call_id) REFERENCES calls (id),
    UNIQUE (org_id, phone_e164)
);

-- Authentication needs to resolve an explicitly public workspace handle before
-- an RLS context exists. This fixed-query function returns only the two UUIDs
-- to the server process; it exposes no tenant rows or user data and accepts no
-- dynamic SQL. Deployers grant EXECUTE only to the non-BYPASSRLS runtime role.
CREATE FUNCTION app.resolve_login_scope(
    requested_organization_slug text,
    requested_department_slug text DEFAULT 'default'
)
RETURNS TABLE (org_id uuid, department_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT organization.org_id, department.id
    FROM public.organizations AS organization
    JOIN public.departments AS department
      ON department.org_id = organization.id
    WHERE organization.login_slug = requested_organization_slug
      AND department.login_slug = requested_department_slug;
$$;
REVOKE ALL ON FUNCTION app.resolve_login_scope(text, text) FROM PUBLIC;

CREATE INDEX departments_tenant_idx ON departments (org_id, department_id);
CREATE INDEX users_tenant_idx ON app_users (org_id, department_id);
CREATE INDEX memberships_tenant_idx ON memberships (org_id, department_id);
CREATE INDEX membership_departments_tenant_idx ON membership_departments (org_id, department_id);
CREATE INDEX invites_tenant_idx ON invites (org_id, department_id);
CREATE INDEX user_sessions_tenant_idx ON user_sessions (org_id, department_id);
CREATE INDEX calls_tenant_idx ON calls (org_id, department_id);
CREATE INDEX active_dials_tenant_idx ON active_dials (org_id, department_id);

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE organizations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON organizations
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE departments ENABLE ROW LEVEL SECURITY;
ALTER TABLE departments FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON departments
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE app_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_users FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app_users
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON memberships
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE membership_departments ENABLE ROW LEVEL SECURITY;
ALTER TABLE membership_departments FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON membership_departments
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE invites FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON invites
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE user_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON user_sessions
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE calls FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON calls
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE do_not_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE do_not_contacts FORCE ROW LEVEL SECURITY;
CREATE POLICY organization_legal_isolation ON do_not_contacts
    USING (org_id = app.current_org_id())
    WITH CHECK (org_id = app.current_org_id());

ALTER TABLE active_dials ENABLE ROW LEVEL SECURITY;
ALTER TABLE active_dials FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON active_dials
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());
