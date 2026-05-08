import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import { computeExpenseWithdrawal } from '@/lib/ledger/calc'
import type { ExpenseWithdrawal } from '@/lib/ledger/types'
import ExpenseForm from './ExpenseForm'

export const dynamic = 'force-dynamic'

export default async function ExpensePage({
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

  const { data: entries } = await admin
    .from('ledger_expense_withdrawals')
    .select('*')
    .eq('investor_id', investorId)
    .order('withdrawal_date', { ascending: false })

  const entriesTyped: ExpenseWithdrawal[] = (entries ?? []).map((e: any) => ({
    id: e.id,
    investorId: e.investor_id,
    withdrawalDate: e.withdrawal_date,
    sourceLabel: e.source_label ?? undefined,
    withdrawFromReserve: parseFloat(e.withdraw_from_reserve),
    withdrawFromAccount2: parseFloat(e.withdraw_from_account2),
    jReceived: parseFloat(e.j_received),
    kReceived: parseFloat(e.k_received),
    kBrotherReceived: parseFloat(e.k_brother_received),
    companyReceived: parseFloat(e.company_received),
    aiDevExpense: parseFloat(e.ai_dev_expense),
    notes: e.notes ?? undefined,
  }))

  const computed = entriesTyped.map(computeExpenseWithdrawal)

  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">LAPLACE</Link>
          <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
            <Link href="/admin" className="text-text-muted hover:text-text">管理</Link>
            <Link href="/admin/ledger" className="text-text-muted hover:text-text">家計簿</Link>
            <Link href={`/admin/ledger/account1?investor=${investorId}`} className="text-text-muted hover:text-text">1つめ口座</Link>
            <Link href={`/admin/ledger/account2?investor=${investorId}`} className="text-text-muted hover:text-text">2つめ口座</Link>
            <span className="text-purple-400 font-semibold">🧾 経費出金</span>
            <Link href={`/admin/ledger/expense-breakdown?investor=${investorId}`} className="text-text-muted hover:text-text">経費内訳</Link>
          </div>
        </div>
      </nav>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-8">
        <div className="hud-label mb-2">Ledger / Expenses</div>
        <h1 className="text-2xl font-black mb-1 font-hud">経費出金台帳 - {investor?.name} さん</h1>
        <p className="text-text-muted text-sm mb-6">
          別チャージ + 2つめ口座 から引き出して J/K/兄/会社/AI 開発費 に配分するイベントを記録
        </p>

        <ExpenseForm investorId={investorId} computed={computed} />
      </div>
    </div>
  )
}
