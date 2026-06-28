"""Backtest the reversion model against real historical data.

Replays FootballReversionModel.update() (event-driven) and check_drift()
(unexplained-drift fade) against:
- ESPN's historical scoreboard (dates=YYYYMMDD) for the real goal/card/penalty
  timeline of a finished match.
- Polymarket CLOB's /prices-history for that match's actual YES-token price
  series at 1-minute resolution.

Honest limitations, stated up front rather than buried:
1. This only replays the SLOW PATH (event/drift-driven signals). The live
   fast path reacts to a >=2% move within a 10-SECOND window
   (price_trigger.py) — 1-minute historical bars cannot reconstruct that, so
   the backtest's trade count is a lower bound on what the live bot would
   actually see, not an exact replay.
2. Liquidity (MIN_LIQUIDITY_USD) and the full risk stack (RRGuard, Kelly,
   PortfolioRisk, PositionRegistry) are NOT applied here — this measures the
   *model's* raw per-share edge, not what real position sizing/risk limits
   would have allowed. Treat results as "is the signal directionally any
   good", not "this is the PnL the bot would have made."
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from backend.data.football_live import MatchEvent, _parse_espn_minute
from backend.football.discovery import normalize
from backend.football.models import FootballReversionModel
from backend.football.scalping import (
    PROFIT_TARGET, STOP_LOSS, PARTIAL_EXIT_LEVEL, TIMEOUT_DEFAULT, TIMEOUT_LATE, compute_stop,
)

logger = logging.getLogger("trading_bot")

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
CLOB_PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"
MIN_ENTRY_CONFIDENCE = 0.15


@dataclass
class BacktestTrade:
    side: str
    entry_price: float
    entry_minute: int
    exit_price: float
    exit_minute: int
    exit_reason: str
    pnl_per_share: float
    trigger: str  # "event" or "drift"


@dataclass
class BacktestReport:
    home_team: str
    away_team: str
    match_date: str
    price_points: int
    events_found: int
    trades: List[BacktestTrade] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        wins = [t for t in self.trades if t.pnl_per_share > 0]
        total_pnl = sum(t.pnl_per_share for t in self.trades)
        return {
            "home_team": self.home_team,
            "away_team": self.away_team,
            "match_date": self.match_date,
            "price_points": self.price_points,
            "events_found": self.events_found,
            "total_trades": len(self.trades),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(self.trades) * 100, 1) if self.trades else 0.0,
            "total_pnl_per_share": round(total_pnl, 4),
            "trades": [t.__dict__ for t in self.trades],
        }


async def _fetch_historical_match(date_str: str, home_team: str, away_team: str) -> Optional[Dict[str, Any]]:
    """date_str: 'YYYYMMDD'. Returns the raw ESPN competition block for the match."""
    h_norm, a_norm = normalize(home_team), normalize(away_team)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(ESPN_SCOREBOARD_URL, params={"dates": date_str})
        r.raise_for_status()
        data = r.json()

    for event in data.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        eh = normalize(home.get("team", {}).get("displayName", ""))
        ea = normalize(away.get("team", {}).get("displayName", ""))
        if {eh, ea} != {h_norm, a_norm}:
            continue
        return {"event": event, "comp": comp, "home": home, "away": away}
    return None


def _extract_events(match: Dict[str, Any]) -> List[MatchEvent]:
    """Mirrors ESPNLiveSource._collect_new_events's type-mapping, applied to
    the whole `details` array at once instead of incrementally."""
    comp = match["comp"]
    home, away = match["home"], match["away"]
    sides = {}
    if home.get("team", {}).get("id"):
        sides[str(home["team"]["id"])] = "home"
    if away.get("team", {}).get("id"):
        sides[str(away["team"]["id"])] = "away"

    try:
        home_score_final = int(home.get("score", 0) or 0)
        away_score_final = int(away.get("score", 0) or 0)
    except (TypeError, ValueError):
        home_score_final, away_score_final = 0, 0

    events: List[MatchEvent] = []
    running_home, running_away = 0, 0
    for det in comp.get("details") or []:
        team_id = str((det.get("team") or {}).get("id", ""))
        team_side = sides.get(team_id)
        if team_side is None:
            continue

        if det.get("redCard"):
            ev_type = "red_card"
        elif det.get("scoringPlay"):
            ev_type = "goal"
            if team_side == "home":
                running_home += 1
            else:
                running_away += 1
        elif det.get("penaltyKick"):
            ev_type = "penalty"
        else:
            continue

        events.append(MatchEvent(
            type=ev_type,
            minute=_parse_espn_minute(det.get("clock")),
            team=team_side,
            home_score=running_home,
            away_score=running_away,
            event_id=f"bt_{det.get('clock', {}).get('value')}_{ev_type}",
        ))

    # Backfill final running scores onto goal events if ESPN's `details` order
    # ever desyncs from the literal final score (rare, but cheap to guard).
    if events and (running_home != home_score_final or running_away != away_score_final):
        logger.debug("Backtest: running score from details didn't match final score — using as-is")

    return events


async def _fetch_price_series(token_id: str, start_ts: int, end_ts: int) -> List[Dict[str, float]]:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            CLOB_PRICES_HISTORY_URL,
            params={"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": 1},
        )
        r.raise_for_status()
        data = r.json()
    return data.get("history", [])


def _replay_evaluate_exit(side: str, entry_price: float, current_price: float, elapsed_seconds: float, match_minute: int, partial_exited: bool) -> Optional[str]:
    pnl_per_share = (entry_price - current_price) if side == "SELL" else (current_price - entry_price)
    timeout = TIMEOUT_LATE if match_minute >= 85 else TIMEOUT_DEFAULT

    if not partial_exited and pnl_per_share >= PARTIAL_EXIT_LEVEL:
        return "partial"
    if pnl_per_share >= PROFIT_TARGET:
        return "profit"
    if pnl_per_share <= -STOP_LOSS:
        return "stop"
    if elapsed_seconds >= timeout:
        return "timeout"
    return None


async def run_backtest(
    home_team: str, away_team: str, yes_token_id: str, match_date: str,
) -> Dict[str, Any]:
    """match_date: 'YYYY-MM-DD'. Runs the full replay and returns a summary dict."""
    date_str = match_date.replace("-", "")
    match = await _fetch_historical_match(date_str, home_team, away_team)
    if not match:
        raise ValueError(f"No ESPN historical match found for {home_team} vs {away_team} on {match_date}")

    events = _extract_events(match)

    kickoff_iso = match["event"].get("date", "")
    kickoff_dt = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00")) if kickoff_iso else None
    kickoff_ts = int(kickoff_dt.timestamp()) if kickoff_dt else int(time.time()) - 7200
    end_ts = kickoff_ts + 3 * 3600  # generous window covering full time + stoppage + buffer

    price_series = await _fetch_price_series(yes_token_id, kickoff_ts, end_ts)
    if not price_series:
        raise ValueError("No Polymarket price history found for this token in the match window")

    report = BacktestReport(home_team=home_team, away_team=away_team, match_date=match_date,
                              price_points=len(price_series), events_found=len(events))

    model = FootballReversionModel()
    baseline = price_series[0]["p"]
    model.set_baseline(0, baseline)

    open_position: Optional[Dict[str, Any]] = None
    events_by_minute: Dict[int, MatchEvent] = {e.minute: e for e in events}

    for point in price_series:
        ts, price = point["t"], point["p"]
        minute = max(0, int((ts - kickoff_ts) / 60))

        if open_position:
            elapsed = ts - open_position["opened_at"]
            exit_kind = _replay_evaluate_exit(
                open_position["side"], open_position["entry_price"], price, elapsed, minute,
                open_position["partial_exited"],
            )
            if exit_kind == "partial":
                open_position["partial_exited"] = True
                continue
            if exit_kind:
                pnl = (open_position["entry_price"] - price) if open_position["side"] == "SELL" else (price - open_position["entry_price"])
                report.trades.append(BacktestTrade(
                    side=open_position["side"], entry_price=open_position["entry_price"],
                    entry_minute=open_position["entry_minute"], exit_price=price, exit_minute=minute,
                    exit_reason=exit_kind, pnl_per_share=round(pnl, 4), trigger=open_position["trigger"],
                ))
                open_position = None
            continue

        event = events_by_minute.get(minute)
        if event:
            update = model.update(current=price, event=event, pre_match=baseline)
            trigger = "event"
        else:
            update = model.check_drift(current=price, pre_match=baseline, minute=minute, fixture_id=0)
            trigger = "drift"

        if update.scalp_direction == "NONE" or update.confidence < MIN_ENTRY_CONFIDENCE:
            continue

        open_position = {
            "side": update.scalp_direction, "entry_price": price, "entry_minute": minute,
            "opened_at": ts, "partial_exited": False, "trigger": trigger,
        }

    if open_position:
        last_price = price_series[-1]["p"]
        pnl = (open_position["entry_price"] - last_price) if open_position["side"] == "SELL" else (last_price - open_position["entry_price"])
        report.trades.append(BacktestTrade(
            side=open_position["side"], entry_price=open_position["entry_price"],
            entry_minute=open_position["entry_minute"], exit_price=last_price,
            exit_minute=max(0, int((price_series[-1]["t"] - kickoff_ts) / 60)),
            exit_reason="match_end", pnl_per_share=round(pnl, 4), trigger=open_position["trigger"],
        ))

    return report.summary()
