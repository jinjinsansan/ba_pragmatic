import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import ExpenseBreakdownForm from './ExpenseBreakdownForm'

export const dynamic = 'force-dynamic'

export default async function ExpenseBreakdownPage({
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

  // 会社累計 (受取累計) を summary view から取得
  const { data: summary } = await admin
    .from('ledger_investor_summary')
    .select('company_total_merged, company_breakdown_total, company_breakdown_remaining')
    .eq('investor_id', investorId)
    .single()

  // 内訳エントリ一覧
  const { data: entries } = await admin
    .from('ledger_company_expense_breakdown')
    .select('*')
    .eq('investor_id', investorId)
    .order('expense_date', { ascending: false })
    .order('created_at', { ascending: false })

  const companyTotal = parseFloat(summary?.company_total_merged ?? '0')
  const breakdownTotal = parseFloat(summary?.company_breakdown_total ?? '0')
  const remaining = parseFloat(summary?.company_breakdown_remaining ?? '0')
  const isBalanced = Math.abs(remaining) < 0.01

  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">LAPLACE</Link>
          <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
            <Link href="/admin" className="text-text-muted hover:text-text">管理</Link>
            <Link href="/admin/ledger" className="text-text-muted hover:text-text">家計簿</Link>
            <Link href={`/admin/ledger/account1?investor=${investorId}`} className="text-text-muted hover:text-text">1つめ</Link>
            <Link href={`/admin/ledger/account2?investor=${investorId}`} className="text-text-muted hover:text-text">2つめ</Link>
            <Link href={`/admin/ledger/expenses?investor=${investorId}`} className="text-text-muted hover:text-text">経費出金</Link>
            <span className="text-purple-400 font-semibold">📒 経費内訳</span>
          </div>
        </div>
      </nav>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-8">
        <div className="hud-label mb-2">Ledger / Company Expense Breakdown</div>
        <h1 className="text-2xl font-black mb-1 font-hud">会社経費 内訳台帳 - {investor?.name} さん</h1>
        <p className="text-text-muted text-sm mb-6">
          会社累計 (= K の兄 + 旧会社配当 + AI 開発費 統合) を実際に何に使ったかを記録。内訳合計は会社累計と一致するべき。
        </p>

        {/* サマリ */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-8">
          <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-4">
            <div className="text-xs text-emerald-400 font-semibold tracking-widest mb-1">受取累計</div>
            <div className="font-mono text-2xl text-emerald-300 font-bold">${companyTotal.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
            <div className="text-[10px] text-text-muted mt-1">会社累計 (= K兄 + 会社 + AI)</div>
          </div>
          <div className="rounded-lg border border-purple-500/40 bg-purple-500/5 p-4">
            <div className="text-xs text-purple-400 font-semibold tracking-widest mb-1">内訳合計</div>
            <div className="font-mono text-2xl text-purple-300 font-bold">${breakdownTotal.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
            <div className="text-[10px] text-text-muted mt-1">下記エントリの合計</div>
          </div>
          <div className={`rounded-lg border p-4 ${isBalanced ? 'border-emerald-500/40 bg-emerald-500/5' : 'border-red-500/40 bg-red-500/10'}`}>
            <div className={`text-xs font-semibold tracking-widest mb-1 ${isBalanced ? 'text-emerald-400' : 'text-red-400'}`}>
              {isBalanced ? '✓ 一致' : '⚠ 残額'}
            </div>
            <div className={`font-mono text-2xl font-bold ${isBalanced ? 'text-emerald-300' : 'text-red-300'}`}>
              ${remaining.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </div>
            <div className="text-[10px] text-text-muted mt-1">
              {isBalanced ? '受取と内訳が一致' : '受取 − 内訳合計 (= 未記録分)'}
            </div>
          </div>
        </div>

        <ExpenseBreakdownForm investorId={investorId} entries={entries || []} />
      </div>
    </div>
  )
}
