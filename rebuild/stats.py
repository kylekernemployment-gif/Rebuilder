"""Simulated box-score stats that look like what players really do.

Each player's per-36-minute rates come from his real ESPN production (rookies and draft picks get
rates from their scouting report). Every game he plays adds his minutes times those rates, scaled up
or down as his rating rises or falls, with game-to-game noise and a little season-to-season
variance. The totals become real-looking season lines, league leaders and awards, and feed back
into his history (games played also drives how injury-prone he's considered).
"""

from __future__ import annotations

import random

from . import cba
from . import league as L
from .ratings import durability, weighted_stats

KEYS = ("pts", "reb", "ast", "stl", "blk", "tpm")


def rates(p: dict, season: int) -> dict:
    if p.get("rates"):
        return p["rates"]
    s, w = weighted_stats(p, season)
    a = p.get("attrs") or {}
    if w > 0 and s.get("mpg", 0) >= 6:
        k = 36 / max(s["mpg"], 6)
        r = {key: s.get(key, 0.0) * k for key in KEYS}
        tpa = s.get("tpa", 0)
        r["tp_pct"] = (s.get("tpm", 0) / tpa) if tpa > 0.5 else 0.33
        r["fg_pct"] = (s.get("fgm", 0) / s["fga"]) if s.get("fga", 0) > 1 else 0.45
    else:  # no NBA track record: build rates from the scouting report
        g = lambda key: a.get(key, 50)
        r = {"pts": 7 + (g("sco") - 40) * 0.28, "reb": 3 + (g("reb") - 40) * 0.13, "ast": 1 + (g("pla") - 40) * 0.11,
             "stl": 0.5 + (g("def") - 40) * 0.015, "blk": 0.3 + (g("def") - 40) * 0.014,
             "tpm": max(0.2, (g("sho") - 40) * 0.05), "tp_pct": 0.30 + (g("sho") - 40) * 0.0016,
             "fg_pct": 0.42 + (g("sco") - 40) * 0.0012}
    r = {k: round(max(0.0, v), 3) for k, v in r.items()}
    p["rates"] = r
    p["rate_ovr"] = p["ovr"]
    return r


def start_season(save: dict, rng: random.Random) -> None:
    save["team_box"] = {}
    for p in save["players"].values():
        p["box"] = {"gp": 0, "min": 0.0, **{k: 0.0 for k in KEYS}}
        p["form"] = round(rng.gauss(1.0, 0.05), 3)  # slight season-to-season variance


def game_lines(save: dict, abbr: str, mins: dict[str, int], rng: random.Random) -> dict[str, dict]:
    """One game's box score lines for a team (not yet recorded)."""
    season = save["season"]
    out = {}
    for pid, m in mins.items():
        if m <= 0:
            continue
        p = save["players"][pid]
        r = rates(p, season)
        delta = p["ovr"] - p.get("rate_ovr", p["ovr"])
        scale_pts = max(0.4, 1 + 0.035 * delta) * p.get("form", 1.0)
        scale = max(0.5, 1 + 0.02 * delta)
        played = max(4.0, m * rng.uniform(0.8, 1.15))
        f = played / 36
        ln = {"min": played, "pts": max(0.0, r["pts"] * f * scale_pts * rng.gauss(1, 0.28))}
        for k in ("reb", "ast", "stl", "blk", "tpm"):
            ln[k] = max(0.0, r[k] * f * scale * rng.gauss(1, 0.3))
        out[pid] = ln
    return out


LEAGUE_NORM = {"pts": 114.0, "reb": 44.0, "ast": 27.0, "stl": 8.0, "blk": 5.0, "tpm": 13.5}


def normalize(lines: dict, own: float, opp: float, rng: random.Random) -> None:
    """Keep team totals in a real NBA range. Points follow the matchup (better team scores more,
    better defense allows less); players keep their share of the team's production."""
    if not lines:
        return
    for k, norm in LEAGUE_NORM.items():
        raw = sum(x[k] for x in lines.values())
        if raw <= 0:
            continue
        if k == "pts":
            target = norm + 0.9 * (own - opp) + rng.gauss(0, 9)
        else:
            target = norm + (raw - norm) * 0.15 + rng.gauss(0, norm * 0.08)
        f = max(0.5, target) / raw
        for x in lines.values():
            x[k] *= f


def settle_score(lines_w: dict, lines_l: dict, rng: random.Random, gap: float) -> None:
    """Make sure the winner actually outscored the loser (scale points, keep totals realistic)."""
    pw = sum(x["pts"] for x in lines_w.values()) or 1.0
    pl = sum(x["pts"] for x in lines_l.values()) or 1.0
    margin = max(1, int(abs(rng.gauss(4 + abs(gap) * 1.2, 7))))
    if pw - pl >= 1:
        return
    mid = (pw + pl) / 2
    kw, kl = (mid + margin / 2) / pw, (mid - margin / 2) / pl
    for x in lines_w.values():
        x["pts"] *= kw
    for x in lines_l.values():
        x["pts"] *= kl


def commit(save: dict, abbr: str, lines: dict, opp_pts: float) -> None:
    tb = save.setdefault("team_box", {}).setdefault(abbr, {"g": 0, "pts": 0.0, "opp": 0.0, **{k: 0.0 for k in KEYS[1:]}})
    tb["g"] += 1
    tb["opp"] += opp_pts
    for pid, ln in lines.items():
        p = save["players"][pid]
        box = p.setdefault("box", {"gp": 0, "min": 0.0, **{k: 0.0 for k in KEYS}})
        box["gp"] += 1
        box["min"] += ln["min"]
        for k in KEYS:
            box[k] += ln[k]
            tb[k] = tb.get(k, 0.0) + ln[k]


def team_line(save: dict, abbr: str) -> dict:
    tb = (save.get("team_box") or {}).get(abbr)
    if not tb or not tb["g"]:
        return {}
    g = tb["g"]
    out = {"g": g, **{k: round(tb.get(k, 0) / g, 1) for k in KEYS}, "opp": round(tb["opp"] / g, 1)}
    out["net"] = round(out["pts"] - out["opp"], 1)
    return out


def team_ranks(save: dict, key: str, reverse: bool = True) -> dict[str, int]:
    vals = {a: team_line(save, a).get(key) for a in cba.TEAMS}
    vals = {a: v for a, v in vals.items() if v is not None}
    order = sorted(vals, key=lambda a: vals[a], reverse=reverse)
    return {a: i + 1 for i, a in enumerate(order)}


def line(p: dict) -> dict:
    box = p.get("box") or {}
    gp = box.get("gp", 0)
    if not gp:
        return {}
    out = {"gp": gp, "mpg": round(box["min"] / gp, 1)}
    for k in KEYS:
        out[k] = round(box[k] / gp, 1)
    return out


def finish_season(save: dict) -> None:
    """Season lines go into each player's history (keeps the last 3 seasons)."""
    season = save["season"]
    for p in save["players"].values():
        ln = line(p)
        if not ln:
            continue
        hist = p.setdefault("hist", {})
        hist[str(season)] = {"gp": ln["gp"], "mpg": ln["mpg"], "pts": ln["pts"], "reb": ln["reb"],
                             "ast": ln["ast"], "stl": ln["stl"], "blk": ln["blk"], "sim": True}
        for yr in sorted(hist, key=int)[:-3]:
            del hist[yr]
        p["season_line"] = ln
        p["dur"] = durability(p, season + 1)
        # rates drift toward what he just did, so careers evolve naturally
        if p.get("rates") and ln["mpg"] >= 10:
            k = 36 / ln["mpg"]
            for key in ("pts", "reb", "ast", "stl", "blk"):
                p["rates"][key] = round(0.7 * p["rates"][key] + 0.3 * ln[key] * k, 3)
            p["rate_ovr"] = p["ovr"]


def leaders(save: dict, key: str, n: int = 5, min_gp_share: float = 0.5) -> list[tuple[dict, float]]:
    played = max((p.get("box", {}).get("gp", 0) for p in save["players"].values()), default=0)
    rows = []
    for p in save["players"].values():
        ln = line(p)
        if ln and ln["gp"] >= played * min_gp_share and p.get("team"):
            rows.append((p, ln[key]))
    rows.sort(key=lambda x: -x[1])
    return rows[:n]


def card_line(p: dict) -> str:
    ln = line(p)
    if not ln:
        return ""
    return (f"{ln['pts']} PTS · {ln['reb']} REB · {ln['ast']} AST · {ln['stl']} STL · {ln['blk']} BLK · "
            f"{ln['tpm']} 3PM · {ln['mpg']} MIN · {ln['gp']} GP")


def team_of(save: dict, abbr: str) -> list[dict]:
    return [p for p in L.roster(save, abbr) if line(p)]


def season_label(save: dict) -> str:
    return cba.season_label(save["season"])
