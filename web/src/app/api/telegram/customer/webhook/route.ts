import { createAdminClient } from '@/lib/supabase-admin'
import { sendCustomerTelegramMessage, verifyCustomerTelegramStartToken } from '@/lib/customer-telegram'
import { NextRequest, NextResponse } from 'next/server'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

function parseCommand(text: string) {
  const raw = String(text || '').trim()
  if (!raw.startsWith('/')) return { cmd: '', arg: '' }
  const [cmd, arg] = raw.split(/\s+/, 2)
  return { cmd: String(cmd || '').toLowerCase(), arg: String(arg || '') }
}

export async function POST(req: NextRequest) {
  const expectedSecret = String(process.env.CUSTOMER_TELEGRAM_WEBHOOK_SECRET || '').trim()
  if (expectedSecret) {
    const got = req.headers.get('x-telegram-bot-api-secret-token') || ''
    if (got !== expectedSecret) {
      return NextResponse.json({ ok: false, error: 'Unauthorized' }, { status: 401 })
    }
  }

  const update = await req.json().catch(() => null)
  const message = update?.message || update?.edited_message
  const chatId = message?.chat?.id
  const text = String(message?.text || '')
  if (!chatId || !text) return NextResponse.json({ ok: true, ignored: true })

  const { cmd, arg } = parseCommand(text)
  const admin = createAdminClient()

  if (cmd === '/start') {
    const verified = verifyCustomerTelegramStartToken(arg)
    if (!verified?.userId) {
      await sendCustomerTelegramMessage(chatId, '連携リンクが無効です。ダッシュボードから再度「Telegram連携」を押してください。')
      return NextResponse.json({ ok: true, linked: false, reason: 'invalid_token' })
    }

    const { data: billing } = await admin
      .from('billing')
      .select('bot_config')
      .eq('user_id', verified.userId)
      .maybeSingle()

    const currentConfig = billing?.bot_config && typeof billing.bot_config === 'object'
      ? (billing.bot_config as Record<string, unknown>)
      : {}

    const nextConfig: Record<string, unknown> = {
      ...currentConfig,
      customer_telegram_chat_id: String(chatId),
      customer_telegram_username: String(message?.from?.username || ''),
      customer_telegram_linked_at: new Date().toISOString(),
      customer_telegram_enabled: true,
    }

    await admin.from('billing').upsert({
      user_id: verified.userId,
      bot_config: nextConfig,
      updated_at: new Date().toISOString(),
    }, { onConflict: 'user_id' })

    await sendCustomerTelegramMessage(chatId, '連携が完了しました。今後、精算結果・未払い/入金反映・セッション開始通知を受け取れます。')
    return NextResponse.json({ ok: true, linked: true })
  }

  if (cmd === '/stop') {
    const chatIdStr = String(chatId)
    const { data: rows } = await admin
      .from('billing')
      .select('user_id, bot_config')
      .contains('bot_config', { customer_telegram_chat_id: chatIdStr })
      .limit(50)

    for (const row of (rows || [])) {
      const currentConfig = row?.bot_config && typeof row.bot_config === 'object'
        ? ({ ...(row.bot_config as Record<string, unknown>) })
        : {}
      delete currentConfig.customer_telegram_chat_id
      delete currentConfig.customer_telegram_username
      delete currentConfig.customer_telegram_linked_at
      currentConfig.customer_telegram_enabled = false
      await admin.from('billing').update({
        bot_config: currentConfig,
        updated_at: new Date().toISOString(),
      }).eq('user_id', row.user_id)
    }

    await sendCustomerTelegramMessage(chatId, '通知連携を解除しました。再開する場合はダッシュボードから再連携してください。')
    return NextResponse.json({ ok: true, unlinked: true, count: (rows || []).length })
  }

  await sendCustomerTelegramMessage(chatId, '使い方: ダッシュボードの「Telegram連携」から開くか、/stop で通知解除できます。')
  return NextResponse.json({ ok: true, ignored: true })
}
