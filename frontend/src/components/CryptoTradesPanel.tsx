import { useQuery } from '@tanstack/react-query'
import { useRef } from 'react'
import { fetchCryptoTrades } from '../api'
import { CryptoTradesFeed } from './CryptoTradesFeed'

export function CryptoTradesPanel() {
  const prevIds = useRef<Set<number>>(new Set())

  const { data: trades = [] } = useQuery({
    queryKey: ['crypto-trades'],
    queryFn: fetchCryptoTrades,
    refetchInterval: 2000,
  })

  const newIds = new Set(trades.filter(t => !prevIds.current.has(t.id)).map(t => t.id))
  trades.forEach(t => prevIds.current.add(t.id))

  return <CryptoTradesFeed trades={trades} newIds={newIds} />
}
