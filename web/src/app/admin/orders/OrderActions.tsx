'use client'

import { useState, useRef } from 'react'
import { useRouter } from 'next/navigation'

export default function OrderActions({ type, id, userId, status, amount }: {
  type: 'order' | 'charge'; id: string; userId: string; status: string; amount?: number
}) {
  const [loading, setLoading] = useState(false)
  const [showUpload, setShowUpload] = useState(false)
  const [uploadMode, setUploadMode] = useState<'url' | 'file'>('url')
  const [zipUrl, setZipUrl] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const router = useRouter()

  async function handleConfirm() {
    setLoading(true)
    const res = await fetch('/api/admin/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type, id, userId, amount }),
    })
    if (res.ok) {
      if (type === 'order') setShowUpload(true)
      else router.refresh()
    } else alert('Error: ' + (await res.text()))
    setLoading(false)
  }

  async function handleUpload() {
    const file = fileRef.current?.files?.[0]
    if (!file) return
    setLoading(true)
    const formData = new FormData()
    formData.append('file', file)
    formData.append('userId', userId)
    formData.append('version', '1.0')
    const res = await fetch('/api/admin/upload', { method: 'POST', body: formData })
    const data = await res.json().catch(() => ({}))
    if (res.ok) {
      await fetch('/api/admin/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: 'deliver', id, userId }),
      })
      router.refresh()
    } else alert('Upload failed: ' + (data.error || res.status))
    setLoading(false)
  }

  async function handleSendUrl() {
    if (!zipUrl.startsWith('http')) { alert('正しいURLを入力してください'); return }
    setLoading(true)
    const res = await fetch('/api/admin/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId, version: '1.0', url: zipUrl }),
    })
    if (res.ok) {
      await fetch('/api/admin/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: 'deliver', id, userId }),
      })
      router.refresh()
    } else alert('送付失敗: ' + (await res.text()))
    setLoading(false)
  }

  function renderUploadPanel() {
    return (
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => setUploadMode('url')}
            className={`px-2 py-1 rounded text-xs transition ${uploadMode === 'url' ? 'bg-player/20 text-player' : 'bg-white/5 text-slate-400'}`}
          >
            URL送付
          </button>
          <button
            onClick={() => setUploadMode('file')}
            className={`px-2 py-1 rounded text-xs transition ${uploadMode === 'file' ? 'bg-player/20 text-player' : 'bg-white/5 text-slate-400'}`}
          >
            ZIPアップロード
          </button>
        </div>
        {uploadMode === 'file' ? (
          <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
            <input ref={fileRef} type="file" accept=".zip" className="text-xs text-slate-400 w-full sm:w-32" />
            <button onClick={handleUpload} disabled={loading}
              className="px-3 py-1 rounded-lg bg-player/20 text-player text-xs font-semibold disabled:opacity-50 w-full sm:w-auto">
              {loading ? '...' : 'ZIP送付'}
            </button>
          </div>
        ) : (
          <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
            <input
              type="url"
              value={zipUrl}
              onChange={e => setZipUrl(e.target.value)}
              placeholder="https://.../laplace.zip"
              className="flex-1 text-xs bg-white/5 border border-white/10 rounded px-2 py-1 text-white placeholder-slate-500"
            />
            <button onClick={handleSendUrl} disabled={loading}
              className="px-3 py-1 rounded-lg bg-player/20 text-player text-xs font-semibold disabled:opacity-50 w-full sm:w-auto">
              {loading ? '...' : '送付'}
            </button>
          </div>
        )}
      </div>
    )
  }

  if (status === 'delivered') return <span className="text-xs text-green-400">配送済み</span>
  if (status === 'confirmed' && type === 'order') {
    return renderUploadPanel()
  }
  if (status === 'confirmed') return null

  return (
    <>
      {showUpload ? (
        renderUploadPanel()
      ) : (
        <button onClick={handleConfirm} disabled={loading}
          className="px-3 py-1 rounded-lg bg-green-500/20 text-green-400 text-xs font-semibold hover:bg-green-500/30 transition disabled:opacity-50">
          {loading ? '...' : '確認する'}
        </button>
      )}
    </>
  )
}
