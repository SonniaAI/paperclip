-- Demo data provenance only.  This migration does not create analytics data.
-- The seed CLI is deliberately opt-in and every record it creates is listed
-- here so it can be deleted without touching customer-created records.

CREATE TABLE demo_seed_runs (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    label text NOT NULL DEFAULT 'manager.sonnia.ai/demo-data',
    seed_version text NOT NULL,
    run_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, department_id, label),
    UNIQUE (org_id, department_id, id),
    CHECK (label = 'manager.sonnia.ai/demo-data'),
    CHECK (jsonb_typeof(run_metadata) = 'object')
);

CREATE TABLE demo_seed_records (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    seed_run_id uuid NOT NULL,
    entity_type text NOT NULL,
    record_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (org_id, department_id, seed_run_id)
        REFERENCES demo_seed_runs (org_id, department_id, id) ON DELETE CASCADE,
    UNIQUE (seed_run_id, entity_type, record_id),
    CHECK (entity_type IN (
        'companies', 'contacts', 'contact_phones', 'contact_emails', 'calls',
        'recordings', 'transcripts', 'transcript_segments', 'call_summaries',
        'follow_ups', 'notes', 'task_lists', 'tasks', 'actions', 'activity_log'
    ))
);

CREATE INDEX demo_seed_runs_tenant_idx
    ON demo_seed_runs (org_id, department_id, created_at DESC);
CREATE INDEX demo_seed_records_lookup_idx
    ON demo_seed_records (org_id, department_id, entity_type, record_id);

ALTER TABLE demo_seed_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_seed_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON demo_seed_runs
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE demo_seed_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_seed_records FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON demo_seed_records
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

-- Production analytics must use this security-invoker predicate.  It sees
-- only the caller's tenant scope under RLS, and a demo run is never silently
-- blended into customer/business metrics.
CREATE FUNCTION app.is_demo_seed_record(candidate_entity_type text, candidate_record_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, public, app
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM demo_seed_records
        WHERE entity_type = candidate_entity_type
          AND record_id = candidate_record_id
    );
$$;
