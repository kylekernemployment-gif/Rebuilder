"""Turn real box-score production into 2K-style ratings.

Overall (OVR) is a rank-based curve over a production score that blends the last three seasons
(weighted toward the most recent, shrunk toward contract value when the sample is small, e.g.
injured stars and rookies). Potential (POT) comes from age and current level.
"""

from __future__ import annotations

import hashlib
import math

from . import cba

# (league rank by production, OVR) - calibrated to look like a 2K ratings spread
RANK_CURVE = [(1, 97), (3, 95), (6, 93), (10, 91), (18, 89), (30, 86), (50, 83), (80, 80),
              (120, 77), (170, 74), (230, 71), (300, 67), (380, 63), (460, 58), (560, 52), (700, 45)]
ATTRS = {"sco": "Scoring", "sho": "Shooting", "pla": "Playmaking", "reb": "Rebounding", "def": "Defense"}


def noise(key: str, spread: float) -> float:
    """Deterministic pseudo-random number in [-spread, spread] (same player, same answer)."""
    h = int(hashlib.sha1(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return (h * 2 - 1) * spread


def game_score(s: dict) -> float:
    g = lambda k: s.get(k, 0.0)
    return (g("pts") + 0.4 * g("fgm") - 0.7 * g("fga") - 0.4 * (g("fta") - g("ftm")) + 0.45 * g("reb")
            + g("stl") + 0.7 * g("ast") + 0.7 * g("blk") - 0.4 * g("pf") - g("tov"))


def season_impact(s: dict) -> float:
    mpg = max(s.get("mpg", 0.0), 1.0)
    gs = game_score(s)
    per36 = gs / mpg * 36
    tot_min = s.get("gp", 0) * mpg
    per36_adj = (per36 * tot_min + 9.0 * 600) / (tot_min + 600)
    return 0.55 * gs + 0.45 * per36_adj * (0.5 + 0.5 * min(1.0, mpg / 32))


def salary_impact(p: dict, season: int) -> float:
    sal = (p.get("salary") or [cba.MIN_SALARY])[0]
    ratio = max(0.0, sal / (0.35 * cba.cap_sheet(season)["cap"]))
    return 3 + 19 * math.sqrt(min(ratio, 1.05))


def weighted_stats(p: dict, season: int) -> tuple[dict, float]:
    """Production-weighted average stat line and the total sample weight (in games)."""
    hist = p.get("hist") or {}
    acc: dict[str, float] = {}
    total = 0.0
    for yr, s in hist.items():
        age_w = {0: 1.0, 1: 1.0, 2: 0.6, 3: 0.3}.get(season - int(yr), 0.0)
        if int(yr) == season:
            age_w = 1.3  # current season counts most once it has a sample
        w = age_w * s.get("gp", 0)
        if w <= 0:
            continue
        total += w
        for k, v in s.items():
            acc[k] = acc.get(k, 0.0) + v * w
    if total <= 0:
        return {}, 0.0
    return {k: v / total for k, v in acc.items()}, total


def production(p: dict, season: int) -> float:
    hist = p.get("hist") or {}
    num = den = 0.0
    for yr, s in hist.items():
        age_w = {0: 1.0, 1: 1.0, 2: 0.6, 3: 0.3}.get(season - int(yr), 0.0)
        if int(yr) == season:
            age_w = 1.3
        w = age_w * s.get("gp", 0)
        num += season_impact(s) * w
        den += w
    prior_w = 14.0
    prior = salary_impact(p, season)
    if p.get("exp", 0) <= 1 and den < 30:
        prior += 1.0  # rookies: draft slot salary undersells them a bit
    return (num + prior * prior_w) / (den + prior_w)


def ovr_from_rank(rank: int) -> float:
    pts = RANK_CURVE
    if rank <= pts[0][0]:
        return pts[0][1]
    for (r1, o1), (r2, o2) in zip(pts, pts[1:]):
        if rank <= r2:
            return o1 + (o2 - o1) * (rank - r1) / (r2 - r1)
    return max(40.0, pts[-1][1] - (rank - pts[-1][0]) * 0.03)


def potential(ovr: int, age: int, key: str) -> int:
    growth = {19: 15, 20: 13, 21: 11, 22: 8, 23: 6, 24: 4, 25: 2, 26: 1}.get(age, 0 if age > 26 else 16)
    room = max(0.0, 96 - ovr) / 30  # less room to grow near the top
    pot = ovr + growth * (0.55 + 0.6 * room) + noise(key + "pot", 3 if age < 25 else 1)
    return int(round(min(99, max(ovr, pot))))


def _pct_rank(values: dict[str, float]) -> dict[str, float]:
    order = sorted(values, key=values.get)
    n = max(1, len(order) - 1)
    return {pid: i / n for i, pid in enumerate(order)}


def attributes(players: dict[str, dict], season: int) -> None:
    raw: dict[str, dict[str, float]] = {k: {} for k in ATTRS}
    for pid, p in players.items():
        s, w = weighted_stats(p, season)
        if w <= 0:
            continue
        m = max(s.get("mpg", 1.0), 6.0)
        per = lambda k: s.get(k, 0.0) / m * 36
        tpa, fta = s.get("tpa", 0.0), s.get("fta", 0.0)
        tp_pct = s.get("tpm", 0) / tpa if tpa > 0.3 else 0.25
        ft_pct = s.get("ftm", 0) / fta if fta > 0.3 else 0.65
        fg_pct = s.get("fgm", 0) / s["fga"] if s.get("fga", 0) > 0.5 else 0.42
        raw["sco"][pid] = per("pts") * (0.7 + 0.6 * fg_pct) + s.get("pts", 0) * 0.5
        raw["sho"][pid] = tp_pct * 100 * min(1.0, per("tpa") / 6 + 0.3) + ft_pct * 25
        raw["pla"][pid] = per("ast") * 1.0 - per("tov") * 0.35 + s.get("ast", 0) * 0.4
        raw["reb"][pid] = per("reb") + s.get("reb", 0) * 0.3
        raw["def"][pid] = per("stl") * 2.2 + per("blk") * 1.8 + per("reb") * 0.15 - per("pf") * 0.2
    pct = {k: _pct_rank(v) for k, v in raw.items()}
    for pid, p in players.items():
        p["attrs"] = {}
        for k in ATTRS:
            base = pct[k].get(pid)
            val = 35 + 60 * base if base is not None else 45 + (p.get("ovr", 55) - 55) * 0.6
            p["attrs"][k] = int(max(25, min(99, round(val + noise(pid + k, 2)))))


def rate_league(players: dict[str, dict], season: int) -> None:
    prod = {pid: production(p, season) for pid, p in players.items()}
    for rank, pid in enumerate(sorted(prod, key=prod.get, reverse=True), start=1):
        p = players[pid]
        ovr = ovr_from_rank(rank) + noise(pid, 0.6)
        p["ovr"] = int(round(max(40, min(99, ovr))))
        p["pot"] = potential(p["ovr"], p.get("age", 25), pid)
        s, _ = weighted_stats(p, season)
        p["line"] = {k: round(s.get(k, 0.0), 1) for k in ("gp", "mpg", "pts", "reb", "ast", "stl", "blk")}
        if s.get("fga"):
            p["line"]["fg"] = round(100 * s.get("fgm", 0) / s["fga"], 1)
        if s.get("tpa"):
            p["line"]["tp"] = round(100 * s.get("tpm", 0) / s["tpa"], 1)
    attributes(players, season)


def grade(v: int) -> str:
    for cut, g in ((92, "A+"), (85, "A"), (80, "A-"), (75, "B+"), (70, "B"), (65, "B-"), (60, "C+"),
                   (55, "C"), (50, "C-"), (45, "D+"), (40, "D")):
        if v >= cut:
            return g
    return "F"
