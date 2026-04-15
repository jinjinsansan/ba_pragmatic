'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'

export default function PromoActions({ promos }: { promos: any[] }) {
  const [code, setCode] = useState('')
  const [type, setType] = useState('package_free')
  const [maxUses, setMaxUses] = useState('10')
  const [discount, setDiscount] = useState('100')
  const [loading, setLoading] = useState(false)
  const router = useRouter()

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    const res = await fetch('/api/admin/promos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'create',
        code: code.toUpperCase(),
        type,
        max_uses: parseInt(maxUses),
        discount_percent: parseInt(discount),
      }),
    })
    if (res.ok) {
      setCode('')
      router.refresh()
    } else alert('Error: ' + (await res.text()))
    setLoading(false)
  }

  async function handleToggle(id: string, active: boolean) {
    await fetch('/api/admin/promos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: active ? 'deactivate' : 'activate', id }),
    })
    router.refresh()
  }

  return (
    <>
      <form onSubmit={handleCreate} className="p-6 rounded-2xl glass-card mb-8">
        <h2 className="text-lg font-bold mb-4">プロモコード作成</h2>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
          <div>
            <label className="block text-sm text-text-muted mb-1">コード</label>
            <input
              value={code} onChange={e => setCode(e.target.value)} required
              className="input-field text-sm"
              placeholder="BETA2026"
            />
          </div>
          <div>
            <label className="block text-sm text-text-muted mb-1">種別</label>
            <select
              value={type} onChange={e => setType(e.target.value)}
              className="input-field text-sm"
            >
              <option value="package_free">パッケージ無料</option>
              <option value="charge_free">チャージ無料</option>
              <option value="discount">割引 %</option>
            </select>
          </div>
          <div>
            <label className="block text-sm text-text-muted mb-1">最大利用回数</label>
            <input
              type="number" value={maxUses} onChange={e => setMaxUses(e.target.value)}
              className="input-field text-sm"
            />
          </div>
          <div>
            <label className="block text-sm text-text-muted mb-1">割引率 %</label>
            <input
              type="number" value={discount} onChange={e => setDiscount(e.target.value)}
              className="input-field text-sm"
            />
          </div>
        </div>
        <button type="submit" disabled={loading} className="btn-primary px-6 py-2 text-sm disabled:opacity-50 w-full sm:w-auto">
          {loading ? '作成中...' : '作成'}
        </button>
      </form>

      <div className="overflow-x-auto">
        <table className="min-w-[640px] w-full text-sm">
          <thead><tr className="text-text-muted text-left border-b border-accent/10">
            <th className="pb-3">コード</th><th className="pb-3">種別</th><th className="pb-3">割引率</th><th className="pb-3">利用数</th><th className="pb-3">状態</th><th className="pb-3">操作</th>
          </tr></thead>
          <tbody>
            {promos.map(p => (
              <tr key={p.id} className="border-b border-accent/10">
                <td className="py-3 font-mono text-player">{p.code}</td>
                <td className="py-3">{p.type === 'package_free' ? 'パッケージ無料' : p.type === 'charge_free' ? 'チャージ無料' : '割引'}</td>
                <td className="py-3">{p.discount_percent}%</td>
                <td className="py-3">{p.used_count}/{p.max_uses}</td>
                <td className="py-3">
                  <span className={`px-2 py-0.5 rounded text-xs ${p.active ? 'bg-green-500/20 text-green-400' : 'bg-slate-500/20 text-slate-400'}`}>
                    {p.active ? '有効' : '無効'}
                  </span>
                </td>
                <td className="py-3">
                  <button
                    onClick={() => handleToggle(p.id, p.active)}
                    className={`px-3 py-1 rounded text-xs ${p.active ? 'bg-banker/20 text-banker' : 'bg-green-500/20 text-green-400'}`}
                  >
                    {p.active ? '無効化' : '有効化'}
                  </button>
                </td>
              </tr>
            ))}
            {!promos.length && <tr><td colSpan={6} className="py-6 text-center text-text-muted">プロモコードはまだありません</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  )
}
