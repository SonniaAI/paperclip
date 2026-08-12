-- V2 profile fields stay nullable at the storage boundary so existing
-- accounts migrate safely; RegisterRequest requires them for new accounts.

ALTER TABLE app_users
    ADD COLUMN phone varchar(16),
    ADD COLUMN date_of_birth date,
    ADD COLUMN gender varchar(80),
    ADD COLUMN profile_role varchar(120),
    ADD COLUMN industry varchar(120),
    ADD COLUMN terms_version varchar(40),
    ADD COLUMN terms_accepted_at timestamptz,
    ADD COLUMN marketing_consent boolean NOT NULL DEFAULT false;

ALTER TABLE organizations
    ADD COLUMN company_size varchar(20),
    ADD COLUMN company_website text,
    ADD COLUMN referral_source varchar(40);
