-- SON-419: instructions versioning, personal task lists, contact imports,
-- and organisation onboarding state.
--
-- This revision layers the four §12/§11/§4.5/§4.6 areas of the SON-419 scope
-- onto the existing §15 feature schema:
--   * instructions become a versioned series with an "in effect since" marker;
--   * task_lists become personal-or-team (visibility + owner);
--   * contact_imports records durable CSV import runs (mapping/preview/summary);
--   * organizations gains an explicit onboarding_state for the §4.6 flow.
-- Every table created here carries both tenant keys and forced RLS, matching
-- the repository-wide tenancy rule.

-- 1. Instructions become a versioned series.
--    slug is the stable identity of one instruction topic; a superseded row is
--    the immutable history entry.  At most one non-superseded version exists
--    per (org, department, slug), so an edit supersedes the active row and
--    creates the next version with its own effective_from.
ALTER TABLE instructions
    ADD COLUMN slug text NOT NULL DEFAULT '',
    ADD COLUMN version integer NOT NULL DEFAULT 1,
    ADD COLUMN effective_from timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN superseded_at timestamptz;

CREATE UNIQUE INDEX instructions_active_slug_key
    ON instructions (org_id, department_id, slug)
    WHERE superseded_at IS NULL;

CREATE INDEX instructions_history_idx
    ON instructions (org_id, department_id, slug, version DESC);

-- 2. Task lists become personal-or-team.
ALTER TABLE task_lists
    ADD COLUMN owner_user_id uuid REFERENCES app_users(id),
    ADD COLUMN visibility varchar(10) NOT NULL DEFAULT 'private',
    ADD CONSTRAINT task_lists_visibility_check
        CHECK (visibility IN ('private', 'shared'));

-- 3. Durable CSV contact-import runs (column mapping, preview, dedupe, summary).
CREATE TABLE contact_imports (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    filename text NOT NULL,
    column_count integer NOT NULL,
    row_count integer NOT NULL,
    created_count integer NOT NULL DEFAULT 0,
    merged_count integer NOT NULL DEFAULT 0,
    skipped_count integer NOT NULL DEFAULT 0,
    created_by_user_id uuid REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (column_count >= 0),
    CHECK (row_count >= 0),
    CHECK (created_count >= 0),
    CHECK (merged_count >= 0),
    CHECK (skipped_count >= 0)
);

CREATE INDEX contact_imports_tenant_idx ON contact_imports (org_id, department_id);

ALTER TABLE contact_imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_imports FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON contact_imports
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

-- 4. Activity entries carry an explicit privacy flag.  The one chronological
--    feed presents every entry to the tenant, but a private entry (a private
--    note/task event) is invisible to everyone except its actor.
ALTER TABLE activity_log
    ADD COLUMN private boolean NOT NULL DEFAULT false;

CREATE INDEX activity_log_feed_idx
    ON activity_log (org_id, department_id, created_at DESC);

-- 5. Organisations carry an explicit onboarding state for the §4.6 flow.
ALTER TABLE organizations
    ADD COLUMN onboarding_state text NOT NULL DEFAULT 'pending',
    ADD CONSTRAINT organizations_onboarding_state_check
        CHECK (onboarding_state IN ('pending', 'instructions', 'import', 'invite', 'complete'));
