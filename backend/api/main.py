"""FastAPI backend for BTC 5-min trading bot dashboard."""
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from typing import List, Optional
import asyncio
import json
import os

from backend.config import settings
from backend.models.database import (
    get_db, init_db, SessionLocal,
    Signal, Trade, BotState, AILog, ScanLog, FootballSession
)
from backend.core.signals import scan_for_signals, TradingSignal
from backend.data.btc_markets import fetch_active_btc_markets, BtcMarket
from backend.data.crypto import fetch_crypto_price, compute_btc_microstructure

from pydantic import BaseModel

app = FastAPI(
    title="BTC 5-Min Trading Bot",
    description="Polymarket BTC Up/Down 5-minute market trading bot",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass


ws_manager = ConnectionManager()


# Pydantic response models
class BtcPriceResponse(BaseModel):
    price: float
    change_24h: float
    change_7d: float
    market_cap: float
    volume_24h: float
    last_updated: datetime


class BtcWindowResponse(BaseModel):
    slug: str
    market_id: str
    up_price: float
    down_price: float
    window_start: datetime
    window_end: datetime
    volume: float
    is_active: bool
    is_upcoming: bool
    time_until_end: float
    spread: float


class CryptoMarketResponse(BaseModel):
    slug: str
    market_id: str
    window_minutes: int
    up_price: float
    down_price: float
    window_end: datetime
    volume: float
    time_until_end: float
    signal_direction: Optional[str] = None
    signal_edge: Optional[float] = None
    signal_confidence: Optional[float] = None


class CryptoStatusResponse(BaseModel):
    enabled: bool
    running: bool
    trading_live: bool
    open_positions: list
    today_pnl: float
    total_trades: int
    risk: dict


class MicrostructureResponse(BaseModel):
    rsi: float = 50.0
    momentum_1m: float = 0.0
    momentum_5m: float = 0.0
    momentum_15m: float = 0.0
    vwap_deviation: float = 0.0
    sma_crossover: float = 0.0
    volatility: float = 0.0
    price: float = 0.0
    source: str = "unknown"


class SignalResponse(BaseModel):
    market_ticker: str
    market_title: str
    platform: str
    direction: str
    model_probability: float
    market_probability: float
    edge: float
    confidence: float
    suggested_size: float
    reasoning: str
    timestamp: datetime
    category: str = "crypto"
    event_slug: Optional[str] = None
    btc_price: float = 0.0
    btc_change_24h: float = 0.0
    window_end: Optional[datetime] = None
    actionable: bool = False


class TradeResponse(BaseModel):
    id: int
    market_ticker: str
    platform: str
    event_slug: Optional[str] = None
    direction: str
    entry_price: float
    size: float
    timestamp: datetime
    settled: bool
    result: str
    pnl: Optional[float]
    exit_reason: Optional[str] = None


class BotStats(BaseModel):
    bankroll: float
    total_trades: int
    winning_trades: int
    win_rate: float
    total_pnl: float
    is_running: bool
    last_run: Optional[datetime]


class CalibrationBucket(BaseModel):
    bucket: str
    predicted_avg: float
    actual_rate: float
    count: int


class CalibrationSummary(BaseModel):
    total_signals: int
    total_with_outcome: int
    accuracy: float
    avg_predicted_edge: float
    avg_actual_edge: float
    brier_score: float


class FootballFixtureResponse(BaseModel):
    home_team: str
    away_team: str
    utc_kickoff: str
    status: str
    matchday: int = 0
    source_id: int = 0


class FootballLiveMatchResponse(BaseModel):
    fixture_id: int
    home_team: str
    away_team: str
    status: str
    minute: int
    home_score: int
    away_score: int
    date: str = ""


class MarketAnalysisResponse(BaseModel):
    session_id: int
    text: Optional[str] = None
    model: Optional[str] = None
    timestamp: Optional[str] = None
    latency_ms: Optional[float] = None


class OddsComparisonResponse(BaseModel):
    session_id: int
    sportsbook_prob: Optional[float] = None
    polymarket_prob: Optional[float] = None
    edge: Optional[float] = None
    bookmaker_count: Optional[int] = None
    fetched_at: Optional[float] = None
    configured: bool = True


class TraderPositionResponse(BaseModel):
    name: str
    wallet: str
    value_usd: float
    pnl_usd: float
    pnl_pct: float


class ConsensusTradeResponse(BaseModel):
    condition_id: str
    market_title: str
    market_slug: str
    outcome: str
    trader_count: int
    traders: List[TraderPositionResponse]
    total_value_usd: float
    avg_price: float


class WhaleScanResponse(BaseModel):
    trades: List[ConsensusTradeResponse]
    scanned_at: Optional[float] = None
    trader_count: int = 0


class BacktestTradeResponse(BaseModel):
    side: str
    entry_price: float
    entry_minute: int
    exit_price: float
    exit_minute: int
    exit_reason: str
    pnl_per_share: float
    trigger: str


class BacktestReportResponse(BaseModel):
    home_team: str
    away_team: str
    match_date: str
    price_points: int
    events_found: int
    total_trades: int
    wins: int
    win_rate: float
    total_pnl_per_share: float
    trades: List[BacktestTradeResponse]


class FootballSessionResponse(BaseModel):
    id: int
    polymarket_link: str
    polymarket_slug: Optional[str] = None
    condition_id: Optional[str] = None
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    fixture_ref: Optional[str] = None
    status: str
    created_at: datetime
    ended_at: Optional[datetime] = None
    realized_pnl: float
    total_trades: int
    error_message: Optional[str] = None


class StartFootballSessionRequest(BaseModel):
    link: str


class DashboardData(BaseModel):
    stats: BotStats
    btc_price: Optional[BtcPriceResponse]
    microstructure: Optional[MicrostructureResponse] = None
    windows: List[BtcWindowResponse]
    active_signals: List[SignalResponse]
    recent_trades: List[TradeResponse]
    equity_curve: List[dict]
    calibration: Optional[CalibrationSummary] = None


class EventResponse(BaseModel):
    timestamp: str
    type: str
    message: str
    data: dict = {}


# Startup / Shutdown
@app.on_event("startup")
async def startup():
    print("=" * 60)
    print("BTC 5-MIN TRADING BOT v3.0")
    print("=" * 60)
    print("Initializing database...")

    init_db()

    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        if not state:
            state = BotState(
                bankroll=settings.INITIAL_BANKROLL,
                total_trades=0,
                winning_trades=0,
                total_pnl=0.0,
                is_running=settings.BTC_ENABLED
            )
            db.add(state)
            db.commit()
            print(f"Created new bot state with ${settings.INITIAL_BANKROLL:,.2f} bankroll")
        else:
            state.is_running = settings.BTC_ENABLED
            db.commit()
            print(f"Loaded bot state: Bankroll ${state.bankroll:,.2f}, P&L ${state.total_pnl:+,.2f}, {state.total_trades} trades")
    finally:
        db.close()

    print("")
    print("Configuration:")
    print(f"  - Simulation mode: {settings.SIMULATION_MODE}")
    print(f"  - Min edge threshold: {settings.MIN_EDGE_THRESHOLD:.0%}")
    print(f"  - Kelly fraction: {settings.KELLY_FRACTION:.0%}")
    print(f"  - Scan interval: {settings.SCAN_INTERVAL_SECONDS}s")
    print(f"  - Settlement interval: {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    print("")

    from backend.core.scheduler import start_scheduler, log_event
    start_scheduler()
    if settings.BTC_ENABLED:
        log_event("success", "BTC 5-min trading bot initialized")
    else:
        log_event("info", "BTC trading paused (BTC_ENABLED=false) — football-only mode")

    # Football sessions are tracked by an in-memory pipeline that does not
    # survive a process restart. Any row still marked "running"/"starting"
    # from a previous process life has no live pipeline behind it — mark it
    # stopped rather than let it sit as a misleading zombie in the dashboard.
    from backend.models.database import FootballSession
    db = SessionLocal()
    try:
        orphaned = db.query(FootballSession).filter(
            FootballSession.status.in_(["running", "starting"])
        ).all()
        for session in orphaned:
            session.status = "stopped"
            session.error_message = "Interrupted by server restart"
            session.ended_at = datetime.utcnow()
        if orphaned:
            db.commit()
            print(f"Marked {len(orphaned)} orphaned football session(s) as stopped (no live pipeline after restart)")
    finally:
        db.close()

    if settings.BTC_ENABLED:
        print("Bot is now running!")
        print(f"  - BTC scan: every {settings.SCAN_INTERVAL_SECONDS}s (edge >= {settings.MIN_EDGE_THRESHOLD:.0%})")
        print(f"  - Settlement check: every {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    else:
        print("BTC trading paused (BTC_ENABLED=false) — football-only mode")

    # Settle crypto scalp trades orphaned by the previous restart
    if settings.CRYPTO_ENABLED:
        import asyncio
        from backend.crypto.engine import settle_orphaned_trades
        asyncio.create_task(settle_orphaned_trades())

    print("=" * 60)


@app.on_event("shutdown")
async def shutdown():
    from backend.core.scheduler import stop_scheduler
    stop_scheduler()


# Core endpoints
@app.get("/")
async def root():
    return {"status": "ok", "message": "BTC 5-Min Trading Bot API v3.0", "simulation_mode": settings.SIMULATION_MODE}


@app.get("/api/health")
async def health():
    return {"status": "healthy"}


@app.get("/api/stats", response_model=BotStats)
async def get_stats(db: Session = Depends(get_db)):
    state = db.query(BotState).first()
    if not state:
        raise HTTPException(status_code=404, detail="Bot state not initialized")

    win_rate = state.winning_trades / state.total_trades if state.total_trades > 0 else 0

    return BotStats(
        bankroll=state.bankroll,
        total_trades=state.total_trades,
        winning_trades=state.winning_trades,
        win_rate=win_rate,
        total_pnl=state.total_pnl,
        is_running=state.is_running,
        last_run=state.last_run
    )


# BTC-specific endpoints
@app.get("/api/btc/price", response_model=Optional[BtcPriceResponse])
async def get_btc_price():
    """Get current BTC price and momentum data."""
    try:
        btc = await fetch_crypto_price("BTC")
        if not btc:
            return None

        return BtcPriceResponse(
            price=btc.current_price,
            change_24h=btc.change_24h,
            change_7d=btc.change_7d,
            market_cap=btc.market_cap,
            volume_24h=btc.volume_24h,
            last_updated=btc.last_updated
        )
    except Exception:
        return None


@app.get("/api/btc/windows", response_model=List[BtcWindowResponse])
async def get_btc_windows():
    """Get upcoming BTC 5-min windows with prices."""
    try:
        markets = await fetch_active_btc_markets()
        return [
            BtcWindowResponse(
                slug=m.slug,
                market_id=m.market_id,
                up_price=m.up_price,
                down_price=m.down_price,
                window_start=m.window_start,
                window_end=m.window_end,
                volume=m.volume,
                is_active=m.is_active,
                is_upcoming=m.is_upcoming,
                time_until_end=m.time_until_end,
                spread=m.spread,
            )
            for m in markets
        ]
    except Exception:
        return []


@app.get("/api/crypto/status", response_model=CryptoStatusResponse)
async def get_crypto_status():
    """Crypto scalp engine running state, open positions, today's P&L."""
    from backend.crypto.engine import get_engine_status
    return CryptoStatusResponse(**await get_engine_status())


@app.post("/api/crypto/start")
async def start_crypto_engine():
    from backend.crypto.engine import set_engine_running
    from backend.core.scheduler import log_event

    ok, reason = set_engine_running(True)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    log_event("success", "Crypto scalp engine started")
    return {"status": "started", "is_running": True}


@app.post("/api/crypto/stop")
async def stop_crypto_engine():
    from backend.crypto.engine import set_engine_running
    from backend.core.scheduler import log_event

    set_engine_running(False)
    log_event("info", "Crypto scalp engine paused")
    return {"status": "stopped", "is_running": False}


@app.get("/api/crypto/markets", response_model=List[CryptoMarketResponse])
async def get_crypto_markets():
    """Active BTC 5m+15m windows with live scalp signal (edge/direction/confidence)."""
    from backend.crypto.engine import _fetch_all_markets
    from backend.core.signals import generate_btc_signal

    try:
        markets = await _fetch_all_markets()
    except Exception:
        return []

    results = []
    for m in markets:
        signal = None
        try:
            signal = await generate_btc_signal(m)
        except Exception:
            pass
        results.append(CryptoMarketResponse(
            slug=m.slug,
            market_id=m.market_id,
            window_minutes=m.window_minutes,
            up_price=m.up_price,
            down_price=m.down_price,
            window_end=m.window_end,
            volume=m.volume,
            time_until_end=m.time_until_end,
            signal_direction=signal.direction if signal else None,
            signal_edge=signal.edge if signal else None,
            signal_confidence=signal.confidence if signal else None,
        ))
    return results


@app.get("/api/crypto/trades", response_model=List[TradeResponse])
async def get_crypto_trades(limit: int = 50, db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(Trade.market_type == "crypto_scalp").order_by(
        Trade.timestamp.desc()
    ).limit(limit).all()
    return [
        TradeResponse(
            id=t.id,
            market_ticker=t.market_ticker,
            platform=t.platform,
            event_slug=t.event_slug,
            direction=t.direction,
            entry_price=t.entry_price,
            size=t.size,
            timestamp=t.timestamp,
            settled=t.settled,
            result=t.result,
            pnl=t.pnl,
            exit_reason=t.exit_reason,
        )
        for t in trades
    ]


@app.post("/api/crypto/scan")
async def run_crypto_scan():
    """Manually trigger one crypto scalp engine tick (entry + exit checks)."""
    from backend.crypto.engine import scan_and_scalp
    from backend.core.scheduler import log_event

    log_event("info", "Manual crypto scan triggered")
    await scan_and_scalp()
    return {"status": "ok"}


@app.get("/api/signals", response_model=List[SignalResponse])
async def get_signals():
    """Get current BTC trading signals."""
    try:
        signals = await scan_for_signals()
        return [_signal_to_response(s) for s in signals]
    except Exception:
        return []


@app.get("/api/signals/actionable", response_model=List[SignalResponse])
async def get_actionable_signals():
    """Get only signals that pass the edge threshold."""
    try:
        signals = await scan_for_signals()
        actionable = [s for s in signals if s.passes_threshold]
        return [_signal_to_response(s) for s in actionable]
    except Exception:
        return []


def _signal_to_response(s: TradingSignal, actionable: bool = False) -> SignalResponse:
    return SignalResponse(
        market_ticker=s.market.market_id,
        market_title=f"BTC 5m - {s.market.slug}",
        platform="polymarket",
        direction=s.direction,
        model_probability=s.model_probability,
        market_probability=s.market_probability,
        edge=s.edge,
        confidence=s.confidence,
        suggested_size=s.suggested_size,
        reasoning=s.reasoning,
        timestamp=s.timestamp,
        category="crypto",
        event_slug=s.market.slug,
        btc_price=s.btc_price,
        btc_change_24h=s.btc_change_24h,
        window_end=s.market.window_end,
        actionable=actionable,
    )


@app.get("/api/trades", response_model=List[TradeResponse])
async def get_trades(
    limit: int = 50,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Trade)
    if status:
        query = query.filter(Trade.result == status)
    trades = query.order_by(Trade.timestamp.desc()).limit(limit).all()

    return [
        TradeResponse(
            id=t.id,
            market_ticker=t.market_ticker,
            platform=t.platform,
            event_slug=t.event_slug,
            direction=t.direction,
            entry_price=t.entry_price,
            size=t.size,
            timestamp=t.timestamp,
            settled=t.settled,
            result=t.result,
            pnl=t.pnl,
            exit_reason=t.exit_reason,
        )
        for t in trades
    ]


@app.get("/api/equity-curve")
async def get_equity_curve(db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(Trade.market_type == "btc", Trade.settled == True).order_by(Trade.timestamp).all()

    curve = []
    cumulative_pnl = 0
    bankroll = settings.INITIAL_BANKROLL

    for trade in trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": bankroll + cumulative_pnl,
                "trade_id": trade.id
            })

    return curve


@app.post("/api/simulate-trade")
async def simulate_trade(signal_ticker: str, db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    signals = await scan_for_signals()
    signal = next((s for s in signals if s.market.market_id == signal_ticker), None)

    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")

    state = db.query(BotState).first()
    if not state:
        raise HTTPException(status_code=500, detail="Bot state not initialized")

    entry_price = signal.market.up_price if signal.direction == "up" else signal.market.down_price

    trade = Trade(
        market_ticker=signal.market.market_id,
        platform="polymarket",
        event_slug=signal.market.slug,
        direction=signal.direction,
        entry_price=entry_price,
        size=min(signal.suggested_size, state.bankroll * 0.05),
        model_probability=signal.model_probability,
        market_price_at_entry=signal.market_probability,
        edge_at_entry=signal.edge
    )

    db.add(trade)
    state.total_trades += 1
    db.commit()

    log_event("trade", f"Manual BTC trade: {signal.direction.upper()} {signal.market.slug}")
    return {"status": "ok", "trade_id": trade.id, "size": trade.size}


@app.post("/api/run-scan")
async def run_scan(db: Session = Depends(get_db)):
    from backend.core.scheduler import run_manual_scan, log_event

    state = db.query(BotState).first()
    if state:
        state.last_run = datetime.utcnow()
        db.commit()

    log_event("info", "Manual scan triggered (BTC)")
    await run_manual_scan()

    signals = await scan_for_signals()
    actionable = [s for s in signals if s.passes_threshold]

    return {
        "status": "ok",
        "total_signals": len(signals),
        "actionable_signals": len(actionable),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/api/settle-trades")
async def settle_trades_endpoint(db: Session = Depends(get_db)):
    from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements
    from backend.core.scheduler import log_event

    log_event("info", "Manual settlement triggered")

    settled = await settle_pending_trades(db)
    await update_bot_state_with_settlements(db, settled)

    return {
        "status": "ok",
        "settled_count": len(settled),
        "trades": [{"id": t.id, "result": t.result, "pnl": t.pnl} for t in settled]
    }


def _compute_calibration_summary(db: Session) -> Optional[CalibrationSummary]:
    """Compute calibration summary from settled signals.

    BTC-only: outcome_correct/Brier scoring assumes a binary 0/1 settlement_value,
    which is only meaningful for BTC's pass/fail resolution — football's
    settlement_value is a continuous exit price (see backend/core/settlement.py's
    market_type=="football" skip) and would silently corrupt this aggregate if mixed in.
    """
    total_signals = db.query(Signal).filter(Signal.market_type == "btc").count()
    settled_signals = db.query(Signal).filter(
        Signal.market_type == "btc", Signal.outcome_correct.isnot(None)
    ).all()

    if not settled_signals:
        if total_signals == 0:
            return None
        return CalibrationSummary(
            total_signals=total_signals,
            total_with_outcome=0,
            accuracy=0.0,
            avg_predicted_edge=0.0,
            avg_actual_edge=0.0,
            brier_score=0.0,
        )

    total_with_outcome = len(settled_signals)
    correct = sum(1 for s in settled_signals if s.outcome_correct)
    accuracy = correct / total_with_outcome if total_with_outcome > 0 else 0.0

    avg_predicted_edge = sum(abs(s.edge) for s in settled_signals) / total_with_outcome
    # Actual edge: for correct predictions, edge was real; for incorrect, edge was negative
    avg_actual_edge = sum(
        abs(s.edge) if s.outcome_correct else -abs(s.edge)
        for s in settled_signals
    ) / total_with_outcome

    # Brier score: mean squared error of probability forecasts
    # For each signal: (predicted_prob - actual_outcome)^2
    brier_sum = 0.0
    for s in settled_signals:
        # Model probability is for UP; actual is 1.0 if UP won, 0.0 if DOWN won
        actual = s.settlement_value if s.settlement_value is not None else 0.5
        brier_sum += (s.model_probability - actual) ** 2
    brier_score = brier_sum / total_with_outcome

    return CalibrationSummary(
        total_signals=total_signals,
        total_with_outcome=total_with_outcome,
        accuracy=accuracy,
        avg_predicted_edge=avg_predicted_edge,
        avg_actual_edge=avg_actual_edge,
        brier_score=brier_score,
    )


@app.get("/api/calibration")
async def get_calibration(db: Session = Depends(get_db)):
    """Return calibration data: predicted probability vs actual win rate."""
    signals = db.query(Signal).filter(
        Signal.market_type == "btc", Signal.outcome_correct.isnot(None)
    ).all()

    if not signals:
        return {"buckets": [], "summary": None}

    # Bucket signals by model_probability into 5% bins
    from collections import defaultdict
    buckets_data = defaultdict(lambda: {"predicted_sum": 0.0, "correct": 0, "total": 0})

    for s in signals:
        # Bin by 5% increments
        bin_start = int(s.model_probability * 100 // 5) * 5
        bin_end = bin_start + 5
        bucket_key = f"{bin_start}-{bin_end}%"

        buckets_data[bucket_key]["predicted_sum"] += s.model_probability
        buckets_data[bucket_key]["total"] += 1
        if s.outcome_correct:
            buckets_data[bucket_key]["correct"] += 1

    buckets = []
    for bucket_key in sorted(buckets_data.keys()):
        d = buckets_data[bucket_key]
        buckets.append(CalibrationBucket(
            bucket=bucket_key,
            predicted_avg=d["predicted_sum"] / d["total"],
            actual_rate=d["correct"] / d["total"],
            count=d["total"],
        ))

    summary = _compute_calibration_summary(db)

    return {"buckets": buckets, "summary": summary}


# Football endpoints (read-only fixture discovery)
@app.get("/api/football/fixtures", response_model=List[FootballFixtureResponse])
async def get_football_fixtures():
    """Get the World Cup fixture calendar from football-data.org."""
    if not settings.FOOTBALL_ENABLED:
        return []

    try:
        from backend.data.football_schedule import get_schedule_service

        service = get_schedule_service()
        matches = await service.get_schedule()
        return [
            FootballFixtureResponse(
                home_team=m.home_team,
                away_team=m.away_team,
                utc_kickoff=m.utc_kickoff,
                status=m.status,
                matchday=m.matchday,
                source_id=m.source_id,
            )
            for m in matches
        ]
    except Exception:
        return []


@app.get("/api/football/live", response_model=List[FootballLiveMatchResponse])
async def get_football_live_matches():
    """Get currently live matches (Telegram primary, Flashscore fallback)."""
    if not settings.FOOTBALL_ENABLED:
        return []

    try:
        from backend.data.football_live import get_live_source

        source = get_live_source()
        matches = await source.get_live_matches()
        return [
            FootballLiveMatchResponse(
                fixture_id=m.fixture_id,
                home_team=m.home_team,
                away_team=m.away_team,
                status=m.status,
                minute=m.minute,
                home_score=m.home_score,
                away_score=m.away_score,
                date=m.date,
            )
            for m in matches
        ]
    except Exception:
        return []


def _session_to_response(s) -> FootballSessionResponse:
    return FootballSessionResponse(
        id=s.id,
        polymarket_link=s.polymarket_link,
        polymarket_slug=s.polymarket_slug,
        condition_id=s.condition_id,
        yes_token_id=s.yes_token_id,
        no_token_id=s.no_token_id,
        home_team=s.home_team,
        away_team=s.away_team,
        fixture_ref=s.fixture_ref,
        status=s.status,
        created_at=s.created_at,
        ended_at=s.ended_at,
        realized_pnl=s.realized_pnl or 0.0,
        total_trades=s.total_trades or 0,
        error_message=s.error_message,
    )


@app.post("/api/football/sessions", response_model=FootballSessionResponse)
async def start_football_session(req: StartFootballSessionRequest):
    """Paste a Polymarket match link to resolve it and start a per-match session."""
    from backend.football.session_manager import start_session, SessionStartError

    try:
        session = await start_session(req.link)
        return _session_to_response(session)
    except SessionStartError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/football/sessions", response_model=List[FootballSessionResponse])
async def get_football_sessions():
    from backend.football.session_manager import list_sessions

    # Offload the blocking SQLite query to a thread — unlike the BTC endpoints,
    # this event loop also drives latency-sensitive per-session WS/spike
    # handling (backend/football/price_trigger.py), so a long synchronous
    # query here would stall live spike processing across all running sessions.
    sessions = await asyncio.to_thread(list_sessions)
    return [_session_to_response(s) for s in sessions]


@app.post("/api/football/sessions/{session_id}/stop", response_model=FootballSessionResponse)
async def stop_football_session(session_id: int):
    from backend.football.session_manager import stop_session

    session = await stop_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_to_response(session)


@app.get("/api/football/sessions/{session_id}/analysis", response_model=MarketAnalysisResponse)
async def get_football_match_analysis(session_id: int):
    from backend.football.ai_analysis import get_latest_analysis

    cached = get_latest_analysis(session_id)
    if not cached:
        return MarketAnalysisResponse(session_id=session_id)
    return MarketAnalysisResponse(
        session_id=session_id,
        text=cached["text"],
        model=cached["model"],
        timestamp=cached["timestamp"],
        latency_ms=cached["latency_ms"],
    )


@app.get("/api/football/sessions/{session_id}/odds", response_model=OddsComparisonResponse)
async def get_football_odds_comparison(session_id: int):
    from backend.football.odds_comparison import get_latest_odds, is_configured

    if not is_configured():
        return OddsComparisonResponse(session_id=session_id, configured=False)

    cached = get_latest_odds(session_id)
    if not cached:
        return OddsComparisonResponse(session_id=session_id)
    return OddsComparisonResponse(session_id=session_id, **cached)


@app.post("/api/whales/scan", response_model=WhaleScanResponse)
async def run_whale_scan():
    """On-demand scan of the top 20 Polymarket sports traders' open positions
    for consensus bets (3+ of them on the same side of the same market)."""
    from backend.football.whale_tracker import find_consensus_trades

    try:
        result = await find_consensus_trades()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Whale scan failed: {e}")
    return WhaleScanResponse(**result)


@app.get("/api/whales/scan", response_model=WhaleScanResponse)
async def get_whale_scan():
    """Last cached whale-consensus scan, without re-running it."""
    from backend.football.whale_tracker import get_last_scan

    return WhaleScanResponse(**get_last_scan())


@app.post("/api/football/sessions/{session_id}/backtest", response_model=BacktestReportResponse)
async def run_session_backtest(session_id: int, db: Session = Depends(get_db)):
    """Replay the reversion model against this session's real historical
    ESPN events + Polymarket price history. See backend/football/backtest.py's
    module docstring for what this does and does not capture."""
    from backend.football.backtest import run_backtest
    from backend.models.database import FootballSession

    session = db.query(FootballSession).filter(FootballSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.home_team or not session.away_team or not session.yes_token_id:
        raise HTTPException(status_code=400, detail="Session is missing team names or token id")

    match_date = session.created_at.strftime("%Y-%m-%d")
    try:
        result = await run_backtest(session.home_team, session.away_team, session.yes_token_id, match_date)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return BacktestReportResponse(**result)


@app.get("/api/football/risk-status")
async def get_football_risk_status():
    """Portfolio-wide football risk state — shared across all concurrent sessions."""
    from backend.football.risk import get_portfolio_risk

    return get_portfolio_risk().get_status_summary()


@app.get("/api/events", response_model=List[EventResponse])
async def get_events(limit: int = 50):
    from backend.core.scheduler import get_recent_events
    events = get_recent_events(limit)
    return [
        EventResponse(
            timestamp=e["timestamp"],
            type=e["type"],
            message=e["message"],
            data=e.get("data", {})
        )
        for e in events
    ]


# Bot control
@app.post("/api/bot/start")
async def start_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import start_scheduler, log_event, is_scheduler_running

    state = db.query(BotState).first()
    if state:
        state.is_running = True
        db.commit()

    if not is_scheduler_running():
        start_scheduler()

    log_event("success", "Trading bot started")
    return {"status": "started", "is_running": True}


@app.post("/api/bot/stop")
async def stop_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    state = db.query(BotState).first()
    if state:
        state.is_running = False
        db.commit()

    log_event("info", "Trading bot paused")
    return {"status": "stopped", "is_running": False}


@app.post("/api/bot/reset")
async def reset_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    try:
        trades_deleted = db.query(Trade).delete()
        state = db.query(BotState).first()
        if state:
            state.bankroll = settings.INITIAL_BANKROLL
            state.total_trades = 0
            state.winning_trades = 0
            state.total_pnl = 0.0
            state.is_running = True

        ai_logs_deleted = db.query(AILog).delete()
        db.commit()

        log_event("success", f"Bot reset: {trades_deleted} trades deleted. Fresh start with ${settings.INITIAL_BANKROLL:,.2f}")

        return {
            "status": "reset",
            "trades_deleted": trades_deleted,
            "ai_logs_deleted": ai_logs_deleted,
            "new_bankroll": settings.INITIAL_BANKROLL
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Reset failed: {e}")


@app.get("/api/dashboard", response_model=DashboardData)
async def get_dashboard(db: Session = Depends(get_db)):
    """Get all dashboard data in one call."""
    stats = await get_stats(db)

    # Fetch BTC price from microstructure first, fallback to CoinGecko
    btc_price_data = None
    micro_data = None
    try:
        micro = await compute_btc_microstructure()
        if micro:
            micro_data = MicrostructureResponse(
                rsi=micro.rsi,
                momentum_1m=micro.momentum_1m,
                momentum_5m=micro.momentum_5m,
                momentum_15m=micro.momentum_15m,
                vwap_deviation=micro.vwap_deviation,
                sma_crossover=micro.sma_crossover,
                volatility=micro.volatility,
                price=micro.price,
                source=micro.source,
            )
            btc_price_data = BtcPriceResponse(
                price=micro.price,
                change_24h=micro.momentum_15m * 96,  # rough extrapolation
                change_7d=0,
                market_cap=0,
                volume_24h=0,
                last_updated=datetime.utcnow(),
            )
    except Exception:
        pass
    if not btc_price_data:
        try:
            btc = await fetch_crypto_price("BTC")
            if btc:
                btc_price_data = BtcPriceResponse(
                    price=btc.current_price,
                    change_24h=btc.change_24h,
                    change_7d=btc.change_7d,
                    market_cap=btc.market_cap,
                    volume_24h=btc.volume_24h,
                    last_updated=btc.last_updated
                )
        except Exception:
            pass

    # Fetch windows
    windows = []
    try:
        markets = await fetch_active_btc_markets()
        windows = [
            BtcWindowResponse(
                slug=m.slug,
                market_id=m.market_id,
                up_price=m.up_price,
                down_price=m.down_price,
                window_start=m.window_start,
                window_end=m.window_end,
                volume=m.volume,
                is_active=m.is_active,
                is_upcoming=m.is_upcoming,
                time_until_end=m.time_until_end,
                spread=m.spread,
            )
            for m in markets
        ]
    except Exception:
        pass

    # Signals — return ALL signals, mark which are actionable
    signals = []
    try:
        raw_signals = await scan_for_signals()
        signals = [_signal_to_response(s, actionable=s.passes_threshold) for s in raw_signals]
    except Exception:
        pass

    # Football signals (persisted by the session pipeline — read from DB, not live-scanned)
    try:
        football_signals = (
            db.query(Signal)
            .filter(Signal.market_type == "football")
            .order_by(Signal.timestamp.desc())
            .limit(50)
            .all()
        )
        session_ids = {s.football_session_id for s in football_signals if s.football_session_id}
        sessions_by_id = {}
        if session_ids:
            for sess in db.query(FootballSession).filter(FootballSession.id.in_(session_ids)).all():
                sessions_by_id[sess.id] = sess

        for s in football_signals:
            sess = sessions_by_id.get(s.football_session_id)
            title = f"{sess.home_team} vs {sess.away_team}" if sess else s.market_ticker
            signals.append(SignalResponse(
                market_ticker=s.market_ticker,
                market_title=title,
                platform=s.platform or "polymarket",
                direction=s.direction,
                model_probability=s.model_probability,
                market_probability=s.market_price,
                edge=s.edge,
                confidence=s.confidence,
                suggested_size=s.suggested_size or 0.0,
                reasoning=s.reasoning or "",
                timestamp=s.timestamp,
                category="football",
                event_slug=None,
                btc_price=0.0,
                btc_change_24h=0.0,
                window_end=None,
                actionable=False,
            ))
    except Exception:
        pass

    # Recent trades
    trades = db.query(Trade).order_by(Trade.timestamp.desc()).limit(50).all()
    recent_trades = [
        TradeResponse(
            id=t.id,
            market_ticker=t.market_ticker,
            platform=t.platform,
            event_slug=t.event_slug,
            direction=t.direction,
            entry_price=t.entry_price,
            size=t.size,
            timestamp=t.timestamp,
            settled=t.settled,
            result=t.result,
            pnl=t.pnl,
            exit_reason=t.exit_reason,
        )
        for t in trades
    ]

    # Equity curve (BTC-only — see get_equity_curve for why)
    equity_trades = db.query(Trade).filter(Trade.market_type == "btc", Trade.settled == True).order_by(Trade.timestamp).all()
    equity_curve = []
    cumulative_pnl = 0
    for trade in equity_trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            equity_curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": settings.INITIAL_BANKROLL + cumulative_pnl
            })

    # Calibration summary
    calibration = _compute_calibration_summary(db)

    return DashboardData(
        stats=stats,
        btc_price=btc_price_data,
        microstructure=micro_data,
        windows=windows,
        active_signals=signals,
        recent_trades=recent_trades,
        equity_curve=equity_curve,
        calibration=calibration,
    )


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await ws_manager.connect(websocket)

    try:
        await websocket.send_json({
            "timestamp": datetime.utcnow().isoformat(),
            "type": "success",
            "message": "Connected to BTC trading bot"
        })

        from backend.core.scheduler import get_recent_events
        for event in get_recent_events(20):
            await websocket.send_json(event)

        last_event_count = len(get_recent_events(200))
        while True:
            await asyncio.sleep(2)

            current_events = get_recent_events(200)
            if len(current_events) > last_event_count:
                new_events = current_events[last_event_count - len(current_events):]
                for event in new_events:
                    await websocket.send_json(event)
                last_event_count = len(current_events)

            await websocket.send_json({
                "type": "heartbeat",
                "timestamp": datetime.utcnow().isoformat()
            })

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
