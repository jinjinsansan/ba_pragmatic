import { createAdminClient } from '@/lib/supabase-admin'
import { createClient as createServerSupabase } from '@/lib/supabase-server'
import { NextRequest, NextResponse } from 'next/server'

export async function POST(req: NextRequest) {
  const serverSupabase = await createServerSupabase()
  const { data: { user } } = await serverSupabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const { data: profile } = await serverSupabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) return NextResponse.json({ error: 'Forbidden' }, { status: 403 })

  const { userId, action, value } = await req.json()
  const admin = createAdminClient()

  function parseRate(v: unknown) {
    const n = Number(v)
    if (!Number.isFinite(n)) return null
    if (n < 0 || n > 1) return null
    const pct = Math.round(n * 100)
    if (pct % 10 !== 0) return null
    return pct / 100
  }

  switch (action) {
    case 'suspend':
      await admin.from('billing').upsert({ user_id: userId, suspended: true, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
      break
    case 'unsuspend':
      await admin.from('billing').upsert({ user_id: userId, suspended: false, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
      break
    case 'set_rate':
      {
        const parsed = parseRate(value)
        if (parsed === null) return NextResponse.json({ error: 'profit_share_rate must be 0-1 in 10% steps' }, { status: 400 })
        await admin.from('billing').upsert({ user_id: userId, profit_share_rate: parsed, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
      }
      break
    case 'set_referrer_rate':
      {
        const parsed = parseRate(value)
        if (parsed === null) return NextResponse.json({ error: 'referrer_share_rate must be 0-1 in 10% steps' }, { status: 400 })
        const upsert = await admin.from('billing').upsert(
          { user_id: userId, referrer_share_rate: parsed, updated_at: new Date().toISOString() },
          { onConflict: 'user_id' },
        )
        if (upsert.error?.code === '42703' || String(upsert.error?.message || '').toLowerCase().includes('does not exist')) {
          const { data: currentBilling } = await admin.from('billing').select('bot_config').eq('user_id', userId).maybeSingle()
          const currentConfig = currentBilling?.bot_config && typeof currentBilling.bot_config === 'object' ? currentBilling.bot_config : {}
          const nextConfig = { ...(currentConfig as Record<string, unknown>), referrer_share_rate: parsed }
          const fallback = await admin
            .from('billing')
            .upsert({ user_id: userId, bot_config: nextConfig, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
          if (fallback.error) {
            return NextResponse.json({ error: fallback.error.message }, { status: 500 })
          }
        } else if (upsert.error) {
          return NextResponse.json({ error: upsert.error.message }, { status: 500 })
        }
      }
      break
    case 'free_license':
      await admin.from('billing').upsert({
        user_id: userId,
        bot_paid: true,
        suspended: false,
        updated_at: new Date().toISOString(),
      }, { onConflict: 'user_id' })
      break
    case 'free_charge':
      await admin.from('billing').upsert({
        user_id: userId,
        is_free: true,
        balance: 99999,
        suspended: false,
        updated_at: new Date().toISOString(),
      }, { onConflict: 'user_id' })
      break
    case 'free_both':
      await admin.from('billing').upsert({
        user_id: userId,
        bot_paid: true,
        is_free: true,
        balance: 99999,
        suspended: false,
        updated_at: new Date().toISOString(),
      }, { onConflict: 'user_id' })
      break
    case 'unfree_charge': {
      const { data: billing } = await admin.from('billing').select('balance').eq('user_id', userId).single()
      const resetBalance = (billing?.balance || 0) >= 99999
      await admin.from('billing').upsert({
        user_id: userId,
        is_free: false,
        balance: resetBalance ? 0 : (billing?.balance || 0),
        updated_at: new Date().toISOString(),
      }, { onConflict: 'user_id' })
      break
    }
    case 'activate':
      await admin.from('billing').upsert({ user_id: userId, suspended: false, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
      break
    case 'deactivate':
      await admin.from('billing').upsert({ user_id: userId, suspended: true, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
      break
    case 'set_bot_config':
      await admin.from('billing').upsert({ user_id: userId, bot_config: value, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
      break
    default:
      return NextResponse.json({ error: 'Unknown action' }, { status: 400 })
  }

  return NextResponse.json({ ok: true })
}
