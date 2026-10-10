import random
import time

import pytest

from fixtures import fake_espn
from rebuild import ai, cba, sim
from rebuild import league as L
from rebuild import rumors as R
from rebuild.data import _salary_schedule, parse_roster


@pytest.fixture(scope="module")
def live():
    return fake_espn()


def fresh(live, team="NYK"):
    s = L.new_save(live, team, 1)
    ai.refresh_modes(s)
    return s


def test_contract_parsing_uses_espn_contract_and_future_years():
    a = {"contract": {"salary": 33_482_143, "yearsRemaining": 4},
         "contracts": [{"salary": 28_000_000, "season": {"year": 2026}},
                       {"salary": 33_482_143, "season": {"year": 2027}},
                       {"salary": 35_000_000, "season": {"year": 2028}}]}
    sched = _salary_schedule(a, 2027)
    assert sched[0] == 33_482_143 and sched[1] == 35_000_000 and len(sched) == 4
    assert _salary_schedule({"contracts": [{"salary": 5_000_000, "season": {"year": 2027}}]}, 2027) == [5_000_000]
    assert _salary_schedule({}, 2027) == []


def test_parse_roster_flags_two_ways_and_camp():
    roster = {"athletes": [{"id": "1", "displayName": "A", "age": 22, "position": {"abbreviation": "G"},
                            "contract": {"salary": 600_000, "yearsRemaining": 1}}]}
    p = parse_roster(roster, "NYK", 2027)[0]
    assert p["twoway"] and p["team"] == "NYK"


def test_new_save_trims_to_15_plus_two_ways(live):
    s = fresh(live)
    for abbr in cba.TEAMS:
        assert L.standard_count(s, abbr) <= 15
        assert len(s["rosters"][abbr]) <= 18
    assert sum(1 for k in s["picks"].values() if k["owner"] == "BKN" and k["orig"] == "NYK") == 3


def test_ratings_look_like_2k(live):
    ovrs = sorted((p["ovr"] for p in live["players"].values()), reverse=True)
    assert 95 <= ovrs[0] <= 99
    assert 64 <= ovrs[len(ovrs) // 2] <= 74


def test_salary_matching_and_aprons(live):
    s = fresh(live)
    rich = max(cba.TEAMS, key=lambda a: L.payroll(s, a))
    other = next(a for a in cba.TEAMS if a != rich)
    cheap = min(L.roster(s, rich, include_twoway=False), key=L.salary_now)
    big = max(L.roster(s, other, include_twoway=False), key=L.salary_now)
    t = ai.Trade(rich, other, [f"p:{cheap['id']}"], [f"p:{big['id']}"])
    probs = ai.validate(s, t)
    assert probs and any("apron" in p or "Salary matching" in p for p in probs)


def test_ai_accepts_overpay_and_rejects_lowball(live):
    s = fresh(live)
    me, them = "NYK", "BKN"
    mine = L.roster(s, me, include_twoway=False)
    theirs = L.roster(s, them, include_twoway=False)
    lowball = ai.Trade(me, them, [f"p:{mine[-1]['id']}"], [f"p:{theirs[0]['id']}"])
    r = ai.respond(s, lowball, random.Random(1))
    assert r.kind in ("reject", "invalid", "counter")
    finder = ai.what_would_it_take(s, ai.Trade(me, them, [], [f"p:{theirs[3]['id']}"]))
    if finder:
        assert ai.respond(s, finder, random.Random(1)).kind == "accept"


def test_shop_returns_legal_offers_fast(live):
    s = fresh(live)
    p = L.roster(s, "NYK", include_twoway=False)[2]
    t0 = time.time()
    offers = ai.shop_player(s, p["id"])
    assert time.time() - t0 < 6
    for t, _ in offers:
        assert not ai.validate(s, t)
        assert t.b == "NYK" and f"p:{p['id']}" in t.b_out


def test_stepien_rule(live):
    s = fresh(live)
    firsts = [k for k in L.team_picks(s, "BOS") if k["round"] == 1][:3]
    out = [f"k:{k['id']}" for k in firsts]
    assert ai.stepien(s, "BOS", [o[2:] for o in out], [])


def test_rumor_heat_and_tiers():
    players = {"1": {"name": "Giannis Antetokounmpo", "team": "MIL"}, "2": {"name": "Jaren Jackson Jr.", "team": "MEM"}}
    f = R.RumorFeed()
    f.set_players(players)
    now = time.time()
    f.ingest([R._item("[Charania] Giannis Antetokounmpo has requested a trade from the Bucks", "", "u", now, "r/nba"),
              R._item("Grizzlies not shopping Jackson", "Memphis has no intention of trading Jaren Jackson Jr., "
                      "per Sam Amick.", "u2", now, "RealGM"),
              R._item("random fan take", "Giannis Antetokounmpo should be traded", "u3", now, "blog")], now)
    assert f.heat["players"]["1"]["h"] > 0.6 and f.heat["players"]["1"]["tier"] == 1
    assert f.heat["players"]["2"]["h"] < -0.4


def test_live_heat_moves_ai_reluctance(live):
    s = fresh(live)
    team = "BOS"
    star = L.roster(s, team)[0]
    before = ai.reluctance(s, team, star)
    s["heat"] = {"players": {star["id"]: {"h": 0.9, "label": "Trade request"}}, "teams": {}}
    assert ai.reluctance(s, team, star) < before - 0.25
    s["live"] = False
    assert ai.reluctance(s, team, star) == pytest.approx(before)


def test_schedule_is_82_games_each():
    from collections import Counter
    games = sim.schedule(42)
    c = Counter()
    for h, a in games:
        c[h] += 1
        c[a] += 1
    assert len(games) == 1230 and set(c.values()) == {82}


def test_full_seasons_stay_sane(live):
    s = fresh(live)
    rng = random.Random(9)
    for _ in range(3):
        entry = sim.sim_postseason(s, rng)
        assert entry["champion"] in cba.TEAMS
        assert sum(w for w, _ in s["standings"].values()) == 1230
        sim.start_offseason(s, rng)
        sim.advance_draft(s, rng)
        while s["phase"] == "draft":
            sim.make_pick(s, s["draft"]["prospects"][0]["id"])
            sim.advance_draft(s, rng)
        sim.start_free_agency(s, rng)
        sim.new_season(s, rng)
        for abbr in cba.TEAMS:
            assert 13 <= L.standard_count(s, abbr) <= 15
        top = sorted((p["ovr"] for p in s["players"].values() if p.get("team")), reverse=True)
        assert top[0] <= 99 and top[10] < 97
    assert len(L.pack(s)) < 400_000


def test_missing_contract_is_estimated_not_two_way():
    from rebuild.data import build_league
    roster = {"athletes": [{"id": "9", "displayName": "Jalen Duren", "age": 22, "position": {"abbreviation": "C"},
                            "contracts": [{"salary": 4_500_000, "season": {"year": 2026}}]}]}
    p = parse_roster(roster, "DET", 2027)[0]
    assert not p["twoway"] and p["est"]
    stats = {2026: {"9": {"gp": 78, "mpg": 30, "pts": 12, "reb": 10, "ast": 2, "blk": 1, "stl": 0.7,
                          "fgm": 5, "fga": 7.5, "ftm": 2, "fta": 3, "tov": 1.5, "pf": 3}}}
    lg = build_league({"DET": [p]}, stats, 2027)
    q = lg["players"]["9"]
    assert q["salary"] and q["salary"][0] > 2_000_000 and len(q["salary"]) == 4
