// =============================================================
// FX 運用家計簿 計算ロジック (SPEC_FX_LEDGER.md §4 / §8 準拠)
// =============================================================
// 浮動小数誤差を避けるため内部はセント単位 (整数) で計算する。
// 仕様書 §11.1 「金額計算は必ず decimal.js または整数で扱うこと」を遵守。
// =============================================================

import type {
  Account1DailyEntry,
  Account1Computed,
  Account2DailyEntry,
  Account2Computed,
  ExpenseWithdrawal,
  ExpenseWithdrawalComputed,
  DistributionRule,
  OperatorSummary,
  InvestorSummary,
} from './types';

// ---- 金額ユーティリティ (内部はセント単位 = 整数) ----
const toC = (usd: number): number => Math.round(usd * 100);   // USD → cents
const fromC = (cents: number): number => cents / 100;          // cents → USD
const sumC = (xs: number[]): number => xs.reduce((a, b) => a + Math.round(b * 100), 0);

/**
 * 1 つめ口座 日次計算 (§4.1)
 * 投資家の取り分・運用者各人の取り分・チャージ返金・累計を計算
 */
export function computeAccount1Daily(
  entries: Account1DailyEntry[],
  rule: DistributionRule,
  initialChargeBalance: number,
): Account1Computed[] {
  let runningInvestorTotalC = 0;
  let runningChargeBalanceC = toC(initialChargeBalance);

  const sorted = [...entries].sort((a, b) => a.tradeDate.localeCompare(b.tradeDate));

  return sorted.map((e) => {
    const profitC = toC(e.dailyProfit);
    const investorShareC = Math.round(profitC * rule.investorSharePct);
    const jShareC = Math.round(profitC * rule.jSharePct);
    const kShareC = Math.round(profitC * rule.kSharePct);
    const companyShareC = Math.round(profitC * rule.companySharePct);
    const chargeRefundC = jShareC + kShareC + companyShareC;

    runningInvestorTotalC += investorShareC;
    runningChargeBalanceC -= chargeRefundC;

    return {
      ...e,
      investorShare: fromC(investorShareC),
      jShare: fromC(jShareC),
      kShare: fromC(kShareC),
      companyShare: fromC(companyShareC),
      chargeRefund: fromC(chargeRefundC),
      investorWithdrawal: e.dailyProfit,
      investorTotalAfter: fromC(runningInvestorTotalC),
      chargeBalanceAfter: fromC(runningChargeBalanceC),
    };
  });
}

/**
 * 2 つめ口座 日次計算 (§4.2)
 */
export function computeAccount2Daily(
  entries: Account2DailyEntry[],
  initialBalance: number,
): Account2Computed[] {
  let balanceC = toC(initialBalance);
  const sorted = [...entries].sort((a, b) => a.tradeDate.localeCompare(b.tradeDate));

  return sorted.map((e) => {
    const profitC = toC(e.dailyProfit);
    const withdrawC = toC(e.withdrawal);
    const netChangeC = profitC - withdrawC;
    balanceC += netChangeC;

    return {
      ...e,
      netChange: fromC(netChangeC),
      balanceAfter: fromC(balanceC),
    };
  });
}

/**
 * 経費出金計算 (§4.3)
 */
export function computeExpenseWithdrawal(
  e: ExpenseWithdrawal,
): ExpenseWithdrawalComputed {
  const reserveC = toC(e.withdrawFromReserve);
  const acc2C = toC(e.withdrawFromAccount2);
  const totalC = reserveC + acc2C;

  const internalC =
    toC(e.jReceived) +
    toC(e.kReceived) +
    toC(e.kBrotherReceived) +
    toC(e.companyReceived) +
    toC(e.aiDevExpense);

  return {
    ...e,
    totalWithdrawal: fromC(totalC),
    internalSum: fromC(internalC),
    isBalanced: Math.abs(totalC - internalC) < 1,    // 1 cent 以内なら一致扱い
  };
}

/**
 * 運用者損益サマリ計算 (§4.4)
 */
export function computeOperatorSummary(args: {
  account1Computed: Account1Computed[];
  account2Entries: Account2DailyEntry[];
  expenseEntries: ExpenseWithdrawal[];
  account2Initial: number;
  reserveInitial: number;
}): OperatorSummary {
  const account1_80pctC = args.account1Computed.reduce(
    (a, e) => a + toC(e.chargeRefund), 0
  );
  const account2ProfitC = args.account2Entries.reduce(
    (a, e) => a + toC(e.dailyProfit), 0
  );
  const account2WithdrawC = args.account2Entries.reduce(
    (a, e) => a + toC(e.withdrawal), 0
  );

  const operatorNetProfitC = account1_80pctC + account2ProfitC;

  const totalExpenseC = args.expenseEntries.reduce(
    (a, e) => a + toC(e.withdrawFromReserve) + toC(e.withdrawFromAccount2), 0
  );
  const fromReserveC = args.expenseEntries.reduce((a, e) => a + toC(e.withdrawFromReserve), 0);
  const fromProfitC = args.expenseEntries.reduce((a, e) => a + toC(e.withdrawFromAccount2), 0);

  const operatorRemainingC = operatorNetProfitC - fromProfitC;

  const account2BalanceC = toC(args.account2Initial) + account2ProfitC - account2WithdrawC;
  const remainingInAcc2C = account2BalanceC - toC(args.account2Initial);
  const remainingChargeRefundC = account1_80pctC;
  const locationTotalC = remainingInAcc2C + remainingChargeRefundC;

  const reserveBalanceC = toC(args.reserveInitial) - fromReserveC;

  const jTotalC = args.expenseEntries.reduce((a, e) => a + toC(e.jReceived), 0);
  const kTotalC = args.expenseEntries.reduce((a, e) => a + toC(e.kReceived), 0);
  const kBrotherTotalC = args.expenseEntries.reduce((a, e) => a + toC(e.kBrotherReceived), 0);
  const companyTotalC = args.expenseEntries.reduce((a, e) => a + toC(e.companyReceived), 0);
  const aiDevTotalC = args.expenseEntries.reduce((a, e) => a + toC(e.aiDevExpense), 0);

  return {
    account1_80pctTotal: fromC(account1_80pctC),
    account2ProfitTotal: fromC(account2ProfitC),
    operatorNetProfit: fromC(operatorNetProfitC),
    totalExpenseWithdrawal: fromC(totalExpenseC),
    withdrawalFromReserve: fromC(fromReserveC),
    withdrawalFromProfit: fromC(fromProfitC),
    operatorRemainingProfit: fromC(operatorRemainingC),
    remainingInAccount2: fromC(remainingInAcc2C),
    remainingChargeRefund: fromC(remainingChargeRefundC),
    locationTotal: fromC(locationTotalC),
    reserveBalance: fromC(reserveBalanceC),
    jTotal: fromC(jTotalC),
    kTotal: fromC(kTotalC),
    kBrotherTotal: fromC(kBrotherTotalC),
    companyTotal: fromC(companyTotalC),
    aiDevTotal: fromC(aiDevTotalC),
  };
}

/**
 * 投資家向けサマリ (§4.5)
 */
export function computeInvestorSummary(
  account1Computed: Account1Computed[],
): InvestorSummary {
  if (account1Computed.length === 0) {
    return { investorReceivedTotal: 0, displayedChargeBalance: 0 };
  }
  const last = account1Computed[account1Computed.length - 1];
  return {
    investorReceivedTotal: last.investorTotalAfter,
    displayedChargeBalance: last.chargeBalanceAfter,
  };
}

/** 通貨フォーマット ($1,234.56 / マイナスは括弧表記) */
export const formatCurrency = (n: number): string => {
  const formatted = new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Math.abs(n));
  if (n === 0) return '-';
  if (n < 0) return `(${formatted})`;
  return formatted;
};

export const formatPercent = (n: number): string =>
  new Intl.NumberFormat('en-US', {
    style: 'percent',
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(n);
