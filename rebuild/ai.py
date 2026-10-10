"""AI front offices: how each team values players and picks, and how it negotiates.

Every AI team has a direction (contending / middle / rebuilding) from its roster strength, age
and, while a save is still on the real timeline, the live rumor feed. A player's value depends on
who is asking: contenders pay for production now, rebuilders pay for youth, potential and picks,
and everyone cares about contracts (cheap good players are gold, bad money is a liability).
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field

from . import cba
from . import league as L

MODES = {"contend": "🏆 Contending", "middle": "⚖️ Middle of the pack", "rebuild": "🌱 Rebuilding"}
PICK_CURVE = [(1, 900), (2, 760), (3, 650), (5, 500), (8, 380), (10, 300), (14, 220), (20, 140), (25, 105), (30, 80)]
SALARY_CURVE = [(50, 1.0), (60, 2.4), (65, 4.0), (70, 8.0), (74, 13.0), (77, 18.5), (80, 26.0), (83, 34.0),
                (86, 42.0), (89, 50.0), (92, 57.7)]  # $M in 2026-27 dollars
DIMINISH = [1.0, 0.8, 0.62, 0.48, 0.38, 0.3]


def _interp(curve, x):
    if x <= curve[0][0]:
        return curve[0][1]
    for (x1, y1), (x2, y2) in zip(curve, curve[1:]):
        if x <= x2:
            return y1 + (y2 - y1) * (x - x1) / (x2 - x1)
    return curve[-1][1]


def market_salary(ovr: float, season: int) -> int:
    k = (1 + cba.CAP_GROWTH) ** max(0, season - cba.FIRST_SEASON)
    return int(_interp(SALARY_CURVE, ovr) * 1_000_000 * k)


def curve_value(e: float) -> float:
    return 100 * math.exp((e - 75) / 6)


# ------------------------------------------------------------------------------------------
# Team direction
# ------------------------------------------------------------------------------------------

def refresh_modes(save: dict) -> dict[str, str]:
    ranks = L.strength_ranks(save)
    team_heat = (save.get("heat") or {}).get("teams", {}) if save.get("live") else {}
    modes = {}
    for abbr in cba.TEAMS:
        r = ranks[abbr]
        top8 = L.roster(save, abbr)[:8]
        avg_age = sum(p["age"] for p in top8) / max(1, len(top8))
        mode = "contend" if r <= 9 else ("rebuild" if r >= 21 else "middle")
        if mode == "middle" and avg_age <= 24.5 and r >= 15:
            mode = "rebuild"
        if mode == "middle" and r <= 12 and avg_age >= 28:
            mode = "contend"
        lean = (team_heat.get(abbr) or {}).get("lean", 0)
        order = ["contend", "middle", "rebuild"]
        i = order.index(mode)
        if lean > 0.35:
            i = min(2, i + 1)
        elif lean < -0.35:
            i = max(0, i - 1)
        modes[abbr] = order[i]
    save["modes"] = modes
    save["ranks"] = ranks
    return modes


def mode_of(save: dict, abbr: str) -> str:
    if "modes" not in save:
        refresh_modes(save)
    return save["modes"].get(abbr, "middle")


# ------------------------------------------------------------------------------------------
# Valuation
# ------------------------------------------------------------------------------------------

def upside(p: dict) -> float:
    """What a young player projects to become (top picks and high-potential kids carry real value)."""
    ovr, pot, age = p["ovr"], p.get("pot", p["ovr"]), p["age"]
    if age > 25:
        return ovr
    # The further away the ceiling, the more it's discounted for risk.
    return max(ovr, pot - 0.45 * max(0, pot - ovr) * (0.6 if age <= 21 else 0.8))


def effective_ovr(p: dict, mode: str) -> float:
    ovr, age = p["ovr"], p["age"]
    now = ovr - 0.9 * max(0, age - 30)          # vets decline: 31+ lose value every year
    up = upside(p)
    if mode == "contend":
        e = 0.75 * now + 0.25 * max(now, up)
    elif mode == "rebuild":
        e = 0.3 * now + 0.7 * max(now, up) - 1.4 * max(0, age - 27)
    else:
        e = 0.55 * now + 0.45 * max(now, up) - 0.5 * max(0, age - 30)
    if p.get("out", 0) > 40:
        e -= 2.5
    return e


def player_value(p: dict, mode: str, season: int, cap_room: bool = False) -> float:
    """Trade value to a team in `mode`. Bad money counts against a player in full, unless the team
    has cap room to absorb it (that's how salary dumps work: a team with space takes the contract
    if it's paid in picks or young players)."""
    if p.get("twoway"):
        return max(2.0, curve_value(effective_ovr(p, mode)) * 0.5)
    e = effective_ovr(p, mode)
    base = curve_value(e)
    if p["age"] <= 22 and p.get("pot", 0) >= 85:
        base *= 1.15                              # blue-chip prospect premium
    dur = p.get("dur", 0.85)
    base *= max(0.82, min(1.03, 0.8 + 0.2 * dur / 0.85))   # injury-prone players are worth a bit less
    k = {"contend": 1.6, "middle": 2.0, "rebuild": 2.4}[mode]
    contract = 0.0
    for i, sal in enumerate(p.get("salary") or []):
        proj = p["ovr"] - (max(0, p["age"] + i - 29) * 1.5) + (min(i, 4) * 1.5 if p["age"] + i <= 24 else 0)
        if p["age"] + i <= 25:
            proj = max(proj, min(upside(p), p["ovr"] + 2.5 * (i + 1)))
        surplus_m = (market_salary(proj, season + i) - sal) / 1_000_000
        w = k * (0.85 ** i)
        if surplus_m < 0:
            w *= 1.45                             # overpaid contracts are hard to move
            if cap_room and i == 0:
                w *= 0.45                         # ...unless you have unused space this season
        contract += surplus_m * w
    return max(-600.0, base + contract)


def expected_slot(save: dict, k: dict) -> float:
    if save.get("draft") and k["year"] == save["season"]:
        order = save["draft"].get("order") or []
        if k["orig"] in order:
            return order.index(k["orig"]) + 1
    ranks = save.get("ranks") or L.strength_ranks(save)
    if k["year"] == save["season"] and save["phase"] in ("postseason", "draft"):
        st = save.get("standings") or {}
        worst = sorted(cba.TEAMS, key=lambda a: (st.get(a, [0, 0])[0], ranks[a] * -1))
        return worst.index(k["orig"]) + 1
    slot_now = 31 - ranks[k["orig"]]
    years_out = k["year"] - save["season"]
    if save["phase"] in ("season", "deadline", "stretch") and years_out == 0:
        return 15.5 + (slot_now - 15.5) * 0.92
    return 15.5 + (slot_now - 15.5) * (0.65 ** (years_out + 0.3))


def pick_value(save: dict, k: dict, mode: str) -> float:
    slot = expected_slot(save, k)
    if k["round"] == 2:
        base = 18 + (30 - slot) * 0.5
    else:
        base = _interp(PICK_CURVE, slot)
        if k.get("prot"):
            base *= 0.3 if slot <= k["prot"] + 1.5 else 0.85
    return base * {"contend": 0.75, "middle": 1.0, "rebuild": 1.3}[mode]


def heat_of(save: dict, pid: str) -> dict:
    p = save["players"].get(pid) or {}
    if p.get("wants_out"):
        return {"h": 0.9, "label": "Trade request", "reporter": "in your league", "tier": 1}
    if not save.get("live"):
        return {}
    return ((save.get("heat") or {}).get("players") or {}).get(pid) or {}


def reluctance(save: dict, abbr: str, p: dict) -> float:
    ros = L.roster(save, abbr)
    idx = next((i for i, q in enumerate(ros) if q["id"] == p["id"]), 99)
    mode = mode_of(save, abbr)
    r = 0.08
    if idx == 0 and p["ovr"] >= 84:
        r += 0.35
    elif idx <= 1 and p["ovr"] >= 80:
        r += 0.15
    if mode == "rebuild" and p["age"] <= 24 and p.get("pot", 0) >= 84:
        r += 0.3
    if mode == "rebuild" and p["age"] >= 29:
        r -= 0.15
    if mode == "contend" and idx <= 7:
        r += 0.08
    if p.get("acq") == save["season"]:
        r += 0.1
    r -= 0.35 * heat_of(save, p["id"]).get("h", 0.0)
    return max(-0.25, min(0.85, r))


UNTOUCHABLE = 0.7
APRON_TOLERANCE = 1_000_000   # house rule: above the first apron, incoming salary may exceed outgoing by $1M


# ------------------------------------------------------------------------------------------
# Trades
# ------------------------------------------------------------------------------------------

@dataclass
class Trade:
    a: str                       # user's team (or the initiating team)
    b: str                       # the other team
    a_out: list[str] = field(default_factory=list)   # asset ids: "p:<id>" / "k:<pick id>"
    b_out: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "a_out": list(self.a_out), "b_out": list(self.b_out)}

    @classmethod
    def from_dict(cls, d: dict) -> "Trade":
        return cls(d["a"], d["b"], list(d["a_out"]), list(d["b_out"]))

    def copy(self) -> "Trade":
        return Trade(self.a, self.b, list(self.a_out), list(self.b_out))


def asset_label(save: dict, aid: str, with_salary: bool = True) -> str:
    kind, key = aid.split(":", 1)
    if kind == "p":
        p = save["players"][key]
        sal = f" · {cba.money(L.salary_now(p))}" if with_salary and not p.get("twoway") else ""
        return f"{p['name']} ({p['pos']}, {p['ovr']} OVR{sal})"
    k = save["picks"][key]
    return L.pick_label(k, short=True)


def _split(assets: list[str]) -> tuple[list[str], list[str]]:
    return [a[2:] for a in assets if a.startswith("p:")], [a[2:] for a in assets if a.startswith("k:")]


def _salary(save: dict, pids: list[str]) -> int:
    return sum(L.salary_now(save["players"][p]) for p in pids if not save["players"][p].get("twoway"))


def roster_limit(save: dict) -> int:
    return cba.ROSTER_MAX_OFFSEASON if save["phase"] in ("postseason", "draft", "resign", "fa") else cba.ROSTER_MAX


def validate(save: dict, t: Trade) -> list[str]:
    problems: list[str] = []
    if t.a == t.b:
        return ["A team can't trade with itself."]
    if save.get("phase") == "stretch":
        return ["The trade deadline has passed. Trades reopen after the playoffs."]
    if save.get("phase") == "draft":
        return ["Trades are paused during the draft. Finish it with /rebuild sim."]
    if not t.a_out and not t.b_out:
        return ["Add something to the trade first."]
    if not t.a_out or not t.b_out:
        problems.append("Both teams have to send something (no straight giveaways).")
    cap = cba.cap_sheet(L.cap_year(save))
    sides = {t.a: (t.a_out, t.b_out), t.b: (t.b_out, t.a_out)}
    for team, (out, inc) in sides.items():
        name = cba.TEAMS[team].name
        out_p, out_k = _split(out)
        in_p, in_k = _split(inc)
        for pid in out_p:
            if save["players"].get(pid, {}).get("team") != team:
                problems.append(f"{name} no longer have that player.")
        for kid in out_k:
            if save["picks"].get(kid, {}).get("owner") != team:
                problems.append(f"{name} don't own that pick.")
        out_sal, in_sal = _salary(save, out_p), _salary(save, in_p)
        before = L.payroll(save, team)
        after = before - out_sal + in_sal
        std_after = L.standard_count(save, team) - sum(1 for p in out_p if not save["players"][p].get("twoway")) \
            + sum(1 for p in in_p if not save["players"][p].get("twoway"))
        if std_after > roster_limit(save) + 3:
            problems.append(f"{name} would have {std_after} players (max {roster_limit(save)}). Send back more players.")
        if after > cap["cap"] and in_sal > out_sal:
            if after > cap["apron1"]:
                if in_sal > out_sal + APRON_TOLERANCE:
                    problems.append(f"{name} would finish above the first apron ({cba.money(after)}), so they can take "
                                    f"back at most {cba.money(APRON_TOLERANCE)} more than they send "
                                    f"({cba.money(in_sal)} in vs {cba.money(out_sal)} out).")
            else:
                allowed = cba.max_incoming(out_sal, L.cap_year(save))
                if before < cap["cap"]:
                    allowed = max(allowed, out_sal + (cap["cap"] - before))
                if in_sal > allowed:
                    problems.append(f"Salary matching: {name} can take back at most {cba.money(allowed)} "
                                    f"for {cba.money(out_sal)} outgoing (getting {cba.money(in_sal)}).")
        if after > cap["apron2"] and len([p for p in out_p if not save["players"][p].get("twoway")]) > 1 and in_sal > 0:
            problems.append(f"{name} would be over the second apron, where teams can't combine salaries in a trade.")
        problems += stepien(save, team, out_k, in_k)
    return list(dict.fromkeys(problems))


def stepien(save: dict, team: str, out_k: list[str], in_k: list[str]) -> list[str]:
    """Stepien rule: a team can't be without a first-round pick in consecutive future drafts."""
    if not any(save["picks"][k]["round"] == 1 for k in out_k if k in save["picks"]):
        return []
    owned = {k["year"] for k in save["picks"].values()
             if k["round"] == 1 and ((k["owner"] == team and k["id"] not in out_k) or k["id"] in in_k)}
    first = save["season"] + (1 if save["phase"] not in ("preseason", "deadline") else 0)
    years = sorted({k["year"] for k in save["picks"].values() if k["year"] >= first})
    for y1, y2 in zip(years, years[1:]):
        if y1 not in owned and y2 not in owned and any(
                save["picks"][k]["year"] in (y1, y2) and save["picks"][k]["round"] == 1 for k in out_k):
            return [f"Stepien rule: {cba.TEAMS[team].name} can't be without a 1st-round pick in both {y1} and {y2}."]
    return []


def _tax_penalty(save: dict, team: str, payroll: int) -> float:
    cap = cba.cap_sheet(L.cap_year(save))
    over = max(0, payroll - cap["tax"]) / 1_000_000
    over2 = max(0, payroll - cap["apron2"]) / 1_000_000
    k = {"contend": 0.9, "middle": 2.4, "rebuild": 3.5}[mode_of(save, team)]
    return over * k + over2 * 4


@dataclass
class Evaluation:
    gain: float
    value_in: float
    value_out: float
    untouchable: list[str]

    @property
    def ratio(self) -> float:
        if self.value_out <= 0:
            return 2.0 if self.value_in >= self.value_out else 0.0
        return self.value_in / self.value_out

    @property
    def meter(self) -> str:
        if self.untouchable:
            return "🚫 Not discussing"
        r = 1.0 if self.gain >= 0 else self.ratio
        if r >= 1.0:
            return "🟢 They'd accept"
        if r >= 0.88:
            return "🟡 Close"
        if r >= 0.65:
            return "🟠 Far apart"
        return "🔴 Not interested"


def evaluate(save: dict, team: str, incoming: list[str], outgoing: list[str]) -> Evaluation:
    """How `team` (an AI) feels about receiving `incoming` for `outgoing`."""
    mode = mode_of(save, team)
    season = save["season"]
    in_p, in_k = _split(incoming)
    out_p, out_k = _split(outgoing)
    before_pay = L.payroll(save, team)
    room_after = before_pay - _salary(save, out_p) + _salary(save, in_p) <= cba.cap_sheet(L.cap_year(save))["cap"]
    pv = sorted((player_value(save["players"][p], mode, season, cap_room=room_after) for p in in_p), reverse=True)
    std_after = L.standard_count(save, team) - len(out_p) + len(in_p)
    overflow = max(0, std_after - roster_limit(save))
    value_in = 0.0
    for i, v in enumerate(pv):
        w = DIMINISH[min(i, len(DIMINISH) - 1)] if v > 0 else 1.0
        if overflow and i >= len(pv) - overflow and v > 0:
            w = 0.1
        value_in += v * w
    for i, kid in enumerate(sorted(in_k, key=lambda x: -pick_value(save, save["picks"][x], mode))):
        value_in += pick_value(save, save["picks"][kid], mode) * (0.92 ** i)
    value_out = 0.0
    untouchable = []
    for pid in out_p:
        p = save["players"][pid]
        rel = reluctance(save, team, p)
        if rel >= UNTOUCHABLE:
            untouchable.append(p["name"])
        v = player_value(p, mode, season)
        value_out += v * (1 + rel) if v > 0 else v
    for kid in out_k:
        value_out += pick_value(save, save["picks"][kid], mode) * 1.08
    before = L.payroll(save, team)
    after = before - _salary(save, out_p) + _salary(save, in_p)
    tax = _tax_penalty(save, team, after) - _tax_penalty(save, team, before)
    return Evaluation(value_in - value_out - tax, value_in, value_out + max(0.0, tax), untouchable)


def apply_trade(save: dict, t: Trade, news: bool = False) -> str:
    from . import chem
    arrivals: dict[str, list[dict]] = {t.a: [], t.b: []}
    for team, out in ((t.a, t.a_out), (t.b, t.b_out)):
        other = t.b if team == t.a else t.a
        for aid in out:
            kind, key = aid.split(":", 1)
            if kind == "p":
                L.move_player(save, key, other)
                save["players"][key]["acq"] = save["season"]
                chem.reset_after_move(save["players"][key])
                arrivals[other].append(save["players"][key])
            else:
                save["picks"][key]["owner"] = other
    if save.get("lineup") and any(arrivals.values()):
        mine = {p["id"] for p in L.roster(save, save["team"])}
        save["lineup"]["minutes"] = {k: v for k, v in save["lineup"]["minutes"].items() if k in mine}
        save["lineup"]["starters"] = [k for k in save["lineup"].get("starters", []) if k in mine]
    for team, ps in arrivals.items():
        chem.after_trade(save, team, ps)
    a_names = ", ".join(asset_label(save, x, False) for x in t.a_out)
    b_names = ", ".join(asset_label(save, x, False) for x in t.b_out)
    text = f"{cba.TEAMS[t.a].abbr} get {b_names} · {cba.TEAMS[t.b].abbr} get {a_names}"
    L.add_log(save, f"🔁 {text}", news=news)
    save["last_waived"] = {}
    for team in (t.a, t.b):
        cut = L.trim_roster(save, team, roster_limit(save))
        if cut:
            save["last_waived"][team] = cut
            L.add_log(save, f"✂️ {cba.TEAMS[team].name} waived {', '.join(cut)} to make roster room", news=news)
    refresh_modes(save)
    return text


# --- negotiation --------------------------------------------------------------------------

LINES = {
    "accept": {
        "contend": ["That moves the needle for us. Let's do it.", "We're in. This helps us win now."],
        "middle": ["Works for us. Pleasure doing business.", "We can live with that. Deal."],
        "rebuild": ["That fits our timeline perfectly. Deal.", "Our scouts love that. Done."],
    },
    "reject": {
        "contend": ["We're trying to win a title. That makes us worse.", "Not happening during a title push."],
        "middle": ["Not enough value coming back for us.", "We'll pass. Doesn't do it for us."],
        "rebuild": ["We need young talent or picks, not that.", "Doesn't fit where we're going. Pass."],
    },
}


@dataclass
class Response:
    kind: str                  # accept | counter | reject | invalid
    message: str
    evaluation: Evaluation | None = None
    counter: Trade | None = None
    problems: list[str] = field(default_factory=list)


def _pick_line(kind: str, mode: str, rng: random.Random) -> str:
    return rng.choice(LINES[kind][mode])


def respond(save: dict, t: Trade, rng: random.Random | None = None) -> Response:
    """The AI team's answer to a user proposal: accept, counter, or reject (with a reason)."""
    rng = rng or random.Random()
    problems = validate(save, t)
    if problems:
        return Response("invalid", "That trade isn't allowed under the CBA.", problems=problems)
    ev = evaluate(save, t.b, t.a_out, t.b_out)
    mode = mode_of(save, t.b)
    if ev.untouchable:
        return Response("reject", f"{', '.join(ev.untouchable)} is off the table. We aren't moving him.", ev)
    if ev.gain >= 0:
        return Response("accept", _pick_line("accept", mode, rng), ev)
    counter = find_counter(save, t, ev)
    if counter:
        c, why = counter
        return Response("counter", why, ev, counter=c)
    return Response("reject", _pick_line("reject", mode, rng), ev)


def find_counter(save: dict, t: Trade, ev: Evaluation) -> tuple[Trade, str] | None:
    if ev.ratio < 0.45:
        return None
    best: tuple[float, Trade, str] | None = None
    mode_b = mode_of(save, t.b)
    user_mode = "middle"
    # Option 1: ask the user to add one more asset (the cheapest one that gets it done).
    mine = [f"p:{p['id']}" for p in L.roster(save, t.a)] + [f"k:{k['id']}" for k in L.team_picks(save, t.a)]
    for aid in mine:
        if aid in t.a_out:
            continue
        c = t.copy()
        c.a_out.append(aid)
        if validate(save, c):
            continue
        e2 = evaluate(save, t.b, c.a_out, c.b_out)
        if e2.gain >= 0 and not e2.untouchable:
            cost = _asset_value(save, aid, user_mode)
            if best is None or cost < best[0]:
                best = (cost, c, f"We'd do it if you add **{asset_label(save, aid, False)}**.")
    # Option 2: they keep one of the pieces you asked for.
    if len(t.b_out) > 1:
        for aid in t.b_out:
            c = t.copy()
            c.b_out.remove(aid)
            if validate(save, c):
                continue
            e2 = evaluate(save, t.b, c.a_out, c.b_out)
            if e2.gain >= 0:
                cost = _asset_value(save, aid, user_mode)
                if best is None or cost < best[0]:
                    best = (cost, c, f"We'd do it if we keep **{asset_label(save, aid, False)}**.")
    if best:
        return best[1], best[2]
    return None


def _asset_value(save: dict, aid: str, mode: str) -> float:
    kind, key = aid.split(":", 1)
    if kind == "p":
        return player_value(save["players"][key], mode, save["season"])
    return pick_value(save, save["picks"][key], mode)


# --- offers built by the AI ---------------------------------------------------------------

def best_package(save: dict, buyer: str, seller: str, pid: str, seller_must_agree: bool,
                 judge_mode: str = "middle") -> Trade | None:
    """The best package `buyer` would give for player `pid` (owned by `seller`).

    Fast pass: price every combination with precomputed values and quick salary math, then run
    the full CBA check and both teams' evaluations only on the most promising few."""
    p = save["players"][pid]
    season = save["season"]
    bmode = mode_of(save, buyer)
    smode = mode_of(save, seller)
    target_val = player_value(p, bmode, season)
    if target_val < 25:
        return None
    yr = L.cap_year(save)
    cap = cba.cap_sheet(yr)
    psal = 0 if p.get("twoway") else L.salary_now(p)
    b_pay, s_pay = L.payroll(save, buyer), L.payroll(save, seller)
    cand = []
    for q in L.roster(save, buyer, include_twoway=False):
        rel = reluctance(save, buyer, q)
        if rel >= UNTOUCHABLE:
            continue
        cost = player_value(q, bmode, season) * (1 + rel)
        cand.append((q, cost))
    cand.sort(key=lambda x: x[1])
    cand = cand[:11]
    picks = [k for k in L.team_picks(save, buyer) if k["year"] >= season][:6]
    pick_cost = {k["id"]: pick_value(save, k, bmode) * 1.08 for k in picks}
    judge = smode if seller_must_agree else judge_mode
    worth_p = {q["id"]: player_value(q, judge, season) for q, _ in cand}
    worth_k = {k["id"]: pick_value(save, k, judge) for k in picks}
    seller_cost = player_value(p, smode, season) * (1 + reluctance(save, seller, p)) if seller_must_agree else 0.0

    def salary_ok(team_pay, out_s, in_s):
        after = team_pay - out_s + in_s
        if after <= cap["cap"] or in_s <= out_s:
            return True
        if after > cap["apron1"]:
            return in_s <= out_s + APRON_TOLERANCE
        return in_s <= max(cba.max_incoming(out_s, yr), out_s + max(0, cap["cap"] - team_pay))

    shortlist: list[tuple[float, list, tuple]] = []
    for n in range(0, 4):
        for combo in itertools.combinations(cand, n):
            out_sal = sum(L.salary_now(q) for q, _ in combo)
            if not salary_ok(b_pay, out_sal, psal) or not salary_ok(s_pay, psal, out_sal):
                continue
            base_cost = sum(c for _, c in combo)
            if base_cost > target_val * 1.02:
                continue
            base_worth = sum(worth_p[q["id"]] for q, _ in combo)
            for m in range(0, 3):
                for pk in itertools.combinations(picks, m):
                    if n == 0 and m == 0:
                        continue
                    cost = base_cost + sum(pick_cost[k["id"]] for k in pk)
                    if cost > target_val * 1.02:
                        continue
                    worth = base_worth + sum(worth_k[k["id"]] for k in pk)
                    if seller_must_agree and worth < seller_cost * 0.9:
                        continue      # clearly not enough for the seller
                    shortlist.append((worth, list(combo), pk))
    shortlist.sort(key=lambda x: -x[0])
    for worth, combo, pk in shortlist[:12]:
        t = Trade(buyer, seller, [f"p:{q['id']}" for q, _ in combo] + [f"k:{k['id']}" for k in pk], [f"p:{pid}"])
        if validate(save, t):
            continue
        if evaluate(save, buyer, t.b_out, t.a_out).gain < 0:
            continue
        if seller_must_agree and evaluate(save, seller, t.a_out, t.b_out).gain < 0:
            continue
        return t
    return None


def shop_player(save: dict, pid: str, limit: int = 3) -> list[tuple[Trade, float]]:
    """Offers from AI teams for one of the user's players, best first (by a neutral valuation)."""
    seller = save["team"]
    offers = []
    for buyer in cba.TEAMS:
        if buyer == seller:
            continue
        t = best_package(save, buyer, seller, pid, seller_must_agree=False)
        if t:
            worth = sum(_asset_value(save, a, "middle") for a in t.a_out)
            offers.append((t, worth))
    offers.sort(key=lambda x: -x[1])
    return offers[:limit]


def ai_trades(save: dict, rng: random.Random, max_trades: int = 4) -> list[str]:
    """Trades between AI teams. Sellers (rebuilding teams, some middling ones, anyone with a player
    who asked out) move veterans and role players to buyers. Stars rarely move unless they've
    requested a trade. Each team makes at most two deals per call."""
    done: list[str] = []
    touched: dict[str, int] = {}
    sellers = [a for a in cba.TEAMS if a != save["team"] and mode_of(save, a) == "rebuild"]
    sellers += [a for a in cba.TEAMS if a != save["team"] and mode_of(save, a) == "middle" and rng.random() < 0.6]
    sellers += [a for a in cba.TEAMS if a != save["team"] and a not in sellers
                and any(p.get("wants_out") for p in L.roster(save, a))]
    rng.shuffle(sellers)
    for _round in range(2):
        for seller in sellers:
            if len(done) >= max_trades:
                return done
            if touched.get(seller, 0) >= 2:
                continue
            ros = L.roster(save, seller, include_twoway=False)
            cands = []
            for i, p in enumerate(ros):
                h = heat_of(save, p["id"]).get("h", 0)
                if p["ovr"] < 68 or not (p["age"] >= 26 or h > 0.3 or p["ovr"] < 76):
                    continue
                if p.get("acq") == save["season"] and h < 0.6:
                    continue      # just got here
                if (i == 0 or p["ovr"] >= 86) and h < 0.6:
                    continue      # franchise stars only move when they've asked out
                if p["ovr"] >= 82 and h < 0.6 and rng.random() < 0.6:
                    continue      # good starters move, but not often
                cands.append(p)
            rng.shuffle(cands)
            for p in cands[:3]:
                buyers = [x for x in cba.TEAMS if x not in (seller, save["team"]) and touched.get(x, 0) < 2
                          and mode_of(save, x) != "rebuild"]
                rng.shuffle(buyers)
                made = False
                for buyer in buyers[:6]:
                    t = best_package(save, buyer, seller, p["id"], seller_must_agree=True)
                    if t and rng.random() < 0.75:
                        done.append(apply_trade(save, t, news=True))
                        touched[buyer] = touched.get(buyer, 0) + 1
                        touched[seller] = touched.get(seller, 0) + 1
                        made = True
                        break
                if made:
                    break
    return done


def what_would_it_take(save: dict, t: Trade, max_players: int = 3) -> Trade | None:
    """Trade finder: the cheapest set of the user's assets that gets `t.b` to say yes to
    sending `t.b_out` (keeps anything the user already put in)."""
    if not t.b_out:
        return None
    me = t.a
    season = save["season"]
    pool = [p for p in L.roster(save, me, include_twoway=False) if f"p:{p['id']}" not in t.a_out]
    picks = [k for k in L.team_picks(save, me) if f"k:{k['id']}" not in t.a_out][:7]
    cost = {f"p:{p['id']}": player_value(p, "middle", season) for p in pool}
    cost.update({f"k:{k['id']}": pick_value(save, k, "middle") for k in picks})
    # Value to the AI team of each asset, to prune hopeless combos early.
    bmode = mode_of(save, t.b)
    target = evaluate(save, t.b, t.a_out, t.b_out)
    if target.untouchable:
        return None
    ranked = sorted(pool, key=lambda p: -player_value(p, bmode, season))[:12]
    vb = {f"p:{p['id']}": player_value(p, bmode, season) for p in ranked}
    vb.update({f"k:{k['id']}": pick_value(save, k, bmode) for k in picks})
    need = target.value_out - target.value_in
    best: tuple[float, Trade] | None = None
    for n in range(0, max_players + 1):
        for combo in itertools.combinations(ranked, n):
            for m in range(0, 3):
                for pk in itertools.combinations(picks, m):
                    if n == 0 and m == 0:
                        continue
                    add = [f"p:{p['id']}" for p in combo] + [f"k:{k['id']}" for k in pk]
                    c = sum(max(0.0, cost[x]) for x in add)
                    if best is not None and c >= best[0]:
                        continue
                    if sum(max(0.0, vb[x]) for x in add) < need:
                        continue
                    trial = Trade(t.a, t.b, t.a_out + add, list(t.b_out))
                    if evaluate(save, t.b, trial.a_out, trial.b_out).gain < 0:
                        continue
                    if validate(save, trial):
                        continue
                    best = (c, trial)
    return best[1] if best else None
