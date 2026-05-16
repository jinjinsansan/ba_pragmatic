'use client'

import { Suspense, useState } from 'react'
import { useRouter } from 'next/navigation'
import { createClient } from '@/lib/supabase-browser'

const PRICE = 2000
const FEATURES = [
  'AI signal engine',
  'Master + Executor workflow',
  'Auto table recommendations',
  'Admin support',
]

function PurchaseForm() {
  const router = useRouter()
  const network = 'TRC-20' as const
  const [promoCode, setPromoCode] = useState('')
  const [promoMessage, setPromoMessage] = useState('')
  const [promoValid, setPromoValid] = useState(false)
  const [loading, setLoading] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [orderId, setOrderId] = useState('')
  const [finalAmount, setFinalAmount] = useState(0)
  const [isFree, setIsFree] = useState(false)

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
    } else if (data.type === 'package_free') {
      setPromoMessage('Free license promo applied')
      setPromoValid(true)
    } else if (data.type === 'discount') {
      setPromoMessage(`Discount applied (${data.discount_percent}%)`)
      setPromoValid(true)
    } else {
      setPromoMessage('Promo applied')
      setPromoValid(true)
    }
  }

  async function handleSubmit() {
    setLoading(true)
    const res = await fetch('/api/purchase', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan: 'standard', promoCode: promoCode.trim() || null, network }),
    })
    const data = await res.json()
    if (res.ok) {
      setOrderId(data.orderId)
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
      <div className="min-h-screen flex items-center justify-center px-4 sm:px-6">
        <div className="max-w-lg text-center glass-card p-6 sm:p-10">
          <div className="text-5xl mb-6">{isFree ? '🎉' : '✅'}</div>
          <h1 className="text-2xl sm:text-3xl font-bold mb-4 font-hud">{isFree ? 'Activated' : 'Order Submitted'}</h1>
          {isFree ? (
            <p className="text-text-muted mb-8">Your license has been activated.</p>
          ) : (
            <>
              <p className="text-text-muted mb-4">Please send <span className="text-text font-bold">${finalAmount} USDT</span> (TRC-20) to the wallet below.</p>
              <div className="p-4 rounded-xl glass-soft font-mono text-sm text-player break-all mb-4">
                {process.env.NEXT_PUBLIC_USDT_TRC20 || 'TRC20 wallet not configured'}
              </div>
              <p className="text-text-muted text-sm mb-8">Order ID: {orderId}<br className="hidden sm:block" />After transfer, wait for admin confirmation.</p>
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
        <div className="hud-label text-center mb-2">Plans</div>
        <h1 className="text-2xl sm:text-3xl font-black text-center mb-2 font-hud">Purchase License</h1>
        <p className="text-center text-sm sm:text-base text-text-muted mb-10 sm:mb-12">Buy a LAPLACE license to start using the service.</p>

        <div className="p-6 sm:p-8 rounded-2xl glass-card mb-8">
          <h2 className="text-xl font-bold mb-1 text-text">LAPLACE License</h2>
          <div className="text-3xl sm:text-4xl font-black my-4 text-text">${PRICE.toLocaleString()} <span className="text-sm text-text-muted font-normal">USDT</span></div>
          <ul className="space-y-3 text-sm text-text-muted">
            {FEATURES.map((f, i) => (
              <li key={i} className="flex gap-2"><span className="text-player">✓</span> {f}</li>
            ))}
          </ul>
        </div>

        <div className="mb-8">
          <label className="block text-sm text-text-muted mb-2">Network</label>
          <div className="p-4 rounded-xl border border-accent/60 bg-accent/10 text-accent text-center">
            TRC-20 (USDT)
          </div>
        </div>

        <div className="mb-8">
          <label className="block text-sm text-text-muted mb-2">Promo Code <span className="text-text-dim">(optional)</span></label>
          <div className="flex flex-col sm:flex-row gap-3">
            <input value={promoCode} onChange={e => setPromoCode(e.target.value)}
              className="input-field flex-1"
              placeholder="Enter promo code" />
            <button onClick={checkPromo} className="btn-outline px-6 py-3 w-full sm:w-auto">Apply</button>
          </div>
          {promoMessage && <p className={`text-sm mt-2 ${promoValid ? 'text-player' : 'text-banker'}`}>{promoMessage}</p>}
        </div>

        <div className="p-5 sm:p-6 rounded-xl glass-soft mb-8">
          <div className="flex justify-between items-center mb-2">
            <span className="text-text-muted">License</span>
            <span className="font-bold text-text">LAPLACE</span>
          </div>
          <div className="flex justify-between items-center mb-2">
            <span className="text-text-muted">Network</span>
            <span className="text-text">{network}</span>
          </div>
          <div className="border-t border-accent/10 my-3" />
          <div className="flex justify-between items-center">
            <span className="text-text-muted">Total</span>
            <span className="text-2xl font-black text-text">${PRICE.toLocaleString()}</span>
          </div>
        </div>

        <button onClick={handleSubmit} disabled={loading}
          className="w-full btn-primary py-4 text-base sm:text-lg disabled:opacity-50">
          {loading ? 'Submitting...' : 'Submit Purchase Request'}
        </button>
      </div>
    </div>
  )
}

export default function PurchasePage() {
  return (
    <Suspense fallback={<div className="min-h-screen flex items-center justify-center text-slate-400">Loading...</div>}>
      <PurchaseForm />
    </Suspense>
  )
}
