"""World Cup fixture calendar — fetched from football-data.org.

Low-frequency, low-value data path: the WC calendar barely changes, so it is
fetched at most a couple of times a day and cached to disk. Live scores/events
come from a separate, latency-sensitive source (see football_live.py).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, List

import httpx

from backend.config import settings

logger = logging.getLogger("trading_bot")

CACHE_FILE = os.path.join("backend", "data", "_cache", "football_schedule_cache.json")


@dataclass
class ScheduledMatch:
    """A World Cup fixture from the calendar (not live state)."""
    home_team: str
    away_team: str
    utc_kickoff: str   # ISO 8601, e.g. 2026-06-13T19:00:00Z
    status: str        # SCHEDULED, LIVE, IN_PLAY, PAUSED, FINISHED
    matchday: int = 0
    source_id: int = 0  # football-data.org match id


class ScheduleService:
    """Fetches and caches the World Cup schedule from football-data.org."""

    BASE = "https://api.football-data.org/v4"

    def __init__(self) -> None:
        self._api_key = settings.FOOTBALL_DATA_API_KEY
        self._competition = settings.FOOTBALL_DATA_COMPETITION
        self._refresh_interval = settings.FOOTBALL_SCHEDULE_REFRESH_SECONDS
        self._client = httpx.AsyncClient(timeout=15)
        self._matches: List[ScheduledMatch] = []
        self._last_refresh: float = 0.0
        self._load_cache()

    async def close(self) -> None:
        await self._client.aclose()

    def _load_cache(self) -> None:
        if not os.path.exists(CACHE_FILE):
            return
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            self._matches = [ScheduledMatch(**m) for m in data.get("matches", [])]
            self._last_refresh = float(data.get("fetched_at", 0.0))
            logger.info(f"Loaded {len(self._matches)} World Cup fixtures from schedule cache")
        except Exception as e:
            logger.debug(f"Failed to load schedule cache: {e}")

    def _save_cache(self) -> None:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump(
                    {
                        "fetched_at": self._last_refresh,
                        "matches": [m.__dict__ for m in self._matches],
                    },
                    f,
                )
        except Exception as e:
            logger.debug(f"Failed to save schedule cache: {e}")

    async def get_schedule(self, force: bool = False) -> List[ScheduledMatch]:
        """Return the WC schedule, refreshing from the API only when stale."""
        now = time.time()
        fresh = (now - self._last_refresh) < self._refresh_interval
        if self._matches and fresh and not force:
            return self._matches
        if not self._api_key:
            logger.warning("FOOTBALL_DATA_API_KEY not set — using cached schedule only")
            return self._matches
        await self._refresh()
        return self._matches

    async def _refresh(self) -> None:
        url = f"{self.BASE}/competitions/{self._competition}/matches"
        headers = {"X-Auth-Token": self._api_key}
        try:
            r = await self._client.get(url, headers=headers)
            r.raise_for_status()
            payload: dict[str, Any] = r.json()
        except Exception as e:
            logger.warning(f"Schedule refresh failed ({e}) — keeping cached schedule")
            return

        matches: List[ScheduledMatch] = []
        for item in payload.get("matches", []):
            home = (item.get("homeTeam", {}) or {}).get("name", "") or ""
            away = (item.get("awayTeam", {}) or {}).get("name", "") or ""
            if not home or not away:
                continue  # TBD fixtures (knockout slots not yet decided)
            matches.append(ScheduledMatch(
                home_team=home,
                away_team=away,
                utc_kickoff=item.get("utcDate", "") or "",
                status=item.get("status", "") or "",
                matchday=int(item.get("matchday", 0) or 0),
                source_id=int(item.get("id", 0) or 0),
            ))

        if matches:
            self._matches = matches
            self._last_refresh = time.time()
            self._save_cache()
            logger.info(f"Refreshed World Cup schedule: {len(matches)} fixtures")

    def matches_on(self, date_yyyy_mm_dd: str) -> List[ScheduledMatch]:
        """Scheduled matches whose UTC kickoff falls on the given date."""
        return [m for m in self._matches if m.utc_kickoff[:10] == date_yyyy_mm_dd]


_schedule_service: ScheduleService | None = None


def get_schedule_service() -> ScheduleService:
    global _schedule_service
    if _schedule_service is None:
        _schedule_service = ScheduleService()
    return _schedule_service
