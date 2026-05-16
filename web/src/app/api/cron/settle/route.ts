import { createAdminClient } from '@/lib/supabase-admin'
import { sendCustomerTelegramMessage } from '@/lib/customer-telegram'
import { NextRequest, NextResponse } from 'next/server'

type PnlSource = 'master_executor_daily_pnl' | 'session_state' | 'manual_api'

function getJstDateString(date = new Date()) {
  return date.toLocaleDateString('en-CA', { timeZone: 'Asia/Tokyo' })
}

function roundMoney(n: number) {
  return Math.round((Number(n) || 0) * 100) / 100
}

function isFeatureEnabled(value: string | undefined, defaultValue = false) {
  const raw = String(value ?? '').trim().toLowerCase()
  if (!raw) return defaultValue
  return raw === '1' || raw === 'true' || raw === 'yes' || raw === 'on'
}

async function sendTelegram(message: string) {
  const token = process.env.ADMIN_TELEGRAM_BOT_TOKEN
  const chatId = process.env.ADMIN_TELEGRAM_CHAT_ID
  if (!token || !chatId) return
  try {
    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text: message, parse_mode: 'HTML' }),
    })
  } catch {}
}

async function fetchMasterDailyPnlByEmail(dateStr: string) {
  const base = (process.env.BACOPY_MASTER_API_URL || process.env.BACOPY_BAFATHER_URL || '').trim().replace(/\/+$/, '')
  const apiKey = (process.env.BACOPY_API_KEY || process.env.LAPLACE_API_KEY || '').trim()
  if (!base || !apiKey) {
    return { enabled: false, map: new Map<string, number>(), error: '' }
  }
  try {
    const res = await fetch(`${base}/api/executors?limit=500`, {
      headers: { Authorization: `Bearer ${apiKey}` },
      cache: 'no-store',
    })
    if (!res.ok) {
      return { enabled: true, map: new Map<string, number>(), error: `master_api_http_${res.status}` }
    }
    const data = await res.json()
    const rows = Array.isArray(data?.executors) ? data.executors : []
    const map = new Map<string, number>()
    for (const row of rows) {
      const email = String(row?.user_email || '').trim().toLowerCase()
      const pnlDate = String(row?.daily_pnl_date || '').trim()
      if (!email || pnlDate !== dateStr) continue
      const pnl = Number(row?.daily_pnl)
      if (!Number.isFinite(pnl)) continue
      map.set(email, roundMoney((map.get(email) || 0) + pnl))
    }
    return { enabled: true, map, error: '' }
  } catch (e: any) {
    return { enabled: true, map: new Map<string, number>(), error: e?.message || 'master_api_fetch_failed' }
  }
}

async function settleUser(
  admin: ReturnType<typeof createAdminClient>,
  userId: string,
  userEmail: string,
  dailyProfit: number,
  dateStr: string,
  pnlSource: PnlSource,
) {
  const { data: billing } = await admin
    .from('billing')
    .select('*')
    .eq('user_id', userId)
    .single()

  if (!billing) return { ok: false, error: 'Billing not found' }

  const { data: existing } = await admin
    .from('deductions')
    .select('id')
    .eq('user_id', userId)
    .eq('date', dateStr)
    .maybeSingle()

  let shouldInsertDeduction = true
  if (existing) {
    const { data: existingInvoice, error: existingInvoiceErr } = await admin
      .from('daily_profit_invoices')
      .select('id')
      .eq('user_id', userId)
      .eq('settle_date', dateStr)
      .maybeSingle()
    const invoiceTableMissing = existingInvoiceErr?.code === '42P01' || String(existingInvoiceErr?.message || '').toLowerCase().includes('does not exist')
    if (invoiceTableMissing || existingInvoice?.id) {
      return { ok: false, error: 'Already settled for this date' }
    }
    shouldInsertDeduction = false
  }

  const balanceBefore = Number(billing.balance) || 0
  const baseBotConfig = (billing as any)?.bot_config && typeof (billing as any).bot_config === 'object'
    ? ((billing as any).bot_config as Record<string, unknown>)
    : {}
  const existingFallbackOutstanding = roundMoney(Number((baseBotConfig as any)?.outstanding_fee_amount || 0))
  const customerTelegramChatId = String((baseBotConfig as any)?.customer_telegram_chat_id || '').trim()
  const carryLoss = Number(billing.carry_loss) || 0
  const netProfit = roundMoney(dailyProfit + carryLoss)
  const operatorRate = Math.min(1, Math.max(0, Number(billing.profit_share_rate) || 0))
  const rawReferrerShareRate = (billing as any).referrer_share_rate ?? (billing as any)?.bot_config?.referrer_share_rate
  const parsedReferrerShareRate = Number(rawReferrerShareRate)
  const hasConfiguredReferrerRate =
    rawReferrerShareRate !== null &&
    rawReferrerShareRate !== undefined &&
    String(rawReferrerShareRate).trim() !== ''
  const defaultReferrerShareRate = 0.2
  const referrerShareRate = Math.min(
    1,
    Math.max(
      0,
      Number.isFinite(parsedReferrerShareRate)
        ? parsedReferrerShareRate
        : (hasConfiguredReferrerRate ? 0 : defaultReferrerShareRate),
    ),
  )
  const dynamicReferralEnabled = isFeatureEnabled(process.env.LAPLACE_ENABLE_DYNAMIC_REFERRAL_SPLIT, false)
  let feeAmount = 0
  let referrerFeeAmount = 0
  let newCarryLoss = 0
  let referrerId: string | null = null

  if (!billing.is_free && netProfit > 0) {
    feeAmount = roundMoney(netProfit * operatorRate)
    if (dynamicReferralEnabled && referrerShareRate > 0) {
      const { data: referredProfile } = await admin
        .from('profiles')
        .select('referred_by')
        .eq('id', userId)
        .maybeSingle()
      const referredByCode = String(referredProfile?.referred_by || '').trim()
      if (referredByCode) {
        const { data: referrerProfile } = await admin
          .from('profiles')
          .select('id')
          .eq('referral_code', referredByCode)
          .maybeSingle()
        if (referrerProfile?.id) {
          referrerId = referrerProfile.id
          referrerFeeAmount = roundMoney(feeAmount * referrerShareRate)
        }
      }
    }
    newCarryLoss = 0
  } else if (netProfit <= 0) {
    feeAmount = 0
    referrerFeeAmount = 0
    newCarryLoss = netProfit
  }

  const paidFromBalance = roundMoney(Math.min(balanceBefore, feeAmount))
  const outstandingAmount = roundMoney(Math.max(0, feeAmount - paidFromBalance))
  const nextBalance = roundMoney(Math.max(0, balanceBefore - feeAmount))
  const invoiceStatus = feeAmount <= 0 ? 'none' : (outstandingAmount > 0 ? 'unpaid' : 'paid')
  const settlementNote = netProfit > 0
    ? `${(operatorRate * 100).toFixed(0)}% of net $${netProfit.toFixed(2)}${referrerFeeAmount > 0 ? ` | referrer $${referrerFeeAmount.toFixed(2)} (${(referrerShareRate * 100).toFixed(0)}% of operator cut)` : ''}`
    : 'Loss carried forward'

  const deductionPayload = {
    user_id: userId,
    date: dateStr,
    daily_profit: roundMoney(dailyProfit),
    fee_amount: feeAmount,
    referrer_fee_amount: referrerFeeAmount,
    outstanding_fee_amount: outstandingAmount,
    pnl_source: pnlSource,
    carry_loss: newCarryLoss,
    note: settlementNote,
  }
  if (shouldInsertDeduction) {
    let { error: deductionErr } = await admin.from('deductions').insert(deductionPayload)
    if (deductionErr?.code === '42703') {
      const fallbackPayload = {
        user_id: userId,
        date: dateStr,
        daily_profit: roundMoney(dailyProfit),
        fee_amount: feeAmount,
        carry_loss: newCarryLoss,
        note: `${settlementNote} | src=${pnlSource} | outstanding=$${outstandingAmount.toFixed(2)} | referrer_fee=$${referrerFeeAmount.toFixed(2)}`,
      }
      const retry = await admin.from('deductions').insert(fallbackPayload)
      deductionErr = retry.error || null
    }
    // race-safe: another worker inserted the row first
    if (deductionErr?.code === '23505') {
      deductionErr = null
    }
    if (deductionErr) {
      return { ok: false, error: `deduction_insert_failed:${deductionErr.message}` }
    }
  }

  const { error: invoiceErr } = await admin.from('daily_profit_invoices').upsert({
    user_id: userId,
    settle_date: dateStr,
    daily_profit: roundMoney(dailyProfit),
    net_profit: netProfit,
    operator_rate: operatorRate,
    operator_fee_amount: feeAmount,
    referrer_fee_amount: referrerFeeAmount,
    paid_amount: paidFromBalance,
    outstanding_amount: outstandingAmount,
    status: invoiceStatus,
    note: settlementNote,
    updated_at: new Date().toISOString(),
  }, { onConflict: 'user_id,settle_date' })
  const invoiceMissing = invoiceErr?.code === '42P01' || String(invoiceErr?.message || '').toLowerCase().includes('does not exist')
  if (invoiceErr && !invoiceMissing) {
    return { ok: false, error: `invoice_upsert_failed:${invoiceErr.message}` }
  }
  const totalOutstandingForLock = invoiceMissing
    ? roundMoney(existingFallbackOutstanding + outstandingAmount)
    : outstandingAmount
  const shouldSuspend = !billing.is_free && (totalOutstandingForLock > 0 || nextBalance <= 0)
  const billingUpdatePayload: Record<string, unknown> = {
    balance: nextBalance,
    carry_loss: newCarryLoss,
    suspended: shouldSuspend,
    grace_deadline: shouldSuspend ? new Date().toISOString() : null,
    updated_at: new Date().toISOString(),
  }
  if (invoiceMissing) {
    billingUpdatePayload.bot_config = {
      ...baseBotConfig,
      outstanding_fee_amount: totalOutstandingForLock,
      last_outstanding_settle_date: dateStr,
    }
  }
  const { error: billingErr } = await admin.from('billing').update(billingUpdatePayload).eq('user_id', userId)
  if (billingErr) {
    return { ok: false, error: `billing_update_failed:${billingErr.message}` }
  }

  if (dynamicReferralEnabled && referrerId && referrerFeeAmount > 0) {
    await admin.from('referral_commissions').insert({
      referrer_id: referrerId,
      referred_id: userId,
      charge_amount: feeAmount,
      commission_rate: referrerShareRate,
      commission_amount: referrerFeeAmount,
      date: dateStr,
    })
  }

  if (feeAmount > 0) {
    await sendTelegram(
      `<b>Daily Profit Settlement</b>\n` +
      `Date: ${dateStr}\n` +
      `User: ${userEmail || userId}\n` +
      `PnL Source: ${pnlSource}\n` +
      `Daily Profit: <b>$${roundMoney(dailyProfit).toFixed(2)}</b>\n` +
      `Carry Loss: $${carryLoss.toFixed(2)}\n` +
      `Net Profit: <b>$${netProfit.toFixed(2)}</b>\n` +
      `Operator Fee: <b>$${feeAmount.toFixed(2)}</b>\n` +
      (referrerFeeAmount > 0 ? `Referrer Fee: $${referrerFeeAmount.toFixed(2)}\n` : '') +
      `Paid from Balance: $${paidFromBalance.toFixed(2)}\n` +
      `Outstanding: <b>$${outstandingAmount.toFixed(2)}</b>\n` +
      `Balance After: <b>$${nextBalance.toFixed(2)}</b>\n` +
      `Status: ${invoiceStatus}${shouldSuspend ? ' (LOCKED)' : ''}`
    )
  }
  if (customerTelegramChatId) {
    await sendCustomerTelegramMessage(
      customerTelegramChatId,
      `<b>日次精算</b>\n` +
      `日付: ${dateStr}\n` +
      `日次損益: <b>${roundMoney(dailyProfit) >= 0 ? '+' : ''}$${roundMoney(dailyProfit).toFixed(2)}</b>\n` +
      `精算対象: $${netProfit.toFixed(2)}\n` +
      `手数料: <b>$${feeAmount.toFixed(2)}</b>\n` +
      `未払い: <b>$${outstandingAmount.toFixed(2)}</b>\n` +
      `残高: <b>$${nextBalance.toFixed(2)}</b>\n` +
      `状態: ${shouldSuspend ? '停止中（要入金）' : '有効'}`
    )
  }

  return {
    ok: true,
    feeAmount,
    referrerFeeAmount,
    outstandingAmount,
    newBalance: nextBalance,
    suspended: shouldSuspend,
    pnlSource,
    invoiceStatus,
  }
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

  const userIds = (billings || []).map(b => String(b.user_id || '')).filter(Boolean)
  const { data: profiles } = userIds.length > 0
    ? await admin.from('profiles').select('id,email').in('id', userIds)
    : { data: [] as Array<{ id: string; email: string }> }
  const emailByUserId = new Map<string, string>()
  for (const p of (profiles || [])) {
    emailByUserId.set(String(p.id), String(p.email || '').toLowerCase())
  }

  const masterPnl = await fetchMasterDailyPnlByEmail(dateStr)
  const pnlSourceCounts: Record<string, number> = {}

  let settled = 0
  let skipped = 0
  const skipReasons: Record<string, number> = {}
  const errors: Array<{ user_id: string; error: string }> = []

  for (const b of billings) {
    if (b.is_free) { skipped++; skipReasons['free'] = (skipReasons['free'] || 0) + 1; continue }
    const userEmail = emailByUserId.get(String(b.user_id)) || ''

    let dailyProfit: number | null = null
    let pnlSource: PnlSource = 'session_state'

    if (userEmail && masterPnl.map.has(userEmail)) {
      dailyProfit = Number(masterPnl.map.get(userEmail) || 0)
      pnlSource = 'master_executor_daily_pnl'
    }

    if (dailyProfit === null) {
    const ss = b.session_state as Record<string, unknown> | null
    if (!ss || typeof ss !== 'object') {
      skipped++
      skipReasons['no_session_state'] = (skipReasons['no_session_state'] || 0) + 1
      continue
    }
    const daily_open = ss.daily_open as { date?: string; balance?: number } | undefined
    const current_balance = typeof ss.current_balance === 'number' ? ss.current_balance : null
    const last_balance_at = typeof ss.last_balance_at === 'string' ? ss.last_balance_at : null
    const daily_open_balance = typeof daily_open?.balance === 'number' ? daily_open.balance : null
    const daily_open_date = typeof daily_open?.date === 'string' ? daily_open.date : ''
    if (daily_open_balance === null || current_balance === null || !last_balance_at) {
      skipped++
      skipReasons['incomplete_state'] = (skipReasons['incomplete_state'] || 0) + 1
      continue
    }
    // daily_open.date がある場合は settle 対象日 (昨日) と一致しているかチェック
    if (daily_open_date && daily_open_date !== dateStr) {
      skipped++
      skipReasons['stale_daily_open'] = (skipReasons['stale_daily_open'] || 0) + 1
      continue
    }
    // last_balance_at は settle 対象日 (昨日/JST) であることを要求
    const lastBalanceDateJst = new Date(last_balance_at).toLocaleDateString('en-CA', { timeZone: 'Asia/Tokyo' })
    if (!lastBalanceDateJst || lastBalanceDateJst !== dateStr) {
      skipped++
      skipReasons['stale_balance'] = (skipReasons['stale_balance'] || 0) + 1
      continue
    }
      dailyProfit = roundMoney(current_balance - daily_open_balance)
      pnlSource = 'session_state'
    }

    const result = await settleUser(admin, b.user_id, userEmail, dailyProfit, dateStr, pnlSource)
    if (result.ok) {
      settled++
      pnlSourceCounts[pnlSource] = (pnlSourceCounts[pnlSource] || 0) + 1
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
    pnlSourceCounts,
    masterPnl: {
      enabled: masterPnl.enabled,
      matchedUsers: masterPnl.map.size,
      error: masterPnl.error || null,
    },
    errors: errors.slice(0, 10),
  })
}

export async function POST(req: NextRequest) {
  const { api_key, email, date, net_profit, pnl_source } = await req.json()
  if (api_key !== process.env.LAPLACE_API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }
  if (!email) return NextResponse.json({ error: 'Missing email' }, { status: 400 })
  if (typeof net_profit !== 'number') {
    return NextResponse.json({ error: 'Missing net_profit' }, { status: 400 })
  }

  const admin = createAdminClient()
  const { data: profile } = await admin.from('profiles').select('id,email').eq('email', email).single()
  if (!profile) return NextResponse.json({ error: 'User not found' }, { status: 404 })

  const dateStr = date || getJstDateString()
  const source: PnlSource = pnl_source === 'master_executor_daily_pnl' || pnl_source === 'session_state'
    ? pnl_source
    : 'manual_api'
  const result = await settleUser(admin, profile.id, String(profile.email || ''), Number(net_profit), dateStr, source)
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 409 })
  }

  return NextResponse.json({
    ok: true,
    date: dateStr,
    fee_amount: result.feeAmount,
    outstanding_amount: result.outstandingAmount,
    invoice_status: result.invoiceStatus,
    balance: result.newBalance,
    suspended: result.suspended,
  })
}
