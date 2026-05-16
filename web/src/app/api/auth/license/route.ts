import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

export async function POST(req: NextRequest) {
  const { email, api_key } = await req.json()

  if (api_key !== process.env.LAPLACE_API_KEY) {
    return NextResponse.json({ ok: false, reason: 'Invalid API key' }, { status: 401 })
  }
  if (!email) {
    return NextResponse.json({ ok: false, reason: 'Email required' }, { status: 400 })
  }

  const admin = createAdminClient()

  // メールからユーザーを検索
  const { data: profile, error: profileError } = await admin
    .from('profiles')
    .select('id, email, is_admin')
    .eq('email', email.toLowerCase().trim())
    .single()

  if (profileError || !profile) {
    return NextResponse.json({ ok: false, reason: 'Account not found. Please check your email.' })
  }

  // 管理者は無条件で通過
  if (profile.is_admin) {
    const { data: billing } = await admin.from('billing').select('bot_config, gui_state').eq('user_id', profile.id).single()
    const { data: deliverables } = await admin
      .from('deliverables')
      .select('file_path, version, created_at')
      .eq('user_id', profile.id)
      .order('created_at', { ascending: false })
      .limit(1)
    const deliverable = Array.isArray(deliverables) && deliverables.length ? deliverables[0] : null
    return NextResponse.json({
      ok: true,
      bot_config: billing?.bot_config || {},
      gui_state: billing?.gui_state || {},
      deliverable: deliverable ? {
        url: deliverable.file_path,
        version: deliverable.version,
        updated_at: deliverable.created_at,
      } : null,
    })
  }

  // サブスクリプション確認
  const { data: billing } = await admin
    .from('billing')
    .select('bot_paid, balance, suspended, is_free, bot_config, gui_state, expires_at')
    .eq('user_id', profile.id)
    .single()

  if (!billing) {
    return NextResponse.json({ ok: false, reason: 'No subscription found. Please purchase a plan at bafather.uk' })
  }

  if (billing.expires_at && new Date(billing.expires_at) < new Date()) {
    return NextResponse.json({ ok: false, reason: 'Your subscription has expired. Please renew at bafather.uk' })
  }

  if (!billing.bot_paid) {
    return NextResponse.json({ ok: false, reason: 'License not active. Please complete your purchase.' })
  }

  if (!billing.is_free) {
    const { data: unpaidInvoice, error: unpaidErr } = await admin
      .from('daily_profit_invoices')
      .select('outstanding_amount, settle_date')
      .eq('user_id', profile.id)
      .eq('status', 'unpaid')
      .gt('outstanding_amount', 0)
      .order('settle_date', { ascending: false })
      .limit(1)
      .maybeSingle()
    const unpaidMissingTable = unpaidErr?.code === '42P01' || String(unpaidErr?.message || '').toLowerCase().includes('does not exist')
    if (!unpaidMissingTable && unpaidInvoice && Number(unpaidInvoice.outstanding_amount) > 0) {
      return NextResponse.json({
        ok: false,
        reason: `Daily profit share payment is pending ($${Number(unpaidInvoice.outstanding_amount).toFixed(2)}). Please charge/pay before live betting.`,
      })
    }
    const fallbackOutstanding = Number((billing as any)?.bot_config?.outstanding_fee_amount || 0)
    if (fallbackOutstanding > 0) {
      return NextResponse.json({
        ok: false,
        reason: `Daily profit share payment is pending ($${fallbackOutstanding.toFixed(2)}). Please charge/pay before live betting.`,
      })
    }
    if (billing.suspended) {
      return NextResponse.json({ ok: false, reason: 'Your account is suspended. Please contact admin.' })
    }
    if ((billing.balance || 0) <= 0) {
      return NextResponse.json({ ok: false, reason: 'Balance is empty. Please charge to enable live betting.' })
    }
  }
  const { data: deliverables } = await admin
    .from('deliverables')
    .select('file_path, version, created_at')
    .eq('user_id', profile.id)
    .order('created_at', { ascending: false })
    .limit(1)
  const deliverable = Array.isArray(deliverables) && deliverables.length ? deliverables[0] : null
  return NextResponse.json({
    ok: true,
    bot_config: billing.bot_config || {},
    gui_state: billing.gui_state || {},
    deliverable: deliverable ? {
      url: deliverable.file_path,
      version: deliverable.version,
      updated_at: deliverable.created_at,
    } : null,
  })
}
