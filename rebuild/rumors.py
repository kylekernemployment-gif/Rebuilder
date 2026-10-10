"""Live rumor engine: free news feeds -> classified rumors -> per-player trade "heat".

Sources (all free, no keys): RealGM's Wiretap RSS, ESPN's NBA news JSON, and r/nba's newest
posts (where insider reports are posted with the reporter in brackets, e.g. "[Charania] ...").

Each item is matched to players and teams on the live rosters, tagged with what it signals
(trade request, available, off the table, extension, rebuild/contend...) and weighted by how
credible the reporter is. Rumors can't truly be "verified" until something happens, so credibility
comes from the reporter tier plus corroboration (several independent insiders saying the same thing).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import math
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Iterable

import aiohttp

from . import cba

log = logging.getLogger("rebuild.rumors")

REALGM_RSS = "https://basketball.realgm.com/rss/wiretap/0/0.xml"
ESPN_NEWS = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news?limit=50"
REDDIT_RSS = "https://www.reddit.com/r/nba/new/.rss?limit=100"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIER_WEIGHT = {1: 1.0, 2: 0.65, 3: 0.35, 4: 0.12}
TIER_BADGE = {1: "✅ Tier 1 insider", 2: "☑️ Established reporter", 3: "📰 Outlet report", 4: "▫️ Unconfirmed"}
HALF_LIFE_DAYS = 12.0
MAX_AGE_DAYS = 60

REPORTERS = {
    1: ["Shams Charania", "Chris Haynes", "Marc Stein", "Jake Fischer", "Brian Windhorst",
        "Adrian Wojnarowski", "Michael Scotto", "Bobby Marks", "Tim MacMahon", "Ramona Shelburne",
        "Sam Amick", "Anthony Slater", "Kelly Iko", "Joe Vardon", "Fred Katz", "Jovan Buha",
        "Dave McMenamin", "Zach Lowe", "Kevin O'Connor"],
    2: ["Evan Sidery", "Ian Begley", "Stefan Bondy", "Marc J. Spears", "Marc Spears", "Brett Siegel",
        "Grant Afseth", "Eric Pincus", "Dan Woike", "Mark Medina", "Keith Smith", "John Hollinger",
        "Jason Lloyd", "Chris Mannix", "Steve Popper", "Brian Lewis", "Barry Jackson", "Ira Winderman",
        "Christian Clark", "Tony Jones", "Adam Himmelsbach", "Jay King", "Jared Weiss", "Kyle Goon",
        "Keith Pompey", "Gina Mizell", "Darren Wolfson", "Jon Krawczynski", "Bennett Durando",
        "Vincent Goodwill", "K.C. Johnson", "Mike Vorkunov", "Josh Robbins", "Jake Weinbach",
        "Kevin Chouinard", "Law Murray", "Mark Deeks", "Hunter Patterson", "Joe Cowley"],
}
OUTLETS = ["ESPN", "The Athletic", "Yahoo", "NBA.com", "SNY", "The Stein Line", "HoopsHype",
           "Bleacher Report", "NBC Sports", "New York Post", "Daily News", "Boston Globe"]

# (label, direction, strength, pattern). Direction > 0 = more available, < 0 = less.
PLAYER_SIGNALS = [
    ("Trade request", 1.0, 1.0, r"request(?:ed|s|ing)? (?:a |his )?trade|trade request|wants out|"
                                r"wants to be traded|seeking a trade|asked (?:out|to be traded)|prefers a trade"),
    ("Off the table", -1.0, 0.85, r"untouchable|not available|off[- ]limits|not (?:actively )?shopping|"
                                  r"no (?:plans?|intention) (?:of|to) (?:trad|mov)|not trading|isn'?t going anywhere|"
                                  r"(?:has|have) no interest in (?:trading|moving)|franchise cornerstone|building around|"
                                  r"not (?:looking|expected) to (?:trade|move)"),
    ("Extension talks", -1.0, 0.5, r"extension"),
    ("On the block", 1.0, 0.65, r"made available|available (?:in|for) (?:a )?trades?|on the (?:trade )?block|"
                                r"shopping|being shopped|listening to (?:trade )?(?:offers|calls)|open to (?:trading|moving|offers|a trade)|"
                                r"gauging (?:the )?(?:trade )?(?:market|interest)|exploring (?:the )?(?:trade|options)|"
                                r"could be (?:traded|moved|dealt)|(?:expected|likely) to be (?:traded|moved|dealt)|"
                                r"trade candidate|trade talks|in talks|discussing (?:a )?(?:trade|deal)|"
                                r"engaged in (?:trade )?(?:talks|discussions)|trade market"),
    ("Unhappy", 1.0, 0.35, r"frustrat|unhappy|disgruntled|rift|discontent|tension"),
    ("Teams interested", 1.0, 0.25, r"interest(?:ed)? in|registered interest|monitoring|inquired|"
                                    r"targeting|eyeing|pursuing|kicked the tires|on (?:their|the) radar"),
    ("Injury", 0.0, 0.0, r"out for the season|torn|tear|fracture|surgery|out indefinitely|sidelined|"
                         r"miss (?:the rest|several|multiple)"),
]
TEAM_SIGNALS = [
    ("Rebuilding", 1.0, r"rebuild|tank|retool|blow it up|full reset|sellers?\b|selling off|prioritiz\w+ (?:youth|picks|development)"),
    ("Going for it", -1.0, r"all[- ]in|win[- ]now|buyers?\b|push for a (?:title|championship)|"
                           r"consolidat|go for it|title contender|aggressive(?:ly)? (?:pursu|chas)"),
]
_PLAYER_RX = [(lbl, d, s, re.compile(rx, re.I)) for lbl, d, s, rx in PLAYER_SIGNALS]
_TEAM_RX = [(lbl, d, re.compile(rx, re.I)) for lbl, d, rx in TEAM_SIGNALS]
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9' ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


# ------------------------------------------------------------------------------------------
# Feeds
# ------------------------------------------------------------------------------------------

def _item(title: str, text: str, url: str, ts: float, feed: str) -> dict:
    iid = hashlib.sha1((title + url).encode()).hexdigest()[:12]
    return {"id": iid, "title": _strip_html(title), "text": _strip_html(text), "url": url, "ts": ts, "feed": feed}


def parse_rss(xml_text: str, feed: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        ts = time.time()
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            pass
        out.append(_item(it.findtext("title") or "", it.findtext("description") or "",
                         it.findtext("link") or "", ts, feed))
    for it in root.iter(atom + "entry"):
        ts = time.time()
        stamp = it.findtext(atom + "updated") or it.findtext(atom + "published") or ""
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        link = it.find(atom + "link")
        out.append(_item(it.findtext(atom + "title") or "", it.findtext(atom + "content") or "",
                         link.get("href") if link is not None else "", ts, feed))
    return out


def parse_espn_news(data: dict) -> list[dict]:
    out = []
    for a in data.get("articles") or []:
        ts = time.time()
        try:
            from datetime import datetime
            ts = datetime.fromisoformat((a.get("published") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        url = (((a.get("links") or {}).get("web") or {}).get("href")) or ""
        text = (a.get("description") or "") + (" by " + a["byline"] if a.get("byline") else "")
        out.append(_item(a.get("headline") or "", text, url, ts, "ESPN"))
    return out


async def _get_text(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=HEADERS) as r:
            if r.status == 200:
                return await r.text()
            log.info("Rumor feed %s -> HTTP %s", url, r.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.info("Rumor feed %s failed: %r", url, e)
    return None


async def fetch_items(session: aiohttp.ClientSession) -> list[dict]:
    import json
    items: list[dict] = []
    realgm, espn, reddit = await asyncio.gather(
        _get_text(session, REALGM_RSS), _get_text(session, ESPN_NEWS), _get_text(session, REDDIT_RSS))
    if realgm:
        items += parse_rss(realgm, "RealGM")
    if espn:
        try:
            items += parse_espn_news(json.loads(espn))
        except ValueError:
            pass
    if reddit:
        items += [i for i in parse_rss(reddit, "r/nba") if i["title"].startswith("[")]
    for handle in [h.strip() for h in os.getenv("RUMOR_BLUESKY_HANDLES", "").split(",") if h.strip()]:
        txt = await _get_text(session, "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
                                       f"?actor={handle}&limit=30")
        if txt:
            try:
                for f in json.loads(txt).get("feed", []):
                    rec = (f.get("post") or {}).get("record") or {}
                    from datetime import datetime
                    ts = datetime.fromisoformat(rec.get("createdAt", "").replace("Z", "+00:00")).timestamp()
                    items.append(_item(rec.get("text", "")[:140], rec.get("text", ""),
                                       f"https://bsky.app/profile/{handle}", ts, f"@{handle}"))
            except (ValueError, TypeError):
                pass
    return items


# ------------------------------------------------------------------------------------------
# Classification
# ------------------------------------------------------------------------------------------

class NameIndex:
    """Find players/teams named in text, using the live rosters."""

    def __init__(self, players: dict[str, dict]):
        self.full: dict[str, str] = {}
        last_count: dict[str, list[str]] = {}
        for pid, p in players.items():
            f = fold(p["name"])
            f2 = re.sub(r" (jr|sr|ii|iii|iv)$", "", f)
            for key in {f, f2}:
                if len(key) > 4:
                    self.full[key] = pid
            last = f2.split(" ")[-1] if " " in f2 else ""
            if len(last) >= 5:
                last_count.setdefault(last, []).append(pid)
        self.last = {k: v[0] for k, v in last_count.items() if len(v) == 1}
        self.players = players
        self.team_terms = []
        for abbr, t in cba.TEAMS.items():
            terms = {fold(t.full), fold(t.name)} | {fold(n) for n in t.nicknames if len(n) > 3}
            for term in terms:
                self.team_terms.append((term, abbr))

    def teams_in(self, folded: str) -> set[str]:
        padded = f" {folded} "
        return {abbr for term, abbr in self.team_terms if f" {term} " in padded}

    def players_in(self, folded: str, teams_in_item: set[str]) -> set[str]:
        padded = f" {folded} "
        found = {pid for name, pid in self.full.items() if f" {name} " in padded}
        named_last = {fold(self.players[pid]["name"]).split(" ")[-1] for pid in found}
        for last, pid in self.last.items():
            if last in named_last or pid in found:
                continue
            if f" {last} " in padded and self.players[pid].get("team") in teams_in_item:
                found.add(pid)
        return found


def reporter_of(item: dict) -> tuple[str | None, int]:
    title, text = item["title"], item["text"]
    m = re.match(r"\[([^\]]+)\]", title)
    blob = f"{title} {text}"
    if m:
        tag = m.group(1).strip().lower()
        for tier, names in REPORTERS.items():
            for n in names:
                if tag in (n.lower(), n.split(" ")[-1].lower()):
                    return n, tier
        if tag in ("highlight", "post game thread", "game thread", "serious"):
            return None, 4
    for tier, names in REPORTERS.items():
        for n in names:
            if n.lower() in blob.lower():
                return n, tier
    for o in OUTLETS:
        if re.search(rf"\b{re.escape(o)}\b", blob):
            return o, 3
    if item.get("feed") == "ESPN":
        return "ESPN", 3
    return None, 4


def classify(item: dict, index: NameIndex) -> dict:
    """Attach players/teams/signals to a feed item."""
    reporter, tier = reporter_of(item)
    body = f"{item['title']}. {item['text']}"
    teams_all = index.teams_in(fold(body))
    hits: dict[str, tuple[str, float, float]] = {}
    team_hits: dict[str, tuple[str, float]] = {}
    for sentence in _SENT_SPLIT.split(body):
        if not sentence.strip():
            continue
        f = fold(sentence)
        players = index.players_in(f, teams_all)
        teams = index.teams_in(f)
        if players:
            best = None
            for lbl, d, s, rx in _PLAYER_RX:
                if rx.search(sentence):
                    best = (lbl, d, s)
                    break  # list is ordered by priority (negations before positives)
            if best:
                for pid in players:
                    prev = hits.get(pid)
                    if not prev or abs(best[1] * best[2]) > abs(prev[1] * prev[2]):
                        hits[pid] = best
        for lbl, d, rx in _TEAM_RX:
            if rx.search(sentence):
                for abbr in teams or teams_all:
                    team_hits.setdefault(abbr, (lbl, d))
    return {**item, "reporter": reporter, "tier": tier,
            "players": {pid: list(v) for pid, v in hits.items()},
            "teams": {abbr: list(v) for abbr, v in team_hits.items()}}


def decay(ts: float, now: float) -> float:
    days = max(0.0, (now - ts) / 86400)
    return 0.5 ** (days / HALF_LIFE_DAYS) if days <= MAX_AGE_DAYS else 0.0


def compute_heat(items: Iterable[dict], now: float | None = None) -> dict:
    """{'players': {pid: {...}}, 'teams': {abbr: {...}}} from classified items."""
    now = now or time.time()
    acc: dict[str, dict] = {}
    for it in items:
        for pid, (lbl, d, s) in it.get("players", {}).items():
            w = TIER_WEIGHT[it["tier"]] * decay(it["ts"], now)
            if w <= 0:
                continue
            a = acc.setdefault(pid, {"sum": 0.0, "reporters": {}, "best": None, "best_w": -1, "inj": None})
            a["sum"] += d * s * w
            if d != 0 and it["tier"] <= 2 and it.get("reporter"):
                a["reporters"].setdefault(int(math.copysign(1, d)), set()).add(it["reporter"])
            if lbl == "Injury" and not a["inj"]:
                a["inj"] = it["title"]
            score = abs(d * s) * w + (0.01 if lbl != "Injury" else 0)
            if score > a["best_w"]:
                a["best_w"] = score
                a["best"] = {"label": lbl, "title": it["title"], "url": it["url"], "ts": it["ts"],
                             "reporter": it.get("reporter"), "tier": it["tier"]}
    players = {}
    for pid, a in acc.items():
        sign = 1 if a["sum"] >= 0 else -1
        n = len(a["reporters"].get(sign, ()))
        corro = min(1.75, 1 + 0.25 * max(0, n - 1))
        heat = math.tanh(a["sum"] * corro)
        players[pid] = {"h": round(heat, 3), "corroborated": n, **(a["best"] or {})}
    teams: dict[str, dict] = {}
    for it in items:
        for abbr, (lbl, d) in it.get("teams", {}).items():
            w = TIER_WEIGHT[it["tier"]] * decay(it["ts"], now)
            t = teams.setdefault(abbr, {"sum": 0.0, "label": lbl, "w": 0.0})
            t["sum"] += d * w * 0.6
            if w > t["w"]:
                t["w"], t["label"] = w, lbl
    return {"players": players,
            "teams": {a: {"lean": round(math.tanh(t["sum"]), 3), "label": t["label"]} for a, t in teams.items()},
            "updated": int(now)}


class RumorFeed:
    """Keeps a rolling window of classified items and the derived heat map."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.heat: dict = {"players": {}, "teams": {}, "updated": 0}
        self.index: NameIndex | None = None

    def set_players(self, players: dict[str, dict]) -> None:
        self.index = NameIndex(players)
        self.items = {k: classify(v, self.index) for k, v in self.items.items()}

    def ingest(self, raw_items: list[dict], now: float | None = None) -> int:
        if not self.index:
            return 0
        now = now or time.time()
        new = 0
        for it in raw_items:
            if it["id"] in self.items or now - it["ts"] > MAX_AGE_DAYS * 86400:
                continue
            self.items[it["id"]] = classify(it, self.index)
            new += 1
        cutoff = now - MAX_AGE_DAYS * 86400
        self.items = {k: v for k, v in self.items.items() if v["ts"] >= cutoff}
        self.heat = compute_heat(self.items.values(), now)
        return new

    def recent(self, limit: int = 10, relevant_only: bool = True) -> list[dict]:
        items = sorted(self.items.values(), key=lambda i: -i["ts"])
        if relevant_only:
            items = [i for i in items if any(v[1] != 0 for v in i["players"].values()) or i["teams"]]
        return items[:limit]

    def dump(self) -> dict:
        return {"items": list(self.items.values())}

    def load(self, data: dict) -> None:
        self.items = {i["id"]: i for i in data.get("items", [])}
        self.heat = compute_heat(self.items.values())
