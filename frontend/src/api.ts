import axios from 'axios'
import type { DashboardData, Signal, Trade, BotStats, BtcPrice, BtcWindow, FootballFixture, FootballLiveMatch, FootballSession, MarketAnalysis, WhaleScan, OddsComparison, CryptoMarket, CryptoStatus } from './types'

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000'

const api = axios.create({
  baseURL: `${API_BASE}/api`,
})

export async function fetchDashboard(): Promise<DashboardData> {
  const { data } = await api.get<DashboardData>('/dashboard')
  return data
}

export async function fetchSignals(): Promise<Signal[]> {
  const { data } = await api.get<Signal[]>('/signals')
  return data
}

export async function fetchBtcPrice(): Promise<BtcPrice | null> {
  const { data } = await api.get<BtcPrice | null>('/btc/price')
  return data
}

export async function fetchBtcWindows(): Promise<BtcWindow[]> {
  const { data } = await api.get<BtcWindow[]>('/btc/windows')
  return data
}

export async function fetchTrades(): Promise<Trade[]> {
  const { data } = await api.get<Trade[]>('/trades')
  return data
}

export async function fetchStats(): Promise<BotStats> {
  const { data } = await api.get<BotStats>('/stats')
  return data
}

export async function runScan(): Promise<{ total_signals: number; actionable_signals: number }> {
  const { data } = await api.post('/run-scan')
  return data
}

export async function simulateTrade(ticker: string): Promise<{ trade_id: number; size: number }> {
  const { data } = await api.post('/simulate-trade', null, {
    params: { signal_ticker: ticker }
  })
  return data
}

export async function startBot(): Promise<{ status: string; is_running: boolean }> {
  const { data } = await api.post('/bot/start')
  return data
}

export async function stopBot(): Promise<{ status: string; is_running: boolean }> {
  const { data } = await api.post('/bot/stop')
  return data
}

export async function settleTradesApi(): Promise<{ settled_count: number }> {
  const { data } = await api.post('/settle-trades')
  return data
}

export async function resetBot(): Promise<{ status: string; trades_deleted: number; new_bankroll: number }> {
  const { data } = await api.post('/bot/reset')
  return data
}

export async function fetchFootballFixtures(): Promise<FootballFixture[]> {
  const { data } = await api.get<FootballFixture[]>('/football/fixtures')
  return data
}

export async function fetchFootballLiveMatches(): Promise<FootballLiveMatch[]> {
  const { data } = await api.get<FootballLiveMatch[]>('/football/live')
  return data
}

export async function fetchFootballSessions(): Promise<FootballSession[]> {
  const { data } = await api.get<FootballSession[]>('/football/sessions')
  return data
}

export async function startFootballSession(link: string): Promise<FootballSession> {
  const { data } = await api.post<FootballSession>('/football/sessions', { link })
  return data
}

export async function stopFootballSession(id: number): Promise<FootballSession> {
  const { data } = await api.post<FootballSession>(`/football/sessions/${id}/stop`)
  return data
}

export async function fetchMarketAnalysis(sessionId: number): Promise<MarketAnalysis> {
  const { data } = await api.get<MarketAnalysis>(`/football/sessions/${sessionId}/analysis`)
  return data
}

export async function fetchOddsComparison(sessionId: number): Promise<OddsComparison> {
  const { data } = await api.get<OddsComparison>(`/football/sessions/${sessionId}/odds`)
  return data
}

export async function fetchWhaleScan(): Promise<WhaleScan> {
  const { data } = await api.get<WhaleScan>('/whales/scan')
  return data
}

export async function runWhaleScan(): Promise<WhaleScan> {
  const { data } = await api.post<WhaleScan>('/whales/scan')
  return data
}

export async function fetchCryptoStatus(): Promise<CryptoStatus> {
  const { data } = await api.get<CryptoStatus>('/crypto/status')
  return data
}

export async function fetchCryptoMarkets(): Promise<CryptoMarket[]> {
  const { data } = await api.get<CryptoMarket[]>('/crypto/markets')
  return data
}

export async function fetchCryptoTrades(): Promise<Trade[]> {
  const { data } = await api.get<Trade[]>('/crypto/trades')
  return data
}

export async function startCryptoEngine(): Promise<{ status: string; is_running: boolean }> {
  const { data } = await api.post('/crypto/start')
  return data
}

export async function stopCryptoEngine(): Promise<{ status: string; is_running: boolean }> {
  const { data } = await api.post('/crypto/stop')
  return data
}

export async function runCryptoScan(): Promise<{ status: string }> {
  const { data } = await api.post('/crypto/scan')
  return data
}
