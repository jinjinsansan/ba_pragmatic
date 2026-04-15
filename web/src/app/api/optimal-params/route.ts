import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

const API_KEY = process.env.LAPLACE_API_KEY || ''

export async function GET(req: NextRequest) {
  const url = new URL(req.url)
  const apiKey = url.searchParams.get('api_key')
  if (apiKey !== API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }

  const admin = createAdminClient()
  const { data, error } = await admin
    .from('optimal_params')
    .select('*')
    .eq('id', 1)
    .single()

  if (error || !data) {
    // Return defaults if table is empty
    return NextResponse.json({
      params: {
        entry_window: 15,
        entry_threshold: 0.85,
        exit_drop3_limit: 2,
        exit_drop5_immediate: true,
        profit_target: 30,
        status: 'default',
      }
    })
  }
  return NextResponse.json({ params: data })
}

export async function POST(req: NextRequest) {
  const body = await req.json()
  if (body.api_key !== API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }

  const admin = createAdminClient()
  const { error } = await admin.from('optimal_params').upsert({
    id: 1,
    entry_window: body.entry_window,
    entry_threshold: body.entry_threshold,
    exit_drop3_limit: body.exit_drop3_limit,
    exit_drop5_immediate: body.exit_drop5_immediate,
    profit_target: body.profit_target,
    status: body.status || 'active',
    reason: body.reason || 'manual_update',
    updated_at: new Date().toISOString(),
  })

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 })
  }

  await admin.from('optimal_params_history').insert({
    entry_window: body.entry_window,
    entry_threshold: body.entry_threshold,
    exit_drop3_limit: body.exit_drop3_limit,
    exit_drop5_immediate: body.exit_drop5_immediate,
    profit_target: body.profit_target,
    status: body.status || 'active',
    reason: body.reason || 'manual_update',
    updated_at: new Date().toISOString(),
  })
  return NextResponse.json({ ok: true })
}
