import random
from collections import Counter

from fixtures import fake_espn
from rebuild import ai, cba, coaches, draft, fa, sim, sources
from rebuild import league as L

BBREF = """<table id="contracts"><tbody>
<tr><th scope="row" class="left" data-append-csv="durenja01" data-stat="player"><a href="/players/d/durenja01.html">Jalen Duren</a></th>
<td class="center" data-stat="age">22</td><td class="right" data-stat="y1" csk="40160643">$40,160,643</td>
<td class="right" data-stat="y2" csk="36947792">$36,947,792</td><td class="right" data-stat="y3" csk="38955824">$38,955,824</td>
<td class="right" data-stat="y4"></td><td class="right" data-stat="remain_gtd">$200,000,000</td></tr>
<tr><th data-stat="player">Team Totals</th><td data-stat="y1">$193,324,469</td></tr>
</tbody></table>"""

TWOK = """<table><tr><td><a href="https://www.2kratings.com/cade-cunningham">Cade Cunningham</a> <span>PG</span></td>
<td><span class="attribute-box rating-94">94</span></td><td><span class="attribute-box">88</span></td></tr>
<tr><td><a href="https://www.2kratings.com/jalen-duren">Jalen Duren</a></td><td><span class="attribute-box">85</span></td></tr>
<tr><td><a href="/teams">Teams</a> 77</td></tr></table>"""


def test_bbref_contract_parser():
    got = sources.parse_bbref_contracts(BBREF)
    assert got["jalen duren"]["salary"] == [40160643, 36947792, 38955824]
    assert "team totals" not in got


def test_2k_parser_only_known_players():
    got = sources.parse_2k_ratings(TWOK, {"cade cunningham", "jalen duren"})
    assert got == {"cade cunningham": 94, "jalen duren": 85}


def save_for(team="NYK"):
    s = L.new_save(fake_espn(), team, 1)
    ai.refresh_modes(s)
    return s


def test_free_agent_negotiation_and_cap_routes():
    s = save_for()
    sim.sim_postseason(s, random.Random(1))
    sim.start_offseason(s, random.Random(1))
    sim.advance_draft(s, random.Random(1), auto_user=True)
    sim.start_free_agency(s, random.Random(1))
    pool = sorted((s["players"][i] for i in s["fa"]), key=lambda p: -p["ovr"])
    p = pool[-1]
    low = fa.offer(s, p["id"], fa.min_salary(s), 1)
    assert low["kind"] in ("signed", "pending", "low", "illegal")
    big = pool[0]
    r = fa.offer(s, big["id"], 1_000_000, 1)
    assert r["kind"] in ("low", "illegal")
    news = fa.advance_day(s, random.Random(2))
    assert s["fa_day"] == 1 and isinstance(news, list)


def test_coach_fire_and_hire():
    s = save_for("BOS")
    old = coaches.coach(s, "BOS")["name"]
    coaches.fire(s, "BOS")
    assert coaches.coach(s, "BOS").get("interim")
    pick = coaches.available(s)[-1]
    ok, _ = coaches.hire(s, "BOS", pick["name"])
    assert ok and coaches.coach(s, "BOS")["name"] == pick["name"]
    assert any(c["name"] == old for c in coaches.available(s))


def test_durable_players_get_hurt_less():
    iron = {"dur": 0.99, "age": 28}
    glass = {"dur": 0.45, "age": 28}
    assert sim.injury_risk(glass) > 2.5 * sim.injury_risk(iron)
    old = {"dur": 0.85, "age": 39, "ovr": 85}
    assert sim.load_managed(old)


def test_draft_busts_follow_historical_rates():
    s = save_for()
    tiers = Counter()
    for seed in range(40):
        for p in draft.make_class(s, 2030, random.Random(seed))[:10]:
            tiers[draft.tier_name(p["pot"])] += 1
    total = sum(tiers.values())
    assert 0.1 < tiers["Bench/fringe"] / total < 0.3      # some lottery picks bust
    assert 0.15 < tiers["All-Star"] / total < 0.45


def test_real_2027_class_and_comps():
    s = save_for()
    cls = draft.ensure_class(s, random.Random(1))
    assert cls[0]["name"] == "Tyran Stokes"
    assert "Ceiling:" in draft.projection(s, cls[0])


def test_extension_and_trade_lock():
    s = save_for()
    p = next(q for q in L.roster(s, "NYK", include_twoway=False) if len(q["salary"]) == 1) \
        if any(len(q["salary"]) == 1 for q in L.roster(s, "NYK", include_twoway=False)) else None
    if p:
        ask = fa.extension_ask(s, p)
        r = fa.extend(s, p["id"], int(ask["amount"] * 1.1), ask["years"])
        assert r["kind"] in ("signed", "no")
    s["phase"] = "stretch"
    t = ai.Trade("NYK", "BKN", [f"p:{L.roster(s, 'NYK')[5]['id']}"], [f"p:{L.roster(s, 'BKN')[5]['id']}"])
    assert "deadline" in ai.validate(s, t)[0]


def test_deadline_stops_sim_and_ai_trades_happen():
    s = save_for()
    r = sim.sim_chunk(s, random.Random(4), to=sim.TOTAL_GAMES)
    assert s["game_idx"] == sim.deadline_idx() and r["deadline"] and s["phase"] == "deadline"
    assert 8 <= len(r["trades"]) <= 30


def test_2k27_snapshot_and_roster_fixes():
    from rebuild.data import build_league, parse_roster
    snap = sources.snapshot_2k()
    assert len(snap) > 500 and snap["jalen brunson"] == 96 and snap["cj mccollum"] == 83
    assert sources.name_key("Day’Ron Sharpe") == sources.name_key("Day'Ron Sharpe") == "dayron sharpe"
    roster = {"athletes": [
        {"id": "1", "displayName": "Jalen Brunson", "age": 30, "position": {"abbreviation": "PG"},
         "contract": {"salary": 43_000_000, "yearsRemaining": 3}},
        {"id": "2", "displayName": "Quentin Grimes", "age": 26, "position": {"abbreviation": "SG"}}]}
    rosters = {a: [] for a in cba.TEAMS}
    rosters["NYK"] = parse_roster(roster, "NYK", 2027)
    stats = {2026: {"1": {"gp": 75, "mpg": 35, "pts": 26}, "2": {"gp": 70, "mpg": 28, "pts": 14}}}
    lg = build_league(rosters, stats, 2027)
    assert lg["players"]["1"]["ovr"] == 96 and lg["players"]["1"]["ovr_src"] == "2K27"
    g = lg["players"]["2"]
    assert g["team"] == "LAL" and g["salary"][0] > 13_000_000 and len(g["salary"]) == 4
    assert "2" in lg["teams"]["LAL"]


def test_2k_parser_ignores_older_games():
    page = TWOK.replace("<table>", "<h2>NBA 2K27 Roster</h2><table>") + \
        '<h2>NBA 2K26</h2><a href="x">Jalen Duren</a><span class="attribute-box">80</span>'
    got = sources.parse_2k_ratings(page, {"jalen duren"})
    assert got == {"jalen duren": 85}


def test_payrolls_decide_rosters_and_retired_players_are_gone():
    """ESPN can lag: DeRozan still listed on the Kings with his old deal, and Westbrook (retired)
    in last season's stats. The payroll snapshot puts DeRozan on Denver, an unsigned vet ESPN still
    lists on a team goes to free agency, and retired players never show up as free agents."""
    from rebuild.data import build_league, parse_roster, refresh_snapshot
    roster = {"athletes": [
        {"id": "10", "displayName": "DeMar DeRozan", "age": 37, "position": {"abbreviation": "SF"},
         "contract": {"salary": 25_000_000, "yearsRemaining": 1}},
        {"id": "11", "displayName": "Stale Veteran", "age": 34, "position": {"abbreviation": "SG"},
         "contract": {"salary": 9_000_000, "yearsRemaining": 1}}]}
    rosters = {a: [] for a in cba.TEAMS}
    rosters["SAC"] = parse_roster(roster, "SAC", 2027)
    stats = {2026: {"10": {"gp": 77, "mpg": 33, "pts": 19}, "11": {"gp": 60, "mpg": 20, "pts": 8},
                    "12": {"gp": 70, "mpg": 25, "pts": 13}, "13": {"gp": 50, "mpg": 18, "pts": 7}}}
    meta = {"12": {"name": "Russell Westbrook", "age": 37, "pos": "PG"},
            "13": {"name": "Free Agent Guy", "age": 30, "pos": "SF"}}
    lg = build_league(rosters, stats, 2027, meta=meta)
    assert lg["players"]["10"]["team"] == "DEN" and "10" in lg["teams"]["DEN"]
    assert lg["players"]["11"]["team"] is None and "11" in lg["fa"]
    assert "12" not in lg["players"] and "13" in lg["fa"]
    assert not any(p["name"] in ("Russell Westbrook", "Nicolas Batum") for p in lg["players"].values())
    # a cached league built before a fix still gets it on the next boot
    old = {**lg, "players": {**lg["players"], "12": {**lg["players"]["13"], "id": "12", "name": "Russell Westbrook"}}}
    again = refresh_snapshot(old)
    assert "12" not in again["players"] and again["players"]["10"]["team"] == "DEN"


def test_new_free_agents_cant_be_flipped_right_away():
    s = save_for()
    s["phase"] = "fa"
    s["fa_day"] = 0
    p = s["players"][L.roster(s, "PHX", include_twoway=False)[-1]["id"]]
    L.waive(s, p["id"])
    p["bird_team"] = "NYK"          # Bird rights: always a legal route
    fa._sign(s, p["id"], "NYK", fa.min_salary(s), 2, "Bird rights")
    them = L.roster(s, "BKN", include_twoway=False)[-1]
    t = ai.Trade("NYK", "BKN", [f"p:{p['id']}"], [f"p:{them['id']}"])
    assert any("can't be traded until Dec" in x for x in ai.validate(s, t))
    # next season, from Dec 15 on, he's fair game
    s["season"] += 1
    s["phase"] = "season"
    s["game_idx"] = sim.DEC15 - 1
    assert any("just signed" in x for x in ai.validate(s, t))
    s["game_idx"] = sim.DEC15
    assert not any("just signed" in x for x in ai.validate(s, t))
    # in-season signings wait three months
    p["signed"] = [s["season"], sim.DEC15]
    assert any("just signed" in x for x in ai.validate(s, t))


def test_stale_buttons_never_replay_a_season_step(tmp_path):
    import asyncio
    import types
    from test_discord import FakeInteraction, choose, click, find
    from rebuild.cog import Rebuild

    c = Rebuild(types.SimpleNamespace(), None, str(tmp_path / "t.db"))
    c.live = fake_espn()
    c.feed.set_players(c.live["players"])

    async def flow():
        await c.store.init()
        i = FakeInteraction()
        await Rebuild.rebuild_cmd.callback(c, i)
        hub = i.sent[-1][3]
        await choose(hub, "Run the (East)", "NYK")
        i2 = FakeInteraction()
        await Rebuild.rebuild_cmd.callback(c, i2)          # a second, older /rebuild message
        stale_week = find(i2.sent[-1][3], "button", "Sim 1 week")
        save = await c.get_save(1)
        await click(hub, "Sim to trade deadline")
        await click(hub, "Sim to end of season")
        stale_playoffs = find(hub, "button", "Play the playoffs")
        await click(hub, "Play the playoffs")
        x = FakeInteraction(component=True)
        await stale_playoffs.callback(x)                   # double-click
        assert len(save["history"]) == 1 and "out of date" in x.last_embed().fields[-1].value
        await click(hub, "Start offseason")
        ages = {pid: p["age"] for pid, p in save["players"].items()}
        await stale_week.callback(FakeInteraction(component=True))
        assert save["phase"] in ("draft", "resign")
        assert all(save["players"][pid]["age"] == a for pid, a in ages.items() if pid in save["players"])

    asyncio.run(flow())


def test_a_broken_live_payroll_page_cant_empty_a_team():
    """If Basketball-Reference serves a page we only half-parse, the team keeps the bundled
    snapshot instead of releasing most of its roster."""
    import json
    from pathlib import Path
    from rebuild.data import build_league, load_payrolls, parse_roster
    snap = json.loads((Path(L.__file__).parent / "data" / "contracts_snapshot.json").read_text())["teams"]
    det = [r for r in snap["DET"] if r["salary"] and not r.get("waived")]
    # live page with only a few real players plus junk rows
    broken = {f"x{i}": {"name": f"Cap Hold {i}", "salary": [1_000_000]} for i in range(10)}
    broken.update({r["name"]: {"name": r["name"], "salary": r["salary"]} for r in det[:3]})
    pay = load_payrolls({"DET": broken})
    assert {r["name"] for r in pay["DET"]} == {r["name"] for r in snap["DET"]}
    # and even with bad payroll data, players with real ESPN contracts stay put
    athletes = [{"id": str(i), "displayName": r["name"], "age": 27, "position": {"abbreviation": "SF"},
                 "contract": {"salary": r["salary"][0], "yearsRemaining": len(r["salary"])}} for i, r in enumerate(det)]
    rosters = {a: [] for a in cba.TEAMS}
    rosters["DET"] = parse_roster({"athletes": athletes}, "DET", 2027)
    stats = {2026: {str(i): {"gp": 60, "mpg": 25, "pts": 12} for i in range(len(det))}}
    lg = build_league(rosters, stats, 2027)
    assert sum(1 for p in lg["players"].values() if p["team"] == "DET" and not p.get("twoway")) >= 13
