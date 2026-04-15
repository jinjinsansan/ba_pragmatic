import { createAdminClient } from '@/lib/supabase-admin'
import { createClient as createServerSupabase } from '@/lib/supabase-server'
import { NextRequest, NextResponse } from 'next/server'

export async function POST(req: NextRequest) {
  const supabase = await createServerSupabase()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const { amount, promoCode, network } = await req.json()
  if (!network || !['TRC-20', 'ERC-20'].includes(network)) return NextResponse.json({ error: 'Invalid network' }, { status: 400 })

  const admin = createAdminClient()
  let chargeAmount = parseFloat(amount) || 0
  let status = 'pending'
  let isFree = false

  if (promoCode) {
    const { data: promo } = await admin
      .from('promo_codes')
      .select('*')
      .eq('code', promoCode.toUpperCase())
      .eq('active', true)
      .single()

    if (!promo || promo.used_count >= promo.max_uses) {
      return NextResponse.json({ error: 'Invalid or expired promo code' }, { status: 400 })
    }

    if (promo.type === 'charge_free') {
      chargeAmount = 0
      status = 'confirmed'
      isFree = true
    } else if (promo.type === 'discount') {
      chargeAmount = Math.round(chargeAmount * (1 - promo.discount_percent / 100))
    }

    await admin.from('promo_codes').update({ used_count: promo.used_count + 1 }).eq('id', promo.id)
  } else {
    if (chargeAmount < 100) return NextResponse.json({ error: 'Minimum charge is $100' }, { status: 400 })
  }

  const { error } = await admin.from('charges').insert({
    user_id: user.id,
    amount: chargeAmount,
    promo_code: promoCode?.toUpperCase() || null,
    status,
    usdt_network: network,
  })

  if (error) return NextResponse.json({ error: error.message }, { status: 500 })

  if (isFree) {
    await admin.from('billing').upsert({
      user_id: user.id,
      is_free: true,
      balance: 99999,
      updated_at: new Date().toISOString(),
    }, { onConflict: 'user_id' })
  }

  return NextResponse.json({ ok: true, amount: chargeAmount, isFree })
}
