-- SON-551: preserve source provenance locally and make contact-only fuzzy
-- memory deletion durable.  The deterministic contact book remains separate
-- from the optional Hindsight copy.

ALTER TABLE contact_memory_batches
    ADD COLUMN source_event_id varchar(200),
    ADD COLUMN source_occurred_at timestamptz,
    ADD COLUMN speaker varchar(100),
    ADD COLUMN extraction_provenance varchar(200);

CREATE TABLE contact_memory_deletions (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    contact_id uuid NOT NULL REFERENCES contacts(id),
    hindsight_bank_id varchar(300) NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    requested_by_user_id uuid REFERENCES app_users(id),
    requested_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, contact_id),
    CHECK (status IN ('pending', 'failed', 'delivered')),
    CHECK (attempts >= 0),
    CHECK (last_error IS NULL OR char_length(last_error) <= 512)
);

CREATE INDEX contact_memory_deletions_retry_idx
    ON contact_memory_deletions (org_id, department_id, status, requested_at);

ALTER TABLE contact_memory_deletions ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_memory_deletions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON contact_memory_deletions
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());
