import { useQuery } from '@tanstack/react-query'
import { fetchMarketAnalysis, fetchOddsComparison } from '../api'

interface Props {
  sessionId: number | null
}

function timeAgo(iso: string): string {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  return `${Math.floor(seconds / 60)}m ago`
}

export function MarketAnalysisPanel({ sessionId }: Props) {
  const { data } = useQuery({
    queryKey: ['market-analysis', sessionId],
    queryFn: () => fetchMarketAnalysis(sessionId as number),
    enabled: sessionId !== null,
    refetchInterval: 20000,
  })

  const { data: odds } = useQuery({
    queryKey: ['odds-comparison', sessionId],
    queryFn: () => fetchOddsComparison(sessionId as number),
    enabled: sessionId !== null,
    refetchInterval: 30000,
  })

  if (sessionId === null) {
    return (
      <div className="h-full flex items-center justify-center text-neutral-600 text-[10px]">
        No football session — AI analysis starts once one is running
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto p-2 space-y-1.5">
      {odds?.configured && odds.sportsbook_prob !== null && (
        <div className="flex items-center justify-between text-[10px] tabular-nums border border-neutral-800 px-1.5 py-1 bg-neutral-900/50">
          <span className="text-neutral-500">Sportsbook ({odds.bookmaker_count})</span>
          <span className="text-neutral-300">{((odds.sportsbook_prob ?? 0) * 100).toFixed(1)}%</span>
          <span className="text-neutral-500">vs Poly</span>
          <span className="text-neutral-300">{((odds.polymarket_prob ?? 0) * 100).toFixed(1)}%</span>
          <span className={(odds.edge ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}>
            {(odds.edge ?? 0) >= 0 ? '+' : ''}{((odds.edge ?? 0) * 100).toFixed(1)}%
          </span>
        </div>
      )}

      {!data?.text ? (
        <div className="flex items-center justify-center text-neutral-600 text-[10px] text-center px-2 py-4">
          Waiting for first AI read (Gemini analyzes the order book every 2 min while a session is live)...
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between text-[9px] text-neutral-500 uppercase tracking-wider">
            <span>{data.model || 'gemini'}</span>
            {data.timestamp && <span className="tabular-nums">{timeAgo(data.timestamp)}</span>}
          </div>
          <p className="text-[11px] text-neutral-300 leading-relaxed whitespace-pre-wrap">
            {data.text}
          </p>
        </>
      )}
    </div>
  )
}
