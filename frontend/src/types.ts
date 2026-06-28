export interface BtcPrice {
  price: number
  change_24h: number
  change_7d: number
  market_cap: number
  volume_24h: number
  last_updated: string
}

export interface Microstructure {
  rsi: number
  momentum_1m: number
  momentum_5m: number
  momentum_15m: number
  vwap_deviation: number
  sma_crossover: number
  volatility: number
  price: number
  source: string
}

export interface BtcWindow {
  slug: string
  market_id: string
  up_price: number
  down_price: number
  window_start: string
  window_end: string
  volume: number
  is_active: boolean
  is_upcoming: boolean
  time_until_end: number
  spread: number
}

export interface Signal {
  market_ticker: string
  market_title: string
  platform: string
  direction: string
  model_probability: number
  market_probability: number
  edge: number
  confidence: number
  suggested_size: number
  reasoning: string
  timestamp: string
  category: string
  event_slug?: string
  btc_price: number
  btc_change_24h: number
  window_end?: string
  actionable: boolean
}

export interface Trade {
  id: number
  market_ticker: string
  platform: string
  event_slug?: string | null
  direction: string
  entry_price: number
  size: number
  timestamp: string
  settled: boolean
  result: string
  pnl: number | null
  exit_reason?: string | null
}

export interface CryptoMarket {
  slug: string
  market_id: string
  window_minutes: number
  up_price: number
  down_price: number
  window_end: string
  volume: number
  time_until_end: number
  signal_direction: string | null
  signal_edge: number | null
  signal_confidence: number | null
}

export interface CryptoPosition {
  slug: string
  trade_id: number
  side: string
  direction: string
  entry_price: number
  size: number
  original_size: number
  stop_price: number
  expected_reversion: number
  opened_at: number
  partial_exited: boolean
  window_minutes: number
}

export interface CryptoStatus {
  enabled: boolean
  running: boolean
  trading_live: boolean
  open_positions: CryptoPosition[]
  today_pnl: number
  total_trades: number
  risk: {
    capital: { initial: number; current: number; peak: number; drawdown_pct: number }
    pnl: { daily: number; monthly: number; total: number }
    kelly_fraction: number
    win_rate: number
    consecutive: { losses: number; wins: number }
    recovery_mode: boolean
    status: string
    max_trade_size: number
    total_trades: number
  }
}

export interface BotStats {
  bankroll: number
  total_trades: number
  winning_trades: number
  win_rate: number
  total_pnl: number
  is_running: boolean
  last_run: string | null
}

export interface EquityPoint {
  timestamp: string
  pnl: number
  bankroll: number
}

export interface CalibrationSummary {
  total_signals: number
  total_with_outcome: number
  accuracy: number
  avg_predicted_edge: number
  avg_actual_edge: number
  brier_score: number
}

export interface FootballFixture {
  home_team: string
  away_team: string
  utc_kickoff: string
  status: string
  matchday: number
  source_id: number
}

export interface FootballLiveMatch {
  fixture_id: number
  home_team: string
  away_team: string
  status: string
  minute: number
  home_score: number
  away_score: number
  date: string
}

export interface FootballSession {
  id: number
  polymarket_link: string
  polymarket_slug: string | null
  condition_id: string | null
  yes_token_id: string | null
  no_token_id: string | null
  home_team: string | null
  away_team: string | null
  fixture_ref: string | null
  status: string
  created_at: string
  ended_at: string | null
  realized_pnl: number
  total_trades: number
  error_message: string | null
}

export interface MarketAnalysis {
  session_id: number
  text: string | null
  model: string | null
  timestamp: string | null
  latency_ms: number | null
}

export interface OddsComparison {
  session_id: number
  sportsbook_prob: number | null
  polymarket_prob: number | null
  edge: number | null
  bookmaker_count: number | null
  fetched_at: number | null
  configured: boolean
}

export interface TraderPosition {
  name: string
  wallet: string
  value_usd: number
  pnl_usd: number
  pnl_pct: number
}

export interface ConsensusTrade {
  condition_id: string
  market_title: string
  market_slug: string
  outcome: string
  trader_count: number
  traders: TraderPosition[]
  total_value_usd: number
  avg_price: number
}

export interface WhaleScan {
  trades: ConsensusTrade[]
  scanned_at: number | null
  trader_count: number
}

export interface DashboardData {
  stats: BotStats
  btc_price: BtcPrice | null
  microstructure: Microstructure | null
  windows: BtcWindow[]
  active_signals: Signal[]
  recent_trades: Trade[]
  equity_curve: EquityPoint[]
  calibration: CalibrationSummary | null
}
