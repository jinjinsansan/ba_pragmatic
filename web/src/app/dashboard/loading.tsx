export default function Loading() {
  return (
    <div className="min-h-screen">
      <nav className="glass-panel border-b border-accent/20 rounded-none">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center">
          <span className="text-sm font-hud tracking-[0.35em] text-accent">BAFATHER</span>
        </div>
      </nav>
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="h-8 w-48 bg-bg-card rounded-lg animate-pulse mb-8" />
        <div className="h-24 bg-bg-card rounded-2xl animate-pulse mb-8" />
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
          {[1,2,3,4].map(i => <div key={i} className="h-20 bg-bg-card rounded-xl animate-pulse" />)}
        </div>
      </div>
    </div>
  )
}
