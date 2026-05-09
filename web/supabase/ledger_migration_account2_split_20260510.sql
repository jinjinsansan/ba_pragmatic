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
         SUM(withdrawal)   AS account2_withdrawal_total,
         SUM(daily_profit * 0.20) AS j_share_a2,
         SUM(daily_profit * 0.30) AS k_share_a2,
         SUM(daily_profit * 0.50) AS company_share_a2
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
  COALESCE(exp.j_total, 0) AS j_total,
  COALESCE(exp.k_total, 0) AS k_total,
  COALESCE(exp.k_brother_total, 0) AS k_brother_total,
  COALESCE(exp.company_total, 0) AS company_total,
  COALESCE(exp.ai_dev_total, 0) AS ai_dev_total,
  COALESCE(exp.company_total_merged, 0) AS company_total_merged,
  COALESCE(a1.j_share_total, 0) AS j_share_in_account1,
  COALESCE(a1.k_share_total, 0) AS k_share_in_account1,
  COALESCE(a1.company_share_total, 0) AS company_share_in_account1,
  COALESCE(a1.j_share_total, 0) - COALESCE(exp.j_total, 0) AS j_unpaid_in_account1,
  COALESCE(a1.k_share_total, 0) - COALESCE(exp.k_total, 0) AS k_unpaid_in_account1,
  COALESCE(a1.company_share_total, 0) - COALESCE(exp.company_total_merged, 0) AS company_unpaid_in_account1,
  COALESCE(breakdown.company_breakdown_total, 0) AS company_breakdown_total,
  COALESCE(exp.company_total_merged, 0) - COALESCE(breakdown.company_breakdown_total, 0) AS company_breakdown_remaining,
  COALESCE(a2.j_share_a2, 0) AS j_share_in_account2,
  COALESCE(a2.k_share_a2, 0) AS k_share_in_account2,
  COALESCE(a2.company_share_a2, 0) AS company_share_in_account2,
  COALESCE(a1.j_share_total, 0) + COALESCE(a2.j_share_a2, 0) AS j_share_total_pool,
  COALESCE(a1.k_share_total, 0) + COALESCE(a2.k_share_a2, 0) AS k_share_total_pool,
  COALESCE(a1.company_share_total, 0) + COALESCE(a2.company_share_a2, 0) AS company_share_total_pool,
  COALESCE(a1.j_share_total, 0) + COALESCE(a2.j_share_a2, 0) - COALESCE(exp.j_total, 0) AS j_unpaid_total,
  COALESCE(a1.k_share_total, 0) + COALESCE(a2.k_share_a2, 0) - COALESCE(exp.k_total, 0) AS k_unpaid_total,
  COALESCE(a1.company_share_total, 0) + COALESCE(a2.company_share_a2, 0) - COALESCE(exp.company_total_merged, 0) AS company_unpaid_total
FROM ledger_investors i
LEFT JOIN ledger_reserve_funds rf ON rf.investor_id = i.id
LEFT JOIN a1 ON a1.investor_id = i.id
LEFT JOIN a2 ON a2.investor_id = i.id
LEFT JOIN exp ON exp.investor_id = i.id
LEFT JOIN breakdown ON breakdown.investor_id = i.id;

SELECT
  investor_name,
  account1_total_profit,
  account2_total_profit,
  j_share_in_account1,
  j_share_in_account2,
  j_share_total_pool,
  j_total,
  j_unpaid_total,
  k_share_in_account1,
  k_share_in_account2,
  k_share_total_pool,
  k_total,
  k_unpaid_total,
  company_share_in_account1,
  company_share_in_account2,
  company_share_total_pool,
  company_total_merged,
  company_unpaid_total
FROM ledger_investor_summary
WHERE investor_name = 'H';
