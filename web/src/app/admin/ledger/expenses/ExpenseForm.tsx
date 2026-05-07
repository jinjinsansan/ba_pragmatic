'use client'

import { useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'
import type { ExpenseWithdrawalComputed } from '@/lib/ledger/types'
import { formatCurrency } from '@/lib/ledger/calc'

const initial = {
  date: new Date().toISOString().slice(0, 10),
  source: '',
  fromReserve: '',
  fromAccount2: '',
  j: '',
  k: '',
  kBro: '',
  company: '',
  aiDev: '',
  notes: '',
}

export default function ExpenseForm({
  investorId,
  computed,
}: {
  investorId: string
  computed: ExpenseWithdrawalComputed[]
}) {
  const router = useRouter()
  const [isPending, startTransition] = useTransition()
  const [f, setF] = useState(initial)
  const [error, setError] = useState<string | null>(null)

  const set = (k: keyof typeof initial) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setF((prev) => ({ ...prev, [k]: e.target.value }))

  // ライブ検算
  const total =
    parseFloat(f.fromReserve || '0') + parseFloat(f.fromAccount2 || '0')
  const internal =
    parseFloat(f.j || '0') +
    parseFloat(f.k || '0') +
    parseFloat(f.kBro || '0') +
    parseFloat(f.company || '0') +
    parseFloat(f.aiDev || '0')
  const balanced = Math.abs(total - internal) < 0.01

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!balanced) return setError(`出金合計 ${total.toFixed(2)} と配分合計 ${internal.toFixed(2)} が一致していません`)

    const res = await fetch('/api/admin/ledger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        table: 'expense',
        action: 'upsert',
        payload: {
          investor_id: investorId,
          withdrawal_date: f.date,
          source_label: f.source || null,
          withdraw_from_reserve: parseFloat(f.fromReserve || '0'),
          withdraw_from_account2: parseFloat(f.fromAccount2 || '0'),
          j_received: parseFloat(f.j || '0'),
          k_received: parseFloat(f.k || '0'),
          k_brother_received: parseFloat(f.kBro || '0'),
          company_received: parseFloat(f.company || '0'),
          ai_dev_expense: parseFloat(f.aiDev || '0'),
          notes: f.notes || null,
        },
      }),
    })

    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      setError(j.error || `エラー: ${res.status}`)
      return
    }
    setF(initial)
    startTransition(() => router.refresh())
  }

  async function remove(id: string) {
    if (!confirm('この経費出金イベントを削除しますか?')) return
    const res = await fetch('/api/admin/ledger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ table: 'expense', action: 'delete', id }),
    })
    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      alert(j.error || `削除失敗: ${res.status}`)
      return
    }
    startTransition(() => router.refresh())
  }

  const totalReserve = computed.reduce((a, e) => a + e.withdrawFromReserve, 0)
  const totalAcc2 = computed.reduce((a, e) => a + e.withdrawFromAccount2, 0)
  const totalJ = computed.reduce((a, e) => a + e.jReceived, 0)
  const totalK = computed.reduce((a, e) => a + e.kReceived, 0)
  const totalKBro = computed.reduce((a, e) => a + e.kBrotherReceived, 0)
  const totalCompany = computed.reduce((a, e) => a + e.companyReceived, 0)
  const totalAiDev = computed.reduce((a, e) => a + e.aiDevExpense, 0)

  return (
    <div className="space-y-6">
      <form onSubmit={submit} className="glass-card p-4 rounded-xl">
        <div className="text-sm text-text-muted mb-3">経費出金イベント追加</div>

        <div className="grid grid-cols-1 sm:grid-cols-12 gap-3 mb-4">
          <label className="sm:col-span-3 flex flex-col gap-1">
            <span className="text-xs text-text-muted">出金日</span>
            <input type="date" value={f.date} onChange={set('date')}
              className="px-3 py-2 rounded bg-black/40 border border-text-muted/30" required />
          </label>
          <label className="sm:col-span-9 flex flex-col gap-1">
            <span className="text-xs text-text-muted">ソースラベル (任意、例: 別+2つめ)</span>
            <input type="text" value={f.source} onChange={set('source')}
              className="px-3 py-2 rounded bg-black/40 border border-text-muted/30" />
          </label>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
          {/* 出金元 */}
          <div className="rounded-lg border border-orange-500/30 p-3 bg-orange-500/5">
            <div className="text-xs text-orange-400 font-semibold mb-3">出金元 (どこから引き出したか)</div>
            <div className="space-y-2">
              <label className="grid grid-cols-2 items-center gap-2">
                <span className="text-sm text-text-muted">別チャージから</span>
                <input type="number" step="0.01" value={f.fromReserve} onChange={set('fromReserve')}
                  className="px-2 py-1 rounded bg-black/40 border border-text-muted/30 text-right font-mono" placeholder="0.00" />
              </label>
              <label className="grid grid-cols-2 items-center gap-2">
                <span className="text-sm text-text-muted">2つめ口座から</span>
                <input type="number" step="0.01" value={f.fromAccount2} onChange={set('fromAccount2')}
                  className="px-2 py-1 rounded bg-black/40 border border-text-muted/30 text-right font-mono" placeholder="0.00" />
              </label>
              <div className="grid grid-cols-2 items-center gap-2 pt-2 border-t border-text-muted/20">
                <span className="text-sm font-bold">合計</span>
                <span className="text-right font-mono font-bold text-orange-300">{formatCurrency(total)}</span>
              </div>
            </div>
          </div>

          {/* 配分先 */}
          <div className="rounded-lg border border-purple-500/30 p-3 bg-purple-500/5">
            <div className="text-xs text-purple-400 font-semibold mb-3">配分先 (誰がいくら受け取ったか)</div>
            <div className="space-y-2">
              <label className="grid grid-cols-2 items-center gap-2">
                <span className="text-sm text-text-muted">J 取り分</span>
                <input type="number" step="0.01" value={f.j} onChange={set('j')}
                  className="px-2 py-1 rounded bg-black/40 border border-text-muted/30 text-right font-mono" placeholder="0.00" />
              </label>
              <label className="grid grid-cols-2 items-center gap-2">
                <span className="text-sm text-text-muted">K 取り分</span>
                <input type="number" step="0.01" value={f.k} onChange={set('k')}
                  className="px-2 py-1 rounded bg-black/40 border border-text-muted/30 text-right font-mono" placeholder="0.00" />
              </label>
              <label className="grid grid-cols-2 items-center gap-2">
                <span className="text-sm text-text-muted">K の兄</span>
                <input type="number" step="0.01" value={f.kBro} onChange={set('kBro')}
                  className="px-2 py-1 rounded bg-black/40 border border-text-muted/30 text-right font-mono" placeholder="0.00" />
              </label>
              <label className="grid grid-cols-2 items-center gap-2">
                <span className="text-sm text-text-muted">会社 (配当金)</span>
                <input type="number" step="0.01" value={f.company} onChange={set('company')}
                  className="px-2 py-1 rounded bg-black/40 border border-text-muted/30 text-right font-mono" placeholder="0.00" />
              </label>
              <label className="grid grid-cols-2 items-center gap-2">
                <span className="text-sm text-text-muted">AI 開発費等</span>
                <input type="number" step="0.01" value={f.aiDev} onChange={set('aiDev')}
                  className="px-2 py-1 rounded bg-black/40 border border-text-muted/30 text-right font-mono" placeholder="0.00" />
              </label>
              <div className="grid grid-cols-2 items-center gap-2 pt-2 border-t border-text-muted/20">
                <span className="text-sm font-bold">合計</span>
                <span className="text-right font-mono font-bold text-purple-300">{formatCurrency(internal)}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="mt-4 flex items-center justify-between">
          <div className={`text-sm font-mono ${balanced ? 'text-emerald-400' : 'text-red-400'}`}>
            {balanced ? `✓ 検算 OK (差額 0)` : `✗ 不一致 (差額 ${formatCurrency(Math.abs(total - internal))})`}
          </div>
          <div className="flex gap-2">
            <button type="button" onClick={() => setF(initial)}
              className="px-4 py-2 rounded border border-text-muted/30 text-text-muted hover:text-text">リセット</button>
            <button type="submit" disabled={isPending || !balanced}
              className="px-6 py-2 rounded bg-purple-500/20 border border-purple-400 text-purple-300 hover:bg-purple-500/30 disabled:opacity-40">
              {isPending ? '保存中…' : '保存'}
            </button>
          </div>
        </div>

        <label className="block mt-3">
          <span className="text-xs text-text-muted">メモ</span>
          <input type="text" value={f.notes} onChange={set('notes')}
            className="w-full mt-1 px-3 py-2 rounded bg-black/40 border border-text-muted/30" />
        </label>

        {error && <div className="text-red-400 text-sm mt-2">{error}</div>}
      </form>

      {/* 履歴テーブル */}
      <div className="glass-card p-4 rounded-xl overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="text-text-muted text-left border-b border-accent/10">
            <tr>
              <th className="pb-2 px-2">日付</th>
              <th className="pb-2 px-2">ソース</th>
              <th className="pb-2 px-2 text-right text-orange-400">別から</th>
              <th className="pb-2 px-2 text-right text-orange-400">2つめから</th>
              <th className="pb-2 px-2 text-right text-purple-400">J</th>
              <th className="pb-2 px-2 text-right text-purple-400">K</th>
              <th className="pb-2 px-2 text-right text-purple-400">兄</th>
              <th className="pb-2 px-2 text-right text-purple-400">会社</th>
              <th className="pb-2 px-2 text-right text-purple-400">AI</th>
              <th className="pb-2 px-2 text-right">合計</th>
              <th className="pb-2 px-2"></th>
              <th className="pb-2 px-2">操作</th>
            </tr>
          </thead>
          <tbody>
            {computed.length === 0 ? (
              <tr>
                <td colSpan={12} className="py-8 text-center text-text-muted">まだ経費出金がありません</td>
              </tr>
            ) : (
              computed.map((e) => (
                <tr key={e.id} className="border-b border-text-muted/10 hover:bg-accent/5">
                  <td className="py-2 px-2 font-mono">{e.withdrawalDate}</td>
                  <td className="py-2 px-2 text-xs">{e.sourceLabel ?? ''}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.withdrawFromReserve)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.withdrawFromAccount2)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.jReceived)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.kReceived)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.kBrotherReceived)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.companyReceived)}</td>
                  <td className="py-2 px-2 text-right font-mono">{formatCurrency(e.aiDevExpense)}</td>
                  <td className="py-2 px-2 text-right font-mono font-bold">{formatCurrency(e.totalWithdrawal)}</td>
                  <td className="py-2 px-2 text-center">
                    {e.isBalanced
                      ? <span className="text-emerald-400 text-xs">✓</span>
                      : <span className="text-red-400 text-xs" title="不一致">⚠</span>}
                  </td>
                  <td className="py-2 px-2">
                    <button onClick={() => e.id && remove(e.id)}
                      className="text-red-400 hover:text-red-300 text-xs"
                      disabled={!e.id || isPending}>削除</button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
          {computed.length > 0 && (
            <tfoot className="border-t-2 border-accent/30">
              <tr className="font-bold">
                <td className="py-2 px-2" colSpan={2}>累計</td>
                <td className="py-2 px-2 text-right font-mono">{formatCurrency(totalReserve)}</td>
                <td className="py-2 px-2 text-right font-mono">{formatCurrency(totalAcc2)}</td>
                <td className="py-2 px-2 text-right font-mono">{formatCurrency(totalJ)}</td>
                <td className="py-2 px-2 text-right font-mono">{formatCurrency(totalK)}</td>
                <td className="py-2 px-2 text-right font-mono">{formatCurrency(totalKBro)}</td>
                <td className="py-2 px-2 text-right font-mono">{formatCurrency(totalCompany)}</td>
                <td className="py-2 px-2 text-right font-mono">{formatCurrency(totalAiDev)}</td>
                <td className="py-2 px-2 text-right font-mono">{formatCurrency(totalReserve + totalAcc2)}</td>
                <td colSpan={2}></td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    </div>
  )
}
