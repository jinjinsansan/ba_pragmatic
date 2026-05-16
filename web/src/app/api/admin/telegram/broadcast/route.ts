import { createAdminClient } from '@/lib/supabase-admin'
import { createClient as createServerSupabase } from '@/lib/supabase-server'
import { sendCustomerTelegramMessage } from '@/lib/customer-telegram'
import { NextRequest, NextResponse } from 'next/server'

function sessionStartTemplate(custom: string) {
  if (custom) return custom
  return [
    '📣 <b>セッション開始のお知らせ</b>',
    '',
    'マスターが新しいセッションを開始しました。',
    '受信側を起動して接続状況をご確認ください。',
  ].join('\n')
}

export async function POST(req: NextRequest) {
  const serverSupabase = await createServerSupabase()
  const { data: { user } } = await serverSupabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const { data: profile } = await serverSupabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) return NextResponse.json({ error: 'Forbidden' }, { status: 403 })

  const body = await req.json().catch(() => ({}))
  const type = String(body?.type || 'session_start')
  const customMessage = String(body?.message || '').trim()
  if (type !== 'session_start' && type !== 'custom') {
    return NextResponse.json({ error: 'Unsupported type' }, { status: 400 })
  }
  const message = type === 'session_start' ? sessionStartTemplate(customMessage) : customMessage
  if (!message) return NextResponse.json({ error: 'Message required' }, { status: 400 })

  const admin = createAdminClient()
  const { data: rows } = await admin
    .from('billing')
    .select('user_id, bot_config')
    .limit(5000)

  const uniqueChatIds = new Set<string>()
  for (const row of (rows || [])) {
    const chatId = String((row as any)?.bot_config?.customer_telegram_chat_id || '').trim()
    const enabled = (row as any)?.bot_config?.customer_telegram_enabled !== false
    if (chatId && enabled) uniqueChatIds.add(chatId)
  }

  let sent = 0
  let failed = 0
  for (const chatId of uniqueChatIds) {
    const ok = await sendCustomerTelegramMessage(chatId, message)
    if (ok) sent++
    else failed++
  }

  return NextResponse.json({
    ok: true,
    type,
    targets: uniqueChatIds.size,
    sent,
    failed,
  })
}
