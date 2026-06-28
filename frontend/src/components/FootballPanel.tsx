import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchFootballFixtures, fetchFootballLiveMatches, fetchFootballSessions, startFootballSession, stopFootballSession } from '../api'

function formatKickoff(iso: string): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function sessionStatusStyle(status: string): string {
  switch (status) {
    case 'running': return 'bg-green-500/10 text-green-500 border-green-500/20'
    case 'finished': return 'bg-neutral-800 text-neutral-500 border-neutral-700'
    case 'stopped': return 'bg-neutral-800 text-neutral-500 border-neutral-700'
    case 'error': return 'bg-red-500/10 text-red-500 border-red-500/20'
    default: return 'bg-amber-500/10 text-amber-400 border-amber-500/20'
  }
}

export function FootballPanel() {
  const queryClient = useQueryClient()
  const [link, setLink] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  const { data: liveMatches = [] } = useQuery({
    queryKey: ['football-live'],
    queryFn: fetchFootballLiveMatches,
    refetchInterval: 5000,
  })

  const { data: fixtures = [] } = useQuery({
    queryKey: ['football-fixtures'],
    queryFn: fetchFootballFixtures,
    refetchInterval: 60000,
  })

  const { data: sessions = [] } = useQuery({
    queryKey: ['football-sessions'],
    queryFn: fetchFootballSessions,
    refetchInterval: 5000,
  })

  const startMutation = useMutation({
    mutationFn: startFootballSession,
    onSuccess: () => {
      setLink('')
      setFormError(null)
      queryClient.invalidateQueries({ queryKey: ['football-sessions'] })
    },
    onError: (err: any) => {
      setFormError(err?.response?.data?.detail || 'Could not resolve a market from that link')
    },
  })

  const stopMutation = useMutation({
    mutationFn: stopFootballSession,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['football-sessions'] }),
  })

  const upcoming = fixtures.filter(f => f.status === 'SCHEDULED' || f.status === 'TIMED').slice(0, 6)
  const activeSessions = sessions.filter(s => s.status === 'running' || s.status === 'starting')

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* Paste-link form */}
      <form
        className="px-2 py-1.5 border-b border-neutral-800 flex items-center gap-1.5 shrink-0"
        onSubmit={(e) => {
          e.preventDefault()
          if (link.trim()) startMutation.mutate(link.trim())
        }}
      >
        <input
          type="text"
          value={link}
          onChange={(e) => setLink(e.target.value)}
          placeholder="Paste Polymarket match link..."
          className="flex-1 min-w-0 bg-neutral-900 border border-neutral-700 px-2 py-1 text-[11px] text-neutral-200 placeholder:text-neutral-600 focus:outline-none focus:border-amber-500/50"
        />
        <button
          type="submit"
          disabled={startMutation.isPending || !link.trim()}
          className="px-2.5 py-1 bg-amber-500/10 border border-amber-500/30 text-amber-400 text-[10px] uppercase tracking-wider hover:bg-amber-500/20 disabled:opacity-40 whitespace-nowrap"
        >
          {startMutation.isPending ? 'Starting...' : 'Start'}
        </button>
      </form>
      {formError && (
        <div className="px-2 py-1 text-[10px] text-red-400 border-b border-neutral-800 shrink-0">{formError}</div>
      )}

      {/* Active sessions */}
      {activeSessions.length > 0 && (
        <div className="px-1.5 py-1 border-b border-neutral-800 flex flex-wrap gap-1 shrink-0">
          {activeSessions.map(s => (
            <div
              key={s.id}
              className={`flex items-center gap-1.5 px-2 py-0.5 border text-[10px] ${sessionStatusStyle(s.status)}`}
            >
              <span>{s.home_team || '?'} vs {s.away_team || '?'}</span>
              <button
                onClick={() => stopMutation.mutate(s.id)}
                disabled={stopMutation.isPending}
                className="text-neutral-500 hover:text-neutral-200 disabled:opacity-40"
                title="Stop session"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Live matches */}
      <div className="px-2 py-1 border-b border-neutral-800 flex items-center justify-between shrink-0">
        <span className="text-[10px] text-neutral-500 uppercase tracking-wider">World Cup — Live</span>
        <span className="text-[10px] text-amber-400 tabular-nums">{liveMatches.length} live</span>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-1.5 space-y-1">
        {liveMatches.length === 0 ? (
          <div className="text-[10px] text-neutral-600 p-2">No live matches</div>
        ) : (
          liveMatches.map(m => (
            <div
              key={m.fixture_id}
              className="flex items-center justify-between px-2 py-1.5 border border-neutral-800 bg-neutral-900/50"
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-[9px] font-bold text-amber-400 uppercase shrink-0">{m.minute}'</span>
                <span className="text-[11px] text-neutral-300 truncate">
                  {m.home_team} <span className="text-neutral-500">vs</span> {m.away_team}
                </span>
              </div>
              <span className="text-[11px] tabular-nums text-neutral-100 shrink-0 ml-2">
                {m.home_score}-{m.away_score}
              </span>
            </div>
          ))
        )}
      </div>

      <div className="px-2 py-1 border-t border-b border-neutral-800 flex items-center justify-between shrink-0">
        <span className="text-[10px] text-neutral-500 uppercase tracking-wider">Upcoming</span>
        <span className="text-[10px] text-neutral-600 tabular-nums">{upcoming.length}</span>
      </div>
      <div className="shrink-0 overflow-y-auto p-1.5 space-y-1" style={{ maxHeight: '22%' }}>
        {upcoming.length === 0 ? (
          <div className="text-[10px] text-neutral-600 p-2">No upcoming fixtures</div>
        ) : (
          upcoming.map(f => (
            <div
              key={f.source_id}
              className="flex items-center justify-between px-2 py-1 border border-neutral-800/60"
            >
              <span className="text-[10px] text-neutral-400 truncate">
                {f.home_team} <span className="text-neutral-600">vs</span> {f.away_team}
              </span>
              <span className="text-[9px] text-neutral-600 tabular-nums shrink-0 ml-2">
                {formatKickoff(f.utc_kickoff)}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
