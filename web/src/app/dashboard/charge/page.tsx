'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { createClient } from '@/lib/supabase-browser'

export default function ChargePage() {
  const [amount, setAmount] = useState('')
  const [network, setNetwork] = useState<'TRC-20' | 'ERC-20'>('TRC-20')
  const [promoCode, setPromoCode] = useState('')
  const [promoMessage, setPromoMessage] = useState('')
  const [promoValid, setPromoValid] = useState(false)
  const [loading, setLoading] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [finalAmount, setFinalAmount] = useState(0)
  const [requestedAmount, setRequestedAmount] = useState<number | null>(null)
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
      setPromoMessage('Code expired')
      setPromoValid(false)
    } else if (data.type === 'charge_free') {
      setPromoMessage('Charge FREE!')
      setPromoValid(true)
    } else if (data.type === 'discount') {
      setPromoMessage(`${data.discount_percent}% off`)
      setPromoValid(true)
    } else {
      setPromoMessage('Code applied')
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
      setRequestedAmount(typeof data.requestedAmount === 'number' ? data.requestedAmount : null)
      setIsFree(data.isFree)
      setSubmitted(true)
    } else {
      alert('Error: ' + (data.error || 'Unknown error'))
    }
    setLoading(false)
  }

  if (submitted) {
    return (
      <div className="min-h-screen flex items-center justify-center px-4 sm:px-6">
        <div className="max-w-lg text-center glass-card p-6 sm:p-10">
          <div className="text-5xl mb-6">{isFree ? '🎉' : '✅'}</div>
          <h1 className="text-2xl sm:text-3xl font-bold mb-4 font-hud">{isFree ? 'Charge Activated!' : 'Charge Submitted'}</h1>
          {isFree ? (
            <p className="text-text-muted mb-8">Your account is now active with unlimited balance.</p>
          ) : (
            <>
              <p className="text-text-muted mb-4">
                Send <span className="text-text font-bold">${Number(finalAmount || 0).toFixed(2)} USDT</span> ({network}) to:
              </p>
              <div className="p-4 rounded-xl glass-soft font-mono text-sm text-player break-all mb-4">
                {network === 'TRC-20' ? (process.env.NEXT_PUBLIC_USDT_TRC20 || 'TRC20 wallet not configured') : (process.env.NEXT_PUBLIC_USDT_ERC20 || 'ERC20 wallet not configured')}
              </div>
              {(requestedAmount !== null && Math.abs(requestedAmount - Number(finalAmount || 0)) >= 0.009) && (
                <p className="text-text-dim text-xs mb-2">Requested: ${Number(requestedAmount || 0).toFixed(2)}</p>
              )}
              <p className="text-text-muted text-sm mb-8">
                Please send the exact amount (including decimals). We use it to match your payment. We&apos;ll confirm within 30 minutes after receiving it.
              </p>
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
    <div className="min-h-screen py-16 sm:py-24 px-4 sm:px-6">
      <div className="max-w-lg mx-auto">
        <div className="hud-label text-center mb-2">Balance Access</div>
        <h1 className="text-2xl sm:text-3xl font-black text-center mb-2 font-hud">Charge Balance</h1>
        <p className="text-center text-sm sm:text-base text-text-muted mb-10 sm:mb-12">Add funds to enable live betting</p>

        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <label className="block text-sm text-text-muted mb-2">Amount (USD)</label>
            <input type="number" min="100" step="100" value={amount}
              onChange={e => setAmount(e.target.value)}
              className="input-field"
              placeholder="Minimum $100" />
          </div>

          <div>
          <label className="block text-sm text-text-muted mb-2">USDT Network</label>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {(['TRC-20', 'ERC-20'] as const).map(n => (
                <button key={n} type="button" onClick={() => setNetwork(n)}
                  className={`p-3 rounded-xl border text-center transition ${network === n ? 'border-accent/60 bg-accent/10 text-accent' : 'border-accent/15 bg-bg-card hover:border-accent/30 text-text-muted'}`}>
                  {n}
                </button>
              ))}
            </div>
          </div>

          <div>
          <label className="block text-sm text-text-muted mb-2">Promo Code <span className="text-text-dim">(optional)</span></label>
          <div className="flex flex-col sm:flex-row gap-3">
              <input value={promoCode} onChange={e => setPromoCode(e.target.value)}
                className="input-field flex-1"
                placeholder="Enter code" />
            <button onClick={checkPromo} className="btn-outline px-6 py-3 w-full sm:w-auto">Apply</button>
            </div>
            {promoMessage && <p className={`text-sm mt-2 ${promoValid ? 'text-green-400' : 'text-banker'}`}>{promoMessage}</p>}
          </div>

          <button type="submit" disabled={loading}
            className="w-full btn-primary py-4 text-base sm:text-lg disabled:opacity-50">
            {loading ? 'Processing...' : 'Submit Charge'}
          </button>
        </form>
      </div>
    </div>
  )
}
