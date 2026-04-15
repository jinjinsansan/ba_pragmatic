import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

// POST: Save GUI state
export async function POST(req: NextRequest) {
  const { email, api_key, gui_state } = await req.json()

  if (api_key !== process.env.LAPLACE_API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }
  if (!email || !gui_state) {
    return NextResponse.json({ error: 'Missing fields' }, { status: 400 })
  }

  const admin = createAdminClient()

  const { data: profile, error: profileError } = await admin
    .from('profiles')
    .select('id')
    .eq('email', email)
    .single()

  if (profileError || !profile) {
    return NextResponse.json({ error: 'User not found' }, { status: 404 })
  }

  const { error } = await admin
    .from('billing')
    .upsert(
      { user_id: profile.id, gui_state, updated_at: new Date().toISOString() },
      { onConflict: 'user_id' }
    )

  if (error) return NextResponse.json({ error: error.message }, { status: 500 })
  return NextResponse.json({ ok: true })
}

// GET: Load GUI state
export async function GET(req: NextRequest) {
  const email = req.nextUrl.searchParams.get('email')
  const api_key = req.nextUrl.searchParams.get('api_key')

  if (api_key !== process.env.LAPLACE_API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }
  if (!email) {
    return NextResponse.json({ error: 'Missing email' }, { status: 400 })
  }

  const admin = createAdminClient()

  const { data: profile, error: profileError } = await admin
    .from('profiles')
    .select('id')
    .eq('email', email)
    .single()

  if (profileError || !profile) {
    return NextResponse.json({ error: 'User not found' }, { status: 404 })
  }

  const { data: billing, error: billingError } = await admin
    .from('billing')
    .select('gui_state')
    .eq('user_id', profile.id)
    .single()

  if (billingError || !billing) {
    return NextResponse.json({ gui_state: {} })
  }

  return NextResponse.json({ gui_state: billing.gui_state || {} })
}
