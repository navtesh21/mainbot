"""Live football match data: a pluggable source behind a small protocol.

TelegramLiveSource is a stub — the user has not chosen a channel yet. It always
reports itself as stale so CompositeLiveSource falls through to
FlashscoreLiveSource (ported from fball_bot/scraper.py), which is free,
requires no API key, and works today. Swapping TelegramLiveSource's internals
later (public-channel scrape vs. Telethon/Pyrogram private login) requires no
changes downstream — callers only depend on the MatchState/MatchEvent shapes
and the LiveEventsSource protocol.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Tuple

import httpx

from backend.config import settings

logger = logging.getLogger("trading_bot")


@dataclass
class MatchEvent:
    type: str          # goal, own_goal, red_card, yellow_red, var_reversal,
                        # penalty_awarded, penalty_missed, substitution
    minute: int
    team: str           # "home" or "away" (from the home team's perspective)
    player: Optional[str] = None
    home_score: int = 0
    away_score: int = 0
    event_id: str = ""
    detail: str = ""
    is_penalty: bool = False


@dataclass
class MatchState:
    fixture_id: int
    home_team: str
    away_team: str
    status: str         # scheduled, live, halftime, finished
    minute: int
    home_score: int
    away_score: int
    date: str = ""


class LiveEventsSource(Protocol):
    async def get_live_matches(self) -> List[MatchState]: ...
    async def poll_new_events(self, fixture_id: int) -> List[MatchEvent]: ...


class TelegramLiveSource:
    """STUB. Channel not yet chosen by the user.

    Always reports stale so CompositeLiveSource uses Flashscore instead. Once
    a channel is chosen, only this class's internals need to change.
    """

    def __init__(self) -> None:
        self.last_update_at: Optional[float] = None

    @property
    def is_stale(self) -> bool:
        return True

    async def get_live_matches(self) -> List[MatchState]:
        return []

    async def poll_new_events(self, fixture_id: int) -> List[MatchEvent]:
        return []

    async def close(self) -> None:
        pass


FLASHSCORE_BASE = "https://www.flashscore.com"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

TOURNAMENT_ROUTES = {
    "world_cup": ["/football/world/world-cup/", "/football/world/world-championship/"],
    "today": ["/"],
}


def _decode(val: str) -> str:
    """Decode HTML entities in Flashscore values."""
    return val.replace("&amp;", "&").replace("&quot;", '"').replace("&#039;", "'")


def _parse_feed(text: str) -> List[Dict[str, str]]:
    """Parse Flashscore's ¬-delimited key÷value feed format."""
    records = []
    current: Dict[str, str] = {}
    for part in text.split("¬"):
        if part.startswith("~"):
            if current:
                records.append(current)
            current = {}
            part = part[1:]
        if "÷" in part:
            k, v = part.split("÷", 1)
            current[k] = v
    if current:
        records.append(current)
    return records


class FlashscoreLiveSource:
    """Zero-cost live match data via Flashscore feed polling.

    Ported from fball_bot/scraper.py::MatchScraper, adapted to the
    LiveEventsSource protocol. Only emits goal events (Flashscore loads red
    cards/VAR dynamically via JS, not in this static feed).
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._prev_scores: Dict[int, Tuple[int, int]] = {}
        self._kickoff_ts: Dict[int, int] = {}
        self.last_update_at: Optional[float] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15, follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_live_matches(self, source: str = "world_cup") -> List[MatchState]:
        client = await self._get_client()
        routes = TOURNAMENT_ROUTES.get(source, TOURNAMENT_ROUTES["today"])

        for route in routes:
            url = f"{FLASHSCORE_BASE}{route}"
            try:
                r = await client.get(url)
                r.raise_for_status()
            except httpx.HTTPError as e:
                logger.debug(f"Flashscore {url}: {e}")
                continue

            matches = self._parse_fixture_feed(r.text)
            if matches:
                self.last_update_at = time.time()
                logger.info(f"Flashscore: {len(matches)} fixtures from {route}")
                return matches

        logger.warning(f"Flashscore: no fixtures found for source={source}")
        return []

    def _parse_fixture_feed(self, html: str) -> List[MatchState]:
        pattern = r"""cjs\.initialFeeds\["(?:summary-fixtures|summary-results|fixtures)"\]\s*=\s*\{[^}]*data:\s*`([^`]+)`"""
        m = re.search(pattern, html, re.DOTALL)
        if not m:
            return []

        records = _parse_feed(m.group(1))
        now_ts = int(time.time())
        matches: List[MatchState] = []

        for rec in records:
            fs_id = rec.get("AA", "")
            home_team = rec.get("CX", "")
            away_team = rec.get("CY", "") or rec.get("AF", "")
            if not fs_id or not home_team or not away_team:
                continue

            home_score = int(rec.get("AB", "0") or "0")
            away_score = int(rec.get("AC", "0") or "0")
            timestamp = int(rec.get("AD", "0") or "0")

            if timestamp > now_ts + 7200:
                status = "scheduled"
            elif home_score > 0 or away_score > 0 or (0 < timestamp < now_ts):
                status = "live"
            else:
                status = "scheduled"

            fid = abs(hash(f"fs_{fs_id}")) % (10 ** 9)
            self._kickoff_ts[fid] = timestamp

            # Initialize from actual scores, not (0, 0), to avoid phantom
            # goals on the first poll.
            if fid not in self._prev_scores:
                self._prev_scores[fid] = (home_score, away_score)

            minute = max(0, min(90, (now_ts - timestamp) // 60)) if 0 < timestamp < now_ts else 0

            matches.append(MatchState(
                fixture_id=fid,
                home_team=_decode(home_team),
                away_team=_decode(away_team),
                status=status,
                minute=minute,
                home_score=home_score,
                away_score=away_score,
                date=time.strftime("%Y-%m-%d %H:%M", time.gmtime(timestamp)) if timestamp else "",
            ))

        return matches

    def detect_score_changes(self, matches: List[MatchState]) -> Dict[int, List[MatchEvent]]:
        """Compare current scores against previous to detect new goals.

        Must be called immediately after get_live_matches(), before its
        result is used to update _prev_scores for the next cycle.
        """
        result: Dict[int, List[MatchEvent]] = {}
        now = int(time.time())

        for f in matches:
            fid = f.fixture_id
            prev_home, prev_away = self._prev_scores.get(fid, (f.home_score, f.away_score))

            home_diff = f.home_score - prev_home
            away_diff = f.away_score - prev_away
            if home_diff <= 0 and away_diff <= 0:
                continue

            events: List[MatchEvent] = []
            ko_ts = self._kickoff_ts.get(fid, 0)
            est_minute = max(0, min(90, (now - ko_ts) // 60)) if ko_ts and ko_ts < now else 90

            for _ in range(max(0, home_diff)):
                events.append(MatchEvent(
                    type="goal", minute=est_minute, team="home",
                    home_score=f.home_score, away_score=f.away_score,
                    event_id=f"fs_{fid}_{est_minute}_goal_home_{now}",
                ))
            for _ in range(max(0, away_diff)):
                events.append(MatchEvent(
                    type="goal", minute=est_minute, team="away",
                    home_score=f.home_score, away_score=f.away_score,
                    event_id=f"fs_{fid}_{est_minute}_goal_away_{now}",
                ))
            result[fid] = events

        for f in matches:
            self._prev_scores[f.fixture_id] = (f.home_score, f.away_score)

        return result


ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
ESPN_LEAGUE = "fifa.world"  # 2026 FIFA World Cup


def _parse_espn_minute(clock: dict) -> int:
    display = (clock or {}).get("displayValue", "")
    m = re.match(r"(\d+)", display)
    if m:
        return int(m.group(1))
    seconds = (clock or {}).get("value")
    if isinstance(seconds, (int, float)):
        return int(seconds // 60)
    return 0


class ESPNLiveSource:
    """Live World Cup scores/events via ESPN's free, keyless hidden API.

    No API key, no rate-limit budget concerns (unlike API-Football's 100/day),
    and unlike FlashscoreLiveSource it returns structured per-event detail
    (goal/own-goal/red-card, exact minute, team) rather than a raw score diff
    — closer to what FootballReversionModel.update() actually wants.
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._team_sides: Dict[int, Dict[str, str]] = {}  # fixture_id -> {team_id: "home"/"away"}
        self._seen_detail_count: Dict[int, int] = {}
        self._pending_events: Dict[int, List[MatchEvent]] = {}
        self.last_update_at: Optional[float] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_live_matches(self) -> List[MatchState]:
        client = await self._get_client()
        try:
            r = await client.get(ESPN_SCOREBOARD_URL.format(league=ESPN_LEAGUE))
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            logger.debug(f"ESPN scoreboard fetch failed: {e}")
            return []

        matches: List[MatchState] = []
        for event in data.get("events", []):
            try:
                fid = int(event["id"])
            except (KeyError, ValueError, TypeError):
                continue

            comp = (event.get("competitions") or [{}])[0]
            competitors = comp.get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue

            sides = {}
            if home.get("team", {}).get("id"):
                sides[str(home["team"]["id"])] = "home"
            if away.get("team", {}).get("id"):
                sides[str(away["team"]["id"])] = "away"
            self._team_sides[fid] = sides

            status_type = comp.get("status", {}).get("type", {})
            if status_type.get("completed"):
                status = "finished"
            elif status_type.get("state") == "pre":
                status = "scheduled"
            else:
                status = "live"

            try:
                home_score = int(home.get("score", 0) or 0)
                away_score = int(away.get("score", 0) or 0)
            except (TypeError, ValueError):
                home_score, away_score = 0, 0

            status_block = comp.get("status", {})
            minute = _parse_espn_minute({
                "value": status_block.get("clock"),
                "displayValue": status_block.get("displayClock", ""),
            })

            matches.append(MatchState(
                fixture_id=fid,
                home_team=home.get("team", {}).get("displayName", ""),
                away_team=away.get("team", {}).get("displayName", ""),
                status=status,
                minute=minute,
                home_score=home_score,
                away_score=away_score,
                date=event.get("date", ""),
            ))

            self._collect_new_events(fid, comp.get("details") or [], sides, home_score, away_score)

        self.last_update_at = time.time()
        return matches

    def _collect_new_events(
        self, fid: int, details: List[dict], sides: Dict[str, str], home_score: int, away_score: int,
    ) -> None:
        seen = self._seen_detail_count.get(fid, 0)
        new_details = details[seen:]
        if not new_details:
            return
        self._seen_detail_count[fid] = len(details)

        new_events: List[MatchEvent] = []
        for det in new_details:
            team_id = str((det.get("team") or {}).get("id", ""))
            team_side = sides.get(team_id)
            if team_side is None:
                continue

            if det.get("redCard"):
                ev_type = "red_card"
            elif det.get("scoringPlay"):
                ev_type = "goal"  # covers own goals too — `team` already names the credited side
            elif det.get("penaltyKick"):
                ev_type = "penalty"
            else:
                continue  # yellow cards, substitutions, etc. — not modeled, would just add noise

            athletes = det.get("athletesInvolved") or []
            player = athletes[0].get("displayName") if athletes else None

            new_events.append(MatchEvent(
                type=ev_type,
                minute=_parse_espn_minute(det.get("clock")),
                team=team_side,
                player=player,
                home_score=home_score,
                away_score=away_score,
                event_id=f"espn_{fid}_{det.get('clock', {}).get('value')}_{ev_type}",
                detail=(det.get("type") or {}).get("text", ""),
                is_penalty=bool(det.get("penaltyKick")),
            ))

        if new_events:
            # Merge, don't overwrite — same reasoning as CompositeLiveSource's
            # Flashscore path: concurrent sessions may poll this singleton
            # independently before a prior cycle's events are consumed.
            self._pending_events.setdefault(fid, []).extend(new_events)

    async def poll_new_events(self, fixture_id: int) -> List[MatchEvent]:
        return self._pending_events.pop(fixture_id, [])


class CompositeLiveSource:
    """Tries Telegram first, then ESPN (free, accurate, no API key), and
    falls back to Flashscore only if both are unavailable.

    poll_new_events() routes to whichever source last produced
    get_live_matches(), since fixture IDs are only meaningful within the
    source that generated them.
    """

    def __init__(
        self,
        telegram: Optional[TelegramLiveSource] = None,
        flashscore: Optional[FlashscoreLiveSource] = None,
        espn: Optional[ESPNLiveSource] = None,
        stale_seconds: Optional[int] = None,
    ) -> None:
        self.telegram = telegram or TelegramLiveSource()
        self.flashscore = flashscore or FlashscoreLiveSource()
        self.espn = espn or ESPNLiveSource()
        self.stale_seconds = stale_seconds if stale_seconds is not None else settings.FOOTBALL_LIVE_SOURCE_STALE_SECONDS
        self._active_source: str = "espn"
        self._last_events_by_fixture: Dict[int, List[MatchEvent]] = {}

    async def close(self) -> None:
        await self.telegram.close()
        await self.flashscore.close()
        await self.espn.close()

    def _telegram_is_fresh(self) -> bool:
        if self.telegram.is_stale:
            return False
        if self.telegram.last_update_at is None:
            return False
        return (time.time() - self.telegram.last_update_at) < self.stale_seconds

    async def get_live_matches(self) -> List[MatchState]:
        if self._telegram_is_fresh():
            matches = await self.telegram.get_live_matches()
            if matches:
                self._active_source = "telegram"
                return matches

        espn_matches = await self.espn.get_live_matches()
        if espn_matches:
            self._active_source = "espn"
            return espn_matches

        logger.debug("Telegram/ESPN unavailable, falling back to Flashscore")
        self._active_source = "flashscore"
        matches = await self.flashscore.get_live_matches()
        # Merge (don't overwrite): with N concurrent football sessions each
        # calling get_live_matches() independently on their own ~15s timer
        # against this shared singleton, a wholesale overwrite here could
        # discard another fixture's not-yet-consumed event before its session
        # calls poll_new_events() to pop it.
        new_events = self.flashscore.detect_score_changes(matches)
        for fid, events in new_events.items():
            if events:
                self._last_events_by_fixture.setdefault(fid, []).extend(events)
        return matches

    async def poll_new_events(self, fixture_id: int) -> List[MatchEvent]:
        if self._active_source == "telegram":
            return await self.telegram.poll_new_events(fixture_id)
        if self._active_source == "espn":
            return await self.espn.poll_new_events(fixture_id)
        return self._last_events_by_fixture.pop(fixture_id, [])


_composite_source: Optional[CompositeLiveSource] = None


def get_live_source() -> CompositeLiveSource:
    global _composite_source
    if _composite_source is None:
        _composite_source = CompositeLiveSource()
    return _composite_source
