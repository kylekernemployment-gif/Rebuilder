"""Free agency with real negotiating.

Every free agent has an asking price (based on his level, age and the market), a preferred contract
length, and opinions: he'll take less to join a contender with a good coach and a real role, and
wants more to join a losing team or sit on the bench. Your offer has to be legal (cap space,
non-taxpayer or taxpayer mid-level, minimum, or Bird rights for your own guys).

In the offseason, free agency runs in days. Stars don't jump at the first offer: they weigh yours
against what AI teams offer, and sign at the end of a day with whoever made the best one. Asking
prices drop the longer a player sits unsigned. During the season, whoever is left signs on the
spot if your offer is good enough.
"""

from __future__ import annotations

import random

from . import ai, cba, coaches
from . import league as L
from .ratings import noise

OFFSEASON = ("resign", "fa")
FA_DAYS = 7


def min_salary(save: dict) -> int:
    yr = L.cap_year(save)
    return int(cba.MIN_SALARY * (1 + cba.CAP_GROWTH) ** max(0, yr - cba.FIRST_SEASON))


def roster_limit(save: dict) -> int:
    return cba.ROSTER_MAX_OFFSEASON if save["phase"] in ("draft", "resign", "fa", "postseason") else cba.ROSTER_MAX


def pref_years(p: dict) -> int:
    a = p["age"]
    return 4 if a <= 26 else 3 if a <= 29 else 2 if a <= 32 else 1


def base_ask(save: dict, p: dict) -> int:
    """Market price for this player right now (before team-specific feelings)."""
    yr = L.cap_year(save)
    exp_ovr = p["ovr"] + (1.5 if p["age"] <= 24 else -1.0 if p["age"] >= 31 else 0)
    amt = ai.market_salary(exp_ovr, yr) * (1 + noise(p["id"] + "ask", 0.08))
    if save["phase"] not in OFFSEASON:
        amt *= 0.7           # whoever is still unsigned during the season has less leverage
    amt *= 0.93 ** save.get("fa_day", 0)
    amt = max(min_salary(save), min(int(amt), cba.max_salary(yr, p.get("exp", 0))))
    return int(round(amt, -4))


def appeal(save: dict, p: dict, team: str) -> float:
    """Multiplier on the money he needs from `team` (below 1 = discount, above 1 = premium)."""
    ranks = save.get("ranks") or L.strength_ranks(save)
    r = ranks.get(team, 15)
    young = p["age"] <= 24
    win = 0.93 if r <= 5 else 0.97 if r <= 10 else 1.02 if r <= 18 else 1.09
    if young:
        win = 1 + (win - 1) * 0.4
    c = coaches.coach(save, team)
    coach_f = 1 + (78 - coaches.rating(c)) / 250 if c else 1.02
    # role: where would he slot in that rotation?
    better = sum(1 for q in L.roster(save, team, include_twoway=False) if q["ovr"] > p["ovr"])
    role = 0.97 if better <= 2 else 1.0 if better <= 6 else 1.05 if better <= 9 else 1.1
    loyal = 0.95 if p.get("bird_team") == team else 1.0
    return win * coach_f * role * loyal


def need(save: dict, p: dict, team: str) -> int:
    return int(round(base_ask(save, p) * appeal(save, p, team), -4))


def offer_score(p: dict, amount: int, years: int) -> float:
    pref = pref_years(p)
    per_year = 0.05 if p["age"] >= 30 else 0.015   # vets really want the extra guaranteed years
    return amount * (1 + per_year * (years - pref) if years < pref else 1 + per_year * 0.3 * (years - pref))


def legal_route(save: dict, team: str, p: dict, amount: int, years: int) -> tuple[bool, str]:
    """Can `team` legally pay him this? Returns (ok, the route or the reason it can't)."""
    yr = L.cap_year(save)
    cap = cba.cap_sheet(yr)
    pay = L.payroll(save, team)
    mins = min_salary(save)
    if L.standard_count(save, team) >= roster_limit(save):
        return False, f"Your roster is full ({roster_limit(save)}). Release or trade someone first (**More → Release a player**)."
    if amount > cba.max_salary(yr, p.get("exp", 0)):
        return False, f"That's above the max for his experience ({cba.money(cba.max_salary(yr, p.get('exp', 0)))})."
    if not 1 <= years <= 5:
        return False, "Contracts run 1 to 5 years."
    if p.get("bird_team") == team:
        return True, "Bird rights (you can go over the cap to keep your own player)"
    if pay + amount <= cap["cap"]:
        return True, "cap space"
    if amount <= mins * 1.05:
        return years <= 2, "a minimum contract" if years <= 2 else "Minimum deals max out at 2 years."
    used = save.get("used_mle", {}).get(team) if isinstance(save.get("used_mle"), dict) else (
        save.get("used_mle") and team == save["team"])
    if not used:
        if amount <= cap["mle"] and pay + amount <= cap["apron1"]:
            return (years <= 4, "the non-taxpayer mid-level exception" if years <= 4
                    else "The non-taxpayer mid-level maxes out at 4 years.")
        if amount <= cap["tp_mle"] and pay + amount <= cap["apron2"]:
            return (years <= 2, "the taxpayer mid-level exception" if years <= 2
                    else "The taxpayer mid-level maxes out at 2 years.")
    space = max(0, cap["cap"] - pay)
    mle = "used" if used else (cba.money(cap["mle"]) if pay < cap["apron1"] else cba.money(cap["tp_mle"]) if pay < cap["apron2"] else "none (over the 2nd apron)")
    return False, (f"You can't fit {cba.money(amount)}: {cba.money(space)} in cap space, mid-level {mle}, "
                   f"minimum {cba.money(mins)}.")


def max_legal_offer(save: dict, team: str, p: dict) -> int:
    yr = L.cap_year(save)
    cap = cba.cap_sheet(yr)
    pay = L.payroll(save, team)
    if p.get("bird_team") == team:
        return cba.max_salary(yr, p.get("exp", 0))
    best = min_salary(save)
    best = max(best, cap["cap"] - pay)
    if not _used_mle(save, team):
        if pay + cap["mle"] <= cap["apron1"]:
            best = max(best, cap["mle"])
        elif pay + cap["tp_mle"] <= cap["apron2"]:
            best = max(best, cap["tp_mle"])
    return min(best, cba.max_salary(yr, p.get("exp", 0)))


def _used_mle(save: dict, team: str) -> bool:
    u = save.get("used_mle")
    if isinstance(u, dict):
        return bool(u.get(team))
    return bool(u) and team == save["team"]


def _mark_mle(save: dict, team: str, route: str) -> None:
    if "mid-level" in route:
        if not isinstance(save.get("used_mle"), dict):
            save["used_mle"] = {save["team"]: bool(save.get("used_mle"))}
        save["used_mle"][team] = True


def _sign(save: dict, pid: str, team: str, amount: int, years: int, route: str) -> None:
    p = save["players"][pid]
    p["salary"] = [int(amount * (1.05 ** i)) for i in range(years)]
    p.pop("est", None)
    p.pop("bird_team", None)
    L.move_player(save, pid, team)
    p["acq"] = save["season"] + (1 if save["phase"] in OFFSEASON else 0)
    # newly signed free agents can't be traded until Dec 15 (or 3 months after an in-season deal)
    p["signed"] = [p["acq"], None if save["phase"] in OFFSEASON else (save.get("game_idx") or 0)]
    _mark_mle(save, team, route)
    (save.get("pending") or {}).pop(pid, None)
    from .chem import reset_after_move
    reset_after_move(p)


def offer(save: dict, pid: str, amount: int, years: int) -> dict:
    """The user offers `amount` per year for `years`. Returns {kind, msg}."""
    p = save["players"].get(pid)
    me = save["team"]
    if not p or p.get("team"):
        return {"kind": "gone", "msg": "He's already signed somewhere."}
    if save["phase"] in ("postseason", "draft"):
        return {"kind": "closed", "msg": "Free agency opens after the draft and the re-signing period."}
    if p.get("refuses") == me:
        return {"kind": "no", "msg": f"{p['name']} has made it clear he won't come back."}
    ok, route = legal_route(save, me, p, amount, years)
    if not ok:
        return {"kind": "illegal", "msg": route}
    want = need(save, p, me)
    score = offer_score(p, amount, years)
    pref = pref_years(p)
    ask_txt = f"about **{cba.money(want)}/yr for {pref} year{'s' if pref > 1 else ''}**"
    big_name = p["ovr"] >= 80 and save["phase"] in OFFSEASON and save.get("fa_day", 0) < FA_DAYS
    if score >= want * (1.08 if big_name else 1.0):
        _sign(save, pid, me, amount, years, route)
        return {"kind": "signed",
                "msg": f"✍️ **{p['name']}** accepts: {years} yr{'s' if years > 1 else ''}, "
                       f"{cba.money(amount)}/yr ({cba.money(sum(save['players'][pid]['salary']))} total) using {route}."}
    if score >= want * 0.9 or (big_name and score >= want):
        if save["phase"] in OFFSEASON:
            save.setdefault("pending", {})[pid] = {"amount": amount, "years": years, "route": route}
            return {"kind": "pending",
                    "msg": f"🤔 **{p['name']}**'s camp is weighing your offer against other teams. He'd prefer {ask_txt}. "
                           f"Decisions come at the end of each free agency day (**Next day**)."}
        return {"kind": "low", "msg": f"💬 Close, but {p['name']} wants {ask_txt}."}
    if score < want * 0.7:
        return {"kind": "low", "msg": f"😤 {p['name']}'s agent hung up. He's looking for {ask_txt}."}
    return {"kind": "low", "msg": f"💬 Not enough. {p['name']} is looking for {ask_txt}."}


def ai_offers(save: dict, p: dict, rng: random.Random) -> list[tuple[float, str, int, int, str]]:
    """Offers AI teams make him today: (score, team, amount, years, route)."""
    out = []
    want_base = base_ask(save, p)
    yr_next = L.cap_year(save)
    for team in cba.TEAMS:
        if team == save["team"]:
            continue
        mode = ai.mode_of(save, team)
        amount = int(round(want_base * rng.uniform(0.92, 1.1), -4))
        years = pref_years(p) if rng.random() < 0.7 else max(1, pref_years(p) + rng.choice([-1, 1]))
        ok, route = legal_route(save, team, p, amount, years)
        if not ok:
            lim = max_legal_offer(save, team, p)
            if lim < want_base * 0.85:
                continue
            amount = int(round(lim, -4))
            ok, route = legal_route(save, team, p, amount, min(years, 2 if lim <= min_salary(save) * 1.05 else years))
            if not ok:
                continue
        value = ai.player_value({**p, "salary": [amount] * years}, mode, yr_next)
        if value < (20 if p["ovr"] < 75 else 60):
            continue
        score = offer_score(p, amount, years) / appeal(save, p, team)
        out.append((score, team, amount, years, route))
    out.sort(reverse=True)
    return out[:4]


def advance_day(save: dict, rng: random.Random) -> list[str]:
    """One day of free agency: AI teams make offers, players decide, unsigned asks drop."""
    me = save["team"]
    news: list[str] = []
    pending = save.setdefault("pending", {})
    pool = sorted((save["players"][i] for i in list(save["fa"]) if i in save["players"]), key=lambda p: -p["ovr"])
    for p in pool[:80]:
        if p.get("team"):
            continue
        offers = ai_offers(save, p, rng)
        mine = pending.get(p["id"])
        if mine:
            ok, route = legal_route(save, me, p, mine["amount"], mine["years"])
            if ok:
                offers.append((offer_score(p, mine["amount"], mine["years"]) / appeal(save, p, me) * 1.01,
                               me, mine["amount"], mine["years"], route))
            else:
                pending.pop(p["id"], None)
                news.append(f"⚠️ Your offer to {p['name']} no longer fits under the cap and was withdrawn.")
        if not offers:
            continue
        offers.sort(reverse=True)
        score, team, amount, years, route = offers[0]
        threshold = base_ask(save, p) * (0.97 - 0.03 * save.get("fa_day", 0))
        if score < threshold:
            continue  # nobody met his price today; he waits (and his price drops)
        _sign(save, p["id"], team, amount, years, route)
        total = cba.money(sum(save["players"][p["id"]]["salary"]))
        if team == me:
            news.insert(0, f"✅ **{p['name']}** accepted your offer: {years} yrs, {total}.")
        else:
            if mine:
                news.insert(0, f"❌ **{p['name']}** chose the {cba.TEAMS[team].name} over you ({years} yrs, {total}).")
            elif p["ovr"] >= 77:
                news.append(f"✍️ {p['name']} signs with the {cba.TEAMS[team].name}: {years} yrs, {total}.")
    save["fa_day"] = save.get("fa_day", 0) + 1
    for line in news[:10]:
        if not line.startswith(("✅", "❌", "⚠️")):
            L.add_log(save, line, news=True)
    ai.refresh_modes(save)
    return news


def release(save: dict, pid: str) -> str:
    p = save["players"].get(pid)
    if not p or p.get("team") != save["team"]:
        return "That's not one of your players."
    dead = sum(p.get("salary") or []) if not p.get("twoway") else 0
    name = L.waive(save, pid)
    save["fa"] = list(dict.fromkeys(save["fa"]))
    return (f"✂️ Released **{name}**." + (f" {cba.money(dead)} stays on your books as dead money." if dead else ""))


# ------------------------------------------------------------------------------------------
# Contract extensions
# ------------------------------------------------------------------------------------------

def extension_eligible(save: dict, p: dict) -> bool:
    """Players in the last year of their deal (rookie-scale or veteran) can be extended."""
    return (p.get("team") == save["team"] and not p.get("twoway") and len(p.get("salary") or []) == 1
            and save["phase"] not in ("draft", "resign", "fa"))


def extension_ask(save: dict, p: dict) -> dict:
    yr = save["season"] + 1
    exp_ovr = p["ovr"] + (2.0 if p["age"] <= 23 else 1.0 if p["age"] <= 25 else -1.0 if p["age"] >= 31 else 0)
    amt = ai.market_salary(exp_ovr, yr) * (1 + noise(p["id"] + "ext", 0.07)) * 0.97  # small loyalty discount
    amt = max(min_salary(save), min(int(amt), cba.max_salary(yr, p.get("exp", 0) + 1)))
    ranks = save.get("ranks") or L.strength_ranks(save)
    wants_fa = (p["ovr"] >= 84 and p["age"] >= 25 and ranks.get(save["team"], 15) >= 20
                and noise(p["id"] + str(save["season"]) + "fa", 1) > -0.2)
    refuses = bool(p.get("wants_out")) or p.get("morale", 70) < 40 or wants_fa
    reason = ("He wants out." if p.get("wants_out") else "He's unhappy with his role." if p.get("morale", 70) < 40
              else "He wants to test free agency." if wants_fa else "")
    return {"amount": int(round(amt, -4)), "years": min(5, pref_years(p) + (1 if p["age"] <= 25 else 0)),
            "refuses": refuses, "reason": reason}


def extend(save: dict, pid: str, amount: int, years: int) -> dict:
    p = save["players"].get(pid)
    if not p or not extension_eligible(save, p):
        return {"kind": "no", "msg": "He isn't eligible for an extension right now (only players in the last year of their deal)."}
    ask = extension_ask(save, p)
    if ask["refuses"]:
        return {"kind": "no", "msg": f"❌ {p['name']} won't discuss an extension. {ask['reason']}"}
    yr = save["season"] + 1
    if amount > cba.max_salary(yr, p.get("exp", 0) + 1):
        return {"kind": "no", "msg": f"That's above his max ({cba.money(cba.max_salary(yr, p.get('exp', 0) + 1))})."}
    if not 1 <= years <= 5:
        return {"kind": "no", "msg": "Extensions run 1 to 5 extra years."}
    pref = ask["years"]
    score = offer_score({**p, "age": p["age"]}, amount, years)
    target = ask["amount"] * (1 + (0.0 if years >= pref else 0.03 * (pref - years)))
    if score >= target:
        p["salary"] = p["salary"][:1] + [int(amount * (1.05 ** i)) for i in range(years)]
        p.pop("est", None)
        p["morale"] = min(100, p.get("morale", 70) + 10)
        L.add_log(save, f"✍️ {cba.TEAMS[save['team']].name} extended {p['name']}: {years} yrs, "
                        f"{cba.money(sum(p['salary'][1:]))}", news=True)
        return {"kind": "signed", "msg": f"✍️ **{p['name']}** signs the extension: {years} more year{'s' if years > 1 else ''}, "
                                         f"{cba.money(amount)}/yr starting next season."}
    return {"kind": "low", "msg": f"💬 {p['name']}'s camp wants about **{cba.money(ask['amount'])}/yr for {pref} years**."}
