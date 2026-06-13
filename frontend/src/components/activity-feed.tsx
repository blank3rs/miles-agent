'use client'

import { useEffect, useRef } from 'react'
import { cn, formatTime } from '@/lib/utils'
import type { AgentEvent } from '@/hooks/use-websocket'

const EVENT_STYLES: Record<string, { label: string; color: string }> = {
  start:           { label: 'wake',   color: 'text-[var(--text-muted)]' },
  thinking:        { label: '...',    color: 'text-[var(--yellow)]' },
  tool_call:       { label: 'call',   color: 'text-[var(--blue)]' },
  tool_result:     { label: 'done',   color: 'text-[var(--text-muted)]' },
  response:        { label: 'said',   color: 'text-[var(--green)]' },
  error:           { label: 'err',    color: 'text-[var(--red)]' },
  heartbeat_fired: { label: 'hb',     color: 'text-[var(--accent)]' },
  status:          { label: 'stat',   color: 'text-[var(--text-muted)]' },
}

function EventRow({ event }: { event: AgentEvent }) {
  const style = EVENT_STYLES[event.type] ?? { label: event.type, color: 'text-[var(--text-muted)]' }

  let detail = ''
  if (event.type === 'start') detail = `[${event.trigger}] ${event.message ?? ''}`
  else if (event.type === 'tool_call') detail = `${event.tool}(${JSON.stringify(event.params ?? {}).slice(0, 80)})`
  else if (event.type === 'tool_result') detail = `${event.tool} → ${(event.result ?? '').slice(0, 120)}`
  else if (event.type === 'response') detail = (event.content ?? '').slice(0, 160)
  else if (event.type === 'error') detail = event.message ?? ''
  else if (event.type === 'heartbeat_fired') detail = `[${event.id}] ${event.reason}`
  else if (event.type === 'thinking') detail = 'processing…'
  else if (event.type === 'status') detail = event.message ?? ''

  return (
    <div className={cn('flex gap-3 py-1 px-3 border-b border-[var(--border-subtle)] hover:bg-[var(--surface-2)] transition-colors', event.historical && 'opacity-40')}>
      <span className="mono text-[10px] text-[var(--text-dim)] shrink-0 w-[60px] pt-[1px]">
        {event.ts ? formatTime(event.ts) : ''}
      </span>
      <span className={cn('mono text-[10px] w-[32px] shrink-0 uppercase pt-[1px]', style.color)}>
        {style.label}
      </span>
      <span className="text-[var(--text-muted)] text-[11px] break-all leading-relaxed">
        {detail}
      </span>
    </div>
  )
}

export function ActivityFeed({ events }: { events: AgentEvent[] }) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events.length])

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="px-3 py-2 border-b border-[var(--border)] shrink-0">
        <span className="text-[10px] uppercase tracking-widest text-[var(--text-dim)]">Activity</span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {events.length === 0 ? (
          <div className="px-3 py-4 text-[var(--text-dim)] text-xs">No activity yet.</div>
        ) : (
          events.map((e, i) => <EventRow key={i} event={e} />)
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
