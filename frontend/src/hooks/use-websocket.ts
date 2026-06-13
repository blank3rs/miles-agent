'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { WS } from '@/lib/utils'

export type AgentEvent = {
  type: 'start' | 'thinking' | 'tool_call' | 'tool_result' | 'response' | 'error' | 'status' | 'heartbeat_fired'
  ts: number
  historical?: boolean
  trigger?: string
  message?: string
  tool?: string
  params?: Record<string, unknown>
  result?: string
  content?: string
  id?: string
  reason?: string
  status?: string
}

export type ConnectionState = 'connecting' | 'connected' | 'disconnected'

export function useWebSocket() {
  const [events, setEvents] = useState<AgentEvent[]>([])
  const [connState, setConnState] = useState<ConnectionState>('connecting')
  const [isThinking, setIsThinking] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    const ws = new WebSocket(WS)
    wsRef.current = ws
    setConnState('connecting')

    ws.onopen = () => setConnState('connected')

    ws.onmessage = (e) => {
      try {
        const event: AgentEvent = JSON.parse(e.data)
        if (event.type === 'thinking') {
          setIsThinking(true)
        } else if (['response', 'error'].includes(event.type)) {
          setIsThinking(false)
        }
        setEvents((prev) => {
          const next = [...prev, event]
          return next.slice(-500) // keep last 500
        })
      } catch {}
    }

    ws.onclose = () => {
      setConnState('disconnected')
      setIsThinking(false)
      reconnectTimer.current = setTimeout(connect, 3000)
    }

    ws.onerror = () => ws.close()
  }, [])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      wsRef.current?.close()
    }
  }, [connect])

  const send = useCallback((message: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'chat', content: message }))
    }
  }, [])

  return { events, connState, isThinking, send }
}
