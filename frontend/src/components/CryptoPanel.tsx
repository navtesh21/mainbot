import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchCryptoMarkets, fetchCryptoStatus, startCryptoEngine, stopCryptoEngine, runCryptoScan } from '../api'

function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const m = Math.floor(s / 60)
  const r = s % 60
  return `${m}:${r.toString().padStart(2, '0')}`
}

export function CryptoPanel() {
  const queryClient = useQueryClient()

  const { data: status } = useQuery({
    queryKey: ['crypto-status'],
    queryFn: fetchCryptoStatus,
    refetchInterval: 2000,
  })

  const { data: markets = [] } = useQuery({
    queryKey: ['crypto-markets'],
    queryFn: fetchCryptoMarkets,
    refetchInterval: 2000,
  })

  const startMutation = useMutation({
    mutationFn: startCryptoEngine,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['crypto-status'] }),
  })
  const stopMutation = useMutation({
    mutationFn: stopCryptoEngine,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['crypto-status'] }),
  })
  const scanMutation = useMutation({
    mutationFn: runCryptoScan,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['crypto-status'] })
      queryClient.invalidateQueries({ queryKey: ['crypto-markets'] })
      queryClient.invalidateQueries({ queryKey: ['crypto-trades'] })
    },
  })

  const openSlugs = new Set((status?.open_positions ?? []).map(p => p.slug))

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="px-2 py-1.5 flex items-center justify-between shrink-0 border-b border-neutral-800">
        <div className="flex items-center gap-2">
          <span className={`px-1.5 py-0.5 text-[9px] font-bold uppercase ${
            status?.running
              ? 'bg-green-500/10 text-green-500 border border-green-500/20'
              : 'bg-neutral-800 text-neutral-500 border border-neutral-700'
          }`}>
            {status?.running ? 'Running' : 'Stopped'}
          </span>
          <span className={`px-1.5 py-0.5 text-[9px] font-bold uppercase border ${
            status?.trading_live
              ? 'bg-red-500/10 text-red-400 border-red-500/30'
              : 'bg-amber-500/10 text-amber-400 border-amber-500/20'
          }`}>
            {status?.trading_live ? 'Live' : 'Dry-Run'}
          </span>
          {status && (
            <>
              <span className={`text-[10px] tabular-nums ${
                (status.open_positions?.length ?? 0) >= 5
                  ? 'text-amber-400'
                  : 'text-neutral-500'
              }`}>
                {status.open_positions?.length ?? 0}/5 slots
              </span>
              <span className={`text-[10px] tabular-nums ${status.today_pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                {status.today_pnl >= 0 ? '+' : ''}${status.today_pnl.toFixed(2)} today
              </span>
            </>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <button
            onClick={() => scanMutation.mutate()}
            disabled={scanMutation.isPending}
            className="text-[10px] px-2 py-0.5 border border-neutral-700 text-neutral-300 hover:border-neutral-600 disabled:opacity-50 uppercase tracking-wider"
          >
            {scanMutation.isPending ? 'Scanning...' : 'Scan'}
          </button>
          {status?.running ? (
            <button
              onClick={() => stopMutation.mutate()}
              className="text-[10px] px-2 py-0.5 border border-red-700/50 text-red-400 hover:bg-red-500/10 uppercase tracking-wider"
            >
              Stop
            </button>
          ) : (
            <button
              onClick={() => startMutation.mutate()}
              disabled={!status?.enabled}
              className="text-[10px] px-2 py-0.5 border border-green-700/50 text-green-400 hover:bg-green-500/10 disabled:opacity-40 uppercase tracking-wider"
              title={!status?.enabled ? 'CRYPTO_ENABLED is false in backend config' : undefined}
            >
              Start
            </button>
          )}
        </div>
      </div>

      {status && status.open_positions.length > 0 && (
        <div className="px-2 py-1 border-b border-neutral-800 space-y-0.5">
          <div className="text-[9px] text-neutral-500 uppercase tracking-wider mb-0.5">Open Positions</div>
          {status.open_positions.map(p => (
            <div key={p.slug} className="flex items-center justify-between text-[10px] tabular-nums">
              <span className="text-neutral-400 truncate max-w-[140px]">{p.slug}</span>
              <span className={p.direction === 'up' ? 'text-green-500' : 'text-red-500'}>{p.direction.toUpperCase()}</span>
              <span className="text-neutral-500">@{p.entry_price.toFixed(3)}</span>
              <span className="text-neutral-500">${p.size.toFixed(2)}</span>
            </div>
          ))}
        </div>
      )}

      <div className="flex-1 overflow-y-auto min-h-0">
        {markets.length === 0 ? (
          <div className="text-[10px] text-neutral-600 p-2 text-center">No active BTC windows</div>
        ) : (
          <div className="divide-y divide-neutral-900">
            {markets.map(m => (
              <a
                key={m.slug}
                href={`https://polymarket.com/event/${m.slug}`}
                target="_blank"
                rel="noopener noreferrer"
                className={`block px-2 py-1.5 hover:bg-neutral-800/60 cursor-pointer ${openSlugs.has(m.slug) ? 'bg-amber-500/5' : ''}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] text-neutral-300 truncate hover:text-white">
                    <span className="text-neutral-500 mr-1">{m.window_minutes}m</span>
                    {m.slug.replace(/^btc-updown-\d+m-/, '')}
                    <span className="ml-1 text-[8px] text-neutral-600">↗</span>
                  </span>
                  <span className="text-[10px] tabular-nums text-neutral-500 shrink-0">{formatCountdown(m.time_until_end)}</span>
                </div>
                <div className="flex items-center gap-3 mt-0.5 text-[10px] tabular-nums">
                  <span className="text-green-400">UP {(m.up_price * 100).toFixed(1)}c</span>
                  <span className="text-red-400">DOWN {(m.down_price * 100).toFixed(1)}c</span>
                  {m.signal_direction && m.signal_edge !== null && (
                    <span className={Math.abs(m.signal_edge) >= 0.02 ? 'text-amber-400 font-semibold' : 'text-neutral-600'}>
                      {m.signal_direction.toUpperCase()} edge {(m.signal_edge * 100).toFixed(1)}%
                    </span>
                  )}
                  {openSlugs.has(m.slug) && (
                    <span className="text-amber-500/70 text-[9px] uppercase tracking-wider ml-auto">open</span>
                  )}
                </div>
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
