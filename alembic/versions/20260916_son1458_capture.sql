-- SON-1458: CP2/CP3 append-only capture logging at the webhook boundary.
-- System-level audit tables: intentionally NOT tenant-scoped (they record
-- pre-ingestion receipts and cross-tenant processing traces); RLS is not
-- applied.  Append-only: the application only ever INSERTs.

CREATE TABLE IF NOT EXISTS webhook_receipt_capture (
    id UUID PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source VARCHAR(40) NOT NULL DEFAULT 'telnyx',
    method VARCHAR(10),
    path TEXT,
    remote_addr TEXT,
    headers_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_body TEXT NOT NULL,
    body_bytes INTEGER NOT NULL,
    signature_algorithm VARCHAR(40),
    signature_result VARCHAR(20) NOT NULL,
    signature_detail TEXT,
    tenant_scope TEXT,
    event_id TEXT
);

CREATE INDEX IF NOT EXISTS ix_webhook_receipt_capture_event_id
    ON webhook_receipt_capture (event_id);
CREATE INDEX IF NOT EXISTS ix_webhook_receipt_capture_received_at
    ON webhook_receipt_capture (received_at);

CREATE TABLE IF NOT EXISTS ingestion_trace_capture (
    id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    receipt_id UUID REFERENCES webhook_receipt_capture (id),
    event_id TEXT NOT NULL,
    event_type TEXT,
    dedupe_decision VARCHAR(30),
    outcome VARCHAR(40) NOT NULL,
    call_id UUID,
    contact_id UUID,
    transcript_ref TEXT,
    recording_refs JSONB,
    build_sha TEXT,
    latency_ms INTEGER
);

CREATE INDEX IF NOT EXISTS ix_ingestion_trace_capture_event_id
    ON ingestion_trace_capture (event_id);
CREATE INDEX IF NOT EXISTS ix_ingestion_trace_capture_receipt_id
    ON ingestion_trace_capture (receipt_id);
