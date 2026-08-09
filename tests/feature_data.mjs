import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { PGlite } from '@electric-sql/pglite'

const base = await readFile('alembic/versions/20260809_gate0_rls.sql', 'utf8')
const feature = await readFile('alembic/versions/20260809_feature_data.sql', 'utf8')
const db = new PGlite()
const tables = [
  'companies', 'contacts', 'contact_phones', 'contact_emails', 'recordings',
  'transcripts', 'transcript_segments', 'call_summaries', 'promises', 'actions',
  'follow_ups', 'notes', 'task_lists', 'tasks', 'campaigns', 'campaign_briefs',
  'brief_chats', 'campaign_materials', 'extracted_claims', 'spend_ledger',
  'org_balance', 'campaign_reads', 'test_personas', 'test_suites', 'test_sessions',
  'instructions', 'activity_log', 'integrations', 'security_events',
  'telnyx_webhook_events',
]

const ids = {
  org: '00000000-0000-0000-0000-0000000000c1',
  dep: '00000000-0000-0000-0000-0000000000c2',
  call: '00000000-0000-0000-0000-0000000000c3',
}

try {
  await db.exec(base)
  await db.exec(feature)
  const columns = await db.query(`
    SELECT table_name, count(*)::int AS scoped
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = ANY($1)
      AND column_name IN ('org_id', 'department_id')
    GROUP BY table_name
  `, [tables])
  assert.equal(columns.rows.length, tables.length)
  assert.ok(columns.rows.every((row) => row.scoped === 2))

  await db.exec(`
    BEGIN;
    SELECT set_config('app.org_id', '${ids.org}', true);
    SELECT set_config('app.department_id', '${ids.dep}', true);
    SET CONSTRAINTS ALL DEFERRED;
    INSERT INTO organizations (id, org_id, department_id, name, login_slug)
      VALUES ('${ids.org}', '${ids.org}', '${ids.dep}', 'Feature Org', 'feature-org');
    INSERT INTO departments (id, org_id, department_id, name, login_slug, shape, is_silent)
      VALUES ('${ids.dep}', '${ids.org}', '${ids.dep}', '__default__', 'default', 'silent', true);
    INSERT INTO calls (id, org_id, department_id, external_call_key, subject)
      VALUES ('${ids.call}', '${ids.org}', '${ids.dep}', 'feature-call', 'Feature call');
    INSERT INTO telnyx_webhook_events
      (id, org_id, department_id, event_id, event_type, raw_payload, payload_sha256)
      VALUES ('${ids.call}', '${ids.org}', '${ids.dep}', 'evt-1', 'call.hangup', '{}', repeat('a', 64));
    COMMIT;
  `)

  const duplicate = await db.query(`
    INSERT INTO telnyx_webhook_events
      (id, org_id, department_id, event_id, event_type, raw_payload, payload_sha256)
      VALUES ('${ids.org}', '${ids.org}', '${ids.dep}', 'evt-1', 'call.hangup', '{}', repeat('a', 64))
      ON CONFLICT (event_id) DO NOTHING RETURNING event_id
  `)
  assert.equal(duplicate.rows.length, 0)

  let privateRejected = false
  try {
    await db.exec(`INSERT INTO recordings
      (id, org_id, department_id, call_id, storage_bucket, storage_key, is_private)
      VALUES ('${ids.org}', '${ids.org}', '${ids.dep}', '${ids.call}', 'private', 'x', false)`)
  } catch {
    privateRejected = true
  }
  assert.equal(privateRejected, true)
  console.log(`PASS: feature schema has ${tables.length} tenant tables; raw Telnyx dedupe and private recording constraints hold.`)
} finally {
  await db.close()
}
