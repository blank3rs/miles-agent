'use client'

import { useEffect, useState } from 'react'
import { API } from '@/lib/utils'

type Skill = { name: string; description: string; parameters: Record<string, unknown> }

export function SkillsPanel() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)

  async function load() {
    try {
      const res = await fetch(`${API}/skills`)
      setSkills(await res.json())
    } catch {}
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 8000)
    return () => clearInterval(t)
  }, [])

  return (
    <div className="flex flex-col gap-1">
      {skills.length === 0 ? (
        <p className="text-[var(--text-dim)] text-xs py-2">No skills yet. Akshay can create them.</p>
      ) : (
        skills.map((skill) => (
          <div key={skill.name} className="border border-[var(--border-subtle)] rounded-md overflow-hidden">
            <button
              onClick={() => setExpanded(expanded === skill.name ? null : skill.name)}
              className="w-full flex items-center justify-between px-3 py-2 hover:bg-[var(--surface-2)] transition-colors text-left"
            >
              <span className="text-[12px] text-[var(--accent)] font-mono">{skill.name}</span>
              <span className="text-[9px] text-[var(--text-dim)] shrink-0 ml-2 truncate max-w-[120px]">{skill.description}</span>
            </button>
            {expanded === skill.name && (
              <div className="px-3 pb-3 pt-1 bg-[var(--surface-2)]">
                <p className="text-[11px] text-[var(--text-muted)] mb-2">{skill.description}</p>
                {Object.keys(skill.parameters).length > 0 && (
                  <pre className="text-[10px] text-[var(--text-dim)] font-mono bg-[var(--bg)] p-2 rounded overflow-x-auto">
                    {JSON.stringify(skill.parameters, null, 2)}
                  </pre>
                )}
              </div>
            )}
          </div>
        ))
      )}
    </div>
  )
}
