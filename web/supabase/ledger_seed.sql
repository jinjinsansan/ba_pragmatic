-- =============================================================
-- FX 運用家計簿 初期データ (投資家 H + 既知 4/30〜5/6 データ)
-- =============================================================
-- ledger_schema.sql 適用後にこの SQL を実行する。
-- 仕様書 §9.1 の確定データを投入し、§9.2 の期待値が出ることを検証する。
-- =============================================================

-- 1. 投資家 H 登録
INSERT INTO ledger_investors (
  id, name, email, total_investment, account1_amount, account2_amount, initial_charge_display, is_active, notes
) VALUES (
  '00000000-0000-0000-0000-00000000000h',
  'H',
  'shojihashimoto0922@gmail.com',
  96900.00,
  50000.00,
  46900.00,
  49000.00,    -- = 2つめ (46,900) + 別チャージ (2,100)
  TRUE,
  '初期投資家、user02 (橋本) と紐付け'
)
ON CONFLICT (id) DO UPDATE SET
  name = EXCLUDED.name,
  email = EXCLUDED.email,
  total_investment = EXCLUDED.total_investment,
  account1_amount = EXCLUDED.account1_amount,
  account2_amount = EXCLUDED.account2_amount,
  initial_charge_display = EXCLUDED.initial_charge_display;

-- 2. 分配ルール (H さん向け、20/20/30/30、2026-04-30 以降有効)
INSERT INTO ledger_distribution_rules (
  investor_id, investor_share_pct, j_share_pct, k_share_pct, company_share_pct, effective_from, effective_to, notes
) VALUES (
  '00000000-0000-0000-0000-00000000000h',
  0.2000, 0.2000, 0.3000, 0.3000,
  '2026-04-30', NULL,
  'H さん向け初期分配ルール'
)
ON CONFLICT DO NOTHING;

-- 3. 別チャージ資金 ($2,100、運用者の自己資金、H さんにはチャージとして見せる)
INSERT INTO ledger_reserve_funds (
  investor_id, initial_amount, notes
) VALUES (
  '00000000-0000-0000-0000-00000000000h',
  2100.00,
  '運用者自己資金、H さん画面では「チャージ資金」として見せている'
)
ON CONFLICT (investor_id) DO UPDATE SET
  initial_amount = EXCLUDED.initial_amount;

-- 4. 1 つめ口座 日次利益 (4/30 〜 5/6)
INSERT INTO ledger_account1_daily (investor_id, trade_date, daily_profit, notes) VALUES
  ('00000000-0000-0000-0000-00000000000h', '2026-04-30',  933.00, '初日'),
  ('00000000-0000-0000-0000-00000000000h', '2026-05-01',    0.00, '利益なし'),
  ('00000000-0000-0000-0000-00000000000h', '2026-05-02', 2049.00, NULL),
  ('00000000-0000-0000-0000-00000000000h', '2026-05-03',  786.00, NULL),
  ('00000000-0000-0000-0000-00000000000h', '2026-05-04', 3990.00, NULL),
  ('00000000-0000-0000-0000-00000000000h', '2026-05-05', 2143.00, NULL),
  ('00000000-0000-0000-0000-00000000000h', '2026-05-06', 2019.00, NULL)
ON CONFLICT (investor_id, trade_date) DO UPDATE SET
  daily_profit = EXCLUDED.daily_profit;

-- 5. 2 つめ口座 (4/30 〜 5/5 の日次内訳は不明なので 5/6 にまとめて入力)
INSERT INTO ledger_account2_daily (investor_id, trade_date, daily_profit, withdrawal, notes) VALUES
  ('00000000-0000-0000-0000-00000000000h', '2026-05-06', 9019.00, 5919.00, '4/30〜5/5 の日次内訳が未取得のため 5/6 にまとめて入力。2 つめからの経費出金 $5,919 もここに含む')
ON CONFLICT (investor_id, trade_date) DO UPDATE SET
  daily_profit = EXCLUDED.daily_profit,
  withdrawal = EXCLUDED.withdrawal,
  notes = EXCLUDED.notes;

-- 6. 経費出金 (5/6)
INSERT INTO ledger_expense_withdrawals (
  id,
  investor_id, withdrawal_date, source_label,
  withdraw_from_reserve, withdraw_from_account2,
  j_received, k_received, k_brother_received, company_received, ai_dev_expense,
  notes
) VALUES (
  '00000000-0000-0000-0000-00000000ex01',
  '00000000-0000-0000-0000-00000000000h', '2026-05-06', '別+2つめ',
  2100.00, 5919.00,
  2100.00,  -- J 取り分
  2000.00,  -- K
  919.00,   -- Kの兄
  2000.00,  -- 会社配当
  1000.00,  -- AI開発費等 (運用益外例外)
  '5/6 の経費出金。別チャージから $2,100、2 つめ口座から $5,919、計 $8,019。J 取り分 $2,100 / K $2,000 / Kの兄 $919 / 会社配当 $2,000 / AI開発費 $1,000 (運用益外例外)'
)
ON CONFLICT (id) DO UPDATE SET
  withdraw_from_reserve = EXCLUDED.withdraw_from_reserve,
  withdraw_from_account2 = EXCLUDED.withdraw_from_account2,
  j_received = EXCLUDED.j_received,
  k_received = EXCLUDED.k_received,
  k_brother_received = EXCLUDED.k_brother_received,
  company_received = EXCLUDED.company_received,
  ai_dev_expense = EXCLUDED.ai_dev_expense;

-- =============================================================
-- 検証 SELECT (= 仕様書 §9.2 の期待値 17 項目と一致するか確認)
-- =============================================================
-- 期待値:
--   investor_received_total       = $2,384.00
--   displayed_charge_balance      = $39,464.00
--   operator_net_profit           = $18,555.00
--   expense_from_account2 (= withdrawal_from_profit) = $5,919.00
--   operator_remaining_profit     = $12,636.00
--   remaining_in_account2         = $3,100.00
--   remaining_charge_refund       = $9,536.00
--   account2_balance              = $50,000.00
--   reserve_balance               = $0.00
--   j_total                       = $2,100.00 (取り分のみ。AI 開発費は別計上)
--   k_total                       = $2,000.00
--   k_brother_total               = $919.00
--   company_total                 = $2,000.00
--   ai_dev_total                  = $1,000.00 (運用益外例外)
--   expense_total                 = $8,019.00
--   j_share_in_account1           = $2,384.00 (1つめ口座 J 累計取り分、未出金)
--   k_share_in_account1           = $3,576.00 (1つめ口座 K 累計取り分、未出金)
--   company_share_in_account1     = $3,576.00 (1つめ口座 会社 累計取り分、未出金)
SELECT * FROM ledger_investor_summary WHERE investor_id = '00000000-0000-0000-0000-00000000000h';
