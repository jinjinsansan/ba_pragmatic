'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'

export default function WithdrawalActions({ id }: { id: string }) {
  const [loading, setLoading] = useState(false)
  const [note, setNote] = useState('')
  const router = useRouter()

  async function act(action: 'approve' | 'reject') {
    setLoading(true)
    await fetch('/api/admin/withdrawals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, action, admin_note: note }),
    })
    setLoading(false)
    router.refresh()
  }

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <input
        type="text" value={note} onChange={e => setNote(e.target.value)}
        placeholder="備考(任意)"
        className="px-2 py-1 rounded bg-bg-primary border border-white/10 text-white text-xs w-full sm:w-28"
      />
      <button onClick={() => act('approve')} disabled={loading}
        className="px-2 py-1 rounded text-xs bg-green-500/20 text-green-400 hover:bg-green-500/30 transition disabled:opacity-50">
        承認
      </button>
      <button onClick={() => act('reject')} disabled={loading}
        className="px-2 py-1 rounded text-xs bg-banker/20 text-banker hover:bg-banker/30 transition disabled:opacity-50">
        却下
      </button>
    </div>
  )
}
