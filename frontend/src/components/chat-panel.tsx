'use client'

import { useEffect, useRef, useState } from 'react'
import { cn, formatTime } from '@/lib/utils'
import type { AgentEvent } from '@/hooks/use-websocket'

type Message = {
  role: 'user' | 'assistant'
  content: string
  ts: number
}

function extractMessages(events: AgentEvent[]): Message[] {
  const msgs: Message[] = []
  for (const e of events) {
    if (e.type === 'start' && e.trigger === 'user' && e.message) {
      msgs.push({ role: 'user', content: e.message, ts: e.ts })
    } else if (e.type === 'response' && e.content) {
      msgs.push({ role: 'assistant', content: e.content, ts: e.ts })
    }
  }
  return msgs
}

export function ChatPanel({
  events,
  isThinking,
  onSend,
}: {
  events: AgentEvent[]
  isThinking: boolean
  onSend: (msg: string) => void
}) {
  const [input, setInput] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const messages = extractMessages(events)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, isThinking])

  function submit() {
    const msg = input.trim()
    if (!msg) return
    onSend(msg)
    setInput('')
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="px-4 py-2 border-b border-[var(--border)] shrink-0">
        <span className="text-[10px] uppercase tracking-widest text-[var(--text-dim)]">Chat</span>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3 flex flex-col gap-4">
        {messages.length === 0 && !isThinking && (
          <div className="text-[var(--text-dim)] text-xs mt-6 text-center">
            Say something.
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} className={cn('flex flex-col gap-1', msg.role === 'user' && 'items-end')}>
            <div className={cn(
              'max-w-[85%] rounded-lg px-3 py-2 text-[13px] leading-relaxed whitespace-pre-wrap',
              msg.role === 'user'
                ? 'bg-[var(--accent)] text-white'
                : 'bg-[var(--surface-2)] text-[var(--text)] border border-[var(--border)]'
            )}>
              {msg.content}
            </div>
            <span className="text-[9px] text-[var(--text-dim)] px-1">{formatTime(msg.ts)}</span>
          </div>
        ))}

        {isThinking && (
          <div className="flex items-center gap-2 text-[var(--yellow)] text-xs">
            <span className="animate-pulse">●</span>
            <span>thinking…</span>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      <div className="px-4 py-3 border-t border-[var(--border)] shrink-0">
        <div className="flex gap-2 items-end">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Talk to Akshay…"
            rows={1}
            className={cn(
              'flex-1 resize-none bg-[var(--surface-2)] border border-[var(--border)] rounded-lg',
              'px-3 py-2 text-[13px] text-[var(--text)] placeholder-[var(--text-dim)]',
              'focus:outline-none focus:border-[var(--accent)] transition-colors',
              'max-h-[120px] overflow-y-auto'
            )}
            style={{ height: 'auto', minHeight: '38px' }}
            onInput={(e) => {
              const t = e.currentTarget
              t.style.height = 'auto'
              t.style.height = `${Math.min(t.scrollHeight, 120)}px`
            }}
          />
          <button
            onClick={submit}
            disabled={!input.trim() || isThinking}
            className={cn(
              'px-3 py-2 rounded-lg text-[12px] font-medium transition-colors shrink-0',
              'bg-[var(--accent)] text-white',
              'disabled:opacity-30 disabled:cursor-not-allowed',
              'hover:bg-[var(--accent-dim)] active:scale-95'
            )}
          >
            Send
          </button>
        </div>
        <p className="text-[9px] text-[var(--text-dim)] mt-1">Enter to send · Shift+Enter for newline</p>
      </div>
    </div>
  )
}
