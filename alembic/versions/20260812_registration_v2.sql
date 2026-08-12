-- V2 profile fields stay nullable at the storage boundary so existing
-- accounts migrate safely; RegisterRequest requires them for new accounts.

ALTER TABLE app_users
    ADD COLUMN phone varchar(16),
    ADD COLUMN date_of_birth date,
    ADD COLUMN gender varchar(80),
    ADD COLUMN profile_role varchar(120),
    ADD COLUMN industry varchar(120);
