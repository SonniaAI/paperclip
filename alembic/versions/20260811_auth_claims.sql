-- Persist the active two-factor challenge so successful completion can claim
-- it atomically. A replacement login challenge overwrites the digest, and a
-- successful conditional UPDATE clears it exactly once.

ALTER TABLE app_users
    ADD COLUMN two_factor_challenge_token_digest char(64),
    ADD COLUMN two_factor_challenge_expires_at timestamptz;
