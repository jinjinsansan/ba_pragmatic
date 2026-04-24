'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'

const DEFAULT_BOT_CONFIG = {
  players_primary: 10,
  relax_wait_sec: 60,
  min_hands: 20,
  max_hands: 40,
  dragon_limit: 5,
  require_pb: true,
}

export default function UserRow({ user, billing }: { user: any; billing: any }) {
  const [rate, setRate] = useState(billing?.profit_share_rate ? (billing.profit_share_rate * 100).toString() : '20')
  const [loading, setLoading] = useState(false)
  const [showConfig, setShowConfig] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [showUrlInput, setShowUrlInput] = useState(false)
  const [zipUrl, setZipUrl] = useState('')
  const isActive = !billing?.suspended

  async function sendZipUrl() {
    if (!zipUrl.startsWith('http')) { alert('正しいURLを入力してください'); return }
    setUploading(true)
    const res = await fetch('/api/admin/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId: user.id, version: '1.0.4', url: zipUrl }),
    })
    if (res.ok) { alert('ZIP送付完了'); setShowUrlInput(false); setZipUrl(''); router.refresh() }
    else { alert('エラー: ' + (await res.text())) }
    setUploading(false)
  }
  const cfg = { ...DEFAULT_BOT_CONFIG, ...(billing?.bot_config || {}) }
  const router = useRouter()

  async function updateUser(action: string, value?: any) {
    setLoading(true)
    const res = await fetch('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId: user.id, action, value }),
    })
    if (res.ok) router.refresh()
    else alert('Error: ' + (await res.text()))
    setLoading(false)
  }

  const isSuspended = billing?.suspended
  const isFree = billing?.is_free
  const isBotPaid = billing?.bot_paid

  return (
    <>
      <tr className="border-b border-white/5">
        <td className="py-3">
          <div>{user.email}</div>
          {user.is_admin && <span className="text-xs text-accent bg-accent/10 px-1.5 py-0.5 rounded">管理者</span>}
        </td>
        <td className="py-3 font-bold">${billing?.balance?.toFixed(2) || '0.00'}</td>
        <td className="py-3">
          <div className="flex items-center gap-2">
            <input
              type="number" min="0" max="100" value={rate}
              onChange={e => setRate(e.target.value)}
              className="w-16 px-2 py-1 rounded bg-bg-primary border border-white/10 text-white text-sm"
            />
            <span className="text-slate-500">%</span>
            <button
              onClick={() => updateUser('set_rate', parseFloat(rate) / 100)}
              disabled={loading}
              className="px-2 py-1 rounded text-xs bg-player/20 text-player hover:bg-player/30 transition disabled:opacity-50"
            >
              設定
            </button>
          </div>
        </td>
        <td className="py-3">
          {isFree ? (
            <span className="px-2 py-0.5 rounded text-xs bg-accent/20 text-accent">無料</span>
          ) : isSuspended ? (
            <span className="px-2 py-0.5 rounded text-xs bg-banker/20 text-banker">停止中</span>
          ) : billing?.balance > 0 ? (
            <span className="px-2 py-0.5 rounded text-xs bg-green-500/20 text-green-400">有効</span>
          ) : isBotPaid ? (
            <span className="px-2 py-0.5 rounded text-xs bg-yellow-500/20 text-yellow-400">ライセンス済</span>
          ) : (
            <span className="px-2 py-0.5 rounded text-xs bg-slate-500/20 text-slate-400">未購入</span>
          )}
        </td>
        <td className="py-3 font-mono text-xs text-slate-500">{user.referral_code}</td>
        <td className="py-3 text-slate-500">{new Date(user.created_at).toLocaleDateString('ja-JP')}</td>
        <td className="py-3">
          <div className="flex gap-2 flex-wrap">
            <button
              onClick={() => updateUser(isSuspended ? 'unsuspend' : 'suspend')}
              disabled={loading}
              className={`px-2 py-1 rounded text-xs transition disabled:opacity-50 ${isSuspended ? 'bg-green-500/20 text-green-400' : 'bg-banker/20 text-banker'}`}
            >
              {isSuspended ? '停止解除' : '停止'}
            </button>
            <button
              onClick={() => updateUser('free_license')}
              disabled={loading}
              className="px-2 py-1 rounded text-xs bg-accent/20 text-accent hover:bg-accent/30 transition disabled:opacity-50"
            >
              ライセンス無料
            </button>
            <button
              onClick={() => updateUser('free_charge')}
              disabled={loading}
              className="px-2 py-1 rounded text-xs bg-accent/20 text-accent hover:bg-accent/30 transition disabled:opacity-50"
            >
              チャージ無料
            </button>
            <button
              onClick={() => updateUser('free_both')}
              disabled={loading}
              className="px-2 py-1 rounded text-xs bg-accent/20 text-accent hover:bg-accent/30 transition disabled:opacity-50"
            >
              両方無料
            </button>
            <button
              onClick={() => updateUser('unfree_charge')}
              disabled={loading || !isFree}
              className="px-2 py-1 rounded text-xs bg-white/10 text-slate-300 hover:text-white transition disabled:opacity-50"
            >
              無料解除
            </button>
            <button
              onClick={() => updateUser(isActive ? 'deactivate' : 'activate')}
              disabled={loading}
              className={`px-2 py-1 rounded text-xs transition disabled:opacity-50 ${isActive ? 'bg-slate-500/20 text-slate-400' : 'bg-green-500/20 text-green-400 hover:bg-green-500/30'}`}
            >
              {isActive ? '無効化' : 'Activate'}
            </button>
            <button
              onClick={() => setShowUrlInput(v => !v)}
              className="px-2 py-1 rounded text-xs bg-purple-500/20 text-purple-400 hover:bg-purple-500/30 transition"
            >
              ZIP送付
            </button>
            <button
              onClick={() => setShowConfig(v => !v)}
              className="px-2 py-1 rounded text-xs bg-white/5 text-slate-400 hover:text-white transition"
            >
              {showConfig ? '▲ 設定' : '▼ 設定'}
            </button>
          </div>
        </td>
      </tr>

      {showConfig && (
        <tr className="border-b border-white/5 bg-white/[0.02]">
          <td colSpan={7} className="py-3 px-4">
            <div className="text-[10px] text-slate-500 mb-2 tracking-widest">TABLE FILTER</div>
            <div className="flex flex-wrap gap-2 text-xs">
              <span className="px-2 py-0.5 rounded bg-white/5 text-slate-300">PRIMARY ≥ <b>{cfg.players_primary}</b>人</span>
              <span className="px-2 py-0.5 rounded bg-white/5 text-slate-300">RELAX <b>{cfg.relax_wait_sec}</b>秒</span>
              <span className="px-2 py-0.5 rounded bg-white/5 text-slate-300">HANDS <b>{cfg.min_hands}〜{cfg.max_hands}</b></span>
              <span className="px-2 py-0.5 rounded bg-white/5 text-slate-300">DRAGON ≥ <b>{cfg.dragon_limit === 0 ? 'OFF' : cfg.dragon_limit}</b></span>
              <span className={`px-2 py-0.5 rounded text-xs font-bold ${cfg.require_pb ? 'bg-player/20 text-player' : 'bg-white/5 text-slate-500'}`}>
                P{'>'} B: {cfg.require_pb ? 'ON' : 'OFF'}
              </span>
            </div>
          </td>
        </tr>
      )}
      {showUrlInput && (
        <tr className="border-b border-white/5 bg-purple-500/5">
          <td colSpan={7} className="py-3 px-4">
            <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
              <input
                type="url"
                placeholder="GitHub Release URL (https://github.com/...)"
                value={zipUrl}
                onChange={e => setZipUrl(e.target.value)}
                className="flex-1 bg-white/5 border border-white/10 rounded px-3 py-1.5 text-xs text-white placeholder-slate-500 outline-none"
              />
              <button
                onClick={sendZipUrl}
                disabled={uploading}
                className="px-3 py-1.5 rounded text-xs bg-purple-500/30 text-purple-300 hover:bg-purple-500/50 transition disabled:opacity-50 w-full sm:w-auto"
              >
                {uploading ? '送付中...' : '送付'}
              </button>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}
