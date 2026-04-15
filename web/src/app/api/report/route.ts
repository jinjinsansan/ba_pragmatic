import { NextRequest, NextResponse } from 'next/server'
import { readFileSync, readdirSync, existsSync } from 'fs'
import { join } from 'path'

const REPORT_PASSWORD = process.env.REPORT_PASSWORD || 'kajiwara123'

function getReportDir(): string {
  // Vercel: reports are bundled in web/reports/
  const bundled = join(process.cwd(), 'reports')
  if (existsSync(bundled)) return bundled
  // Local dev: read from project root report/
  const local = join(process.cwd(), '..', 'report')
  if (existsSync(local)) return local
  return bundled
}

export async function POST(req: NextRequest) {
  const body = await req.json()
  const { password, file } = body

  if (password !== REPORT_PASSWORD) {
    return NextResponse.json({ error: 'Invalid password' }, { status: 401 })
  }

  const reportDir = getReportDir()

  if (file) {
    const safeName = file.replace(/[^a-zA-Z0-9_\-.]/g, '')
    if (!safeName.endsWith('.html')) {
      return NextResponse.json({ error: 'Invalid file' }, { status: 400 })
    }
    try {
      const content = readFileSync(join(reportDir, safeName), 'utf-8')
      return new NextResponse(content, {
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
      })
    } catch {
      return NextResponse.json({ error: 'File not found' }, { status: 404 })
    }
  }

  try {
    const files = readdirSync(reportDir)
      .filter((f: string) => f.endsWith('.html') && f !== 'index.html')
      .sort()
    return NextResponse.json({ files })
  } catch {
    return NextResponse.json({ error: 'Report dir not found' }, { status: 500 })
  }
}
