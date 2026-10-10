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
