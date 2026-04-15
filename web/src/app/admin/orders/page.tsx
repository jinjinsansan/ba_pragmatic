import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import OrderActions from './OrderActions'

export const dynamic = 'force-dynamic'

export default async function AdminOrdersPage() {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) redirect('/login')

  const { data: profile } = await supabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) redirect('/dashboard')

  const admin = createAdminClient()
  const { data: orders } = await admin
    .from('orders')
    .select('*, profiles(email)')
    .order('created_at', { ascending: false })

  const { data: charges } = await admin
    .from('charges')
    .select('*, profiles(email)')
    .order('created_at', { ascending: false })

  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">LAPLACE</Link>
          <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
            <Link href="/admin" className="text-text-muted hover:text-text">管理</Link>
            <Link href="/admin/orders" className="text-text font-semibold">注文</Link>
            <Link href="/admin/users" className="text-text-muted hover:text-text">ユーザー</Link>
            <Link href="/admin/promos" className="text-text-muted hover:text-text">プロモ</Link>
            <Link href="/admin/tickets" className="text-text-muted hover:text-text">チケット</Link>
          </div>
        </div>
      </nav>

      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="hud-label mb-2">Admin Console</div>
        <h2 className="text-xl sm:text-2xl font-bold mb-4 font-hud">パッケージ注文</h2>
        <div className="overflow-x-auto mb-12 glass-card p-4">
          <table className="min-w-[760px] w-full text-sm">
            <thead><tr className="text-text-muted text-left border-b border-accent/10">
              <th className="pb-3">メール</th><th className="pb-3">プラン</th><th className="pb-3">金額</th><th className="pb-3">ネットワーク</th><th className="pb-3">プロモ</th><th className="pb-3">ステータス</th><th className="pb-3">日付</th><th className="pb-3">操作</th>
            </tr></thead>
            <tbody>
              {orders?.map((o: any) => (
                <tr key={o.id} className="border-b border-accent/10">
                  <td className="py-3">{o.profiles?.email}</td>
                  <td className="py-3 capitalize">{o.plan}</td>
                  <td className="py-3 font-bold">${Number(o.amount).toLocaleString()}</td>
                  <td className="py-3">{o.usdt_network}</td>
                  <td className="py-3 text-text-muted">{o.promo_code || '—'}</td>
                  <td className="py-3">
                    <span className={`px-2 py-0.5 rounded text-xs ${
                      o.status === 'delivered' ? 'bg-green-500/20 text-green-400' :
                      o.status === 'confirmed' ? 'bg-player/20 text-player' :
                      'bg-yellow-500/20 text-yellow-400'
                    }`}>{o.status === 'delivered' ? '配送済み' : o.status === 'confirmed' ? '確認済み' : o.status === 'sent' ? '送信済み' : '未確認'}</span>
                  </td>
                  <td className="py-3 text-text-muted">{new Date(o.created_at).toLocaleDateString('ja-JP')}</td>
                  <td className="py-3">
                    <OrderActions type="order" id={o.id} userId={o.user_id} status={o.status} />
                  </td>
                </tr>
              ))}
              {!orders?.length && <tr><td colSpan={8} className="py-6 text-center text-text-muted">注文はまだありません</td></tr>}
            </tbody>
          </table>
        </div>

        <h2 className="text-xl sm:text-2xl font-bold mb-4 font-hud">チャージ申請</h2>
        <div className="overflow-x-auto glass-card p-4">
          <table className="min-w-[700px] w-full text-sm">
            <thead><tr className="text-text-muted text-left border-b border-accent/10">
              <th className="pb-3">メール</th><th className="pb-3">金額</th><th className="pb-3">ネットワーク</th><th className="pb-3">プロモ</th><th className="pb-3">ステータス</th><th className="pb-3">日付</th><th className="pb-3">操作</th>
            </tr></thead>
            <tbody>
              {charges?.map((c: any) => (
                <tr key={c.id} className="border-b border-accent/10">
                  <td className="py-3">{c.profiles?.email}</td>
                  <td className="py-3 font-bold">${Number(c.amount).toLocaleString()}</td>
                  <td className="py-3">{c.usdt_network}</td>
                  <td className="py-3 text-text-muted">{c.promo_code || '—'}</td>
                  <td className="py-3">
                    <span className={`px-2 py-0.5 rounded text-xs ${c.status === 'confirmed' ? 'bg-green-500/20 text-green-400' : 'bg-yellow-500/20 text-yellow-400'}`}>
                      {c.status === 'confirmed' ? '確認済み' : '未確認'}
                    </span>
                  </td>
                  <td className="py-3 text-text-muted">{new Date(c.created_at).toLocaleDateString('ja-JP')}</td>
                  <td className="py-3">
                    <OrderActions type="charge" id={c.id} userId={c.user_id} status={c.status} amount={Number(c.amount)} />
                  </td>
                </tr>
              ))}
              {!charges?.length && <tr><td colSpan={7} className="py-6 text-center text-text-muted">チャージ申請はまだありません</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
