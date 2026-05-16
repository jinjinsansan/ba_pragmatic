import crypto from 'crypto'

const TOKEN_TTL_SEC = 60 * 60 * 24 * 30

function base64urlEncode(input: string) {
  return Buffer.from(input, 'utf8').toString('base64url')
}

function base64urlDecode(input: string) {
  return Buffer.from(input, 'base64url').toString('utf8')
}

function getLinkSecret() {
  return (process.env.CUSTOMER_TELEGRAM_LINK_SECRET || process.env.SUPABASE_SERVICE_ROLE_KEY || '').trim()
}

function getCustomerBotToken() {
  return (process.env.CUSTOMER_TELEGRAM_BOT_TOKEN || '').trim()
}

function signPayload(payloadB64: string) {
  const secret = getLinkSecret()
  if (!secret) return ''
  return crypto.createHmac('sha256', secret).update(payloadB64).digest('base64url')
}

export function generateCustomerTelegramStartToken(userId: string) {
  if (!userId) return ''
  const secret = getLinkSecret()
  if (!secret) return ''
  const payload = base64urlEncode(JSON.stringify({ u: userId, ts: Date.now() }))
  const sig = signPayload(payload)
  if (!sig) return ''
  return `${payload}.${sig}`
}

export function verifyCustomerTelegramStartToken(token: string) {
  const secret = getLinkSecret()
  if (!secret || !token) return null
  const parts = token.split('.')
  if (parts.length !== 2) return null
  const [payloadB64, sig] = parts
  const expected = signPayload(payloadB64)
  const sigBuf = Buffer.from(sig || '', 'utf8')
  const expectedBuf = Buffer.from(expected || '', 'utf8')
  if (!sigBuf.length || sigBuf.length !== expectedBuf.length) return null
  if (!crypto.timingSafeEqual(sigBuf, expectedBuf)) return null
  try {
    const parsed = JSON.parse(base64urlDecode(payloadB64))
    const userId = String(parsed?.u || '')
    const ts = Number(parsed?.ts || 0)
    if (!userId || !Number.isFinite(ts)) return null
    if (Date.now() - ts > TOKEN_TTL_SEC * 1000) return null
    return { userId }
  } catch {
    return null
  }
}

export function buildCustomerTelegramStartLink(userId: string) {
  const username = String(process.env.CUSTOMER_TELEGRAM_BOT_USERNAME || '').trim().replace(/^@/, '')
  const token = generateCustomerTelegramStartToken(userId)
  if (!username || !token) return ''
  return `https://t.me/${username}?start=${token}`
}

export async function sendCustomerTelegramMessage(chatId: string | number, message: string) {
  const token = getCustomerBotToken()
  if (!token || !chatId || !message) return false
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: String(chatId),
        text: message,
        parse_mode: 'HTML',
      }),
      cache: 'no-store',
    })
    return res.ok
  } catch {
    return false
  }
}
