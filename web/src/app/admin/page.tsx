import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'

export const dynamic = 'force-dynamic'

export default async function AdminPage() {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) redirect('/login')

  const { data: profile } = await supabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) redirect('/dashboard')

  const admin = createAdminClient()
  const [
    { count: userCount },
    { count: pendingOrders },
    { count: pendingCharges },
    { count: openTickets },
    { count: pendingWithdrawals },
  ] = await Promise.all([
    admin.from('profiles').select('*', { count: 'exact', head: true }),
    admin.from('orders').select('*', { count: 'exact', head: true }).eq('status', 'pending'),
    admin.from('charges').select('*', { count: 'exact', head: true }).eq('status', 'pending'),
    admin.from('support_tickets').select('*', { count: 'exact', head: true }).eq('status', 'open'),
    admin.from('referral_withdrawals').select('*', { count: 'exact', head: true }).eq('status', 'pending'),
  ])

  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">LAPLACE</Link>
          <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
            <Link href="/admin" className="text-text font-semibold">管理</Link>
            <Link href="/dashboard" className="text-text-muted hover:text-text">Dashboard</Link>
            <Link href="/admin/users" className="text-text-muted hover:text-text">ユーザー</Link>
            <Link href="/admin/promos" className="text-text-muted hover:text-text">プロモ</Link>
            <Link href="/admin/tickets" className="text-text-muted hover:text-text">チケット</Link>
            <Link href="/admin/withdrawals" className="text-text-muted hover:text-text">出金申請</Link>
            <Link href="/dashboard" className="text-text-muted hover:text-text">マイページ</Link>
          </div>
        </div>
      </nav>

      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="hud-label mb-2">Admin Console</div>
        <h1 className="text-2xl sm:text-3xl font-black mb-6 sm:mb-8 font-hud">管理パネル</h1>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
          <Link href="/admin/users" className="p-6 rounded-2xl glass-card hover:border-accent/40 transition">
            <div className="text-3xl font-black text-player">{userCount || 0}</div>
            <div className="text-sm text-text-muted mt-1">総ユーザー数</div>
          </Link>
          <Link href="/admin/orders" className="p-6 rounded-2xl glass-card hover:border-accent/40 transition">
            <div className="text-3xl font-black text-yellow-400">{pendingOrders || 0}</div>
            <div className="text-sm text-text-muted mt-1">未確認注文</div>
          </Link>
          <Link href="/admin/orders" className="p-6 rounded-2xl glass-card hover:border-accent/40 transition">
            <div className="text-3xl font-black text-yellow-400">{pendingCharges || 0}</div>
            <div className="text-sm text-text-muted mt-1">未確認チャージ</div>
          </Link>
          <Link href="/admin/tickets" className="p-6 rounded-2xl glass-card hover:border-accent/40 transition">
            <div className="text-3xl font-black text-banker">{openTickets || 0}</div>
            <div className="text-sm text-text-muted mt-1">未対応チケット</div>
          </Link>
          <Link href="/admin/withdrawals" className="p-6 rounded-2xl glass-card hover:border-accent/40 transition">
            <div className="text-3xl font-black text-green-400">{pendingWithdrawals || 0}</div>
            <div className="text-sm text-text-muted mt-1">出金申請</div>
          </Link>
        </div>
      </div>
    </div>
  )
}
