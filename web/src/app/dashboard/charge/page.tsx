'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { createClient } from '@/lib/supabase-browser'

export default function ChargePage() {
  const [amount, setAmount] = useState('')
  const network = 'TRC-20' as const
  const [promoCode, setPromoCode] = useState('')
  const [promoMessage, setPromoMessage] = useState('')
  const [promoValid, setPromoValid] = useState(false)
  const [loading, setLoading] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [finalAmount, setFinalAmount] = useState(0)
  const [isFree, setIsFree] = useState(false)
  const router = useRouter()

  async function checkPromo() {
    if (!promoCode.trim()) return
    const supabase = createClient()
    const { data } = await supabase
      .from('promo_codes')
      .select('*')
      .eq('code', promoCode.toUpperCase())
      .eq('active', true)
      .single()

    if (!data) {
      setPromoMessage('Invalid promo code')
      setPromoValid(false)
    } else if (data.used_count >= data.max_uses) {
      setPromoMessage('Promo code expired')
      setPromoValid(false)
    } else if (data.type === 'charge_free') {
      setPromoMessage('Free charge promo applied')
      setPromoValid(true)
    } else if (data.type === 'discount') {
      setPromoMessage(`Discount applied (${data.discount_percent}%)`)
      setPromoValid(true)
    } else {
      setPromoMessage('Promo applied')
      setPromoValid(true)
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    const res = await fetch('/api/charge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount: amount || '0', promoCode: promoCode.trim() || null, network }),
    })
    const data = await res.json()
    if (res.ok) {
      setFinalAmount(data.amount)
      setIsFree(data.isFree)
      setSubmitted(true)
    } else {
      alert('Error: ' + (data.error || 'Unknown error'))
    }
    setLoading(false)
  }

  if (submitted) {
    return (
      <div className="min-h-screen relative z-10 flex items-center justify-center px-4 sm:px-6">
        <div className="max-w-lg text-center glass-card p-6 sm:p-10">
          <div className="text-5xl mb-6">{isFree ? '🎉' : '✅'}</div>
          <h1 className="text-2xl sm:text-3xl font-bold mb-4 font-hud">{isFree ? 'Activated' : 'Request Submitted'}</h1>
          {isFree ? (
            <p className="text-text-muted mb-8">Your free charge has been activated.</p>
          ) : (
            <>
              <p className="text-text-muted mb-4">Please send <span className="text-text font-bold">${finalAmount} USDT</span> (TRC-20) to the wallet below.</p>
              <div className="p-4 rounded-xl glass-soft font-mono text-sm text-player break-all mb-4">
                {process.env.NEXT_PUBLIC_USDT_TRC20 || 'TRC20 wallet not configured'}
              </div>
              <p className="text-text-muted text-sm mb-8">After transfer, wait for admin confirmation.</p>
            </>
          )}
          <button onClick={() => router.push('/dashboard')} className="btn-primary px-8 py-3 w-full sm:w-auto">
            Go to Dashboard
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen relative z-10 py-16 sm:py-24 px-4 sm:px-6">
      <div className="max-w-lg mx-auto">
        <div className="hud-label text-center mb-2">Wallet</div>
        <h1 className="text-2xl sm:text-3xl font-black text-center mb-2 font-hud">Charge Balance</h1>
        <p className="text-center text-sm sm:text-base text-text-muted mb-10 sm:mb-12">Add USDT balance to enable live betting.</p>

        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <label className="block text-sm text-text-muted mb-2">Amount (USD)</label>
            <input type="number" min="100" step="100" value={amount}
              onChange={e => setAmount(e.target.value)}
              className="input-field"
              placeholder="Minimum 100" />
          </div>

          <div>
            <label className="block text-sm text-text-muted mb-2">Network</label>
            <div className="p-3 rounded-xl border border-accent/60 bg-accent/10 text-accent text-center">
              TRC-20 (USDT)
            </div>
          </div>

          <div>
          <label className="block text-sm text-text-muted mb-2">Promo Code <span className="text-text-dim">(optional)</span></label>
          <div className="flex flex-col sm:flex-row gap-3">
              <input value={promoCode} onChange={e => setPromoCode(e.target.value)}
                className="input-field flex-1"
                placeholder="Enter promo code" />
            <button onClick={checkPromo} className="btn-outline px-6 py-3 w-full sm:w-auto">Apply</button>
            </div>
            {promoMessage && <p className={`text-sm mt-2 ${promoValid ? 'text-green-400' : 'text-banker'}`}>{promoMessage}</p>}
          </div>

          <button type="submit" disabled={loading}
            className="w-full btn-primary py-4 text-base sm:text-lg disabled:opacity-50">
            {loading ? 'Submitting...' : 'Submit Charge Request'}
          </button>
        </form>
      </div>
    </div>
  )
}
