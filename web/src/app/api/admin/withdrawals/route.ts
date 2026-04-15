import { createAdminClient } from '@/lib/supabase-admin'
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

  const { data: profile } = await supabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) return NextResponse.json({ error: 'Forbidden' }, { status: 403 })

  const { id, action, admin_note } = await req.json()
  const admin = createAdminClient()

  const status = action === 'approve' ? 'approved' : 'rejected'
  await admin.from('referral_withdrawals').update({
    status,
    admin_note: admin_note || null,
    processed_at: new Date().toISOString(),
  }).eq('id', id)

  // 申請者へのTelegram通知
  const { data: withdrawal } = await admin
    .from('referral_withdrawals')
    .select('amount, user_id')
    .eq('id', id)
    .single()

  if (withdrawal) {
    const { data: userProfile } = await admin.from('profiles').select('email').eq('id', withdrawal.user_id).single()
    await sendTelegram(
      `<b>${status === 'approved' ? '✅ 出金承認' : '❌ 出金却下'}</b>\n\n` +
      `ユーザー: ${userProfile?.email || withdrawal.user_id}\n` +
      `金額: <b>$${Number(withdrawal.amount).toFixed(2)} USDT</b>\n` +
      (admin_note ? `備考: ${admin_note}` : '')
    )
  }

  return NextResponse.json({ ok: true })
}
