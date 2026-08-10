-- Deterministic contact-memory rows remain the system of record.  Hindsight
-- receives only a retryable, redacted document assembled from these rows.

CREATE TABLE contact_memory_batches (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    contact_id uuid NOT NULL REFERENCES contacts(id),
    call_id uuid REFERENCES calls(id),
    transcript_id uuid REFERENCES transcripts(id),
    source_kind varchar(20) NOT NULL DEFAULT 'manual',
    idempotency_key varchar(200) NOT NULL,
    hindsight_document_id varchar(200) NOT NULL,
    created_by_user_id uuid REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, id),
    UNIQUE (org_id, contact_id, idempotency_key),
    UNIQUE (org_id, hindsight_document_id),
    CHECK (source_kind IN ('manual', 'call_transcript')),
    CHECK (
        (source_kind = 'manual' AND transcript_id IS NULL)
        OR (source_kind = 'call_transcript' AND transcript_id IS NOT NULL)
    )
);

CREATE TABLE contact_memory_entries (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    batch_id uuid NOT NULL REFERENCES contact_memory_batches(id) ON DELETE CASCADE,
    contact_id uuid NOT NULL REFERENCES contacts(id),
    kind varchar(20) NOT NULL,
    value text NOT NULL,
    occurred_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, id),
    CHECK (kind IN ('fact', 'preference')),
    CHECK (length(btrim(value)) > 0)
);

CREATE TABLE hindsight_sync_jobs (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    batch_id uuid NOT NULL REFERENCES contact_memory_batches(id) ON DELETE CASCADE,
    status varchar(20) NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    delivered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, batch_id),
    CHECK (status IN ('pending', 'failed', 'delivered')),
    CHECK (attempts >= 0),
    CHECK (last_error IS NULL OR char_length(last_error) <= 512)
);

CREATE INDEX contact_memory_entries_contact_idx
    ON contact_memory_entries (org_id, department_id, contact_id, created_at DESC);
CREATE INDEX contact_memory_entries_batch_idx
    ON contact_memory_entries (org_id, batch_id, created_at);
CREATE INDEX hindsight_sync_jobs_retry_idx
    ON hindsight_sync_jobs (org_id, department_id, status, created_at);

ALTER TABLE contact_memory_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_memory_batches FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON contact_memory_batches
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE contact_memory_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_memory_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON contact_memory_entries
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE hindsight_sync_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE hindsight_sync_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON hindsight_sync_jobs
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());
