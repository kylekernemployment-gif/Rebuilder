"""Draft classes, scouting and busts.

The first draft uses the real 2027 class (ESPN's big board). Later classes are generated. Every
prospect has a hidden true outcome drawn from historical hit rates by draft range, so some top
picks become stars, many become starters or role players, and a real share bust. The board only
shows scouts' estimates: a rough ceiling and floor (with NBA player comparisons) during the season,
a tighter range at the draft.

Approximate historical outcomes by draft range (All-Star / starter / role player / bust):
  picks 1-3:   45% / 30% / 15% / 10%
  picks 4-10:  20% / 35% / 25% / 20%
  picks 11-20:  7% / 25% / 33% / 35%
  picks 21-30:  4% / 18% / 33% / 45%
  2nd round:    1% /  8% / 25% / 66%
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from . import cba

REAL = Path(__file__).parent / "data" / "draft_2027.json"
TIERS = ["All-Star", "Starter", "Role player", "Bust"]
TIER_POT = {"All-Star": (86, 95), "Starter": (78, 85), "Role player": (70, 77), "Bust": (58, 69)}
ODDS = [(3, [45, 30, 15, 10]), (10, [20, 35, 25, 20]), (20, [7, 25, 33, 35]), (30, [4, 18, 33, 45]),
        (99, [1, 8, 25, 66])]
SCHOOLS = ["Duke", "Kentucky", "Kansas", "UConn", "North Carolina", "Arizona", "Gonzaga", "Baylor", "Houston",
           "Alabama", "Auburn", "Tennessee", "Michigan", "Florida", "Texas", "Arkansas", "UCLA", "Purdue",
           "Villanova", "Illinois", "USC", "Indiana", "Real Madrid", "Partizan", "ASVEL (France)", "NBL Next Stars",
           "G League Ignite", "Overtime Elite", "Baskonia", "Mega Basket"]


def tier_odds(rank: int) -> list[int]:
    return next(o for cut, o in ODDS if rank <= cut)


def _real(year: int) -> list[dict] | None:
    try:
        data = json.loads(REAL.read_text())
    except (OSError, ValueError):
        return None
    return data["prospects"] if data.get("year") == year else None


def make_class(save: dict, year: int, rng: random.Random) -> list[dict]:
    from .sim import FIRST, LAST, make_player
    real = _real(year)
    n = 72
    out = []
    for i in range(n):
        rank = i + 1
        r = real[i] if real and i < len(real) else None
        age = r["age"] if r else rng.choice([19, 19, 19, 20, 20, 21, 22])
        tier = rng.choices(TIERS, weights=tier_odds(rank))[0]
        lo, hi = TIER_POT[tier]
        true_pot = rng.randint(lo, hi)
        # how NBA-ready he is now: top picks and older prospects play sooner
        ovr = int(max(45, min(78, 74 - rank * 0.28 + (age - 19) * 1.6 + rng.gauss(0, 2.5))))
        ovr = min(ovr, true_pot - 2)
        p = make_player(rng, year, ovr=ovr, age=age, pot=true_pot,
                        name=r["name"] if r else f"{rng.choice(FIRST)} {rng.choice(LAST)}")
        if r:
            p["pos"] = r["pos"] if r["pos"] in ("PG", "SG", "SF", "PF", "C") else p["pos"]
        consensus = 92 - rank * 0.42                       # what the league thinks he can be
        p["scout"] = int(round(consensus + (true_pot - consensus) * 0.35 + rng.gauss(0, 2.5)))
        p["ceil"] = min(97, p["scout"] + rng.randint(4, 7))
        p["floor"] = max(55, p["scout"] - rng.randint(9, 14))
        p["school"] = r["school"] if r else rng.choice(SCHOOLS)
        p["draft_year"] = year
        p["rank"] = rank
        p["dur"] = round(rng.uniform(0.7, 0.95), 3)
        out.append(p)
    out.sort(key=lambda p: p["rank"])
    for i, p in enumerate(out, 1):
        p["big_board"] = i
    return out


def ensure_class(save: dict, rng: random.Random) -> list[dict]:
    """The class for this season's draft (scouted all season, picked in June)."""
    year = save["season"]
    dc = save.get("draft_class")
    if not dc or dc.get("year") != year:
        save["draft_class"] = {"year": year, "prospects": make_class(save, year, rng)}
    return save["draft_class"]["prospects"]


def label_for(v: float) -> str:
    return "All-Star" if v >= 86 else "Starter" if v >= 78 else "Role player" if v >= 70 else "Bench/fringe"


def comp(save: dict, pos: str, target: float, exclude: set[str] | None = None) -> str | None:
    """A current NBA player at the same position whose level matches a projection."""
    pool = [p for p in save["players"].values() if p.get("team") and p["pos"] == pos and 23 <= p["age"] <= 32
            and not p.get("generated") and p["id"] not in (exclude or set())]
    if not pool:
        return None
    best = min(pool, key=lambda p: (abs(p["ovr"] - target), -p.get("exp", 0)))
    return best["name"]


def projection(save: dict, p: dict, precise: bool = False) -> str:
    """'Ceiling: All-Star (like X) · Floor: Role player (like Y)'. Tighter at the draft."""
    ceil, floor = p["ceil"], p["floor"]
    if precise:
        ceil, floor = p["scout"] + 3, p["scout"] - 6
    c = comp(save, p["pos"], ceil)
    f = comp(save, p["pos"], floor + 6, {c} if c else None)
    ceil_txt = f"{label_for(ceil)}" + (f" (like {c})" if c else "")
    floor_txt = f"{label_for(floor + 6)}" + (f" (like {f})" if f else "")
    return f"Ceiling: {ceil_txt} · Floor: {floor_txt}"


def my_projected_picks(save: dict) -> list[str]:
    from . import ai
    from . import league as L
    out = []
    for k in L.team_picks(save, save["team"]):
        if k["year"] == save["season"]:
            slot = ai.expected_slot(save, k) + (30 if k["round"] == 2 else 0)
            out.append(f"{L.pick_label(k)} (projects around #{round(slot)})")
    return out


def tier_name(true_pot: int) -> str:
    return label_for(true_pot)


def all_teams() -> list[str]:
    return list(cba.TEAMS)
