"""Extra free data layered on top of ESPN:

Real contracts from Basketball-Reference's team payroll pages (they post new deals and extensions
within a day or two, well before ESPN does), and NBA 2K27 overall ratings from 2kratings.com.

Both are public web pages read politely (one page every few seconds, a couple of times a day).
If either site is unreachable, the bot keeps the last good copy, or falls back to ESPN contracts
and its own stats-based ratings.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import logging
import re

import aiohttp

from . import cba
from .rumors import fold

log = logging.getLogger("rebuild.sources")

BBREF_URL = "https://www.basketball-reference.com/contracts/{abbr}.html"
BBREF_ABBR = {"BKN": "BRK", "CHA": "CHO", "PHX": "PHO"}
TWOK_URL = "https://www.2kratings.com/teams/{slug}"
BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
BBREF_DELAY = 3.5   # Basketball-Reference allows ~20 requests a minute
TWOK_DELAY = 2.0


def _text(s: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def name_key(name: str) -> str:
    return re.sub(r" (jr|sr|ii|iii|iv)$", "", fold(name))


async def _html(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=BROWSER) as r:
            if r.status == 200:
                return await r.text()
            log.warning("%s -> HTTP %s", url, r.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("%s failed: %r", url, e)
    return None


# ------------------------------------------------------------------------------------------
# Basketball-Reference contracts
# ------------------------------------------------------------------------------------------

def _money(s: str) -> int | None:
    digits = re.sub(r"[^\d]", "", _text(s))
    return int(digits) if digits else None


def parse_bbref_contracts(page: str) -> dict[str, dict]:
    """{name key: {"salary": [this season, next, ...], "name": display name}} from a team payroll page."""
    page = page.replace("<!--", "").replace("-->", "")
    out: dict[str, dict] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        m = re.search(r'data-stat="player"[^>]*>(.*?)</t[hd]>', row, re.S)
        if not m:
            continue
        name = _text(m.group(1))
        if not name or "total" in name.lower():
            continue
        years = re.findall(r'data-stat="y(\d)"[^>]*>(.*?)</td>', row, re.S)
        salaries: list[int] = []
        if years:
            for _, cell in sorted(years, key=lambda x: int(x[0])):
                amt = _money(cell)
                if not amt:
                    break
                salaries.append(amt)
        else:  # layout fallback: dollar amounts in order, minus the "guaranteed" total at the end
            amts = [int(re.sub(r"[^\d]", "", a)) for a in re.findall(r"\$[\d,]+", _text(row))]
            salaries = amts[:-1] if len(amts) > 1 else amts
        if salaries:
            out[name_key(name)] = {"salary": salaries[:6], "name": name}
    return out


async def fetch_contracts(session: aiohttp.ClientSession) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for abbr in cba.TEAMS:
        page = await _html(session, BBREF_URL.format(abbr=BBREF_ABBR.get(abbr, abbr)))
        if page:
            got = parse_bbref_contracts(page)
            if got:
                out[abbr] = got
        await asyncio.sleep(BBREF_DELAY)
    log.info("Basketball-Reference contracts: %d/30 teams, %d players",
             len(out), sum(len(v) for v in out.values()))
    return out


# ------------------------------------------------------------------------------------------
# NBA 2K27 ratings (2kratings.com)
# ------------------------------------------------------------------------------------------

def team_slugs(abbr: str) -> list[str]:
    t = cba.TEAMS[abbr]
    full = re.sub(r"[^a-z0-9]+", "-", t.full.lower()).strip("-")
    slugs = [full]
    if t.city == "LA":
        slugs.append("los-angeles-" + re.sub(r"[^a-z0-9]+", "-", t.name.lower()))
    return slugs


_ANCHOR = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S | re.I)
_RATING_BOX = re.compile(r"attribute-box[^>]*>\s*(\d{2})\s*<", re.I)
_BARE = re.compile(r">\s*(\d{2})\s*<")


def parse_2k_ratings(page: str, known: set[str]) -> dict[str, int]:
    """{name key: 2K overall} for the players named on a 2kratings team page.

    Each player's overall is the first rating box (or bare two-digit number) after his name."""
    out: dict[str, int] = {}
    anchors = [(m.end(), name_key(_text(m.group(1)))) for m in _ANCHOR.finditer(page)]
    for i, (end, key) in enumerate(anchors):
        if key not in known or key in out:
            continue
        window = page[end:end + 1500]
        # stop at the next player's name so we never steal his rating
        for nend, nkey in anchors[i + 1:]:
            if nkey in known and nkey != key:
                window = page[end:nend]
                break
        m = _RATING_BOX.search(window)
        val = int(m.group(1)) if m else None
        if val is None:
            for b in _BARE.finditer(window):
                v = int(b.group(1))
                if 40 <= v <= 99:
                    val = v
                    break
        if val and 40 <= val <= 99:
            out[key] = val
    return out


async def fetch_2k(session: aiohttp.ClientSession, known: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    teams = 0
    for abbr in cba.TEAMS:
        for slug in team_slugs(abbr):
            page = await _html(session, TWOK_URL.format(slug=slug))
            await asyncio.sleep(TWOK_DELAY)
            if page:
                got = parse_2k_ratings(page, known)
                if got:
                    out.update(got)
                    teams += 1
                    break
    log.info("2K27 ratings: %d players from %d/30 team pages", len(out), teams)
    return out
