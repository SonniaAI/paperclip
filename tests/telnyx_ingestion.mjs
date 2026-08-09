import { spawn } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { PGlite } from '@electric-sql/pglite'
import { PGLiteSocketServer } from '@electric-sql/pglite-socket'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')
const gate = await readFile(join(root, 'alembic', 'versions', '20260809_gate0_rls.sql'), 'utf8')
const features = await readFile(
  join(root, 'alembic', 'versions', '20260809_feature_data.sql'),
  'utf8',
)

const ids = {
  org: '00000000-0000-0000-0000-0000000000d1',
  department: '00000000-0000-0000-0000-0000000000d2',
}

const db = new PGlite()
const server = new PGLiteSocketServer({ db, host: '127.0.0.1', port: 0, maxConnections: 10 })

function runPython(databaseUrl) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.PYTHON || 'python3', ['tests/telnyx_ingestion.py'], {
      cwd: root,
      env: {
        ...process.env,
        MANAGER_DATABASE_URL: databaseUrl,
        MANAGER_TEST_ORG_ID: ids.org,
        MANAGER_TEST_DEPARTMENT_ID: ids.department,
      },
      stdio: 'inherit',
    })
    child.on('error', reject)
    child.on('exit', (code, signal) => {
      if (code === 0) {
        resolve()
      } else {
        reject(
          new Error(
            `Telnyx ingestion Python runner exited with code ${code ?? 'null'} ` +
              `(${signal ?? 'no signal'})`,
          ),
        )
      }
    })
  })
}

try {
  await db.exec(gate)
  await db.exec(features)
  await db.exec(`
    BEGIN;
    SELECT set_config('app.org_id', '${ids.org}', true);
    SELECT set_config('app.department_id', '${ids.department}', true);
    SET CONSTRAINTS ALL DEFERRED;
    INSERT INTO organizations (id, org_id, department_id, name, login_slug)
      VALUES ('${ids.org}', '${ids.org}', '${ids.department}', 'Ingestion Org', 'ingestion-org');
    INSERT INTO departments (id, org_id, department_id, name, login_slug, shape, is_silent)
      VALUES (
        '${ids.department}', '${ids.org}', '${ids.department}',
        '__default__', 'default', 'silent', true
      );
    COMMIT;
    CREATE ROLE manager_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    GRANT USAGE ON SCHEMA public, app TO manager_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO manager_app;
  `)
  await server.start()
  const databaseUrl = `postgresql+asyncpg://manager_app:ignored@${server.getServerConn()}/postgres`
  await runPython(databaseUrl)
} finally {
  await server.stop()
  await db.close()
}
