-- SON-1529: persist campaign schedule + tone dials on `campaigns`
-- (SON-1493 ruling: typed nullable columns in place; NO campaign_settings
-- side table, NO JSONB). Metadata-only ADD COLUMN — no table rewrite, no
-- backfill; NULL = "not configured", the app resolves org defaults at read
-- time. Old images ignore the new columns, so DDL and image rolls are
-- decoupled (DDL first).

ALTER TABLE campaigns
    ADD COLUMN schedule_timezone text,
    ADD COLUMN call_window_start time,
    ADD COLUMN call_window_end time,
    ADD COLUMN active_days text[],
    ADD COLUMN max_attempts_per_lead smallint,
    ADD COLUMN retry_delay_minutes integer,
    ADD COLUMN daily_call_cap integer,
    ADD COLUMN max_total_calls integer,
    ADD COLUMN tone_formality smallint,
    ADD COLUMN tone_pace smallint,
    ADD COLUMN tone_persistence smallint,
    ADD COLUMN tone_warmth smallint,
    ADD COLUMN tone_depth smallint;

ALTER TABLE campaigns
    ADD CONSTRAINT campaigns_max_attempts_per_lead_check
        CHECK (max_attempts_per_lead IS NULL OR max_attempts_per_lead >= 1),
    ADD CONSTRAINT campaigns_retry_delay_minutes_check
        CHECK (retry_delay_minutes IS NULL OR retry_delay_minutes >= 0),
    ADD CONSTRAINT campaigns_daily_call_cap_check
        CHECK (daily_call_cap IS NULL OR daily_call_cap >= 0),
    ADD CONSTRAINT campaigns_max_total_calls_check
        CHECK (max_total_calls IS NULL OR max_total_calls >= 0),
    ADD CONSTRAINT campaigns_tone_formality_check
        CHECK (tone_formality IS NULL OR tone_formality BETWEEN -2 AND 2),
    ADD CONSTRAINT campaigns_tone_pace_check
        CHECK (tone_pace IS NULL OR tone_pace BETWEEN -2 AND 2),
    ADD CONSTRAINT campaigns_tone_persistence_check
        CHECK (tone_persistence IS NULL OR tone_persistence BETWEEN 0 AND 1),
    ADD CONSTRAINT campaigns_tone_warmth_check
        CHECK (tone_warmth IS NULL OR tone_warmth BETWEEN -2 AND 2),
    ADD CONSTRAINT campaigns_tone_depth_check
        CHECK (tone_depth IS NULL OR tone_depth BETWEEN 0 AND 2),
    ADD CONSTRAINT campaigns_active_days_check
        CHECK (active_days IS NULL OR (
            array_length(active_days, 1) >= 1
            AND active_days <@ ARRAY['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']::text[]
        ));
