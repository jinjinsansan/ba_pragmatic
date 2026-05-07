import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'

export const dynamic = 'force-dynamic'

const fmt = (n: number | string | null | undefined): string => {
  const v = typeof n === 'number' ? n : parseFloat(String(n ?? 0))
  if (!Number.isFinite(v) || v === 0) return '-'
  const formatted = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD',
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(Math.abs(v))
  return v < 0 ? `(${formatted})` : formatted
}

const fmtPct = (n: number | string): string => {
  const v = typeof n === 'number' ? n : parseFloat(String(n ?? 0))
  return new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 1 }).format(v)
}

export default async function AdminLedgerPage() {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) redirect('/login')

  const { data: profile } = await supabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) redirect('/dashboard')

  const admin = createAdminClient()

  // 投資家サマリ
  const { data: summaries } = await admin
    .from('ledger_investor_summary')
    .select('*')
    .order('investor_name')

  // 分配ルール (現在有効)
  const { data: rules } = await admin
    .from('ledger_distribution_rules')
    .select('*')
    .is('effective_to', null)

  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">LAPLACE</Link>
          <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
            <Link href="/admin" className="text-text-muted hover:text-text">管理</Link>
            <Link href="/admin/users" className="text-text-muted hover:text-text">ユーザー</Link>
            <Link href="/admin/ledger" className="text-text font-semibold">家計簿</Link>
            <Link href="/admin/orders" className="text-text-muted hover:text-text">注文</Link>
            <Link href="/admin/tickets" className="text-text-muted hover:text-text">チケット</Link>
          </div>
        </div>
      </nav>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="hud-label mb-2">Admin Console</div>
        <h1 className="text-2xl sm:text-3xl font-black mb-2 font-hud">FX 運用家計簿</h1>
        <p className="text-text-muted text-sm mb-6">投資家・運用者・会社の資金フローをすべて記録・可視化</p>

        {/* 投資家別タブ (将来複数投資家対応) */}
        <div className="flex flex-wrap gap-2 mb-6">
          {summaries?.map((s: any) => (
            <span key={s.investor_id} className="px-3 py-1 rounded bg-accent/10 text-accent text-sm font-semibold">
              {s.investor_name}
            </span>
          ))}
          <Link
            href="/admin/ledger/investors/new"
            className="px-3 py-1 rounded border border-text-muted/30 text-text-muted hover:text-text text-sm"
          >
            + 投資家追加
          </Link>
        </div>

        {/* 各投資家のダッシュボード */}
        {summaries?.map((s: any) => {
          const rule = rules?.find((r: any) => r.investor_id === s.investor_id)
          return (
            <section key={s.investor_id} className="mb-12">
              <div className="flex items-baseline gap-3 mb-4 border-b border-accent/20 pb-2">
                <h2 className="text-xl font-bold font-hud">{s.investor_name} さん</h2>
                <span className="text-xs text-text-muted">
                  投資総額 {fmt(s.total_investment)}
                </span>
              </div>

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">

                {/* 左カラム: 投資家情報 + 分配ルール */}
                <div className="space-y-4">
                  {/* 投資家情報 (緑系) */}
                  <div className="rounded-lg p-4 border border-emerald-500/20" style={{ background: 'rgba(34,197,94,0.05)' }}>
                    <div className="text-xs text-emerald-400 font-semibold tracking-widest mb-3">INVESTOR INFO</div>
                    <div className="grid grid-cols-2 gap-2 text-sm">
                      <div className="text-text-muted">投資総額</div>
                      <div className="font-mono text-right">{fmt(s.total_investment)}</div>
                      <div className="text-text-muted">1 つめ口座</div>
                      <div className="font-mono text-right">{fmt(s.account1_amount)}</div>
                      <div className="text-text-muted">2 つめ口座</div>
                      <div className="font-mono text-right">{fmt(s.account2_amount)}</div>
                      <div className="text-text-muted">別チャージ資金</div>
                      <div className="font-mono text-right">{fmt(s.reserve_initial)}</div>
                      <div className="text-text-muted border-t border-text-muted/20 pt-1">画面上初期チャージ</div>
                      <div className="font-mono text-right border-t border-text-muted/20 pt-1 font-bold">
                        {fmt(s.initial_charge_display)}
                      </div>
                    </div>
                  </div>

                  {/* 分配ルール */}
                  <div className="rounded-lg p-4 border border-blue-500/20" style={{ background: 'rgba(59,130,246,0.05)' }}>
                    <div className="text-xs text-blue-400 font-semibold tracking-widest mb-3">DISTRIBUTION RULE (1つめ口座)</div>
                    {rule ? (
                      <div className="grid grid-cols-2 gap-2 text-sm">
                        <div className="text-text-muted">{s.investor_name} 取り分</div>
                        <div className="font-mono text-right">{fmtPct(rule.investor_share_pct)}</div>
                        <div className="text-text-muted">J 取り分</div>
                        <div className="font-mono text-right">{fmtPct(rule.j_share_pct)}</div>
                        <div className="text-text-muted">K 取り分</div>
                        <div className="font-mono text-right">{fmtPct(rule.k_share_pct)}</div>
                        <div className="text-text-muted">会社内部保留</div>
                        <div className="font-mono text-right">{fmtPct(rule.company_share_pct)}</div>
                        <div className="text-text-muted border-t border-text-muted/20 pt-1">合計</div>
                        <div className="font-mono text-right border-t border-text-muted/20 pt-1 font-bold">
                          {fmtPct(parseFloat(rule.investor_share_pct) + parseFloat(rule.j_share_pct) + parseFloat(rule.k_share_pct) + parseFloat(rule.company_share_pct))}
                        </div>
                      </div>
                    ) : (
                      <div className="text-sm text-amber-400">分配ルール未設定</div>
                    )}
                  </div>

                  {/* 口座残高 */}
                  <div className="rounded-lg p-4 border border-slate-500/20" style={{ background: 'rgba(100,116,139,0.05)' }}>
                    <div className="text-xs text-slate-400 font-semibold tracking-widest mb-3">ACCOUNT BALANCES</div>
                    <div className="grid grid-cols-2 gap-2 text-sm">
                      <div className="text-text-muted">2 つめ口座 現在残高</div>
                      <div className="font-mono text-right">{fmt(s.account2_balance)}</div>
                      <div className="text-text-muted">別チャージ残高</div>
                      <div className="font-mono text-right">{fmt(s.reserve_balance)}</div>
                    </div>
                  </div>
                </div>

                {/* 右カラム: 現状サマリ */}
                <div className="space-y-4">
                  {/* 投資家サマリ (緑) */}
                  <div className="rounded-lg p-4 border border-emerald-500/30" style={{ background: 'rgba(34,197,94,0.08)' }}>
                    <div className="text-xs text-emerald-400 font-semibold tracking-widest mb-3">INVESTOR ({s.investor_name}) - 投資家から見える数値</div>
                    <div className="grid grid-cols-2 gap-2 text-sm">
                      <div className="text-text-muted">受け取った利益累計</div>
                      <div className="font-mono text-right text-emerald-300 font-bold">{fmt(s.investor_received_total)}</div>
                      <div className="text-text-muted">画面上のチャージ資金残高</div>
                      <div className="font-mono text-right text-emerald-300 font-bold">{fmt(s.displayed_charge_balance)}</div>
                    </div>
                  </div>

                  {/* 運用者サマリ (黄) */}
                  <div className="rounded-lg p-4 border border-amber-500/30" style={{ background: 'rgba(234,179,8,0.08)' }}>
                    <div className="text-xs text-amber-400 font-semibold tracking-widest mb-3">OPERATOR (運用者損益)</div>
                    <div className="grid grid-cols-2 gap-2 text-sm">
                      <div className="text-text-muted">1 つめ 80% 累計</div>
                      <div className="font-mono text-right">{fmt(s.account1_80pct_total)}</div>
                      <div className="text-text-muted">2 つめ 利益累計</div>
                      <div className="font-mono text-right">{fmt(s.account2_total_profit)}</div>
                      <div className="text-text-muted border-t border-text-muted/20 pt-1">総合計純利益</div>
                      <div className="font-mono text-right border-t border-text-muted/20 pt-1 text-amber-300 font-bold">
                        {fmt(s.operator_net_profit)}
                      </div>
                      <div className="text-text-muted">利益から出金</div>
                      <div className="font-mono text-right text-red-400">({fmt(s.expense_from_account2).replace(/[()]/g, '')})</div>
                      <div className="text-text-muted border-t border-text-muted/20 pt-1">残利益 (未出金)</div>
                      <div className="font-mono text-right border-t border-text-muted/20 pt-1 text-amber-200 font-bold text-lg">
                        {fmt(s.operator_remaining_profit)}
                      </div>
                    </div>
                  </div>

                  {/* 利益所在 (黄/橙) */}
                  <div className="rounded-lg p-4 border border-orange-500/20" style={{ background: 'rgba(249,115,22,0.05)' }}>
                    <div className="text-xs text-orange-400 font-semibold tracking-widest mb-3">PROFIT LOCATION (利益の所在)</div>
                    <div className="grid grid-cols-2 gap-2 text-sm">
                      <div className="text-text-muted">2 つめ口座内残存</div>
                      <div className="font-mono text-right">{fmt(s.remaining_in_account2)}</div>
                      <div className="text-text-muted">1 つめチャージ返金分</div>
                      <div className="font-mono text-right">{fmt(s.remaining_charge_refund)}</div>
                      <div className="text-text-muted border-t border-text-muted/20 pt-1">所在合計 (検算)</div>
                      <div className="font-mono text-right border-t border-text-muted/20 pt-1 font-bold">
                        {fmt(parseFloat(s.remaining_in_account2) + parseFloat(s.remaining_charge_refund))}
                      </div>
                    </div>
                    {Math.abs((parseFloat(s.remaining_in_account2) + parseFloat(s.remaining_charge_refund)) - parseFloat(s.operator_remaining_profit)) > 0.01 && (
                      <div className="mt-2 text-xs text-red-400">⚠ 検算不一致</div>
                    )}
                  </div>

                  {/* 経費受領 */}
                  <div className="rounded-lg p-4 border border-purple-500/20" style={{ background: 'rgba(168,85,247,0.05)' }}>
                    <div className="text-xs text-purple-400 font-semibold tracking-widest mb-3">EXPENSE RECIPIENTS (経費受取累計)</div>
                    <div className="grid grid-cols-2 gap-2 text-sm">
                      <div className="text-text-muted">J 受取累計</div>
                      <div className="font-mono text-right">{fmt(s.j_total)}</div>
                      <div className="text-text-muted">K 受取累計</div>
                      <div className="font-mono text-right">{fmt(s.k_total)}</div>
                      <div className="text-text-muted">K の兄 受取累計</div>
                      <div className="font-mono text-right">{fmt(s.k_brother_total)}</div>
                      <div className="text-text-muted">会社 (配当金) 累計</div>
                      <div className="font-mono text-right">{fmt(s.company_total)}</div>
                      <div className="text-text-muted">AI 開発費等</div>
                      <div className="font-mono text-right">{fmt(s.ai_dev_total)}</div>
                      <div className="text-text-muted border-t border-text-muted/20 pt-1">出金合計</div>
                      <div className="font-mono text-right border-t border-text-muted/20 pt-1 font-bold">
                        {fmt(s.expense_total)}
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              {/* サブページへのリンク */}
              <div className="mt-6 flex flex-wrap gap-2 text-sm">
                <Link href={`/admin/ledger/account1?investor=${s.investor_id}`}
                  className="px-4 py-2 rounded border border-emerald-500/30 hover:bg-emerald-500/10 text-emerald-400">
                  📊 1 つめ口座 (日次入力)
                </Link>
                <Link href={`/admin/ledger/account2?investor=${s.investor_id}`}
                  className="px-4 py-2 rounded border border-amber-500/30 hover:bg-amber-500/10 text-amber-400">
                  💰 2 つめ口座 (日次入力)
                </Link>
                <Link href={`/admin/ledger/expenses?investor=${s.investor_id}`}
                  className="px-4 py-2 rounded border border-purple-500/30 hover:bg-purple-500/10 text-purple-400">
                  🧾 経費出金台帳
                </Link>
              </div>
            </section>
          )
        })}

        {(!summaries || summaries.length === 0) && (
          <div className="text-center py-20 text-text-muted">
            <p>投資家が登録されていません。</p>
            <Link href="/admin/ledger/investors/new" className="text-accent hover:underline mt-4 inline-block">
              + 最初の投資家を追加
            </Link>
          </div>
        )}
      </div>
    </div>
  )
}
