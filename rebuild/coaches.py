"""Head coaches: real current coaches and real coaches without a job. Coaches add a little to (or
take a little from) their team's results and to how fast young players develop. You can fire
yours and hire anyone available; AI teams run their own coaching carousel each offseason."""

from __future__ import annotations

import json
import random
from pathlib import Path

from . import cba
from . import league as L
from .ratings import noise

PATH = Path(__file__).parent / "data" / "coaches.json"


def load() -> list[dict]:
    try:
        return json.loads(PATH.read_text())["coaches"]
    except (OSError, ValueError, KeyError):
        return []


def rating(c: dict) -> int:
    return int(round(0.4 * c["off"] + 0.4 * c["def"] + 0.2 * c["dev"]))


def init(save: dict) -> None:
    coaches = {}
    for c in load():
        coaches[c["name"]] = {**c, "w": 0, "l": 0, "titles": 0, "seasons": 0, "hired": save["season"]}
    save["coaches"] = coaches
    save["coach_of"] = {c["team"]: c["name"] for c in coaches.values() if c.get("team") in cba.TEAMS}
    for abbr in cba.TEAMS:  # any team missing from the file gets an interim
        if abbr not in save["coach_of"]:
            interim(save, abbr)


def coach(save: dict, abbr: str) -> dict | None:
    if "coaches" not in save:
        init(save)
    name = save["coach_of"].get(abbr)
    return save["coaches"].get(name) if name else None


def team_bonus(save: dict, abbr: str) -> float:
    """Rotation-strength points a coach is worth (roughly -1 to +1.7, about -4 to +6 wins)."""
    c = coach(save, abbr)
    return (rating(c) - 78) * 0.12 if c else -0.6


def dev_bonus(save: dict, abbr: str | None) -> float:
    c = coach(save, abbr) if abbr else None
    return (c["dev"] - 78) / 12 if c else 0.0


def available(save: dict) -> list[dict]:
    if "coaches" not in save:
        init(save)
    return sorted((c for c in save["coaches"].values() if not c.get("team") and not c.get("interim")),
                  key=lambda c: -rating(c))


def interim(save: dict, abbr: str) -> dict:
    name = f"{cba.TEAMS[abbr].name} interim coach"
    c = {"name": name, "team": abbr, "off": 72, "def": 72, "dev": 74, "age": 45, "interim": True,
         "w": 0, "l": 0, "titles": 0, "seasons": 0, "hired": save["season"],
         "note": "Top assistant filling in"}
    save["coaches"][name] = c
    save["coach_of"][abbr] = name
    return c


def fire(save: dict, abbr: str) -> str:
    c = coach(save, abbr)
    if not c:
        return "There's no coach to fire."
    if c.get("interim"):
        del save["coaches"][c["name"]]
        interim(save, abbr)
        return "The interim coach is already just filling in. Hire a real coach to replace him."
    c["team"] = None
    c["note"] = f"Fired by {cba.TEAMS[abbr].name} ({c['w']}-{c['l']} with the team)"
    interim(save, abbr)
    L.add_log(save, f"🔥 {cba.TEAMS[abbr].name} fired {c['name']}", news=True)
    return f"🔥 You fired **{c['name']}** ({c['w']}-{c['l']} with the team). An interim coach takes over until you hire someone."


def would_accept(save: dict, abbr: str, c: dict) -> tuple[bool, str]:
    from . import ai
    mode = ai.mode_of(save, abbr)
    if rating(c) >= 84 and mode == "rebuild" and noise(c["name"] + abbr + str(save["season"]), 1) > -0.2:
        return False, f"{c['name']} wants to coach a team that can win now, not a rebuild."
    if c.get("age", 50) >= 66 and mode == "rebuild":
        return False, f"{c['name']} isn't interested in a long rebuild at this stage of his career."
    return True, ""


def hire(save: dict, abbr: str, name: str) -> tuple[bool, str]:
    c = save["coaches"].get(name)
    if not c or c.get("team"):
        return False, "He's not available anymore."
    ok, why = would_accept(save, abbr, c)
    if not ok:
        return False, f"❌ {why}"
    old = coach(save, abbr)
    if old and old.get("interim"):
        del save["coaches"][old["name"]]
    elif old:
        old["team"] = None
        old["note"] = f"Replaced by {cba.TEAMS[abbr].name}"
    c["team"] = abbr
    c["hired"] = save["season"]
    c["w"] = c["l"] = 0
    c.pop("note", None)
    save["coach_of"][abbr] = name
    L.add_log(save, f"🧑‍💼 {cba.TEAMS[abbr].name} hired {name} as head coach", news=True)
    return True, f"✅ **{name}** is your new head coach ({rating(c)} overall)."


def record_game(save: dict, winner: str, loser: str) -> None:
    for abbr, won in ((winner, True), (loser, False)):
        c = coach(save, abbr)
        if c:
            c["w" if won else "l"] += 1


def end_of_season(save: dict, champ: str) -> None:
    for abbr in cba.TEAMS:
        c = coach(save, abbr)
        if c:
            c["seasons"] = c.get("seasons", 0) + 1
            if abbr == champ:
                c["titles"] = c.get("titles", 0) + 1


def carousel(save: dict, rng: random.Random) -> list[str]:
    """Offseason firings and hirings around the league (not your team)."""
    news = []
    st = save.get("standings") or {}
    expect = save.get("preseason_ranks") or save.get("ranks") or {}
    for abbr in cba.TEAMS:
        if abbr == save["team"]:
            continue
        c = coach(save, abbr)
        w, l = st.get(abbr, [41, 41])
        exp_w = 61 - (expect.get(abbr, 15) - 1) * 1.4
        under = exp_w - w
        if c.get("interim"):
            fire_it = True
        else:
            tenure = save["season"] - c.get("hired", save["season"])
            p = 0.0
            if under > 10:
                p += 0.45
            if w < 28 and tenure >= 2:
                p += 0.3
            if (save.get("playoffs") or {}).get("result", {}).get(abbr, "").startswith("Lost in First") and under > 4:
                p += 0.15
            fire_it = rng.random() < p
        if not fire_it:
            continue
        old = c["name"]
        if not c.get("interim"):
            fire(save, abbr)
        pool = [x for x in available(save) if would_accept(save, abbr, x)[0]][:4]
        if pool:
            pick = rng.choice(pool)
            hire(save, abbr, pick["name"])
            if not c.get("interim"):
                news.append(f"🔁 {cba.TEAMS[abbr].name} fired {old} and hired {pick['name']}")
            else:
                news.append(f"🧑‍💼 {cba.TEAMS[abbr].name} hired {pick['name']}")
    for c in list(save["coaches"].values()):  # old unemployed coaches retire
        if not c.get("team") and c.get("age", 50) >= 70 and rng.random() < 0.3:
            del save["coaches"][c["name"]]
    for c in save["coaches"].values():
        c["age"] = c.get("age", 50) + 1
    return news
