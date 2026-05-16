import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import WithdrawalActions from './WithdrawalActions'

export const dynamic = 'force-dynamic'

export default async function AdminWithdrawalsPage() {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) redirect('/login')

  const { data: profile } = await supabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) redirect('/dashboard')

  const admin = createAdminClient()
  const { data: withdrawals } = await admin
    .from('referral_withdrawals')
    .select('*, profiles(email)')
    .order('created_at', { ascending: false })

  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">BAFATHER</Link>
          <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
            <Link href="/admin" className="text-text-muted hover:text-text">管理</Link>
            <Link href="/admin/orders" className="text-text-muted hover:text-text">注文</Link>
            <Link href="/admin/users" className="text-text-muted hover:text-text">ユーザー</Link>
            <Link href="/admin/promos" className="text-text-muted hover:text-text">プロモ</Link>
            <Link href="/admin/tickets" className="text-text-muted hover:text-text">チケット</Link>
            <Link href="/admin/withdrawals" className="text-text font-semibold">出金申請</Link>
            {/* <Link href="/admin/ledger" className="text-emerald-400 hover:text-emerald-300">📊 資金管理</Link> */}
          </div>
        </div>
      </nav>

      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="hud-label mb-2">III · Admin Console</div>
        <h1 className="text-2xl sm:text-3xl font-black mb-6 sm:mb-8 font-hud">出金申請管理</h1>
        <div className="overflow-x-auto glass-card p-4">
          <table className="min-w-[760px] w-full text-sm">
            <thead>
              <tr className="text-text-muted text-left border-b border-accent/10">
                <th className="pb-3">ユーザー</th>
                <th className="pb-3">金額</th>
                <th className="pb-3">ネットワーク</th>
                <th className="pb-3">ウォレット</th>
                <th className="pb-3">ステータス</th>
                <th className="pb-3">申請日</th>
                <th className="pb-3">操作</th>
              </tr>
            </thead>
            <tbody>
              {withdrawals?.map((w: any) => (
                <tr key={w.id} className="border-b border-accent/10">
                  <td className="py-3">{w.profiles?.email || w.user_id}</td>
                  <td className="py-3 font-bold text-green-400">${Number(w.amount).toFixed(2)}</td>
                  <td className="py-3 text-text-muted">{w.network}</td>
                  <td className="py-3 font-mono text-xs text-text-dim max-w-[160px] truncate">{w.wallet_address}</td>
                  <td className="py-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                      w.status === 'approved' ? 'bg-green-500/20 text-green-400' :
                      w.status === 'rejected' ? 'bg-banker/20 text-banker' :
                      'bg-yellow-500/20 text-yellow-400'
                    }`}>
                      {w.status === 'approved' ? '承認済' : w.status === 'rejected' ? '却下' : '申請中'}
                    </span>
                  </td>
                  <td className="py-3 text-text-muted">{new Date(w.created_at).toLocaleDateString('ja-JP')}</td>
                  <td className="py-3">
                    {w.status === 'pending' && <WithdrawalActions id={w.id} />}
                  </td>
                </tr>
              ))}
              {!withdrawals?.length && (
                <tr><td colSpan={7} className="py-8 text-center text-text-muted">申請はありません</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
