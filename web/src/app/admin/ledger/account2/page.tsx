import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import { computeAccount2Daily } from '@/lib/ledger/calc'
import type { Account2DailyEntry } from '@/lib/ledger/types'
import Account2Form from './Account2Form'

export const dynamic = 'force-dynamic'

export default async function Account2Page({
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
    .select('id, name, account2_amount')
    .eq('is_active', true)
    .order('name')

  const investorId = params.investor || investors?.[0]?.id
  if (!investorId) {
    return (
      <div className="max-w-6xl mx-auto p-8">
        <p className="text-text-muted">投資家が登録されていません。</p>
        <Link href="/admin/ledger" className="text-accent">← 資金管理トップへ</Link>
      </div>
    )
  }

  const investor = investors?.find((i: any) => i.id === investorId)
  const initialBalance = parseFloat(investor?.account2_amount ?? '0')

  const { data: entries } = await admin
    .from('ledger_account2_daily')
    .select('*')
    .eq('investor_id', investorId)
    .order('trade_date')

  const entriesTyped: Account2DailyEntry[] = (entries ?? []).map((e: any) => ({
    id: e.id,
    investorId: e.investor_id,
    tradeDate: e.trade_date,
    dailyProfit: parseFloat(e.daily_profit),
    withdrawal: parseFloat(e.withdrawal),
    notes: e.notes ?? undefined,
  }))

  const computed = computeAccount2Daily(entriesTyped, initialBalance)

  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">LAPLACE</Link>
          <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
            <Link href="/admin" className="text-text-muted hover:text-text">管理</Link>
            <Link href="/admin/ledger" className="text-text-muted hover:text-text">資金管理</Link>
            <Link href={`/admin/ledger/account1?investor=${investorId}`} className="text-text-muted hover:text-text">1つめ口座</Link>
            <span className="text-amber-400 font-semibold">💰 2つめ口座</span>
            <Link href={`/admin/ledger/expenses?investor=${investorId}`} className="text-text-muted hover:text-text">経費出金</Link>
            <Link href={`/admin/ledger/expense-breakdown?investor=${investorId}`} className="text-text-muted hover:text-text">経費内訳</Link>
          </div>
        </div>
      </nav>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-8">
        <div className="hud-label mb-2">Ledger / Account 2</div>
        <h1 className="text-2xl font-black mb-1 font-hud">2 つめ口座 日次入力 - {investor?.name} さん</h1>
        <p className="text-text-muted text-sm mb-2">
          初期残高: ${initialBalance.toLocaleString('en-US', { minimumFractionDigits: 2 })} (Hさんの資本金 / 分配対象外) / withdrawal は経費出金額
        </p>
        <p className="text-text-muted text-xs mb-6">
          利益分配率 (固定): <span className="text-amber-300">J 20% / K 30% / 会社内部留保 50%</span> (Hさんは取り分なし)
        </p>

        <Account2Form investorId={investorId} computed={computed} initialBalance={initialBalance} />
      </div>
    </div>
  )
}
