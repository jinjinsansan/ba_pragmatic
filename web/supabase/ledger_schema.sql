-- =============================================================
-- FX 運用家計簿システム スキーマ (SPEC_FX_LEDGER.md §5.1 準拠)
-- =============================================================
-- 投資家・運用者・会社の 3 階層の資金フローを管理。
-- 既存 deductions/billing テーブルとは独立して、より細かい家計簿として運用。
-- 実装の最重要原則: 仕様書 §9.2 の期待値 17 項目に 1 セント単位で一致すること。
-- =============================================================

-- 1. 投資家マスタ
CREATE TABLE IF NOT EXISTS ledger_investors (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  email TEXT,                                        -- 任意 (auth.users との紐付けに使用可能)
  total_investment DECIMAL(14,2) NOT NULL,           -- 投資総額
  account1_amount DECIMAL(14,2) NOT NULL,            -- 1 つめ口座への投資額
  account2_amount DECIMAL(14,2) NOT NULL,            -- 2 つめ口座への投資額
  initial_charge_display DECIMAL(14,2) NOT NULL,     -- 投資家画面上の初期チャージ表示額 (= 2つめ + 別チャージ)
  is_active BOOLEAN DEFAULT TRUE,                    -- 投資家がアクティブか
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ledger_investors_email ON ledger_investors(email);

-- 2. 分配ルール (投資家ごと、履歴管理あり)
CREATE TABLE IF NOT EXISTS ledger_distribution_rules (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  investor_id UUID NOT NULL REFERENCES ledger_investors(id) ON DELETE CASCADE,
  investor_share_pct DECIMAL(6,4) NOT NULL DEFAULT 0.2000,
  j_share_pct DECIMAL(6,4) NOT NULL DEFAULT 0.2000,
  k_share_pct DECIMAL(6,4) NOT NULL DEFAULT 0.3000,
  company_share_pct DECIMAL(6,4) NOT NULL DEFAULT 0.3000,
  effective_from DATE NOT NULL,                      -- このルールが効力を持つ開始日
  effective_to DATE,                                 -- 終了日 (NULL = 現在も有効)
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  -- 4 比率の合計が 1.0 となることをチェック
  CHECK (ABS(investor_share_pct + j_share_pct + k_share_pct + company_share_pct - 1.0) < 0.0001)
);

CREATE INDEX IF NOT EXISTS idx_ledger_dist_rules_investor ON ledger_distribution_rules(investor_id, effective_from);

-- 3. 別チャージ資金 (運用者の自己資金、投資家にはチャージ資金の一部として見せる)
CREATE TABLE IF NOT EXISTS ledger_reserve_funds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  investor_id UUID NOT NULL REFERENCES ledger_investors(id) ON DELETE CASCADE,
  initial_amount DECIMAL(14,2) NOT NULL,             -- 別チャージ資金の初期額
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(investor_id)                                -- 投資家ごとに 1 つ
);

-- 4. 1 つめ口座 日次利益
CREATE TABLE IF NOT EXISTS ledger_account1_daily (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  investor_id UUID NOT NULL REFERENCES ledger_investors(id) ON DELETE CASCADE,
  trade_date DATE NOT NULL,
  daily_profit DECIMAL(14,2) NOT NULL,               -- 入力値 (その日の 1 つめ口座利益)
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(investor_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_ledger_acc1_investor_date ON ledger_account1_daily(investor_id, trade_date);

-- 5. 2 つめ口座 日次記録
CREATE TABLE IF NOT EXISTS ledger_account2_daily (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  investor_id UUID NOT NULL REFERENCES ledger_investors(id) ON DELETE CASCADE,
  trade_date DATE NOT NULL,
  daily_profit DECIMAL(14,2) NOT NULL DEFAULT 0,     -- その日の 2 つめ口座利益
  withdrawal DECIMAL(14,2) NOT NULL DEFAULT 0,       -- 経費出金として 2 つめから引き出した額
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(investor_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_ledger_acc2_investor_date ON ledger_account2_daily(investor_id, trade_date);

-- 6. 経費出金イベント (不定期、各受領者への配分内訳付き)
CREATE TABLE IF NOT EXISTS ledger_expense_withdrawals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  investor_id UUID NOT NULL REFERENCES ledger_investors(id) ON DELETE CASCADE,
  withdrawal_date DATE NOT NULL,
  source_label TEXT,                                 -- '別+2つめ' などの表示用
  withdraw_from_reserve DECIMAL(14,2) NOT NULL DEFAULT 0,
  withdraw_from_account2 DECIMAL(14,2) NOT NULL DEFAULT 0,
  total_withdrawal DECIMAL(14,2) GENERATED ALWAYS AS (withdraw_from_reserve + withdraw_from_account2) STORED,
  -- 内訳 (受領者別)
  j_received DECIMAL(14,2) NOT NULL DEFAULT 0,
  k_received DECIMAL(14,2) NOT NULL DEFAULT 0,
  k_brother_received DECIMAL(14,2) NOT NULL DEFAULT 0,
  company_received DECIMAL(14,2) NOT NULL DEFAULT 0,
  ai_dev_expense DECIMAL(14,2) NOT NULL DEFAULT 0,
  internal_sum DECIMAL(14,2) GENERATED ALWAYS AS (
    j_received + k_received + k_brother_received + company_received + ai_dev_expense
  ) STORED,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  -- 内訳合計と出金合計が一致することを保証
  CHECK (
    ABS(
      (j_received + k_received + k_brother_received + company_received + ai_dev_expense)
      - (withdraw_from_reserve + withdraw_from_account2)
    ) < 0.01
  )
);

CREATE INDEX IF NOT EXISTS idx_ledger_expense_investor_date ON ledger_expense_withdrawals(investor_id, withdrawal_date);

-- =============================================================
-- 計算 View: 投資家・運用者の現状サマリ
-- =============================================================
-- これを SELECT するだけで、仕様書 §9.2 期待値の大半が取れる。
CREATE OR REPLACE VIEW ledger_investor_summary AS
WITH a1 AS (
  SELECT investor_id,
         SUM(daily_profit) AS account1_total_profit,
         SUM(daily_profit * 0.20) AS investor_received_total,
         SUM(daily_profit * 0.20) AS j_share_total,
         SUM(daily_profit * 0.30) AS k_share_total,
         SUM(daily_profit * 0.30) AS company_share_total,
         SUM(daily_profit * 0.80) AS account1_80pct_total,    -- = chargeRefund 累計
         SUM(daily_profit) AS investor_withdrawal_total
    FROM ledger_account1_daily
   GROUP BY investor_id
),
a2 AS (
  SELECT investor_id,
         SUM(daily_profit) AS account2_total_profit,
         SUM(withdrawal)   AS account2_withdrawal_total
    FROM ledger_account2_daily
   GROUP BY investor_id
),
exp AS (
  SELECT investor_id,
         SUM(total_withdrawal) AS expense_total,
         SUM(withdraw_from_reserve) AS expense_from_reserve,
         SUM(withdraw_from_account2) AS expense_from_account2,
         SUM(j_received) AS j_total,
         SUM(k_received) AS k_total,
         SUM(k_brother_received) AS k_brother_total,
         SUM(company_received) AS company_total,
         SUM(ai_dev_expense) AS ai_dev_total
    FROM ledger_expense_withdrawals
   GROUP BY investor_id
)
SELECT
  i.id AS investor_id,
  i.name AS investor_name,
  i.total_investment,
  i.account1_amount,
  i.account2_amount,
  i.initial_charge_display,
  rf.initial_amount AS reserve_initial,
  -- 1 つめ口座
  COALESCE(a1.account1_total_profit, 0) AS account1_total_profit,
  COALESCE(a1.investor_received_total, 0) AS investor_received_total,
  COALESCE(a1.account1_80pct_total, 0) AS account1_80pct_total,
  -- 2 つめ口座
  COALESCE(a2.account2_total_profit, 0) AS account2_total_profit,
  COALESCE(a2.account2_withdrawal_total, 0) AS account2_withdrawal_total,
  i.account2_amount + COALESCE(a2.account2_total_profit, 0) - COALESCE(a2.account2_withdrawal_total, 0) AS account2_balance,
  -- 投資家画面上のチャージ資金残高 = 初期 - chargeRefund 累計
  i.initial_charge_display - COALESCE(a1.account1_80pct_total, 0) AS displayed_charge_balance,
  -- 運用者損益
  COALESCE(a1.account1_80pct_total, 0) + COALESCE(a2.account2_total_profit, 0) AS operator_net_profit,
  -- 経費
  COALESCE(exp.expense_total, 0) AS expense_total,
  COALESCE(exp.expense_from_reserve, 0) AS expense_from_reserve,
  COALESCE(exp.expense_from_account2, 0) AS expense_from_account2,
  -- 残利益 (利益から出金した分のみ引く、別チャージは元々自己資金)
  (COALESCE(a1.account1_80pct_total, 0) + COALESCE(a2.account2_total_profit, 0))
    - COALESCE(exp.expense_from_account2, 0) AS operator_remaining_profit,
  -- 利益所在内訳
  COALESCE(a2.account2_total_profit, 0) - COALESCE(a2.account2_withdrawal_total, 0) AS remaining_in_account2,
  COALESCE(a1.account1_80pct_total, 0) AS remaining_charge_refund,
  -- 別チャージ残高
  COALESCE(rf.initial_amount, 0) - COALESCE(exp.expense_from_reserve, 0) AS reserve_balance,
  -- 経費受領累計
  COALESCE(exp.j_total, 0) AS j_total,
  COALESCE(exp.k_total, 0) AS k_total,
  COALESCE(exp.k_brother_total, 0) AS k_brother_total,
  COALESCE(exp.company_total, 0) AS company_total,
  COALESCE(exp.ai_dev_total, 0) AS ai_dev_total,
  -- 1 つめ口座 累計取り分 (1 つめ口座の利益 × 配分率)
  COALESCE(a1.j_share_total, 0) AS j_share_in_account1,
  COALESCE(a1.k_share_total, 0) AS k_share_in_account1,
  COALESCE(a1.company_share_total, 0) AS company_share_in_account1,
  -- 1 つめ口座のみで計算した 未出金取り分 (= 累計取り分 - 既出金)
  -- (経費出金は物理は account2/reserve から出るが、概念上 1 つめ取り分から差し引く)
  -- (AI 開発費は運用益外例外で 1 つめ取り分とは無関係なので、ここでは差し引かない)
  COALESCE(a1.j_share_total, 0) - COALESCE(exp.j_total, 0) AS j_unpaid_in_account1,
  COALESCE(a1.k_share_total, 0) - COALESCE(exp.k_total, 0) - COALESCE(exp.k_brother_total, 0) AS k_unpaid_in_account1,
  COALESCE(a1.company_share_total, 0) - COALESCE(exp.company_total, 0) AS company_unpaid_in_account1
FROM ledger_investors i
LEFT JOIN ledger_reserve_funds rf ON rf.investor_id = i.id
LEFT JOIN a1 ON a1.investor_id = i.id
LEFT JOIN a2 ON a2.investor_id = i.id
LEFT JOIN exp ON exp.investor_id = i.id;

-- =============================================================
-- RLS (Row Level Security): 運用者 (= is_admin) のみ全データ閲覧可
-- =============================================================
ALTER TABLE ledger_investors ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_distribution_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_reserve_funds ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_account1_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_account2_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_expense_withdrawals ENABLE ROW LEVEL SECURITY;

-- 管理者は全テーブル全操作可能
CREATE POLICY "ledger_investors_admin_all" ON ledger_investors FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);
CREATE POLICY "ledger_dist_rules_admin_all" ON ledger_distribution_rules FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);
CREATE POLICY "ledger_reserve_funds_admin_all" ON ledger_reserve_funds FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);
CREATE POLICY "ledger_acc1_admin_all" ON ledger_account1_daily FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);
CREATE POLICY "ledger_acc2_admin_all" ON ledger_account2_daily FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);
CREATE POLICY "ledger_expense_admin_all" ON ledger_expense_withdrawals FOR ALL USING (
  EXISTS (SELECT 1 FROM profiles WHERE id = auth.uid() AND is_admin = TRUE)
);

-- 投資家自身は自分のデータのみ閲覧可能 (= 投資家ログインモード対応)
-- 1 つめ口座と initial_charge_display, displayed_charge_balance のみ表示
-- 注: ledger_investors.email == auth.users.email で紐付け
CREATE POLICY "ledger_investors_self_select" ON ledger_investors FOR SELECT USING (
  email = (SELECT email FROM auth.users WHERE id = auth.uid())
);
CREATE POLICY "ledger_acc1_self_select" ON ledger_account1_daily FOR SELECT USING (
  EXISTS (
    SELECT 1 FROM ledger_investors WHERE id = investor_id
      AND email = (SELECT email FROM auth.users WHERE id = auth.uid())
  )
);
-- 注: 2 つめ口座 / 別チャージ / 経費出金は投資家には見せない (RLS で SELECT 不可、INSERT/UPDATE 不可)

-- =============================================================
-- updated_at 自動更新トリガ
-- =============================================================
CREATE OR REPLACE FUNCTION ledger_set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ledger_investors_updated BEFORE UPDATE ON ledger_investors
  FOR EACH ROW EXECUTE FUNCTION ledger_set_updated_at();
CREATE TRIGGER trg_ledger_acc1_updated BEFORE UPDATE ON ledger_account1_daily
  FOR EACH ROW EXECUTE FUNCTION ledger_set_updated_at();
CREATE TRIGGER trg_ledger_acc2_updated BEFORE UPDATE ON ledger_account2_daily
  FOR EACH ROW EXECUTE FUNCTION ledger_set_updated_at();
CREATE TRIGGER trg_ledger_expense_updated BEFORE UPDATE ON ledger_expense_withdrawals
  FOR EACH ROW EXECUTE FUNCTION ledger_set_updated_at();

-- END OF LEDGER SCHEMA
