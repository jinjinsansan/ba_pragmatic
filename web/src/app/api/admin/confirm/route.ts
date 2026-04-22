import { createAdminClient } from '@/lib/supabase-admin'
import { createClient as createServerSupabase } from '@/lib/supabase-server'
import { NextRequest, NextResponse } from 'next/server'

export async function POST(req: NextRequest) {
  const serverSupabase = await createServerSupabase()
  const { data: { user } } = await serverSupabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const { data: profile } = await serverSupabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) return NextResponse.json({ error: 'Forbidden' }, { status: 403 })

  const { type, id, userId, amount } = await req.json()
  const admin = createAdminClient()

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
      .select('balance, total_charged')
      .eq('user_id', chargeUserId)
      .single()

    const newBalance = (Number(billing?.balance || 0) || 0) + chargeAmount
    const newTotal = (Number(billing?.total_charged || 0) || 0) + chargeAmount

    // Charge confirmation should immediately re-enable the bot.
    await admin.from('billing').upsert({
      user_id: chargeUserId,
      balance: newBalance,
      total_charged: newTotal,
      suspended: false,
      grace_deadline: null,
      updated_at: new Date().toISOString(),
    }, { onConflict: 'user_id' })

    // Referral commission
    const { data: userProfile } = await admin.from('profiles').select('referred_by').eq('id', chargeUserId).single()
    if (userProfile?.referred_by) {
      const { data: referrer } = await admin.from('profiles').select('id').eq('referral_code', userProfile.referred_by).single()
      if (referrer) {
        const { data: referrerBilling } = await admin.from('billing').select('profit_share_rate').eq('user_id', referrer.id).single()
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
  } else if (type === 'deliver') {
    await admin.from('orders').update({ status: 'delivered' }).eq('id', id)
  }

  return NextResponse.json({ ok: true })
}
