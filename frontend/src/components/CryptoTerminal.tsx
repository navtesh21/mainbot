import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { useMemo, useRef, useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  fetchCryptoStatus, fetchCryptoMarkets, fetchCryptoTrades,
  startCryptoEngine, stopCryptoEngine, runCryptoScan,
} from '../api'
import type { CryptoMarket, CryptoStatus, Trade } from '../types'

// ─── helpers ────────────────────────────────────────────────────────────────

function fmt(n: number, digits = 4) {
  return (n >= 0 ? '+' : '') + '$' + Math.abs(n).toFixed(digits)
}

function fmtPrice(p: number) { return (p * 100).toFixed(1) + 'c' }

function countdown(s: number) {
  const sec = Math.max(0, Math.floor(s))
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
}

function shortSlug(slug: string) {
  return slug.replace(/^btc-updown-\d+m-/, '')
}

// ─── sub-panels ─────────────────────────────────────────────────────────────

interface OpenPos {
  slug: string
  direction: string
  entry_price: number
  size: number
  currentPrice?: number
  unrealizedPnl?: number
  opened_at: number
  window_minutes: number
}

function PositionsPanel({ positions }: { positions: OpenPos[] }) {
  if (positions.length === 0) {
    return (
      <div className="flex-none border-b border-neutral-800">
        <div className="px-2 py-1 border-b border-neutral-800 flex items-center gap-2">
          <span className="text-[9px] text-neutral-600 uppercase tracking-widest">Positions</span>
          <span className="text-[9px] text-neutral-700">0 open</span>
        </div>
        <div className="px-2 py-3 text-[10px] text-neutral-700 text-center">no open positions</div>
      </div>
    )
  }

  return (
    <div className="flex-none border-b border-neutral-800">
      <div className="px-2 py-1 border-b border-neutral-800 flex items-center gap-2">
        <span className="text-[9px] text-neutral-500 uppercase tracking-widest">Positions</span>
        <span className="text-[9px] text-amber-400">{positions.length} open</span>
      </div>
      <table className="w-full text-[10px] tabular-nums">
        <thead>
          <tr className="text-neutral-700 border-b border-neutral-900">
            <th className="text-left px-2 py-0.5 font-normal">window</th>
            <th className="text-left px-1 py-0.5 font-normal">dir</th>
            <th className="text-right px-1 py-0.5 font-normal">entry</th>
            <th className="text-right px-1 py-0.5 font-normal">now</th>
            <th className="text-right px-2 py-0.5 font-normal">unreal</th>
          </tr>
        </thead>
        <tbody>
          <AnimatePresence>
            {positions.map(pos => {
              const upnl = pos.unrealizedPnl ?? 0
              const now = pos.currentPrice
              return (
                <motion.tr
                  key={pos.slug}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  className="border-b border-neutral-900/60 hover:bg-neutral-900/60"
                >
                  <td className="px-2 py-1 text-neutral-400">
                    <span className="text-neutral-600 text-[9px]">{pos.window_minutes}m </span>
                    {shortSlug(pos.slug)}
                  </td>
                  <td className={`px-1 py-1 font-bold uppercase ${pos.direction === 'up' ? 'text-green-400' : 'text-red-400'}`}>
                    {pos.direction === 'up' ? '↑' : '↓'}{pos.direction}
                  </td>
                  <td className="px-1 py-1 text-right text-neutral-500">{fmtPrice(pos.entry_price)}</td>
                  <td className="px-1 py-1 text-right text-neutral-300">
                    {now != null ? fmtPrice(now) : '…'}
                  </td>
                  <td className={`px-2 py-1 text-right font-semibold ${upnl > 0 ? 'text-green-400' : upnl < 0 ? 'text-red-400' : 'text-neutral-600'}`}>
                    {pos.unrealizedPnl != null ? fmt(upnl, 3) : '…'}
                  </td>
                </motion.tr>
              )
            })}
          </AnimatePresence>
        </tbody>
      </table>
    </div>
  )
}

function SignalsPanel({ markets, openSlugs }: { markets: CryptoMarket[]; openSlugs: Set<string> }) {
  return (
    <div className="flex-1 overflow-y-auto min-h-0">
      <div className="px-2 py-1 border-b border-neutral-800 sticky top-0 bg-black flex items-center gap-2">
        <span className="text-[9px] text-neutral-500 uppercase tracking-widest">Markets</span>
        <span className="text-[9px] text-neutral-700">{markets.length} active</span>
      </div>
      {markets.length === 0 && (
        <div className="px-2 py-3 text-[10px] text-neutral-700 text-center">fetching…</div>
      )}
      {markets.map(m => {
        const isOpen = openSlugs.has(m.slug)
        const hasEdge = m.signal_edge != null && Math.abs(m.signal_edge) >= 0.02
        return (
          <a
            key={m.slug}
            href={`https://polymarket.com/event/${m.slug}`}
            target="_blank"
            rel="noopener noreferrer"
            className={`block px-2 py-1 border-b border-neutral-900 hover:bg-neutral-900 cursor-pointer ${isOpen ? 'bg-amber-500/5' : ''}`}
          >
            <div className="flex items-center gap-1.5 text-[10px]">
              <span className="text-neutral-600 text-[9px] w-5 shrink-0">{m.window_minutes}m</span>
              <span className={`shrink-0 w-1.5 h-1.5 rounded-full ${isOpen ? 'bg-amber-400' : 'bg-neutral-700'}`} />
              <span className="text-neutral-400 truncate flex-1">{shortSlug(m.slug)}</span>
              <span className="text-neutral-600 shrink-0 text-[9px]">{countdown(m.time_until_end)}</span>
            </div>
            <div className="flex items-center gap-2 text-[9px] tabular-nums mt-0.5">
              <span className="text-green-500/70">↑{fmtPrice(m.up_price)}</span>
              <span className="text-red-500/70">↓{fmtPrice(m.down_price)}</span>
              {m.signal_direction && m.signal_edge != null && (
                <span className={hasEdge ? 'text-amber-400' : 'text-neutral-700'}>
                  {m.signal_direction === 'up' ? '↑' : '↓'} {(m.signal_edge * 100).toFixed(1)}% edge
                </span>
              )}
            </div>
          </a>
        )
      })}
    </div>
  )
}

function TradeBlotter({ trades }: { trades: Trade[] }) {
  const prevIds = useRef<Set<number>>(new Set())
  const [flashIds, setFlashIds] = useState<Set<number>>(new Set())

  const sorted = useMemo(
    () => [...trades].sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()),
    [trades]
  )

  useEffect(() => {
    const newOnes = sorted.filter(t => !prevIds.current.has(t.id))
    sorted.forEach(t => prevIds.current.add(t.id))
    if (newOnes.length === 0) return
    const ids = new Set(newOnes.map(t => t.id))
    setFlashIds(prev => new Set([...prev, ...ids]))
    const timer = setTimeout(() => setFlashIds(prev => {
      const next = new Set(prev)
      ids.forEach(id => next.delete(id))
      return next
    }), 1500)
    return () => clearTimeout(timer)
  }, [sorted])

  const settled = sorted.filter(t => t.settled)
  const totalPnl = settled.reduce((s, t) => s + (t.pnl ?? 0), 0)
  const wins = settled.filter(t => t.result === 'win').length
  const losses = settled.filter(t => t.result === 'loss').length

  return (
    <div className="flex flex-col min-h-0">
      {/* blotter header */}
      <div className="shrink-0 px-3 py-1 border-b border-neutral-800 flex items-center gap-3 text-[9px] tabular-nums bg-neutral-950">
        <span className="text-neutral-500 uppercase tracking-wider">Executions</span>
        <span className="text-neutral-600">{sorted.length} total</span>
        <span className="text-green-500">{wins}W</span>
        <span className="text-red-500">{losses}L</span>
        <span className="text-neutral-600">{sorted.filter(t => !t.settled).length} pending</span>
        <span className={`ml-auto font-semibold text-[10px] ${totalPnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
          {fmt(totalPnl, 4)} realized
        </span>
      </div>

      {/* column headers */}
      <div className="shrink-0 grid grid-cols-[56px_110px_34px_46px_46px_52px_80px_1fr] px-2 py-0.5 text-[9px] text-neutral-700 border-b border-neutral-900 font-mono uppercase tracking-wider">
        <span>time</span>
        <span>window</span>
        <span>dir</span>
        <span className="text-right">entry</span>
        <span className="text-right">size</span>
        <span className="text-right">p&amp;l</span>
        <span>reason</span>
        <span>st</span>
      </div>

      {/* rows */}
      <div className="flex-1 overflow-y-auto min-h-0 font-mono">
        {sorted.length === 0 ? (
          <div className="flex items-center justify-center h-20 text-[10px] text-neutral-700">
            <span className="animate-pulse">● waiting for first trade</span>
          </div>
        ) : (
          <AnimatePresence initial={false}>
            {sorted.map(trade => {
              const isNew = flashIds.has(trade.id)
              const isPending = !trade.settled
              const isWin = trade.result === 'win'
              const pnl = trade.pnl ?? 0
              const slug = shortSlug(trade.event_slug || trade.market_ticker)
              const time = new Date(trade.timestamp)
              const timeStr = time.toTimeString().slice(0, 8)

              return (
                <motion.div
                  key={trade.id}
                  layout
                  initial={{ opacity: 0, backgroundColor: 'rgba(234,179,8,0.2)' }}
                  animate={{
                    opacity: 1,
                    backgroundColor: isNew ? 'rgba(234,179,8,0.06)' : 'rgba(0,0,0,0)',
                  }}
                  transition={{ duration: 0.4 }}
                  className="grid grid-cols-[56px_110px_34px_46px_46px_52px_80px_1fr] px-2 py-1 border-b border-neutral-900/70 hover:bg-neutral-900/50 text-[10px] tabular-nums items-center"
                >
                  <span className="text-neutral-700 text-[9px]">{timeStr}</span>

                  <span className="text-neutral-500 truncate pr-1">{slug}</span>

                  <span className={`font-bold ${trade.direction === 'up' ? 'text-green-400' : 'text-red-400'}`}>
                    {trade.direction === 'up' ? '↑' : '↓'}
                  </span>

                  <span className="text-right text-neutral-500">{fmtPrice(trade.entry_price)}</span>

                  <span className="text-right text-neutral-600">${trade.size.toFixed(2)}</span>

                  <span className={`text-right font-semibold ${
                    isPending ? 'text-neutral-700' :
                    pnl > 0 ? 'text-green-400' :
                    pnl < 0 ? 'text-red-400' : 'text-neutral-600'
                  }`}>
                    {isPending ? '—' : fmt(pnl, 4)}
                  </span>

                  <span className="text-neutral-700 text-[9px] truncate pr-1">
                    {trade.exit_reason ?? (isPending ? 'open' : '?')}
                  </span>

                  <span className={`text-[9px] font-bold uppercase ${
                    isPending ? 'text-amber-500 animate-pulse' :
                    isWin ? 'text-green-500' : 'text-red-500'
                  }`}>
                    {isPending ? '●' : isWin ? 'WIN' : 'LOSS'}
                  </span>
                </motion.div>
              )
            })}
          </AnimatePresence>
        )}
      </div>
    </div>
  )
}

// ─── main terminal ───────────────────────────────────────────────────────────

export function CryptoTerminal() {
  const queryClient = useQueryClient()

  const { data: status } = useQuery<CryptoStatus>({
    queryKey: ['crypto-status'],
    queryFn: fetchCryptoStatus,
    refetchInterval: 2000,
  })

  const { data: markets = [] } = useQuery<CryptoMarket[]>({
    queryKey: ['crypto-markets'],
    queryFn: fetchCryptoMarkets,
    refetchInterval: 2000,
  })

  const { data: trades = [] } = useQuery<Trade[]>({
    queryKey: ['crypto-trades'],
    queryFn: fetchCryptoTrades,
    refetchInterval: 2000,
  })

  const startMut = useMutation({ mutationFn: startCryptoEngine, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['crypto-status'] }) })
  const stopMut = useMutation({ mutationFn: stopCryptoEngine, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['crypto-status'] }) })
  const scanMut = useMutation({
    mutationFn: runCryptoScan,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['crypto-status'] })
      queryClient.invalidateQueries({ queryKey: ['crypto-markets'] })
      queryClient.invalidateQueries({ queryKey: ['crypto-trades'] })
    },
  })

  const marketsBySlug = useMemo(() => new Map(markets.map(m => [m.slug, m])), [markets])

  const openPositions: OpenPos[] = useMemo(() => {
    return (status?.open_positions ?? []).map(pos => {
      const market = marketsBySlug.get(pos.slug)
      const currentPrice = pos.direction === 'up' ? market?.up_price : market?.down_price
      const unrealizedPnl = currentPrice != null && pos.entry_price > 0
        ? (pos.size / pos.entry_price) * (currentPrice - pos.entry_price)
        : undefined
      return { ...pos, currentPrice, unrealizedPnl }
    })
  }, [status, marketsBySlug])

  const openSlugs = useMemo(() => new Set(openPositions.map(p => p.slug)), [openPositions])

  const settledTrades = trades.filter(t => t.settled)
  const totalPnl = settledTrades.reduce((s, t) => s + (t.pnl ?? 0), 0)
  const totalUnrealized = openPositions.reduce((s, p) => s + (p.unrealizedPnl ?? 0), 0)

  const isRunning = status?.running ?? false
  const isLive = status?.trading_live ?? false
  const slots = openPositions.length

  return (
    <div className="h-screen bg-black text-neutral-200 flex flex-col overflow-hidden font-mono select-none">

      {/* ── top status bar ── */}
      <div className="shrink-0 border-b border-neutral-800 bg-neutral-950 px-3 py-1.5 flex items-center gap-4 text-[10px] tabular-nums">
        <Link to="/" className="text-neutral-600 hover:text-neutral-400 text-[9px] uppercase tracking-wider shrink-0">
          ← DESK
        </Link>

        <span className="text-neutral-700">|</span>
        <span className="text-neutral-400 font-bold tracking-widest uppercase text-[9px]">Polymarket BTC Scalp</span>

        <span className="text-neutral-700">|</span>

        {/* engine state */}
        <span className="flex items-center gap-1">
          <span className={`w-1.5 h-1.5 rounded-full ${isRunning ? 'bg-green-500 animate-pulse' : 'bg-neutral-700'}`} />
          <span className={isRunning ? 'text-green-400' : 'text-neutral-600'}>{isRunning ? 'RUNNING' : 'STOPPED'}</span>
        </span>

        {/* mode */}
        <span className={isLive ? 'text-red-400 font-bold' : 'text-amber-400'}>
          {isLive ? '⚡ LIVE' : '◎ DRY-RUN'}
        </span>

        {/* slots */}
        <span className={`${slots >= 5 ? 'text-amber-400' : 'text-neutral-500'}`}>
          {slots}/5 slots
        </span>

        {/* pnl */}
        <span className={totalPnl >= 0 ? 'text-green-500' : 'text-red-500'}>
          {fmt(totalPnl, 4)} realized
        </span>
        <span className={totalUnrealized >= 0 ? 'text-green-400/70' : 'text-red-400/70'}>
          {fmt(totalUnrealized, 4)} unrealized
        </span>

        {/* controls */}
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => scanMut.mutate()}
            disabled={scanMut.isPending}
            className="px-2 py-0.5 text-[9px] border border-neutral-700 text-neutral-400 hover:border-amber-600 hover:text-amber-400 disabled:opacity-40 uppercase tracking-wider transition-colors"
          >
            {scanMut.isPending ? '…' : 'SCAN'}
          </button>
          {isRunning ? (
            <button
              onClick={() => stopMut.mutate()}
              className="px-2 py-0.5 text-[9px] border border-red-800 text-red-400 hover:bg-red-500/10 uppercase tracking-wider"
            >
              STOP
            </button>
          ) : (
            <button
              onClick={() => startMut.mutate()}
              disabled={!status?.enabled}
              className="px-2 py-0.5 text-[9px] border border-green-800 text-green-400 hover:bg-green-500/10 disabled:opacity-40 uppercase tracking-wider"
            >
              START
            </button>
          )}
        </div>
      </div>

      {/* ── body: left sidebar + blotter ── */}
      <div className="flex-1 min-h-0 flex overflow-hidden">

        {/* left column: positions + signals */}
        <div className="w-64 shrink-0 border-r border-neutral-800 flex flex-col min-h-0">
          <PositionsPanel positions={openPositions} />
          <SignalsPanel markets={markets} openSlugs={openSlugs} />
        </div>

        {/* right: execution blotter */}
        <div className="flex-1 min-h-0 flex flex-col">
          <TradeBlotter trades={trades} />
        </div>
      </div>
    </div>
  )
}
