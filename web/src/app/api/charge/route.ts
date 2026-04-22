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

  const requestedAmount = chargeAmount
  let finalAmount = requestedAmount
  // Unique-amount scheme (no TXID required): if amount is an integer, add unique cents (1..99).
  // This lets the admin identify deposits in a single shared wallet by amount.
  if (!isFree && status === 'pending' && Number.isFinite(requestedAmount) && requestedAmount > 0) {
    const baseCents = Math.round(requestedAmount * 100)
    if (baseCents % 100 === 0) {
      const seen = new Set<number>()
      for (let i = 0; i < 99; i++) {
        const cents = 1 + Math.floor(Math.random() * 99)
        if (seen.has(cents)) continue
        seen.add(cents)
        const candidate = Number(((baseCents + cents) / 100).toFixed(2))
        const { data: exists } = await admin
          .from('charges')
          .select('id')
          .eq('status', 'pending')
          .eq('usdt_network', network)
          .eq('amount', candidate)
          .gte('created_at', new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString())
          .limit(1)
        if (!exists || exists.length === 0) {
          finalAmount = candidate
          break
        }
      }
      if (finalAmount === requestedAmount) {
        // Extremely unlikely; fall back to +$0.01 even if collision.
        finalAmount = Number(((baseCents + 1) / 100).toFixed(2))
      }
    }
  }

  const { error } = await admin.from('charges').insert({
    user_id: user.id,
    amount: finalAmount,
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

  return NextResponse.json({ ok: true, amount: finalAmount, requestedAmount, isFree })
}
