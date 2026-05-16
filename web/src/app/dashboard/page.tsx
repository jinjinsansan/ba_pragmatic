import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import DashboardClient from './DashboardClient'
import SupportForm from './SupportForm'
import ReferralSection from './ReferralSection'
import { buildCustomerTelegramStartLink } from '@/lib/customer-telegram'

export default async function DashboardPage() {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) redirect('/login')

  const admin = createAdminClient()

  // 全クエリを並列実行
  const [
    { data: profile },
    { data: billing },
    { data: orders },
    { data: charges },
    { data: deductions },
    { data: deliverables },
    { data: commissions },
    { data: withdrawals },
  ] = await Promise.all([
    supabase.from('profiles').select('*').eq('id', user.id).single(),
    supabase.from('billing').select('*').eq('user_id', user.id).single(),
    supabase.from('orders').select('*').eq('user_id', user.id).order('created_at', { ascending: false }),
    supabase.from('charges').select('*').eq('user_id', user.id).order('created_at', { ascending: false }),
    supabase.from('deductions').select('*').eq('user_id', user.id).order('date', { ascending: false }).limit(30),
    supabase.from('deliverables').select('*').eq('user_id', user.id).order('created_at', { ascending: false }).limit(1),
    supabase.from('referral_commissions').select('*').eq('referrer_id', user.id),
    supabase.from('referral_withdrawals').select('*').eq('user_id', user.id).order('created_at', { ascending: false }),
  ])

  const referralCode = profile?.referral_code || ''
  const siteUrl = process.env.NEXT_PUBLIC_SITE_URL || 'https://bafather.uk'
  const referralUrl = `${siteUrl}/signup?ref=${referralCode}`

  // 紹介ユーザーと全チャージを一括取得 (N+1解消)
  const { data: referredProfiles } = await admin
    .from('profiles')
    .select('id, email, created_at')
    .eq('referred_by', referralCode)

  const referredIds = (referredProfiles || []).map(p => p.id)
  const { data: allReferredCharges } = referredIds.length > 0
    ? await admin.from('charges').select('user_id, amount').in('user_id', referredIds).eq('status', 'confirmed')
    : { data: [] }

  const commissionByReferred = new Map<string, number>()
  for (const c of (commissions || [])) {
    const rid = String((c as any).referred_id || '')
    if (!rid) continue
    commissionByReferred.set(rid, (commissionByReferred.get(rid) || 0) + Number((c as any).commission_amount || 0))
  }

  const referredWithCharges = (referredProfiles || []).map(p => {
    const totalCharged = (allReferredCharges || [])
      .filter(c => c.user_id === p.id)
      .reduce((s, c) => s + Number(c.amount), 0)
    return {
      ...p,
      total_charged: totalCharged,
      commission: commissionByReferred.get(p.id) || 0,
    }
  })

  const totalEarned = commissions?.reduce((s, c) => s + Number(c.commission_amount), 0) ?? 0
  const totalWithdrawn = withdrawals?.filter(w => ['pending', 'approved'].includes(w.status))
    .reduce((s, w) => s + Number(w.amount), 0) ?? 0

  const latestOrder = orders?.[0]
  const hasPackage = latestOrder?.status === 'delivered' || latestOrder?.status === 'confirmed'
  const hasActiveCharge = billing && billing.balance > 0 && !billing.suspended
  const canDownload = !!deliverables?.length
  const telegramLinked = !!billing?.bot_config?.customer_telegram_chat_id
  const telegramLink = buildCustomerTelegramStartLink(user.id)
  const latestDeliverable = deliverables?.[0]
  const deliverableDate = latestDeliverable?.created_at
    ? new Date(latestDeliverable.created_at).toISOString().split('T')[0]
    : ''

  let status: 'no_purchase' | 'pending' | 'dry_run' | 'active' | 'suspended'
  if (!latestOrder) status = 'no_purchase'
  else if (latestOrder.status === 'pending' || latestOrder.status === 'sent') status = 'pending'
  else if (billing?.suspended) status = 'suspended'
  else if (!hasActiveCharge) status = 'dry_run'
  else status = 'active'

  return (
    <div className="min-h-screen">
      {/* Header */}
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <Link href="/" className="text-sm font-hud tracking-[0.35em] text-accent">BAFATHER</Link>
          <div className="flex items-center gap-3 sm:gap-4 flex-wrap justify-end">
            {profile?.is_admin && <Link href="/admin" className="text-sm text-text-muted hover:text-text">Admin</Link>}
            <DashboardClient />
          </div>
        </div>
      </nav>

      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="hud-label mb-2">II · Member Console</div>
        <h1 className="text-2xl sm:text-3xl mb-6 sm:mb-8 font-hud">Operations Dashboard</h1>

        {/* Status Banner */}
        <div className={`p-6 rounded-2xl border mb-8 glass-soft ${
          status === 'active' ? 'bg-green-500/10 border-green-500/30' :
          status === 'dry_run' ? 'bg-player/10 border-player/30' :
          status === 'pending' ? 'bg-yellow-500/10 border-yellow-500/30' :
          status === 'suspended' ? 'bg-banker/10 border-banker/30' :
          'bg-bg-card border-white/10'
        }`}>
          <div className="flex items-center justify-between flex-wrap gap-4">
            <div>
              <div className="text-sm text-text-muted mb-1">Account Status</div>
              <div className={`text-xl sm:text-2xl font-bold leading-tight ${
                status === 'active' ? 'text-green-400' :
                status === 'dry_run' ? 'text-player' :
                status === 'pending' ? 'text-yellow-400' :
                status === 'suspended' ? 'text-banker' :
                'text-text-muted'
              }`}>
                {status === 'active' && 'ACTIVE — Live Betting Enabled'}
                {status === 'dry_run' && 'DRY RUN — Charge to enable live bets'}
                {status === 'pending' && 'PENDING — Awaiting payment confirmation'}
                {status === 'suspended' && 'SUSPENDED — Contact support'}
                {status === 'no_purchase' && 'No License — Purchase to get started'}
              </div>
            </div>
            {status === 'no_purchase' && (
              <Link href="/purchase" className="btn-primary px-6 py-3 w-full sm:w-auto text-center">
                Purchase License
              </Link>
            )}
            {status === 'dry_run' && (
              <Link href="/dashboard/charge" className="btn-primary px-6 py-3 w-full sm:w-auto text-center">
                Charge Balance
              </Link>
            )}
          </div>
        </div>

        {/* Stats Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
          <div className="p-5 rounded-xl glass-card">
            <div className="text-sm text-text-muted">Balance</div>
            {billing?.is_free ? (
              <div className="flex items-center gap-2 mt-1">
                <div className="text-2xl font-bold text-text">—</div>
                <span className="px-2 py-0.5 rounded text-xs font-black bg-accent/20 text-accent tracking-widest">FREE</span>
              </div>
            ) : (
              <div className="text-2xl font-bold text-text">${billing?.balance?.toFixed(2) || '0.00'}</div>
            )}
          </div>
          <div className="p-5 rounded-xl glass-card">
            <div className="text-sm text-text-muted">Profit Share Rate</div>
            <div className="text-2xl font-bold text-text">{billing ? `${(billing.profit_share_rate * 100).toFixed(0)}%` : '—'}</div>
          </div>
          <div className="p-5 rounded-xl glass-card">
            <div className="text-sm text-text-muted">Total Charged</div>
            <div className="text-2xl font-bold text-text">${billing?.total_charged?.toFixed(2) || '0.00'}</div>
          </div>
          <div className="p-5 rounded-xl glass-card">
            <div className="text-sm text-text-muted">Carry Loss</div>
            <div className="text-2xl font-bold text-banker">${billing?.carry_loss?.toFixed(2) || '0.00'}</div>
          </div>
        </div>

        {/* Download + Referral */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
          {/* Download Section */}
          <div className="p-6 rounded-2xl glass-card">
            <h2 className="text-lg font-bold mb-4">Software Download</h2>
            {canDownload ? (
              <div className="space-y-3">
                <a
                  href={`/api/download?file=${deliverables![0].file_path}`}
                  className="btn-primary inline-block px-6 py-3 w-full sm:w-auto text-center"
                >
                  Download BAFATHER v{deliverables![0].version}
                </a>
                {deliverableDate && (
                  <div className="text-xs text-text-muted">Updated: {deliverableDate}</div>
                )}
                {latestDeliverable?.file_path && (
                  <div className="text-xs text-text-dim break-all">
                    Direct URL: {latestDeliverable.file_path}
                  </div>
                )}
              </div>
            ) : hasPackage ? (
              <p className="text-text-muted">Your download is being prepared...</p>
            ) : (
              <p className="text-text-muted">Purchase a license to download.</p>
            )}
          </div>

          <div className="p-6 rounded-2xl glass-card">
            <h2 className="text-lg font-bold mb-4">Telegram通知連携</h2>
            <div className="text-sm text-text-muted mb-3">
              1タップで顧客向け通知を連携できます（日次精算・未払い/入金反映・セッション開始通知）。
            </div>
            {telegramLinked ? (
              <div className="space-y-2">
                <div className="text-sm text-green-400 font-semibold">連携済み</div>
                {telegramLink && (
                  <a
                    href={telegramLink}
                    target="_blank"
                    rel="noreferrer"
                    className="btn-primary inline-block px-5 py-2.5"
                  >
                    Telegramを開く
                  </a>
                )}
                <div className="text-xs text-text-muted">解除したい場合はBotで /stop を送信してください。</div>
              </div>
            ) : telegramLink ? (
              <div className="space-y-2">
                <a
                  href={telegramLink}
                  target="_blank"
                  rel="noreferrer"
                  className="btn-primary inline-block px-5 py-2.5"
                >
                  1タップ連携する
                </a>
                <div className="text-xs text-text-muted">Telegramで /start が実行されると連携完了です。</div>
              </div>
            ) : (
              <div className="text-sm text-yellow-400">現在は連携リンクを生成できません（環境設定未完了）。</div>
            )}
          </div>

        </div>

        {/* Referral Section */}
        <ReferralSection
          referralUrl={referralUrl}
          referred={referredWithCharges}
          totalEarned={totalEarned}
          totalWithdrawn={totalWithdrawn}
          withdrawals={withdrawals || []}
        />

        {/* Charge History */}
        <div className="p-6 rounded-2xl glass-card mb-8">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-bold">Charge History</h2>
            <Link href="/dashboard/charge" className="text-sm text-accent hover:underline">Add Charge</Link>
          </div>
          {charges?.length ? (
            <div className="overflow-x-auto">
              <table className="min-w-[520px] w-full text-sm">
                <thead><tr className="text-text-muted text-left"><th className="pb-2">Date</th><th className="pb-2">Amount</th><th className="pb-2">Status</th></tr></thead>
                <tbody>
                  {charges.map(c => (
                    <tr key={c.id} className="border-t border-accent/10">
                      <td className="py-2">{new Date(c.created_at).toLocaleDateString()}</td>
                      <td className="py-2 font-bold">${Number(c.amount).toLocaleString()}</td>
                      <td className="py-2">
                        <span className={`px-2 py-0.5 rounded text-xs ${c.status === 'confirmed' ? 'bg-green-500/20 text-green-400' : 'bg-yellow-500/20 text-yellow-400'}`}>
                          {c.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <p className="text-text-muted text-sm">No charges yet.</p>}
        </div>

        {/* Daily Settlements */}
        <div className="p-6 rounded-2xl glass-card">
          <h2 className="text-lg font-bold mb-4">Daily Settlements</h2>
          {deductions?.length ? (
            <div className="overflow-x-auto">
              <table className="min-w-[640px] w-full text-sm">
                <thead><tr className="text-text-muted text-left"><th className="pb-2">Date</th><th className="pb-2">Profit</th><th className="pb-2">Fee</th><th className="pb-2">Note</th></tr></thead>
                <tbody>
                  {deductions.map(d => (
                    <tr key={d.id} className="border-t border-accent/10">
                      <td className="py-2">{d.date}</td>
                      <td className={`py-2 font-bold ${Number(d.daily_profit) >= 0 ? 'text-green-400' : 'text-banker'}`}>
                        {Number(d.daily_profit) >= 0 ? '+' : ''}${Number(d.daily_profit).toFixed(2)}
                      </td>
                      <td className="py-2 text-banker">${Number(d.fee_amount).toFixed(2)}</td>
                      <td className="py-2 text-text-muted">{d.note || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <p className="text-text-muted text-sm">No settlements yet.</p>}
        </div>

        {/* Support */}
        <SupportForm />
      </div>
    </div>
  )
}
