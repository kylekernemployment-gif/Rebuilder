"""A user's save: a full copy of the league that evolves as they trade and simulate."""

from __future__ import annotations

import base64
import copy
import json
import time
import zlib
from pathlib import Path

from . import cba

PICKS_PATH = Path(__file__).parent / "data" / "picks.json"
ROTATION_WEIGHTS = [0.20, 0.17, 0.15, 0.13, 0.11, 0.08, 0.06, 0.04, 0.03, 0.03]

PHASES = {
    "preseason": "Preseason",
    "season": "Regular Season",
    "deadline": "Trade Deadline",
    "stretch": "Stretch Run (trades locked)",
    "postseason": "Season Complete",
    "draft": "NBA Draft",
    "resign": "Re-signing Period",
    "fa": "Free Agency",
}


def load_traded_picks() -> list[dict]:
    try:
        return json.loads(PICKS_PATH.read_text())["traded_firsts"]
    except (OSError, ValueError, KeyError):
        return []


def pick_id(year: int, orig: str, rnd: int) -> str:
    return f"{year}-{orig}-{rnd}"


def make_picks(first_year: int, last_year: int, traded: list[dict] | None = None) -> dict[str, dict]:
    picks = {}
    for yr in range(first_year, last_year + 1):
        for abbr in cba.TEAMS:
            for rnd in (1, 2):
                pid = pick_id(yr, abbr, rnd)
                picks[pid] = {"id": pid, "year": yr, "orig": abbr, "owner": abbr, "round": rnd, "prot": 0}
    for t in traded or []:
        pid = pick_id(int(t["year"]), t["orig"], 1)
        if pid in picks and t.get("owner") in cba.TEAMS:
            picks[pid]["owner"] = t["owner"]
            picks[pid]["prot"] = int(t.get("prot") or 0)
    return picks


def new_save(live: dict, team: str, user_id: int, heat: dict | None = None) -> dict:
    season = live["season"]
    players = copy.deepcopy(live["players"])
    rosters = {abbr: [pid for pid in ids if pid in players] for abbr, ids in live["teams"].items()}
    save = {
        "v": 1,
        "user": user_id,
        "team": team,
        "season": season,
        "start_season": season,
        "phase": "preseason",
        "created": int(time.time()),
        "data_as_of": live.get("fetched", int(time.time())),
        "players": players,
        "rosters": rosters,
        "picks": make_picks(season, season + cba.PICK_YEARS - 1, load_traded_picks()),
        "fa": [],
        "heat": copy.deepcopy(heat or {}),
        "live": True,              # still on the real timeline: live rumors keep shaping AI teams
        "standings": {},
        "results": {},
        "history": [],
        "log": [],
        "news": [],
        "titles": 0,
        "used_mle": False,
        "draft": None,
        "trades": 0,
    }
    for pid in live.get("fa", []):
        if pid in players and not players[pid].get("team"):
            save["fa"].append(pid)
    trim_camp_rosters(save)
    from . import chem, coaches, draft
    import random as _r
    rng = _r.Random()
    for p in save["players"].values():   # not every young player reaches his projection
        if p["age"] <= 23:
            p["pot"] = int(max(p["ovr"], min(99, p.get("pot", p["ovr"]) + rng.gauss(-1.0, 3.5))))
    coaches.init(save)
    chem.init(save)
    draft.ensure_class(save, rng)
    return save


def trim_camp_rosters(save: dict) -> None:
    """Preseason ESPN rosters include camp invites; keep 15 standard deals + up to 3 two-ways."""
    for abbr, ids in save["rosters"].items():
        ps = [save["players"][i] for i in ids]
        standard = sorted([p for p in ps if not p.get("twoway")], key=lambda p: -p["salary"][0])
        twoway = sorted([p for p in ps if p.get("twoway")], key=lambda p: -p["ovr"])
        keep = standard[:cba.ROSTER_MAX] + twoway[:3]
        for p in standard[cba.ROSTER_MAX:] + twoway[3:]:
            p["team"] = None
            p["salary"] = []
            p.pop("twoway", None)
            save["fa"].append(p["id"])
        save["rosters"][abbr] = [p["id"] for p in keep]


# --- helpers ------------------------------------------------------------------------------

def roster(save: dict, abbr: str, include_twoway: bool = True) -> list[dict]:
    ps = [save["players"][i] for i in save["rosters"].get(abbr, []) if i in save["players"]]
    if not include_twoway:
        ps = [p for p in ps if not p.get("twoway")]
    return sorted(ps, key=lambda p: (-p["ovr"], p["name"]))


def salary_now(p: dict) -> int:
    return p["salary"][0] if p.get("salary") else 0


def payroll(save: dict, abbr: str, year_offset: int = 0) -> int:
    dead = (save.get("dead") or {}).get(abbr) or []
    total = dead[year_offset] if len(dead) > year_offset else 0
    for p in roster(save, abbr, include_twoway=False):
        sal = p.get("salary") or []
        if len(sal) > year_offset:
            total += sal[year_offset]
    return total


def standard_count(save: dict, abbr: str) -> int:
    return len(roster(save, abbr, include_twoway=False))


AUTO_MIN = [36, 34, 32, 30, 28, 24, 20, 16, 12, 8]   # how coaches split 240 minutes by default
GUARDS, BIGS = {"PG", "SG"}, {"PF", "C"}


def auto_minutes(ps: list[dict]) -> dict[str, int]:
    order = sorted(ps, key=lambda p: (-p["ovr"], p["name"]))
    return {p["id"]: (AUTO_MIN[i] if i < len(AUTO_MIN) else 0) for i, p in enumerate(order)}


def minutes(save: dict, abbr: str, healthy_only: bool = True) -> dict[str, int]:
    """Minutes per game for each player. Your team follows your lineup settings (injured players'
    minutes go to the next guys up); AI teams play their best players the most."""
    ps = [p for p in roster(save, abbr) if not (healthy_only and (p.get("out", 0) > 0 or p.get("rest")))]
    lineup = save.get("lineup") if abbr == save.get("team") else None
    if not lineup or not lineup.get("minutes"):
        return auto_minutes(ps)
    um = lineup["minutes"]
    mins = {p["id"]: int(um.get(p["id"], 0)) for p in ps}
    short = 240 - sum(mins.values())
    if short > 0:  # injuries/trades left holes: hand minutes to the best available players
        for p in sorted(ps, key=lambda p: -p["ovr"]):
            give = min(short, max(0, 34 - mins[p["id"]]))
            mins[p["id"]] += give
            short -= give
            if short <= 0:
                break
    elif short < 0:
        k = 240 / max(1, sum(mins.values()))
        mins = {pid: int(round(m * k)) for pid, m in mins.items()}
    return mins


def starters(save: dict, abbr: str) -> list[dict]:
    lineup = save.get("lineup") if abbr == save.get("team") else None
    ros = {p["id"]: p for p in roster(save, abbr) if p.get("out", 0) <= 0}
    chosen = [ros[i] for i in (lineup or {}).get("starters", []) if i in ros]
    if len(chosen) < 5:
        mins = minutes(save, abbr)
        for p in sorted(ros.values(), key=lambda p: -mins.get(p["id"], 0)):
            if p not in chosen:
                chosen.append(p)
            if len(chosen) == 5:
                break
    return chosen[:5]


def strength(save: dict, abbr: str, healthy_only: bool = False) -> float:
    """Team quality: rotation-weighted ratings, adjusted for how minutes are actually split, fatigue,
    unhappy players and a balanced starting five."""
    ps = {p["id"]: p for p in roster(save, abbr) if not (healthy_only and (p.get("out", 0) > 0 or p.get("rest")))}
    mins = minutes(save, abbr, healthy_only)
    order = sorted(ps.values(), key=lambda p: (-mins.get(p["id"], 0), -p["ovr"]))[: len(ROTATION_WEIGHTS)]
    total_w = 0.0
    acc = 0.0
    for slot, p in enumerate(order):
        m = mins.get(p["id"], 0)
        if m <= 0:
            continue
        w = ROTATION_WEIGHTS[slot] * min(m / AUTO_MIN[slot], 1.6)
        eff = p["ovr"] - max(0, m - 36) * 0.35 - (2.0 if p.get("wants_out") else 0.0)
        acc += w * eff
        total_w += w
    if total_w < 1.0:  # thin roster: empty rotation spots play like replacement-level guys
        acc += (1.0 - total_w) * 45
        total_w = 1.0
    base = acc / total_w
    five = starters(save, abbr) if (healthy_only or abbr == save.get("team")) else []
    if five and (not any(p["pos"] in GUARDS for p in five) or not any(p["pos"] in BIGS for p in five)):
        base -= 0.6
    return base


def strength_ranks(save: dict) -> dict[str, int]:
    order = sorted(cba.TEAMS, key=lambda a: -strength(save, a))
    return {a: i + 1 for i, a in enumerate(order)}


OFFSEASON = ("postseason", "draft", "resign", "fa")


def cap_year(save: dict) -> int:
    """Season whose cap applies right now (next season's once contracts roll over)."""
    return save["season"] + 1 if save["phase"] in ("draft", "resign", "fa") else save["season"]


def cap_status(save: dict, abbr: str) -> tuple[str, str]:
    """(short label, emoji) for where a team's payroll sits."""
    cap = cba.cap_sheet(cap_year(save))
    pay = payroll(save, abbr)
    if pay > cap["apron2"]:
        return "Over 2nd apron", "🟥"
    if pay > cap["apron1"]:
        return "Over 1st apron", "🟧"
    if pay > cap["tax"]:
        return "In the tax", "🟨"
    if pay > cap["cap"]:
        return "Over the cap", "🟩"
    return f"{cba.money(cap['cap'] - pay)} in cap space", "🟦"


def team_picks(save: dict, abbr: str) -> list[dict]:
    ps = [k for k in save["picks"].values() if k["owner"] == abbr]
    return sorted(ps, key=lambda k: (k["year"], k["round"], k["orig"] != abbr, k["orig"]))


def pick_label(k: dict, short: bool = False) -> str:
    rnd = "1st" if k["round"] == 1 else "2nd"
    own = f" ({k['orig']})" if k["orig"] != k["owner"] else ""
    prot = f" top-{k['prot']} prot." if k.get("prot") else ""
    if short:
        return f"{k['year']} {k['orig']} {rnd}{prot}"
    return f"{k['year']} {rnd}{own}{prot}"


def move_player(save: dict, pid: str, to: str | None) -> None:
    p = save["players"][pid]
    if p.get("team") and pid in save["rosters"].get(p["team"], []):
        save["rosters"][p["team"]].remove(pid)
    if pid in save["fa"]:
        save["fa"].remove(pid)
    p["team"] = to
    if to:
        save["rosters"][to].append(pid)
    else:
        save["fa"].append(pid)


def waive(save: dict, pid: str) -> str:
    """Release a player. His remaining guaranteed money stays on the books as dead cap."""
    p = save["players"][pid]
    team = p.get("team")
    if team and not p.get("twoway"):
        dead = save.setdefault("dead", {}).setdefault(team, [])
        for i, sal in enumerate(p.get("salary") or []):
            if i < len(dead):
                dead[i] += sal
            else:
                dead.append(sal)
    p["salary"] = []
    p.pop("twoway", None)
    move_player(save, pid, None)
    return p["name"]


def trim_roster(save: dict, abbr: str, limit: int) -> list[str]:
    """Waive the least useful players (lowest OVR, cheapest first) until the roster fits."""
    cut = []
    std = roster(save, abbr, include_twoway=False)
    while len(std) > limit:
        worst = min(std, key=lambda p: (p["ovr"] + (3 if p["age"] <= 22 else 0), salary_now(p)))
        std.remove(worst)
        cut.append(waive(save, worst["id"]))
    return cut


def add_log(save: dict, text: str, news: bool = False) -> None:
    key = "news" if news else "log"
    save[key].insert(0, text)
    del save[key][40:]


def record(save: dict, abbr: str) -> tuple[int, int]:
    w, l = save["standings"].get(abbr, [0, 0])
    return w, l


# --- storage ------------------------------------------------------------------------------

def pack(save: dict) -> str:
    raw = json.dumps(save, separators=(",", ":")).encode()
    return base64.b64encode(zlib.compress(raw, 6)).decode()


def unpack(blob: str) -> dict:
    return json.loads(zlib.decompress(base64.b64decode(blob)))
