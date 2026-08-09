-- Phase 1 feature data model.
--
-- Gate 0 owns the organizations/app_users/calls/auth tables.  This revision
-- extends that schema with the §15 product model.  Every table created here
-- carries both tenant keys and is protected by forced RLS; the existing
-- app_users table is the canonical users table and organizations is the
-- canonical organisation table.

CREATE TABLE companies (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    name text NOT NULL,
    legal_name text,
    domain text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, id)
);

CREATE TABLE contacts (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    company_id uuid REFERENCES companies(id),
    display_name text NOT NULL,
    first_name text,
    last_name text,
    job_title text,
    do_not_contact boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, id)
);

CREATE TABLE contact_phones (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    contact_id uuid NOT NULL REFERENCES contacts(id),
    phone_e164 text NOT NULL,
    label text,
    is_primary boolean NOT NULL DEFAULT false,
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, contact_id, phone_e164)
);

CREATE TABLE contact_emails (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    contact_id uuid NOT NULL REFERENCES contacts(id),
    email text NOT NULL,
    label text,
    is_primary boolean NOT NULL DEFAULT false,
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, contact_id, email)
);

ALTER TABLE calls
    ADD COLUMN contact_id uuid REFERENCES contacts(id),
    ADD COLUMN company_id uuid REFERENCES companies(id),
    ADD COLUMN direction varchar(10),
    ADD COLUMN status varchar(20) NOT NULL DEFAULT 'completed',
    ADD COLUMN telnyx_call_control_id text,
    ADD COLUMN telnyx_call_session_id text,
    ADD COLUMN from_phone_e164 text,
    ADD COLUMN to_phone_e164 text,
    ADD COLUMN started_at timestamptz,
    ADD COLUMN ended_at timestamptz,
    ADD COLUMN metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD CONSTRAINT calls_org_id_id_key UNIQUE (org_id, id),
    ADD CONSTRAINT calls_direction_check CHECK (direction IS NULL OR direction IN ('inbound', 'outbound')),
    ADD CONSTRAINT calls_status_check CHECK (status IN ('ringing', 'in_progress', 'completed', 'failed', 'missed'));

CREATE TABLE recordings (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    call_id uuid NOT NULL REFERENCES calls(id),
    storage_bucket text NOT NULL,
    storage_key text NOT NULL,
    content_type text,
    byte_size bigint,
    duration_ms integer,
    is_private boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, storage_bucket, storage_key),
    CHECK (is_private = true),
    CHECK (byte_size IS NULL OR byte_size >= 0),
    CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE TABLE transcripts (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    call_id uuid NOT NULL REFERENCES calls(id),
    provider text,
    language_code text,
    status varchar(20) NOT NULL DEFAULT 'pending',
    raw_text text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, call_id),
    CHECK (status IN ('pending', 'processing', 'complete', 'failed'))
);

CREATE TABLE transcript_segments (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    transcript_id uuid NOT NULL REFERENCES transcripts(id),
    sequence integer NOT NULL,
    speaker text,
    text text NOT NULL,
    start_ms integer NOT NULL,
    end_ms integer NOT NULL,
    markers jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, transcript_id, sequence),
    CHECK (sequence >= 0),
    CHECK (start_ms >= 0 AND end_ms >= start_ms)
);

CREATE TABLE call_summaries (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    call_id uuid NOT NULL REFERENCES calls(id),
    summary text NOT NULL,
    sentiment varchar(20),
    outcome text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, call_id)
);

CREATE TABLE promises (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    call_id uuid REFERENCES calls(id),
    contact_id uuid REFERENCES contacts(id),
    owner_user_id uuid REFERENCES app_users(id),
    promise text NOT NULL,
    due_at timestamptz,
    status varchar(20) NOT NULL DEFAULT 'open',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN ('open', 'kept', 'broken', 'cancelled'))
);

CREATE TABLE actions (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    call_id uuid REFERENCES calls(id),
    contact_id uuid REFERENCES contacts(id),
    owner_user_id uuid REFERENCES app_users(id),
    action text NOT NULL,
    due_at timestamptz,
    status varchar(20) NOT NULL DEFAULT 'open',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN ('open', 'in_progress', 'done', 'cancelled'))
);

CREATE TABLE follow_ups (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    call_id uuid REFERENCES calls(id),
    contact_id uuid REFERENCES contacts(id),
    owner_user_id uuid REFERENCES app_users(id),
    scheduled_for timestamptz NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'scheduled',
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN ('scheduled', 'completed', 'cancelled'))
);

CREATE TABLE notes (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    contact_id uuid REFERENCES contacts(id),
    company_id uuid REFERENCES companies(id),
    call_id uuid REFERENCES calls(id),
    author_user_id uuid REFERENCES app_users(id),
    body text NOT NULL,
    visibility varchar(10) NOT NULL DEFAULT 'shared',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (visibility IN ('private', 'shared'))
);

CREATE TABLE task_lists (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    name text NOT NULL,
    description text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, department_id, name)
);

CREATE TABLE tasks (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    task_list_id uuid REFERENCES task_lists(id),
    contact_id uuid REFERENCES contacts(id),
    call_id uuid REFERENCES calls(id),
    owner_user_id uuid REFERENCES app_users(id),
    created_by_user_id uuid REFERENCES app_users(id),
    title text NOT NULL,
    description text,
    due_at timestamptz,
    status varchar(20) NOT NULL DEFAULT 'open',
    visibility varchar(10) NOT NULL DEFAULT 'shared',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN ('open', 'in_progress', 'done', 'cancelled')),
    CHECK (visibility IN ('private', 'shared'))
);

CREATE TABLE campaigns (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    name text NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'draft',
    starts_at timestamptz,
    ends_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN ('draft', 'active', 'paused', 'completed', 'archived'))
);

CREATE TABLE campaign_briefs (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    campaign_id uuid NOT NULL REFERENCES campaigns(id),
    title text NOT NULL,
    body text NOT NULL,
    version integer NOT NULL DEFAULT 1,
    created_by_user_id uuid REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (version > 0)
);

CREATE TABLE brief_chats (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    campaign_id uuid REFERENCES campaigns(id),
    campaign_brief_id uuid REFERENCES campaign_briefs(id),
    user_id uuid REFERENCES app_users(id),
    role varchar(20) NOT NULL,
    message text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (role IN ('user', 'assistant', 'system'))
);

CREATE TABLE campaign_materials (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    campaign_id uuid REFERENCES campaigns(id),
    filename text NOT NULL,
    content_type text,
    storage_bucket text NOT NULL,
    storage_key text NOT NULL,
    is_private boolean NOT NULL DEFAULT true,
    created_by_user_id uuid REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (is_private = true),
    UNIQUE (org_id, storage_bucket, storage_key)
);

CREATE TABLE extracted_claims (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    campaign_material_id uuid NOT NULL REFERENCES campaign_materials(id),
    claim text NOT NULL,
    rank integer NOT NULL,
    confidence numeric(5, 4),
    source_locator text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (rank > 0),
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE TABLE spend_ledger (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    campaign_id uuid REFERENCES campaigns(id),
    provider text NOT NULL,
    external_reference text,
    amount_minor bigint NOT NULL,
    currency char(3) NOT NULL,
    occurred_at timestamptz NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, provider, external_reference)
);

CREATE TABLE org_balance (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    balance_minor bigint NOT NULL DEFAULT 0,
    currency char(3) NOT NULL,
    as_of timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, department_id, currency)
);

CREATE TABLE campaign_reads (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    campaign_id uuid NOT NULL REFERENCES campaigns(id),
    user_id uuid NOT NULL REFERENCES app_users(id),
    material_id uuid REFERENCES campaign_materials(id),
    read_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, department_id, campaign_id, user_id, material_id)
);

CREATE TABLE test_personas (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    name text NOT NULL,
    description text,
    traits jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test_suites (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    name text NOT NULL,
    description text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test_sessions (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    suite_id uuid REFERENCES test_suites(id),
    persona_id uuid REFERENCES test_personas(id),
    status varchar(20) NOT NULL DEFAULT 'queued',
    started_at timestamptz,
    finished_at timestamptz,
    results jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN ('queued', 'running', 'passed', 'failed', 'cancelled'))
);

CREATE TABLE instructions (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    title text NOT NULL,
    body text NOT NULL,
    applies_to text NOT NULL DEFAULT 'all',
    active boolean NOT NULL DEFAULT true,
    created_by_user_id uuid REFERENCES app_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE activity_log (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    actor_user_id uuid REFERENCES app_users(id),
    action text NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE integrations (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    provider text NOT NULL,
    kind text NOT NULL,
    external_account_id text,
    credentials_ref text,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    status varchar(20) NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN ('active', 'paused', 'error', 'revoked')),
    UNIQUE (org_id, department_id, provider, kind)
);

CREATE TABLE security_events (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    actor_user_id uuid REFERENCES app_users(id),
    event_type text NOT NULL,
    ip_address text,
    user_agent text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- The raw body is the source of truth for replay.  event_id is globally
-- unique so the same provider event cannot be applied twice to two scopes.
CREATE TABLE telnyx_webhook_events (
    id uuid PRIMARY KEY,
    org_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid NOT NULL REFERENCES departments(id),
    event_id text NOT NULL UNIQUE,
    event_type text NOT NULL,
    raw_payload jsonb NOT NULL,
    payload_sha256 char(64) NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'received',
    parse_error text,
    received_at timestamptz NOT NULL DEFAULT now(),
    parsed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN ('received', 'processing', 'processed', 'ignored', 'failed'))
);

CREATE INDEX companies_tenant_idx ON companies (org_id, department_id);
CREATE INDEX contacts_tenant_idx ON contacts (org_id, department_id);
CREATE INDEX contact_phones_tenant_idx ON contact_phones (org_id, department_id);
CREATE INDEX contact_emails_tenant_idx ON contact_emails (org_id, department_id);
CREATE INDEX recordings_tenant_idx ON recordings (org_id, department_id);
CREATE INDEX transcripts_tenant_idx ON transcripts (org_id, department_id);
CREATE INDEX transcript_segments_tenant_idx ON transcript_segments (org_id, department_id);
CREATE INDEX call_summaries_tenant_idx ON call_summaries (org_id, department_id);
CREATE INDEX promises_tenant_idx ON promises (org_id, department_id);
CREATE INDEX actions_tenant_idx ON actions (org_id, department_id);
CREATE INDEX follow_ups_tenant_idx ON follow_ups (org_id, department_id);
CREATE INDEX notes_tenant_idx ON notes (org_id, department_id);
CREATE INDEX task_lists_tenant_idx ON task_lists (org_id, department_id);
CREATE INDEX tasks_tenant_idx ON tasks (org_id, department_id);
CREATE INDEX campaigns_tenant_idx ON campaigns (org_id, department_id);
CREATE INDEX campaign_briefs_tenant_idx ON campaign_briefs (org_id, department_id);
CREATE INDEX brief_chats_tenant_idx ON brief_chats (org_id, department_id);
CREATE INDEX campaign_materials_tenant_idx ON campaign_materials (org_id, department_id);
CREATE INDEX extracted_claims_tenant_idx ON extracted_claims (org_id, department_id);
CREATE INDEX spend_ledger_tenant_idx ON spend_ledger (org_id, department_id);
CREATE INDEX org_balance_tenant_idx ON org_balance (org_id, department_id);
CREATE INDEX campaign_reads_tenant_idx ON campaign_reads (org_id, department_id);
CREATE INDEX test_personas_tenant_idx ON test_personas (org_id, department_id);
CREATE INDEX test_suites_tenant_idx ON test_suites (org_id, department_id);
CREATE INDEX test_sessions_tenant_idx ON test_sessions (org_id, department_id);
CREATE INDEX instructions_tenant_idx ON instructions (org_id, department_id);
CREATE INDEX activity_log_tenant_idx ON activity_log (org_id, department_id);
CREATE INDEX integrations_tenant_idx ON integrations (org_id, department_id);
CREATE INDEX security_events_tenant_idx ON security_events (org_id, department_id);
CREATE INDEX telnyx_webhook_events_tenant_idx ON telnyx_webhook_events (org_id, department_id);

ALTER TABLE companies ENABLE ROW LEVEL SECURITY;
ALTER TABLE companies FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON companies
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE contacts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON contacts
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE contact_phones ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_phones FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON contact_phones
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE contact_emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_emails FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON contact_emails
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE recordings ENABLE ROW LEVEL SECURITY;
ALTER TABLE recordings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON recordings
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE transcripts ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcripts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON transcripts
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE transcript_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcript_segments FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON transcript_segments
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE call_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE call_summaries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON call_summaries
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE promises ENABLE ROW LEVEL SECURITY;
ALTER TABLE promises FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON promises
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE actions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON actions
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE follow_ups ENABLE ROW LEVEL SECURITY;
ALTER TABLE follow_ups FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON follow_ups
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE notes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON notes
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE task_lists ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_lists FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON task_lists
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tasks
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaigns FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON campaigns
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE campaign_briefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_briefs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON campaign_briefs
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE brief_chats ENABLE ROW LEVEL SECURITY;
ALTER TABLE brief_chats FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON brief_chats
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE campaign_materials ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_materials FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON campaign_materials
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE extracted_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE extracted_claims FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON extracted_claims
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE spend_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE spend_ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON spend_ledger
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE org_balance ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_balance FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON org_balance
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE campaign_reads ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_reads FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON campaign_reads
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE test_personas ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_personas FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON test_personas
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE test_suites ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_suites FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON test_suites
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE test_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON test_sessions
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE instructions ENABLE ROW LEVEL SECURITY;
ALTER TABLE instructions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON instructions
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE activity_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE activity_log FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON activity_log
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE integrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE integrations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON integrations
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE security_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE security_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON security_events
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());

ALTER TABLE telnyx_webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE telnyx_webhook_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON telnyx_webhook_events
    USING (org_id = app.current_org_id() AND department_id = app.current_department_id())
    WITH CHECK (org_id = app.current_org_id() AND department_id = app.current_department_id());
