import { NextResponse } from 'next/server'

export async function GET() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY

  if (!url || !anonKey) {
    return NextResponse.json({ ok: false, error: 'Missing Supabase public config' }, { status: 500 })
  }

  return NextResponse.json({
    ok: true,
    supabase_url: url,
    supabase_anon_key: anonKey,
  })
}
