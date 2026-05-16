-- Referral split migration (safe / reversible)
-- Apply:
--   psql ... -f migration_20260516_referral_split.sql
--
-- Rollback (if needed):
--   ALTER TABLE billing DROP CONSTRAINT IF EXISTS billing_referrer_share_rate_range;
--   ALTER TABLE billing DROP COLUMN IF EXISTS referrer_share_rate;

ALTER TABLE billing
  ADD COLUMN IF NOT EXISTS referrer_share_rate NUMERIC(4,2) DEFAULT 0.20;

UPDATE billing
SET referrer_share_rate = 0.20
WHERE referrer_share_rate IS NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'billing_referrer_share_rate_range'
  ) THEN
    ALTER TABLE billing
      ADD CONSTRAINT billing_referrer_share_rate_range
      CHECK (referrer_share_rate >= 0 AND referrer_share_rate <= 1);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_referral_commissions_referrer_date
  ON referral_commissions(referrer_id, date DESC);
