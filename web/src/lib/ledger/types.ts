// =============================================================
// FX 運用家計簿 型定義 (SPEC_FX_LEDGER.md §8.1 準拠)
// =============================================================

/** 投資家マスタ */
export interface Investor {
  id: string;
  name: string;
  email?: string;
  totalInvestment: number;
  account1Amount: number;
  account2Amount: number;
  initialChargeDisplay: number;       // 投資家画面上の初期チャージ表示額
  isActive: boolean;
  notes?: string;
}

/** 分配ルール (時系列対応) */
export interface DistributionRule {
  id: string;
  investorId: string;
  investorSharePct: number;    // 例 0.20
  jSharePct: number;
  kSharePct: number;
  companySharePct: number;
  effectiveFrom: string;        // YYYY-MM-DD
  effectiveTo?: string | null;
}

/** 別チャージ資金 (= 運用者自己資金) */
export interface ReserveFund {
  id: string;
  investorId: string;
  initialAmount: number;
  notes?: string;
}

/** 1 つめ口座 日次入力 */
export interface Account1DailyEntry {
  id?: string;
  investorId: string;
  tradeDate: string;            // YYYY-MM-DD
  dailyProfit: number;          // 入力値
  investorRecharge?: number;    // Hさんが当日 1 つめから別チャージへ自発入金した額
  notes?: string;
}

/** 1 つめ口座 計算結果 (= UI 表示用、§4.1) */
export interface Account1Computed extends Account1DailyEntry {
  investorShare: number;        // 投資家取り分 (利益 × 20%、概念上)
  jShare: number;
  kShare: number;
  companyShare: number;
  chargeRefund: number;         // = 80% 合計
  investorWithdrawal: number;   // = dailyProfit
  investorRecharge: number;     // Hさん→別チャージ 自発入金額 (default 0)
  investorNetReceived: number;  // 当日 Hさん 実受取 = dailyProfit − investorRecharge
  investorTotalAfter: number;   // 累計 (実受取累計、= Σ investorNetReceived)
  chargeBalanceAfter: number;   // 累計 chargeBalance (initial − 80%累計 + recharge累計)
}

/** 2 つめ口座 日次入力 */
export interface Account2DailyEntry {
  id?: string;
  investorId: string;
  tradeDate: string;
  dailyProfit: number;
  withdrawal: number;           // 経費出金として 2 つめから引き出した額
  notes?: string;
}

/** 2 つめ口座 計算結果 (§4.2) */
export interface Account2Computed extends Account2DailyEntry {
  netChange: number;            // = dailyProfit - withdrawal
  balanceAfter: number;         // 残高
  jShare: number;               // J 取り分 (= dailyProfit × 20%)
  kShare: number;               // K 取り分 (= dailyProfit × 30%)
  companyShare: number;         // 会社内部留保 (= dailyProfit × 50%)
}

/** 2 つめ口座 分配率 (固定) */
export const ACCOUNT2_DISTRIBUTION = {
  jSharePct: 0.20,
  kSharePct: 0.30,
  companySharePct: 0.50,
} as const;

/** 経費出金イベント */
export interface ExpenseWithdrawal {
  id?: string;
  investorId: string;
  withdrawalDate: string;
  sourceLabel?: string;
  withdrawFromReserve: number;
  withdrawFromAccount2: number;
  jReceived: number;
  kReceived: number;
  kBrotherReceived: number;
  companyReceived: number;
  aiDevExpense: number;
  notes?: string;
}

/** 経費出金 計算結果 */
export interface ExpenseWithdrawalComputed extends ExpenseWithdrawal {
  totalWithdrawal: number;      // = reserve + account2
  internalSum: number;          // = J + K + 兄 + 会社 + AI
  isBalanced: boolean;          // totalWithdrawal == internalSum か
}

/** 運用者損益サマリ (§4.4) */
export interface OperatorSummary {
  // 純利益構成
  account1_80pctTotal: number;
  account2ProfitTotal: number;
  operatorNetProfit: number;
  // 経費
  totalExpenseWithdrawal: number;
  withdrawalFromReserve: number;
  withdrawalFromProfit: number;
  // 残利益
  operatorRemainingProfit: number;
  // 利益所在内訳
  remainingInAccount2: number;
  remainingChargeRefund: number;
  locationTotal: number;
  // 別チャージ
  reserveBalance: number;
  // 経費受領累計
  jTotal: number;
  kTotal: number;
  kBrotherTotal: number;
  companyTotal: number;
  aiDevTotal: number;
}

/** 投資家サマリ */
export interface InvestorSummary {
  investorReceivedTotal: number;       // H が受け取った利益累計
  displayedChargeBalance: number;       // 画面上のチャージ資金残高
}
