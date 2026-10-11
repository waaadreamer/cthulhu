import { FormEvent, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { api } from '../api'
import { Sigil } from './Sigil'

export function Login({ onDone }: { onDone: (user: string) => void }) {
  const [username, setUsername] = useState('owner')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const r = await api<{ user: string }>('login', { username, password })
      onDone(r.user)
    } catch (err) {
      setError((err as Error).message || '登录失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-full place-items-center px-4">
      <form onSubmit={submit} className="glass w-full max-w-sm rounded-3xl p-8 shadow-2xl shadow-violet-950/50 animate-rise">
        <div className="mb-8 text-center">
          <div className="mb-4 flex justify-center"><Sigil className="h-16 w-16 !rounded-2xl" /></div>
          <h1 className="font-display text-2xl tracking-wider text-white">CTHULHU</h1>
          <p className="mt-1 text-sm text-slate-400">中心神殿</p>
        </div>
        <label className="mb-1.5 block text-xs font-medium text-slate-400">用户名</label>
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          className="mb-4 w-full rounded-xl border border-white/10 bg-black/30 px-4 py-2.5 text-white outline-none transition focus:border-violet-400/60 focus:ring-4 focus:ring-violet-500/10"
        />
        <label className="mb-1.5 block text-xs font-medium text-slate-400">密码</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          autoFocus
          className="mb-2 w-full rounded-xl border border-white/10 bg-black/30 px-4 py-2.5 text-white outline-none transition focus:border-violet-400/60 focus:ring-4 focus:ring-violet-500/10"
        />
        <p className="h-5 text-xs text-rose-400">{error}</p>
        <button
          disabled={busy || !password}
          className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-violet-500 to-fuchsia-500 py-2.5 font-medium text-white shadow-lg shadow-violet-900/40 transition hover:brightness-110 disabled:opacity-50"
        >
          {busy && <Loader2 className="h-4 w-4 animate-spin" />}
          进入神殿
        </button>
      </form>
    </div>
  )
}
