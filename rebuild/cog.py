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

from . import ai, cba, sim
from . import embeds as E
from . import league as L
from .data import fetch_league
from .rumors import RumorFeed, TIER_BADGE, fetch_items, fold
from .storage import Store

log = logging.getLogger("rebuild")

LEAGUE_REFRESH_SECS = 6 * 3600
RUMOR_REFRESH_SECS = 10 * 60
VIEW_TIMEOUT = 15 * 60
MAX_CACHED_SAVES = 20
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
        if ph == "preseason":
            self._button("Sim to trade deadline", "⏩", discord.ButtonStyle.primary, self.to_deadline)
            self._button("Sim full season + playoffs", "⏭️", discord.ButtonStyle.secondary, self.to_end)
        elif ph == "deadline":
            self._button("Sim rest of season + playoffs", "⏭️", discord.ButtonStyle.primary, self.to_end)
        elif ph == "postseason":
            self._button("Start offseason (lottery + draft)", "🎟️", discord.ButtonStyle.primary, self.offseason)
        elif ph == "draft":
            prospects = (save.get("draft") or {}).get("prospects") or []
            if sim.on_the_clock(save) and sim.on_the_clock(save)["owner"] == save["team"]:
                opts = [discord.SelectOption(
                    label=f"#{p['big_board']} {p['name']} ({p['pos']}, {p['age']})"[:100], value=p["id"],
                    description=f"OVR {p['ovr']} · Scouts' ceiling {p['scout'] - 3}-{p['scout'] + 3}"[:100])
                    for p in prospects[:25]]
                sel = discord.ui.Select(placeholder="You're on the clock: pick a player", options=opts, row=0)
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
                desc = "Won't re-sign: wants a contender" if a["refuses"] else \
                    f"Asking {cba.money(a['amount'])}/yr × {a['years']} (Bird rights)"
                opts.append(discord.SelectOption(label=f"{p['name']} ({p['pos']}, {p['ovr']}, age {p['age']})"[:100],
                                                 value=pid, description=desc[:100]))
            if opts:
                sel = discord.ui.Select(placeholder="Re-sign one of your free agents", options=opts[:25], row=0)
                sel.callback = self._resign
                self.add_item(sel)
            self._button("Open free agency", "🛒", discord.ButtonStyle.primary, self.open_fa)
        elif ph == "fa":
            pool = sorted((save["players"][i] for i in save["fa"] if i in save.get("fa_asks", {})),
                          key=lambda p: -p["ovr"])[:25]
            opts = []
            for p in pool:
                ok, how, amt = sim.fa_options(save, p["id"])
                opts.append(discord.SelectOption(
                    label=f"{p['name']} ({p['pos']}, {p['ovr']}, age {p['age']})"[:100], value=p["id"],
                    description=(f"{'✅' if ok else '❌'} {cba.money(amt)}/yr × {save['fa_asks'][p['id']]['years']}"
                                 f"{' · ' + how if ok else ''}")[:100]))
            if opts:
                sel = discord.ui.Select(placeholder="Sign a free agent", options=opts, row=0)
                sel.callback = self._sign
                self.add_item(sel)
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

    async def to_deadline(self, interaction):
        def embed(save, r):
            e = discord.Embed(title="⏰ Trade deadline", color=E.color(save["team"]))
            w, l = L.record(save, save["team"])
            e.description = f"**{cba.TEAMS[save['team']].name}: {w}-{l}** at the deadline."
            if r["trades"]:
                e.add_field(name="Around the league", value="\n".join(f"🔁 {t}" for t in r["trades"])[:1024], inline=False)
            inj = [n for n in save.get("news", [])[:8] if n.startswith("🚑")][:4]
            if inj:
                e.add_field(name="Injury report", value="\n".join(inj), inline=False)
            if r["inbox"]:
                e.add_field(name="📞 Your phone is ringing", value=f"{r['inbox']} team(s) sent you offers. Check `/rebuild inbox`.",
                            inline=False)
            e.add_field(name="Now", value="Make your deadline moves with `/rebuild trade`, then come back here to finish the season.",
                        inline=False)
            e.set_footer(text=E.footer(save))
            return e
        await self._run(interaction, sim.sim_to_deadline, embed)

    async def to_end(self, interaction):
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
                e.add_field(name="Player development", value="\n".join(r["notes"])[:1024], inline=False)
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
                return f"{save['players'][pid]['name']} wants to play for a contender and won't re-sign."
            return sim.resign(save, pid)

        def embed(save, msg):
            e = discord.Embed(title="✍️ Re-signing period", color=E.color(save["team"]), description=msg)
            e.add_field(name="Your free agents", value=_asks_text(save), inline=False)
            e.add_field(name="Payroll next season", value=f"{cba.money(L.payroll(save, save['team']))} · "
                                                          f"{L.cap_status(save, save['team'])[0]}", inline=False)
            return e
        await self._run(interaction, work, embed)

    async def open_fa(self, interaction):
        def embed(save, _):
            return fa_embed(save, "🛒 Free agency is open. Anyone you didn't re-sign is now on the market.")
        await self._run(interaction, sim.start_free_agency, embed)

    async def _sign(self, interaction):
        pid = interaction.data["values"][0]
        await self._run(interaction, lambda save, rng: sim.sign_fa(save, pid), lambda save, msg: fa_embed(save, msg))

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


def fa_embed(save: dict, msg: str) -> discord.Embed:
    me = save["team"]
    cap = cba.cap_sheet(L.cap_year(save))
    pay = L.payroll(save, me)
    e = discord.Embed(title="🛒 Free agency", color=E.color(me), description=msg)
    e.add_field(name="Your books", value=(
        f"Payroll {cba.money(pay)} · {L.cap_status(save, me)[0]}\n"
        f"Mid-level: {'used' if save.get('used_mle') else (cba.money(cap['mle']) + ' (non-taxpayer)' if pay < cap['apron1'] else cba.money(cap['tp_mle']) + ' (taxpayer)')}\n"
        f"Roster {L.standard_count(save, me)}/{cba.ROSTER_MAX} (cuts to 15 before the season)"), inline=False)
    top = sorted((save["players"][i] for i in save["fa"] if i in save.get("fa_asks", {})), key=lambda p: -p["ovr"])[:10]
    e.add_field(name="Best available", value="\n".join(
        f"`{p['ovr']}` **{p['name']}** {p['pos']} · {p['age']} · asks {cba.money(save['fa_asks'][p['id']]['amount'])}"
        for p in top)[:1024] or "Market is empty.", inline=False)
    e.set_footer(text="Players on losing teams ask for more. Your own players can be re-signed over the cap with Bird rights.")
    return e


class ConfirmView(OwnerView):
    def __init__(self, cog, user_id, on_yes):
        super().__init__(cog, user_id)
        self.on_yes = on_yes

    @discord.ui.button(label="Yes, start over", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _):
        self.stop()
        await self.on_yes(interaction)

    @discord.ui.button(label="Keep my save", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _):
        self.stop()
        await interaction.response.edit_message(content="Kept your current save.", embed=None, view=None)


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

    # --- lifecycle ---
    async def cog_load(self) -> None:
        await self.store.init()
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        cached = await self.store.get_cache("live_league_v2")
        if cached:
            try:
                self.live = L.unpack(cached[0])
                self.last_league = cached[1]
                self.feed.set_players(self.live["players"])
                log.info("Loaded cached league (%d players)", len(self.live["players"]))
            except Exception:
                log.exception("Cached league unreadable")
        cached = await self.store.get_cache("rumors")
        if cached and self.live:
            try:
                self.feed.load(L.unpack(cached[0]))
            except Exception:
                log.exception("Cached rumors unreadable")
        self.refresh.start()

    async def cog_unload(self) -> None:
        self.refresh.cancel()
        if self.session:
            await self.session.close()

    @tasks.loop(minutes=2)
    async def refresh(self) -> None:
        now = time.time()
        if now - self.last_league >= LEAGUE_REFRESH_SECS or self.live is None:
            self.loading = True
            try:
                live = await fetch_league(self.session)
                if live:
                    self.live = live
                    self.feed.set_players(live["players"])
                    await self.store.put_cache("live_league_v2", L.pack(live))
                self.last_league = now if live else now - LEAGUE_REFRESH_SECS + 300
            except Exception:
                log.exception("League refresh failed")
                self.last_league = now - LEAGUE_REFRESH_SECS + 300
            finally:
                self.loading = False
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
