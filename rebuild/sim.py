"""Season simulation and the full offseason: injuries, standings, play-in, playoffs, awards,
progression/aging, draft lottery, draft, re-signing and free agency."""

from __future__ import annotations

import math
import random
import zlib

from . import ai, cba, chem, coaches, draft, stats, style
from . import league as L
from .ratings import noise, potential

GAMES = 82
DEADLINE_SHARE = 0.62          # trade deadline lands in early February
TOTAL_GAMES = 30 * GAMES // 2
DEC15 = int(TOTAL_GAMES * 55 / 174)   # game index around December 15
WEEK = 52                      # league games in a week of the schedule
SEASON_DAYS = 174              # opening night (late Oct) to the last regular-season game (mid Apr)
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
    return (L.strength(save, abbr, healthy_only=True) + chem.bonus(save, abbr)
            + save.get("form", {}).get(abbr, 0.0) + coaches.team_bonus(save, abbr))


def deadline_idx() -> int:
    return int(TOTAL_GAMES * DEADLINE_SHARE)


def calendar(save: dict) -> str:
    """Rough date in the season for the current game index ('Jan 14')."""
    import datetime
    idx = save.get("game_idx")
    if idx is None:
        return "Preseason"
    start = datetime.date(save["season"] - 1, 10, 21)
    d = start + datetime.timedelta(days=int(idx / TOTAL_GAMES * SEASON_DAYS))
    return d.strftime("%b %-d")


def date_at(save: dict, idx: int) -> str:
    """Calendar date of a game index in the current (or, in the offseason, next) season."""
    import datetime
    season = save["season"] + (0 if save["phase"] in ("preseason", "season", "deadline", "stretch") else 1)
    d = datetime.date(season - 1, 10, 21) + datetime.timedelta(days=int(idx / TOTAL_GAMES * SEASON_DAYS))
    return d.strftime("%b %-d")


def win_prob(r_home: float, r_away: float) -> float:
    return 1 / (1 + math.exp(-(WIN_K * (r_home - r_away) + HOME_EDGE)))


BASE_INJURY = 0.0072     # per player per game; about 10 games missed a season on average


def injury_risk(p: dict) -> float:
    """Durable guys (never miss games) rarely get hurt; injury-prone guys get hurt more; age adds risk.
    Injury-prone players in a contract year play through some of the small stuff."""
    dur = p.get("dur", 0.85)
    prone = 0.4 + 1.9 * (1 - dur) ** 1.15          # ~0.4x for iron men, ~1.5x+ for the most fragile
    age = 1 + max(0, p["age"] - 30) * 0.07
    return BASE_INJURY * prone * age


def contract_year(p: dict) -> bool:
    return len(p.get("salary") or []) == 1 and p["age"] >= 24 and not p.get("twoway")


def _injury_roll(save: dict, rng: random.Random, abbr: str, news: list[str], minutes: dict | None = None) -> None:
    mins = minutes or L.minutes(save, abbr)
    for p in L.roster(save, abbr)[:11]:
        if p.get("out", 0) > 0 or p.get("rest") or mins.get(p["id"], 0) <= 0:
            continue
        risk = injury_risk(p) * (0.6 + 0.4 * min(mins.get(p["id"], 0), 40) / 30)
        if mins.get(p["id"], 0) > 38:
            risk *= 1.25                                 # heavy minutes wear players down
        if rng.random() >= risk:
            continue
        sev = rng.random() - (0.08 if p.get("dur", 0.85) < 0.65 else 0)  # fragile bodies, longer stints
        if sev < 0.6 and contract_year(p) and p.get("dur", 0.85) < 0.75 and rng.random() < 0.3:
            continue                                     # playing through it for the next contract
        name, games, alter = _pick_injury(rng, sev)
        p["out"] = games
        p["inj"] = f"{name}, out {games} games" if games < 60 else f"{name}, out for the season"
        if alter and rng.random() < alter:
            _career_altering(p, name, rng)
        if games >= 4:
            log = save.setdefault("inj_log", [])
            log.insert(0, {"pid": p["id"], "name": p["name"], "team": abbr, "injury": name, "games": games,
                           "date": calendar(save), "ovr": p["ovr"], "altered": bool(p.get("altered", "").startswith(name))})
            del log[80:]
        if games >= 15 and p["ovr"] >= 78:
            news.append(f"🚑 {p['name']} ({abbr}): {name.lower()}, expected to miss "
                        + (f"about {games} games." if games < 60 else "the rest of the season."))


INJURIES = {
    "minor": [("Ankle sprain", 1, 6), ("Sore knee", 1, 4), ("Back spasms", 1, 5), ("Illness", 1, 3),
              ("Hip contusion", 1, 4), ("Sprained wrist", 1, 5)],
    "moderate": [("Hamstring strain", 5, 18), ("Calf strain", 6, 20), ("Sprained MCL", 8, 22),
                 ("Fractured hand", 10, 25), ("Groin strain", 5, 14), ("Bone bruise", 6, 16)],
    "severe": [("Torn meniscus", 15, 35), ("Fractured foot", 20, 45), ("Torn labrum", 25, 60),
               ("Stress reaction", 15, 40)],
}
MAJOR = [("Torn ACL", 70, 110, 0.30), ("Ruptured Achilles", 80, 120, 0.40)]


def _pick_injury(rng: random.Random, sev: float) -> tuple[str, int, float]:
    """(injury, games out, chance it permanently changes his career)."""
    if sev >= 0.975:
        name, lo, hi, alter = rng.choice(MAJOR)
        return name, rng.randint(lo, hi), alter
    tier = "minor" if sev < 0.6 else "moderate" if sev < 0.9 else "severe"
    name, lo, hi = rng.choice(INJURIES[tier])
    return name, rng.randint(lo, hi), (0.08 if tier == "severe" else 0.0)


def _career_altering(p: dict, injury: str, rng: random.Random) -> None:
    """Some major injuries leave a player noticeably worse for good."""
    drop = rng.randint(4, 9) if "Achilles" in injury else rng.randint(3, 7) if "ACL" in injury else rng.randint(2, 4)
    p["ovr"] = max(40, p["ovr"] - drop)
    p["pot"] = max(p["ovr"], p.get("pot", p["ovr"]) - drop)
    p["dur"] = round(max(0.3, p.get("dur", 0.85) - 0.08), 3)
    for k in ("sco", "def"):
        if k in p.get("attrs", {}):
            p["attrs"][k] = max(25, p["attrs"][k] - drop)
    p["altered"] = f"{injury} ({-drop} OVR)"


def _tick_injuries(save: dict, abbr: str) -> None:
    for pid in save["rosters"][abbr]:
        p = save["players"][pid]
        if p.get("rest"):
            p["rest"] = False
            p["gp_missed"] = p.get("gp_missed", 0) + 1
        if p.get("out", 0) > 0:
            p["out"] -= 1
            p["gp_missed"] = p.get("gp_missed", 0) + 1
            if p["out"] == 0:
                p["inj"] = None


def load_managed(p: dict) -> bool:
    """Old stars and injury-prone stars sit the second night of back-to-backs."""
    return p["ovr"] >= 78 and (p["age"] >= 35 or p.get("dur", 0.85) < 0.6)


def _rest_for_b2b(save: dict, abbr: str, game_idx: int, playoffs: bool) -> None:
    if playoffs:
        return
    if abbr == save["team"] and not (save.get("lineup") or {}).get("rest_b2b", True):
        return
    # roughly 14 back-to-backs a season; second nights are where teams rest guys
    if zlib.crc32(f"{save.get('sched_seed')}:{abbr}:{game_idx}".encode()) % 100 >= 17:  # same nights every run
        return
    for p in L.roster(save, abbr)[:8]:
        if p.get("out", 0) <= 0 and load_managed(p):
            p["rest"] = True


def play_game(save: dict, rng: random.Random, home: str, away: str, news: list[str], playoffs: bool = False) -> str:
    idx = save.get("game_idx", 0)
    for t in (home, away):
        _rest_for_b2b(save, t, idx, playoffs)
    rh = team_rating(save, home) + style.matchup(save, home, away)
    ra = team_rating(save, away)
    p = win_prob(rh, ra)
    winner = home if rng.random() < p else away
    loser = away if winner == home else home
    mins = {t: L.minutes(save, t) for t in (home, away)}
    lines = {t: stats.game_lines(save, t, mins[t], rng) for t in (home, away)}
    stats.normalize(lines[home], rh, ra, rng)
    stats.normalize(lines[away], ra, rh, rng)
    stats.settle_score(lines[winner], lines[loser], rng, rh - ra if winner == home else ra - rh)
    pts = {t: sum(x["pts"] for x in lines[t].values()) for t in (home, away)}
    for t in (home, away):
        stats.commit(save, t, lines[t], pts[loser if t == winner else winner])
        _injury_roll(save, rng, t, news, mins[t])
        _tick_injuries(save, t)
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
            p.pop("est", None)
            p.pop("twoway", None)
            L.move_player(save, p["id"], abbr)


def _start_season_if_needed(save: dict, rng: random.Random) -> None:
    if save.get("game_idx") is not None:
        return
    save["sched_seed"] = rng.randrange(1 << 30)
    save["game_idx"] = 0
    save["standings"] = {a: [0, 0] for a in cba.TEAMS}
    save["form"] = {a: round(rng.gauss(0, 0.4), 2) for a in cba.TEAMS}  # luck/health variance
    if "chem" not in save:
        chem.init(save)
    for p in save["players"].values():
        p["gp_missed"] = 0
        carry = p.pop("out_carry", 0)
        if carry and p.get("team"):
            p["out"] = carry
            p["inj"] = f"Still recovering ({carry} games)"
    stats.start_season(save, rng)
    draft.ensure_class(save, rng)
    ensure_rosters(save, rng)
    ai.refresh_modes(save)
    save["preseason_ranks"] = dict(save["ranks"])
    if save["phase"] == "preseason":
        save["phase"] = "season"


def sim_games(save: dict, rng: random.Random, until_idx: int) -> list[str]:
    _start_season_if_needed(save, rng)
    games = schedule(save["sched_seed"])
    news: list[str] = []
    while save["game_idx"] < min(until_idx, len(games)):
        home, away = games[save["game_idx"]]
        w = play_game(save, rng, home, away, news)
        loser = away if w == home else home
        save["standings"][w][0] += 1
        save["standings"][loser][1] += 1
        coaches.record_game(save, w, loser)
        save["game_idx"] += 1
    for n in news[-6:]:
        L.add_log(save, n, news=True)
    return news


def _deadline_event(save: dict, rng: random.Random) -> dict:
    save["phase"] = "deadline"
    save["deadline_done"] = True
    # Real deadlines see roughly 15-25 trades in the final week (21 in 2025)
    trades = ai.ai_trades(save, rng, max_trades=rng.randint(14, 22))
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


def sim_chunk(save: dict, rng: random.Random, games: int | None = None, to: int | None = None) -> dict:
    """Sim part of the regular season. Always stops at the trade deadline so you can make moves."""
    _start_season_if_needed(save, rng)
    start = save["game_idx"]
    target = to if to is not None else start + (games or WEEK)
    target = min(target, TOTAL_GAMES)
    dl = deadline_idx()
    if start < dl <= target:
        target = dl
    out = {"trades": [], "inbox": 0, "deadline": False, "games": 0, "news": []}
    # in-season trades between AI teams happen now and then before the deadline
    weeks = max(1, (target - start) // WEEK)
    news = []
    step = start
    while step < target:
        style.refresh(save)
        nxt = min(target, step + WEEK)
        news += sim_games(save, rng, nxt)
        news += chem.weekly(save, rng, max(1, round((nxt - step) / WEEK)))
        # Newly signed players can't be traded until Dec 15, so in-season deals pick up from mid-December
        if nxt < dl and save.get("game_idx", 0) >= DEC15 and rng.random() < 0.55:
            out["trades"] += ai.ai_trades(save, rng, max_trades=rng.randint(1, 2))
        step = nxt
    out["games"] = save["game_idx"] - start
    out["news"] = news
    if save["game_idx"] >= dl and not save.get("deadline_done"):
        ev = _deadline_event(save, rng)
        out["trades"] += ev["trades"]
        out["inbox"] = ev["inbox"]
        out["deadline"] = True
    elif save["game_idx"] > dl:
        save["phase"] = "stretch"
    if save["game_idx"] >= TOTAL_GAMES:
        save["phase"] = "stretch"
        if not save.get("awards"):
            out["awards"] = _end_regular_season(save, rng)
    del weeks
    return out


def _end_regular_season(save: dict, rng: random.Random) -> dict:
    stats.finish_season(save)
    save["awards"] = regular_awards(save, rng)
    L.add_log(save, f"🏅 {cba.season_label(save['season'])} MVP: {save['awards']['MVP']}", news=True)
    return save["awards"]


def sim_to_deadline(save: dict, rng: random.Random) -> dict:
    return sim_chunk(save, rng, to=deadline_idx())


def season_over(save: dict) -> bool:
    return (save.get("game_idx") or 0) >= TOTAL_GAMES


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
    style.refresh(save)
    need = best_of // 2 + 1
    wh = wl = 0
    homes = [high, high, low, low, high, low, high]
    g = 0
    while wh < need and wl < need:
        home = homes[g] if best_of == 7 else high
        away = low if home == high else high
        p = win_prob(team_rating(save, home) + style.matchup(save, home, away), team_rating(save, away))
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
    while not season_over(save):
        sim_chunk(save, rng, to=TOTAL_GAMES)
    if not save.get("awards"):
        _end_regular_season(save, rng)
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
    awards = {**save.get("awards", {}), "Finals MVP": finals_mvp(save, rng, champ)}
    me = save["team"]
    w, l = st[me]
    seed_txt = next((f"{conf} #{order.index(me) + 1}" for conf, order in sd.items() if me in order), "")
    entry = {"season": save["season"], "record": f"{w}-{l}", "seed": seed_txt, "result": result[me],
             "champion": champ, "awards": awards}
    save["history"].append(entry)
    if champ == me:
        save["titles"] = save.get("titles", 0) + 1
    coaches.end_of_season(save, champ)
    c = coaches.coach(save, me)
    entry["coach"] = c["name"] if c else ""
    save["playoffs"] = {"result": result, "log": bracket_log, "seeds": sd}
    save["phase"] = "postseason"
    save["live"] = False
    save.pop("inbox", None)
    for abbr in cba.TEAMS:  # remember each player's role (minutes) for development
        for pid, m in L.minutes(save, abbr, healthy_only=False).items():
            save["players"][pid]["role_min"] = m
    for p in save["players"].values():
        if p.get("out", 0) > 25:   # long injuries carry into next season
            p["out_carry"] = min(60, p["out"] - 25)
        p["out"] = 0
        p["inj"] = None if not p.get("out_carry") else f"Rehabbing ({p['out_carry']} games into next season)"
    L.add_log(save, f"🏆 {cba.TEAMS[champ].full} win the {cba.season_label(save['season'])} title", news=True)
    return entry


def _ln(p: dict) -> dict:
    return p.get("season_line") or stats.line(p) or {}


def _prod(p: dict) -> float:
    x = _ln(p)
    return x.get("pts", 0) + 0.8 * x.get("reb", 0) + 1.1 * x.get("ast", 0) + 1.5 * (x.get("stl", 0) + x.get("blk", 0))


def _fmt(p: dict) -> str:
    x = _ln(p)
    s = f" · {x['pts']} PTS / {x['reb']} REB / {x['ast']} AST" if x else ""
    return f"{p['name']} ({p['team']}){s}"


def regular_awards(save: dict, rng: random.Random) -> dict[str, str]:
    """End-of-regular-season awards (the 65-game rule applies to MVP, DPOY and All-NBA)."""
    st = save["standings"]
    rostered = [p for p in save["players"].values() if p.get("team") and _ln(p).get("gp", 0) > 0]
    eligible = [p for p in rostered if _ln(p).get("gp", 0) >= 65] or rostered

    def wp(p):
        w, l = st.get(p["team"], [41, 41])
        return w / max(1, w + l)
    mvp_score = {p["id"]: p["ovr"] * 0.8 + _prod(p) * 0.25 + wp(p) * 14 + rng.gauss(0, 1.0) for p in eligible}
    ranked = sorted(eligible, key=lambda p: -mvp_score[p["id"]])
    dpoy = max(eligible, key=lambda p: p.get("attrs", {}).get("def", 50) * 0.5 + p["ovr"] * 0.2
               + 6 * (_ln(p).get("stl", 0) + _ln(p).get("blk", 0)) + rng.gauss(0, 2))
    rookies = [p for p in rostered if p.get("exp", 0) == 0 and _ln(p).get("gp", 0) >= 40]
    roy = max(rookies, key=lambda p: _prod(p) + p["ovr"] * 0.3 + rng.gauss(0, 1)) if rookies else None
    bench = []
    for abbr in cba.TEAMS:
        five = {q["id"] for q in L.starters(save, abbr)}
        bench += [p for p in L.roster(save, abbr) if p["id"] not in five and _ln(p).get("gp", 0) >= 50]
    smoy = max(bench, key=lambda p: _prod(p) + rng.gauss(0, 1)) if bench else None

    def improved(p):
        prev = (p.get("hist") or {}).get(str(save["season"] - 1)) or {}
        if not prev or prev.get("gp", 0) < 25:
            return -99
        before = prev.get("pts", 0) + 0.8 * prev.get("reb", 0) + 1.1 * prev.get("ast", 0)
        return _prod(p) - before + rng.gauss(0, 0.8)
    mip = max(rostered, key=improved) if rostered else None
    exp = save.get("preseason_ranks") or {}
    coy_team = max(cba.TEAMS, key=lambda a: st[a][0] - (61 - (exp.get(a, 15) - 1) * 1.4))
    c = coaches.coach(save, coy_team)
    out = {"MVP": _fmt(ranked[0]), "DPOY": _fmt(dpoy)}
    if roy:
        out["ROY"] = _fmt(roy)
    if smoy:
        out["6MOY"] = _fmt(smoy)
    if mip and improved(mip) > 0:
        out["MIP"] = _fmt(mip)
    if c:
        out["COY"] = f"{c['name']} ({coy_team}, {st[coy_team][0]}-{st[coy_team][1]})"
    out["All-NBA 1st Team"] = ", ".join(f"{p['name']} ({p['team']})" for p in ranked[:5])
    return out


def finals_mvp(save: dict, rng: random.Random, champ: str) -> str:
    p = max(L.roster(save, champ)[:4], key=lambda p: p["ovr"] + _prod(p) * 0.2 + rng.gauss(0, 1.5))
    return _fmt(p)


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
            "inj": None, "jersey": "", "headshot": "", "generated": True, "dur": round(rng.uniform(0.68, 0.96), 3)}


def _top_mean(save: dict, n: int = 150) -> float:
    ovrs = sorted((p["ovr"] for p in save["players"].values() if p.get("team")), reverse=True)[:n]
    return sum(ovrs) / max(1, len(ovrs))


def progress_players(save: dict, rng: random.Random) -> list[str]:
    notes = []
    retired = []
    for p in list(save["players"].values()):
        a, ovr, pot = p["age"], p["ovr"], p.get("pot", p["ovr"])
        dev = coaches.dev_bonus(save, p.get("team")) if a <= 25 else 0.0
        if a <= 21:                                  # big jumps for teenagers
            d = (pot - ovr) * 0.24 + rng.gauss(0, 2.4)
        elif a <= 23:
            d = (pot - ovr) * 0.18 + rng.gauss(0, 2.0)
        elif a <= 25:                                # growth slows as he nears his peak
            d = (pot - ovr) * 0.10 + rng.gauss(0.2, 1.7)
        elif a <= 28:                                # prime: small ups and downs
            d = (pot - ovr) * 0.05 + rng.gauss(0.1, 1.4)
        elif a <= 30:                                # still prime, very slow fade
            d = rng.gauss(-0.3, 1.3)
        elif a <= 32:
            d = rng.gauss(-0.9, 1.3)
        elif a <= 33:
            d = rng.gauss(-1.7, 1.4)
        elif a <= 35:                                # real decline every year from here
            d = min(-1.0, rng.gauss(-3.0, 1.6))
        elif a <= 37:
            d = min(-1.5, rng.gauss(-4.0, 1.8))
        else:
            d = min(-2.0, rng.gauss(-5.0, 2.0))
        if a >= 33 and p["ovr"] >= 84 and noise(p["id"] + "ageless", 1) > 0.9:
            d = d * 0.45                              # the rare LeBron-type who ages slowly
        d += dev
        if a <= 24 and p.get("team"):
            d += max(-0.8, min(0.8, (p.get("role_min", 18) - 18) / 15))  # young guys grow with minutes
        if d > 0:
            d *= max(0.25, (99 - ovr) / 14) if ovr >= 85 else 1.0
        d = round(d)
        p["ovr"] = int(max(40, min(99, ovr + d)))
        p["age"] = a + 1
        p["exp"] = p.get("exp", 0) + 1
        if p["age"] <= 28:
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
    notes += [n for n in coaches.carousel(save, rng)]
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
    prospects = [dict(p) for p in draft.ensure_class(save, rng)]
    save["draft"] = {"order": order, "picks": picks, "idx": 0, "prospects": prospects, "log": []}
    save["phase"] = "draft"
    lot_top = ", ".join(f"#{i + 1} {a}" for i, a in enumerate(order[:4]))
    return {"notes": notes, "lottery": lot_top}


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
    for k in ("scout", "ceil", "floor", "rank", "big_board"):
        p.pop(k, None)
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
        for k in ("scout", "ceil", "floor", "rank", "big_board"):
            p.pop(k, None)
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
    p.pop("est", None)
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
                p.pop("est", None)
                p["expiring"] = False
            else:
                L.move_player(save, p["id"], None)
                p["expiring"] = False


def start_free_agency(save: dict, rng: random.Random) -> None:
    me = save["team"]
    for pid, a in list(save.get("asks", {}).items()):
        p = save["players"].get(pid)
        if p:
            if a.get("refuses"):
                p["refuses"] = me
            let_walk(save, pid)
            p["bird_team"] = me          # you keep his Bird rights in free agency
    for p in L.roster(save, me):
        if p.get("expiring"):
            let_walk(save, p["id"])
            p["bird_team"] = me
    ai_resign(save, rng)
    for t in ai.ai_trades(save, rng, max_trades=rng.randint(6, 12)):  # draft-night and summer deals
        L.add_log(save, f"🔁 Offseason trade: {t}", news=True)
    save["fa"] = [pid for pid in dict.fromkeys(save["fa"]) if pid in save["players"]]
    for pid in save["fa"]:
        p = save["players"][pid]
        p["salary"] = []
        p.pop("twoway", None)
    save["phase"] = "fa"
    save["fa_day"] = 0
    save["pending"] = {}
    save["used_mle"] = {}
    ai.refresh_modes(save)


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
            p.pop("est", None)
            p["signed"] = [season, None]
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
    for k in ("game_idx", "draft", "draft_class", "playoffs", "asks", "fa_asks", "inbox", "deadline_done", "pending", "fa_day",
              "inj_log", "awards"):
        save.pop(k, None)
    for p in save["players"].values():
        p.pop("bird_team", None)
        p.pop("refuses", None)
    save["standings"] = {}
    save["phase"] = "preseason"
    save["used_mle"] = {}
    ensure_rosters(save, rng)
    champ = save["history"][-1]["champion"] if save.get("history") else None
    chem.season_rollover(save, champ)
    ai.refresh_modes(save)
    return {"signings": news, "cut": cut}
