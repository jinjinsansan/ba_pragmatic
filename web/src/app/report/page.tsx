'use client'

import { useState } from 'react'

export default function ReportPage() {
  const [password, setPassword] = useState('')
  const [authenticated, setAuthenticated] = useState(false)
  const [files, setFiles] = useState<string[]>([])
  const [error, setError] = useState('')
  const [viewingFile, setViewingFile] = useState('')
  const [htmlContent, setHtmlContent] = useState('')

  async function handleLogin() {
    setError('')
    const res = await fetch('/api/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    })
    if (!res.ok) {
      setError('Invalid password')
      return
    }
    const data = await res.json()
    setFiles(data.files || [])
    setAuthenticated(true)
  }

  async function openFile(file: string) {
    const res = await fetch('/api/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password, file }),
    })
    if (!res.ok) {
      setError('Failed to load file')
      return
    }
    const html = await res.text()
    setHtmlContent(html)
    setViewingFile(file)
  }

  if (viewingFile) {
    return (
      <div style={{ background: 'var(--bg)', minHeight: '100vh' }}>
        <div style={{
          padding: '8px 16px',
          background: 'var(--bg-card)',
          borderBottom: '1px solid var(--border)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: '8px',
        }}>
          <span style={{ color: 'var(--accent)', fontSize: '13px', fontWeight: 700, wordBreak: 'break-all' }}>{viewingFile}</span>
          <button
            onClick={() => { setViewingFile(''); setHtmlContent(''); }}
            style={{
              background: 'var(--bg-glass)', border: '1px solid var(--border)', color: 'var(--text-muted)',
              padding: '4px 14px', borderRadius: '4px', cursor: 'pointer', fontSize: '12px',
            }}
          >
            ← BACK
          </button>
        </div>
        <iframe
          srcDoc={htmlContent}
          style={{ width: '100%', height: 'calc(100vh - 44px)', border: 'none' }}
          sandbox="allow-same-origin"
        />
      </div>
    )
  }

  if (!authenticated) {
    return (
      <div style={{
        background: 'var(--bg)', minHeight: '100vh',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '12px',
          padding: '32px 24px', width: 'min(90vw, 360px)', textAlign: 'center',
        }}>
          <div style={{ color: 'var(--accent)', fontSize: 'clamp(20px, 4vw, 24px)', fontWeight: 900, letterSpacing: '4px', marginBottom: '8px' }}>
            LAPLACE
          </div>
          <div style={{ color: 'var(--text-dim)', fontSize: '11px', letterSpacing: '2px', marginBottom: '24px' }}>
            REPORT ACCESS
          </div>
          {error && (
            <div style={{ color: 'var(--lose)', fontSize: '12px', marginBottom: '12px' }}>{error}</div>
          )}
          <input
            type="password"
            value={password}
            onChange={e => setPassword(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleLogin()}
            placeholder="Password"
            style={{
              width: '100%', padding: '10px 14px', background: 'var(--bg)',
              border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text)',
              fontSize: '14px', outline: 'none', marginBottom: '14px', boxSizing: 'border-box',
            }}
          />
          <button
            onClick={handleLogin}
            style={{
              width: '100%', padding: '10px', background: 'linear-gradient(135deg, #009fb2, #00e5ff)',
              border: 'none', borderRadius: '6px', color: '#fff', fontSize: '13px',
              fontWeight: 700, letterSpacing: '2px', cursor: 'pointer',
            }}
          >
            ENTER
          </button>
        </div>
      </div>
    )
  }

  return (
    <div style={{ background: 'var(--bg)', minHeight: '100vh', padding: '24px 16px' }}>
      <div style={{ maxWidth: '900px', margin: '0 auto' }}>
        <h1 style={{ color: 'var(--accent)', fontSize: 'clamp(20px, 3vw, 24px)', borderBottom: '2px solid var(--accent)', paddingBottom: '8px', marginBottom: '20px' }}>
          LAPLACE Report
        </h1>
        <div style={{
          display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
          gap: '12px',
        }}>
          {files.map(f => (
            <button
              key={f}
              onClick={() => openFile(f)}
              style={{
                background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '8px',
                padding: '14px 16px', textAlign: 'left', cursor: 'pointer',
                color: 'var(--accent)', fontSize: '13px', fontWeight: 600,
                transition: 'border-color 0.2s',
              }}
              onMouseEnter={e => (e.currentTarget.style.borderColor = 'var(--accent)')}
              onMouseLeave={e => (e.currentTarget.style.borderColor = 'var(--border)')}
            >
              {f.replace('.html', '').replace(/_/g, ' ')}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
