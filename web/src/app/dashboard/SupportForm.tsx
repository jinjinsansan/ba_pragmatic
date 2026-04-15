'use client'

import { useState } from 'react'

export default function SupportForm() {
  const [message, setMessage] = useState('')
  const [loading, setLoading] = useState(false)
  const [sent, setSent] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!message.trim()) return
    setLoading(true)
    const res = await fetch('/api/tickets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    })
    if (res.ok) {
      setMessage('')
      setSent(true)
      setTimeout(() => setSent(false), 3000)
    }
    setLoading(false)
  }

  return (
    <div className="p-6 rounded-2xl glass-card">
      <h2 className="text-lg font-bold mb-4">Support</h2>
      {sent ? (
        <p className="text-green-400 text-sm">Message sent! We&apos;ll get back to you soon.</p>
      ) : (
        <form onSubmit={handleSubmit} className="flex flex-col sm:flex-row gap-3">
          <input
            value={message} onChange={e => setMessage(e.target.value)}
            className="input-field flex-1 text-sm"
            placeholder="Describe your issue..."
            required
          />
          <button type="submit" disabled={loading}
            className="btn-outline px-6 py-3 text-sm disabled:opacity-50">
            {loading ? '...' : 'Send'}
          </button>
        </form>
      )}
    </div>
  )
}
