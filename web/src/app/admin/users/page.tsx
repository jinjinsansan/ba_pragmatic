import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import UserRow from './UserRow'

export const dynamic = 'force-dynamic'

export default async function AdminUsersPage() {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) redirect('/login')

  const { data: profile } = await supabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) redirect('/dashboard')

  const admin = createAdminClient()
  const { data: users } = await admin
    .from('profiles')
    .select('*, billing(*)')
    .order('created_at', { ascending: false })

  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">LAPLACE</Link>
          <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
            <Link href="/admin" className="text-text-muted hover:text-text">管理</Link>
            <Link href="/admin/orders" className="text-text-muted hover:text-text">注文</Link>
            <Link href="/admin/users" className="text-text font-semibold">ユーザー</Link>
            <Link href="/admin/promos" className="text-text-muted hover:text-text">プロモ</Link>
            <Link href="/admin/tickets" className="text-text-muted hover:text-text">チケット</Link>
          </div>
        </div>
      </nav>

      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="hud-label mb-2">Admin Console</div>
        <h1 className="text-2xl sm:text-3xl font-black mb-6 sm:mb-8 font-hud">ユーザー管理</h1>
        <div className="overflow-x-auto glass-card p-4">
          <table className="min-w-[820px] w-full text-sm">
            <thead><tr className="text-text-muted text-left border-b border-accent/10">
              <th className="pb-3">メール</th>
              <th className="pb-3">残高</th>
              <th className="pb-3">利益分配率</th>
              <th className="pb-3">ステータス</th>
              <th className="pb-3">紹介コード</th>
              <th className="pb-3">登録日</th>
              <th className="pb-3">操作</th>
            </tr></thead>
            <tbody>
              {users?.map((u: any) => {
                const b = Array.isArray(u.billing) ? u.billing[0] : u.billing
                return <UserRow key={u.id} user={u} billing={b} />
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
