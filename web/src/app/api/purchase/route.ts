import { createAdminClient } from '@/lib/supabase-admin'
import { createClient as createServerSupabase } from '@/lib/supabase-server'
import { NextRequest, NextResponse } from 'next/server'

const PLANS: Record<string, number> = { standard: 2000 }

export async function POST(req: NextRequest) {
  const supabase = await createServerSupabase()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const { plan, promoCode, network } = await req.json()
  if (!plan || !PLANS[plan]) return NextResponse.json({ error: 'Invalid plan' }, { status: 400 })
  if (!network || !['TRC-20', 'ERC-20'].includes(network)) return NextResponse.json({ error: 'Invalid network' }, { status: 400 })

  const admin = createAdminClient()
  let amount = PLANS[plan]
  let status = 'pending'

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

    if (promo.type === 'package_free') {
      amount = 0
      status = 'confirmed'
    } else if (promo.type === 'discount') {
      amount = Math.round(amount * (1 - promo.discount_percent / 100))
    }

    await admin.from('promo_codes').update({ used_count: promo.used_count + 1 }).eq('id', promo.id)
  }

  const { data: order, error } = await admin.from('orders').insert({
    user_id: user.id,
    plan,
    amount,
    promo_code: promoCode?.toUpperCase() || null,
    status,
    usdt_network: network,
  }).select().single()

  if (error) return NextResponse.json({ error: error.message }, { status: 500 })

  if (status === 'confirmed') {
    await admin.from('billing').upsert({
      user_id: user.id,
      bot_paid: true,
      profit_share_rate: 0.20,
    }, { onConflict: 'user_id' })
  }

  return NextResponse.json({ ok: true, orderId: order.id, amount, isFree: amount === 0 })
}
