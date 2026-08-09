import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { PGlite } from '@electric-sql/pglite'

const here = dirname(fileURLToPath(import.meta.url))
const schemaPath = join(here, '..', 'alembic', 'versions', '20260809_gate0_rls.sql')
const schema = await readFile(schemaPath, 'utf8')

const ids = {
  orgA: '00000000-0000-0000-0000-0000000000a1',
  depA: '00000000-0000-0000-0000-0000000000a2',
  callA: '00000000-0000-0000-0000-0000000000a3',
  orgB: '00000000-0000-0000-0000-0000000000b1',
  depB: '00000000-0000-0000-0000-0000000000b2',
  callB: '00000000-0000-0000-0000-0000000000b3',
}

const db = new PGlite()

try {
  await db.exec(schema)

  for (const [org, dep, call, suffix] of [
    [ids.orgA, ids.depA, ids.callA, 'a'],
    [ids.orgB, ids.depB, ids.callB, 'b'],
  ]) {
    await db.exec(`
      BEGIN;
      SELECT set_config('app.org_id', '${org}', true);
      SELECT set_config('app.department_id', '${dep}', true);
      SET CONSTRAINTS ALL DEFERRED;
      INSERT INTO organizations (id, org_id, department_id, name, login_slug)
        VALUES ('${org}', '${org}', '${dep}', 'Org ${suffix.toUpperCase()}', 'org-${suffix}');
      INSERT INTO departments (id, org_id, department_id, name, login_slug, shape, is_silent)
        VALUES ('${dep}', '${org}', '${dep}', '__default__', 'default', 'silent', true);
      INSERT INTO calls (id, org_id, department_id, external_call_key, subject)
        VALUES ('${call}', '${org}', '${dep}', 'call-${suffix}', 'Call ${suffix.toUpperCase()}');
      COMMIT;
    `)
  }

  const policies = await db.query(
    "SELECT tablename FROM pg_policies WHERE schemaname = 'public' ORDER BY tablename",
  )
  assert.deepEqual(
    policies.rows.map((row) => row.tablename),
    [
      'active_dials',
      'app_users',
      'calls',
      'departments',
      'do_not_contacts',
      'invites',
      'membership_departments',
      'memberships',
      'organizations',
      'user_sessions',
    ],
  )

  const rlsFlags = await db.query(`
    SELECT relrowsecurity, relforcerowsecurity
    FROM pg_class
    WHERE oid = 'public.calls'::regclass
  `)
  assert.deepEqual(rlsFlags.rows, [{ relrowsecurity: true, relforcerowsecurity: true }])

  await db.exec(`
    CREATE ROLE manager_auth_resolver NOLOGIN BYPASSRLS;
    GRANT USAGE ON SCHEMA public, app TO manager_auth_resolver;
    GRANT SELECT ON organizations, departments TO manager_auth_resolver;
    ALTER FUNCTION app.resolve_login_scope(text, text) OWNER TO manager_auth_resolver;
    CREATE ROLE manager_app NOLOGIN;
    GRANT USAGE ON SCHEMA app TO manager_app;
    GRANT SELECT ON calls TO manager_app;
    GRANT EXECUTE ON FUNCTION app.resolve_login_scope(text, text) TO manager_app;
    SET ROLE manager_app;
    SELECT set_config('app.org_id', '${ids.orgA}', false);
    SELECT set_config('app.department_id', '${ids.depA}', false);
  `)

  const loginScope = await db.query(
    "SELECT * FROM app.resolve_login_scope('org-a', 'default')",
  )
  assert.deepEqual(loginScope.rows, [{ org_id: ids.orgA, department_id: ids.depA }])

  const ownCall = await db.query(`SELECT id FROM calls WHERE id = '${ids.callA}'`)
  const foreignCall = await db.query(`SELECT id FROM calls WHERE id = '${ids.callB}'`)

  assert.equal(ownCall.rows.length, 1, 'Org A must see its own call')
  assert.equal(foreignCall.rows.length, 0, 'Org A must receive no row for Org B call ID')
  console.log('PASS: PostgreSQL RLS hid Org B call from an Org A database role; API maps zero rows to 404.')
} finally {
  await db.close()
}
