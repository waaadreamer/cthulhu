/** The temple's mark: an eye within tentacled rings, drawn inline so it never depends on emoji fonts. */
export function Sigil({ className = 'h-10 w-10' }: { className?: string }) {
  return (
    <div className={`${className} grid place-items-center rounded-xl bg-gradient-to-br from-violet-500 via-fuchsia-500 to-cyan-400 shadow-lg shadow-fuchsia-900/40`}>
      <svg viewBox="0 0 32 32" className="h-[62%] w-[62%] text-white" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
        <path d="M4 16c3-6 7-9 12-9s9 3 12 9c-3 6-7 9-12 9s-9-3-12-9Z" />
        <circle cx="16" cy="16" r="4.2" fill="currentColor" stroke="none" />
        <path d="M9 24c-1.5 2-1 4 1 4.5M16 25.5V29M23 24c1.5 2 1 4-1 4.5" />
      </svg>
    </div>
  )
}
