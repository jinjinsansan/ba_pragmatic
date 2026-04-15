import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

// GET: 推奨テーブル一覧取得（ユーザー固有 or 全体デフォルト）
export async function GET(req: NextRequest) {
  const email = req.nextUrl.searchParams.get('email')
  const api_key = req.nextUrl.searchParams.get('api_key')

  if (api_key !== process.env.LAPLACE_API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }

  const admin = createAdminClient()

  // email指定あり → ユーザー固有のリスト
  if (email) {
    const { data: profile } = await admin
      .from('profiles')
      .select('id')
      .eq('email', email)
      .single()

    if (profile) {
      const { data: billing } = await admin
        .from('billing')
        .select('recommended_tables')
        .eq('user_id', profile.id)
        .single()

      if (billing?.recommended_tables && Array.isArray(billing.recommended_tables) && billing.recommended_tables.length > 0) {
        return NextResponse.json({ tables: billing.recommended_tables, source: 'user' })
      }
    }
  }

  // フォールバック: 全体デフォルト（admin billingレコードから取得 or ハードコード）
  // 管理者のadmin=trueのアカウントを探してそのrecommended_tablesを使う
  const { data: adminProfile } = await admin
    .from('profiles')
    .select('id')
    .eq('is_admin', true)
    .limit(1)
    .single()

  if (adminProfile) {
    const { data: adminBilling } = await admin
      .from('billing')
      .select('recommended_tables')
      .eq('user_id', adminProfile.id)
      .single()

    if (adminBilling?.recommended_tables && Array.isArray(adminBilling.recommended_tables) && adminBilling.recommended_tables.length > 0) {
      return NextResponse.json({ tables: adminBilling.recommended_tables, source: 'admin-default' })
    }
  }

  // 最終フォールバック: ハードコード
  return NextResponse.json({
    tables: [
      { name: 'Japanese Speed Baccarat A', enabled: true, priority: 1 },
      { name: 'Korean Speed Baccarat B', enabled: true, priority: 2 },
    ],
    source: 'hardcoded-fallback'
  })
}

// POST: 推奨テーブル更新（VPS auto-update 用、admin 更新用）
export async function POST(req: NextRequest) {
  const { email, api_key, tables } = await req.json()

  if (api_key !== process.env.LAPLACE_API_KEY) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }
  if (!email || !Array.isArray(tables)) {
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
      { user_id: profile.id, recommended_tables: tables, updated_at: new Date().toISOString() },
      { onConflict: 'user_id' }
    )

  if (error) return NextResponse.json({ error: error.message }, { status: 500 })
  return NextResponse.json({ ok: true, count: tables.length })
}
