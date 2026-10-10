"""Live NBA data from ESPN's free public JSON feeds.

Rosters (with real contract numbers) come from the team roster endpoint; season stats for every
player come from ESPN's bulk "by athlete" stats endpoint. Both are free and need no key.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from . import cba
from .ratings import rate_league

log = logging.getLogger("rebuild.data")

ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster"
# Fallbacks if the main roster endpoint refuses us (same data, different ESPN routes).
ROSTER_FALLBACKS = [
    "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster",
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}?enable=roster",
]
STATS_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byathlete"
# ESPN rejects requests that don't look like a normal browser (same headers PC Pick Em' uses).
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

STAT_KEYS = {
    "gamesPlayed": "gp", "avgMinutes": "mpg", "avgRebounds": "reb", "avgFouls": "pf",
    "avgPoints": "pts", "avgFieldGoalsMade": "fgm", "avgFieldGoalsAttempted": "fga",
    "avgThreePointFieldGoalsMade": "tpm", "avgThreePointFieldGoalsAttempted": "tpa",
    "avgFreeThrowsMade": "ftm", "avgFreeThrowsAttempted": "fta", "avgAssists": "ast",
    "avgTurnovers": "tov", "avgSteals": "stl", "avgBlocks": "blk",
}


# ------------------------------------------------------------------------------------------
# Parsing (pure functions, tested without the network)
# ------------------------------------------------------------------------------------------

def _athletes(roster_json: dict) -> list[dict]:
    items = roster_json.get("athletes") or []
    out: list[dict] = []
    for a in items:
        if isinstance(a, dict) and "items" in a and "id" not in a:  # grouped by position
            out.extend(a.get("items") or [])
        elif isinstance(a, dict):
            out.append(a)
    return out


def _salary_schedule(a: dict, season: int) -> list[int]:
    """This season's salary followed by each remaining year (ESPN 'contracts' + 'contract')."""
    contract = a.get("contract") or {}
    by_year: dict[int, int] = {}
    for c in a.get("contracts") or []:
        try:
            yr = int((c.get("season") or {}).get("year"))
            sal = int(c.get("salary") or 0)
        except (TypeError, ValueError):
            continue
        if yr >= season and sal > 0:
            by_year[yr] = sal
    current = int(contract.get("salary") or 0) or by_year.get(season, 0)
    years_left = int(contract.get("yearsRemaining") or 0)
    if current <= 0:
        return []
    n = years_left if years_left > 0 else 1 + sum(1 for y in by_year if y > season)
    sched = [current]
    for i in range(1, min(n, 6)):
        sched.append(by_year.get(season + i) or int(sched[-1] * 1.05))
    return sched


def _position(espn_pos: str, s: dict | None) -> str:
    p = (espn_pos or "").upper()
    if p in ("PG", "SG", "SF", "PF", "C"):
        return p
    s = s or {}
    ast, reb, blk, pts = s.get("ast", 0), s.get("reb", 0), s.get("blk", 0), s.get("pts", 0) or 1
    if p in ("G", "G-F"):
        return "PG" if ast >= 5 or ast / pts > 0.33 else ("SF" if p == "G-F" and reb >= 5 else "SG")
    if p in ("F", "F-G"):
        return "PF" if reb >= 6.5 or blk >= 0.9 else "SF"
    if p in ("C", "F-C", "C-F"):
        return "C" if p != "F-C" or reb >= 8 or blk >= 1.3 else "PF"
    return "SF"


def parse_roster(roster_json: dict, abbr: str, season: int) -> list[dict]:
    players = []
    for a in _athletes(roster_json):
        pid = str(a.get("id") or "")
        name = a.get("displayName") or a.get("fullName")
        if not pid or not name:
            continue
        status = ((a.get("status") or {}).get("type") or "active").lower()
        sched = _salary_schedule(a, season)
        injuries = a.get("injuries") or []
        inj = None
        if injuries:
            first = injuries[0] if isinstance(injuries[0], dict) else {}
            inj = first.get("status") or first.get("type", {}).get("description") or "Injured"
        exp = (a.get("experience") or {}).get("years")
        players.append({
            "id": pid,
            "name": name,
            "team": abbr,
            "espn_pos": ((a.get("position") or {}).get("abbreviation") or ""),
            "age": int(a.get("age") or 25),
            "exp": int(exp) if isinstance(exp, (int, float)) else 0,
            "salary": sched,
            "twoway": not sched or sched[0] < 1_150_000,
            "inj": inj,
            "jersey": a.get("jersey") or "",
            "headshot": ((a.get("headshot") or {}).get("href") or ""),
            "status": status,
        })
    return players


def parse_stats_page(page_json: dict) -> tuple[dict[str, dict], int]:
    """{athlete id: per-game stat dict} for one page, plus the total page count."""
    names_by_cat = {c.get("name"): c.get("names") or [] for c in page_json.get("categories") or []}
    out: dict[str, dict] = {}
    for row in page_json.get("athletes") or []:
        ath = row.get("athlete") or {}
        pid = str(ath.get("id") or "")
        if not pid:
            continue
        s: dict[str, float] = {}
        for cat in row.get("categories") or []:
            names = names_by_cat.get(cat.get("name")) or []
            for key, val in zip(names, cat.get("values") or []):
                short = STAT_KEYS.get(key)
                if short and short not in s:
                    try:
                        s[short] = float(val)
                    except (TypeError, ValueError):
                        pass
        if s.get("gp"):
            out[pid] = s
    pages = int((page_json.get("pagination") or {}).get("pages") or 1)
    return out, pages


def build_league(rosters: dict[str, list[dict]], stats: dict[int, dict[str, dict]], season: int) -> dict:
    """Combine rosters + stats into the league snapshot every new save starts from."""
    players: dict[str, dict] = {}
    for abbr, plist in rosters.items():
        for p in plist:
            p = dict(p)
            hist = {str(yr): stats[yr][p["id"]] for yr in sorted(stats) if p["id"] in stats[yr]}
            p["hist"] = hist
            last = hist[max(hist)] if hist else None
            p["pos"] = _position(p.pop("espn_pos", ""), last)
            players[p["id"]] = p
    rate_league(players, season)
    return {
        "season": season,
        "fetched": int(time.time()),
        "players": players,
        "teams": {abbr: [p["id"] for p in plist] for abbr, plist in rosters.items()},
    }


# ------------------------------------------------------------------------------------------
# Network
# ------------------------------------------------------------------------------------------

async def _get(session: aiohttp.ClientSession, url: str, params: dict | None = None, tries: int = 3) -> Any:
    for attempt in range(tries):
        try:
            async with session.get(url, params=params, headers=HEADERS) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                log.warning("ESPN %s -> HTTP %s", url, r.status)
                if r.status in (403, 404):
                    return None  # blocked or missing: retrying the same URL won't help
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("ESPN %s failed: %r", url, e)
        await asyncio.sleep(1.5 * (attempt + 1))
    return None


async def fetch_roster(session: aiohttp.ClientSession, tid: int) -> dict | None:
    for url in [ROSTER_URL] + ROSTER_FALLBACKS:
        data = await _get(session, url.format(tid=tid))
        if data:
            team = data.get("team") if isinstance(data.get("team"), dict) else None
            if team and "athletes" in team and "athletes" not in data:
                data = team
            if data.get("athletes"):
                return data
    return None


def current_season(now: float | None = None) -> int:
    """ESPN season label: the 2026-27 season (starting October 2026) is 2027."""
    t = time.gmtime(now or time.time())
    return t.tm_year + 1 if t.tm_mon >= 8 else t.tm_year


async def fetch_stats(session: aiohttp.ClientSession, year: int) -> dict[str, dict]:
    params = {"region": "us", "lang": "en", "contentorigin": "espn", "isqualified": "false",
              "limit": 300, "sort": "offensive.avgPoints:desc", "season": year, "seasontype": 2}
    out: dict[str, dict] = {}
    page, pages = 1, 1
    while page <= pages and page <= 25:  # follows ESPN pagination even if it caps the page size
        data = await _get(session, STATS_URL, {**params, "page": page})
        if not data:
            break
        got, pages = parse_stats_page(data)
        out.update(got)
        page += 1
    return out


async def fetch_league(session: aiohttp.ClientSession) -> dict | None:
    season = current_season()
    rosters: dict[str, list[dict]] = {}
    sem = asyncio.Semaphore(3)

    async def one(abbr: str, tid: int) -> None:
        async with sem:
            data = await fetch_roster(session, tid)
            await asyncio.sleep(0.3)
        if data:
            rosters[abbr] = parse_roster(data, abbr, season)

    await asyncio.gather(*(one(a, t.espn_id) for a, t in cba.TEAMS.items()))
    if len(rosters) < 30:
        log.error("Only got %d/30 rosters from ESPN; keeping the previous snapshot", len(rosters))
        return None
    stats = {}
    for yr in (season - 2, season - 1, season):
        got = await fetch_stats(session, yr)
        if got:
            stats[yr] = got
    if not stats:
        log.error("No stats from ESPN; keeping the previous snapshot")
        return None
    league = build_league(rosters, stats, season)
    log.info("Fetched live league: %d players, stats seasons %s", len(league["players"]), sorted(stats))
    return league
