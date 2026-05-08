'use client'

import { useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'

const CATEGORY_SUGGESTIONS = [
  '給与',
  '報酬',
  '会社配当',
  '賃料',
  '設備投資',
  'AI 開発費',
  'ソフトウェア',
  '税金',
  '通信費',
  '交通費',
  'その他',
]

const initial = {
  date: new Date().toISOString().slice(0, 10),
  category: '',
  recipient: '',
  amount: '',
  notes: '',
}

const fmt = (n: number | string | null | undefined): string => {
  const v = typeof n === 'number' ? n : parseFloat(String(n ?? 0))
  if (!Number.isFinite(v) || v === 0) return '-'
  const formatted = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD',
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(Math.abs(v))
  return v < 0 ? `(${formatted})` : formatted
}

export default function ExpenseBreakdownForm({
  investorId,
  entries,
}: {
  investorId: string
  entries: any[]
}) {
  const router = useRouter()
  const [isPending, startTransition] = useTransition()
  const [f, setF] = useState(initial)
  const [error, setError] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)

  const set = (k: keyof typeof initial) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setF((prev) => ({ ...prev, [k]: e.target.value }))

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!f.category) return setError('カテゴリを入力してください')
    if (!f.amount || parseFloat(f.amount) <= 0) return setError('金額を入力してください')

    const payload: any = {
      investor_id: investorId,
      expense_date: f.date,
      category: f.category,
      recipient: f.recipient || null,
      amount: parseFloat(f.amount),
      notes: f.notes || null,
    }
    if (editingId) payload.id = editingId

    const res = await fetch('/api/admin/ledger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ table: 'company_breakdown', action: 'upsert', payload }),
    })

    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      setError(j.error || `エラー: ${res.status}`)
      return
    }
    setF(initial)
    setEditingId(null)
    startTransition(() => router.refresh())
  }

  function startEdit(entry: any) {
    setEditingId(entry.id)
    setF({
      date: entry.expense_date,
      category: entry.category,
      recipient: entry.recipient ?? '',
      amount: String(entry.amount),
      notes: entry.notes ?? '',
    })
  }

  function cancelEdit() {
    setEditingId(null)
    setF(initial)
    setError(null)
  }

  async function remove(id: string) {
    if (!confirm('この内訳エントリを削除しますか?')) return
    const res = await fetch('/api/admin/ledger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ table: 'company_breakdown', action: 'delete', id }),
    })
    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      alert(j.error || `削除失敗: ${res.status}`)
      return
    }
    startTransition(() => router.refresh())
  }

  return (
    <div className="space-y-6">
      {/* 入力フォーム */}
      <form onSubmit={submit} className="glass-card p-4 rounded-xl">
        <div className="text-sm text-text-muted mb-3">
          {editingId ? '編集中' : '新規追加'}
          {editingId && (
            <button type="button" onClick={cancelEdit} className="ml-3 text-xs text-text-muted hover:text-text underline">
              キャンセル
            </button>
          )}
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-12 gap-3">
          <label className="sm:col-span-2 flex flex-col gap-1">
            <span className="text-xs text-text-muted">支出日</span>
            <input type="date" value={f.date} onChange={set('date')}
              className="px-3 py-2 rounded bg-black/40 border border-text-muted/30 text-text" required />
          </label>
          <label className="sm:col-span-3 flex flex-col gap-1">
            <span className="text-xs text-text-muted">カテゴリ *</span>
            <input type="text" value={f.category} onChange={set('category')}
              list="category-suggestions"
              placeholder="給与、賃料、設備…"
              className="px-3 py-2 rounded bg-black/40 border border-text-muted/30 text-text" required />
            <datalist id="category-suggestions">
              {CATEGORY_SUGGESTIONS.map(c => <option key={c} value={c} />)}
            </datalist>
          </label>
          <label className="sm:col-span-2 flex flex-col gap-1">
            <span className="text-xs text-text-muted">受取人 (任意)</span>
            <input type="text" value={f.recipient} onChange={set('recipient')}
              placeholder="K の兄、外部開発者…"
              className="px-3 py-2 rounded bg-black/40 border border-text-muted/30 text-text" />
          </label>
          <label className="sm:col-span-2 flex flex-col gap-1">
            <span className="text-xs text-text-muted">金額 (USD) *</span>
            <input type="number" step="0.01" value={f.amount} onChange={set('amount')}
              className="px-3 py-2 rounded bg-black/40 border border-text-muted/30 text-text font-mono"
              placeholder="0.00" required />
          </label>
          <label className="sm:col-span-3 flex flex-col gap-1">
            <span className="text-xs text-text-muted">メモ</span>
            <input type="text" value={f.notes} onChange={set('notes')}
              className="px-3 py-2 rounded bg-black/40 border border-text-muted/30 text-text" />
          </label>
          <div className="sm:col-span-12 flex items-center justify-end gap-2 mt-2">
            {error && <div className="text-red-400 text-sm mr-auto">{error}</div>}
            <button type="submit" disabled={isPending}
              className="px-6 py-2 rounded bg-purple-500/20 border border-purple-400 text-purple-300 hover:bg-purple-500/30 disabled:opacity-40">
              {isPending ? '保存中…' : (editingId ? '更新' : '追加')}
            </button>
          </div>
        </div>
      </form>

      {/* 履歴テーブル */}
      <div className="glass-card p-4 rounded-xl overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="text-text-muted text-left border-b border-accent/10">
            <tr>
              <th className="pb-2 px-2">支出日</th>
              <th className="pb-2 px-2">カテゴリ</th>
              <th className="pb-2 px-2">受取人</th>
              <th className="pb-2 px-2 text-right">金額</th>
              <th className="pb-2 px-2">メモ</th>
              <th className="pb-2 px-2">操作</th>
            </tr>
          </thead>
          <tbody>
            {entries.length === 0 ? (
              <tr>
                <td colSpan={6} className="py-8 text-center text-text-muted">まだ内訳がありません</td>
              </tr>
            ) : (
              entries.map((e) => (
                <tr key={e.id} className="border-b border-text-muted/10 hover:bg-accent/5">
                  <td className="py-2 px-2 font-mono">{e.expense_date}</td>
                  <td className="py-2 px-2">{e.category}</td>
                  <td className="py-2 px-2 text-text-muted text-xs">{e.recipient ?? ''}</td>
                  <td className="py-2 px-2 text-right font-mono font-bold">{fmt(e.amount)}</td>
                  <td className="py-2 px-2 text-xs text-text-muted">{e.notes ?? ''}</td>
                  <td className="py-2 px-2 whitespace-nowrap">
                    <button onClick={() => startEdit(e)}
                      className="text-blue-400 hover:text-blue-300 text-xs mr-2"
                      disabled={isPending}>編集</button>
                    <button onClick={() => remove(e.id)}
                      className="text-red-400 hover:text-red-300 text-xs"
                      disabled={isPending}>削除</button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
          {entries.length > 0 && (
            <tfoot className="border-t-2 border-accent/30">
              <tr className="font-bold">
                <td className="py-2 px-2" colSpan={3}>内訳合計</td>
                <td className="py-2 px-2 text-right font-mono">
                  {fmt(entries.reduce((a, e) => a + parseFloat(e.amount), 0))}
                </td>
                <td colSpan={2}></td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    </div>
  )
}
