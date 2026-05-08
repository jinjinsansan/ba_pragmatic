import { createAdminClient } from '@/lib/supabase-admin'
import { createClient as createServerSupabase } from '@/lib/supabase-server'
import { NextRequest, NextResponse } from 'next/server'

// =============================================================
// FX 運用家計簿 統合 API ルート
// 仕様: SPEC_FX_LEDGER.md §5.x
// 操作対象テーブル:
//   - ledger_account1_daily
//   - ledger_account2_daily
//   - ledger_expense_withdrawals
//   - ledger_distribution_rules
// 操作: upsert / delete
// =============================================================

export async function POST(req: NextRequest) {
  const serverSupabase = await createServerSupabase()
  const { data: { user } } = await serverSupabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const { data: profile } = await serverSupabase
    .from('profiles')
    .select('is_admin')
    .eq('id', user.id)
    .single()
  if (!profile?.is_admin) return NextResponse.json({ error: 'Forbidden' }, { status: 403 })

  const body = await req.json()
  const { table, action, payload, id } = body as {
    table: 'account1' | 'account2' | 'expense' | 'rule' | 'company_breakdown'
    action: 'upsert' | 'delete'
    payload?: any
    id?: string
  }

  const admin = createAdminClient()

  const tableMap = {
    account1: 'ledger_account1_daily',
    account2: 'ledger_account2_daily',
    expense: 'ledger_expense_withdrawals',
    rule: 'ledger_distribution_rules',
    company_breakdown: 'ledger_company_expense_breakdown',
  } as const
  const tableName = tableMap[table]
  if (!tableName) return NextResponse.json({ error: 'Unknown table' }, { status: 400 })

  if (action === 'delete') {
    if (!id) return NextResponse.json({ error: 'id required' }, { status: 400 })
    const { error } = await admin.from(tableName).delete().eq('id', id)
    if (error) return NextResponse.json({ error: error.message }, { status: 400 })
    return NextResponse.json({ ok: true })
  }

  if (action === 'upsert') {
    if (!payload) return NextResponse.json({ error: 'payload required' }, { status: 400 })

    let row: any
    let onConflict: string | undefined

    switch (table) {
      case 'account1':
        row = {
          investor_id: payload.investor_id,
          trade_date: payload.trade_date,
          daily_profit: Number(payload.daily_profit),
          notes: payload.notes ?? null,
        }
        onConflict = 'investor_id,trade_date'
        break
      case 'account2':
        row = {
          investor_id: payload.investor_id,
          trade_date: payload.trade_date,
          daily_profit: Number(payload.daily_profit),
          withdrawal: Number(payload.withdrawal ?? 0),
          notes: payload.notes ?? null,
        }
        onConflict = 'investor_id,trade_date'
        break
      case 'expense':
        row = {
          investor_id: payload.investor_id,
          withdrawal_date: payload.withdrawal_date,
          source_label: payload.source_label ?? null,
          withdraw_from_reserve: Number(payload.withdraw_from_reserve ?? 0),
          withdraw_from_account2: Number(payload.withdraw_from_account2 ?? 0),
          j_received: Number(payload.j_received ?? 0),
          k_received: Number(payload.k_received ?? 0),
          k_brother_received: Number(payload.k_brother_received ?? 0),
          company_received: Number(payload.company_received ?? 0),
          ai_dev_expense: Number(payload.ai_dev_expense ?? 0),
          notes: payload.notes ?? null,
        }
        if (payload.id) row.id = payload.id
        break
      case 'rule':
        row = {
          investor_id: payload.investor_id,
          investor_share_pct: Number(payload.investor_share_pct),
          j_share_pct: Number(payload.j_share_pct),
          k_share_pct: Number(payload.k_share_pct),
          company_share_pct: Number(payload.company_share_pct),
          effective_from: payload.effective_from,
          effective_to: payload.effective_to ?? null,
          notes: payload.notes ?? null,
        }
        break
      case 'company_breakdown':
        row = {
          investor_id: payload.investor_id,
          expense_date: payload.expense_date,
          category: payload.category,
          recipient: payload.recipient ?? null,
          amount: Number(payload.amount),
          notes: payload.notes ?? null,
        }
        if (payload.id) row.id = payload.id
        break
    }

    const query = onConflict
      ? admin.from(tableName).upsert(row, { onConflict })
      : admin.from(tableName).insert(row)

    const { error } = await query
    if (error) return NextResponse.json({ error: error.message }, { status: 400 })
    return NextResponse.json({ ok: true })
  }

  return NextResponse.json({ error: 'Unknown action' }, { status: 400 })
}
