import { createClient } from '@/lib/supabase-server'
import { createAdminClient } from '@/lib/supabase-admin'
import { NextRequest, NextResponse } from 'next/server'

export async function GET(req: NextRequest) {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()
  if (!user) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })

  const file = req.nextUrl.searchParams.get('file')
  if (!file) return NextResponse.json({ error: 'No file specified' }, { status: 400 })

  // Verify user has access
  const { data: deliverable } = await supabase.from('deliverables').select('*').eq('user_id', user.id).eq('file_path', file).single()
  if (!deliverable) return NextResponse.json({ error: 'Not found' }, { status: 404 })

  // file_path がURLの場合は直接リダイレクト
  if (deliverable.file_path.startsWith('http')) {
    return NextResponse.redirect(deliverable.file_path)
  }

  const admin = createAdminClient()
  const { data, error } = await admin.storage.from('deliverables').download(file)
  if (error || !data) return NextResponse.json({ error: 'Download failed' }, { status: 500 })

  return new NextResponse(data, {
    headers: {
      'Content-Type': 'application/zip',
      'Content-Disposition': `attachment; filename="LAPLACE-v${deliverable.version}.zip"`,
    },
  })
}
