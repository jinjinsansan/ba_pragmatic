import Link from 'next/link'
import Image from 'next/image'

export default function Home() {
  return (
    <main className="min-h-screen bg-bg-primary text-text relative">

      {/* Navbar */}
      <nav className="fixed top-0 inset-x-0 z-50 glass-panel border-b border-accent/20">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
          <span className="text-xs font-hud tracking-[0.4em] text-accent uppercase">BAFATHER</span>
          <div className="hidden md:flex items-center gap-10 text-xs tracking-widest text-text-muted uppercase">
            <a href="#features" className="hover:text-text transition-colors">System</a>
            <a href="#operations" className="hover:text-text transition-colors">Operations</a>
            <a href="#pricing" className="hover:text-text transition-colors">Access</a>
            <a href="#faq" className="hover:text-text transition-colors">FAQ</a>
            <Link href="/login" className="hover:text-text transition-colors">Login</Link>
            <Link href="/signup" className="btn-outline px-5 py-2">
              GET ACCESS
            </Link>
          </div>
          <div className="flex md:hidden items-center gap-4">
            <Link href="/login" className="text-xs text-text-muted hover:text-text tracking-widest uppercase">Login</Link>
            <Link href="/signup" className="btn-outline px-3 py-1.5 text-xs">Access</Link>
          </div>
        </div>
      </nav>

      {/* Hero */}
      <section className="relative min-h-screen flex items-center px-4 sm:px-6 pt-16 overflow-hidden">
        <div className="max-w-6xl mx-auto w-full grid lg:grid-cols-2 gap-10 lg:gap-16 items-center py-16 sm:py-20">
          {/* Left — text */}
          <div className="text-center lg:text-left">
            <p className="hud-label mb-8">I · Premium Baccarat Operations</p>
            <h1 className="text-4xl sm:text-5xl md:text-7xl lg:text-8xl leading-[0.92] tracking-tight mb-8 font-hud">
              <span className="block text-text">ONYX NOIR</span>
              <span className="block text-accent">TRADING DESK</span>
              <span className="block text-text-muted text-2xl sm:text-3xl md:text-4xl mt-3">for Baccarat Execution</span>
            </h1>
            <p className="text-text-muted text-sm sm:text-base leading-relaxed max-w-md mx-auto lg:mx-0 mb-10 sm:mb-12">
              Signal generation, execution control, and daily settlement are unified in one premium console for operators, members, and admin teams.
            </p>
            <div className="flex flex-col sm:flex-row items-center gap-4 sm:gap-6 justify-center lg:justify-start">
              <Link href="/signup" className="btn-primary px-8 py-3.5 w-full sm:w-auto">
                GET ACCESS
              </Link>
              <a href="#features" className="text-xs tracking-widest text-text-muted uppercase hover:text-text transition-colors border-b border-transparent hover:border-accent/40 pb-0.5">
                How it works
              </a>
            </div>

            {/* Stats inline */}
            <div className="mt-12 sm:mt-16 grid grid-cols-1 sm:grid-cols-3 gap-6 border-t border-white/5 pt-8 sm:pt-10">
                {[
                { num: '24/7', label: 'Ops Desk', color: 'text-player' },
                { num: 'T+1', label: 'Daily Settle', color: 'text-accent' },
                { num: 'Auto', label: 'Lock Control', color: 'text-banker' },
              ].map((s, i) => (
                <div key={i} className="text-center sm:text-left">
                  <div className={`text-2xl font-hud tabular-nums ${s.color}`}>{s.num}</div>
                    <div className="text-[10px] tracking-widest text-text-dim uppercase mt-1">{s.label}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Right — image */}
          <div className="relative flex justify-center lg:justify-end">
            <div className="relative w-64 sm:w-72 lg:w-96">
              <Image
                src="/foodblack.jpg"
                alt="bafather"
                width={480}
                height={600}
                className="w-full object-cover"
                style={{filter:'contrast(1.05) brightness(0.95)'}}
                priority
              />
              <div className="absolute inset-x-0 bottom-0 h-32 bg-gradient-to-t from-bg-primary to-transparent" />
              <div className="absolute inset-y-0 right-0 w-16 bg-gradient-to-l from-bg-primary to-transparent hidden md:block" />
            </div>
          </div>
        </div>
      </section>

      {/* Features */}
      <section id="features" className="py-20 sm:py-28 lg:py-32 px-4 sm:px-6 border-t border-accent/10">
        <div className="max-w-6xl mx-auto">
          <div className="grid lg:grid-cols-2 gap-4 mb-4">
            <p className="hud-label">II · System Architecture</p>
          </div>
          <h2 className="text-3xl sm:text-4xl lg:text-5xl text-text mb-12 sm:mb-20 leading-tight font-hud">
            Built for<br className="hidden sm:block" /><span className="text-text-dim">precision and longevity.</span>
          </h2>
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {[
              { num: 'I', title: 'Signal Core', desc: 'Server-authoritative prediction and session-aware fallback logic for stable execution.', c: 'text-player' },
              { num: 'II', title: 'Settlement Ledger', desc: 'Daily profit-share billing, outstanding tracking, and invoice status persistence.', c: 'text-banker' },
              { num: 'III', title: 'Telegram Ops', desc: 'Admin + customer notifications for settlement, payment reflection, and session broadcasts.', c: 'text-accent' },
              { num: 'IV', title: 'License Guard', desc: 'Unpaid lock enforcement at API and receiver levels to prevent unbilled live operation.', c: 'text-player' },
              { num: 'V', title: 'Operator Console', desc: 'Admin workflows for users, promos, tickets, withdrawals, and charge confirmations.', c: 'text-banker' },
              { num: 'VI', title: 'Member Console', desc: 'Dashboard, referrals, support, and charge history with clear operational state.', c: 'text-accent' },
            ].map((f, i) => (
              <div key={i} className="glass-card p-6 sm:p-8 group">
                <div className={`text-[11px] tracking-[0.22em] mb-6 font-mono ${f.c}`}>{f.num}</div>
                <h3 className="text-sm font-bold text-text mb-3 tracking-wide">{f.title}</h3>
                <p className="text-xs text-text-muted leading-relaxed">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="operations" className="py-20 sm:py-24 px-4 sm:px-6 border-t border-accent/10">
        <div className="max-w-6xl mx-auto">
          <p className="hud-label mb-4">III · Operations Cycle</p>
          <h2 className="text-3xl sm:text-4xl lg:text-5xl text-text mb-12 font-hud">Designed for daily operation rhythm.</h2>
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {[
              { step: 'I', title: 'Run Session', desc: 'Master and receiver operate in synchronized runtime.' },
              { step: 'II', title: 'Settle PnL', desc: 'Cron computes daily fee and records paid/unpaid state.' },
              { step: 'III', title: 'Notify', desc: 'Telegram sends operator and customer-facing updates.' },
              { step: 'IV', title: 'Recharge', desc: 'Charge confirmation auto-consumes outstanding invoices.' },
            ].map((s) => (
              <div key={s.step} className="glass-card p-6">
                <div className="text-[11px] tracking-[0.22em] mb-4 text-accent font-mono">{s.step}</div>
                <h3 className="text-sm font-bold mb-2">{s.title}</h3>
                <p className="text-xs text-text-muted leading-relaxed">{s.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Pricing */}
      <section id="pricing" className="py-20 sm:py-28 lg:py-32 px-4 sm:px-6 border-t border-accent/10">
        <div className="max-w-6xl mx-auto">
          <p className="hud-label mb-4">IV · Access</p>
          <h2 className="text-3xl sm:text-4xl lg:text-5xl text-text mb-12 sm:mb-20 font-hud">One license.<br className="hidden sm:block" /><span className="text-text-dim">No subscription.</span></h2>
          <div className="max-w-lg">
            <div className="glass-card p-6 sm:p-10 relative">
              <div className="absolute top-0 left-0 w-8 h-px bg-player" />
              <div className="absolute top-0 left-0 w-px h-8 bg-player" />
              <div className="absolute bottom-0 right-0 w-8 h-px bg-banker" />
              <div className="absolute bottom-0 right-0 w-px h-8 bg-banker" />

              <p className="text-[10px] tracking-widest text-text-dim uppercase mb-4">LAPLACE License</p>
              <div className="text-5xl font-black text-text mb-1">$2,000</div>
              <p className="text-xs text-text-dim mb-10">USDT · One-time · Deducted from first charge</p>

              <div className="space-y-4 mb-10">
                {[
                  'Full prediction engine access',
                  'Automated bet execution',
                  'Live dashboard',
                  'Cloud logic processing',
                  'Lifetime updates',
                ].map((item, i) => (
                  <div key={i} className="flex items-center gap-3 text-xs text-text-muted">
                    <div className="w-3 h-px bg-player flex-shrink-0" />
                    {item}
                  </div>
                ))}
              </div>

              <Link href="/signup" className="btn-primary block text-center py-4">
                GET ACCESS
              </Link>
            </div>
            <p className="text-xs text-text-dim mt-6 leading-relaxed">
              Daily profit share applied at end of each session day. Losses carry forward and offset future fees before any deduction is made.
            </p>
          </div>
        </div>
      </section>

      {/* How It Works */}
      <section className="py-20 sm:py-28 lg:py-32 px-4 sm:px-6 border-t border-accent/10">
        <div className="max-w-6xl mx-auto">
          <p className="hud-label mb-4">V · Deployment</p>
          <h2 className="text-3xl sm:text-4xl lg:text-5xl text-text mb-12 sm:mb-20 font-hud">Four steps.<br className="hidden sm:block" /><span className="text-text-dim">Then operate.</span></h2>
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {[
              { step: 'I', title: 'Purchase', desc: 'Submit license request and wait for confirmation.', c: 'text-player' },
              { step: 'II', title: 'Download', desc: 'Receive your bound receiver package.', c: 'text-banker' },
              { step: 'III', title: 'Charge', desc: 'Top up balance and verify reflected status.', c: 'text-accent' },
              { step: 'IV', title: 'Run', desc: 'Start session and monitor dashboard metrics.', c: 'text-player' },
            ].map((s, i) => (
              <div key={i} className="glass-card p-6 sm:p-8">
                <div className={`text-[11px] tracking-[0.22em] font-mono mb-6 ${s.c}`}>{s.step}</div>
                <h4 className="text-sm font-bold text-text mb-3">{s.title}</h4>
                <p className="text-xs text-text-muted leading-relaxed">{s.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* FAQ */}
      <section id="faq" className="py-20 sm:py-28 lg:py-32 px-4 sm:px-6 border-t border-accent/10">
        <div className="max-w-6xl mx-auto grid lg:grid-cols-2 gap-12 lg:gap-20">
          <div>
            <p className="hud-label mb-4">VI · FAQ</p>
            <h2 className="text-3xl sm:text-4xl lg:text-5xl text-text leading-tight font-hud">Questions<br className="hidden sm:block" /><span className="text-text-dim">answered.</span></h2>
          </div>
          <div className="space-y-0">
            {[
              { q: 'How does the profit share work?', a: 'At end of each session day, net profit is calculated. A percentage is deducted from your balance. Losing days carry forward — no fees until prior losses are recovered.' },
              { q: 'What happens when the balance hits zero?', a: 'A 24-hour grace period activates. Bot pauses. Recharge at any time to resume. No penalties, no account loss.' },
              { q: 'How is the license fee charged?', a: 'Deducted automatically from your first charge. Pay $2,000 license + $3,000 charge = $3,000 operational balance.' },
              { q: 'What payment methods are accepted?', a: 'USDT only. TRC-20 (TRON) or ERC-20 (Ethereum). Manual confirmation, typically under 30 minutes.' },
              { q: 'Can this run on a cloud machine?', a: 'Yes. Any Windows 10/11 environment — local or cloud. AWS WorkSpaces, Paperspace, Shadow PC all confirmed.' },
            ].map((f, i) => (
              <details key={i} className="group border-b border-accent/10 py-6">
                <summary className="cursor-pointer text-sm font-semibold text-text flex justify-between items-center gap-4 list-none">
                  {f.q}
                  <span className="text-text-dim group-open:text-player transition-colors flex-shrink-0 font-light text-lg leading-none">+</span>
                </summary>
                <p className="mt-4 text-xs text-text-muted leading-relaxed">{f.a}</p>
              </details>
            ))}
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="py-20 sm:py-28 lg:py-32 px-4 sm:px-6 border-t border-accent/10">
        <div className="max-w-6xl mx-auto flex flex-col lg:flex-row items-start lg:items-end justify-between gap-10 lg:gap-12">
          <div>
            <p className="hud-label mb-4 sm:mb-6">VII · Start Operating</p>
            <h2 className="text-4xl sm:text-5xl lg:text-7xl text-text leading-tight font-hud">
              <span className="block sm:inline">Ready</span>{' '}
              <span className="block sm:inline">when</span>{' '}
              <span className="block sm:inline text-text-dim">you are.</span>
            </h2>
          </div>
          <Link href="/signup" className="btn-primary px-10 py-4 w-full sm:w-auto flex-shrink-0">
            CREATE ACCOUNT
          </Link>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-accent/10 py-8 px-4 sm:px-6">
        <div className="max-w-6xl mx-auto flex flex-col sm:flex-row justify-between items-center text-[10px] tracking-widest text-text-dim uppercase gap-2">
          <span className="text-accent">BAFATHER</span>
          <span>&copy; 2026</span>
        </div>
      </footer>
    </main>
  )
}
