"""Link -> market -> fixture resolution for the "paste a Polymarket link" flow.

Team-name matching ported from fball_bot/discovery.py's extract_teams()/
normalize()/TEAM_ALIASES verbatim, generalized to match against
football-data.org/Telegram fixtures (backend/data/football_schedule.py,
backend/data/football_live.py) instead of API-Football's.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlparse

from backend.football.pm_client import GammaClient

logger = logging.getLogger("trading_bot")

TEAM_ALIASES: dict[str, str] = {
    "usa": "united states", "united states": "usa",
    "england": "england", "uk": "england",
    "korea republic": "south korea", "south korea": "south korea",
    "ivory coast": "cote d'ivoire",
    "bosnia and herzegovina": "bosnia & herzegovina",
    "bosnia-herzegovina": "bosnia & herzegovina",
    "dr congo": "congo dr", "drc": "congo dr",
}


def normalize(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[^a-z0-9\s]", "", n).strip()
    return TEAM_ALIASES.get(n, n)


def extract_teams(question: str) -> Optional[Tuple[str, str]]:
    q = question.replace("?", "").replace(".", "").lower()

    m = re.search(r"will\s+(.+?)\s+win\s+on\s+", q)
    if m:
        return m.group(1).strip().title(), ""

    m = re.search(r"(?:between\s+)?(.+?)\s+(?:vs\.?|beat|defeat|against|v\s)\s+(.+)", q)
    if m:
        return m.group(1).strip().title(), m.group(2).strip().title()
    m = re.search(r"will\s+(.+?)\s+(?:win|beat|defeat)\s+(.+)", q)
    if m:
        return m.group(1).strip().title(), m.group(2).strip().title()
    return None


def extract_slug_from_link(link: str) -> str:
    """Pull the event slug out of a Polymarket URL, or pass through a bare slug."""
    link = link.strip()
    if "polymarket.com" not in link:
        return link  # assume the user pasted a bare slug

    parsed = urlparse(link)
    parts = [p for p in parsed.path.split("/") if p]
    if "event" in parts:
        idx = parts.index("event")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[-1] if parts else link


@dataclass
class ResolvedMarket:
    polymarket_slug: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    question: str
    home_team: str
    away_team: str


async def resolve_link(link: str) -> Optional[ResolvedMarket]:
    """Resolve a pasted Polymarket link/slug to a market with extracted team names."""
    slug = extract_slug_from_link(link)
    client = GammaClient()
    try:
        events = await client.get_event_by_slug(slug)
    finally:
        await client.close()

    if not events:
        return None

    for ev in events:
        # The event title is consistently "Team A vs. Team B" for World Cup
        # fixtures and is far more reliable than a sub-market's question,
        # since an event often has several sub-markets (team A win / team B
        # win / draw) whose questions don't all name both teams.
        event_teams = extract_teams(ev.get("title", "") or "")

        markets = ev.get("markets", [])
        chosen_gm = None
        chosen_question = ""
        for gm in markets:
            cid = gm.get("conditionId") or gm.get("condition_id", "")
            if not cid:
                continue
            question = (gm.get("question", "") or gm.get("title", "")).lower()
            if "draw" not in question:
                chosen_gm = gm
                chosen_question = gm.get("question", "") or gm.get("title", "")
                break
        if chosen_gm is None and markets:
            chosen_gm = markets[0]
            chosen_question = chosen_gm.get("question", "") or chosen_gm.get("title", "")

        if chosen_gm is None:
            continue

        cid = chosen_gm.get("conditionId") or chosen_gm.get("condition_id", "")
        if not cid:
            continue
        yes_tok, no_tok = GammaClient.extract_tokens(chosen_gm)

        home_team, away_team = event_teams or extract_teams(chosen_question) or ("", "")

        return ResolvedMarket(
            polymarket_slug=slug,
            condition_id=cid,
            yes_token_id=yes_tok,
            no_token_id=no_tok,
            question=chosen_question or ev.get("title", ""),
            home_team=home_team,
            away_team=away_team,
        )

    return None


def match_fixture_ref(home_team: str, away_team: str, candidates: list[tuple[str, str, str]]) -> Optional[str]:
    """Match (home_team, away_team) against a list of (fixture_ref, home, away) candidates.

    Returns the matched fixture_ref, or None if no candidate matches both teams.
    """
    t1, t2 = normalize(home_team), normalize(away_team)
    if not t1 and not t2:
        return None

    for fixture_ref, cand_home, cand_away in candidates:
        h, a = normalize(cand_home), normalize(cand_away)
        if t2:
            if (t1 == h and t2 == a) or (t1 == a and t2 == h):
                return fixture_ref
        elif t1:
            if t1 == h or t1 == a:
                return fixture_ref
    return None
