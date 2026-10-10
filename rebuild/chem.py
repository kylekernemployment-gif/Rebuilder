"""Team chemistry and player morale.

Chemistry (0-100) starts from roster continuity (how many of a team's key players were together
last season) and coach tenure. A team that just won the title starts sky high. New-look teams start
lower and build it over the season, faster when they win. Trades and unhappy players cost
chemistry. Its effect is real but small: from about -2 wins for a team in chaos to about +4 for a
team that's played together for years.

Morale (0-100) tracks whether a player is happy with his minutes. Players compare their minutes
to what their level says they deserve; stars also get restless on losing teams. If morale bottoms
out, the player requests a trade. Give him his minutes back and he may calm down.
"""

from __future__ import annotations

import random

from . import cba
from . import league as L

HAPPY = 70


def _continuity(save: dict, abbr: str) -> float:
    top = L.roster(save, abbr, include_twoway=False)[:9]
    if not top:
        return 0.5
    same = sum(1 for p in top if p.get("prev_team") == abbr or (p.get("tenure", 0) >= 1))
    return same / len(top)


def target(save: dict, abbr: str) -> float:
    from . import coaches
    c = coaches.coach(save, abbr)
    tenure = max(0, save["season"] - c.get("hired", save["season"])) if c and not c.get("interim") else 0
    return 48 + 38 * _continuity(save, abbr) + min(tenure, 4) * 2


def init(save: dict, champion: str | None = None) -> None:
    save["chem"] = {}
    for abbr in cba.TEAMS:
        v = target(save, abbr)
        if abbr == champion:
            v = max(v, 92)
        save["chem"][abbr] = round(min(99.0, v), 1)
    for p in save["players"].values():
        p.setdefault("morale", HAPPY)


def value(save: dict, abbr: str) -> float:
    return float((save.get("chem") or {}).get(abbr, 60))


def bonus(save: dict, abbr: str) -> float:
    """Rotation-strength points from chemistry (about -0.6 to +1.2)."""
    return (value(save, abbr) - 60) * 0.03


def label(v: float) -> str:
    return ("🔥 Elite" if v >= 88 else "💪 Strong" if v >= 75 else "🙂 Solid" if v >= 62
            else "😐 Still gelling" if v >= 50 else "🧊 Disjointed")


def after_trade(save: dict, abbr: str, incoming: list[dict]) -> None:
    """New faces cost chemistry, more if they're going to be in the rotation."""
    if "chem" not in save:
        return
    mins = L.minutes(save, abbr)
    hit = sum(4.5 if mins.get(p["id"], 0) >= 24 else 2.0 if mins.get(p["id"], 0) >= 12 else 0.5 for p in incoming)
    save["chem"][abbr] = round(max(20.0, value(save, abbr) - hit), 1)


def expected_minutes(save: dict, abbr: str, p: dict) -> int:
    order = sorted(L.roster(save, abbr), key=lambda q: -q["ovr"])
    rank = next((i for i, q in enumerate(order) if q["id"] == p["id"]), 99)
    return L.AUTO_MIN[rank] if rank < len(L.AUTO_MIN) else 0


def weekly(save: dict, rng: random.Random, weeks: int = 1) -> list[str]:
    """Advance chemistry and morale by a number of weeks. Returns news lines."""
    if "chem" not in save:
        init(save)
    news: list[str] = []
    st = save.get("standings") or {}
    for abbr in cba.TEAMS:
        w, l = st.get(abbr, [0, 0])
        pct = w / max(1, w + l)
        tgt = target(save, abbr) + (pct - 0.5) * 24 + (8 if abbr == save.get("last_champ") else 0)
        cur = value(save, abbr)
        for _ in range(weeks):
            step = 0.9 if cur < tgt else -0.5        # bonds build faster than they fade
            cur += max(-abs(cur - tgt), min(abs(cur - tgt), step))
        unhappy = [p for p in L.roster(save, abbr) if p.get("wants_out")]
        cur -= 0.6 * len(unhappy) * weeks
        save["chem"][abbr] = round(max(20.0, min(99.0, cur)), 1)

        mins = L.minutes(save, abbr)
        for p in L.roster(save, abbr, include_twoway=False)[:12]:
            if p.get("out", 0) > 0:
                continue
            m = p.get("morale", HAPPY)
            gap = mins.get(p["id"], 0) - expected_minutes(save, abbr, p)
            ego = 1.5 if p["ovr"] >= 82 else 1.0
            for _ in range(weeks):
                if gap <= -8:
                    m -= 3.0 * ego
                elif gap <= -4:
                    m -= 1.0 * ego
                else:
                    m += 1.5 if m < HAPPY else 0
                if p["ovr"] >= 84 and p["age"] >= 26 and w + l >= 25 and pct < 0.38:
                    m -= 1.5                           # stars hate losing
            m = max(0.0, min(100.0, m))
            p["morale"] = round(m, 1)
            if not p.get("wants_out") and m <= 25:
                p["wants_out"] = True
                reason = "unhappy with his role" if gap <= -4 else "frustrated with losing"
                news.append(f"📢 {p['name']} has requested a trade from the {cba.TEAMS[abbr].name} ({reason}).")
            elif p.get("wants_out") and m >= 55:
                p["wants_out"] = False
                news.append(f"🤝 {p['name']} is happy again with the {cba.TEAMS[abbr].name}.")
    return news


def reset_after_move(p: dict) -> None:
    """A traded or signed player starts fresh with his new team."""
    p["morale"] = HAPPY
    p["wants_out"] = False
    p["tenure"] = 0


def season_rollover(save: dict, champion: str | None) -> None:
    """Between seasons: everyone who stayed gains tenure; chemistry carries over partly."""
    for p in save["players"].values():
        if p.get("team"):
            p["tenure"] = p.get("tenure", 0) + 1
        p["prev_team"] = p.get("team")
    old = dict(save.get("chem") or {})
    save["last_champ"] = champion
    init(save, champion)
    for abbr, v in old.items():  # some of last year's bond survives the offseason
        save["chem"][abbr] = round(min(99.0, 0.5 * save["chem"][abbr] + 0.5 * v + (8 if abbr == champion else 0)), 1)
