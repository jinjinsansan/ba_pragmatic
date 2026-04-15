import { createAdminClient } from '@/lib/supabase-admin'
import { createClient as createServerSupabase } from '@/lib/supabase-server'
import { NextRequest, NextResponse } from 'next/server'

export async function POST(req: NextRequest) {
  const serverSupabase = await createServerSupabase()
  const { data: { user } } = await serverSupabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const { data: profile } = await serverSupabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) return NextResponse.json({ error: 'Forbidden' }, { status: 403 })

  const { action, ...params } = await req.json()
  const admin = createAdminClient()

  switch (action) {
    case 'create':
      const { error } = await admin.from('promo_codes').insert({
        code: params.code,
        type: params.type,
        max_uses: params.max_uses,
        discount_percent: params.discount_percent,
        created_by: user.id,
      })
      if (error) return NextResponse.json({ error: error.message }, { status: 400 })
      break
    case 'activate':
      await admin.from('promo_codes').update({ active: true }).eq('id', params.id)
      break
    case 'deactivate':
      await admin.from('promo_codes').update({ active: false }).eq('id', params.id)
      break
    default:
      return NextResponse.json({ error: 'Unknown action' }, { status: 400 })
  }

  return NextResponse.json({ ok: true })
}
