import { createClient } from '@/lib/supabase-server'
import { NextRequest, NextResponse } from 'next/server'

async function sendTelegram(message: string) {
  const token = process.env.ADMIN_TELEGRAM_BOT_TOKEN
  const chatId = process.env.ADMIN_TELEGRAM_CHAT_ID
  if (!token || !chatId) return
  try {
    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text: message, parse_mode: 'HTML' }),
    })
  } catch {}
}

export async function POST(req: NextRequest) {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const { message } = await req.json()
  if (!message?.trim()) return NextResponse.json({ error: 'Empty message' }, { status: 400 })

  const { error } = await supabase.from('support_tickets').insert({
    user_id: user.id,
    message: message.trim(),
  })

  if (error) return NextResponse.json({ error: error.message }, { status: 500 })

  await sendTelegram(
    `📩 <b>New Support Ticket</b>\n\nFrom: ${user.email}\n\n${message.trim()}`
  )

  return NextResponse.json({ ok: true })
}
