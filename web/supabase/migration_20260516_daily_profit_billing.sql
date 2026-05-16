-- Daily profit billing migration
-- Apply:
--   psql ... -f migration_20260516_daily_profit_billing.sql

ALTER TABLE deductions
  ADD COLUMN IF NOT EXISTS referrer_fee_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS outstanding_fee_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS pnl_source TEXT DEFAULT 'session_state';

CREATE TABLE IF NOT EXISTS daily_profit_invoices (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  settle_date DATE NOT NULL,
  daily_profit NUMERIC(12,2) NOT NULL DEFAULT 0,
  net_profit NUMERIC(12,2) NOT NULL DEFAULT 0,
  operator_rate NUMERIC(6,4) NOT NULL DEFAULT 0,
  operator_fee_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
  referrer_fee_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
  paid_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
  outstanding_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'none' CHECK (status IN ('none', 'paid', 'unpaid')),
  note TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (user_id, settle_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_profit_invoices_user_settle_date
  ON daily_profit_invoices(user_id, settle_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_profit_invoices_status
  ON daily_profit_invoices(status, settle_date DESC);

ALTER TABLE daily_profit_invoices ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'daily_profit_invoices'
      AND policyname = 'Users can view own daily profit invoices'
  ) THEN
    CREATE POLICY "Users can view own daily profit invoices"
      ON daily_profit_invoices FOR SELECT
      USING (auth.uid() = user_id);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'daily_profit_invoices'
      AND policyname = 'Admins can do anything on daily profit invoices'
  ) THEN
    CREATE POLICY "Admins can do anything on daily profit invoices"
      ON daily_profit_invoices FOR ALL
      USING (EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE));
  END IF;
END $$;
