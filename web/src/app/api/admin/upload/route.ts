import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3'
import { createAdminClient } from '@/lib/supabase-admin'
import { createClient as createServerSupabase } from '@/lib/supabase-server'
import { NextRequest, NextResponse } from 'next/server'

export const maxDuration = 60
export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

async function sendTelegram(message: string) {
  const token = process.env.ADMIN_TELEGRAM_BOT_TOKEN
  const chatId = process.env.ADMIN_TELEGRAM_CHAT_ID
  if (!token || !chatId) return
  try {
    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text: message }),
    })
  } catch {}
}

async function uploadToR2(file: File, key: string) {
  const endpoint = process.env.R2_ENDPOINT
  const bucket = process.env.R2_BUCKET
  const accessKeyId = process.env.R2_ACCESS_KEY_ID
  const secretAccessKey = process.env.R2_SECRET_ACCESS_KEY
  if (!endpoint || !bucket || !accessKeyId || !secretAccessKey) {
    throw new Error('R2 env missing')
  }
  const client = new S3Client({
    region: 'auto',
    endpoint,
    credentials: { accessKeyId, secretAccessKey },
  })
  const buffer = Buffer.from(await file.arrayBuffer())
  await client.send(new PutObjectCommand({
    Bucket: bucket,
    Key: key,
    Body: buffer,
    ContentType: file.type || 'application/zip',
  }))
}

export async function POST(req: NextRequest) {
  const serverSupabase = await createServerSupabase()
  const { data: { user } } = await serverSupabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const { data: profile } = await serverSupabase.from('profiles').select('is_admin').eq('id', user.id).single()
  if (!profile?.is_admin) return NextResponse.json({ error: 'Forbidden' }, { status: 403 })

  const contentType = req.headers.get('content-type') || ''
  let userId = ''
  let version = '1.0'
  let url = ''
  let file: File | null = null

  if (contentType.includes('multipart/form-data')) {
    const form = await req.formData()
    userId = String(form.get('userId') || '')
    version = String(form.get('version') || '1.0')
    const fileValue = form.get('file')
    if (fileValue instanceof File) file = fileValue
  } else {
    const body = await req.json()
    userId = body.userId
    version = body.version || '1.0'
    url = body.url || ''
  }

  if (!userId) return NextResponse.json({ error: 'Missing userId' }, { status: 400 })

  const admin = createAdminClient()
  let filePath = url

  if (file) {
    const key = `deliverables/${userId}/laplace.zip`
    await uploadToR2(file, key)
    const publicBase = (process.env.R2_PUBLIC_URL || '').replace(/\/$/, '')
    if (!publicBase) return NextResponse.json({ error: 'R2_PUBLIC_URL missing' }, { status: 500 })
    filePath = `${publicBase}/${key}`
  }

  if (!filePath) return NextResponse.json({ error: 'Missing url or file' }, { status: 400 })

  await admin.from('deliverables').delete().eq('user_id', userId)
  await admin.from('deliverables').insert({
    user_id: userId,
    file_path: filePath,
    version,
  })

  await sendTelegram(`✅ Deliverable updated for user ${userId} (v${version})`)
  return NextResponse.json({ ok: true })
}
