"""Discord embeds for the Rebuild simulator (no interaction logic here)."""

from __future__ import annotations

import time

import discord

from . import ai, cba, chem, coaches, sim, stats, style
from . import league as L
from .ratings import ATTRS, grade
from .rumors import TIER_BADGE

ACCENT = 0xF58426


def color(abbr: str | None) -> int:
    return cba.TEAMS[abbr].color if abbr in cba.TEAMS else ACCENT


def badges(save: dict, p: dict) -> str:
    b = []
    h = ai.heat_of(save, p["id"]).get("h", 0)
    if h >= 0.55:
        b.append("🔥")
    elif h >= 0.25:
        b.append("👀")
    elif h <= -0.35:
        b.append("🔒")
    if p.get("out", 0) > 0 or p.get("inj"):
        b.append("🚑")
    if p["age"] <= 23 and p.get("pot", 0) - p["ovr"] >= 7:
        b.append("📈")
    if p.get("twoway"):
        b.append("`2W`")
    return (" " + " ".join(b)) if b else ""


def contract_text(p: dict) -> str:
    sal = p.get("salary") or []
    if p.get("twoway"):
        return "two-way"
    if not sal:
        return "unsigned"
    if p.get("est"):
        return f"~{cba.money(sal[0])} ×{len(sal)} (est.)"
    return f"{cba.money(sal[0])} ×{len(sal)}"


def player_line(save: dict, p: dict, bold: bool = True) -> str:
    name = f"**{p['name']}**" if bold else p["name"]
    return f"`{p['ovr']:>2}` {name} · {p['pos']} · {p['age']} · {contract_text(p)}{badges(save, p)}"


def footer(save: dict, live_updated: int | None = None) -> str:
    base = f"{cba.TEAMS[save['team']].full} · {cba.season_label(save['season'])} · {L.PHASES[save['phase']]}"
    if save.get("live"):
        base += " · live rumors ON"
    return base


def team_embed(save: dict, abbr: str) -> discord.Embed:
    info = cba.TEAMS[abbr]
    mode = ai.mode_of(save, abbr)
    ros = L.roster(save, abbr)
    cap = cba.cap_sheet(L.cap_year(save))
    pay = L.payroll(save, abbr)
    label, dot = L.cap_status(save, abbr)
    w, l = L.record(save, abbr)
    rank = (save.get("ranks") or L.strength_ranks(save)).get(abbr)
    title = f"{info.full}" + (" (your team)" if abbr == save["team"] else "")
    e = discord.Embed(title=title, color=color(abbr))
    rec = f" · **{w}-{l}**" if w + l else ""
    c = coaches.coach(save, abbr)
    coach_txt = f"Coach: **{c['name']}** ({coaches.rating(c)})" if c else ""
    e.description = (f"{ai.MODES[mode]} · Power rank **#{rank}**{rec}\n{coach_txt} · Chemistry "
                     f"{chem.label(chem.value(save, abbr))}\n{style.identity(save, abbr)}\n\n"
                     + "\n".join(player_line(save, p) for p in ros))
    std = L.standard_count(save, abbr)
    dead = (save.get("dead") or {}).get(abbr) or []
    dead_txt = f"\nDead money: {cba.money(dead[0])}" if dead and dead[0] else ""
    e.add_field(name="Cap sheet", value=(
        f"{dot} Payroll **{cba.money(pay)}** · {label}\n"
        f"Cap {cba.money(cap['cap'])} · Tax {cba.money(cap['tax'])} · Aprons {cba.money(cap['apron1'])} / "
        f"{cba.money(cap['apron2'])}\nRoster {std}/{ai.roster_limit(save)}{dead_txt}"), inline=False)
    firsts = [k for k in L.team_picks(save, abbr) if k["round"] == 1]
    seconds = [k for k in L.team_picks(save, abbr) if k["round"] == 2]
    pick_txt = ", ".join(L.pick_label(k) for k in firsts) or "No first-round picks"
    e.add_field(name=f"Draft capital · {len(firsts)} firsts, {len(seconds)} seconds", value=pick_txt[:1024], inline=False)
    e.set_footer(text="🔥 on the block · 👀 rumored · 🔒 off the table · 📈 rising · 🚑 injured")
    return e


def player_embed(save: dict, p: dict) -> discord.Embed:
    team = p.get("team")
    e = discord.Embed(title=f"{p['name']}", color=color(team))
    if p.get("headshot"):
        e.set_thumbnail(url=p["headshot"])
    where = cba.TEAMS[team].full if team else "Free agent"
    pot = p.get("pot", p["ovr"])
    src = " (NBA 2K27)" if p.get("ovr_src") == "2K27" else ""
    dur = p.get("dur", 0.85)
    health = ("🛡️ Iron man" if dur >= 0.93 else "✅ Durable" if dur >= 0.82 else "⚠️ Injury concerns" if dur >= 0.65
              else "🩹 Injury prone")
    mood = p.get("morale", 70)
    mood_txt = "😠 Wants out" if p.get("wants_out") else "😕 Unhappy" if mood < 45 else "🙂 Happy" if mood >= 60 else "😐 Content"
    cy = " · 📝 Contract year" if sim.contract_year(p) else ""
    e.description = (f"**{p['ovr']} OVR**{src} · {pot} POT · {p['pos']} · Age {p['age']} · {where}\n"
                     f"{health} · {mood_txt}{cy}"
                     + (f"\nDrafted {p['drafted']}" if p.get("drafted") else "")
                     + (f"\n🚑 {p['inj']}" if p.get("inj") else "")
                     + (f"\n🦵 Career altered by injury: {p['altered']}" if p.get("altered") else ""))
    if stats.line(p):
        e.add_field(name=f"{cba.season_label(save['season'])} season (sim)", value=stats.card_line(p), inline=False)
    attrs = " · ".join(f"{ATTRS[k]} **{grade(v)}**" for k, v in p.get("attrs", {}).items())
    if attrs:
        e.add_field(name="Scouting report", value=attrs, inline=False)
    line = p.get("line") or {}
    if line.get("gp"):
        shoot = ""
        if line.get("fg"):
            shoot = f" · {line['fg']}% FG"
        if line.get("tp"):
            shoot += f" · {line['tp']}% 3P"
        e.add_field(name="Real production (ESPN, recent seasons)",
                    value=(f"{line['pts']} PTS · {line['reb']} REB · {line['ast']} AST · {line['stl']} STL · "
                           f"{line['blk']} BLK · {line['mpg']} MIN{shoot}"), inline=False)
    sal = p.get("salary") or []
    if sal and not p.get("twoway"):
        yrs = " → ".join(f"{cba.season_label(save['season'] + i + (1 if save['phase'] in ('draft', 'resign', 'fa') else 0))[-5:]}: "
                         f"{cba.money(s)}" for i, s in enumerate(sal))
        mkt = ai.market_salary(p["ovr"], save["season"])
        est = "\n_Estimated: ESPN hasn't posted the terms of this deal yet._" if p.get("est") else ""
        e.add_field(name=f"Contract · {cba.money(sum(sal))} total",
                    value=f"{yrs}\nMarket value at his level: ~{cba.money(mkt)}/yr{est}", inline=False)
    if team and team != save["team"]:
        rel = ai.reluctance(save, team, p)
        stance = ("🚫 Untouchable" if rel >= ai.UNTOUCHABLE else "😬 Very reluctant" if rel >= 0.4 else
                  "🤔 Would need to be blown away" if rel >= 0.2 else "🙂 Available for the right price" if rel >= 0.0
                  else "🟢 Actively shopping him")
        e.add_field(name=f"{cba.TEAMS[team].name} stance", value=f"{stance} ({ai.MODES[ai.mode_of(save, team)]})",
                    inline=False)
    h = ai.heat_of(save, p["id"])
    if h.get("label"):
        when = f" <t:{int(h['ts'])}:R>" if h.get("ts") else ""
        src = h.get("reporter") or "report"
        corro = f" · {h['corroborated']} insiders" if h.get("corroborated", 0) > 1 else ""
        e.add_field(name=f"Live intel · {h['label']}",
                    value=f"{TIER_BADGE.get(h.get('tier', 4))} · {src}{when}{corro}\n[{h.get('title', '')[:180]}]({h.get('url') or 'https://www.espn.com/nba/'})",
                    inline=False)
    return e


def trade_embed(save: dict, t: ai.Trade, note: str | None = None) -> discord.Embed:
    a, b = cba.TEAMS[t.a], cba.TEAMS[t.b]
    e = discord.Embed(title=f"Trade talks: {a.name} ⇄ {b.name}", color=color(t.b))
    sal = lambda assets: sum(L.salary_now(save["players"][x[2:]]) for x in assets
                             if x.startswith("p:") and not save["players"][x[2:]].get("twoway"))
    send = "\n".join(f"• {ai.asset_label(save, x)}" for x in t.a_out) or "_Nothing yet_"
    get = "\n".join(f"• {ai.asset_label(save, x)}" for x in t.b_out) or "_Nothing yet_"
    e.add_field(name=f"You send ({cba.money(sal(t.a_out))})", value=send[:1024], inline=True)
    e.add_field(name=f"You get ({cba.money(sal(t.b_out))})", value=get[:1024], inline=True)
    if t.a_out or t.b_out:
        problems = ai.validate(save, t)
        if problems:
            e.add_field(name="❌ CBA check", value="\n".join(f"• {p}" for p in problems)[:1024], inline=False)
        else:
            e.add_field(name="✅ CBA check", value="Legal trade: salary matching, aprons, roster size and Stepien rule all pass.",
                        inline=False)
            ev = ai.evaluate(save, t.b, t.a_out, t.b_out)
            e.add_field(name=f"{b.name} front office", value=f"{ev.meter} · {ai.MODES[ai.mode_of(save, t.b)]}", inline=False)
            over = []
            for team, out, inc in ((t.a, t.a_out, t.b_out), (t.b, t.b_out, t.a_out)):
                n = L.standard_count(save, team) - sum(1 for x in out if x.startswith("p:") and not save["players"][x[2:]].get("twoway")) \
                    + sum(1 for x in inc if x.startswith("p:") and not save["players"][x[2:]].get("twoway"))
                if n > ai.roster_limit(save):
                    over.append(f"{cba.TEAMS[team].name} would have to waive {n - ai.roster_limit(save)} player(s) (dead money stays on the cap).")
            if over:
                e.add_field(name="⚠️ Roster crunch", value="\n".join(over), inline=False)
    if note:
        e.add_field(name="💬 " + b.name, value=note[:1024], inline=False)
    e.set_footer(text=f"Your payroll {cba.money(L.payroll(save, t.a))} ({L.cap_status(save, t.a)[0]}) · "
                      f"Theirs {cba.money(L.payroll(save, t.b))} ({L.cap_status(save, t.b)[0]})")
    return e


def offer_lines(save: dict, t: ai.Trade, me: str) -> tuple[str, str]:
    """(what you get, what you give) for a trade from either side."""
    give = t.a_out if t.a == me else t.b_out
    get = t.b_out if t.a == me else t.a_out
    return (", ".join(ai.asset_label(save, x) for x in get), ", ".join(ai.asset_label(save, x, False) for x in give))


def standings_embed(save: dict) -> discord.Embed:
    e = discord.Embed(title=f"{cba.season_label(save['season'])} Standings", color=ACCENT)
    if not save.get("standings"):
        e.description = "The season hasn't tipped off yet. Use `/rebuild sim` to start it.\n\n**Power rankings**\n" + "\n".join(
            f"{r}. {'**' + a + '**' if a == save['team'] else a} {ai.MODES[ai.mode_of(save, a)].split(' ')[0]}"
            for a, r in sorted((save.get("ranks") or L.strength_ranks(save)).items(), key=lambda x: x[1]))
        return e
    sd = sim.seeds(save)
    for conf, order in sd.items():
        lines = []
        for i, a in enumerate(order, 1):
            w, l = save["standings"][a]
            name = f"**{a}**" if a == save["team"] else a
            mark = " ·" if i in (6, 10) else ""
            lines.append(f"`{i:>2}` {name} {w}-{l}{mark}")
        e.add_field(name=conf, value="\n".join(lines), inline=True)
    played = save.get("game_idx", 0)
    e.set_footer(text=f"{played}/1230 games played · top 6 make the playoffs, 7-10 play-in")
    return e


def season_embed(save: dict, entry: dict) -> discord.Embed:
    me = save["team"]
    e = discord.Embed(title=f"{cba.season_label(entry['season'])} season complete", color=color(me))
    e.description = (f"**{cba.TEAMS[me].full}: {entry['record']}** ({entry['seed']})\n{entry['result']}")
    log = (save.get("playoffs") or {}).get("log") or []
    if log:
        e.add_field(name="Playoffs", value="\n".join(log[-7:])[:1024], inline=False)
    aw = "\n".join(f"**{k}:** {v}" for k, v in entry["awards"].items())
    e.add_field(name="Awards", value=aw[:1024], inline=False)
    mine = sorted((p for p in L.roster(save, me) if stats.line(p)), key=lambda p: -stats.line(p)["pts"])[:4]
    if mine:
        e.add_field(name="Your top players", value="\n".join(f"**{p['name']}** · {stats.card_line(p)}" for p in mine)[:1024],
                    inline=False)
    return e


def history_embed(save: dict) -> discord.Embed:
    e = discord.Embed(title=f"{cba.TEAMS[save['team']].full} · Your tenure", color=color(save["team"]))
    lines = [f"**{cba.season_label(h['season'])}** {h['record']} · {h['result']}" for h in save["history"]]
    e.description = ("\n".join(lines) or "No seasons finished yet.") + f"\n\n🏆 Titles: **{save.get('titles', 0)}** · Trades made: **{save.get('trades', 0)}**"
    return e


def summary(save: dict) -> str:
    me = save["team"]
    if save["history"]:
        h = save["history"][-1]
        return f"{h['record']} · {h['result']}"
    return f"{L.PHASES[save['phase']]} · power rank #{(save.get('ranks') or {}).get(me, '?')}"


def welcome_embed(save: dict, live_rumors: list[str]) -> discord.Embed:
    me = save["team"]
    info = cba.TEAMS[me]
    mode = ai.mode_of(save, me)
    goal = {"contend": "Ownership expects a **championship run**. The window is open now.",
            "middle": "Ownership wants a direction: **push for the playoffs or start a real rebuild**.",
            "rebuild": "Ownership is patient: **build a contender within 3 seasons**."}[mode]
    e = discord.Embed(title=f"🏀 Welcome to the {info.full} front office", color=color(me))
    rank = (save.get("ranks") or {}).get(me)
    c = coaches.coach(save, me)
    e.description = (f"You're the new GM. {goal}\n\nDirection: {ai.MODES[mode]} · Power rank **#{rank}** of 30\n"
                     f"Coach: **{c['name'] if c else 'none'}** · Chemistry {chem.label(chem.value(save, me))}\n"
                     f"Rosters, contracts and stats are live from ESPN as of <t:{save['data_as_of']}:R>.")
    core = "\n".join(player_line(save, p) for p in L.roster(save, me)[:6])
    e.add_field(name="Your core", value=core, inline=False)
    label, dot = L.cap_status(save, me)
    firsts = sum(1 for k in L.team_picks(save, me) if k["round"] == 1)
    e.add_field(name="Cap sheet", value=f"{dot} {cba.money(L.payroll(save, me))} · {label}", inline=True)
    e.add_field(name="Draft capital", value=f"{firsts} first-round picks (7 years)", inline=True)
    if live_rumors:
        e.add_field(name="📡 Live intel on your roster", value="\n".join(live_rumors)[:1024], inline=False)
    e.add_field(name="Get started", value=(
        "`/rebuild team` roster & cap sheet · `/rebuild trade` build a trade · `/rebuild shop` see offers for a player\n"
        "`/rebuild market` who's available · `/rebuild rumors` live feed · `/rebuild sim` play the season"), inline=False)
    e.set_footer(text=footer(save))
    return e


HELP = (
    "**Rebuild** puts you in charge of any NBA team using the real, current rosters and contracts. "
    "Type `/rebuild` once; everything else is buttons.\n\n"
    "**Menu**\n"
    "🏠 **Home** sim a week or a month, the deadline, playoffs, draft and free agency · 📋 **Roster** any team's "
    "roster, cap sheet and player cards · 🔁 **Trade** builder with a live CBA check and \"What would it take?\" · "
    "🛒 **Free agents** negotiate any time · 🧩 **Lineup** starters, minutes, load management\n"
    "☰ **More**: shop a player, offers inbox, extensions, release, coach, standings, league leaders, team stats, "
    "injury report, draft board, trade market, rumor feed, history, server leaderboard, switch teams\n\n"
    "**Where the data comes from**\n"
    "Rosters and stats: ESPN. Contracts: Basketball-Reference. Overalls: NBA 2K27 (2kratings.com). Rumors: RealGM, "
    "ESPN and r/nba insiders. Coaches and the 2027 draft class are real.\n\n"
    "**Realism**\n"
    "The sim always pauses at the trade deadline and trades lock after it. Chemistry builds over a season, players "
    "unhappy with their minutes can ask out, injury-prone players get hurt more and some major injuries change careers, "
    "prospects can bust, stars age, coaches give a small edge, and AI teams trade at real-world rates."
)
