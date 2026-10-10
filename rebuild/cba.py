"""League constants: teams, the 2026-27 cap sheet, and CBA trade rules.

Cap figures are the official 2026-27 numbers announced by the NBA. Trade salary-matching
brackets follow the 2023 CBA structure (scaled to 2026-27) and are close approximations.
"""

from __future__ import annotations

from dataclasses import dataclass

FIRST_SEASON = 2027  # ESPN labels the 2026-27 season "2027"

# --- 2026-27 cap sheet (NBA official) ----------------------------------------------------
CAP_2027 = {
    "cap": 164_961_000,
    "tax": 200_428_000,
    "apron1": 209_015_000,
    "apron2": 221_686_000,
    "minimum": 148_465_000,
    "mle": 15_044_000,       # non-taxpayer mid-level
    "tp_mle": 6_064_000,     # taxpayer mid-level
    "room_mle": 9_366_000,   # mid-level for teams with cap room
}
CAP_GROWTH = 0.07  # assumed yearly growth after 2026-27 (CBA caps it at 10%)
MIN_SALARY = 2_400_000          # rough veteran-minimum average for display/fillers
ROOKIE_MIN = 1_300_000
MAX_PCT = {0: 0.25, 7: 0.30, 10: 0.35}  # max contract % of cap by years of service

ROSTER_MAX = 15
ROSTER_MAX_OFFSEASON = 18
ROSTER_MIN = 13
PICK_YEARS = 7  # teams may trade picks up to seven drafts out


def cap_sheet(season: int) -> dict[str, int]:
    """Cap numbers for a season (ESPN year label), grown from the 2026-27 sheet."""
    k = (1 + CAP_GROWTH) ** max(0, season - FIRST_SEASON)
    return {key: int(round(v * k, -3)) for key, v in CAP_2027.items()}


def max_salary(season: int, years_of_service: int) -> int:
    pct = 0.25
    for yrs, p in sorted(MAX_PCT.items()):
        if years_of_service >= yrs:
            pct = p
    return int(cap_sheet(season)["cap"] * pct)


def season_label(year: int) -> str:
    return f"{year - 1}-{str(year)[-2:]}"


# --- Teams --------------------------------------------------------------------------------

@dataclass(frozen=True)
class TeamInfo:
    abbr: str
    espn_id: int
    city: str
    name: str
    conf: str
    div: str
    color: int
    nicknames: tuple[str, ...] = ()

    @property
    def full(self) -> str:
        return f"{self.city} {self.name}"


_T = [
    ("ATL", 1, "Atlanta", "Hawks", "East", "Southeast", 0xE03A3E, ("hawks", "atlanta")),
    ("BOS", 2, "Boston", "Celtics", "East", "Atlantic", 0x007A33, ("celtics", "boston", "c's")),
    ("BKN", 17, "Brooklyn", "Nets", "East", "Atlantic", 0x000000, ("nets", "brooklyn")),
    ("CHA", 30, "Charlotte", "Hornets", "East", "Southeast", 0x1D1160, ("hornets", "charlotte")),
    ("CHI", 4, "Chicago", "Bulls", "East", "Central", 0xCE1141, ("bulls", "chicago")),
    ("CLE", 5, "Cleveland", "Cavaliers", "East", "Central", 0x860038, ("cavaliers", "cavs", "cleveland")),
    ("DAL", 6, "Dallas", "Mavericks", "West", "Southwest", 0x00538C, ("mavericks", "mavs", "dallas")),
    ("DEN", 7, "Denver", "Nuggets", "West", "Northwest", 0x0E2240, ("nuggets", "denver")),
    ("DET", 8, "Detroit", "Pistons", "East", "Central", 0xC8102E, ("pistons", "detroit")),
    ("GSW", 9, "Golden State", "Warriors", "West", "Pacific", 0x1D428A, ("warriors", "golden state", "dubs")),
    ("HOU", 10, "Houston", "Rockets", "West", "Southwest", 0xCE1141, ("rockets", "houston")),
    ("IND", 11, "Indiana", "Pacers", "East", "Central", 0x002D62, ("pacers", "indiana")),
    ("LAC", 12, "LA", "Clippers", "West", "Pacific", 0xC8102E, ("clippers",)),
    ("LAL", 13, "Los Angeles", "Lakers", "West", "Pacific", 0x552583, ("lakers",)),
    ("MEM", 29, "Memphis", "Grizzlies", "West", "Southwest", 0x5D76A9, ("grizzlies", "memphis")),
    ("MIA", 14, "Miami", "Heat", "East", "Southeast", 0x98002E, ("heat", "miami")),
    ("MIL", 15, "Milwaukee", "Bucks", "East", "Central", 0x00471B, ("bucks", "milwaukee")),
    ("MIN", 16, "Minnesota", "Timberwolves", "West", "Northwest", 0x0C2340, ("timberwolves", "wolves", "minnesota")),
    ("NOP", 3, "New Orleans", "Pelicans", "West", "Southwest", 0x0C2340, ("pelicans", "new orleans")),
    ("NYK", 18, "New York", "Knicks", "East", "Atlantic", 0x006BB6, ("knicks", "new york")),
    ("OKC", 25, "Oklahoma City", "Thunder", "West", "Northwest", 0x007AC1, ("thunder", "oklahoma city", "okc")),
    ("ORL", 19, "Orlando", "Magic", "East", "Southeast", 0x0077C0, ("magic", "orlando")),
    ("PHI", 20, "Philadelphia", "76ers", "East", "Atlantic", 0x006BB6, ("76ers", "sixers", "philadelphia", "philly")),
    ("PHX", 21, "Phoenix", "Suns", "West", "Pacific", 0x1D1160, ("suns", "phoenix")),
    ("POR", 22, "Portland", "Trail Blazers", "West", "Northwest", 0xE03A3E, ("trail blazers", "blazers", "portland")),
    ("SAC", 23, "Sacramento", "Kings", "West", "Pacific", 0x5A2D81, ("kings", "sacramento")),
    ("SAS", 24, "San Antonio", "Spurs", "West", "Southwest", 0xC4CED4, ("spurs", "san antonio")),
    ("TOR", 28, "Toronto", "Raptors", "East", "Atlantic", 0xCE1141, ("raptors", "toronto")),
    ("UTA", 26, "Utah", "Jazz", "West", "Northwest", 0x002B5C, ("jazz", "utah")),
    ("WAS", 27, "Washington", "Wizards", "East", "Southeast", 0x002B5C, ("wizards", "washington")),
]
TEAMS: dict[str, TeamInfo] = {t[0]: TeamInfo(*t) for t in _T}
BY_ESPN_ID = {t.espn_id: t.abbr for t in TEAMS.values()}
ESPN_ABBR = {"NY": "NYK", "GS": "GSW", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS",
             "BKN": "BKN", "PHO": "PHX"}


def norm_abbr(abbr: str) -> str:
    a = (abbr or "").upper()
    return ESPN_ABBR.get(a, a)


def find_team(text: str) -> str | None:
    """Team abbreviation from an abbreviation, city or nickname typed by a user."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t.upper() in TEAMS:
        return t.upper()
    if norm_abbr(t) in TEAMS:
        return norm_abbr(t)
    for info in TEAMS.values():
        if t in (info.full.lower(), info.name.lower(), info.city.lower()) or t in info.nicknames:
            return info.abbr
    for info in TEAMS.values():
        if t in info.full.lower():
            return info.abbr
    return None


# --- Salary matching ----------------------------------------------------------------------

def max_incoming(outgoing: int, season: int) -> int:
    """Most salary an over-the-cap (below first apron) team may take back for `outgoing`."""
    k = (1 + CAP_GROWTH) ** max(0, season - FIRST_SEASON)
    low, high, cushion = 8_000_000 * k, 32_000_000 * k, 250_000 * k
    if outgoing <= low:
        return int(2 * outgoing + cushion)
    if outgoing <= high:
        return int(outgoing + 9_000_000 * k)
    return int(1.25 * outgoing + cushion)


def money(x: float) -> str:
    x = float(x)
    if abs(x) >= 1_000_000:
        return f"${x / 1_000_000:.1f}M"
    if abs(x) >= 1_000:
        return f"${x / 1_000:.0f}K"
    return f"${x:,.0f}"
