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
            # Two-way only when ESPN lists a tiny salary. No contract data at all usually means ESPN
            # hasn't loaded a new deal yet (re-signings, extensions), so we estimate it instead.
            "twoway": bool(sched) and sched[0] < 1_150_000,
            "est": not sched,
            "inj": inj,
            "jersey": a.get("jersey") or "",
            "headshot": ((a.get("headshot") or {}).get("href") or ""),
            "status": status,
        })
    return players


def parse_stats_page(page_json: dict, meta: dict | None = None) -> tuple[dict[str, dict], int]:
    """{athlete id: per-game stat dict} for one page, plus the total page count.
    If `meta` is given, also records each athlete's name/age/position (for free agents)."""
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
            if meta is not None and ath.get("displayName"):
                pos = ath.get("position")
                pos = (pos.get("abbreviation") if isinstance(pos, dict) else pos) or ""
                meta[pid] = {"name": ath["displayName"], "age": ath.get("age"), "pos": pos,
                             "headshot": ((ath.get("headshot") or {}).get("href") or ""),
                             "team_id": ath.get("teamId")}
    pages = int((page_json.get("pagination") or {}).get("pages") or 1)
    return out, pages


def _apply_contracts(players: dict[str, dict], contracts: dict[str, dict[str, dict]]) -> int:
    """Overwrite ESPN contract numbers with Basketball-Reference's (newer and complete)."""
    from .sources import name_key
    hits = 0
    by_name: dict[str, list[dict]] = {}
    for abbr, rows in contracts.items():
        for key, row in rows.items():
            by_name.setdefault(key, []).append({**row, "team": abbr})
    for p in players.values():
        rows = by_name.get(name_key(p["name"]))
        if not rows:
            continue
        row = next((r for r in rows if r["team"] == p.get("team")), rows[0] if len(rows) == 1 else None)
        if not row:
            continue
        sal = row["salary"]
        if sal and sal[0] >= 1_150_000:
            p["salary"] = list(sal)
            p["twoway"] = False
            p["est"] = False
            p["src"] = "bbref"
            hits += 1
    return hits


def _apply_2k(players: dict[str, dict], ratings: dict[str, int]) -> int:
    """Use NBA 2K27 overalls where we have them; re-scale our own ratings for everyone else."""
    from .sources import name_key
    from .ratings import potential
    pairs = []
    for p in players.values():
        r = ratings.get(name_key(p["name"]))
        if r:
            pairs.append((p["ovr"], r))
    a, b = 1.0, 0.0
    if len(pairs) >= 30:
        n = len(pairs)
        mx = sum(x for x, _ in pairs) / n
        my = sum(y for _, y in pairs) / n
        var = sum((x - mx) ** 2 for x, _ in pairs)
        if var > 0:
            a = sum((x - mx) * (y - my) for x, y in pairs) / var
            b = my - a * mx
    hits = 0
    for pid, p in players.items():
        r = ratings.get(name_key(p["name"]))
        if r:
            p["ovr"] = r
            p["ovr_src"] = "2K27"
            hits += 1
        elif pairs:
            p["ovr"] = int(max(40, min(99, round(a * p["ovr"] + b))))
        p["pot"] = potential(p["ovr"], p.get("age", 25), pid)
    return hits


def build_league(rosters: dict[str, list[dict]], stats: dict[int, dict[str, dict]], season: int,
                 meta: dict | None = None, extras: dict | None = None) -> dict:
    """Combine rosters + stats (+ real contracts and 2K ratings) into the league snapshot."""
    import copy
    extras = extras or {}
    players: dict[str, dict] = {}
    for abbr, plist in rosters.items():
        for p in plist:
            p = copy.deepcopy(p)
            hist = {str(yr): stats[yr][p["id"]] for yr in sorted(stats) if p["id"] in stats[yr]}
            p["hist"] = hist
            last = hist[max(hist)] if hist else None
            p["pos"] = _position(p.get("espn_pos", ""), last)
            p.pop("espn_pos", None)
            tid = (meta or {}).get(p["id"], {}).get("team_id")
            try:
                p["prev_team"] = cba.BY_ESPN_ID.get(int(tid)) if tid else None
            except (TypeError, ValueError):
                p["prev_team"] = None
            players[p["id"]] = p
    # Real free agents: played last season, not on any roster now.
    fa_ids = []
    last_year = max((y for y in stats if y < season), default=None)
    for pid, m in (meta or {}).items():
        if pid in players or last_year is None or pid not in stats.get(last_year, {}):
            continue
        if stats[last_year][pid].get("gp", 0) < 15 or (m.get("age") or 30) > 38:
            continue
        hist = {str(yr): stats[yr][pid] for yr in sorted(stats) if pid in stats[yr]}
        players[pid] = {"id": pid, "name": m["name"], "team": None, "age": int(m.get("age") or 28),
                        "exp": 5, "salary": [], "inj": None, "jersey": "", "headshot": m.get("headshot", ""),
                        "status": "active", "hist": hist,
                        "pos": _position(m.get("pos", ""), hist[max(hist)])}
        fa_ids.append(pid)
    n_contracts = _apply_contracts(players, extras.get("contracts") or {})
    rate_league(players, season)
    n_2k = _apply_2k(players, extras.get("ratings") or {}) if extras.get("ratings") else 0
    from .ai import market_salary
    for p in players.values():
        if p.get("est") and p.get("team"):
            amt = int(round(market_salary(p["ovr"], season), -5))
            yrs = 4 if p["age"] <= 26 else 3 if p["age"] <= 30 else 2
            p["salary"] = [int(amt * 1.05 ** i) for i in range(yrs)]
    if n_contracts or n_2k:
        log.info("Applied %d Basketball-Reference contracts and %d NBA 2K27 ratings", n_contracts, n_2k)
    return {
        "season": season,
        "fetched": int(time.time()),
        "players": players,
        "teams": {abbr: [p["id"] for p in plist] for abbr, plist in rosters.items()},
        "fa": fa_ids,
        "sources": {"contracts": n_contracts, "ratings": n_2k},
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


async def fetch_stats(session: aiohttp.ClientSession, year: int, meta: dict | None = None) -> dict[str, dict]:
    params = {"region": "us", "lang": "en", "contentorigin": "espn", "isqualified": "false",
              "limit": 300, "sort": "offensive.avgPoints:desc", "season": year, "seasontype": 2}
    out: dict[str, dict] = {}
    page, pages = 1, 1
    while page <= pages and page <= 25:  # follows ESPN pagination even if it caps the page size
        data = await _get(session, STATS_URL, {**params, "page": page})
        if not data:
            break
        got, pages = parse_stats_page(data, meta)
        out.update(got)
        page += 1
    return out


async def fetch_espn(session: aiohttp.ClientSession) -> dict | None:
    """Raw ESPN data: rosters, stats and player info. Fast (about 20 seconds)."""
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
    stats: dict[int, dict] = {}
    meta: dict[str, dict] = {}
    for yr in (season - 2, season - 1, season):
        got = await fetch_stats(session, yr, meta if yr == season - 1 else None)
        if got:
            stats[yr] = got
    if not stats:
        log.error("No stats from ESPN; keeping the previous snapshot")
        return None
    return {"season": season, "rosters": rosters, "stats": stats, "meta": meta}


def league_from(raw: dict, extras: dict | None = None) -> dict:
    league = build_league(raw["rosters"], raw["stats"], raw["season"], raw.get("meta"), extras)
    log.info("Built live league: %d players (%d free agents), stats seasons %s",
             len(league["players"]), len(league["fa"]), sorted(raw["stats"]))
    return league


async def fetch_league(session: aiohttp.ClientSession, extras: dict | None = None) -> dict | None:
    raw = await fetch_espn(session)
    return league_from(raw, extras) if raw else None
