import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

// =============================================================
// Master UI 家計簿 read-only API
// 認証: LAPLACE_API_KEY (Bearer) — bacopy executor / Master UI 用
// 返却: ledger_investor_summary 全件 + 現行 distribution rules
// =============================================================

export const dynamic = 'force-dynamic'
export const revalidate = 0

export async function GET(req: NextRequest) {
  const auth = req.headers.get('authorization') || ''
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : ''
  const expected = (process.env.LAPLACE_API_KEY || '').trim()
  if (!expected || token !== expected) {
    return NextResponse.json({ error: 'unauthorized' }, { status: 401 })
  }

  const admin = createAdminClient()

  const [summariesRes, rulesRes] = await Promise.all([
    admin.from('ledger_investor_summary').select('*').order('investor_name'),
    admin.from('ledger_distribution_rules').select('*').is('effective_to', null),
  ])

  if (summariesRes.error) {
    return NextResponse.json({ error: summariesRes.error.message }, { status: 500 })
  }

  return NextResponse.json({
    summaries: summariesRes.data || [],
    rules: rulesRes.data || [],
    fetched_at: new Date().toISOString(),
  })
}
