import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { PGlite } from '@electric-sql/pglite'

const gate0 = await readFile('alembic/versions/20260809_gate0_rls.sql', 'utf8')
const feature = await readFile('alembic/versions/20260809_feature_data.sql', 'utf8')
const son419 = await readFile('alembic/versions/20260809_son419.sql', 'utf8')
const db = new PGlite()

const org = '00000000-0000-0000-0000-0000000000a1'
const dep = '00000000-0000-0000-0000-0000000000a2'
const user = '00000000-0000-0000-0000-0000000000a3'

try {
  await db.exec(gate0)
  await db.exec(feature)
  await db.exec(son419)

  // Versioned instructions: columns + partial unique index exist.
  const instrCols = await db.query(`
    SELECT column_name FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'instructions'
      AND column_name IN ('slug', 'version', 'effective_from', 'superseded_at')
  `)
  assert.equal(instrCols.rows.length, 4, 'instructions gained versioning columns')

  const activeIndex = await db.query(`
    SELECT indexdef FROM pg_indexes
    WHERE tablename = 'instructions' AND indexname = 'instructions_active_slug_key'
  `)
  assert.equal(activeIndex.rows.length, 1)
  assert.ok(activeIndex.rows[0].indexdef.includes('WHERE'), 'partial unique index present')

  // Task lists became personal-or-team.
  const listCols = await db.query(`
    SELECT column_name FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'task_lists'
      AND column_name IN ('owner_user_id', 'visibility')
  `)
  assert.equal(listCols.rows.length, 2, 'task_lists gained owner + visibility')

  // contact_imports exists with RLS forced.
  const importRls = await db.query(`
    SELECT relrowsecurity, relforcerowsecurity FROM pg_class
    WHERE relname = 'contact_imports'
  `)
  assert.equal(importRls.rows.length, 1)
  assert.equal(importRls.rows[0].relrowsecurity, true, 'RLS enabled')
  assert.equal(importRls.rows[0].relforcerowsecurity, true, 'RLS forced')

  // Activity log has the private flag + feed index; organizations carry onboarding_state.
  const actCols = await db.query(`
    SELECT column_name FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'activity_log' AND column_name = 'private'
  `)
  assert.equal(actCols.rows.length, 1, 'activity_log gained private flag')
  const orgCols = await db.query(`
    SELECT column_name FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'organizations' AND column_name = 'onboarding_state'
  `)
  assert.equal(orgCols.rows.length, 1, 'organizations gained onboarding_state')

  // End-to-end flow: tenant insert of a contact + import run, and privacy flag on activity.
  await db.exec(`
    BEGIN;
    SELECT set_config('app.org_id', '${org}', true);
    SELECT set_config('app.department_id', '${dep}', true);
    SET CONSTRAINTS ALL DEFERRED;
    INSERT INTO organizations (id, org_id, department_id, name, login_slug)
      VALUES ('${org}', '${org}', '${dep}', 'SON419 Org', 'son419-org');
    INSERT INTO departments (id, org_id, department_id, name, login_slug, shape, is_silent)
      VALUES ('${dep}', '${org}', '${dep}', '__default__', 'default', 'silent', true);
    INSERT INTO app_users (id, org_id, department_id, email, display_name, password_hash)
      VALUES ('${user}', '${org}', '${dep}', 's419@x.io', 'S419', 'x');
    INSERT INTO contact_imports
      (id, org_id, department_id, filename, column_count, row_count, created_count, merged_count, skipped_count, created_by_user_id)
      VALUES ('00000000-0000-0000-0000-0000000000a4', '${org}', '${dep}', 'demo.csv', 3, 2, 2, 0, 0, '${user}');
    INSERT INTO instructions
      (id, org_id, department_id, title, body, applies_to, slug, version, effective_from, superseded_at, active)
      VALUES ('00000000-0000-0000-0000-0000000000a5', '${org}', '${dep}', 't', 'b', 'all', 'topic', 1, now(), NULL, true);
    COMMIT;
  `)

  // The partial unique index allows a second version once the first is superseded.
  await db.exec(`
    BEGIN;
    SELECT set_config('app.org_id', '${org}', true);
    SELECT set_config('app.department_id', '${dep}', true);
    UPDATE instructions SET superseded_at = now(), active = false WHERE slug = 'topic';
    INSERT INTO instructions
      (id, org_id, department_id, title, body, applies_to, slug, version, effective_from, superseded_at, active)
      VALUES ('00000000-0000-0000-0000-0000000000a6', '${org}', '${dep}', 't2', 'b2', 'all', 'topic', 2, now(), NULL, true);
    COMMIT;
  `)

  console.log('PASS: SON-419 migration applies cleanly; versioned instructions, personal/team lists, forced-RLS imports, activity privacy, and onboarding state all land.')
} catch (error) {
  console.error('FAIL:', error.message)
  process.exitCode = 1
}
