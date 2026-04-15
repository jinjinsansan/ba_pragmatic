-- LAPLACE SaaS Platform Schema
-- Run this in Supabase SQL Editor (https://supabase.com/dashboard/project/xsxuyjctqvxxqdethmua/sql)

-- 1. Profiles (extends auth.users)
CREATE TABLE profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  display_name TEXT,
  referral_code TEXT UNIQUE,
  referred_by TEXT,
  is_admin BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own profile" ON profiles FOR SELECT USING (auth.uid() = id);
CREATE POLICY "Users can update own profile" ON profiles FOR UPDATE USING (auth.uid() = id);
CREATE POLICY "Admins can view all profiles" ON profiles FOR SELECT USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);
CREATE POLICY "Admins can update all profiles" ON profiles FOR UPDATE USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- Auto-create profile on signup
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO profiles (id, email, referral_code)
  VALUES (
    NEW.id,
    NEW.email,
    'REF-' || UPPER(SUBSTR(MD5(NEW.id::TEXT), 1, 8))
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION handle_new_user();

-- 2. Orders (package purchases)
CREATE TABLE orders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  plan TEXT NOT NULL CHECK (plan IN ('standard')),
  amount NUMERIC(12,2) NOT NULL,
  promo_code TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'confirmed', 'delivered')),
  usdt_network TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  confirmed_at TIMESTAMPTZ
);

ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own orders" ON orders FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own orders" ON orders FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Users can update own orders" ON orders FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "Admins can do anything on orders" ON orders FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- 3. Charges (operation funds)
CREATE TABLE charges (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  amount NUMERIC(12,2) NOT NULL,
  promo_code TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'confirmed')),
  usdt_network TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  confirmed_at TIMESTAMPTZ
);

ALTER TABLE charges ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own charges" ON charges FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own charges" ON charges FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Users can update own charges" ON charges FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "Admins can do anything on charges" ON charges FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- 4. Billing state
CREATE TABLE billing (
  user_id UUID PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
  bot_price NUMERIC(12,2) DEFAULT 0,
  bot_paid BOOLEAN DEFAULT FALSE,
  balance NUMERIC(12,2) DEFAULT 0,
  total_charged NUMERIC(12,2) DEFAULT 0,
  profit_share_rate NUMERIC(4,2) DEFAULT 0.20,
  carry_loss NUMERIC(12,2) DEFAULT 0,
  is_free BOOLEAN DEFAULT FALSE,
  suspended BOOLEAN DEFAULT FALSE,
  grace_deadline TIMESTAMPTZ,
  bot_config JSONB DEFAULT '{}'::jsonb,
  gui_state JSONB DEFAULT '{}'::jsonb,
  session_state JSONB DEFAULT '{}'::jsonb,
  recommended_tables JSONB DEFAULT '[]'::jsonb,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Migration (既存DBに追加する場合):
-- ALTER TABLE billing ADD COLUMN IF NOT EXISTS bot_config JSONB DEFAULT '{}'::jsonb;
-- ALTER TABLE billing ADD COLUMN IF NOT EXISTS gui_state JSONB DEFAULT '{}'::jsonb;
-- ALTER TABLE billing ADD COLUMN IF NOT EXISTS session_state JSONB DEFAULT '{}'::jsonb;
-- ALTER TABLE billing ADD COLUMN IF NOT EXISTS recommended_tables JSONB DEFAULT '[]'::jsonb;

ALTER TABLE billing ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own billing" ON billing FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Admins can do anything on billing" ON billing FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- 5. Deductions (daily settlements)
CREATE TABLE deductions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  date DATE NOT NULL,
  daily_profit NUMERIC(12,2) NOT NULL,
  fee_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
  carry_loss NUMERIC(12,2) DEFAULT 0,
  note TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE deductions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own deductions" ON deductions FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Admins can do anything on deductions" ON deductions FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- 6. Promo codes
CREATE TABLE promo_codes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code TEXT UNIQUE NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('package_free', 'charge_free', 'discount')),
  discount_percent INTEGER DEFAULT 100,
  max_uses INTEGER DEFAULT 1,
  used_count INTEGER DEFAULT 0,
  created_by UUID REFERENCES profiles(id),
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE promo_codes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Anyone can read active promos" ON promo_codes FOR SELECT USING (active = TRUE);
CREATE POLICY "Admins can do anything on promos" ON promo_codes FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- 7. Referral commissions
CREATE TABLE referral_commissions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  referrer_id UUID NOT NULL REFERENCES profiles(id),
  referred_id UUID NOT NULL REFERENCES profiles(id),
  charge_amount NUMERIC(12,2) NOT NULL,
  commission_rate NUMERIC(4,2) NOT NULL,
  commission_amount NUMERIC(12,2) NOT NULL,
  date DATE DEFAULT CURRENT_DATE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE referral_commissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own commissions" ON referral_commissions FOR SELECT USING (auth.uid() = referrer_id);
CREATE POLICY "Admins can do anything on commissions" ON referral_commissions FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- 8. Deliverables (ZIP downloads)
CREATE TABLE deliverables (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  file_path TEXT NOT NULL,
  version TEXT DEFAULT '1.0',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE deliverables ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own deliverables" ON deliverables FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Admins can do anything on deliverables" ON deliverables FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- 9. Referral withdrawals
CREATE TABLE referral_withdrawals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  amount NUMERIC(12,2) NOT NULL,
  wallet_address TEXT NOT NULL,
  network TEXT NOT NULL CHECK (network IN ('TRC-20', 'ERC-20')),
  status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  admin_note TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  processed_at TIMESTAMPTZ
);

ALTER TABLE referral_withdrawals ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own withdrawals" ON referral_withdrawals FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own withdrawals" ON referral_withdrawals FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Admins can do anything on withdrawals" ON referral_withdrawals FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- Migration (既存DBに追加する場合):
-- CREATE TABLE IF NOT EXISTS referral_withdrawals ( ... );

-- 10. Support tickets
CREATE TABLE support_tickets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  message TEXT NOT NULL,
  status TEXT DEFAULT 'open' CHECK (status IN ('open', 'replied', 'closed')),
  admin_reply TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE support_tickets ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own tickets" ON support_tickets FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own tickets" ON support_tickets FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Admins can do anything on tickets" ON support_tickets FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- Create storage bucket for ZIP deliverables
INSERT INTO storage.buckets (id, name, public) VALUES ('deliverables', 'deliverables', FALSE);

CREATE POLICY "Users can download own files" ON storage.objects FOR SELECT USING (
  bucket_id = 'deliverables' AND auth.uid()::TEXT = (storage.foldername(name))[1]
);
CREATE POLICY "Admins can upload files" ON storage.objects FOR INSERT WITH CHECK (
  bucket_id = 'deliverables' AND EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- RPC: Increment promo code usage
CREATE OR REPLACE FUNCTION increment_promo_usage(code_text TEXT)
RETURNS VOID AS $$
BEGIN
  UPDATE promo_codes SET used_count = used_count + 1 WHERE code = code_text;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ============================================================
-- LAPLACE Learning System Tables
-- ============================================================

-- Daily environment metrics (from daily_learning.py)
CREATE TABLE IF NOT EXISTS daily_metrics (
  date DATE PRIMARY KEY,
  tereko_rate NUMERIC,
  avg_duration NUMERIC,
  counter_wr NUMERIC,
  short5h_rate NUMERIC,
  best_hour INTEGER,
  worst_hour INTEGER,
  best_wr NUMERIC,
  worst_wr NUMERIC,
  total_shoes INTEGER,
  total_hands INTEGER,
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Pattern winrate database (from daily_learning.py)
CREATE TABLE IF NOT EXISTS pattern_winrates (
  pattern_hash TEXT PRIMARY KEY,
  window_size INTEGER DEFAULT 10,
  win_rate NUMERIC,
  samples INTEGER,
  last_updated DATE
);

-- Optimal parameters (updated weekly by learning batch)
CREATE TABLE IF NOT EXISTS optimal_params (
  id INTEGER PRIMARY KEY DEFAULT 1,
  entry_window INTEGER DEFAULT 15,
  entry_threshold NUMERIC DEFAULT 0.85,
  exit_drop3_limit INTEGER DEFAULT 2,
  exit_drop5_immediate BOOLEAN DEFAULT true,
  profit_target INTEGER DEFAULT 30,
  status TEXT DEFAULT 'active',
  reason TEXT,
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS optimal_params_history (
  id BIGSERIAL PRIMARY KEY,
  entry_window INTEGER DEFAULT 15,
  entry_threshold NUMERIC DEFAULT 0.85,
  exit_drop3_limit INTEGER DEFAULT 2,
  exit_drop5_immediate BOOLEAN DEFAULT true,
  profit_target INTEGER DEFAULT 30,
  status TEXT DEFAULT 'active',
  reason TEXT,
  updated_at TIMESTAMPTZ DEFAULT now()
);
