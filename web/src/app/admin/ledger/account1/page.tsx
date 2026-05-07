import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import { computeAccount1Daily } from '@/lib/ledger/calc'
import type { Account1DailyEntry, DistributionRule } from '@/lib/ledger/types'
import Account1Form from './Account1Form'

export const dynamic = 'force-dynamic'

export default async function Account1Page({
  searchParams,
}: {
  searchParams: Promise<{ investor?: string }>
}) {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) redirect('/login')
  const { data: profile } = await supabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) redirect('/dashboard')

  const admin = createAdminClient()
  const params = await searchParams

  const { data: investors } = await admin
    .from('ledger_investors')
    .select('id, name')
    .eq('is_active', true)
    .order('name')

  const investorId = params.investor || investors?.[0]?.id
  if (!investorId) {
    return (
      <div className="max-w-6xl mx-auto p-8">
        <p className="text-text-muted">投資家が登録されていません。</p>
        <Link href="/admin/ledger" className="text-accent">← 家計簿トップへ</Link>
      </div>
    )
  }

  const investor = investors?.find((i: any) => i.id === investorId)

  const { data: rule } = await admin
    .from('ledger_distribution_rules')
    .select('*')
    .eq('investor_id', investorId)
    .is('effective_to', null)
    .single()

  const { data: investorRow } = await admin
    .from('ledger_investors')
    .select('initial_charge_display')
    .eq('id', investorId)
    .single()

  const { data: entries } = await admin
    .from('ledger_account1_daily')
    .select('*')
    .eq('investor_id', investorId)
    .order('trade_date')

  const ruleTyped: DistributionRule | null = rule
    ? {
        id: rule.id,
        investorId: rule.investor_id,
        investorSharePct: parseFloat(rule.investor_share_pct),
        jSharePct: parseFloat(rule.j_share_pct),
        kSharePct: parseFloat(rule.k_share_pct),
        companySharePct: parseFloat(rule.company_share_pct),
        effectiveFrom: rule.effective_from,
      }
    : null

  const entriesTyped: Account1DailyEntry[] = (entries ?? []).map((e: any) => ({
    id: e.id,
    investorId: e.investor_id,
    tradeDate: e.trade_date,
    dailyProfit: parseFloat(e.daily_profit),
    notes: e.notes ?? undefined,
  }))

  const computed = ruleTyped
    ? computeAccount1Daily(
        entriesTyped,
        ruleTyped,
        parseFloat(investorRow?.initial_charge_display ?? '0'),
      )
    : []

  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">LAPLACE</Link>
          <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
            <Link href="/admin" className="text-text-muted hover:text-text">管理</Link>
            <Link href="/admin/ledger" className="text-text-muted hover:text-text">家計簿</Link>
            <span className="text-emerald-400 font-semibold">📊 1つめ口座</span>
            <Link href={`/admin/ledger/account2?investor=${investorId}`} className="text-text-muted hover:text-text">2つめ口座</Link>
            <Link href={`/admin/ledger/expenses?investor=${investorId}`} className="text-text-muted hover:text-text">経費出金</Link>
          </div>
        </div>
      </nav>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-8">
        <div className="hud-label mb-2">Ledger / Account 1</div>
        <h1 className="text-2xl font-black mb-1 font-hud">1 つめ口座 日次入力 - {investor?.name} さん</h1>
        <p className="text-text-muted text-sm mb-6">
          利益分配率: 投資家{((ruleTyped?.investorSharePct ?? 0) * 100).toFixed(0)}% / J {((ruleTyped?.jSharePct ?? 0) * 100).toFixed(0)}% / K {((ruleTyped?.kSharePct ?? 0) * 100).toFixed(0)}% / 会社 {((ruleTyped?.companySharePct ?? 0) * 100).toFixed(0)}%
        </p>

        <Account1Form investorId={investorId} computed={computed} rule={ruleTyped} />
      </div>
    </div>
  )
}
