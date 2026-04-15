import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

const API_KEY = process.env.LAPLACE_API_KEY || ''

export async function POST(req: NextRequest) {
  const body = await req.json()
  if (body.api_key !== API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }

  const admin = createAdminClient()
  const patterns = body.patterns || []
  const today = new Date().toISOString().split('T')[0]

  for (const p of patterns) {
    await admin.from('pattern_winrates').upsert({
      pattern_hash: p.pattern,
      window_size: 10,
      win_rate: p.win_rate,
      samples: p.samples,
      last_updated: today,
    }, { onConflict: 'pattern_hash' })
  }

  return NextResponse.json({ ok: true, count: patterns.length })
}

export async function GET(req: NextRequest) {
  const url = new URL(req.url)
  const apiKey = url.searchParams.get('api_key')
  if (apiKey !== API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }

  const minWr = parseFloat(url.searchParams.get('min_wr') || '52')
  const admin = createAdminClient()
  const { data, error } = await admin
    .from('pattern_winrates')
    .select('*')
    .gte('win_rate', minWr)
    .order('win_rate', { ascending: false })
    .limit(100)

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 })
  }
  return NextResponse.json({ patterns: data })
}
