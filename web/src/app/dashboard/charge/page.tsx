'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { createClient } from '@/lib/supabase-browser'
import { copyTextToClipboard } from '@/lib/clipboard'

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
  const [txHash, setTxHash] = useState('')
  const [copyMessage, setCopyMessage] = useState('')
  const router = useRouter()
  const walletAddress = process.env.NEXT_PUBLIC_USDT_TRC20 || 'TRC20 wallet not configured'
  const walletCopyDisabled = walletAddress === 'TRC20 wallet not configured'

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
      body: JSON.stringify({ amount: amount || '0', promoCode: promoCode.trim() || null, network, txHash: txHash.trim() || null }),
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

  async function copyWallet() {
    if (walletCopyDisabled) {
      setCopyMessage('Wallet address is not configured.')
      return
    }
    const ok = await copyTextToClipboard(walletAddress)
    setCopyMessage(ok ? 'Address copied to clipboard.' : 'Copy failed. Please copy manually.')
  }

  if (submitted) {
    return (
      <div className="min-h-screen relative z-10 bg-[#05080f] text-[#d9f7ff] flex items-center justify-center px-4 sm:px-6 py-10">
        <div className="max-w-xl w-full p-6 sm:p-10 rounded-xl border border-cyan-400/40 bg-[#0c1220]/90 shadow-[0_0_40px_rgba(0,229,255,0.2)]">
          <div className="text-[10px] tracking-[0.25em] text-cyan-300/70 mb-3">CHARGE STATUS</div>
          <h1 className="text-2xl sm:text-3xl font-black mb-2 tracking-[0.06em] text-cyan-200">
            {isFree ? 'CHARGE ACTIVATED' : 'REQUEST SUBMITTED'}
          </h1>
          <p className="text-xs text-cyan-100/70 mb-8">Pending admin confirmation after transfer check.</p>
          {isFree ? (
            <p className="text-cyan-100/80 mb-8">Your free charge has been activated.</p>
          ) : (
            <>
              <p className="text-cyan-100/80 mb-4">
                Please send <span className="text-cyan-200 font-bold">${finalAmount} USDT</span> via TRC-20.
              </p>
              <div className="p-4 rounded-lg border border-cyan-400/40 bg-[#0a1322] font-mono text-sm text-cyan-300 break-all mb-4">
                {walletAddress}
              </div>
              <p className="text-cyan-100/70 text-sm mb-8">After transfer, wait for admin confirmation.</p>
            </>
          )}
          <button
            onClick={() => router.push('/dashboard')}
            className="w-full sm:w-auto px-8 py-3 rounded-md bg-gradient-to-r from-cyan-800 to-cyan-500 text-white text-xs tracking-[0.2em] font-bold"
          >
            Go to Dashboard
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen relative z-10 bg-[#05080f] text-[#d9f7ff] py-12 px-4 sm:px-6 overflow-hidden">
      <div
        className="pointer-events-none absolute inset-0 opacity-35"
        style={{
          backgroundImage:
            'linear-gradient(rgba(0,229,255,0.08) 1px, transparent 1px), linear-gradient(90deg, rgba(0,229,255,0.08) 1px, transparent 1px)',
          backgroundSize: '36px 36px',
        }}
      />
      <div className="relative max-w-6xl mx-auto">
        <div className="mb-8">
          <div className="text-[11px] tracking-[0.22em] text-cyan-300/70">TOP-UP FLOW · TRC-20</div>
          <h1 className="text-3xl sm:text-5xl font-black tracking-[0.05em] text-cyan-200 mt-2">ADD CHARGE</h1>
          <p className="text-xs sm:text-sm text-cyan-100/70 mt-3">Submit amount, transfer USDT, then wait for confirmation.</p>
        </div>

        <form onSubmit={handleSubmit} className="grid lg:grid-cols-[1.4fr_1fr] gap-5">
          <div className="space-y-4">
            <div className="rounded-xl border border-cyan-400/30 bg-[#0c1220]/90 p-5">
              <div className="text-[10px] tracking-[0.2em] text-cyan-300/70 mb-3">01 · AMOUNT</div>
              <input
                type="number"
                min="100"
                step="100"
                value={amount}
                onChange={e => setAmount(e.target.value)}
                className="w-full rounded-md bg-[#0a1322] border border-cyan-400/30 px-4 py-3 text-lg text-cyan-200 outline-none focus:border-cyan-300"
                placeholder="Minimum 100"
              />
            </div>

            <div className="rounded-xl border border-cyan-400/30 bg-[#0c1220]/90 p-5">
              <div className="text-[10px] tracking-[0.2em] text-cyan-300/70 mb-3">02 · NETWORK</div>
              <div className="rounded-md border border-cyan-400 bg-cyan-500/10 px-4 py-3">
                <div className="text-lg font-black text-cyan-300">TRC-20</div>
                <div className="text-[11px] text-cyan-100/60 mt-1">USDT on TRON</div>
              </div>
            </div>

            <div className="rounded-xl border border-cyan-400/30 bg-[#0c1220]/90 p-5">
              <div className="text-[10px] tracking-[0.2em] text-cyan-300/70 mb-3">03 · PROMO CODE · OPTIONAL</div>
              <div className="flex flex-col sm:flex-row gap-2">
                <input
                  value={promoCode}
                  onChange={e => setPromoCode(e.target.value)}
                  className="flex-1 rounded-md bg-[#0a1322] border border-cyan-400/30 px-4 py-3 text-sm text-cyan-200 outline-none focus:border-cyan-300"
                  placeholder="Enter promo code"
                />
                <button
                  type="button"
                  onClick={checkPromo}
                  className="px-6 py-3 rounded-md border border-cyan-300/60 text-cyan-300 text-xs tracking-[0.18em] font-bold"
                >
                  APPLY
                </button>
              </div>
              {promoMessage && (
                <p className={`text-xs mt-3 ${promoValid ? 'text-emerald-300' : 'text-rose-300'}`}>{promoMessage}</p>
              )}
            </div>

            <div className="rounded-xl border border-cyan-400/30 bg-[#0c1220]/90 p-5">
              <div className="text-[10px] tracking-[0.2em] text-cyan-300/70 mb-3">04 · SEND TO</div>
              <div className="p-3 rounded-md border border-cyan-400/40 bg-[#0a1322] font-mono text-xs sm:text-sm text-cyan-300 break-all">
                {walletAddress}
              </div>
              <button
                type="button"
                onClick={copyWallet}
                disabled={walletCopyDisabled}
                className="mt-3 px-4 py-2 rounded-md border border-cyan-300/60 text-cyan-300 text-[11px] tracking-[0.18em] font-bold disabled:opacity-40 disabled:cursor-not-allowed"
              >
                COPY ADDRESS
              </button>
              {copyMessage && <p className="text-xs text-cyan-100/70 mt-2">{copyMessage}</p>}
            </div>

            <div className="rounded-xl border border-cyan-400/30 bg-[#0c1220]/90 p-5">
              <div className="text-[10px] tracking-[0.2em] text-cyan-300/70 mb-3">05 · TX HASH · OPTIONAL</div>
              <input
                value={txHash}
                onChange={e => setTxHash(e.target.value)}
                placeholder="0x... or T..."
                className="w-full rounded-md bg-[#0a1322] border border-cyan-400/30 px-4 py-3 text-sm text-cyan-200 outline-none focus:border-cyan-300"
              />
              <p className="text-xs text-cyan-100/60 mt-2">Fill after transfer if you want faster review.</p>
            </div>
          </div>

          <div className="rounded-xl border border-cyan-400/40 bg-[#0c1220]/95 p-5 lg:sticky lg:top-8 h-fit shadow-[0_0_30px_rgba(0,229,255,0.18)]">
            <div className="text-[10px] tracking-[0.2em] text-cyan-300/70 mb-3">REQUEST SUMMARY</div>
            <div className="space-y-2 text-sm text-cyan-100/70">
              <div className="flex justify-between"><span>Amount</span><span className="font-mono text-cyan-200">${Number(amount || 0).toFixed(2)}</span></div>
              <div className="flex justify-between"><span>Network</span><span className="font-mono text-cyan-200">{network}</span></div>
              <div className="flex justify-between"><span>Promo</span><span className="font-mono text-cyan-200">{promoCode.trim() || 'None'}</span></div>
            </div>
            <div className="h-px bg-cyan-400/40 my-5" />
            <button
              type="submit"
              disabled={loading}
              className="w-full px-5 py-3 rounded-md bg-gradient-to-r from-cyan-800 to-cyan-500 text-white text-xs tracking-[0.2em] font-bold disabled:opacity-60"
            >
              {loading ? 'SUBMITTING...' : 'SUBMIT CHARGE REQUEST'}
            </button>
            <p className="text-[11px] text-cyan-100/55 mt-3 text-center">Manual confirm · ETA ~30 min</p>
          </div>
        </form>
      </div>
    </div>
  )
}
