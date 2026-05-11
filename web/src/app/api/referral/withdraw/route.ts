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

  const { amount, wallet_address, network } = await req.json()
  if (!amount || !wallet_address || !network) {
    return NextResponse.json({ error: 'Missing fields' }, { status: 400 })
  }
  if (network !== 'TRC-20') {
    return NextResponse.json({ error: 'Invalid network (TRC-20 only)' }, { status: 400 })
  }

  const admin = createAdminClient()

  // 利用可能残高を計算
  const { data: commissions } = await admin
    .from('referral_commissions')
    .select('commission_amount')
    .eq('referrer_id', user.id)

  const { data: withdrawals } = await admin
    .from('referral_withdrawals')
    .select('amount')
    .eq('user_id', user.id)
    .in('status', ['pending', 'approved'])

  const totalEarned = commissions?.reduce((s, c) => s + Number(c.commission_amount), 0) ?? 0
  const totalWithdrawn = withdrawals?.reduce((s, w) => s + Number(w.amount), 0) ?? 0
  const available = totalEarned - totalWithdrawn

  if (amount > available) {
    return NextResponse.json({ error: `Insufficient balance. Available: $${available.toFixed(2)}` }, { status: 400 })
  }
  if (amount < 10) {
    return NextResponse.json({ error: 'Minimum withdrawal is $10' }, { status: 400 })
  }

  const { error } = await admin.from('referral_withdrawals').insert({
    user_id: user.id,
    amount,
    wallet_address,
    network,
    status: 'pending',
  })

  if (error) return NextResponse.json({ error: error.message }, { status: 500 })

  // ユーザー情報取得
  const { data: profile } = await admin.from('profiles').select('email').eq('id', user.id).single()

  // Telegram通知
  await sendTelegram(
    `<b>💸 出金申請</b>\n\n` +
    `ユーザー: ${profile?.email || user.id}\n` +
    `金額: <b>$${Number(amount).toFixed(2)} USDT</b>\n` +
    `ネットワーク: ${network}\n` +
    `ウォレット: <code>${wallet_address}</code>\n\n` +
    `管理画面で承認してください。`
  )

  return NextResponse.json({ ok: true })
}
