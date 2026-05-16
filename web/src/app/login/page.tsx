'use client'

import { useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { createClient } from '@/lib/supabase-browser'

export default function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const router = useRouter()

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    setError('')

    const supabase = createClient()
    const { error: loginError } = await supabase.auth.signInWithPassword({ email, password })

    if (loginError) {
      setError(loginError.message)
      setLoading(false)
    } else {
      router.push('/dashboard')
      router.refresh()
    }
  }

  return (
    <div className="min-h-screen px-4 sm:px-6 py-10 sm:py-16 flex items-center">
      <div className="max-w-6xl w-full mx-auto grid lg:grid-cols-2 gap-8 lg:gap-12 items-center">
        <div className="hidden lg:block">
          <div className="hud-label mb-3">I · Member Access</div>
          <h1 className="text-5xl xl:text-6xl leading-tight font-hud mb-5">
            BAFATHER
            <br />
            <span className="text-accent">MEMBER LOGIN</span>
          </h1>
          <p className="text-text-muted leading-relaxed max-w-md">
            Sign in to continue daily operation, monitor settlements, and manage your runtime state.
          </p>
        </div>

        <div className="w-full max-w-md lg:ml-auto glass-card p-6 sm:p-8">
          <div className="hud-label text-center mb-2">BAFATHER ACCESS</div>
          <h1 className="text-2xl sm:text-3xl text-center mb-2 font-hud">Welcome Back</h1>
          <p className="text-center text-sm sm:text-base text-text-muted mb-8">Login to your operations dashboard</p>

          {error && (
            <div className="mb-6 p-4 rounded-xl bg-banker/10 border border-banker/30 text-banker text-sm text-center">
              {error}
            </div>
          )}

          <form onSubmit={handleLogin} className="space-y-4">
            <div>
              <label className="block text-sm text-text-muted mb-1">Email</label>
              <input
                type="email" required value={email} onChange={e => setEmail(e.target.value)}
                className="input-field text-sm sm:text-base"
                placeholder="you@example.com"
              />
            </div>
            <div>
              <label className="block text-sm text-text-muted mb-1">Password</label>
              <input
                type="password" required value={password} onChange={e => setPassword(e.target.value)}
                className="input-field text-sm sm:text-base"
                placeholder="Your password"
              />
            </div>
            <button
              type="submit" disabled={loading}
              className="w-full btn-primary py-3 disabled:opacity-50"
            >
              {loading ? 'Signing in...' : 'Login'}
            </button>
          </form>

          <p className="text-center text-sm text-text-muted mt-6">
            Don&apos;t have an account? <Link href="/signup" className="text-accent hover:underline">Sign Up</Link>
          </p>
        </div>
      </div>
    </div>
  )
}
