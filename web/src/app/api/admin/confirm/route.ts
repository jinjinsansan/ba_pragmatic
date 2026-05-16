import { createAdminClient } from '@/lib/supabase-admin'
import { createClient as createServerSupabase } from '@/lib/supabase-server'
import { sendCustomerTelegramMessage } from '@/lib/customer-telegram'
import { NextRequest, NextResponse } from 'next/server'

function roundMoney(n: number) {
  return Math.round((Number(n) || 0) * 100) / 100
}

async function consumeUnpaidInvoices(admin: ReturnType<typeof createAdminClient>, userId: string, startingBalance: number) {
  let remaining = roundMoney(startingBalance)
  let totalOutstanding = 0
  let totalPaid = 0

  const { data: invoices, error: listErr } = await admin
    .from('daily_profit_invoices')
    .select('id, outstanding_amount, paid_amount, status')
    .eq('user_id', userId)
    .eq('status', 'unpaid')
    .gt('outstanding_amount', 0)
    .order('settle_date', { ascending: true })

  if (listErr) {
    // Migration not yet applied in some environments.
    if (listErr.code === '42P01' || String(listErr.message || '').toLowerCase().includes('does not exist')) {
      const { data: billingFallback } = await admin
        .from('billing')
        .select('bot_config')
        .eq('user_id', userId)
        .maybeSingle()
      const fallbackOutstanding = roundMoney(Number((billingFallback as any)?.bot_config?.outstanding_fee_amount || 0))
      const fallbackPaid = roundMoney(Math.min(remaining, fallbackOutstanding))
      const fallbackRemainingOutstanding = roundMoney(Math.max(0, fallbackOutstanding - fallbackPaid))
      return {
        remainingBalance: roundMoney(remaining - fallbackPaid),
        totalOutstanding: fallbackRemainingOutstanding,
        totalPaid: fallbackPaid,
        usedFallback: true,
      }
    }
    throw listErr
  }

  for (const invoice of (invoices || [])) {
    const outstanding = roundMoney(Number(invoice.outstanding_amount) || 0)
    if (outstanding <= 0) continue
    totalOutstanding = roundMoney(totalOutstanding + outstanding)
    if (remaining <= 0) continue

    const pay = roundMoney(Math.min(remaining, outstanding))
    const nextOutstanding = roundMoney(outstanding - pay)
    const nextPaid = roundMoney((Number(invoice.paid_amount) || 0) + pay)
    remaining = roundMoney(remaining - pay)
    totalPaid = roundMoney(totalPaid + pay)

    await admin
      .from('daily_profit_invoices')
      .update({
        paid_amount: nextPaid,
        outstanding_amount: nextOutstanding,
        status: nextOutstanding > 0 ? 'unpaid' : 'paid',
        updated_at: new Date().toISOString(),
      })
      .eq('id', invoice.id)
  }

  const outstandingAfter = roundMoney(Math.max(0, totalOutstanding - totalPaid))
  return { remainingBalance: remaining, totalOutstanding: outstandingAfter, totalPaid, usedFallback: false }
}

export async function POST(req: NextRequest) {
  const serverSupabase = await createServerSupabase()
  const { data: { user } } = await serverSupabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const { data: profile } = await serverSupabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) return NextResponse.json({ error: 'Forbidden' }, { status: 403 })

  const { type, id, userId, amount } = await req.json()
  const admin = createAdminClient()
  const dynamicReferralEnabled = String(process.env.LAPLACE_ENABLE_DYNAMIC_REFERRAL_SPLIT || '0') === '1'
  const chargeReferralEnabled = String(process.env.LAPLACE_ENABLE_CHARGE_REFERRAL || '1') === '1'

  if (type === 'order') {
    await admin.from('orders').update({ status: 'confirmed', confirmed_at: new Date().toISOString() }).eq('id', id)
    await admin.from('billing').upsert({
      user_id: userId,
      bot_paid: true,
      profit_share_rate: 0.20,
    }, { onConflict: 'user_id' })
  } else if (type === 'charge') {
    // Don't trust client-provided amount/userId: load from DB to prevent mistakes.
    const { data: charge, error: chargeErr } = await admin
      .from('charges')
      .select('user_id, amount, status')
      .eq('id', id)
      .single()
    if (chargeErr || !charge) {
      return NextResponse.json({ error: 'Charge not found' }, { status: 404 })
    }
    const chargeUserId = charge.user_id
    const chargeAmount = Number(charge.amount || 0) || 0

    if (charge.status !== 'confirmed') {
      await admin.from('charges').update({ status: 'confirmed', confirmed_at: new Date().toISOString() }).eq('id', id)
    }

    const { data: billing } = await admin
      .from('billing')
      .select('balance, total_charged, bot_config')
      .eq('user_id', chargeUserId)
      .single()

    const toppedUpBalance = roundMoney((Number(billing?.balance || 0) || 0) + chargeAmount)
    const newTotal = roundMoney((Number(billing?.total_charged || 0) || 0) + chargeAmount)
    const invoiceResult = await consumeUnpaidInvoices(admin, chargeUserId, toppedUpBalance)
    const finalBalance = roundMoney(invoiceResult.remainingBalance)
    const shouldSuspend = invoiceResult.totalOutstanding > 0

    const billingPayload: Record<string, any> = {
      user_id: chargeUserId,
      balance: finalBalance,
      total_charged: newTotal,
      suspended: shouldSuspend ? true : false,
      grace_deadline: shouldSuspend ? new Date().toISOString() : null,
      updated_at: new Date().toISOString(),
    }
    if (invoiceResult.usedFallback) {
      const currentBotConfig = billing?.bot_config && typeof billing.bot_config === 'object' ? billing.bot_config : {}
      billingPayload.bot_config = {
        ...(currentBotConfig as Record<string, unknown>),
        outstanding_fee_amount: roundMoney(invoiceResult.totalOutstanding),
      }
    }

    await admin.from('billing').upsert(billingPayload, { onConflict: 'user_id' })
    const customerChatId = String(
      (billingPayload.bot_config?.customer_telegram_chat_id ?? (billing as any)?.bot_config?.customer_telegram_chat_id) || ''
    ).trim()
    if (customerChatId) {
      await sendCustomerTelegramMessage(
        customerChatId,
        `<b>入金反映完了</b>\n` +
        `反映額: <b>$${chargeAmount.toFixed(2)}</b>\n` +
        `未払い充当: $${roundMoney(invoiceResult.totalPaid).toFixed(2)}\n` +
        `未払い残: <b>$${roundMoney(invoiceResult.totalOutstanding).toFixed(2)}</b>\n` +
        `残高: <b>$${finalBalance.toFixed(2)}</b>\n` +
        `状態: ${shouldSuspend ? '停止中（未払いあり）' : '有効'}`
      )
    }

    // Legacy referral commission (charge-based 5%)
    // New PnL-based referral split is handled in /api/cron/settle when
    // LAPLACE_ENABLE_DYNAMIC_REFERRAL_SPLIT=1.
    if (!dynamicReferralEnabled && chargeReferralEnabled) {
      const { data: userProfile } = await admin.from('profiles').select('referred_by').eq('id', chargeUserId).single()
      if (userProfile?.referred_by) {
        const { data: referrer } = await admin.from('profiles').select('id').eq('referral_code', userProfile.referred_by).single()
        if (referrer) {
          const commissionRate = 0.05
          const commissionAmount = chargeAmount * commissionRate
          await admin.from('referral_commissions').insert({
            referrer_id: referrer.id,
            referred_id: chargeUserId,
            charge_amount: chargeAmount,
            commission_rate: commissionRate,
            commission_amount: commissionAmount,
          })
        }
      }
    }
  } else if (type === 'deliver') {
    await admin.from('orders').update({ status: 'delivered' }).eq('id', id)
  }

  return NextResponse.json({ ok: true })
}
