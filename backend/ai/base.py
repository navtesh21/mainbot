"""Base classes and types for AI integration."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum


class AIProvider(str, Enum):
    """Supported AI providers."""
    CLAUDE = "claude"
    GROQ = "groq"
    GEMINI = "gemini"


@dataclass
class AIAnalysis:
    """Result of AI analysis on a market or signal."""
    reasoning: str
    confidence: float  # 0-1
    recommendation: Optional[str] = None
    risk_factors: List[str] = field(default_factory=list)
    raw_response: str = ""
    model_used: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    tokens_used: int = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "recommendation": self.recommendation,
            "risk_factors": self.risk_factors,
            "model_used": self.model_used,
            "provider": self.provider,
            "latency_ms": self.latency_ms,
            "tokens_used": self.tokens_used,
            "timestamp": self.timestamp.isoformat()
        }


@dataclass
class AnomalyReport:
    """Report of detected market anomaly."""
    market_ticker: str
    anomaly_type: str  # "price_spike", "volume_anomaly", "spread_unusual"
    severity: str  # "low", "medium", "high"
    description: str
    detected_at: datetime = field(default_factory=datetime.utcnow)
    ai_analysis: Optional[str] = None


@dataclass
class TradeRecommendation:
    """AI-generated trade recommendation."""
    signal_ticker: str
    should_trade: bool
    recommended_size: Optional[float] = None
    reasoning: str = ""
    risk_assessment: str = ""
    confidence: float = 0.5
    caveats: List[str] = field(default_factory=list)


class BaseAIClient(ABC):
    """Abstract base class for AI clients."""

    @abstractmethod
    async def analyze_signal(
        self,
        signal_data: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> AIAnalysis:
        """Analyze a trading signal and provide reasoning."""
        pass

    @abstractmethod
    async def classify_market(
        self,
        title: str,
        description: str = ""
    ) -> tuple[str, float]:
        """Classify a market into a category."""
        pass

    @abstractmethod
    async def detect_anomalies(
        self,
        markets: List[Dict[str, Any]]
    ) -> List[AnomalyReport]:
        """Detect anomalies in market data."""
        pass


def create_signal_prompt(signal_data: Dict[str, Any], context: Dict[str, Any] = None) -> str:
    """Create a prompt for signal analysis."""
    prompt = f"""Analyze this prediction market trading signal:

Market: {signal_data.get('market_title', 'Unknown')}
Platform: {signal_data.get('platform', 'Unknown')}
Category: {signal_data.get('category', 'Unknown')}

Model Probability: {signal_data.get('model_probability', 0):.1%}
Market Price: {signal_data.get('market_probability', 0):.1%}
Edge: {signal_data.get('edge', 0):.1%}
Suggested Size: ${signal_data.get('suggested_size', 0):.2f}

Direction: {signal_data.get('direction', 'Unknown').upper()}
"""

    if context:
        if 'weather_data' in context:
            wd = context['weather_data']
            prompt += f"""
Weather Context:
- High Temperature Forecast: {wd.get('high_temp', 'N/A')}°F
- Ensemble Agreement: {wd.get('confidence', 0):.0%}
- Number of Models: {wd.get('ensemble_count', 'N/A')}
"""
        if 'crypto_data' in context:
            cd = context['crypto_data']
            prompt += f"""
Crypto Context:
- Current Price: ${cd.get('current_price', 'N/A'):,.2f}
- 24h Change: {cd.get('change_24h', 0):.1%}
- Market Cap: ${cd.get('market_cap', 0):,.0f}
"""

    prompt += """
Provide a brief analysis (2-3 sentences) covering:
1. Why this edge might exist
2. Key risk factors
3. Confidence in the model's probability estimate

Be concise and actionable."""

    return prompt


def create_match_analysis_prompt(data: Dict[str, Any]) -> str:
    """Create a prompt for a live football-match market analysis (order book + match state)."""
    book = data.get("order_book", {})
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    bid_lines = "\n".join(f"  {b['price']:.3f} x {b['size']:.0f}" for b in bids) or "  (none)"
    ask_lines = "\n".join(f"  {a['price']:.3f} x {a['size']:.0f}" for a in asks) or "  (none)"

    open_trades = data.get("open_trades", [])
    trades_summary = "\n".join(
        f"  - {t['side']} ${t['size']:.2f} @ {t['entry_price']:.3f} (opened {t['minutes_ago']:.0f}m ago)"
        for t in open_trades
    ) or "  (none)"

    odds = data.get("sportsbook_odds")
    odds_block = (
        f"\nSportsbook consensus ({odds['bookmaker_count']} books): {odds['sportsbook_prob']:.1%} implied "
        f"for {data.get('home_team', 'home')} vs Polymarket's {odds['polymarket_prob']:.1%} (edge {odds['edge']:+.1%})\n"
        if odds else ""
    )

    return f"""You are a sports-prediction-market analyst. Give a concise but detailed read on this live market.

Match: {data.get('home_team', '?')} vs {data.get('away_team', '?')}
Minute: {data.get('minute', '?')}'  Score: {data.get('home_score', 0)}-{data.get('away_score', 0)}  Status: {data.get('match_status', 'unknown')}

Polymarket YES price (home win): {data.get('yes_price', 0):.3f}
Pre-match baseline price: {data.get('baseline_price', 0):.3f}
Price drift since kickoff: {data.get('price_drift', 0):+.3f}
Spread: {data.get('spread', 0):.3f}
Total order-book depth: ${data.get('depth_usd', 0):,.0f}
{odds_block}
Order book (YES token):
Bids:
{bid_lines}
Asks:
{ask_lines}

Bot's open paper positions this match:
{trades_summary}

Paper-trading capital: ${data.get('capital', 0):.2f} (started at ${data.get('initial_capital', 0):.2f}), {data.get('total_trades', 0)} trades so far, {data.get('win_rate', 0):.0f}% win rate.

In 3-5 sentences: assess whether the current price looks fair, overreacting, or under-reacting given the match state (factor in the sportsbook consensus above if present — a meaningful gap from it is itself a signal); note any liquidity/spread concerns for trading at this size; and flag anything actionable for a mean-reversion scalper. Be concrete and reference the actual numbers above. Do not give financial advice disclaimers."""


def create_classification_prompt(title: str, description: str = "") -> str:
    """Create a prompt for market classification."""
    return f"""Classify this prediction market into one category:

Title: {title}
Description: {description or 'N/A'}

Categories:
- weather: Temperature, precipitation, climate events
- crypto: Cryptocurrency prices, blockchain events
- politics: Elections, legislation, government actions
- economics: Inflation, GDP, employment, Fed decisions
- sports: Any sports-related markets (games, scores, tournaments)
- other: Doesn't fit above categories

Respond with just the category name (lowercase) and confidence (0-100).
Format: category,confidence

Example: crypto,85"""
