/** Slow-drifting nebula behind everything. */
export function Backdrop() {
  return (
    <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
      <div className="absolute -top-1/3 -left-1/4 h-[70vmax] w-[70vmax] rounded-full bg-violet-700/20 blur-[120px] animate-drift" />
      <div className="absolute top-1/4 -right-1/4 h-[60vmax] w-[60vmax] rounded-full bg-cyan-600/10 blur-[120px] animate-drift [animation-delay:-8s]" />
      <div className="absolute -bottom-1/3 left-1/4 h-[55vmax] w-[55vmax] rounded-full bg-fuchsia-700/10 blur-[120px] animate-drift [animation-delay:-15s]" />
      <div
        className="absolute inset-0 opacity-[0.35]"
        style={{
          backgroundImage:
            'radial-gradient(1px 1px at 20% 30%, rgba(255,255,255,.5), transparent), radial-gradient(1px 1px at 70% 80%, rgba(255,255,255,.35), transparent), radial-gradient(1px 1px at 40% 70%, rgba(255,255,255,.3), transparent), radial-gradient(1.5px 1.5px at 85% 20%, rgba(196,181,253,.6), transparent), radial-gradient(1px 1px at 10% 85%, rgba(255,255,255,.3), transparent)',
          backgroundSize: '420px 420px',
        }}
      />
    </div>
  )
}
