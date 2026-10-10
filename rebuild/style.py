"""Team identities and matchups.

Each team's rotation (weighted by minutes) gets a profile: spacing, playmaking, inside scoring,
rebounding, perimeter defense and rim protection. Matchups then nudge single games and series:
a great shooting team pulls a defense that leans on one rim-protecting big away from the basket,
bigs feast on teams with no rim protection, creators pick apart weak perimeter defenders, and the
better rebounding team gets extra possessions. Effects are a couple of points of win probability,
not more.
"""

from __future__ import annotations

from . import cba
from . import league as L

WINGS, GUARDS, BIGS = {"SF"}, {"PG", "SG"}, {"PF", "C"}


def profile(save: dict, abbr: str) -> dict:
    mins = L.minutes(save, abbr)
    ps = [(save["players"][pid], m) for pid, m in mins.items() if m > 0]
    if not ps:
        return {"sho": 50, "pla": 50, "ins": 50, "reb": 50, "perim": 50, "rim": 50}

    def avg(key, filt=None):
        rows = [(p.get("attrs", {}).get(key, 50), m) for p, m in ps if not filt or p["pos"] in filt]
        tot = sum(m for _, m in rows)
        return sum(v * m for v, m in rows) / tot if tot else 50.0
    bigs = sorted(((p, m) for p, m in ps if p["pos"] in BIGS), key=lambda x: -x[1])
    rim = 45.0
    if bigs:
        top = bigs[0][0].get("attrs", {}).get("def", 50)
        rim = top * 0.7 + avg("def", BIGS) * 0.3       # one great shot-blocker anchors it
    return {"sho": avg("sho"), "pla": avg("pla", GUARDS | WINGS), "ins": avg("sco", BIGS),
            "reb": avg("reb"), "perim": avg("def", GUARDS | WINGS), "rim": rim}


def refresh(save: dict) -> None:
    save["profiles"] = {a: {k: round(v, 1) for k, v in profile(save, a).items()} for a in cba.TEAMS}


def edge(a: dict, b: dict) -> float:
    """How much team a's style hurts team b (rating points, roughly -1.5 to +1.5)."""
    e = 0.0
    e += (a["sho"] - 60) / 10 * max(0.0, b["rim"] - b["perim"]) / 10 * 0.6   # spacing vs a drop-big defense
    e += (a["ins"] - b["rim"]) / 10 * 0.25                                  # bigs vs no rim protection
    e += (a["pla"] - b["perim"]) / 10 * 0.2                                 # creators vs weak perimeter D
    e += (a["reb"] - b["reb"]) / 10 * 0.2                                   # extra possessions
    return max(-1.5, min(1.5, e))


def matchup(save: dict, home: str, away: str) -> float:
    prof = save.get("profiles") or {}
    if home not in prof or away not in prof:
        return 0.0
    return edge(prof[home], prof[away]) - edge(prof[away], prof[home])


def identity(save: dict, abbr: str) -> str:
    p = profile(save, abbr)
    good = {"sho": "elite spacing", "pla": "great playmaking", "ins": "dominant inside", "reb": "owns the glass",
            "perim": "lockdown perimeter D", "rim": "elite rim protection"}
    bad = {"sho": "can't shoot", "pla": "no creator", "ins": "soft inside", "reb": "gets outrebounded",
           "perim": "leaky perimeter D", "rim": "no rim protection"}
    strengths = [good[k] for k, v in sorted(p.items(), key=lambda x: -x[1]) if v >= 68][:2]
    weaknesses = [bad[k] for k, v in sorted(p.items(), key=lambda x: x[1]) if v <= 48][:2]
    parts = []
    if strengths:
        parts.append("💪 " + ", ".join(strengths))
    if weaknesses:
        parts.append("⚠️ " + ", ".join(weaknesses))
    return " · ".join(parts) or "Balanced roster"
