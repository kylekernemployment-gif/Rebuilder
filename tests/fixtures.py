"""A fake but realistic league built from ESPN-shaped JSON, so tests run without the network."""

from __future__ import annotations

import random

from rebuild import cba
from rebuild.data import build_league, parse_roster, parse_stats_page

OFF = ["avgPoints", "avgFieldGoalsMade", "avgFieldGoalsAttempted", "fieldGoalPct", "avgThreePointFieldGoalsMade",
       "avgThreePointFieldGoalsAttempted", "threePointFieldGoalPct", "avgFreeThrowsMade", "avgFreeThrowsAttempted",
       "freeThrowPct", "avgAssists", "avgTurnovers"]
GEN = ["gamesPlayed", "avgMinutes", "avgFouls", "flagrantFouls", "technicalFouls", "ejections", "doubleDouble",
       "tripleDouble", "minutes", "rebounds", "fouls", "avgRebounds"]
DEF = ["avgSteals", "avgBlocks", "steals", "blocks"]


def fake_espn(seed: int = 7, season: int = 2027):
    rng = random.Random(seed)
    rosters_json = {}
    stat_rows = []
    pid = 1000
    for abbr in cba.TEAMS:
        team_q = rng.gauss(0, 1)
        athletes = []
        for i in range(17):
            pid += 1
            talent = max(0.0, rng.gauss(1.0 - i * 0.07 + team_q * 0.08, 0.25))
            age = rng.randint(19, 36)
            sal = int(max(1_200_000, min(57_000_000, talent ** 2.2 * 30_000_000 + rng.gauss(0, 3e6))))
            if i >= 15:
                sal = 800_000  # camp / two-way
            yrs = rng.randint(1, 5)
            pos = rng.choice(["G", "F", "C", "PG", "SG", "SF", "PF", "G-F", "F-C"])
            athletes.append({
                "id": str(pid), "displayName": f"Player {abbr}{i}", "age": age, "jersey": str(i),
                "experience": {"years": max(0, age - 20)}, "position": {"abbreviation": pos},
                "status": {"type": "active"}, "injuries": [],
                "contract": {"salary": sal, "yearsRemaining": yrs},
                "contracts": [{"salary": int(sal * 1.05 ** k), "season": {"year": season + k}} for k in range(yrs)],
            })
            mpg = max(4.0, min(37.0, 8 + talent * 24 + rng.gauss(0, 3)))
            pts = max(1.0, talent * 22 * mpg / 34 + rng.gauss(0, 2))
            fga = pts / 2.2
            for yr in (season - 2, season - 1):
                gp = rng.randint(10, 82)
                stat_rows.append((yr, {
                    "athlete": {"id": str(pid)},
                    "categories": [
                        {"name": "general", "values": [gp, mpg, 2.0, 0, 0, 0, 0, 0, gp * mpg, 0, 0, 3 + talent * 6]},
                        {"name": "offensive", "values": [pts, fga * 0.47, fga, 47, fga * 0.12, fga * 0.34, 36,
                                                         pts * 0.18, pts * 0.23, 78, 1 + talent * 5, 1 + talent * 2]},
                        {"name": "defensive", "values": [0.4 + talent * 0.9, 0.2 + talent * 0.8, 0, 0]},
                    ]}))
        rosters_json[abbr] = {"athletes": athletes}
    cats = [{"name": "general", "names": GEN}, {"name": "offensive", "names": OFF + ["x"] * 9},
            {"name": "defensive", "names": DEF}]
    stats = {}
    for yr in (season - 2, season - 1):
        page = {"categories": cats, "athletes": [r for y, r in stat_rows if y == yr], "pagination": {"pages": 1}}
        stats[yr] = parse_stats_page(page)[0]
    rosters = {a: parse_roster(j, a, season) for a, j in rosters_json.items()}
    return build_league(rosters, stats, season)
