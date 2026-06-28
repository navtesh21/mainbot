"""Configuration settings for the BTC 5-min trading bot."""
import os
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database (SQLite for Phase 1, PostgreSQL for production)
    DATABASE_URL: str = "sqlite:///./tradingbot.db"

    # API Keys (optional)
    POLYMARKET_API_KEY: Optional[str] = None

    # AI API Keys
    GROQ_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None

    # AI Model Configuration
    GROQ_MODEL: str = "llama-3.1-8b-instant"
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # AI Feature Flags
    AI_LOG_ALL_CALLS: bool = True
    AI_DAILY_BUDGET_USD: float = 1.0

    # Bot settings - BTC 5-MIN TRADING
    BTC_ENABLED: bool = True  # set False to pause BTC scanning/trading/heartbeat entirely
    SIMULATION_MODE: bool = True
    INITIAL_BANKROLL: float = 10000.0
    KELLY_FRACTION: float = 0.15  # Fractional Kelly

    # BTC 5-min specific settings
    SCAN_INTERVAL_SECONDS: int = 60  # Scan every minute
    SETTLEMENT_INTERVAL_SECONDS: int = 120  # Check settlements every 2 min
    BTC_PRICE_SOURCE: str = "coinbase"
    MIN_EDGE_THRESHOLD: float = 0.02  # 2% edge required — these are 50/50 markets
    MAX_ENTRY_PRICE: float = 0.55  # Enter up to 55c
    MAX_TRADES_PER_WINDOW: int = 1
    MAX_TOTAL_PENDING_TRADES: int = 20

    # Risk management
    DAILY_LOSS_LIMIT: float = 300.0
    MAX_TRADE_SIZE: float = 75.0
    MIN_TIME_REMAINING: int = 60  # Don't trade windows closing in < 60s
    MAX_TIME_REMAINING: int = 1800  # Trade windows up to 30min out

    # Indicator weights for composite signal (must sum to ~1.0)
    WEIGHT_RSI: float = 0.20
    WEIGHT_MOMENTUM: float = 0.35
    WEIGHT_VWAP: float = 0.20
    WEIGHT_SMA: float = 0.15
    WEIGHT_MARKET_SKEW: float = 0.10

    # Volume filter
    MIN_MARKET_VOLUME: float = 100.0  # Low volume for 5-min markets

    # Football (World Cup) — fixture discovery, gated off until ready
    FOOTBALL_ENABLED: bool = False
    FOOTBALL_DATA_API_KEY: Optional[str] = None  # football-data.org (fixture calendar)
    FOOTBALL_API_KEY: Optional[str] = None  # api-football.com — reserved, not yet wired up
    FOOTBALL_DATA_COMPETITION: str = "WC"
    FOOTBALL_SCHEDULE_REFRESH_SECONDS: int = 43200  # 12h
    FOOTBALL_LIVE_SOURCE_STALE_SECONDS: int = 30

    # Sportsbook odds comparison (the-odds-api.com) — free tier covers all
    # sports incl. soccer_fifa_world_cup, ~500 credits/month. Get a free key
    # at https://the-odds-api.com/ and set it here; feature no-ops without it.
    ODDS_API_KEY: Optional[str] = None
    ODDS_POLL_SECONDS: int = 600  # 10 min — budget-conscious given the free tier's monthly cap

    # Football real order execution — kill switch, defaults OFF.
    # Flipping this true requires a funded wallet and WALLET_PRIVATE_KEY set;
    # do not enable without having explicitly confirmed both.
    FOOTBALL_TRADING_ENABLED: bool = False
    WALLET_PRIVATE_KEY: Optional[str] = None
    FOOTBALL_INITIAL_CAPITAL: float = 10.0
    FOOTBALL_RISK_MAX_PER_TRADE_USD: float = 3.0

    # Crypto scalping — BTC 5m/15m Up/Down momentum scalp on Polymarket,
    # gated off until ready. Separate risk ledger from football's; reuses
    # the same WALLET_PRIVATE_KEY (any funded Polygon wallet, not football-specific)
    # and the existing WEIGHT_*/KELLY_FRACTION signal globals.
    CRYPTO_ENABLED: bool = False
    CRYPTO_SCAN_INTERVAL_SECONDS: int = 12

    # Entry gating — deliberately separate from MIN_EDGE_THRESHOLD/MAX_ENTRY_PRICE/
    # MIN_TIME_REMAINING above, which govern the old dormant hold-to-settlement
    # BTC strategy and must stay untouched.
    CRYPTO_MIN_EDGE_THRESHOLD: float = 0.02
    CRYPTO_MAX_ENTRY_PRICE: float = 0.55
    CRYPTO_MIN_CONFIDENCE: float = 0.35
    CRYPTO_MIN_LIQUIDITY_USD: float = 20.0
    CRYPTO_MIN_TIME_REMAINING_5M: int = 30
    CRYPTO_MIN_TIME_REMAINING_15M: int = 60

    # Exit timeouts — window-length-aware (see backend/crypto/scalping.py)
    CRYPTO_TIMEOUT_5M_SECONDS: int = 75
    CRYPTO_TIMEOUT_15M_SECONDS: int = 150

    # Risk / capital — own ledger, own state file, isolated from football's
    CRYPTO_INITIAL_CAPITAL: float = 10.0
    CRYPTO_RISK_MAX_PER_TRADE_USD: float = 3.0

    # Crypto real order execution — kill switch, defaults OFF, same semantics
    # as FOOTBALL_TRADING_ENABLED. Flipping this true requires a funded wallet
    # and WALLET_PRIVATE_KEY set; do not enable without having explicitly
    # confirmed both.
    CRYPTO_TRADING_ENABLED: bool = False

    # When True (dry-run only), skip the RRGuard EV/R:R gate and edge/confidence
    # thresholds so ANY signal fires a dry-run trade — for model observation and
    # calibration. Never enable alongside CRYPTO_TRADING_ENABLED=true.
    CRYPTO_UNRESTRICTED: bool = False

    class Config:
        env_file = ".env"


settings = Settings()
