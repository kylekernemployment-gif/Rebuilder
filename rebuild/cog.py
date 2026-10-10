"""The /rebuild slash commands and their interactive menus."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import ai, cba, chem, coaches, fa, sim, style
from . import stats as stats_mod
from . import draft as draft_mod
from . import embeds as E
from . import league as L
from .data import fetch_espn, league_from
from .sources import fetch_2k, fetch_contracts, name_key
from .rumors import RumorFeed, TIER_BADGE, fetch_items, fold
from .storage import Store

log = logging.getLogger("rebuild")

LEAGUE_REFRESH_SECS = 6 * 3600
RUMOR_REFRESH_SECS = 10 * 60
VIEW_TIMEOUT = 15 * 60
MAX_CACHED_SAVES = 20
EXTRAS_REFRESH_SECS = 12 * 3600
LIVE_KEY = "live_league_v3"
EXTRAS_KEY = "extras_v1"
NO_PINGS = discord.AllowedMentions.none()


class NotReady(Exception):
    pass


# ------------------------------------------------------------------------------------------
# Base view: only the save's owner can press buttons
# ------------------------------------------------------------------------------------------

class OwnerView(discord.ui.View):
    def __init__(self, cog: "Rebuild", user_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.user_id = user_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your front office. Start your own with `/rebuild start`.",
                                                    ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log.exception("View error", exc_info=error)
        msg = "Something went wrong with that action. Try the command again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# ------------------------------------------------------------------------------------------
# Trade builder
# ------------------------------------------------------------------------------------------

def _player_option(save: dict, p: dict, selected: bool) -> discord.SelectOption:
    h = ai.heat_of(save, p["id"])
    tag = f" · {h['label']}" if h.get("label") and h.get("label") != "Injury" else ""
    sal = "two-way" if p.get("twoway") else f"{cba.money(L.salary_now(p))} ×{len(p.get('salary') or [])}"
    return discord.SelectOption(label=f"{p['name']} ({p['pos']}, {p['ovr']})"[:100], value=f"p:{p['id']}",
                                description=f"Age {p['age']} · {sal} · POT {p.get('pot', p['ovr'])}{tag}"[:100],
                                default=selected)


def _pick_option(save: dict, k: dict, selected: bool) -> discord.SelectOption:
    slot = ai.expected_slot(save, k)
    return discord.SelectOption(label=L.pick_label(k, short=True)[:100], value=f"k:{k['id']}",
                                description=f"Projects around #{round(slot) + (30 if k['round'] == 2 else 0)}",
                                default=selected, emoji="🎟️")


def asset_options(save: dict, team: str, chosen: list[str]) -> list[discord.SelectOption]:
    opts = [_player_option(save, p, f"p:{p['id']}" in chosen) for p in L.roster(save, team)]
    picks = L.team_picks(save, team)
    picks = sorted(picks, key=lambda k: (k["round"], k["year"]))
    for k in picks:
        if len(opts) >= 25:
            break
        opts.append(_pick_option(save, k, f"k:{k['id']}" in chosen))
    # keep anything already chosen visible even if it fell past the 25-option limit
    return opts[:25]


class SideSelect(discord.ui.Select):
    def __init__(self, view: "TradeView", mine: bool, options: list[discord.SelectOption]):
        team = view.trade.a if mine else view.trade.b
        placeholder = ("Your players & picks to send" if mine else f"{cba.TEAMS[team].name} players & picks to ask for")
        super().__init__(placeholder=placeholder, min_values=0, max_values=len(options), options=options,
                         row=0 if mine else 1)
        self.mine = mine

    async def callback(self, interaction: discord.Interaction) -> None:
        v: TradeView = self.view
        if self.mine:
            v.trade.a_out = list(self.values)
        else:
            v.trade.b_out = list(self.values)
        v.note = None
        await v.refresh(interaction)


class TradeView(OwnerView):
    def __init__(self, cog: "Rebuild", user_id: int, trade: ai.Trade, note: str | None = None):
        super().__init__(cog, user_id)
        self.trade = trade
        self.note = note
        self.done = False

    def build(self, save: dict) -> None:
        self.clear_items()
        self.add_item(SideSelect(self, True, asset_options(save, self.trade.a, self.trade.a_out)))
        self.add_item(SideSelect(self, False, asset_options(save, self.trade.b, self.trade.b_out)))
        self.add_item(self.propose)
        self.add_item(self.finder)
        self.add_item(self.clear)

    async def refresh(self, interaction: discord.Interaction) -> None:
        save = await self.cog.get_save(self.user_id)
        if not save:
            return await interaction.response.edit_message(content="Your save is gone.", embed=None, view=None)
        self.trade.a = save["team"]
        self.build(save)
        await interaction.response.edit_message(embed=E.trade_embed(save, self.trade, self.note), view=self)

    @discord.ui.button(label="Propose trade", style=discord.ButtonStyle.success, emoji="📨", row=2)
    async def propose(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            if not save:
                return
            if save["phase"] == "draft":
                return await interaction.response.send_message("Finish the draft first (`/rebuild sim`).", ephemeral=True)
            resp = await asyncio.to_thread(ai.respond, save, self.trade.copy())
            if resp.kind == "accept":
                text = ai.apply_trade(save, self.trade)
                save["trades"] = save.get("trades", 0) + 1
                await self.cog.persist(save, interaction)
                e = E.trade_embed(save, self.trade)
                e.clear_fields()
                e.title = f"✅ Trade accepted: {cba.TEAMS[self.trade.a].name} ⇄ {cba.TEAMS[self.trade.b].name}"
                you_get = "\n".join(f"• {ai.asset_label(save, x)}" for x in self.trade.b_out)
                you_send = "\n".join(f"• {ai.asset_label(save, x, False)}" for x in self.trade.a_out)
                e.add_field(name="You get", value=you_get[:1024], inline=True)
                e.add_field(name="You send", value=you_send[:1024], inline=True)
                e.add_field(name="💬 " + cba.TEAMS[self.trade.b].name, value=resp.message, inline=False)
                waived = save.get("last_waived", {}).get(save["team"])
                if waived:
                    e.add_field(name="✂️ Roster moves", value=f"You waived {', '.join(waived)} to make room.", inline=False)
                self.stop()
                return await interaction.response.edit_message(embed=e, view=None)
            if resp.kind == "invalid":
                self.note = "That isn't legal yet:\n" + "\n".join(f"• {p}" for p in resp.problems)
            elif resp.kind == "counter":
                self.trade = resp.counter
                self.note = f"{resp.message}\nCounter loaded. Hit **Propose** to accept it, or keep tweaking."
            else:
                self.note = f"❌ {resp.message}"
            self.build(save)
            await interaction.response.edit_message(embed=E.trade_embed(save, self.trade, self.note), view=self)

    @discord.ui.button(label="What would it take?", style=discord.ButtonStyle.primary, emoji="🔎", row=2)
    async def finder(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        save = await self.cog.get_save(self.user_id)
        if not self.trade.b_out:
            return await interaction.response.send_message("Pick at least one of their players or picks first.", ephemeral=True)
        await interaction.response.defer()
        found = await asyncio.to_thread(ai.what_would_it_take, save, self.trade.copy())
        if found:
            self.trade = found
            self.note = "Here's the cheapest package from your roster they'd say yes to. Hit **Propose** to make it official."
        else:
            ev = ai.evaluate(save, self.trade.b, self.trade.a_out, self.trade.b_out)
            self.note = (f"{', '.join(ev.untouchable)} is off the table." if ev.untouchable else
                         "Nothing on your roster gets this done under the CBA right now. Try asking for less, "
                         "or clear salary first.")
        self.build(save)
        await interaction.edit_original_response(embed=E.trade_embed(save, self.trade, self.note), view=self)

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.secondary, row=2)
    async def clear(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.trade.a_out, self.trade.b_out, self.note = [], [], None
        await self.refresh(interaction)


# ------------------------------------------------------------------------------------------
# Offers (shop + inbox)
# ------------------------------------------------------------------------------------------

class OffersView(OwnerView):
    def __init__(self, cog: "Rebuild", user_id: int, offers: list[dict], source: str):
        super().__init__(cog, user_id)
        self.offers = offers
        self.source = source
        for i, _ in enumerate(offers[:5]):
            b = discord.ui.Button(label=f"Accept offer {i + 1}", style=discord.ButtonStyle.success, row=0)
            b.callback = self._make_cb(i)
            self.add_item(b)

    def _make_cb(self, i: int):
        async def cb(interaction: discord.Interaction) -> None:
            async with self.cog.lock(self.user_id):
                save = await self.cog.get_save(self.user_id)
                t = ai.Trade.from_dict(self.offers[i])
                problems = ai.validate(save, t)
                if problems:
                    return await interaction.response.send_message(
                        "That offer is no longer valid:\n" + "\n".join(f"• {p}" for p in problems), ephemeral=True)
                ai.apply_trade(save, t)
                save["trades"] = save.get("trades", 0) + 1
                if self.source == "inbox":
                    save["inbox"] = [o for o in save.get("inbox", []) if o != self.offers[i]]
                await self.cog.persist(save, interaction)
                get, give = E.offer_lines(save, t, save["team"])
                other = t.a if t.b == save["team"] else t.b
                e = discord.Embed(title=f"✅ Deal done with the {cba.TEAMS[other].name}", color=E.color(other),
                                  description=f"**You get:** {get}\n**You send:** {give}")
                waived = save.get("last_waived", {}).get(save["team"])
                if waived:
                    e.add_field(name="✂️ Roster moves", value=f"You waived {', '.join(waived)} to make room.")
                self.stop()
                await interaction.response.edit_message(embed=e, view=None)
        return cb


def offers_embed(save: dict, offers: list[dict], title: str, intro: str) -> discord.Embed:
    e = discord.Embed(title=title, color=E.color(save["team"]), description=intro)
    for i, o in enumerate(offers[:5], 1):
        t = ai.Trade.from_dict(o)
        other = t.a if t.b == save["team"] else t.b
        get, give = E.offer_lines(save, t, save["team"])
        e.add_field(name=f"{i}. {cba.TEAMS[other].full} ({ai.MODES[ai.mode_of(save, other)]})",
                    value=f"**You get:** {get}\n**You send:** {give}"[:1024], inline=False)
    e.set_footer(text=E.footer(save))
    return e


# ------------------------------------------------------------------------------------------
# Season + offseason flow
# ------------------------------------------------------------------------------------------

class SimView(OwnerView):
    """One view whose buttons change with the phase of the season."""

    def __init__(self, cog: "Rebuild", user_id: int, save: dict):
        super().__init__(cog, user_id)
        self.build(save)

    def build(self, save: dict) -> None:
        self.clear_items()
        ph = save["phase"]
        if ph in ("preseason", "season", "deadline", "stretch") and not sim.season_over(save):
            self._button("Sim 1 week", "▶️", discord.ButtonStyle.primary, self.week)
            self._button("Sim 1 month", "⏩", discord.ButtonStyle.secondary, self.month)
            if not save.get("deadline_done"):
                self._button("Sim to trade deadline", "⏰", discord.ButtonStyle.secondary, self.to_deadline)
            else:
                self._button("Sim to end of season", "⏭️", discord.ButtonStyle.secondary, self.to_end_rs)
        elif ph in ("season", "deadline", "stretch"):
            self._button("Play the playoffs", "🏆", discord.ButtonStyle.success, self.playoffs)
        elif ph == "postseason":
            self._button("Start offseason (lottery + draft)", "🎟️", discord.ButtonStyle.primary, self.offseason)
        elif ph == "draft":
            prospects = (save.get("draft") or {}).get("prospects") or []
            slot = sim.on_the_clock(save)
            if slot and slot["owner"] == save["team"]:
                opts = [discord.SelectOption(
                    label=f"#{p['big_board']} {p['name']} ({p['pos']}, {p['age']})"[:100], value=p["id"],
                    description=f"OVR {p['ovr']} · Scouts' ceiling {p['scout'] - 3}-{p['scout'] + 3}"[:100])
                    for p in prospects[:25]]
                sel = discord.ui.Select(placeholder=f"You're on the clock at #{slot['no']}: pick a player", options=opts, row=0)
                sel.callback = self._draft_pick
                self.add_item(sel)
                self._button("Auto-pick best available", "🤖", discord.ButtonStyle.secondary, self.auto_pick)
            self._button("Auto-draft the rest", "⏩", discord.ButtonStyle.secondary, self.auto_rest)
        elif ph == "resign":
            asks = save.get("asks") or {}
            opts = []
            for pid, a in asks.items():
                p = save["players"].get(pid)
                if not p:
                    continue
                desc = "Won't re-sign: wants a fresh start" if a["refuses"] else \
                    f"Asking {cba.money(a['amount'])}/yr × {a['years']} (Bird rights)"
                opts.append(discord.SelectOption(label=f"{p['name']} ({p['pos']}, {p['ovr']}, age {p['age']})"[:100],
                                                 value=pid, description=desc[:100]))
            if opts:
                sel = discord.ui.Select(placeholder="Re-sign one of your free agents", options=opts[:25], row=0)
                sel.callback = self._resign
                self.add_item(sel)
            self._button("Open free agency", "🛒", discord.ButtonStyle.primary, self.open_fa)
        elif ph == "fa":
            self._button(f"Next free agency day (day {save.get('fa_day', 0) + 1})", "📅",
                         discord.ButtonStyle.primary, self.next_day)
            self._button("Finish offseason → next season", "🏁", discord.ButtonStyle.success, self.next_season)

    def _button(self, label, emoji, style, fn):
        b = discord.ui.Button(label=label, emoji=emoji, style=style, row=1)

        async def cb(interaction: discord.Interaction):
            await fn(interaction)
        b.callback = cb
        self.add_item(b)

    async def _run(self, interaction: discord.Interaction, work, make_embed) -> None:
        await interaction.response.defer()
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            rng = random.Random()
            result = await asyncio.to_thread(work, save, rng)
            await self.cog.persist(save, interaction)
            self.build(save)
            embed = make_embed(save, result)
            await interaction.edit_original_response(embed=embed, view=self if self.children else None)

    def _chunk_embed(self, save: dict, r: dict) -> discord.Embed:
        me = save["team"]
        w, l = L.record(save, me)
        title = "⏰ Trade deadline day" if r.get("deadline") else f"📅 {sim.calendar(save)}"
        e = discord.Embed(title=title, color=E.color(me))
        seed = next((f"#{o.index(me) + 1} in the {conf}" for conf, o in sim.seeds(save).items() if me in o), "")
        e.description = (f"**{cba.TEAMS[me].name}: {w}-{l}** · {seed}\n"
                         f"Chemistry: {chem.label(chem.value(save, me))} ({chem.value(save, me):.0f})")
        if r.get("trades"):
            e.add_field(name="Around the league", value="\n".join(f"🔁 {t}" for t in r["trades"])[:1024], inline=False)
        notes = [n for n in r.get("news", []) if n.startswith(("📢", "🤝"))][:5]
        notes += [n for n in r.get("news", []) if n.startswith("🚑")][:4]
        if notes:
            e.add_field(name="News", value="\n".join(notes)[:1024], inline=False)
        hurt = [p for p in L.roster(save, me) if p.get("out", 0) > 0]
        if hurt:
            e.add_field(name="Your injury report", value="\n".join(f"🚑 {p['name']}: {p['inj']}" for p in hurt)[:1024],
                        inline=False)
        if r.get("deadline"):
            msg = ("**Simulation paused for the deadline.** This is your last chance to trade until the summer "
                   "(`/rebuild trade`, `/rebuild shop`), and a good time to extend players in the final year "
                   "of their deals (`/rebuild extend`). Hit a sim button when you're done.")
            if r.get("inbox"):
                msg = f"📞 {r['inbox']} team(s) sent you offers (`/rebuild inbox`). " + msg
            e.add_field(name="Deadline", value=msg, inline=False)
        elif save["phase"] == "stretch":
            e.set_footer(text="Trade deadline has passed · free agents can still be signed")
        if r.get("awards"):
            e.add_field(name="🏅 Regular-season awards", value="\n".join(f"**{k}:** {v}" for k, v in r["awards"].items())[:1024],
                        inline=False)
        if sim.season_over(save):
            e.add_field(name="Regular season over", value="Hit **Play the playoffs**.", inline=False)
        return e

    async def week(self, interaction):
        await self._run(interaction, lambda s, rng: sim.sim_chunk(s, rng, games=sim.WEEK), self._chunk_embed)

    async def month(self, interaction):
        await self._run(interaction, lambda s, rng: sim.sim_chunk(s, rng, games=sim.WEEK * 4), self._chunk_embed)

    async def to_deadline(self, interaction):
        await self._run(interaction, sim.sim_to_deadline, self._chunk_embed)

    async def to_end_rs(self, interaction):
        await self._run(interaction, lambda s, rng: sim.sim_chunk(s, rng, to=sim.TOTAL_GAMES), self._chunk_embed)

    async def playoffs(self, interaction):
        await self._run(interaction, sim.sim_postseason, lambda save, entry: E.season_embed(save, entry))

    async def offseason(self, interaction):
        def work(save, rng):
            r = sim.start_offseason(save, rng)
            r["picks"] = sim.advance_draft(save, rng)
            return r

        def embed(save, r):
            e = discord.Embed(title=f"🎟️ {save['season']} Draft Lottery", color=E.color(save["team"]))
            e.description = f"Lottery results: {r['lottery']}"
            if r["notes"]:
                e.add_field(name="Offseason news", value="\n".join(r["notes"])[:1024], inline=False)
            e.add_field(name="Draft so far", value=_draft_log(save), inline=False)
            mine = [p for p in save["draft"]["picks"][save["draft"]["idx"]:] if p["owner"] == save["team"]]
            e.add_field(name="Your picks", value=", ".join(f"#{p['no']}" for p in mine) or "You don't have a pick left.",
                        inline=False)
            e.set_footer(text=E.footer(save))
            return e
        await self._run(interaction, work, embed)

    async def _draft_pick(self, interaction):
        pid = interaction.data["values"][0]
        await self._do_draft(interaction, lambda save, rng: [sim.make_pick(save, pid)] + sim.advance_draft(save, rng))

    async def auto_pick(self, interaction):
        await self._do_draft(interaction, lambda save, rng: [sim.ai_pick(save, rng)] + sim.advance_draft(save, rng))

    async def auto_rest(self, interaction):
        await self._do_draft(interaction, lambda save, rng: sim.advance_draft(save, rng, auto_user=True))

    async def _do_draft(self, interaction, work):
        def embed(save, lines):
            e = discord.Embed(title=f"🎟️ {save['season']} NBA Draft", color=E.color(save["team"]))
            e.add_field(name="Latest picks", value=_draft_log(save), inline=False)
            mine = [p for p in L.roster(save, save["team"]) if p.get("drafted", "").startswith(str(save["season"]))]
            if mine:
                e.add_field(name="Your draft class",
                            value="\n".join(f"{p['drafted']} · {E.player_line(save, p)}" for p in mine)[:1024], inline=False)
            if save["phase"] == "resign":
                e.add_field(name="Draft complete", value="Next: re-sign your own free agents, then free agency.", inline=False)
                e.add_field(name="Your free agents", value=_asks_text(save), inline=False)
            e.set_footer(text=E.footer(save))
            return e
        await self._run(interaction, work, embed)

    async def _resign(self, interaction):
        pid = interaction.data["values"][0]

        def work(save, rng):
            a = save["asks"].get(pid)
            if not a:
                return "He's no longer available."
            if a["refuses"]:
                return f"{save['players'][pid]['name']} wants a fresh start and won't re-sign."
            return sim.resign(save, pid)

        def embed(save, msg):
            e = discord.Embed(title="✍️ Re-signing period", color=E.color(save["team"]), description=msg)
            e.add_field(name="Your free agents", value=_asks_text(save), inline=False)
            e.add_field(name="Payroll next season", value=f"{cba.money(L.payroll(save, save['team']))} · "
                                                          f"{L.cap_status(save, save['team'])[0]}", inline=False)
            e.set_footer(text="Anyone you don't re-sign becomes a free agent (you keep his Bird rights).")
            return e
        await self._run(interaction, work, embed)

    async def open_fa(self, interaction):
        await self._run(interaction, sim.start_free_agency,
                        lambda save, _: fa_embed(save, "🛒 Free agency is open. Make offers with `/rebuild freeagents`, "
                                                       "then advance day by day. Stars weigh every offer before deciding."))

    async def next_day(self, interaction):
        await self._run(interaction, fa.advance_day,
                        lambda save, news: fa_embed(save, "\n".join(news[:8]) or "A quiet day. Nobody met their price."))

    async def next_season(self, interaction):
        def embed(save, r):
            e = discord.Embed(title=f"🏀 Welcome to the {cba.season_label(save['season'])} season", color=E.color(save["team"]))
            e.description = E.team_embed(save, save["team"]).description
            if r["signings"]:
                e.add_field(name="Free agency around the league", value="\n".join(r["signings"][:8])[:1024], inline=False)
            if r["cut"]:
                e.add_field(name="✂️ Final cuts", value=f"You waived {', '.join(r['cut'])} to get down to 15.", inline=False)
            e.set_footer(text=E.footer(save))
            return e
        await self._run(interaction, sim.new_season, embed)


def _draft_log(save: dict) -> str:
    d = save.get("draft") or {}
    lines = (d.get("log") or [])[-10:]
    return ("\n".join(f"**{x}**" if x.split(":")[0].endswith(save["team"]) else x for x in lines) or "No picks yet.")[:1024]


def _asks_text(save: dict) -> str:
    asks = save.get("asks") or {}
    lines = []
    for pid, a in asks.items():
        p = save["players"].get(pid)
        if p:
            lines.append(f"`{p['ovr']}` {p['name']} · {'won’t re-sign' if a['refuses'] else cba.money(a['amount']) + ' × ' + str(a['years'])}")
    return ("\n".join(lines) or "Nobody left to re-sign.")[:1024]


def fa_pool(save: dict, limit: int = 25) -> list[dict]:
    return sorted((save["players"][i] for i in dict.fromkeys(save["fa"]) if i in save["players"]
                   and not save["players"][i].get("team")), key=lambda p: -p["ovr"])[:limit]


def fa_embed(save: dict, msg: str) -> discord.Embed:
    me = save["team"]
    cap = cba.cap_sheet(L.cap_year(save))
    pay = L.payroll(save, me)
    off = save["phase"] in fa.OFFSEASON
    title = f"🛒 Free agency · day {save.get('fa_day', 0) + 1}" if save["phase"] == "fa" else "🛒 Free agents"
    e = discord.Embed(title=title, color=E.color(me), description=msg[:4000])
    used = fa._used_mle(save, me)
    mle = "used" if used else (cba.money(cap["mle"]) + " (non-taxpayer)" if pay < cap["apron1"] else
                               cba.money(cap["tp_mle"]) + " (taxpayer)" if pay < cap["apron2"] else "none (2nd apron)")
    e.add_field(name="Your books", value=(
        f"Payroll {cba.money(pay)} · {L.cap_status(save, me)[0]}\nMid-level: {mle} · Minimum: {cba.money(fa.min_salary(save))}\n"
        f"Roster {L.standard_count(save, me)}/{fa.roster_limit(save)}"), inline=False)
    lines = []
    for p in fa_pool(save, 12):
        want = fa.need(save, p, me)
        can = fa.max_legal_offer(save, me, p) >= want * 0.9
        pend = " · ⏳ your offer is pending" if p["id"] in (save.get("pending") or {}) else ""
        lines.append(f"{'✅' if can else '❌'} `{p['ovr']}` **{p['name']}** {p['pos']} · {p['age']} · "
                     f"wants ~{cba.money(want)} × {fa.pref_years(p)}{pend}")
    e.add_field(name="Best available (what they'd want from you)", value="\n".join(lines)[:1024] or "Market is empty.",
                inline=False)
    e.set_footer(text=("Stars take less to join contenders with good coaches and a real role. "
                       "Unsigned players' prices drop every day." if off else
                       "In-season free agents sign on the spot if your offer is good enough."))
    return e


class OfferModal(discord.ui.Modal):
    def __init__(self, view, p: dict, want: int, years: int, title: str = "Offer"):
        super().__init__(title=f"{title}: {p['name']}"[:45])
        self.view_ref = view
        self.pid = p["id"]
        self.amount = discord.ui.TextInput(label="Salary per year, in millions (e.g. 12.5)",
                                           default=f"{want / 1_000_000:.1f}", max_length=6)
        self.years = discord.ui.TextInput(label="Years (1-5)", default=str(years), max_length=1)
        self.add_item(self.amount)
        self.add_item(self.years)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount = int(round(float(str(self.amount.value).replace("$", "").replace("M", "").strip()) * 1_000_000, -4))
            years = int(str(self.years.value).strip())
        except ValueError:
            return await interaction.response.send_message("Use numbers, like 12.5 and 3.", ephemeral=True)
        await self.view_ref.make_offer(interaction, self.pid, amount, years)


class FAView(OwnerView):
    def __init__(self, cog: "Rebuild", user_id: int, save: dict):
        super().__init__(cog, user_id)
        self.build(save)

    def build(self, save: dict) -> None:
        self.clear_items()
        me = save["team"]
        opts = []
        for p in fa_pool(save):
            want = fa.need(save, p, me)
            opts.append(discord.SelectOption(label=f"{p['name']} ({p['pos']}, {p['ovr']}, age {p['age']})"[:100],
                                             value=p["id"],
                                             description=f"Wants ~{cba.money(want)}/yr × {fa.pref_years(p)}"[:100]))
        if opts:
            sel = discord.ui.Select(placeholder="Make an offer to a free agent", options=opts, row=0)
            sel.callback = self._pick
            self.add_item(sel)
        if save["phase"] == "fa":
            b = discord.ui.Button(label="Next free agency day", emoji="📅", style=discord.ButtonStyle.primary, row=1)
            b.callback = self._next_day
            self.add_item(b)

    async def _pick(self, interaction: discord.Interaction) -> None:
        save = await self.cog.get_save(self.user_id)
        p = save["players"].get(interaction.data["values"][0])
        if not p or p.get("team"):
            return await interaction.response.send_message("He just signed somewhere else.", ephemeral=True)
        want = fa.need(save, p, save["team"])
        await interaction.response.send_modal(OfferModal(self, p, want, fa.pref_years(p)))

    async def make_offer(self, interaction: discord.Interaction, pid: str, amount: int, years: int) -> None:
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            if save["phase"] == "draft":
                return await interaction.response.send_message("Finish the draft first.", ephemeral=True)
            r = fa.offer(save, pid, amount, years)
            await self.cog.persist(save, interaction)
            self.build(save)
            await interaction.response.edit_message(embed=fa_embed(save, r["msg"]), view=self)

    async def _next_day(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            news = await asyncio.to_thread(fa.advance_day, save, random.Random())
            await self.cog.persist(save, interaction)
            self.build(save)
            await interaction.edit_original_response(
                embed=fa_embed(save, "\n".join(news[:8]) or "A quiet day. Nobody met their price."), view=self)


class ExtendView(OwnerView):
    def __init__(self, cog: "Rebuild", user_id: int, save: dict):
        super().__init__(cog, user_id)
        self.build(save)

    def eligible(self, save: dict) -> list[dict]:
        return [p for p in L.roster(save, save["team"]) if fa.extension_eligible(save, p)][:25]

    def build(self, save: dict) -> None:
        self.clear_items()
        opts = []
        for p in self.eligible(save):
            a = fa.extension_ask(save, p)
            desc = f"Won't extend: {a['reason']}" if a["refuses"] else f"Wants ~{cba.money(a['amount'])}/yr × {a['years']}"
            opts.append(discord.SelectOption(label=f"{p['name']} ({p['pos']}, {p['ovr']}, age {p['age']})"[:100],
                                             value=p["id"], description=desc[:100]))
        if opts:
            sel = discord.ui.Select(placeholder="Offer an extension", options=opts, row=0)
            sel.callback = self._pick
            self.add_item(sel)

    def embed(self, save: dict, note: str | None = None) -> discord.Embed:
        e = discord.Embed(title="📝 Contract extensions", color=E.color(save["team"]))
        rows = []
        for p in self.eligible(save):
            a = fa.extension_ask(save, p)
            rows.append(f"`{p['ovr']}` **{p['name']}** · {p['age']} · now {cba.money(L.salary_now(p))} · "
                        + (f"❌ {a['reason']}" if a["refuses"] else f"wants ~{cba.money(a['amount'])} × {a['years']}"))
        e.description = "\n".join(rows) or "Nobody on your roster is in the last year of his contract."
        if note:
            e.add_field(name="Update", value=note[:1024], inline=False)
        e.set_footer(text="Extend players before they hit free agency. Unhappy players and stars on bad teams may want to test the market.")
        return e

    async def _pick(self, interaction: discord.Interaction) -> None:
        save = await self.cog.get_save(self.user_id)
        p = save["players"][interaction.data["values"][0]]
        a = fa.extension_ask(save, p)
        if a["refuses"]:
            return await interaction.response.edit_message(
                embed=self.embed(save, f"❌ {p['name']} won't discuss an extension. {a['reason']}"), view=self)
        await interaction.response.send_modal(OfferModal(self, p, a["amount"], a["years"], title="Extension"))

    async def make_offer(self, interaction: discord.Interaction, pid: str, amount: int, years: int) -> None:
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            r = fa.extend(save, pid, amount, years)
            await self.cog.persist(save, interaction)
            self.build(save)
            await interaction.response.edit_message(embed=self.embed(save, r["msg"]), view=self)


# ------------------------------------------------------------------------------------------
# Lineup, coach, release
# ------------------------------------------------------------------------------------------

def lineup_embed(save: dict, note: str | None = None) -> discord.Embed:
    me = save["team"]
    mins = L.minutes(save, me, healthy_only=False)
    five = {p["id"] for p in L.starters(save, me)}
    custom = bool((save.get("lineup") or {}).get("minutes"))
    e = discord.Embed(title=f"🧩 {cba.TEAMS[me].name} rotation", color=E.color(me))
    lines = []
    for p in sorted(L.roster(save, me), key=lambda p: (p["id"] not in five, -mins.get(p["id"], 0), -p["ovr"])):
        exp = chem.expected_minutes(save, me, p)
        mood = "😠" if p.get("wants_out") else "😕" if p.get("morale", 70) < 45 else ""
        flag = " ⚠️ wants more" if mins.get(p["id"], 0) <= exp - 8 and p["ovr"] >= 70 else ""
        lm = " 💤 rests B2Bs" if sim.load_managed(p) and (save.get("lineup") or {}).get("rest_b2b", True) else ""
        star = "⭐ " if p["id"] in five else ""
        lines.append(f"{star}`{mins.get(p['id'], 0):>2} min` **{p['name']}** {p['pos']} · {p['ovr']}{badge_inj(p)}{mood}{flag}{lm}")
    e.description = "\n".join(lines)
    total = sum(mins.values())
    e.add_field(name="Settings", value=(
        f"{'Custom minutes' if custom else 'Coach decides (auto)'} · {total}/240 minutes\n"
        f"Chemistry: {chem.label(chem.value(save, me))} ({chem.value(save, me):.0f}) · "
        f"Load management: {'on' if (save.get('lineup') or {}).get('rest_b2b', True) else 'off'}"), inline=False)
    if note:
        e.add_field(name="Update", value=note[:1024], inline=False)
    e.set_footer(text="Players who get well under the minutes their level deserves get unhappy and can request a trade. "
                      "Over 36 minutes adds fatigue and injury risk.")
    return e


def badge_inj(p: dict) -> str:
    return " 🚑" if p.get("out", 0) > 0 else ""


class MinutesModal(discord.ui.Modal):
    def __init__(self, view: "LineupView", p: dict, current: int):
        super().__init__(title=f"Minutes: {p['name']}"[:45])
        self.view_ref = view
        self.pid = p["id"]
        self.mins = discord.ui.TextInput(label="Minutes per game (0-42)", default=str(current), max_length=2)
        self.add_item(self.mins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            m = max(0, min(42, int(str(self.mins.value).strip())))
        except ValueError:
            return await interaction.response.send_message("Use a number from 0 to 42.", ephemeral=True)
        await self.view_ref.set_minutes(interaction, self.pid, m)


class LineupView(OwnerView):
    def __init__(self, cog: "Rebuild", user_id: int, save: dict):
        super().__init__(cog, user_id)
        self.build(save)

    def build(self, save: dict) -> None:
        self.clear_items()
        me = save["team"]
        ros = L.roster(save, me)[:25]
        five = {p["id"] for p in L.starters(save, me)}
        s1 = discord.ui.Select(placeholder="Choose your 5 starters", min_values=5, max_values=5, row=0,
                               options=[discord.SelectOption(label=f"{p['name']} ({p['pos']}, {p['ovr']})"[:100],
                                                             value=p["id"], default=p["id"] in five) for p in ros])
        s1.callback = self._starters
        self.add_item(s1)
        mins = L.minutes(save, me, healthy_only=False)
        s2 = discord.ui.Select(placeholder="Change a player's minutes", row=1,
                               options=[discord.SelectOption(label=f"{p['name']}: {mins.get(p['id'], 0)} min"[:100],
                                                             value=p["id"]) for p in ros])
        s2.callback = self._pick_minutes
        self.add_item(s2)
        auto = discord.ui.Button(label="Let the coach decide (auto)", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
        auto.callback = self._auto
        self.add_item(auto)
        on = (save.get("lineup") or {}).get("rest_b2b", True)
        lm = discord.ui.Button(label=f"Load management: {'on' if on else 'off'}", emoji="💤",
                               style=discord.ButtonStyle.secondary, row=2)
        lm.callback = self._toggle_lm
        self.add_item(lm)

    def _ensure_custom(self, save: dict) -> dict:
        lu = save.setdefault("lineup", {})
        if not lu.get("minutes"):
            lu["minutes"] = dict(L.minutes(save, save["team"], healthy_only=False))
        return lu

    async def _update(self, interaction, save, note):
        await self.cog.persist(save, interaction)
        self.build(save)
        await interaction.response.edit_message(embed=lineup_embed(save, note), view=self)

    async def _starters(self, interaction: discord.Interaction) -> None:
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            lu = self._ensure_custom(save)
            lu["starters"] = list(interaction.data["values"])
            for pid in lu["starters"]:
                lu["minutes"][pid] = max(lu["minutes"].get(pid, 0), 24)
            note = _rebalance(save)
            await self._update(interaction, save, "Starters set. " + note)

    async def _pick_minutes(self, interaction: discord.Interaction) -> None:
        save = await self.cog.get_save(self.user_id)
        pid = interaction.data["values"][0]
        cur = L.minutes(save, save["team"], healthy_only=False).get(pid, 0)
        await interaction.response.send_modal(MinutesModal(self, save["players"][pid], cur))

    async def set_minutes(self, interaction: discord.Interaction, pid: str, m: int) -> None:
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            lu = self._ensure_custom(save)
            lu["minutes"][pid] = m
            note = _rebalance(save, keep=pid)
            await self._update(interaction, save, f"{save['players'][pid]['name']} set to {m} minutes. {note}")

    async def _auto(self, interaction: discord.Interaction) -> None:
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            lu = save.setdefault("lineup", {})
            lu["minutes"], lu["starters"] = {}, []
            await self._update(interaction, save, "Back to the coach's rotation.")

    async def _toggle_lm(self, interaction: discord.Interaction) -> None:
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            lu = save.setdefault("lineup", {})
            lu["rest_b2b"] = not lu.get("rest_b2b", True)
            await self._update(interaction, save, "Load management " + ("on: old and injury-prone stars sit back-to-backs."
                                                                         if lu["rest_b2b"] else "off: everyone plays every night."))


def _rebalance(save: dict, keep: str | None = None) -> str:
    """Keep the rotation at 240 minutes by trimming/adding to the bench, never the player just set."""
    lu = save["lineup"]
    mins = lu["minutes"]
    ros = [p["id"] for p in L.roster(save, save["team"])]
    for pid in list(mins):
        if pid not in ros:
            del mins[pid]
    total = sum(mins.values())
    order = sorted(ros, key=lambda i: save["players"][i]["ovr"])  # adjust the deep bench first
    diff = 240 - total
    for pid in (order if diff < 0 else list(reversed(order))):
        if diff == 0:
            break
        if pid == keep:
            continue
        cur = mins.get(pid, 0)
        new = max(0, min(36, cur + diff))
        diff -= new - cur
        mins[pid] = new
    return "" if diff == 0 else f"⚠️ Couldn't balance to 240 ({240 - diff} set); the sim fills or trims the rest."


def coach_embed(save: dict, note: str | None = None) -> discord.Embed:
    me = save["team"]
    c = coaches.coach(save, me)
    e = discord.Embed(title=f"🧑‍💼 {cba.TEAMS[me].name} head coach", color=E.color(me))
    if c:
        rec = f"{c['w']}-{c['l']} with the team" + (f" · 🏆×{c['titles']}" if c.get("titles") else "")
        e.description = (f"**{c['name']}** · {coaches.rating(c)} overall{' (interim)' if c.get('interim') else ''}\n"
                         f"Offense {c['off']} · Defense {c['def']} · Player development {c['dev']}\n{rec}")
        hist = save.get("history") or []
        if hist and not c.get("interim"):
            last = hist[-1]
            hot = "🔥 Hot seat" if last["result"] in ("Missed playoffs", "Lost in Play-In") and coaches.rating(c) < 80 else ""
            if hot:
                e.description += f"\n{hot}: ownership expected more than {last['record']}."
    avail = coaches.available(save)[:10]
    e.add_field(name="Available coaches", value="\n".join(
        f"`{coaches.rating(x)}` **{x['name']}** · O {x['off']} / D {x['def']} / Dev {x['dev']}"
        + (f" · _{x['note']}_" if x.get("note") else "") for x in avail)[:1024] or "Nobody available.", inline=False)
    if note:
        e.add_field(name="Update", value=note[:1024], inline=False)
    e.set_footer(text="Coaches add a few wins (or cost a few) and speed up young players' development.")
    return e


class CoachView(OwnerView):
    def __init__(self, cog: "Rebuild", user_id: int, save: dict):
        super().__init__(cog, user_id)
        self.build(save)

    def build(self, save: dict) -> None:
        self.clear_items()
        avail = coaches.available(save)[:25]
        if avail:
            sel = discord.ui.Select(placeholder="Hire a coach", row=0, options=[
                discord.SelectOption(label=f"{c['name']} ({coaches.rating(c)})"[:100], value=c["name"],
                                     description=f"O {c['off']} / D {c['def']} / Dev {c['dev']} · age {c.get('age', '?')}"[:100])
                for c in avail])
            sel.callback = self._hire
            self.add_item(sel)
        c = coaches.coach(save, save["team"])
        if c and not c.get("interim"):
            b = discord.ui.Button(label=f"Fire {c['name']}", emoji="🔥", style=discord.ButtonStyle.danger, row=1)
            b.callback = self._fire
            self.add_item(b)

    async def _fire(self, interaction: discord.Interaction) -> None:
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            msg = coaches.fire(save, save["team"])
            await self.cog.persist(save, interaction)
            self.build(save)
            await interaction.response.edit_message(embed=coach_embed(save, msg), view=self)

    async def _hire(self, interaction: discord.Interaction) -> None:
        async with self.cog.lock(self.user_id):
            save = await self.cog.get_save(self.user_id)
            ok, msg = coaches.hire(save, save["team"], interaction.data["values"][0])
            await self.cog.persist(save, interaction)
            self.build(save)
            await interaction.response.edit_message(embed=coach_embed(save, msg), view=self)


class ConfirmView(OwnerView):
    def __init__(self, cog, user_id, on_yes, yes_label: str = "Yes, start over"):
        super().__init__(cog, user_id)
        self.on_yes = on_yes
        yes = discord.ui.Button(label=yes_label[:80], style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        yes.callback = self._yes
        no.callback = self._no
        self.add_item(yes)
        self.add_item(no)

    async def _yes(self, interaction: discord.Interaction):
        self.stop()
        await self.on_yes(interaction)

    async def _no(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Never mind, nothing changed.", embed=None, view=None)


# ------------------------------------------------------------------------------------------
# The cog
# ------------------------------------------------------------------------------------------

async def team_autocomplete(_: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower().strip()
    out = []
    for abbr, t in cba.TEAMS.items():
        if not cur or cur in t.full.lower() or cur in abbr.lower() or any(cur in n for n in t.nicknames):
            out.append(app_commands.Choice(name=t.full, value=abbr))
    return out[:25]


class Rebuild(commands.Cog):
    rebuild = app_commands.Group(name="rebuild", description="NBA front office simulator with live rosters and rumors")

    def __init__(self, bot: commands.Bot, database_url: str | None = None, db_path: str = "rebuild.db"):
        self.bot = bot
        self.store = Store(database_url, db_path)
        self.session: aiohttp.ClientSession | None = None
        self.live: dict | None = None
        self.feed = RumorFeed()
        self.saves: dict[int, dict] = {}
        self.locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last_league = 0.0
        self.last_rumors = 0.0
        self.loading = False
        self.raw: dict | None = None
        self.extras: dict = {}
        self.last_extras = 0.0
        self.extras_task: asyncio.Task | None = None

    # --- lifecycle ---
    async def cog_load(self) -> None:
        await self.store.init()
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        cached = await self.store.get_cache(LIVE_KEY)
        if cached:
            try:
                self.live = L.unpack(cached[0])
                self.last_league = cached[1]
                self.feed.set_players(self.live["players"])
                log.info("Loaded cached league (%d players)", len(self.live["players"]))
            except Exception:
                log.exception("Cached league unreadable")
        cached = await self.store.get_cache(EXTRAS_KEY)
        if cached:
            try:
                self.extras = L.unpack(cached[0])
                self.last_extras = cached[1]
            except Exception:
                log.exception("Cached contracts/ratings unreadable")
        cached = await self.store.get_cache("rumors")
        if cached and self.live:
            try:
                self.feed.load(L.unpack(cached[0]))
            except Exception:
                log.exception("Cached rumors unreadable")
        self.refresh.start()

    async def cog_unload(self) -> None:
        self.refresh.cancel()
        if self.extras_task:
            self.extras_task.cancel()
        if self.session:
            await self.session.close()

    async def rebuild_live(self) -> None:
        if not self.raw:
            return
        live = await asyncio.to_thread(league_from, self.raw, self.extras)
        self.live = live
        self.feed.set_players(live["players"])
        await self.store.put_cache(LIVE_KEY, L.pack(live))

    async def refresh_extras(self) -> None:
        """Real contracts (Basketball-Reference) and NBA 2K27 ratings. Slow and polite, so it runs
        in the background a couple of times a day."""
        try:
            contracts = await fetch_contracts(self.session)
            known = {name_key(p["name"]) for p in (self.live or {}).get("players", {}).values()}
            ratings = await fetch_2k(self.session, known)
            changed = False
            if len(contracts) >= 20:
                self.extras["contracts"] = contracts
                changed = True
            if len(ratings) >= 250:
                self.extras["ratings"] = ratings
                changed = True
            self.last_extras = time.time()
            if changed:
                await self.store.put_cache(EXTRAS_KEY, L.pack(self.extras))
                await self.rebuild_live()
        except Exception:
            log.exception("Contract/ratings refresh failed")
            self.last_extras = time.time() - EXTRAS_REFRESH_SECS + 1800

    @tasks.loop(minutes=2)
    async def refresh(self) -> None:
        now = time.time()
        if self.raw is None or now - self.last_league >= LEAGUE_REFRESH_SECS:
            self.loading = True
            try:
                raw = await fetch_espn(self.session)
                if raw:
                    self.raw = raw
                    await self.rebuild_live()
                self.last_league = now if raw else now - LEAGUE_REFRESH_SECS + 300
            except Exception:
                log.exception("League refresh failed")
                self.last_league = now - LEAGUE_REFRESH_SECS + 300
            finally:
                self.loading = False
        if self.raw and now - self.last_extras >= EXTRAS_REFRESH_SECS and not (
                self.extras_task and not self.extras_task.done()):
            self.extras_task = asyncio.create_task(self.refresh_extras())
        if self.live and now - self.last_rumors >= RUMOR_REFRESH_SECS:
            self.last_rumors = now
            try:
                items = await fetch_items(self.session)
                new = self.feed.ingest(items)
                if new:
                    await self.store.put_cache("rumors", L.pack(self.feed.dump()))
                    log.info("Rumor feed: %d new items (%d tracked players)", new, len(self.feed.heat["players"]))
            except Exception:
                log.exception("Rumor refresh failed")

    @refresh.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()

    # --- saves ---
    def lock(self, user_id: int) -> asyncio.Lock:
        return self.locks[user_id]

    async def get_save(self, user_id: int) -> dict | None:
        save = self.saves.get(user_id)
        if save is None:
            blob = await self.store.get_save(user_id)
            if not blob:
                return None
            save = L.unpack(blob)
            self._remember(save)
        if save.get("live") and save.get("heat_at") != self.feed.heat.get("updated"):
            save["heat"] = self.feed.heat
            save["heat_at"] = self.feed.heat.get("updated")
            ai.refresh_modes(save)
        elif "modes" not in save:
            ai.refresh_modes(save)
        return save

    def _remember(self, save: dict) -> None:
        """Keep recently used saves in memory (bounded, so a free 512 MB instance is plenty)."""
        self.saves.pop(save["user"], None)
        self.saves[save["user"]] = save
        while len(self.saves) > MAX_CACHED_SAVES:
            self.saves.pop(next(iter(self.saves)))

    async def persist(self, save: dict, interaction: discord.Interaction | None = None) -> None:
        self._remember(save)
        gid = interaction.guild_id if interaction else None
        blob = await asyncio.to_thread(L.pack, save)
        await self.store.put_save(save["user"], gid, save["team"], save["season"], save.get("titles", 0),
                                  E.summary(save), blob)

    async def need_save(self, interaction: discord.Interaction) -> dict | None:
        save = await self.get_save(interaction.user.id)
        if not save:
            await interaction.response.send_message("You don't run a team yet. Pick one with `/rebuild start`.",
                                                    ephemeral=True)
        return save

    async def player_choices(self, interaction: discord.Interaction, current: str, mine_only: bool = False):
        save = await self.get_save(interaction.user.id)
        pool = (save or self.live or {}).get("players") or {}
        cur = fold(current)
        out = []
        for p in sorted(pool.values(), key=lambda p: -p["ovr"]):
            if not p.get("team"):
                continue
            if mine_only and save and p.get("team") != save["team"]:
                continue
            if cur and cur not in fold(p["name"]):
                continue
            out.append(app_commands.Choice(name=f"{p['name']} · {p['team']} · {p['ovr']} OVR"[:100], value=p["id"]))
            if len(out) >= 25:
                break
        return out

    # --- commands ---
    @rebuild.command(name="start", description="Take over any NBA team (live rosters, contracts and rumors)")
    @app_commands.describe(team="The team you want to run")
    @app_commands.autocomplete(team=team_autocomplete)
    async def start(self, interaction: discord.Interaction, team: str) -> None:
        abbr = cba.find_team(team)
        if not abbr:
            return await interaction.response.send_message("I don't know that team. Pick one from the list.", ephemeral=True)
        if not self.live:
            return await interaction.response.send_message(
                "Still loading today's NBA rosters from ESPN. Give it a minute and try again.", ephemeral=True)

        async def create(inter: discord.Interaction) -> None:
            from_button = inter.type == discord.InteractionType.component
            if from_button:
                await inter.response.edit_message(content=f"Starting fresh with the {cba.TEAMS[abbr].full}...", view=None)
            async with self.lock(inter.user.id):
                save = L.new_save(self.live, abbr, inter.user.id, self.feed.heat)
                save["heat_at"] = self.feed.heat.get("updated")
                ai.refresh_modes(save)
                await self.persist(save, inter)
            rum = []
            for p in L.roster(save, abbr):
                h = ai.heat_of(save, p["id"])
                if h.get("label") and h.get("label") != "Injury":
                    rum.append(f"{TIER_BADGE.get(h['tier'], '').split(' ')[0]} **{p['name']}**: {h['label']} "
                               f"({h.get('reporter') or 'report'})")
            e = E.welcome_embed(save, rum[:5])
            if from_button:
                await inter.followup.send(embed=e)
            else:
                await inter.response.send_message(embed=e)

        existing = await self.get_save(interaction.user.id)
        if existing:
            v = ConfirmView(self, interaction.user.id, create)
            await interaction.response.send_message(
                f"You already run the **{cba.TEAMS[existing['team']].full}** ({cba.season_label(existing['season'])}). "
                f"Start over with the **{cba.TEAMS[abbr].full}**? This replaces your save.", view=v, ephemeral=True)
            v.message = await interaction.original_response()
            return
        await create(interaction)

    @rebuild.command(name="team", description="Roster, contracts, cap sheet and picks for any team in your league")
    @app_commands.describe(team="Leave empty for your own team")
    @app_commands.autocomplete(team=team_autocomplete)
    async def team(self, interaction: discord.Interaction, team: str | None = None) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        abbr = cba.find_team(team) if team else save["team"]
        if not abbr:
            return await interaction.response.send_message("I don't know that team.", ephemeral=True)
        await interaction.response.send_message(embed=E.team_embed(save, abbr))

    @rebuild.command(name="player", description="Player card: ratings, contract, team stance and live rumors")
    async def player(self, interaction: discord.Interaction, name: str) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        p = save["players"].get(name) or self._find_player(save, name)
        if not p:
            return await interaction.response.send_message("Couldn't find that player. Pick one from the list.", ephemeral=True)
        await interaction.response.send_message(embed=E.player_embed(save, p))

    @player.autocomplete("name")
    async def _player_ac(self, interaction, current):
        return await self.player_choices(interaction, current)

    def _find_player(self, save: dict, text: str) -> dict | None:
        f = fold(text)
        for p in save["players"].values():
            if fold(p["name"]) == f:
                return p
        return None

    @rebuild.command(name="trade", description="Build a trade with any team (live CBA check + trade finder)")
    @app_commands.describe(team="Team to trade with", target="Optional: a player of theirs you want")
    @app_commands.autocomplete(team=team_autocomplete)
    async def trade(self, interaction: discord.Interaction, team: str, target: str | None = None) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        abbr = cba.find_team(team)
        if not abbr or abbr == save["team"]:
            return await interaction.response.send_message("Pick another team to trade with.", ephemeral=True)
        if save["phase"] == "draft":
            return await interaction.response.send_message("Trades open back up after the draft. Finish it with `/rebuild sim`.",
                                                           ephemeral=True)
        if save["phase"] == "stretch":
            return await interaction.response.send_message(
                "The trade deadline has passed. Trades reopen after the playoffs. You can still sign free agents "
                "(`/rebuild freeagents`).", ephemeral=True)
        t = ai.Trade(save["team"], abbr)
        if target and target in save["players"] and save["players"][target].get("team") == abbr:
            t.b_out = [f"p:{target}"]
        v = TradeView(self, interaction.user.id, t)
        v.build(save)
        await interaction.response.send_message(embed=E.trade_embed(save, t), view=v)
        v.message = await interaction.original_response()

    @trade.autocomplete("target")
    async def _target_ac(self, interaction, current):
        save = await self.get_save(interaction.user.id)
        team = cba.find_team(getattr(interaction.namespace, "team", "") or "")
        if not save or not team:
            return []
        cur = fold(current)
        return [app_commands.Choice(name=f"{p['name']} · {p['ovr']} OVR"[:100], value=p["id"])
                for p in L.roster(save, team) if not cur or cur in fold(p["name"])][:25]

    @rebuild.command(name="shop", description="Shop one of your players: see the best offers from around the league")
    async def shop(self, interaction: discord.Interaction, player: str) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        p = save["players"].get(player) or self._find_player(save, player)
        if not p or p.get("team") != save["team"]:
            return await interaction.response.send_message("Pick one of your own players.", ephemeral=True)
        if save["phase"] == "draft":
            return await interaction.response.send_message("Finish the draft first.", ephemeral=True)
        await interaction.response.defer(thinking=True)
        offers = await asyncio.to_thread(ai.shop_player, save, p["id"], 3)
        if not offers:
            return await interaction.followup.send(
                f"Nobody is offering real value for **{p['name']}** right now (contract, age or fit).")
        dicts = [t.to_dict() for t, _ in offers]
        e = offers_embed(save, dicts, f"📞 Offers for {p['name']}",
                         f"You put **{p['name']}** ({p['ovr']} OVR, {E.contract_text(p)}) on the block. "
                         f"These are the best packages teams would actually give up:")
        v = OffersView(self, interaction.user.id, dicts, "shop")
        msg = await interaction.followup.send(embed=e, view=v, wait=True)
        v.message = msg

    @shop.autocomplete("player")
    async def _shop_ac(self, interaction, current):
        return await self.player_choices(interaction, current, mine_only=True)

    @rebuild.command(name="inbox", description="Trade offers other teams sent you at the deadline")
    async def inbox(self, interaction: discord.Interaction) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        offers = [o for o in save.get("inbox", []) if not ai.validate(save, ai.Trade.from_dict(o))]
        if not offers:
            return await interaction.response.send_message("No open offers. Teams call around the trade deadline.", ephemeral=True)
        v = OffersView(self, interaction.user.id, offers, "inbox")
        await interaction.response.send_message(embed=offers_embed(save, offers, "📞 Incoming trade offers",
                                                                  "Teams called about your players:"), view=v)
        v.message = await interaction.original_response()

    @rebuild.command(name="market", description="Who's available: players teams are shopping (live rumors + team direction)")
    async def market(self, interaction: discord.Interaction) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        rows = []
        for p in save["players"].values():
            team = p.get("team")
            if not team or team == save["team"] or p["ovr"] < 72 or p.get("twoway"):
                continue
            rel = ai.reluctance(save, team, p)
            h = ai.heat_of(save, p["id"]).get("h", 0)
            if rel <= 0.02 or h >= 0.25:
                rows.append((p, rel, h))
        rows.sort(key=lambda x: (-x[0]["ovr"], x[1]))
        e = discord.Embed(title="🏪 Trade market", color=E.ACCENT)
        lines = []
        for p, rel, h in rows[:18]:
            why = ai.heat_of(save, p["id"]).get("label") or ("rebuilding team" if ai.mode_of(save, p["team"]) == "rebuild" else "team open to it")
            lines.append(f"`{p['ovr']}` **{p['name']}** {p['team']} · {p['age']} · {E.contract_text(p)}{E.badges(save, p)} · _{why}_")
        e.description = "\n".join(lines) or "Nobody is being actively shopped right now."
        e.set_footer(text="Based on each team's direction" + (" and the live rumor feed" if save.get("live") else ""))
        await interaction.response.send_message(embed=e)

    @rebuild.command(name="rumors", description="The live NBA rumor feed and how it's moving the market")
    @app_commands.describe(team="Only rumors about this team")
    @app_commands.autocomplete(team=team_autocomplete)
    async def rumors(self, interaction: discord.Interaction, team: str | None = None) -> None:
        abbr = cba.find_team(team) if team else None
        items = self.feed.recent(40)
        players = (self.live or {}).get("players", {})
        if abbr:
            items = [i for i in items if abbr in i["teams"] or any(players.get(p, {}).get("team") == abbr for p in i["players"])]
        e = discord.Embed(title="📡 Live rumor feed" + (f" · {cba.TEAMS[abbr].name}" if abbr else ""), color=E.ACCENT)
        for it in items[:8]:
            effects = []
            for pid, (lbl, d, _) in list(it["players"].items())[:3]:
                name = players.get(pid, {}).get("name", "?")
                arrow = "↑ more available" if d > 0 else "↓ less available" if d < 0 else "injury note"
                effects.append(f"{name}: {lbl} ({arrow})")
            for t, (lbl, _) in list(it["teams"].items())[:2]:
                effects.append(f"{cba.TEAMS[t].name}: {lbl}")
            src = it.get("reporter") or it.get("feed")
            val = (f"{TIER_BADGE[it['tier']]} · {src} · <t:{int(it['ts'])}:R>\n" + "\n".join(f"→ {x}" for x in effects)
                   + (f"\n[Read]({it['url']})" if it.get("url") else ""))
            e.add_field(name=it["title"][:250] or "Report", value=val[:1024], inline=False)
        if not items:
            e.description = "No market-moving reports right now." if self.feed.items else "The rumor feed is still loading."
        e.set_footer(text="Sources: RealGM Wiretap, ESPN, r/nba insider posts · updates every 10 minutes")
        await interaction.response.send_message(embed=e)

    @rebuild.command(name="sim", description="Play your season and run the offseason (draft, re-signing, free agency)")
    async def sim_cmd(self, interaction: discord.Interaction) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        v = SimView(self, interaction.user.id, save)
        ph = save["phase"]
        if ph == "fa":
            e = fa_embed(save, "Sign free agents, then finish the offseason.")
        elif ph == "resign":
            e = discord.Embed(title="✍️ Re-signing period", color=E.color(save["team"]))
            e.add_field(name="Your free agents", value=_asks_text(save))
        elif ph == "draft":
            e = discord.Embed(title=f"🎟️ {save['season']} NBA Draft", color=E.color(save["team"]))
            e.add_field(name="Latest picks", value=_draft_log(save), inline=False)
        elif ph == "postseason":
            e = E.season_embed(save, save["history"][-1])
        else:
            e = E.standings_embed(save)
            e.title = f"{cba.TEAMS[save['team']].full} · {L.PHASES[ph]}"
        await interaction.response.send_message(embed=e, view=v)
        v.message = await interaction.original_response()

    @rebuild.command(name="freeagents", description="Free agents you can sign right now, and make them offers")
    async def freeagents(self, interaction: discord.Interaction) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        if save["phase"] == "draft":
            return await interaction.response.send_message("Finish the draft first (`/rebuild sim`).", ephemeral=True)
        if save["phase"] == "resign":
            return await interaction.response.send_message(
                "Re-sign your own players first, then open free agency from `/rebuild sim`.", ephemeral=True)
        intro = ("Pick a player to make an offer (salary and years). In the offseason, stars weigh offers and decide "
                 "at the end of each day." if save["phase"] == "fa" else
                 "These players are unsigned. Make an offer and they'll answer right away.")
        v = FAView(self, interaction.user.id, save)
        await interaction.response.send_message(embed=fa_embed(save, intro), view=v)
        v.message = await interaction.original_response()

    @rebuild.command(name="release", description="Waive one of your players (his remaining money stays on your cap)")
    async def release(self, interaction: discord.Interaction, player: str) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        p = save["players"].get(player) or self._find_player(save, player)
        if not p or p.get("team") != save["team"]:
            return await interaction.response.send_message("Pick one of your own players.", ephemeral=True)
        dead = sum(p.get("salary") or []) if not p.get("twoway") else 0

        async def do(inter: discord.Interaction) -> None:
            async with self.lock(inter.user.id):
                s2 = await self.get_save(inter.user.id)
                msg = fa.release(s2, p["id"])
                await self.persist(s2, inter)
            await inter.response.edit_message(content=msg, view=None)

        v = ConfirmView(self, interaction.user.id, do, yes_label=f"Release {p['name']}")
        await interaction.response.send_message(
            f"Release **{p['name']}**? {cba.money(dead)} would stay on your books as dead money." if dead
            else f"Release **{p['name']}**?", view=v, ephemeral=True)

    @release.autocomplete("player")
    async def _release_ac(self, interaction, current):
        return await self.player_choices(interaction, current, mine_only=True)

    @rebuild.command(name="lineup", description="Set your starters and minutes, and load management")
    async def lineup(self, interaction: discord.Interaction) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        v = LineupView(self, interaction.user.id, save)
        await interaction.response.send_message(embed=lineup_embed(save), view=v)
        v.message = await interaction.original_response()

    @rebuild.command(name="coach", description="Your head coach: fire him or hire a real available coach")
    async def coach(self, interaction: discord.Interaction) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        v = CoachView(self, interaction.user.id, save)
        await interaction.response.send_message(embed=coach_embed(save), view=v)
        v.message = await interaction.original_response()

    @rebuild.command(name="injuries", description="Injury report: who's hurt now and the season's injuries")
    @app_commands.describe(team="Only this team (leave empty for the whole league)")
    @app_commands.autocomplete(team=team_autocomplete)
    async def injuries(self, interaction: discord.Interaction, team: str | None = None) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        abbr = cba.find_team(team) if team else None
        hurt = [p for p in save["players"].values() if p.get("team") and (p.get("out", 0) > 0 or p.get("out_carry"))
                and (not abbr or p["team"] == abbr)]
        hurt.sort(key=lambda p: -p["ovr"])
        e = discord.Embed(title="🚑 Injury report" + (f" · {cba.TEAMS[abbr].name}" if abbr else ""), color=E.ACCENT)
        e.description = "\n".join(
            f"`{p['ovr']}` **{p['name']}** {p['team']} · {p.get('inj') or 'Injured'}"
            + (" · ⚠️ career-altering" if p.get("altered") else "") for p in hurt[:20]) or "Nobody is hurt right now."
        log_rows = [r for r in save.get("inj_log", []) if not abbr or r["team"] == abbr][:10]
        if log_rows:
            e.add_field(name="This season", value="\n".join(
                f"{r['date']} · {r['name']} ({r['team']}): {r['injury']}, {r['games']} games"
                + (" · lost a step for good" if r.get("altered") else "") for r in log_rows)[:1024], inline=False)
        e.set_footer(text="Injury-prone players get hurt more; iron men rarely miss games. Old and fragile stars rest back-to-backs.")
        await interaction.response.send_message(embed=e)

    @rebuild.command(name="stats", description="League leaders, or a team's season stats")
    @app_commands.describe(team="Show this team's stats (leave empty for league leaders)")
    @app_commands.autocomplete(team=team_autocomplete)
    async def stats_cmd(self, interaction: discord.Interaction, team: str | None = None) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        abbr = cba.find_team(team) if team else None
        label = cba.season_label(save["season"])
        if abbr:
            e = discord.Embed(title=f"📊 {cba.TEAMS[abbr].full} · {label}", color=E.color(abbr))
            rows = [p for p in L.roster(save, abbr) if stats_mod.line(p)]
            rows.sort(key=lambda p: -stats_mod.line(p)["pts"])
            e.description = "\n".join(f"**{p['name']}** · {stats_mod.card_line(p)}" for p in rows)[:4000] or \
                "No games played yet this season."
        else:
            e = discord.Embed(title=f"📊 League leaders · {label}", color=E.ACCENT)
            for key, name in (("pts", "Points"), ("reb", "Rebounds"), ("ast", "Assists"), ("stl", "Steals"),
                              ("blk", "Blocks"), ("tpm", "3-pointers made")):
                rows = stats_mod.leaders(save, key)
                if rows:
                    e.add_field(name=name, value="\n".join(f"{p['name']} ({p['team']}) **{v}**" for p, v in rows),
                                inline=True)
            if not e.fields:
                e.description = "No games played yet this season. Sim some games with `/rebuild sim`."
            aw = save.get("awards")
            if aw:
                e.add_field(name="🏅 Awards", value="\n".join(f"**{k}:** {v}" for k, v in aw.items())[:1024], inline=False)
        await interaction.response.send_message(embed=e)

    @rebuild.command(name="extend", description="Extend players in the last year of their contracts")
    async def extend(self, interaction: discord.Interaction) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        if save["phase"] in ("draft", "resign", "fa"):
            return await interaction.response.send_message(
                "Extensions open again once next season's contracts kick in (after free agency).", ephemeral=True)
        v = ExtendView(self, interaction.user.id, save)
        await interaction.response.send_message(embed=v.embed(save), view=v)
        v.message = await interaction.original_response()

    @rebuild.command(name="draftboard", description="Scouting board for this season's draft class")
    async def draftboard(self, interaction: discord.Interaction) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        at_draft = save["phase"] == "draft"
        prospects = (save.get("draft") or {}).get("prospects") if at_draft else draft_mod.ensure_class(save, random.Random())
        e = discord.Embed(title=f"🎟️ {save['season']} NBA Draft big board", color=E.color(save["team"]))
        lines = []
        for p in (prospects or [])[:15]:
            lines.append(f"`{p['big_board']:>2}` **{p['name']}** {p['pos']} · {p.get('school', '')} · {p['age']}\n"
                         f"  {draft_mod.projection(save, p, precise=at_draft)}")
        e.description = "\n".join(lines)[:4000] or "No prospects."
        mine = draft_mod.my_projected_picks(save)
        e.add_field(name="Your picks this year", value="\n".join(mine) or "You don't own a pick in this draft.", inline=False)
        e.set_footer(text=("Draft-day scouting: tighter ranges. " if at_draft else
                           "Early read only: full workouts and interviews happen before the draft. ")
                          + "Not every prospect hits; some top picks bust and some late picks become stars.")
        await interaction.response.send_message(embed=e)

    @rebuild.command(name="teamstats", description="What a team is averaging this season, with league ranks")
    @app_commands.describe(team="Leave empty for your own team")
    @app_commands.autocomplete(team=team_autocomplete)
    async def teamstats(self, interaction: discord.Interaction, team: str | None = None) -> None:
        save = await self.need_save(interaction)
        if not save:
            return
        abbr = cba.find_team(team) if team else save["team"]
        if not abbr:
            return await interaction.response.send_message("I don't know that team.", ephemeral=True)
        t = stats_mod.team_line(save, abbr)
        e = discord.Embed(title=f"📊 {cba.TEAMS[abbr].full} · {cba.season_label(save['season'])}", color=E.color(abbr))
        if not t:
            e.description = "No games played yet this season. Sim some games with `/rebuild sim`."
            return await interaction.response.send_message(embed=e)
        w, l = L.record(save, abbr)
        rk = lambda key, rev=True: stats_mod.team_ranks(save, key, rev).get(abbr, "?")
        ordinal = lambda n: f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}" \
            if isinstance(n, int) else "?"
        e.description = (f"**{w}-{l}** · {t['g']} games · Net rating **{t['net']:+.1f}** ({ordinal(rk('net'))})\n"
                         f"{style.identity(save, abbr)}")
        e.add_field(name="Offense", value=(f"{t['pts']} PPG ({ordinal(rk('pts'))})\n{t['ast']} APG ({ordinal(rk('ast'))})\n"
                                           f"{t['tpm']} 3PM ({ordinal(rk('tpm'))})"), inline=True)
        e.add_field(name="Defense", value=(f"{t['opp']} opp PPG ({ordinal(rk('opp', False))})\n{t['stl']} SPG "
                                           f"({ordinal(rk('stl'))})\n{t['blk']} BPG ({ordinal(rk('blk'))})"), inline=True)
        e.add_field(name="Boards", value=f"{t['reb']} RPG ({ordinal(rk('reb'))})", inline=True)
        top = sorted((p for p in L.roster(save, abbr) if stats_mod.line(p)), key=lambda p: -stats_mod.line(p)["pts"])[:5]
        if top:
            e.add_field(name="Leading scorers", value="\n".join(
                f"**{p['name']}** {stats_mod.line(p)['pts']} PTS · {stats_mod.line(p)['reb']} REB · {stats_mod.line(p)['ast']} AST"
                for p in top), inline=False)
        await interaction.response.send_message(embed=e)

    @rebuild.command(name="standings", description="Conference standings (or power rankings before tip-off)")
    async def standings(self, interaction: discord.Interaction) -> None:
        save = await self.need_save(interaction)
        if save:
            await interaction.response.send_message(embed=E.standings_embed(save))

    @rebuild.command(name="history", description="Your seasons, titles and trades")
    async def history(self, interaction: discord.Interaction) -> None:
        save = await self.need_save(interaction)
        if save:
            await interaction.response.send_message(embed=E.history_embed(save))

    @rebuild.command(name="leaderboard", description="Every GM in this server")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        rows = await self.store.leaderboard(interaction.guild_id)
        e = discord.Embed(title="🏆 Rebuild leaderboard", color=E.ACCENT)
        e.description = "\n".join(
            f"`{i}.` <@{uid}> · **{team}** · {cba.season_label(season)} · 🏆×{titles} · {summ or ''}"
            for i, (uid, team, season, titles, summ) in enumerate(rows, 1)) or "No GMs yet. Be the first with `/rebuild start`."
        await interaction.response.send_message(embed=e, allowed_mentions=NO_PINGS)

    @rebuild.command(name="help", description="How the Rebuild simulator works")
    async def help(self, interaction: discord.Interaction) -> None:
        e = discord.Embed(title="🏀 Rebuild: NBA front office simulator", description=E.HELP, color=E.ACCENT)
        if self.live:
            e.set_footer(text=f"Live rosters loaded: {len(self.live['players'])} players · rumors tracked: {len(self.feed.items)}")
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.exception("Rebuild command failed", exc_info=error)
        msg = "Something went wrong running that. Try again in a moment."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass
