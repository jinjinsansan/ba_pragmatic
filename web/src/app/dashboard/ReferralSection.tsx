'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'

type Referred = {
  id: string
  email: string
  created_at: string
  total_charged: number
  commission: number
}

export default function ReferralSection({
  referralUrl,
  referred,
  totalEarned,
  totalWithdrawn,
  withdrawals,
}: {
  referralUrl: string
  referred: Referred[]
  totalEarned: number
  totalWithdrawn: number
  withdrawals: any[]
}) {
  const available = totalEarned - totalWithdrawn
  const [copied, setCopied] = useState(false)
  const [showWithdraw, setShowWithdraw] = useState(false)
  const [amount, setAmount] = useState('')
  const [wallet, setWallet] = useState('')
  const [network, setNetwork] = useState('TRC-20')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const router = useRouter()

  function copyUrl() {
    navigator.clipboard.writeText(referralUrl)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  async function submitWithdraw() {
    setError('')
    const amt = parseFloat(amount)
    if (!amt || amt < 10) return setError('Minimum withdrawal is $10')
    if (!wallet) return setError('Enter wallet address')
    setLoading(true)
    const res = await fetch('/api/referral/withdraw', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount: amt, wallet_address: wallet, network }),
    })
    const data = await res.json()
    setLoading(false)
    if (!res.ok) return setError(data.error || 'Error')
    setShowWithdraw(false)
    setAmount('')
    setWallet('')
    router.refresh()
  }

  return (
    <div className="p-6 rounded-2xl glass-card mb-8">
      <h2 className="text-lg font-bold mb-6">Referral Program</h2>

      {/* Referral URL */}
      <div className="mb-6">
        <div className="text-sm text-text-muted mb-2">Your Referral URL</div>
        <div className="flex flex-col sm:flex-row gap-2 items-stretch sm:items-center">
          <code className="flex-1 px-4 py-2.5 rounded-lg glass-soft text-player font-mono text-xs sm:text-sm break-all">
            {referralUrl}
          </code>
          <button
            onClick={copyUrl}
            className="btn-outline px-4 py-2.5 text-sm w-full sm:w-auto flex-shrink-0"
          >
            {copied ? 'Copied!' : 'Copy'}
          </button>
        </div>
      </div>

      {/* Commission Balance */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
        <div className="p-4 rounded-xl glass-soft">
          <div className="text-xs text-text-dim mb-1">Total Earned</div>
          <div className="text-lg sm:text-xl font-black text-green-400">${totalEarned.toFixed(2)}</div>
        </div>
        <div className="p-4 rounded-xl glass-soft">
          <div className="text-xs text-text-dim mb-1">Withdrawn</div>
          <div className="text-lg sm:text-xl font-black text-text-muted">${totalWithdrawn.toFixed(2)}</div>
        </div>
        <div className="p-4 rounded-xl glass-soft">
          <div className="text-xs text-text-dim mb-1">Available</div>
          <div className="text-lg sm:text-xl font-black text-player">${available.toFixed(2)}</div>
        </div>
      </div>

      {/* Referred Users */}
      {referred.length > 0 && (
        <div className="mb-6">
          <div className="text-sm text-text-muted mb-3">Referred Users ({referred.length})</div>
          <div className="overflow-x-auto">
            <table className="min-w-[640px] w-full text-sm">
              <thead>
                <tr className="text-text-muted text-left border-b border-accent/10">
                  <th className="pb-2">Email</th>
                  <th className="pb-2">Joined</th>
                  <th className="pb-2">Total Charged</th>
                  <th className="pb-2">Your Commission</th>
                </tr>
              </thead>
              <tbody>
                {referred.map(r => (
                  <tr key={r.id} className="border-b border-accent/10">
                    <td className="py-2 text-text">{r.email}</td>
                    <td className="py-2 text-text-muted">{new Date(r.created_at).toLocaleDateString()}</td>
                    <td className="py-2 font-bold">${r.total_charged.toFixed(2)}</td>
                    <td className="py-2 text-green-400 font-bold">+${r.commission.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Withdrawal Requests */}
      {withdrawals.length > 0 && (
        <div className="mb-6">
          <div className="text-sm text-text-muted mb-3">Withdrawal History</div>
          <div className="space-y-2">
            {withdrawals.map((w: any) => (
              <div key={w.id} className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 p-3 rounded-lg glass-soft text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-bold">${Number(w.amount).toFixed(2)}</span>
                  <span className="text-text-muted">{w.network}</span>
                  <span className="text-text-dim text-xs">{new Date(w.created_at).toLocaleDateString()}</span>
                </div>
                <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                  w.status === 'approved' ? 'bg-green-500/20 text-green-400' :
                  w.status === 'rejected' ? 'bg-banker/20 text-banker' :
                  'bg-yellow-500/20 text-yellow-400'
                }`}>
                  {w.status}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Withdraw Button */}
      {available >= 10 && !showWithdraw && (
        <button
          onClick={() => setShowWithdraw(true)}
          className="btn-primary px-6 py-3 text-sm"
        >
          Request Withdrawal
        </button>
      )}

      {showWithdraw && (
        <div className="p-5 rounded-xl glass-soft space-y-4">
          <div className="text-sm font-bold mb-2">Withdrawal Request</div>
          <div>
            <label className="text-xs text-text-dim mb-1 block">Amount (USD) — Available: ${available.toFixed(2)}</label>
            <input
              type="number" value={amount} onChange={e => setAmount(e.target.value)}
              placeholder="10.00" min="10" max={available}
              className="input-field text-sm"
            />
          </div>
          <div>
            <label className="text-xs text-text-dim mb-1 block">Network</label>
            <div className="flex flex-wrap gap-2">
              {['TRC-20', 'ERC-20'].map(n => (
                <button key={n} onClick={() => setNetwork(n)}
                  className={`px-4 py-2 rounded-lg text-sm font-semibold transition ${network === n ? 'bg-accent text-black' : 'bg-bg-card text-text-muted border border-accent/20'}`}>
                  {n}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className="text-xs text-text-dim mb-1 block">USDT Wallet Address</label>
            <input
              type="text" value={wallet} onChange={e => setWallet(e.target.value)}
              placeholder="T... or 0x..."
              className="input-field text-sm font-mono"
            />
          </div>
          {error && <p className="text-banker text-xs">{error}</p>}
          <div className="flex flex-col sm:flex-row gap-3">
            <button onClick={submitWithdraw} disabled={loading}
              className="btn-primary px-5 py-2.5 text-sm disabled:opacity-50 w-full sm:w-auto">
              {loading ? 'Submitting...' : 'Submit'}
            </button>
            <button onClick={() => { setShowWithdraw(false); setError('') }}
              className="btn-outline px-5 py-2.5 text-sm w-full sm:w-auto">
              Cancel
            </button>
          </div>
        </div>
      )}

      {available < 10 && available > 0 && (
        <p className="text-xs text-text-dim mt-2">Minimum withdrawal is $10. Keep earning to reach the threshold.</p>
      )}
    </div>
  )
}
