'use client'

import { useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'
import type { Account1Computed, DistributionRule } from '@/lib/ledger/types'
import { formatCurrency } from '@/lib/ledger/calc'

export default function Account1Form({
  investorId,
  computed,
  rule,
}: {
  investorId: string
  computed: Account1Computed[]
  rule: DistributionRule | null
}) {
  const router = useRouter()
  const [isPending, startTransition] = useTransition()
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10))
  const [profit, setProfit] = useState('')
  const [notes, setNotes] = useState('')
  const [error, setError] = useState<string | null>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!profit) return setError('利益額を入力してください')
    if (!rule) return setError('分配ルールが未設定です')

    const res = await fetch('/api/admin/ledger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        table: 'account1',
        action: 'upsert',
        payload: {
          investor_id: investorId,
          trade_date: date,
          daily_profit: parseFloat(profit),
          notes: notes || null,
        },
      }),
    })

    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      setError(j.error || `エラー: ${res.status}`)
      return
    }
    setProfit('')
    setNotes('')
    startTransition(() => router.refresh())
  }

  async function remove(id: string) {
    if (!confirm('この日次レコードを削除しますか?')) return
    const res = await fetch('/api/admin/ledger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ table: 'account1', action: 'delete', id }),
    })
    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      alert(j.error || `削除失敗: ${res.status}`)
      return
    }
    startTransition(() => router.refresh())
  }

  // 集計
  const totalProfit = computed.reduce((a, e) => a + e.dailyProfit, 0)
  const totalInvestor = computed.reduce((a, e) => a + e.investorShare, 0)
  const totalCharge = computed.reduce((a, e) => a + e.chargeRefund, 0)

  return (
    <div className="space-y-6">
      {/* 入力フォーム */}
      <form onSubmit={submit} className="glass-card p-4 rounded-xl">
        <div className="text-sm text-text-muted mb-3">新規/更新 (同じ日付は上書き)</div>
        <div className="grid grid-cols-1 sm:grid-cols-12 gap-3">
          <label className="sm:col-span-3 flex flex-col gap-1">
            <span className="text-xs text-text-muted">取引日</span>
            <input
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className="px-3 py-2 rounded bg-black/40 border border-text-muted/30 text-text"
              required
            />
          </label>
          <label className="sm:col-span-3 flex flex-col gap-1">
            <span className="text-xs text-text-muted">日次利益 (USD)</span>
            <input
              type="number"
              step="0.01"
              value={profit}
              onChange={(e) => setProfit(e.target.value)}
              className="px-3 py-2 rounded bg-black/40 border border-text-muted/30 text-text font-mono"
              placeholder="0.00"
              required
            />
          </label>
          <label className="sm:col-span-4 flex flex-col gap-1">
            <span className="text-xs text-text-muted">メモ (任意)</span>
            <input
              type="text"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="px-3 py-2 rounded bg-black/40 border border-text-muted/30 text-text"
            />
          </label>
          <div className="sm:col-span-2 flex items-end">
            <button
              type="submit"
              disabled={isPending}
              className="w-full px-4 py-2 rounded bg-emerald-500/20 border border-emerald-400 text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-50"
            >
              {isPending ? '保存中…' : '保存'}
            </button>
          </div>
        </div>
        {error && <div className="text-red-400 text-sm mt-2">{error}</div>}
      </form>

      {/* テーブル */}
      <div className="glass-card p-4 rounded-xl overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="text-text-muted text-left border-b border-accent/10">
            <tr>
              <th className="pb-2 px-2">日付</th>
              <th className="pb-2 px-2 text-right">日次利益</th>
              <th className="pb-2 px-2 text-right text-emerald-400">投資家 (20%)</th>
              <th className="pb-2 px-2 text-right text-amber-400">J (20%)</th>
              <th className="pb-2 px-2 text-right text-amber-400">K (30%)</th>
              <th className="pb-2 px-2 text-right text-amber-400">会社 (30%)</th>
              <th className="pb-2 px-2 text-right">チャージ返金 (80%)</th>
              <th className="pb-2 px-2 text-right">受取累計</th>
              <th className="pb-2 px-2 text-right">残チャージ</th>
              <th className="pb-2 px-2">操作</th>
            </tr>
          </thead>
          <tbody>
            {computed.length === 0 ? (
              <tr>
                <td colSpan={10} className="py-8 text-center text-text-muted">
                  まだ入力がありません
                </td>
              </tr>
            ) : (
              computed.map((e) => (
                <tr key={e.id ?? e.tradeDate} className="border-b border-text-muted/10 hover:bg-accent/5">
                  <td className="py-2 px-2 font-mono">{e.tradeDate}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.dailyProfit)}</td>
                  <td className="py-2 px-2 text-right font-mono text-emerald-300">{formatCurrency(e.investorShare)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.jShare)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.kShare)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.companyShare)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.chargeRefund)}</td>
                  <td className="py-2 px-2 text-right font-mono text-emerald-200 font-bold">{formatCurrency(e.investorTotalAfter)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.chargeBalanceAfter)}</td>
                  <td className="py-2 px-2">
                    <button
                      onClick={() => e.id && remove(e.id)}
                      className="text-red-400 hover:text-red-300 text-xs"
                      disabled={!e.id || isPending}
                    >
                      削除
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
          {computed.length > 0 && (
            <tfoot className="border-t-2 border-accent/30">
              <tr className="font-bold">
                <td className="py-2 px-2">合計</td>
                <td className="py-2 px-2 text-right font-mono">{formatCurrency(totalProfit)}</td>
                <td className="py-2 px-2 text-right font-mono text-emerald-300">{formatCurrency(totalInvestor)}</td>
                <td className="py-2 px-2 text-right font-mono">
                  {formatCurrency(computed.reduce((a, e) => a + e.jShare, 0))}
                </td>
                <td className="py-2 px-2 text-right font-mono">
                  {formatCurrency(computed.reduce((a, e) => a + e.kShare, 0))}
                </td>
                <td className="py-2 px-2 text-right font-mono">
                  {formatCurrency(computed.reduce((a, e) => a + e.companyShare, 0))}
                </td>
                <td className="py-2 px-2 text-right font-mono">{formatCurrency(totalCharge)}</td>
                <td colSpan={3}></td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    </div>
  )
}
