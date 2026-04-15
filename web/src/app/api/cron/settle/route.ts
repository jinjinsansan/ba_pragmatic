import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

function getJstDateString() {
  return new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Tokyo' })
}

async function settleUser(admin: ReturnType<typeof createAdminClient>, userId: string, dailyProfit: number, dateStr: string) {
  const { data: billing } = await admin
    .from('billing')
    .select('balance, profit_share_rate, carry_loss, is_free, suspended')
    .eq('user_id', userId)
    .single()

  if (!billing) return { ok: false, error: 'Billing not found' }

  const { data: existing } = await admin
    .from('deductions')
    .select('id')
    .eq('user_id', userId)
    .eq('date', dateStr)
    .maybeSingle()

  if (existing) return { ok: false, error: 'Already settled for this date' }

  const carryLoss = Number(billing.carry_loss) || 0
  const netProfit = dailyProfit + carryLoss
  let feeAmount = 0
  let newCarryLoss = 0

  if (!billing.is_free && netProfit > 0) {
    feeAmount = netProfit * Number(billing.profit_share_rate)
    newCarryLoss = 0
  } else if (netProfit <= 0) {
    feeAmount = 0
    newCarryLoss = netProfit
  }

  const newBalance = Number(billing.balance) - feeAmount
  const nextBalance = Math.max(0, newBalance)
  const shouldSuspend = !billing.is_free && nextBalance <= 0

  await admin.from('deductions').insert({
    user_id: userId,
    date: dateStr,
    daily_profit: dailyProfit,
    fee_amount: feeAmount,
    carry_loss: newCarryLoss,
    note: netProfit > 0
      ? `${(Number(billing.profit_share_rate) * 100).toFixed(0)}% of net $${netProfit.toFixed(2)}`
      : 'Loss carried forward',
  })

  await admin.from('billing').update({
    balance: nextBalance,
    carry_loss: newCarryLoss,
    suspended: shouldSuspend,
    updated_at: new Date().toISOString(),
  }).eq('user_id', userId)

  return { ok: true, feeAmount, newBalance: nextBalance, suspended: shouldSuspend }
}

export async function GET(req: NextRequest) {
  const authHeader = req.headers.get('authorization')
  if (authHeader !== `Bearer ${process.env.CRON_SECRET}`) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }

  const admin = createAdminClient()
  // 「昨日分」を確定する。JST 00:05 頃に実行される前提で、昨日の日付を返す。
  const now = new Date()
  const jstNow = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Tokyo' }))
  const yesterday = new Date(jstNow)
  yesterday.setDate(jstNow.getDate() - 1)
  const dateStr = yesterday.toLocaleDateString('en-CA', { timeZone: 'Asia/Tokyo' })

  // session_state も含めて取得 (current_balance + daily_open を使う)
  const { data: billings } = await admin
    .from('billing')
    .select('user_id, balance, profit_share_rate, carry_loss, is_free, suspended, session_state')
    .eq('suspended', false)
    .gt('balance', 0)

  if (!billings?.length) {
    return NextResponse.json({ message: 'No active users', settled: 0, skipped: 0, date: dateStr })
  }

  let settled = 0
  let skipped = 0
  const skipReasons: Record<string, number> = {}
  const errors: Array<{ user_id: string; error: string }> = []

  // Stale threshold: 最後の balance 更新が昨日 23時 より前なら stale (信頼できない)
  const staleCutoff = new Date(yesterday)
  staleCutoff.setHours(23, 0, 0, 0)

  for (const b of billings) {
    if (b.is_free) { skipped++; skipReasons['free'] = (skipReasons['free'] || 0) + 1; continue }
    const ss = b.session_state as Record<string, unknown> | null
    if (!ss || typeof ss !== 'object') {
      skipped++
      skipReasons['no_session_state'] = (skipReasons['no_session_state'] || 0) + 1
      continue
    }
    const daily_open = ss.daily_open as { date?: string; balance?: number } | undefined
    const current_balance = typeof ss.current_balance === 'number' ? ss.current_balance : null
    const last_balance_at = typeof ss.last_balance_at === 'string' ? ss.last_balance_at : null
    if (!daily_open?.balance || !current_balance || !last_balance_at) {
      skipped++
      skipReasons['incomplete_state'] = (skipReasons['incomplete_state'] || 0) + 1
      continue
    }
    // daily_open.date が settle 対象日 (昨日) と一致しているかチェック
    if (daily_open.date !== dateStr) {
      skipped++
      skipReasons['stale_daily_open'] = (skipReasons['stale_daily_open'] || 0) + 1
      continue
    }
    // last_balance_at が昨日23時以降でない = GUI 長時間停止 → skip
    if (new Date(last_balance_at) < staleCutoff) {
      skipped++
      skipReasons['stale_balance'] = (skipReasons['stale_balance'] || 0) + 1
      continue
    }
    const dailyProfit = current_balance - daily_open.balance
    const result = await settleUser(admin, b.user_id, dailyProfit, dateStr)
    if (result.ok) {
      settled++
    } else {
      // "Already settled" は Python 側で先行処理された証 → ok 扱い
      if (result.error === 'Already settled for this date') {
        settled++
      } else {
        errors.push({ user_id: b.user_id, error: result.error || 'unknown' })
      }
    }
  }

  return NextResponse.json({
    message: `Settled ${settled}, skipped ${skipped}`,
    date: dateStr,
    settled,
    skipped,
    skipReasons,
    errors: errors.slice(0, 10),
  })
}

export async function POST(req: NextRequest) {
  const { api_key, email, date, net_profit } = await req.json()
  if (api_key !== process.env.LAPLACE_API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }
  if (!email) return NextResponse.json({ error: 'Missing email' }, { status: 400 })
  if (typeof net_profit !== 'number') {
    return NextResponse.json({ error: 'Missing net_profit' }, { status: 400 })
  }

  const admin = createAdminClient()
  const { data: profile } = await admin.from('profiles').select('id').eq('email', email).single()
  if (!profile) return NextResponse.json({ error: 'User not found' }, { status: 404 })

  const dateStr = date || getJstDateString()
  const result = await settleUser(admin, profile.id, net_profit, dateStr)
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 409 })
  }

  return NextResponse.json({
    ok: true,
    date: dateStr,
    fee_amount: result.feeAmount,
    balance: result.newBalance,
    suspended: result.suspended,
  })
}
