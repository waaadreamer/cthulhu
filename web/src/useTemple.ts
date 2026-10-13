import { useEffect, useRef, useState } from 'react'
import { api, Directory, Message, Turn } from './api'

export interface Stream { reply: string; activity: string[] }

type Handlers = {
  onChat?: (t: Turn) => void
  onMessage?: (m: Message) => void
  onPatrol?: () => void
  onDeploy?: () => void
  onTask?: (taskId: string) => void
}

/** Live temple state over the owner websocket, with reconnect. */
export function useTemple(handlers: Handlers) {
  const [dir, setDir] = useState<Directory>({ agents: [], galaxies: [] })
  const [connected, setConnected] = useState(false)
  const [streams, setStreams] = useState<Record<string, Stream>>({})
  const h = useRef(handlers)
  h.current = handlers

  useEffect(() => {
    let ws: WebSocket | null = null
    let stopped = false
    let delay = 500
    api<Directory>('directory').then(setDir).catch(() => {})

    const connect = () => {
      const url = new URL('api/events', location.href)
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(url)
      ws.onopen = () => { setConnected(true); delay = 500 }
      ws.onclose = () => {
        setConnected(false)
        if (!stopped) setTimeout(connect, (delay = Math.min(delay * 2, 15000)))
      }
      ws.onmessage = (e) => {
        const f = JSON.parse(e.data)
        switch (f.type) {
          case 'welcome':
          case 'directory':
            setDir((d) => ({ ...d, agents: f.agents, galaxies: f.galaxies }))
            break
          case 'status':
            setDir((d) => ({
              ...d,
              agents: d.agents.map((a) => (a.id === f.agent_id ? { ...a, ...f.status } : a)),
            }))
            break
          case 'stream':
            setStreams((s) => {
              const cur = s[f.turn_id] ?? { reply: '', activity: [] }
              return {
                ...s,
                [f.turn_id]: {
                  reply: f.delta ? cur.reply + f.delta : cur.reply,
                  activity: f.activity ?? cur.activity,
                },
              }
            })
            break
          case 'chat':
            h.current.onChat?.(f.turn)
            if (f.turn.status !== 'running' && f.turn.status !== 'queued')
              setStreams((s) => { const { [f.turn.id]: _, ...rest } = s; return rest })
            break
          case 'message':
            h.current.onMessage?.(f.message)
            break
          case 'patrol':
            h.current.onPatrol?.()
            break
          case 'deploy':
            h.current.onDeploy?.()
            break
          case 'task':
            h.current.onTask?.(f.task_id)
            break
        }
      }
    }
    connect()
    return () => { stopped = true; ws?.close() }
  }, [])

  return { dir, connected, streams }
}
