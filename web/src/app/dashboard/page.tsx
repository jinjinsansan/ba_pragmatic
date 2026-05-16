import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { revalidatePath } from 'next/cache'
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
    { data: invoices },
    { data: deliverables },
    { data: commissions },
    { data: withdrawals },
  ] = await Promise.all([
    supabase.from('profiles').select('*').eq('id', user.id).single(),
    supabase.from('billing').select('*').eq('user_id', user.id).single(),
    supabase.from('orders').select('*').eq('user_id', user.id).order('created_at', { ascending: false }),
    supabase.from('charges').select('*').eq('user_id', user.id).order('created_at', { ascending: false }),
    supabase.from('deductions').select('*').eq('user_id', user.id).order('date', { ascending: false }).limit(30),
    supabase.from('daily_profit_invoices').select('*').eq('user_id', user.id).order('settle_date', { ascending: false }).limit(30),
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
  const telegramUsername = String(billing?.bot_config?.customer_telegram_username || '').trim()
  const telegramLinkedAtRaw = String(billing?.bot_config?.customer_telegram_linked_at || '').trim()
  const telegramLinkedAt = telegramLinkedAtRaw ? new Date(telegramLinkedAtRaw).toLocaleString('ja-JP') : ''
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

  const jstDate = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Tokyo' })
  const todayDeductionRows = (deductions || []).filter((d: any) => String(d.date || '') === jstDate)
  const todayProfit = todayDeductionRows.reduce((s: number, d: any) => s + Number(d.daily_profit || 0), 0)
  const todayFee = todayDeductionRows.reduce((s: number, d: any) => s + Number(d.fee_amount || 0), 0)
  const outstandingAmount = (invoices || [])
    .filter((i: any) => String(i.status || '') === 'unpaid')
    .reduce((s: number, i: any) => s + Number(i.outstanding_amount || 0), 0)
  const pendingChargeCount = (charges || []).filter((c: any) => String(c.status || '') !== 'confirmed').length
  const lastConfirmedCharge = (charges || []).find((c: any) => String(c.status || '') === 'confirmed')
  const walletAddress = String(process.env.NEXT_PUBLIC_USDT_TRC20 || '').trim()
  const nextSettlementAt = '毎日 JST 00:05 以降'
  const isLocked = !!billing?.suspended

  const nextSteps: Array<{ done: boolean; title: string; actionText?: string; href?: string; desc: string }> = [
    { done: !!latestOrder, title: 'ライセンス購入', actionText: '購入する', href: '/purchase', desc: 'まずライセンスを有効化します。' },
    { done: !!hasActiveCharge, title: '残高チャージ', actionText: 'チャージする', href: '/dashboard/charge', desc: 'ライブ運用には残高が必要です。' },
    { done: telegramLinked, title: 'Telegram通知連携', actionText: '連携する', href: telegramLink || '', desc: '精算・未払い・開始通知を受信します。' },
  ]
  const pendingStep = nextSteps.find(s => !s.done)

  const timeline: Array<{ at: number; label: string; detail: string; tone: 'ok' | 'warn' | 'info' }> = []
  if (telegramLinkedAtRaw) {
    timeline.push({
      at: new Date(telegramLinkedAtRaw).getTime(),
      label: 'Telegram連携完了',
      detail: telegramUsername ? `@${telegramUsername} と連携` : 'Telegram通知を有効化',
      tone: 'ok',
    })
  }
  for (const c of (charges || []).slice(0, 8)) {
    timeline.push({
      at: new Date(c.created_at).getTime(),
      label: `チャージ申請 (${String(c.status)})`,
      detail: `$${Number(c.amount || 0).toFixed(2)} / ${new Date(c.created_at).toLocaleDateString('ja-JP')}`,
      tone: c.status === 'confirmed' ? 'ok' : 'warn',
    })
  }
  for (const d of (deductions || []).slice(0, 8)) {
    timeline.push({
      at: new Date(`${d.date}T00:00:00+09:00`).getTime(),
      label: '日次精算',
      detail: `${d.date} / PnL ${Number(d.daily_profit || 0) >= 0 ? '+' : ''}$${Number(d.daily_profit || 0).toFixed(2)} / Fee $${Number(d.fee_amount || 0).toFixed(2)}`,
      tone: Number(d.fee_amount || 0) > 0 ? 'warn' : 'info',
    })
  }
  for (const i of (invoices || []).slice(0, 8)) {
    const settleDate = String(i.settle_date || '')
    timeline.push({
      at: settleDate ? new Date(`${settleDate}T00:00:00+09:00`).getTime() : 0,
      label: `請求ステータス (${String(i.status || 'none')})`,
      detail: `${settleDate} / outstanding $${Number(i.outstanding_amount || 0).toFixed(2)}`,
      tone: i.status === 'unpaid' ? 'warn' : 'ok',
    })
  }
  timeline.sort((a, b) => b.at - a.at)
  const recentTimeline = timeline.slice(0, 8)

  async function unlinkTelegramAction() {
    'use server'
    const actionSupabase = await createClient()
    const { data: { user: actionUser } } = await actionSupabase.auth.getUser()
    if (!actionUser) return
    const actionAdmin = createAdminClient()
    const { data: row } = await actionAdmin
      .from('billing')
      .select('bot_config')
      .eq('user_id', actionUser.id)
      .maybeSingle()
    const currentConfig = row?.bot_config && typeof row.bot_config === 'object'
      ? ({ ...(row.bot_config as Record<string, unknown>) })
      : {}
    delete currentConfig.customer_telegram_chat_id
    delete currentConfig.customer_telegram_username
    delete currentConfig.customer_telegram_linked_at
    currentConfig.customer_telegram_enabled = false
    await actionAdmin.from('billing').upsert({
      user_id: actionUser.id,
      bot_config: currentConfig,
      updated_at: new Date().toISOString(),
    }, { onConflict: 'user_id' })
    revalidatePath('/dashboard')
  }

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

        {/* Charge Quick Guide */}
        <div className="p-5 rounded-2xl glass-card mb-8">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div>
              <div className="hud-label mb-1">Charge Guide</div>
              <h2 className="text-lg font-hud">資金追加（チャージ）</h2>
              <p className="text-xs text-text-muted mt-1">
                ライブ運用には残高が必要です。迷わないように手順を固定表示しています。
              </p>
            </div>
            <Link href="/dashboard/charge" className="btn-primary px-5 py-2.5 w-full sm:w-auto text-center">
              今すぐ資金追加
            </Link>
          </div>
          <div className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
            <div className="p-3 rounded-lg bg-bg-glass border border-accent/10">
              <div className="text-text-dim">現在残高</div>
              <div className="text-base font-semibold text-text mt-1">${Number(billing?.balance || 0).toFixed(2)}</div>
            </div>
            <div className="p-3 rounded-lg bg-bg-glass border border-accent/10">
              <div className="text-text-dim">未確認チャージ</div>
              <div className={`text-base font-semibold mt-1 ${pendingChargeCount > 0 ? 'text-yellow-400' : 'text-green-400'}`}>
                {pendingChargeCount} 件
              </div>
            </div>
            <div className="p-3 rounded-lg bg-bg-glass border border-accent/10">
              <div className="text-text-dim">最終反映</div>
              <div className="text-base font-semibold text-text mt-1">
                {lastConfirmedCharge ? new Date(lastConfirmedCharge.created_at).toLocaleDateString('ja-JP') : '未反映'}
              </div>
            </div>
          </div>
          <div className="mt-4 space-y-2 text-xs text-text-muted">
            <div className="flex items-start gap-2"><span className="text-accent">1.</span><span>チャージ画面で金額を入力して申請</span></div>
            <div className="flex items-start gap-2"><span className="text-accent">2.</span><span>USDT (TRC-20) を送金</span></div>
            <div className="flex items-start gap-2"><span className="text-accent">3.</span><span>管理承認後に残高へ反映（未払いがあれば自動充当）</span></div>
          </div>
          {walletAddress && (
            <div className="mt-3 text-[11px] text-text-dim break-all">
              送金先(TRC-20): {walletAddress}
            </div>
          )}
        </div>

        {/* Next Step + Daily Summary (mobile-first) */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-8">
          <div className="p-5 rounded-2xl glass-card">
            <div className="hud-label mb-2">Next Action</div>
            <h2 className="text-lg font-hud mb-3">次にやること</h2>
            {pendingStep ? (
              <div className="space-y-3">
                <div className="text-sm text-text">{pendingStep.title}</div>
                <div className="text-xs text-text-muted">{pendingStep.desc}</div>
                {pendingStep.href && (
                  <Link
                    href={pendingStep.href}
                    className="btn-primary inline-block px-5 py-2.5 w-full text-center"
                    target={pendingStep.href.startsWith('http') ? '_blank' : undefined}
                    rel={pendingStep.href.startsWith('http') ? 'noreferrer' : undefined}
                  >
                    {pendingStep.actionText || '進む'}
                  </Link>
                )}
              </div>
            ) : (
              <div className="text-sm text-green-400">すべての初期セットアップが完了しています。</div>
            )}
            <div className="mt-4 space-y-2">
              {nextSteps.map((s, idx) => (
                <div key={s.title} className="flex items-start gap-3 text-xs">
                  <span className={`mt-0.5 inline-flex h-5 w-5 items-center justify-center rounded-full border ${s.done ? 'border-green-500/40 text-green-400' : 'border-accent/40 text-accent'}`}>
                    {s.done ? '✓' : idx + 1}
                  </span>
                  <div>
                    <div className={s.done ? 'text-green-400' : 'text-text'}>{s.title}</div>
                    <div className="text-text-dim">{s.desc}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="p-5 rounded-2xl glass-card">
            <div className="hud-label mb-2">Today Snapshot</div>
            <h2 className="text-lg font-hud mb-3">今日の運用サマリー</h2>
            <div className="grid grid-cols-2 gap-3">
              <div className="p-3 rounded-lg bg-bg-glass border border-accent/10">
                <div className="text-[11px] text-text-dim">Today PnL</div>
                <div className={`text-lg font-bold ${todayProfit >= 0 ? 'text-green-400' : 'text-banker'}`}>
                  {todayProfit >= 0 ? '+' : ''}${todayProfit.toFixed(2)}
                </div>
              </div>
              <div className="p-3 rounded-lg bg-bg-glass border border-accent/10">
                <div className="text-[11px] text-text-dim">Today Fee</div>
                <div className="text-lg font-bold text-accent">${todayFee.toFixed(2)}</div>
              </div>
              <div className="p-3 rounded-lg bg-bg-glass border border-accent/10">
                <div className="text-[11px] text-text-dim">Outstanding</div>
                <div className={`text-lg font-bold ${outstandingAmount > 0 ? 'text-yellow-400' : 'text-green-400'}`}>${outstandingAmount.toFixed(2)}</div>
              </div>
              <div className="p-3 rounded-lg bg-bg-glass border border-accent/10">
                <div className="text-[11px] text-text-dim">Lock Status</div>
                <div className={`text-lg font-bold ${isLocked ? 'text-banker' : 'text-green-400'}`}>{isLocked ? 'LOCKED' : 'ACTIVE'}</div>
              </div>
            </div>
            <div className="text-xs text-text-muted mt-3">次回精算: {nextSettlementAt}</div>
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
              このダッシュボードのアカウント（{profile?.email || 'your account'}）にTelegramを紐づけます。
              日次精算・未払い/入金反映・セッション開始通知を受け取れます。
            </div>
            {telegramLinked ? (
              <div className="space-y-2">
                <div className="text-sm text-green-400 font-semibold">連携済み</div>
                <div className="text-xs text-text-muted">
                  {telegramUsername ? `連携先: @${telegramUsername}` : '連携先: Telegramアカウント'}
                  {telegramLinkedAt ? ` / 連携日時: ${telegramLinkedAt}` : ''}
                </div>
                <div className="flex flex-col sm:flex-row gap-2">
                  {telegramLink && (
                    <a
                      href={telegramLink}
                      target="_blank"
                      rel="noreferrer"
                      className="btn-primary inline-block px-5 py-2.5 text-center"
                    >
                      Telegramを開く
                    </a>
                  )}
                  <form action={unlinkTelegramAction}>
                    <button type="submit" className="btn-outline px-5 py-2.5 w-full sm:w-auto">
                      ワンタップ解除
                    </button>
                  </form>
                </div>
                <div className="text-xs text-text-muted">再連携は「Telegramを開く」→ /start 実行で完了します。</div>
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
                <div className="text-xs text-text-muted">Telegramで /start が実行されると、このアカウントに紐づいて連携完了です。</div>
              </div>
            ) : (
              <div className="text-sm text-yellow-400">現在は連携リンクを生成できません（環境設定未完了）。</div>
            )}
          </div>

        </div>

        {/* Timeline */}
        <div className="p-6 rounded-2xl glass-card mb-8">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-bold">運用タイムライン</h2>
            <span className="text-xs text-text-dim">最新8件</span>
          </div>
          {recentTimeline.length ? (
            <div className="space-y-3">
              {recentTimeline.map((item, idx) => (
                <div key={`${item.label}-${item.at}-${idx}`} className="p-3 rounded-xl bg-bg-glass border border-accent/10">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className={`text-sm font-semibold ${
                        item.tone === 'ok' ? 'text-green-400' :
                        item.tone === 'warn' ? 'text-yellow-400' :
                        'text-text'
                      }`}>{item.label}</div>
                      <div className="text-xs text-text-muted mt-1">{item.detail}</div>
                    </div>
                    <div className="text-[11px] text-text-dim whitespace-nowrap">
                      {item.at ? new Date(item.at).toLocaleDateString('ja-JP') : '—'}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-text-muted">まだ履歴がありません。チャージ・精算が発生するとここに表示されます。</p>
          )}
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

        {/* Daily PnL History */}
        <div className="p-6 rounded-2xl glass-card">
          <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
            <h2 className="text-lg font-bold">Daily PnL History（日次）</h2>
            <span className="text-xs text-text-dim">JST日次確定ベース</span>
          </div>
          {(invoices?.length || deductions?.length) ? (
            <div className="overflow-x-auto">
              <table className="min-w-[760px] w-full text-sm">
                <thead>
                  <tr className="text-text-muted text-left">
                    <th className="pb-2">Date</th>
                    <th className="pb-2">Daily PnL</th>
                    <th className="pb-2">Net</th>
                    <th className="pb-2">Fee</th>
                    <th className="pb-2">Outstanding</th>
                    <th className="pb-2">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {(invoices?.length ? invoices : deductions || []).map((row: any, idx: number) => {
                    const date = String(row.settle_date || row.date || '')
                    const dailyProfit = Number(row.daily_profit || 0)
                    const netProfit = Number(row.net_profit ?? row.daily_profit ?? 0)
                    const fee = Number(row.operator_fee_amount ?? row.fee_amount ?? 0)
                    const outstanding = Number(row.outstanding_amount ?? row.outstanding_fee_amount ?? 0)
                    const statusLabel = String(row.status || (outstanding > 0 ? 'unpaid' : 'paid'))
                    return (
                      <tr key={`${row.id || idx}-${date}`} className="border-t border-accent/10">
                        <td className="py-2">{date || '—'}</td>
                        <td className={`py-2 font-bold ${dailyProfit >= 0 ? 'text-green-400' : 'text-banker'}`}>
                          {dailyProfit >= 0 ? '+' : ''}${dailyProfit.toFixed(2)}
                        </td>
                        <td className={`py-2 ${netProfit >= 0 ? 'text-text' : 'text-banker'}`}>
                          {netProfit >= 0 ? '+' : ''}${netProfit.toFixed(2)}
                        </td>
                        <td className="py-2 text-accent">${fee.toFixed(2)}</td>
                        <td className={`py-2 ${outstanding > 0 ? 'text-yellow-400' : 'text-text-muted'}`}>${outstanding.toFixed(2)}</td>
                        <td className="py-2">
                          <span className={`px-2 py-0.5 rounded text-xs ${
                            statusLabel === 'unpaid' ? 'bg-yellow-500/20 text-yellow-400' :
                            statusLabel === 'paid' ? 'bg-green-500/20 text-green-400' :
                            'bg-slate-500/20 text-slate-300'
                          }`}>
                            {statusLabel}
                          </span>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          ) : <p className="text-text-muted text-sm">No daily PnL history yet.</p>}
        </div>

        {/* Support */}
        <SupportForm />
      </div>
    </div>
  )
}
