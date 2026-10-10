"""Season simulation and the full offseason: injuries, standings, play-in, playoffs, awards,
progression/aging, draft lottery, draft, re-signing and free agency."""

from __future__ import annotations

import math
import random

from . import ai, cba
from . import league as L
from .ratings import noise, potential

GAMES = 82
DEADLINE_SHARE = 0.56          # trade deadline falls a bit past halfway
WIN_K = 0.17                   # strength gap -> win probability slope
HOME_EDGE = 0.22
LOTTERY_ODDS = [140, 140, 140, 125, 105, 90, 75, 60, 45, 30, 20, 15, 10, 5]
ROOKIE_SCALE = [13.8, 12.3, 11.1, 10.0, 9.1, 8.3, 7.6, 7.0, 6.5, 6.1, 5.8, 5.5, 5.2, 4.9, 4.7, 4.4, 4.2,
                4.0, 3.8, 3.6, 3.5, 3.4, 3.2, 3.1, 3.0, 2.9, 2.8, 2.7, 2.65, 2.6]


# ------------------------------------------------------------------------------------------
# Schedule
# ------------------------------------------------------------------------------------------

def schedule(seed: int) -> list[tuple[str, str]]:
    """A real-format 82-game schedule: 4 vs division, 3-4 vs conference, 2 vs other conference."""
    rng = random.Random(seed)
    teams = list(cba.TEAMS)
    games: list[tuple[str, str]] = []
    by_conf = {c: [a for a in teams if cba.TEAMS[a].conf == c] for c in ("East", "West")}
    for conf, members in by_conf.items():
        # Each team plays 4 games vs 6 of its 10 non-division conference opponents, 3 vs the other 4.
        four: set[frozenset] = set()
        nondiv = {a: [b for b in members if cba.TEAMS[b].div != cba.TEAMS[a].div] for a in members}
        need = {a: 6 for a in members}
        for _ in range(200):
            four.clear()
            need = {a: 6 for a in members}
            order = members[:]
            rng.shuffle(order)
            ok = True
            for a in order:
                opts = [b for b in nondiv[a] if frozenset((a, b)) not in four and need[b] > 0]
                rng.shuffle(opts)
                opts.sort(key=lambda b: -need[b])
                while need[a] > 0 and opts:
                    b = opts.pop(0)
                    four.add(frozenset((a, b)))
                    need[a] -= 1
                    need[b] -= 1
                if need[a] > 0:
                    ok = False
                    break
            if ok and all(v == 0 for v in need.values()):
                break
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if cba.TEAMS[a].div == cba.TEAMS[b].div:
                    n = 4
                else:
                    n = 4 if frozenset((a, b)) in four else 3
                for g in range(n):
                    games.append((a, b) if (g + (i % 2)) % 2 == 0 else (b, a))
    for a in by_conf["East"]:
        for b in by_conf["West"]:
            games += [(a, b), (b, a)]
    rng.shuffle(games)
    return games


# ------------------------------------------------------------------------------------------
# Games
# ------------------------------------------------------------------------------------------

def team_rating(save: dict, abbr: str) -> float:
    return L.strength(save, abbr, healthy_only=True) + save.get("chem", {}).get(abbr, 0.0)


def win_prob(r_home: float, r_away: float) -> float:
    return 1 / (1 + math.exp(-(WIN_K * (r_home - r_away) + HOME_EDGE)))


def _injury_roll(save: dict, rng: random.Random, abbr: str, news: list[str]) -> None:
    for p in L.roster(save, abbr)[:10]:
        if p.get("out", 0) > 0:
            continue
        risk = 0.0045 + max(0, p["age"] - 30) * 0.0006
        if rng.random() < risk:
            sev = rng.random()
            games = rng.randint(1, 4) if sev < 0.6 else rng.randint(5, 15) if sev < 0.9 else rng.randint(20, 70)
            p["out"] = games
            p["inj"] = "Out " + (f"{games} games" if games < 40 else "for the season")
            if games >= 15 and p["ovr"] >= 80:
                news.append(f"🚑 {p['name']} ({abbr}) will miss about {games} games.")


def _tick_injuries(save: dict, abbr: str) -> None:
    for pid in save["rosters"][abbr]:
        p = save["players"][pid]
        if p.get("out", 0) > 0:
            p["out"] -= 1
            p["gp_missed"] = p.get("gp_missed", 0) + 1
            if p["out"] == 0:
                p["inj"] = None


def play_game(save: dict, rng: random.Random, home: str, away: str, news: list[str]) -> str:
    p = win_prob(team_rating(save, home), team_rating(save, away))
    winner = home if rng.random() < p else away
    for t in (home, away):
        _tick_injuries(save, t)
        _injury_roll(save, rng, t, news)
    return winner


def ensure_rosters(save: dict, rng: random.Random) -> None:
    """Every team keeps at least 13 standard players (signs minimum guys if short)."""
    for abbr in cba.TEAMS:
        while L.standard_count(save, abbr) < cba.ROSTER_MIN:
            min_sal = cba.MIN_SALARY * (1 + cba.CAP_GROWTH) ** max(0, save["season"] - cba.FIRST_SEASON)
            pool = sorted((save["players"][i] for i in save["fa"]
                           if ai.market_salary(save["players"][i]["ovr"], save["season"]) <= min_sal * 1.3),
                          key=lambda p: -p["ovr"])
            if pool:
                p = pool[0]
            else:
                p = make_player(rng, save["season"], ovr=rng.randint(46, 54), age=rng.randint(23, 30))
                save["players"][p["id"]] = p
                save["fa"].append(p["id"])
            p["salary"] = [cba.MIN_SALARY]
            p.pop("twoway", None)
            L.move_player(save, p["id"], abbr)


def _start_season_if_needed(save: dict, rng: random.Random) -> None:
    if save.get("game_idx") is not None:
        return
    save["sched_seed"] = rng.randrange(1 << 30)
    save["game_idx"] = 0
    save["standings"] = {a: [0, 0] for a in cba.TEAMS}
    save["chem"] = {a: round(rng.gauss(0, 0.6), 2) for a in cba.TEAMS}
    for p in save["players"].values():
        p["gp_missed"] = 0
    ensure_rosters(save, rng)
    ai.refresh_modes(save)


def sim_games(save: dict, rng: random.Random, until_idx: int) -> list[str]:
    _start_season_if_needed(save, rng)
    games = schedule(save["sched_seed"])
    news: list[str] = []
    while save["game_idx"] < min(until_idx, len(games)):
        home, away = games[save["game_idx"]]
        w = play_game(save, rng, home, away, news)
        save["standings"][w][0] += 1
        save["standings"][away if w == home else home][1] += 1
        save["game_idx"] += 1
    for n in news[-6:]:
        L.add_log(save, n, news=True)
    return news


def sim_to_deadline(save: dict, rng: random.Random) -> dict:
    total = len(cba.TEAMS) * GAMES // 2
    sim_games(save, rng, int(total * DEADLINE_SHARE))
    save["phase"] = "deadline"
    trades = ai.ai_trades(save, rng, max_trades=rng.randint(3, 6))
    inbox = []
    mine = [p for p in L.roster(save, save["team"]) if p["ovr"] >= 72 and not p.get("twoway")]
    rng.shuffle(mine)
    for p in mine[:3]:
        offers = ai.shop_player(save, p["id"], limit=1)
        if offers:
            inbox.append(offers[0][0].to_dict())
        if len(inbox) >= 2:
            break
    save["inbox"] = inbox
    ai.refresh_modes(save)
    return {"trades": trades, "inbox": len(inbox)}


# ------------------------------------------------------------------------------------------
# Postseason
# ------------------------------------------------------------------------------------------

def seeds(save: dict) -> dict[str, list[str]]:
    st = save["standings"]
    out = {}
    for conf in ("East", "West"):
        teams = [a for a in cba.TEAMS if cba.TEAMS[a].conf == conf]
        out[conf] = sorted(teams, key=lambda a: (-st[a][0], st[a][1], noise(a + str(save["season"]), 1)))
    return out


def series(save: dict, rng: random.Random, high: str, low: str, best_of: int = 7) -> tuple[str, str, str]:
    need = best_of // 2 + 1
    wh = wl = 0
    homes = [high, high, low, low, high, low, high]
    g = 0
    while wh < need and wl < need:
        home = homes[g] if best_of == 7 else high
        away = low if home == high else high
        p = win_prob(team_rating(save, home), team_rating(save, away))
        winner = home if rng.random() < p else away
        if winner == high:
            wh += 1
        else:
            wl += 1
        for t in (high, low):
            _tick_injuries(save, t)
        g += 1
    w, l = (high, low) if wh > wl else (low, high)
    return w, l, f"{max(wh, wl)}-{min(wh, wl)}"


def sim_postseason(save: dict, rng: random.Random) -> dict:
    total = len(cba.TEAMS) * GAMES // 2
    if save.get("phase") == "preseason":
        sim_to_deadline(save, rng)
    sim_games(save, rng, total)
    sd = seeds(save)
    result: dict[str, str] = {}
    bracket_log: list[str] = []
    finalists = []
    for conf, order in sd.items():
        s = order[:]
        w78, l78, _ = series(save, rng, s[6], s[7], best_of=1)
        w910, _, _ = series(save, rng, s[8], s[9], best_of=1)
        w8, _, _ = series(save, rng, l78, w910, best_of=1)
        for a in s[10:]:
            result[a] = "Missed playoffs"
        for a in (s[6], s[7], s[8], s[9]):
            result[a] = "Lost in Play-In"
        field = s[:6] + [w78, w8]
        bracket_log.append(f"**{conf} Play-In:** {w78} (7) · {w8} (8)")
        rounds = ["First Round", f"{conf} Semifinals", f"{conf} Finals"]
        seeds_map = {a: i + 1 for i, a in enumerate(field)}
        alive = [(field[0], field[7]), (field[3], field[4]), (field[2], field[5]), (field[1], field[6])]
        for rname in rounds:
            winners = []
            for hi, lo in alive:
                if seeds_map[lo] < seeds_map[hi]:
                    hi, lo = lo, hi
                w, l, score = series(save, rng, hi, lo)
                result[l] = f"Lost in {rname}"
                winners.append(w)
                if rname != "First Round":
                    bracket_log.append(f"{rname}: **{w}** def. {l} ({score})")
            alive = [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)] if len(winners) > 1 else []
            if len(winners) == 1:
                finalists.append(winners[0])
    st = save["standings"]
    a, b = finalists
    hi, lo = (a, b) if (st[a][0], -st[a][1]) >= (st[b][0], -st[b][1]) else (b, a)
    champ, runner, score = series(save, rng, hi, lo)
    result[runner] = "Lost in NBA Finals"
    result[champ] = "🏆 NBA Champions"
    bracket_log.append(f"**NBA Finals:** {cba.TEAMS[champ].full} def. {cba.TEAMS[runner].full} ({score})")
    awards = pick_awards(save, rng, champ)
    me = save["team"]
    w, l = st[me]
    seed_txt = next((f"{conf} #{order.index(me) + 1}" for conf, order in sd.items() if me in order), "")
    entry = {"season": save["season"], "record": f"{w}-{l}", "seed": seed_txt, "result": result[me],
             "champion": champ, "awards": awards}
    save["history"].append(entry)
    if champ == me:
        save["titles"] = save.get("titles", 0) + 1
    save["playoffs"] = {"result": result, "log": bracket_log, "seeds": sd}
    save["phase"] = "postseason"
    save["live"] = False
    save.pop("inbox", None)
    for p in save["players"].values():
        p["out"] = 0
        p["inj"] = None
    L.add_log(save, f"🏆 {cba.TEAMS[champ].full} win the {cba.season_label(save['season'])} title", news=True)
    return entry


def pick_awards(save: dict, rng: random.Random, champ: str) -> dict[str, str]:
    st = save["standings"]
    rostered = [p for p in save["players"].values() if p.get("team")]
    eligible = [p for p in rostered if p.get("gp_missed", 0) <= 17]

    def wp(p):
        w, l = st.get(p["team"], [41, 41])
        return w / max(1, w + l)
    mvp = max(eligible or rostered, key=lambda p: p["ovr"] * 1.0 + wp(p) * 14 + rng.gauss(0, 1.2))
    dpoy = max(eligible or rostered, key=lambda p: p["attrs"]["def"] * 0.7 + p["ovr"] * 0.3 + rng.gauss(0, 2))
    rookies = [p for p in rostered if p.get("exp", 0) == 0]
    roy = max(rookies, key=lambda p: p["ovr"] + rng.gauss(0, 1)) if rookies else None
    fmvp = max(L.roster(save, champ)[:3], key=lambda p: p["ovr"] + rng.gauss(0, 1.5))
    fmt = lambda p: f"{p['name']} ({p['team']})"
    out = {"MVP": fmt(mvp), "DPOY": fmt(dpoy), "Finals MVP": fmt(fmvp)}
    if roy:
        out["ROY"] = fmt(roy)
    return out


# ------------------------------------------------------------------------------------------
# Offseason
# ------------------------------------------------------------------------------------------

FIRST = ["Jalen", "Cam", "Tyrese", "Malik", "Darius", "Isaiah", "Jaylen", "Amari", "Elijah", "Kobe", "Trey", "Marcus",
         "Andre", "Luca", "Nikola", "Mateo", "Omar", "Yusuf", "Bilal", "Hassan", "Kenji", "Tariq", "Devin", "Jordan",
         "Caleb", "Ayo", "Dylan", "Xavier", "Zion", "Bryce", "Noah", "Ethan", "Ousmane", "Moussa", "Karim", "Aleksa",
         "Jamal", "Terrence", "Keon", "Rashad", "Dante", "Julian", "Miles", "Asa", "Ty", "Jace", "Khalil", "Ismail"]
LAST = ["Walker", "Johnson", "Okafor", "Bennett", "Diallo", "Harris", "Thompson", "Petrovic", "Mensah", "Brooks",
        "Coleman", "Reed", "Carter", "Ndiaye", "Hughes", "Fields", "Morales", "Price", "Grant", "Sow", "Bamba", "Ellis",
        "Washington", "Murray", "Vasquez", "Kone", "Adebayo", "Simmons", "Fox", "Bridges", "Bell", "Mitchell",
        "Rivers", "Turner", "Hayes", "Kovac", "Traore", "Edwards", "Wright", "Holmes", "Banks", "Ford", "Lyons",
        "Stewart", "Moreno", "Sarr", "Chukwu", "Iqbal", "Rahman", "Castillo"]
POSITIONS = ["PG", "SG", "SF", "PF", "C"]


def make_player(rng: random.Random, season: int, ovr: int, age: int, pot: int | None = None,
                name: str | None = None) -> dict:
    pid = f"g{season}{rng.randrange(10**8):08d}"
    pos = rng.choice(POSITIONS)
    pot = pot if pot is not None else potential(ovr, age, pid)
    tilt = {"PG": ("pla", "sho"), "SG": ("sho", "sco"), "SF": ("sco", "def"), "PF": ("reb", "def"), "C": ("reb", "def")}[pos]
    attrs = {}
    for k in ("sco", "sho", "pla", "reb", "def"):
        base = 30 + (ovr - 40) * 0.9 + rng.gauss(0, 7) + (9 if k in tilt else -4)
        attrs[k] = int(max(25, min(99, base)))
    return {"id": pid, "name": name or f"{rng.choice(FIRST)} {rng.choice(LAST)}", "team": None, "pos": pos,
            "age": age, "exp": 0, "salary": [], "ovr": ovr, "pot": pot, "attrs": attrs, "line": {}, "hist": {},
            "inj": None, "jersey": "", "headshot": "", "generated": True}


def _top_mean(save: dict, n: int = 150) -> float:
    ovrs = sorted((p["ovr"] for p in save["players"].values() if p.get("team")), reverse=True)[:n]
    return sum(ovrs) / max(1, len(ovrs))


def progress_players(save: dict, rng: random.Random) -> list[str]:
    notes = []
    retired = []
    for p in list(save["players"].values()):
        a, ovr, pot = p["age"], p["ovr"], p.get("pot", p["ovr"])
        if a <= 21:
            d = (pot - ovr) * 0.22 + rng.gauss(0, 2.4)
        elif a <= 24:
            d = (pot - ovr) * 0.18 + rng.gauss(0, 2.1)
        elif a <= 27:
            d = rng.gauss(0.2, 1.8)
        elif a <= 30:
            d = rng.gauss(-0.9, 1.8)
        elif a <= 32:
            d = rng.gauss(-2.2, 2.0)
        elif a <= 34:
            d = rng.gauss(-3.5, 2.2)
        else:
            d = rng.gauss(-5.0, 2.5)
        if d > 0:
            d *= max(0.25, (99 - ovr) / 14) if ovr >= 85 else 1.0
        d = round(d)
        p["ovr"] = int(max(40, min(99, ovr + d)))
        p["age"] = a + 1
        p["exp"] = p.get("exp", 0) + 1
        if p["age"] <= 26:
            p["pot"] = int(max(p["ovr"], min(99, pot + rng.gauss(0, 1.5))))
        else:
            p["pot"] = p["ovr"]
        for k in p.get("attrs", {}):
            p["attrs"][k] = int(max(25, min(99, p["attrs"][k] + d * 0.8 + rng.gauss(0, 1))))
        p["last_delta"] = d
        if p.get("team") == save["team"] and abs(d) >= 4:
            notes.append(f"{'📈' if d > 0 else '📉'} {p['name']} {'+' if d > 0 else ''}{d} → {p['ovr']} OVR")
        if (p["age"] >= 36 and (p["ovr"] < 74 or rng.random() < 0.35)) or p["age"] >= 40 \
                or (p["age"] >= 33 and p["ovr"] < 60 and not p.get("salary")):
            retired.append(p)
    # Keep the league's talent level stable over the years, like the real NBA.
    base = save.setdefault("talent_base", _top_mean(save))
    drift = _top_mean(save) - base
    if abs(drift) > 0.4:
        shift = -drift * 0.7
        for p in save["players"].values():
            p["ovr"] = int(max(40, min(99, round(p["ovr"] + shift))))
            p["pot"] = max(p["ovr"], int(round(p.get("pot", p["ovr"]) + shift)))
    for p in retired:
        if p.get("team") == save["team"]:
            notes.append(f"👋 {p['name']} retired at {p['age']}.")
        elif p["ovr"] >= 80:
            L.add_log(save, f"👋 {p['name']} retires after {p['exp']} seasons.", news=True)
        if p.get("team"):
            save["rosters"][p["team"]].remove(p["id"])
        if p["id"] in save["fa"]:
            save["fa"].remove(p["id"])
        del save["players"][p["id"]]
    return notes


def lottery(save: dict, rng: random.Random) -> list[str]:
    st = save["standings"]
    result = save["playoffs"]["result"]
    by_record = sorted(cba.TEAMS, key=lambda a: (st[a][0], -st[a][1], noise(a, 1)))
    lotto = [a for a in by_record if result[a] in ("Missed playoffs", "Lost in Play-In")][:14]
    others = [a for a in by_record if a not in lotto]
    winners: list[str] = []
    pool = list(zip(lotto, LOTTERY_ODDS))
    for _ in range(4):
        total = sum(w for _, w in pool)
        r = rng.uniform(0, total)
        acc = 0
        for i, (team, w) in enumerate(pool):
            acc += w
            if r <= acc:
                winners.append(team)
                pool.pop(i)
                break
    order = winners + [a for a in lotto if a not in winners] + others
    return order


def start_offseason(save: dict, rng: random.Random) -> dict:
    """After the Finals: aging/progression, contracts roll over, lottery, draft class."""
    notes = progress_players(save, rng)
    for p in save["players"].values():
        if p.get("salary"):
            p["salary"] = p["salary"][1:]
        p["expiring"] = bool(p.get("team")) and not p.get("salary")
    save["dead"] = {a: d[1:] for a, d in (save.get("dead") or {}).items() if len(d) > 1}
    order = lottery(save, rng)
    year = save["season"]
    second = sorted(cba.TEAMS, key=lambda a: (save["standings"][a][0], -save["standings"][a][1]))
    picks = []
    for rnd, ordr in ((1, order), (2, second)):
        for slot, orig in enumerate(ordr, start=1):
            k = save["picks"].get(L.pick_id(year, orig, rnd))
            owner = k["owner"] if k else orig
            if k and k.get("prot") and slot <= k["prot"]:
                owner = orig
                L.add_log(save, f"🛡️ {orig}'s pick landed #{slot} and stayed with {orig} (top-{k['prot']} protected).", news=True)
            picks.append({"no": slot + (30 if rnd == 2 else 0), "orig": orig, "owner": owner})
    save["draft"] = {"order": order, "picks": picks, "idx": 0, "prospects": make_class(rng, year), "log": []}
    save["phase"] = "draft"
    lot_top = ", ".join(f"#{i + 1} {a}" for i, a in enumerate(order[:4]))
    return {"notes": notes, "lottery": lot_top}


def make_class(rng: random.Random, year: int) -> list[dict]:
    strength = rng.gauss(0, 1.5)
    out = []
    for i in range(72):
        tier = i / 72
        age = rng.choice([19, 19, 19, 20, 20, 21, 22])
        pot = int(max(55, min(97, 90 - tier * 32 + rng.gauss(strength, 3.5) - (age - 19) * 1.5)))
        ovr = int(max(40, min(78, pot - rng.randint(10, 22) + (age - 19) * 2)))
        p = make_player(rng, year, ovr=ovr, age=age, pot=pot)
        p["draft_year"] = year
        p["scout"] = pot + rng.randint(-4, 4)  # what scouts think his ceiling is
        out.append(p)
    out.sort(key=lambda p: -(p["scout"] * 0.7 + p["ovr"] * 0.3))
    for i, p in enumerate(out, 1):
        p["big_board"] = i
    return out


def on_the_clock(save: dict) -> dict | None:
    d = save.get("draft")
    if not d or d["idx"] >= len(d["picks"]):
        return None
    return d["picks"][d["idx"]]


def rookie_salary(no: int, season: int) -> list[int]:
    k = (1 + cba.CAP_GROWTH) ** max(0, season - cba.FIRST_SEASON)
    if no <= 30:
        base = ROOKIE_SCALE[no - 1] * 1_000_000 * k
        return [int(base * (1.045 ** i)) for i in range(4)]
    return [int(cba.ROOKIE_MIN * k), int(cba.ROOKIE_MIN * k * 1.05)]


def make_pick(save: dict, prospect_id: str) -> str:
    d = save["draft"]
    slot = d["picks"][d["idx"]]
    p = next(x for x in d["prospects"] if x["id"] == prospect_id)
    d["prospects"].remove(p)
    p["salary"] = rookie_salary(slot["no"], save["season"] + 1)
    p["drafted"] = f"{save['season']} #{slot['no']}"
    p.pop("scout", None)
    save["players"][p["id"]] = p
    p["team"] = None
    L.move_player(save, p["id"], slot["owner"])
    save["picks"].pop(L.pick_id(save["season"], slot["orig"], 1 if slot["no"] <= 30 else 2), None)
    line = f"#{slot['no']} {slot['owner']}: {p['name']} ({p['pos']}, {p['age']})"
    d["log"].append(line)
    d["idx"] += 1
    return line


def ai_pick(save: dict, rng: random.Random) -> str:
    d = save["draft"]
    slot = on_the_clock(save)
    mode = ai.mode_of(save, slot["owner"])
    best = max(d["prospects"][:12], key=lambda p: p["scout"] * (0.8 if mode == "contend" else 1.0)
               + p["ovr"] * (0.35 if mode == "contend" else 0.2) + rng.gauss(0, 2.2))
    return make_pick(save, best["id"])


def advance_draft(save: dict, rng: random.Random, auto_user: bool = False) -> list[str]:
    """AI teams pick until the user's team is on the clock (or the draft ends)."""
    lines = []
    while True:
        slot = on_the_clock(save)
        if slot is None:
            finish_draft(save)
            break
        if slot["owner"] == save["team"] and not auto_user:
            break
        lines.append(ai_pick(save, rng))
    return lines


def finish_draft(save: dict) -> None:
    if save["phase"] != "draft":
        return
    d = save["draft"]
    for p in d["prospects"][:20]:  # best undrafted guys hit free agency
        p.pop("scout", None)
        save["players"][p["id"]] = p
        save["fa"].append(p["id"])
    d["prospects"] = []
    save["phase"] = "resign"
    asks = {}
    for p in L.roster(save, save["team"]):
        if p.get("expiring"):
            asks[p["id"]] = asking(save, p, own_team=True)
    save["asks"] = asks


def contract_years(p: dict) -> int:
    a = p["age"]
    return 5 if a <= 24 else 4 if a <= 27 else 3 if a <= 30 else 2 if a <= 33 else 1


def asking(save: dict, p: dict, own_team: bool = False) -> dict:
    season = save["season"] + 1
    exp_ovr = p["ovr"] + (1.5 if p["age"] <= 24 else -1.0 if p["age"] >= 31 else 0)
    amt = ai.market_salary(exp_ovr, season) * (1 + noise(p["id"] + "ask", 0.1))
    amt = max(int(cba.MIN_SALARY * (1 + cba.CAP_GROWTH) ** max(0, season - cba.FIRST_SEASON)), int(amt))
    amt = min(amt, cba.max_salary(season, p.get("exp", 0)))
    refuses = False
    if own_team:
        w, l = L.record(save, save["team"])
        if w < 33 and p["age"] >= 28 and p["ovr"] >= 80 and noise(p["id"] + "loyal", 1) > 0.2:
            refuses = True
    return {"amount": int(round(amt, -4)), "years": contract_years(p), "refuses": refuses}


def resign(save: dict, pid: str) -> str:
    p = save["players"][pid]
    ask = save["asks"].pop(pid)
    p["salary"] = [int(ask["amount"] * (1.05 ** i)) for i in range(ask["years"])]
    p["expiring"] = False
    return f"✍️ Re-signed {p['name']}: {ask['years']} yrs, {cba.money(sum(p['salary']))} total"


def let_walk(save: dict, pid: str) -> None:
    save["asks"].pop(pid, None)
    L.move_player(save, pid, None)
    save["players"][pid]["expiring"] = False


def ai_resign(save: dict, rng: random.Random) -> None:
    cap = cba.cap_sheet(save["season"] + 1)
    for abbr in cba.TEAMS:
        if abbr == save["team"]:
            continue
        mode = ai.mode_of(save, abbr)
        for p in L.roster(save, abbr):
            if not p.get("expiring"):
                continue
            ask = asking(save, p)
            value = ai.player_value({**p, "salary": [ask["amount"]] * ask["years"]}, mode, save["season"] + 1)
            after = L.payroll(save, abbr) + ask["amount"]
            keep = value > 40 and rng.random() < 0.8
            if after > cap["apron2"] and p["ovr"] < 86:
                keep = False
            elif after > cap["tax"] and mode != "contend" and p["ovr"] < 82:
                keep = False
            if keep:
                p["salary"] = [int(ask["amount"] * (1.05 ** i)) for i in range(ask["years"])]
                p["expiring"] = False
            else:
                L.move_player(save, p["id"], None)
                p["expiring"] = False


def start_free_agency(save: dict, rng: random.Random) -> None:
    for pid in list(save.get("asks", {})):
        let_walk(save, pid)
    for p in L.roster(save, save["team"]):
        if p.get("expiring"):
            let_walk(save, p["id"])
    ai_resign(save, rng)
    save["fa"] = [pid for pid in save["fa"] if pid in save["players"]]
    for pid in save["fa"]:
        p = save["players"][pid]
        p["salary"] = []
        p.pop("twoway", None)
    save["fa_asks"] = {pid: asking(save, save["players"][pid]) for pid in save["fa"]}
    save["phase"] = "fa"
    save["used_mle"] = False
    ai.refresh_modes(save)


def fa_options(save: dict, pid: str) -> tuple[bool, str, int]:
    """Can the user sign this free agent at his asking price? (ok, how/why, amount)"""
    p = save["players"][pid]
    ask = save["fa_asks"][pid]
    season = save["season"] + 1
    cap = cba.cap_sheet(season)
    pay = L.payroll(save, save["team"])
    amt = ask["amount"]
    w, l = L.record(save, save["team"])
    appeal = 1.0 if w >= 47 else 1.08 if w >= 38 else 1.18
    if p["age"] <= 25:
        appeal = 1.0
    need = int(round(amt * appeal, -4))
    min_sal = int(cba.MIN_SALARY * (1 + cba.CAP_GROWTH) ** max(0, season - cba.FIRST_SEASON))
    if L.standard_count(save, save["team"]) >= cba.ROSTER_MAX_OFFSEASON:
        return False, "Your roster is full (18). Trade or release someone first.", need
    if pay + need <= cap["cap"]:
        return True, "using cap space", need
    if need <= min_sal * 1.05:
        return True, "on a minimum contract", min_sal
    if not save.get("used_mle"):
        if pay + need <= cap["apron1"] and need <= cap["mle"]:
            return True, "with the non-taxpayer mid-level exception", need
        if pay + need <= cap["apron2"] and need <= cap["tp_mle"]:
            return True, "with the taxpayer mid-level exception", need
    space = max(0, cap["cap"] - pay)
    return False, (f"He wants {cba.money(need)}{' (more to join a losing team)' if appeal > 1 else ''}. "
                   f"You have {cba.money(space)} in cap space"
                   + ("" if save.get("used_mle") else f" and your mid-level tops out at {cba.money(cap['mle'] if pay + need <= cap['apron1'] else cap['tp_mle'])}")
                   + "."), need


def sign_fa(save: dict, pid: str) -> str:
    ok, how, amt = fa_options(save, pid)
    if not ok:
        return how
    p = save["players"][pid]
    yrs = save["fa_asks"][pid]["years"]
    if "mid-level" in how:
        save["used_mle"] = True
        yrs = min(yrs, 4 if "non-taxpayer" in how else 2)
    if "minimum" in how:
        yrs = min(yrs, 2)
    p["salary"] = [int(amt * (1.05 ** i)) for i in range(yrs)]
    save["fa_asks"].pop(pid, None)
    L.move_player(save, pid, save["team"])
    p["acq"] = save["season"] + 1
    return f"✍️ Signed {p['name']} {how}: {yrs} yrs, {cba.money(sum(p['salary']))} total"


def ai_free_agency(save: dict, rng: random.Random) -> list[str]:
    season = save["season"] + 1
    cap = cba.cap_sheet(season)
    news = []
    pool = sorted(save["fa"], key=lambda i: -save["players"][i]["ovr"])
    used_mle: set[str] = set()
    teams = [a for a in cba.TEAMS if a != save["team"]]
    for pid in pool:
        p = save["players"][pid]
        ask = save.get("fa_asks", {}).get(pid) or asking(save, p)
        rng.shuffle(teams)
        teams.sort(key=lambda a: -(cap["cap"] - L.payroll(save, a)))
        for team in teams:
            if L.standard_count(save, team) >= cba.ROSTER_MAX:
                continue
            pay = L.payroll(save, team)
            amt = ask["amount"]
            how = None
            if pay + amt <= cap["cap"]:
                how = "room"
            elif team not in used_mle and pay + amt <= cap["apron1"] and amt <= cap["mle"]:
                how = "mle"
            elif amt <= cap["mle"] * 0.4 and L.standard_count(save, team) < 14:
                amt, how = int(cba.MIN_SALARY * (1 + cba.CAP_GROWTH) ** (season - cba.FIRST_SEASON)), "min"
            if not how:
                continue
            mode = ai.mode_of(save, team)
            if ai.player_value({**p, "salary": [amt] * ask["years"]}, mode, season) < 15 and how != "min":
                continue
            if how == "mle":
                used_mle.add(team)
            p["salary"] = [int(amt * (1.05 ** i)) for i in range(ask["years"] if how != "min" else 1)]
            L.move_player(save, pid, team)
            if p["ovr"] >= 78:
                news.append(f"✍️ {p['name']} signs with the {cba.TEAMS[team].name} ({ask['years']} yrs, {cba.money(sum(p['salary']))})")
            break
    for n in news[:8]:
        L.add_log(save, n, news=True)
    return news


def new_season(save: dict, rng: random.Random) -> dict:
    """Close free agency and roll into next season's preseason."""
    news = ai_free_agency(save, rng)
    old = save["season"]
    save["season"] = old + 1
    for kid in [k for k, v in save["picks"].items() if v["year"] <= old]:
        del save["picks"][kid]
    newest = save["season"] + cba.PICK_YEARS - 1
    for k in L.make_picks(newest, newest).values():
        save["picks"].setdefault(k["id"], k)
    cut = []
    for abbr in cba.TEAMS:
        names = L.trim_roster(save, abbr, cba.ROSTER_MAX)
        if abbr == save["team"]:
            cut = names
    save["fa"] = [pid for pid in save["fa"] if save["players"][pid]["ovr"] >= 50 and save["players"][pid]["age"] <= 36][:150]
    keep = set(save["fa"]) | {pid for ids in save["rosters"].values() for pid in ids}
    save["players"] = {k: v for k, v in save["players"].items() if k in keep}
    for k in ("game_idx", "draft", "playoffs", "asks", "fa_asks", "inbox"):
        save.pop(k, None)
    save["standings"] = {}
    save["phase"] = "preseason"
    save["used_mle"] = False
    ensure_rosters(save, rng)
    ai.refresh_modes(save)
    return {"signings": news, "cut": cut}
