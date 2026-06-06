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
  if (!userId) return NextResponse.json({ error: 'Missing userId' }, { status: 400 })
  const admin = createAdminClient()

  function parseRate(v: unknown) {
    const n = Number(v)
    if (!Number.isFinite(n)) return null
    if (n < 0 || n > 1) return null
    const pct = Math.round(n * 100)
    if (pct % 10 !== 0) return null
    return pct / 100
  }

  // すべての billing upsert で error を必ず確認する。以前は free_charge 等が
  // upsert の結果を無視して ok:true を返していたため、DB 書き込みが失敗しても
  // 管理者には成功に見え「設定しても無料にならない（何も変わらない）」という
  // サイレント失敗になっていた(billing 行が無い新規ユーザの INSERT で顕在化)。
  async function setBilling(patch: Record<string, unknown>) {
    return admin.from('billing').upsert(
      { user_id: userId, updated_at: new Date().toISOString(), ...patch },
      { onConflict: 'user_id' },
    )
  }
  function fail(error: { message?: string } | null) {
    return NextResponse.json({ error: error?.message || 'billing upsert failed' }, { status: 500 })
  }

  switch (action) {
    case 'suspend': {
      const { error } = await setBilling({ suspended: true })
      if (error) return fail(error)
      break
    }
    case 'unsuspend': {
      const { error } = await setBilling({ suspended: false })
      if (error) return fail(error)
      break
    }
    case 'set_rate': {
      const parsed = parseRate(value)
      if (parsed === null) return NextResponse.json({ error: 'profit_share_rate must be 0-1 in 10% steps' }, { status: 400 })
      const { error } = await setBilling({ profit_share_rate: parsed })
      if (error) return fail(error)
      break
    }
    case 'set_referrer_rate': {
      const parsed = parseRate(value)
      if (parsed === null) return NextResponse.json({ error: 'referrer_share_rate must be 0-1 in 10% steps' }, { status: 400 })
      const upsert = await setBilling({ referrer_share_rate: parsed })
      if (upsert.error?.code === '42703' || String(upsert.error?.message || '').toLowerCase().includes('does not exist')) {
        // 旧スキーマ(referrer_share_rate 列なし)へのフォールバック: bot_config に格納
        const { data: currentBilling } = await admin.from('billing').select('bot_config').eq('user_id', userId).maybeSingle()
        const currentConfig = currentBilling?.bot_config && typeof currentBilling.bot_config === 'object' ? currentBilling.bot_config : {}
        const nextConfig = { ...(currentConfig as Record<string, unknown>), referrer_share_rate: parsed }
        const { error } = await setBilling({ bot_config: nextConfig })
        if (error) return fail(error)
      } else if (upsert.error) {
        return fail(upsert.error)
      }
      break
    }
    case 'free_license': {
      const { error } = await setBilling({ bot_paid: true, suspended: false })
      if (error) return fail(error)
      break
    }
    case 'free_charge': {
      const { error } = await setBilling({ is_free: true, balance: 99999, suspended: false })
      if (error) return fail(error)
      break
    }
    case 'free_both': {
      const { error } = await setBilling({ bot_paid: true, is_free: true, balance: 99999, suspended: false })
      if (error) return fail(error)
      break
    }
    case 'unfree_charge': {
      const { data: billing } = await admin.from('billing').select('balance').eq('user_id', userId).maybeSingle()
      const resetBalance = (billing?.balance || 0) >= 99999
      const { error } = await setBilling({ is_free: false, balance: resetBalance ? 0 : (billing?.balance || 0) })
      if (error) return fail(error)
      break
    }
    case 'activate': {
      const { error } = await setBilling({ suspended: false })
      if (error) return fail(error)
      break
    }
    case 'deactivate': {
      const { error } = await setBilling({ suspended: true })
      if (error) return fail(error)
      break
    }
    case 'set_bot_config': {
      const { error } = await setBilling({ bot_config: value })
      if (error) return fail(error)
      break
    }
    default:
      return NextResponse.json({ error: 'Unknown action' }, { status: 400 })
  }

  return NextResponse.json({ ok: true })
}
