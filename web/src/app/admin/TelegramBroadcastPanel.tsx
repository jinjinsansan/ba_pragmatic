'use client'

import { useState } from 'react'

export default function TelegramBroadcastPanel() {
  const [loading, setLoading] = useState(false)
  const [customMessage, setCustomMessage] = useState('')
  const [result, setResult] = useState<string>('')

  async function notifySessionStart() {
    setLoading(true)
    setResult('')
    try {
      const res = await fetch('/api/admin/telegram/broadcast', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type: 'session_start',
          message: customMessage.trim() || undefined,
        }),
      })
      const data = await res.json().catch(() => null)
      if (!res.ok) {
        setResult(`失敗: ${data?.error || 'unknown error'}`)
      } else {
        setResult(`配信完了: 対象 ${data.targets} / 成功 ${data.sent} / 失敗 ${data.failed}`)
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="p-6 rounded-2xl glass-card mb-8">
      <h2 className="text-lg font-bold mb-3">顧客Telegram配信</h2>
      <p className="text-sm text-text-muted mb-4">
        「セッション開始通知」を連携済み顧客へ一斉送信します。メッセージを空欄にすると既定文を使います。
      </p>
      <textarea
        value={customMessage}
        onChange={(e) => setCustomMessage(e.target.value)}
        rows={4}
        placeholder="任意メッセージ（空欄で既定文）"
        className="w-full bg-bg-card border border-white/10 rounded-lg px-3 py-2 text-sm mb-3"
      />
      <button
        onClick={notifySessionStart}
        disabled={loading}
        className="btn-primary px-5 py-2.5 disabled:opacity-50"
      >
        {loading ? '送信中...' : 'セッション開始を一斉通知'}
      </button>
      {result && <div className="text-sm mt-3 text-text-muted">{result}</div>}
    </div>
  )
}
