'use client'

import { useEffect, useState } from 'react'
import { API, formatRelative } from '@/lib/utils'

type Task = {
  id: string
  title: string
  status: 'open' | 'in_progress' | 'blocked' | 'done'
  notes: string
  created_at: string
  updated_at: string
}

const STATUS_COLOR: Record<Task['status'], string> = {
  in_progress: 'var(--yellow)',
  blocked: 'var(--red)',
  open: 'var(--text-dim)',
  done: 'var(--green)',
}

const STATUS_ORDER: Record<Task['status'], number> = {
  in_progress: 0,
  blocked: 1,
  open: 2,
  done: 3,
}

export function TasksPanel() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)

  async function load() {
    try {
      const res = await fetch(`${API}/tasks`)
      setTasks(await res.json())
    } catch {}
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  const sorted = [...tasks].sort(
    (a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || a.created_at.localeCompare(b.created_at),
  )

  return (
    <div className="flex flex-col gap-1">
      {sorted.length === 0 ? (
        <p className="text-[var(--text-dim)] text-xs py-2">No tasks in the ledger.</p>
      ) : (
        sorted.map((task) => (
          <div key={task.id} className="border border-[var(--border-subtle)] rounded-md overflow-hidden">
            <button
              onClick={() => setExpanded(expanded === task.id ? null : task.id)}
              className="w-full flex items-center gap-2 px-3 py-2 hover:bg-[var(--surface-2)] transition-colors text-left"
            >
              <span
                className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
                style={{ backgroundColor: STATUS_COLOR[task.status] }}
              />
              <span className="text-[12px] text-[var(--text)] font-medium truncate flex-1">{task.title}</span>
              <span className="text-[9px] text-[var(--text-dim)] shrink-0">{formatRelative(task.updated_at)}</span>
            </button>
            {expanded === task.id && (
              <div className="px-3 pb-3 pt-1 bg-[var(--surface-2)]">
                <p className="text-[9px] uppercase tracking-widest mb-1" style={{ color: STATUS_COLOR[task.status] }}>
                  {task.status.replace('_', ' ')}
                </p>
                <p className="text-[11px] text-[var(--text-muted)] whitespace-pre-wrap break-words leading-relaxed">
                  {task.notes || '(no notes)'}
                </p>
              </div>
            )}
          </div>
        ))
      )}
    </div>
  )
}
