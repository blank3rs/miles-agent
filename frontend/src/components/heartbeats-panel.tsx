'use client'

import { useEffect, useState } from 'react'
import { API } from '@/lib/utils'

type Heartbeat = {
  id: string
  reason: string
  context: string
  fire_at: number
  fire_at_iso: string
  created_at: string
}

function useCountdown(fireAt: number) {
  const [remaining, setRemaining] = useState(Math.max(0, fireAt - Date.now() / 1000))
  useEffect(() => {
    const t = setInterval(() => setRemaining(Math.max(0, fireAt - Date.now() / 1000)), 1000)
    return () => clearInterval(t)
  }, [fireAt])
  const h = Math.floor(remaining / 3600)
  const m = Math.floor((remaining % 3600) / 60)
  const s = Math.floor(remaining % 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

function HeartbeatRow({ hb, onCancel }: { hb: Heartbeat; onCancel: (id: string) => void }) {
  const countdown = useCountdown(hb.fire_at)
  return (
    <div className="border border-[var(--border-subtle)] rounded-md p-3 flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-[var(--accent)] mono text-[10px]">{hb.id}</span>
        <span className="text-[var(--yellow)] mono text-[11px] font-medium">{countdown}</span>
      </div>
      <p className="text-[12px] text-[var(--text)]">{hb.reason}</p>
      <p className="text-[10px] text-[var(--text-dim)] truncate">{hb.context}</p>
      <button
        onClick={() => onCancel(hb.id)}
        className="mt-1 text-[10px] text-[var(--red)] hover:underline text-left w-fit"
      >
        cancel
      </button>
    </div>
  )
}

export function HeartbeatsPanel() {
  const [heartbeats, setHeartbeats] = useState<Heartbeat[]>([])

  async function load() {
    try {
      const res = await fetch(`${API}/heartbeats`)
      setHeartbeats(await res.json())
    } catch {}
  }

  async function cancel(id: string) {
    try {
      await fetch(`${API}/heartbeats/${id}`, { method: 'DELETE' })
      load()
    } catch {}
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [])

  return (
    <div className="flex flex-col gap-2">
      {heartbeats.length === 0 ? (
        <p className="text-[var(--text-dim)] text-xs py-2">No scheduled heartbeats.</p>
      ) : (
        heartbeats.map((hb) => (
          <HeartbeatRow key={hb.id} hb={hb} onCancel={cancel} />
        ))
      )}
    </div>
  )
}
