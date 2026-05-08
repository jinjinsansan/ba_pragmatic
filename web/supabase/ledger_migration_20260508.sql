-- =============================================================
-- 2026-05-08 ledger 拡張
-- (1) ledger_company_expense_breakdown 新規テーブル
--     会社累計 (= K の兄 + 元会社配当 + AI 開発費 統合) の支出内訳を記録
--     合計が常に会社累計と一致するべき
-- (2) view 更新:
--     - company_total_merged = company_total + k_brother_total + ai_dev_total
--     - K 未出金 = K 累計取り分 - K 既出金 (Kの兄 を引かない)
--     - 会社 未出金 = 会社 累計取り分 - 会社累計 (= K兄 + 会社 + AI)
-- (3) 5/6 初期 seed (= 3 件で合計 $3,919)
-- =============================================================
-- 適用方法: Supabase SQL Editor でこの SQL 全体を貼り付けて Run
-- =============================================================

-- (1) 新テーブル
CREATE TABLE IF NOT EXISTS ledger_company_expense_breakdown (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  investor_id UUID NOT NULL REFERENCES ledger_investors(id) ON DELETE CASCADE,
  expense_date DATE NOT NULL,
  category TEXT NOT NULL,                  -- 自由入力 (例: 給与/賃料/設備/AI開発/税金/その他)
  recipient TEXT,                          -- 受取人名 (任意, 例: K の兄, 外部開発者)
  amount NUMERIC(14,2) NOT NULL,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lceb_investor_date
  ON ledger_company_expense_breakdown(investor_id, expense_date DESC);

-- updated_at trigger
CREATE OR REPLACE FUNCTION update_lceb_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_lceb_updated_at ON ledger_company_expense_breakdown;
CREATE TRIGGER trg_lceb_updated_at
BEFORE UPDATE ON ledger_company_expense_breakdown
FOR EACH ROW EXECUTE FUNCTION update_lceb_updated_at();

-- RLS (admin only, same pattern as other ledger tables)
ALTER TABLE ledger_company_expense_breakdown ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lceb_admin_all ON ledger_company_expense_breakdown;
CREATE POLICY lceb_admin_all ON ledger_company_expense_breakdown
  FOR ALL USING (
    EXISTS (SELECT 1 FROM profiles WHERE profiles.id = auth.uid() AND profiles.is_admin = TRUE)
  );

-- (2) View 更新
CREATE OR REPLACE VIEW ledger_investor_summary AS
WITH a1 AS (
  SELECT investor_id,
         SUM(daily_profit) AS account1_total_profit,
         SUM(daily_profit * 0.20) AS investor_received_total,
         SUM(daily_profit * 0.20) AS j_share_total,
         SUM(daily_profit * 0.30) AS k_share_total,
         SUM(daily_profit * 0.30) AS company_share_total,
         SUM(daily_profit * 0.80) AS account1_80pct_total,
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
         SUM(ai_dev_expense) AS ai_dev_total,
         -- 会社累計 = 旧会社配当 + Kの兄 + AI 開発費 (= 全て会社経費扱い)
         SUM(company_received) + SUM(k_brother_received) + SUM(ai_dev_expense) AS company_total_merged
    FROM ledger_expense_withdrawals
   GROUP BY investor_id
),
breakdown AS (
  SELECT investor_id,
         SUM(amount) AS company_breakdown_total
    FROM ledger_company_expense_breakdown
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
  COALESCE(a1.account1_total_profit, 0) AS account1_total_profit,
  COALESCE(a1.investor_received_total, 0) AS investor_received_total,
  COALESCE(a1.account1_80pct_total, 0) AS account1_80pct_total,
  COALESCE(a2.account2_total_profit, 0) AS account2_total_profit,
  COALESCE(a2.account2_withdrawal_total, 0) AS account2_withdrawal_total,
  i.account2_amount + COALESCE(a2.account2_total_profit, 0) - COALESCE(a2.account2_withdrawal_total, 0) AS account2_balance,
  i.initial_charge_display - COALESCE(a1.account1_80pct_total, 0) AS displayed_charge_balance,
  COALESCE(a1.account1_80pct_total, 0) + COALESCE(a2.account2_total_profit, 0) AS operator_net_profit,
  COALESCE(exp.expense_total, 0) AS expense_total,
  COALESCE(exp.expense_from_reserve, 0) AS expense_from_reserve,
  COALESCE(exp.expense_from_account2, 0) AS expense_from_account2,
  (COALESCE(a1.account1_80pct_total, 0) + COALESCE(a2.account2_total_profit, 0))
    - COALESCE(exp.expense_from_account2, 0) AS operator_remaining_profit,
  COALESCE(a2.account2_total_profit, 0) - COALESCE(a2.account2_withdrawal_total, 0) AS remaining_in_account2,
  COALESCE(a1.account1_80pct_total, 0) AS remaining_charge_refund,
  COALESCE(rf.initial_amount, 0) - COALESCE(exp.expense_from_reserve, 0) AS reserve_balance,
  -- 既存のカラム (個別、過去互換のため残す)
  COALESCE(exp.j_total, 0) AS j_total,
  COALESCE(exp.k_total, 0) AS k_total,
  COALESCE(exp.k_brother_total, 0) AS k_brother_total,
  COALESCE(exp.company_total, 0) AS company_total,
  COALESCE(exp.ai_dev_total, 0) AS ai_dev_total,
  -- 会社累計 (= K兄 + 旧会社 + AI、新カテゴリ)
  COALESCE(exp.company_total_merged, 0) AS company_total_merged,
  -- 1つめ口座 累計取り分
  COALESCE(a1.j_share_total, 0) AS j_share_in_account1,
  COALESCE(a1.k_share_total, 0) AS k_share_in_account1,
  COALESCE(a1.company_share_total, 0) AS company_share_in_account1,
  -- 1つめ口座 未出金 (新カテゴリ準拠)
  COALESCE(a1.j_share_total, 0) - COALESCE(exp.j_total, 0) AS j_unpaid_in_account1,
  -- K 未出金: Kの兄を引かない (= 兄 は会社経費扱い)
  COALESCE(a1.k_share_total, 0) - COALESCE(exp.k_total, 0) AS k_unpaid_in_account1,
  -- 会社 未出金: 会社累計 (= K兄 + 旧会社 + AI) を引く
  COALESCE(a1.company_share_total, 0) - COALESCE(exp.company_total_merged, 0) AS company_unpaid_in_account1,
  -- 会社経費内訳 関連
  COALESCE(breakdown.company_breakdown_total, 0) AS company_breakdown_total,
  COALESCE(exp.company_total_merged, 0) - COALESCE(breakdown.company_breakdown_total, 0) AS company_breakdown_remaining
FROM ledger_investors i
LEFT JOIN ledger_reserve_funds rf ON rf.investor_id = i.id
LEFT JOIN a1 ON a1.investor_id = i.id
LEFT JOIN a2 ON a2.investor_id = i.id
LEFT JOIN exp ON exp.investor_id = i.id
LEFT JOIN breakdown ON breakdown.investor_id = i.id;

-- (3) 5/6 初期 seed (= K兄 + 会社配当 + AI 開発費 を内訳エントリ化)
INSERT INTO ledger_company_expense_breakdown (investor_id, expense_date, category, recipient, amount, notes)
SELECT
  '00000000-0000-0000-0000-000000000001'::uuid,
  '2026-05-06'::date,
  '報酬',
  'K の兄',
  919.00,
  '5/6 経費出金 ledger_expense_withdrawals.k_brother_received より自動投入 (migration_20260508)'
WHERE NOT EXISTS (
  SELECT 1 FROM ledger_company_expense_breakdown
   WHERE investor_id = '00000000-0000-0000-0000-000000000001'::uuid
     AND expense_date = '2026-05-06'::date
     AND recipient = 'K の兄'
);

INSERT INTO ledger_company_expense_breakdown (investor_id, expense_date, category, recipient, amount, notes)
SELECT
  '00000000-0000-0000-0000-000000000001'::uuid,
  '2026-05-06'::date,
  '会社配当',
  '会社',
  2000.00,
  '5/6 経費出金 ledger_expense_withdrawals.company_received より自動投入 (migration_20260508)'
WHERE NOT EXISTS (
  SELECT 1 FROM ledger_company_expense_breakdown
   WHERE investor_id = '00000000-0000-0000-0000-000000000001'::uuid
     AND expense_date = '2026-05-06'::date
     AND category = '会社配当'
);

INSERT INTO ledger_company_expense_breakdown (investor_id, expense_date, category, recipient, amount, notes)
SELECT
  '00000000-0000-0000-0000-000000000001'::uuid,
  '2026-05-06'::date,
  'AI 開発費',
  '外部開発者',
  1000.00,
  '5/6 経費出金 ledger_expense_withdrawals.ai_dev_expense より自動投入 (migration_20260508)'
WHERE NOT EXISTS (
  SELECT 1 FROM ledger_company_expense_breakdown
   WHERE investor_id = '00000000-0000-0000-0000-000000000001'::uuid
     AND expense_date = '2026-05-06'::date
     AND category = 'AI 開発費'
);

-- 確認
SELECT
  investor_name,
  j_total                   AS "J受取",
  k_total                   AS "K受取",
  company_total_merged      AS "会社累計",
  expense_total             AS "出金合計",
  j_unpaid_in_account1      AS "J未出金",
  k_unpaid_in_account1      AS "K未出金(新)",
  company_unpaid_in_account1 AS "会社未出金(新)",
  company_breakdown_total   AS "内訳合計",
  company_breakdown_remaining AS "残額(=0なら一致)"
FROM ledger_investor_summary
WHERE investor_name = 'H';
