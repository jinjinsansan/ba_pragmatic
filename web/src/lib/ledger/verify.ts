// =============================================================
// FX 運用家計簿 検証スクリプト
// SPEC_FX_LEDGER.md §9.1 の確定データを投入して §9.2 の期待値と一致するか確認
// =============================================================
// 実行: npx tsx src/lib/ledger/verify.ts
// =============================================================

import {
  computeAccount1Daily,
  computeAccount2Daily,
  computeExpenseWithdrawal,
  computeOperatorSummary,
  computeInvestorSummary,
  formatCurrency,
} from './calc';
import type {
  Account1DailyEntry,
  Account2DailyEntry,
  ExpenseWithdrawal,
  DistributionRule,
} from './types';

// ---- 入力データ (§9.1) ----
const investorId = 'H';

const rule: DistributionRule = {
  id: 'r1',
  investorId,
  investorSharePct: 0.20,
  jSharePct: 0.20,
  kSharePct: 0.30,
  companySharePct: 0.30,
  effectiveFrom: '2026-04-30',
};

const account1Entries: Account1DailyEntry[] = [
  { investorId, tradeDate: '2026-04-30', dailyProfit: 933 },
  { investorId, tradeDate: '2026-05-01', dailyProfit: 0 },
  { investorId, tradeDate: '2026-05-02', dailyProfit: 2049 },
  { investorId, tradeDate: '2026-05-03', dailyProfit: 786 },
  { investorId, tradeDate: '2026-05-04', dailyProfit: 3990 },
  { investorId, tradeDate: '2026-05-05', dailyProfit: 2143 },
  { investorId, tradeDate: '2026-05-06', dailyProfit: 2019 },
];

const account2Entries: Account2DailyEntry[] = [
  // 4/30〜5/5 の日次内訳が未取得のため 5/6 にまとめて入力
  { investorId, tradeDate: '2026-05-06', dailyProfit: 9019, withdrawal: 5919 },
];

const expenseEntries: ExpenseWithdrawal[] = [
  {
    investorId,
    withdrawalDate: '2026-05-06',
    sourceLabel: '別+2つめ',
    withdrawFromReserve: 2100,
    withdrawFromAccount2: 5919,
    jReceived: 1000,
    kReceived: 2000,
    kBrotherReceived: 919,
    companyReceived: 2000,
    aiDevExpense: 2100,
  },
];

const ACCOUNT2_INITIAL = 46900;
const RESERVE_INITIAL = 2100;
const INITIAL_CHARGE_DISPLAY = 49000;

// ---- 計算実行 ----
const account1Computed = computeAccount1Daily(account1Entries, rule, INITIAL_CHARGE_DISPLAY);
const account2Computed = computeAccount2Daily(account2Entries, ACCOUNT2_INITIAL);
const expenseComputed = expenseEntries.map(computeExpenseWithdrawal);
const opSummary = computeOperatorSummary({
  account1Computed,
  account2Entries,
  expenseEntries,
  account2Initial: ACCOUNT2_INITIAL,
  reserveInitial: RESERVE_INITIAL,
});
const invSummary = computeInvestorSummary(account1Computed);

// ---- 期待値 (§9.2) ----
const expected: Array<[string, number]> = [
  ['Hさんが受け取った利益累計',     2384.00],
  ['Hさん画面上のチャージ資金残高', 39464.00],
  ['運用者 総合計純利益',           18555.00],
  ['運用者 利益からの出金累計',     5919.00],
  ['運用者 残利益',                 12636.00],
  ['2 つめ口座内残存利益',          3100.00],
  ['1 つめ口座チャージ返金分',      9536.00],
  ['所在合計 (検算)',               12636.00],
  ['2 つめ口座 現在残高',           50000.00],
  ['別チャージ残高',                0.00],
  ['J 受取累計',                    1000.00],
  ['K 受取累計',                    2000.00],
  ['Kの兄 受取累計',                919.00],
  ['会社 受取累計',                 2000.00],
  ['AI開発費等',                    2100.00],
  ['出金合計 (全配布)',             8019.00],
];

const actual: Record<string, number> = {
  'Hさんが受け取った利益累計':      invSummary.investorReceivedTotal,
  'Hさん画面上のチャージ資金残高':  invSummary.displayedChargeBalance,
  '運用者 総合計純利益':            opSummary.operatorNetProfit,
  '運用者 利益からの出金累計':      opSummary.withdrawalFromProfit,
  '運用者 残利益':                  opSummary.operatorRemainingProfit,
  '2 つめ口座内残存利益':           opSummary.remainingInAccount2,
  '1 つめ口座チャージ返金分':       opSummary.remainingChargeRefund,
  '所在合計 (検算)':                opSummary.locationTotal,
  '2 つめ口座 現在残高':            account2Computed[account2Computed.length - 1]?.balanceAfter ?? 0,
  '別チャージ残高':                 opSummary.reserveBalance,
  'J 受取累計':                     opSummary.jTotal,
  'K 受取累計':                     opSummary.kTotal,
  'Kの兄 受取累計':                 opSummary.kBrotherTotal,
  '会社 受取累計':                  opSummary.companyTotal,
  'AI開発費等':                     opSummary.aiDevTotal,
  '出金合計 (全配布)':              expenseComputed.reduce((a, e) => a + e.totalWithdrawal, 0),
};

// ---- 検証 ----
console.log('===== FX 運用家計簿 検証スクリプト =====');
console.log('SPEC_FX_LEDGER.md §9.1 の確定データを投入、§9.2 の期待値と比較します\n');

let okCount = 0;
let ngCount = 0;
const TOLERANCE = 0.01;       // 1 セント以内の誤差まで許容

for (const [name, exp] of expected) {
  const act = actual[name];
  const diff = Math.abs(act - exp);
  const pass = diff < TOLERANCE;
  const mark = pass ? '✓' : '✗';
  const expStr = formatCurrency(exp).padStart(15);
  const actStr = formatCurrency(act).padStart(15);
  console.log(`${mark} ${name.padEnd(30)} expected=${expStr} actual=${actStr}${pass ? '' : ` DIFF=${diff.toFixed(4)}`}`);
  if (pass) okCount++;
  else ngCount++;
}

console.log('\n=====');
console.log(`OK: ${okCount} / ${expected.length}`);
console.log(`NG: ${ngCount}`);

if (ngCount === 0) {
  console.log('\n🎉 全項目一致! 計算ロジックは仕様書通り。');
  process.exit(0);
} else {
  console.log('\n❌ 不一致あり、計算ロジックを見直してください。');
  process.exit(1);
}
