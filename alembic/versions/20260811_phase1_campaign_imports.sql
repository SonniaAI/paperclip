-- Live Phase 1 campaigns and CSV customer-list imports.
--
-- These records deliberately use the same forced RLS policy as the existing
-- feature model.  API handlers source both tenant keys from the verified
-- session scope and never accept them from request payloads.

ALTER TABLE campaigns
    ADD COLUMN objective text NOT NULL DEFAULT '',
    ADD COLUMN launched_at timestamptz;

CREATE TABLE campaign_targets (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    campaign_id uuid NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id uuid NOT NULL REFERENCES contacts(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, campaign_id, contact_id)
);

CREATE TABLE customer_list_imports (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    filename text NOT NULL,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    total_rows integer NOT NULL CHECK (total_rows >= 0),
    valid_rows integer NOT NULL CHECK (valid_rows >= 0),
    invalid_rows integer NOT NULL CHECK (invalid_rows >= 0),
    status varchar(20) NOT NULL DEFAULT 'completed',
    rows jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_by_user_id uuid REFERENCES app_users(id),
    CHECK (status IN ('processing', 'completed', 'failed')),
    CHECK (valid_rows + invalid_rows <= total_rows),
    UNIQUE (org_id, id)
);

CREATE TABLE customer_list_import_contacts (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    import_id uuid NOT NULL REFERENCES customer_list_imports(id) ON DELETE CASCADE,
    contact_id uuid NOT NULL REFERENCES contacts(id),
    row_index integer NOT NULL CHECK (row_index > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, import_id, contact_id),
    UNIQUE (org_id, import_id, row_index)
);

CREATE INDEX campaign_targets_tenant_idx ON campaign_targets (org_id, department_id);
CREATE INDEX campaign_targets_campaign_idx ON campaign_targets (campaign_id);
CREATE INDEX customer_list_imports_tenant_idx
    ON customer_list_imports (org_id, department_id, uploaded_at DESC);
CREATE INDEX customer_list_import_contacts_tenant_idx
    ON customer_list_import_contacts (org_id, department_id, import_id, row_index);

ALTER TABLE campaign_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_targets FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON campaign_targets
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE customer_list_imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_list_imports FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON customer_list_imports
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE customer_list_import_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_list_import_contacts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON customer_list_import_contacts
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());
