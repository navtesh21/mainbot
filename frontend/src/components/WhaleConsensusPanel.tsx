import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchWhaleScan, runWhaleScan } from '../api'

function timeAgo(epochSeconds: number): string {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds))
  if (seconds < 60) return `${seconds}s ago`
  return `${Math.floor(seconds / 60)}m ago`
}

export function WhaleConsensusPanel() {
  const queryClient = useQueryClient()
  const [expanded, setExpanded] = useState<string | null>(null)

  const { data } = useQuery({
    queryKey: ['whale-scan'],
    queryFn: fetchWhaleScan,
  })

  const scanMutation = useMutation({
    mutationFn: runWhaleScan,
    onSuccess: (result) => queryClient.setQueryData(['whale-scan'], result),
  })

  const trades = data?.trades ?? []

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="px-2 py-1 flex items-center justify-between shrink-0 border-b border-neutral-800">
        <span className="text-[10px] text-neutral-500">
          {data?.scanned_at ? `Top ${data.trader_count} traders · scanned ${timeAgo(data.scanned_at)}` : 'No scan yet'}
        </span>
        <button
          onClick={() => scanMutation.mutate()}
          disabled={scanMutation.isPending}
          className="text-[10px] px-2 py-0.5 border border-amber-700/50 text-amber-400 hover:bg-amber-500/10 disabled:opacity-50 disabled:cursor-not-allowed uppercase tracking-wider"
        >
          {scanMutation.isPending ? 'Scanning...' : 'Scan'}
        </button>
      </div>

      <div className="flex-1 overflow-y-auto min-h-0">
        {scanMutation.isError && (
          <div className="text-[10px] text-red-400 p-2">Scan failed — Polymarket data API may be unavailable.</div>
        )}
        {trades.length === 0 ? (
          <div className="text-[10px] text-neutral-600 p-2 text-center">
            Click Scan to check the top 20 sports traders' open positions for consensus bets
          </div>
        ) : (
          <div className="divide-y divide-neutral-900">
            {trades.map((t) => (
              <div
                key={`${t.condition_id}-${t.outcome}`}
                className="px-2 py-1.5 cursor-pointer hover:bg-neutral-900/50"
                onClick={() => setExpanded(expanded === t.condition_id + t.outcome ? null : t.condition_id + t.outcome)}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] text-neutral-300 truncate">{t.market_title}</span>
                  <span className="text-[10px] font-bold text-green-400 shrink-0 tabular-nums">{t.outcome}</span>
                </div>
                <div className="flex items-center gap-3 mt-0.5 text-[10px] text-neutral-500 tabular-nums">
                  <span className="text-amber-400">{t.trader_count}/20 traders</span>
                  <span>${t.total_value_usd.toLocaleString()}</span>
                  <span>avg {(t.avg_price * 100).toFixed(1)}c</span>
                </div>
                {expanded === t.condition_id + t.outcome && (
                  <div className="mt-1 space-y-0.5 border-t border-neutral-900 pt-1">
                    {t.traders.map((trader, i) => (
                      <div key={i} className="flex items-center justify-between text-[10px] tabular-nums">
                        <a
                          href={`https://polymarket.com/profile/${trader.wallet}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          className="text-neutral-400 truncate hover:text-amber-400 hover:underline"
                        >
                          {trader.name}
                        </a>
                        <span className="text-neutral-500">${trader.value_usd.toLocaleString()}</span>
                        <span className={trader.pnl_usd >= 0 ? 'text-green-400' : 'text-red-400'}>
                          {trader.pnl_usd >= 0 ? '+' : ''}${trader.pnl_usd.toLocaleString()} ({trader.pnl_pct >= 0 ? '+' : ''}{trader.pnl_pct.toFixed(1)}%)
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
