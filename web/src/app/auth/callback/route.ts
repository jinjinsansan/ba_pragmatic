import { createServerClient } from '@supabase/ssr'
import { cookies } from 'next/headers'
import { NextResponse, type NextRequest } from 'next/server'
import { createAdminClient } from '@/lib/supabase-admin'

export async function GET(request: NextRequest) {
  const { searchParams } = new URL(request.url)
  const code = searchParams.get('code')
  const next = searchParams.get('next') ?? '/dashboard'

  if (code) {
    const cookieStore = await cookies()
    const supabase = createServerClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
      {
        cookies: {
          getAll() { return cookieStore.getAll() },
          setAll(cookiesToSet) {
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options)
            )
          },
        },
      }
    )
    const { error } = await supabase.auth.exchangeCodeForSession(code)
    if (!error) {
      const { data: { user } } = await supabase.auth.getUser()
      const referredByCode = String((user?.user_metadata as any)?.referred_by || '').trim().toUpperCase()
      if (user?.id && referredByCode) {
        const admin = createAdminClient()
        const { data: profile } = await admin
          .from('profiles')
          .select('id,referred_by,email')
          .eq('id', user.id)
          .maybeSingle()
        if (!profile) {
          await admin.from('profiles').upsert({
            id: user.id,
            email: user.email || null,
            referred_by: referredByCode,
          }, { onConflict: 'id' })
        } else if (!String(profile.referred_by || '').trim()) {
          await admin.from('profiles').update({ referred_by: referredByCode }).eq('id', user.id)
        }
      }
      return NextResponse.redirect(new URL(next, request.url))
    }
  }

  return NextResponse.redirect(new URL('/login?error=auth', request.url))
}
