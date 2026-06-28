import { formatDistanceToNow } from 'date-fns'
import { motion, AnimatePresence } from 'framer-motion'
import { useState, useEffect } from 'react'
import type { Trade } from '../types'

interface Props {
  trades: Trade[]
  newIds: Set<number>
}

export function CryptoTradesFeed({ trades, newIds }: Props) {
  const [flashIds, setFlashIds] = useState<Set<number>>(new Set())

  useEffect(() => {
    if (newIds.size === 0) return
    setFlashIds(prev => new Set([...prev, ...newIds]))
    const t = setTimeout(() => {
      setFlashIds(prev => {
        const next = new Set(prev)
        newIds.forEach(id => next.delete(id))
        return next
      })
    }, 1800)
    return () => clearTimeout(t)
  }, [newIds])

  const sorted = [...trades].sort(
    (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
  )

  if (sorted.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-neutral-600">
        <div className="w-2 h-2 rounded-full bg-amber-500 animate-pulse mb-2" />
        <p className="text-[10px] uppercase tracking-wider">Waiting for first trade…</p>
      </div>
    )
  }

  const settled = sorted.filter(t => t.settled)
  const pending = sorted.filter(t => !t.settled)
  const totalPnl = settled.reduce((s, t) => s + (t.pnl ?? 0), 0)
  const wins = settled.filter(t => t.result === 'win').length
  const losses = settled.filter(t => t.result === 'loss').length

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* Summary bar */}
      <div className="shrink-0 px-2 py-1 border-b border-neutral-800 flex items-center gap-3 text-[10px] tabular-nums">
        <span className="text-neutral-500">{sorted.length} trades</span>
        <span className="text-amber-400">{pending.length} open</span>
        <span className="text-green-500">{wins}W</span>
        <span className="text-red-500">{losses}L</span>
        <span className={`ml-auto font-semibold ${totalPnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
          {totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(4)} total
        </span>
      </div>

      {/* Live feed */}
      <div className="flex-1 overflow-y-auto min-h-0 font-mono">
        <AnimatePresence initial={false}>
          {sorted.map(trade => {
            const isFlash = flashIds.has(trade.id)
            const isPending = !trade.settled
            const isWin = trade.result === 'win'
            const isDown = trade.direction === 'down'
            const pnl = trade.pnl ?? 0
            const slug = (trade.event_slug || trade.market_ticker)
              .replace(/^btc-updown-\d+m-/, '')

            return (
              <motion.div
                key={trade.id}
                layout
                initial={{ opacity: 0, x: -8, backgroundColor: 'rgba(245,158,11,0.15)' }}
                animate={{
                  opacity: 1,
                  x: 0,
                  backgroundColor: isFlash ? 'rgba(245,158,11,0.08)' : 'rgba(0,0,0,0)',
                }}
                transition={{ duration: 0.3 }}
                className="px-2 py-1.5 border-b border-neutral-900 hover:bg-neutral-800/30"
              >
                <div className="flex items-center gap-2 text-[10px]">
                  {/* Status dot */}
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                    isPending
                      ? 'bg-amber-400 animate-pulse'
                      : isWin ? 'bg-green-500' : 'bg-red-500'
                  }`} />

                  {/* Direction */}
                  <span className={`font-bold uppercase w-8 shrink-0 ${isDown ? 'text-red-400' : 'text-green-400'}`}>
                    {trade.direction}
                  </span>

                  {/* Window slot */}
                  <span className="text-neutral-500 shrink-0">{slug}</span>

                  {/* Entry price */}
                  <span className="text-neutral-400 shrink-0">@{trade.entry_price.toFixed(3)}</span>

                  {/* Size */}
                  <span className="text-neutral-600 shrink-0">${trade.size.toFixed(2)}</span>

                  {/* PnL */}
                  <span className={`ml-auto shrink-0 font-semibold tabular-nums ${
                    isPending ? 'text-neutral-600' :
                    pnl > 0 ? 'text-green-500' : pnl < 0 ? 'text-red-500' : 'text-neutral-500'
                  }`}>
                    {isPending ? '…' : `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(4)}`}
                  </span>
                </div>

                <div className="flex items-center gap-2 mt-0.5 text-[9px] text-neutral-600">
                  <span>{trade.exit_reason ?? (isPending ? 'open' : 'exited')}</span>
                  <span className="ml-auto">
                    {formatDistanceToNow(new Date(trade.timestamp), { addSuffix: true })}
                  </span>
                </div>
              </motion.div>
            )
          })}
        </AnimatePresence>
      </div>
    </div>
  )
}
