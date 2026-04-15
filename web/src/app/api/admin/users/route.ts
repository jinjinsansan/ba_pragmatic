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

  switch (action) {
    case 'suspend':
      await admin.from('billing').upsert({ user_id: userId, suspended: true, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
      break
    case 'unsuspend':
      await admin.from('billing').upsert({ user_id: userId, suspended: false, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
      break
    case 'set_rate':
      await admin.from('billing').upsert({ user_id: userId, profit_share_rate: value, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
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
      break
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
