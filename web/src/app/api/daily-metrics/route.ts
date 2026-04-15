import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

const API_KEY = process.env.LAPLACE_API_KEY || ''

export async function POST(req: NextRequest) {
  const body = await req.json()
  if (body.api_key !== API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }

  const admin = createAdminClient()
  const { date, metrics } = body

  const { error } = await admin.from('daily_metrics').upsert({
    date,
    tereko_rate: metrics.tereko_rate,
    avg_duration: metrics.avg_duration,
    counter_wr: metrics.counter_wr,
    short5h_rate: metrics.short5h_rate,
    best_hour: metrics.best_hour,
    worst_hour: metrics.worst_hour,
    best_wr: metrics.best_wr,
    worst_wr: metrics.worst_wr,
    total_shoes: metrics.total_shoes,
    total_hands: metrics.total_hands,
    updated_at: new Date().toISOString(),
  }, { onConflict: 'date' })

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 })
  }
  return NextResponse.json({ ok: true })
}

export async function GET(req: NextRequest) {
  const url = new URL(req.url)
  const apiKey = url.searchParams.get('api_key')
  if (apiKey !== API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }

  const days = parseInt(url.searchParams.get('days') || '7')
  const admin = createAdminClient()
  const { data, error } = await admin
    .from('daily_metrics')
    .select('*')
    .order('date', { ascending: false })
    .limit(days)

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 })
  }
  return NextResponse.json({ metrics: data })
}
