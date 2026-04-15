import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

const API_KEY = process.env.LAPLACE_API_KEY || ''

const DEFAULT_PARAMS = {
  entry_window: 15,
  entry_threshold: 0.85,
  exit_drop3_limit: 2,
  exit_drop5_immediate: true,
  profit_target: 30,
  status: 'default',
}

export async function GET(req: NextRequest) {
  const url = new URL(req.url)
  const apiKey = url.searchParams.get('api_key')
  if (apiKey !== API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }

  const admin = createAdminClient()
  let data: any[] | null = null
  let error: any = null

  const history = await admin
    .from('optimal_params_history')
    .select('*')
    .order('updated_at', { ascending: false })
    .limit(3)

  if (!history.error && history.data && history.data.length > 0) {
    data = history.data
  } else {
    error = history.error
    const fallback = await admin
      .from('optimal_params')
      .select('*')
      .order('updated_at', { ascending: false })
      .limit(3)
    if (!fallback.error && fallback.data && fallback.data.length > 0) {
      data = fallback.data
      error = null
    } else {
      error = fallback.error || error
    }
  }

  if (!data || data.length === 0) {
    return NextResponse.json({ candidates: [{ ...DEFAULT_PARAMS }] })
  }

  const candidates = data.map((row, idx) => ({
    rank: idx + 1,
    entry_window: row.entry_window ?? DEFAULT_PARAMS.entry_window,
    entry_threshold: row.entry_threshold ?? DEFAULT_PARAMS.entry_threshold,
    exit_drop3_limit: row.exit_drop3_limit ?? DEFAULT_PARAMS.exit_drop3_limit,
    exit_drop5_immediate: row.exit_drop5_immediate ?? DEFAULT_PARAMS.exit_drop5_immediate,
    profit_target: row.profit_target ?? DEFAULT_PARAMS.profit_target,
    status: row.status ?? 'active',
    reason: row.reason ?? '',
    updated_at: row.updated_at ?? null,
  }))

  return NextResponse.json({ candidates })
}
