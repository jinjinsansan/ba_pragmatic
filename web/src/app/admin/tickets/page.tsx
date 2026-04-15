import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import TicketActions from './TicketActions'

export const dynamic = 'force-dynamic'

export default async function AdminTicketsPage() {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) redirect('/login')

  const { data: profile } = await supabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) redirect('/dashboard')

  const admin = createAdminClient()
  const { data: tickets } = await admin
    .from('support_tickets')
    .select('*, profiles(email)')
    .order('created_at', { ascending: false })

  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">LAPLACE</Link>
          <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
            <Link href="/admin" className="text-text-muted hover:text-text">管理</Link>
            <Link href="/admin/orders" className="text-text-muted hover:text-text">注文</Link>
            <Link href="/admin/users" className="text-text-muted hover:text-text">ユーザー</Link>
            <Link href="/admin/promos" className="text-text-muted hover:text-text">プロモ</Link>
            <Link href="/admin/tickets" className="text-text font-semibold">チケット</Link>
          </div>
        </div>
      </nav>

      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="hud-label mb-2">Admin Console</div>
        <h1 className="text-2xl sm:text-3xl font-black mb-6 sm:mb-8 font-hud">サポートチケット</h1>
        <div className="space-y-4">
          {tickets?.map((t: any) => (
            <div key={t.id} className="p-6 rounded-2xl glass-card">
              <div className="flex items-center justify-between mb-3">
                <div>
                  <span className="text-sm text-text-muted">{t.profiles?.email}</span>
                  <span className="text-xs text-text-dim ml-3">{new Date(t.created_at).toLocaleString('ja-JP')}</span>
                </div>
                <span className={`px-2 py-0.5 rounded text-xs ${
                  t.status === 'open' ? 'bg-yellow-500/20 text-yellow-400' :
                  t.status === 'replied' ? 'bg-player/20 text-player' :
                  'bg-slate-500/20 text-slate-400'
                }`}>{t.status === 'open' ? '未対応' : t.status === 'replied' ? '返信済み' : 'クローズ'}</span>
              </div>
              <p className="text-text mb-3">{t.message}</p>
              {t.admin_reply && (
                <div className="p-3 rounded-lg glass-soft mb-3">
                  <div className="text-xs text-player mb-1">管理者返信</div>
                  <p className="text-text-muted text-sm">{t.admin_reply}</p>
                </div>
              )}
              <TicketActions ticketId={t.id} status={t.status} />
            </div>
          ))}
          {!tickets?.length && <p className="text-center text-text-muted py-12">チケットはまだありません</p>}
        </div>
      </div>
    </div>
  )
}
