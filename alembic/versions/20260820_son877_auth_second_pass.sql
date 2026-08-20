-- SON-877: marketing-consent evidence columns.
-- The registration flow already persists marketing_consent as a boolean
-- alongside terms_version and terms_accepted_at; these columns add the
-- consent timestamp and its source so consent can be evidenced (Registration
-- & Password spec §6.4 and the founder second-pass review item 9).

ALTER TABLE app_users
    ADD COLUMN IF NOT EXISTS marketing_consent_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS marketing_consent_source VARCHAR(40);
