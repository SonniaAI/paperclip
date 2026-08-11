-- Run once as a database administrator after `alembic upgrade head`.
-- The FastAPI runtime must never use a superuser, table owner with BYPASSRLS,
-- or a role that can change RLS policies. The one narrowly-scoped exception is
-- the NOLOGIN function owner below: it can resolve a login identity and revoke
-- that identity's sessions only through fixed-query functions.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'manager_app') THEN
        CREATE ROLE manager_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'manager_auth_resolver') THEN
        CREATE ROLE manager_auth_resolver NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE manager_sonnia TO manager_app;
GRANT USAGE ON SCHEMA public, app TO manager_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO manager_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE app.auth_login_attempts TO manager_app;
GRANT EXECUTE ON FUNCTION app.resolve_login_scope(text, text) TO manager_app;
GRANT EXECUTE ON FUNCTION app.resolve_auth_scope(text) TO manager_app;
GRANT EXECUTE ON FUNCTION app.revoke_user_sessions(uuid, uuid) TO manager_app;

GRANT USAGE ON SCHEMA public, app TO manager_auth_resolver;
GRANT SELECT ON TABLE organizations, departments, app_users, user_sessions
    TO manager_auth_resolver;
GRANT UPDATE ON TABLE user_sessions TO manager_auth_resolver;
ALTER FUNCTION app.resolve_login_scope(text, text) OWNER TO manager_auth_resolver;
ALTER FUNCTION app.resolve_auth_scope(text) OWNER TO manager_auth_resolver;
ALTER FUNCTION app.revoke_user_sessions(uuid, uuid) OWNER TO manager_auth_resolver;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO manager_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA app
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO manager_app;
