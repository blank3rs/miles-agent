'use client'

import { useState } from 'react'
import { useWebSocket } from '@/hooks/use-websocket'
import { ActivityFeed } from '@/components/activity-feed'
import { ChatPanel } from '@/components/chat-panel'
import { TasksPanel } from '@/components/tasks-panel'
import { SkillsPanel } from '@/components/skills-panel'
import { HeartbeatsPanel } from '@/components/heartbeats-panel'
import { cn } from '@/lib/utils'

type Tab = 'tasks' | 'skills' | 'heartbeats'

function StatusDot({ state }: { state: string }) {
  return (
    <span className={cn(
      'inline-block w-2 h-2 rounded-full',
      state === 'connected' ? 'bg-[var(--green)] shadow-[0_0_6px_var(--green)]' :
      state === 'connecting' ? 'bg-[var(--yellow)] animate-pulse' :
      'bg-[var(--red)]'
    )} />
  )
}

function Clock() {
  const [time, setTime] = useState(() => new Date().toLocaleTimeString('en-US', { hour12: false }))
  if (typeof window !== 'undefined') {
    setTimeout(() => setTime(new Date().toLocaleTimeString('en-US', { hour12: false })), 1000)
  }
  return <span className="mono text-[11px] text-[var(--text-dim)]">{time}</span>
}

export default function Home() {
  const { events, connState, isThinking, send } = useWebSocket()
  const [tab, setTab] = useState<Tab>('tasks')

  const tabs: { id: Tab; label: string }[] = [
    { id: 'tasks', label: 'Tasks' },
    { id: 'skills', label: 'Skills' },
    { id: 'heartbeats', label: 'Heartbeats' },
  ]

  return (
    <div className="h-screen flex flex-col overflow-hidden">
      {/* Header */}
      <header className="flex items-center justify-between px-4 py-2 border-b border-[var(--border)] bg-[var(--surface)] shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-[13px] font-semibold tracking-tight text-[var(--text)]">HESO</span>
          <span className="text-[var(--border)] text-xs">/</span>
          <span className="text-[12px] text-[var(--text-muted)]">CEO</span>
        </div>
        <div className="flex items-center gap-3">
          {isThinking && (
            <span className="text-[10px] text-[var(--yellow)] animate-pulse uppercase tracking-wider">thinking</span>
          )}
          <StatusDot state={connState} />
          <span className="text-[10px] text-[var(--text-dim)] capitalize">{connState}</span>
          <Clock />
        </div>
      </header>

      {/* Main layout */}
      <div className="flex-1 flex overflow-hidden">
        {/* Activity feed — left */}
        <aside className="w-[280px] border-r border-[var(--border)] bg-[var(--surface)] flex flex-col overflow-hidden shrink-0">
          <ActivityFeed events={events} />
        </aside>

        {/* Chat — center */}
        <main className="flex-1 flex flex-col overflow-hidden bg-[var(--bg)]">
          <ChatPanel events={events} isThinking={isThinking} onSend={send} />
        </main>

        {/* Data sidebar — right */}
        <aside className="w-[280px] border-l border-[var(--border)] bg-[var(--surface)] flex flex-col overflow-hidden shrink-0">
          {/* Tabs */}
          <div className="flex border-b border-[var(--border)] shrink-0">
            {tabs.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={cn(
                  'flex-1 py-2 text-[10px] uppercase tracking-widest transition-colors',
                  tab === t.id
                    ? 'text-[var(--text)] border-b-2 border-[var(--accent)] -mb-px'
                    : 'text-[var(--text-dim)] hover:text-[var(--text-muted)]'
                )}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="flex-1 overflow-y-auto p-3">
            {tab === 'tasks' && <TasksPanel />}
            {tab === 'skills' && <SkillsPanel />}
            {tab === 'heartbeats' && <HeartbeatsPanel />}
          </div>
        </aside>
      </div>
    </div>
  )
}
