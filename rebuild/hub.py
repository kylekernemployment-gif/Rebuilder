"""The /rebuild hub: one message with a menu, so the whole game runs on buttons.

Discord can't draw a real sidebar, so the hub keeps its navigation pinned to the top two rows of
every screen (main buttons + a "More" menu) and swaps the page underneath in place.
"""

from __future__ import annotations

import asyncio
import random

import discord

from . import ai, cba, chem, coaches, fa, sim, style
from . import draft as draft_mod
from . import embeds as E
from . import league as L
from . import stats as stats_mod
from .cog import (CoachView, ExtendView, FAView, LineupView, OffersView, OwnerView, SimView, TradeView,
                  _asks_text, _draft_log, coach_embed, fa_embed, lineup_embed, offers_embed)
from .rumors import TIER_BADGE, fold

MAIN = [("home", "Home", "🏠"), ("roster", "Roster", "📋"), ("trade", "Trade", "🔁"), ("fa", "Free agents", "🛒"),
        ("lineup", "Lineup", "🧩")]
MORE = [
    ("player", "Find a player", "🔎", "Player cards: ratings, contract, rumors"),
    ("shop", "Shop a player", "📞", "Get offers for one of your players"),
    ("inbox", "Offers inbox", "📨", "Offers teams sent you"),
    ("extend", "Extensions", "📝", "Extend players in a contract year"),
    ("release", "Release a player", "✂️", "Waive someone (dead money stays)"),
    ("coach", "Coach", "🧑‍💼", "Fire or hire a head coach"),
    ("standings", "Standings", "🏆", "Conference standings"),
    ("stats", "League leaders", "📊", "Player stats and awards"),
    ("teamstats", "Team stats", "📈", "Team averages and ranks"),
    ("injuries", "Injury report", "🚑", "Who's hurt"),
    ("draftboard", "Draft board", "🎟️", "This year's prospects"),
    ("market", "Trade market", "🏪", "Who's available"),
    ("rumors", "Rumor feed", "📡", "Live NBA rumors"),
    ("history", "History", "📜", "Your seasons and titles"),
    ("leaderboard", "Server leaderboard", "🥇", "Every GM here"),
    ("newteam", "Switch teams", "🔄", "Start over with another team"),
    ("help", "How it works", "❓", "What's realistic and how to play"),
]


# ------------------------------------------------------------------------------------------
# Small pages
# ------------------------------------------------------------------------------------------

def team_selects(page: OwnerView, on_pick, rows=(0, 1), exclude: str | None = None, placeholder: str = "") -> None:
    for i, conf in enumerate(("East", "West")):
        opts = [discord.SelectOption(label=t.full, value=a) for a, t in cba.TEAMS.items()
                if t.conf == conf and a != exclude]
        sel = discord.ui.Select(placeholder=f"{placeholder or 'Pick a team'} ({conf})", options=opts, row=rows[i])

        async def cb(interaction: discord.Interaction, sel=sel):
            await on_pick(interaction, interaction.data["values"][0])
        sel.callback = cb
        page.add_item(sel)


class Page(OwnerView):
    """A blank page that hub screens fill with their own controls."""


def market_embed(save: dict) -> discord.Embed:
    rows = []
    for p in save["players"].values():
        team = p.get("team")
        if not team or team == save["team"] or p["ovr"] < 72 or p.get("twoway"):
            continue
        rel = ai.reluctance(save, team, p)
        h = ai.heat_of(save, p["id"]).get("h", 0)
        if rel <= 0.02 or h >= 0.25:
            rows.append((p, rel))
    rows.sort(key=lambda x: (-x[0]["ovr"], x[1]))
    e = discord.Embed(title="🏪 Trade market", color=E.ACCENT)
    lines = []
    for p, _ in rows[:18]:
        why = ai.heat_of(save, p["id"]).get("label") or (
            "rebuilding team" if ai.mode_of(save, p["team"]) == "rebuild" else "team open to it")
        lines.append(f"`{p['ovr']}` **{p['name']}** {p['team']} · {p['age']} · {E.contract_text(p)}{E.badges(save, p)} · _{why}_")
    e.description = "\n".join(lines) or "Nobody is being actively shopped right now."
    return e


def rumors_embed(cog, save: dict) -> discord.Embed:
    items = cog.feed.recent(40)
    players = (cog.live or {}).get("players", {})
    e = discord.Embed(title="📡 Live rumor feed", color=E.ACCENT)
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
        e.description = "No market-moving reports right now." if cog.feed.items else "The rumor feed is still loading."
    foot = "RealGM, ESPN and r/nba insiders · updates every 10 minutes"
    if not save.get("live"):
        foot += " · your league has left the real timeline, so these no longer move your AI teams"
    e.set_footer(text=foot)
    return e


def injuries_embed(save: dict, abbr: str | None) -> discord.Embed:
    hurt = [p for p in save["players"].values() if p.get("team") and (p.get("out", 0) > 0 or p.get("out_carry"))
            and (not abbr or p["team"] == abbr)]
    hurt.sort(key=lambda p: -p["ovr"])
    e = discord.Embed(title="🚑 Injury report" + (f" · {cba.TEAMS[abbr].name}" if abbr else ""), color=E.ACCENT)
    e.description = "\n".join(f"`{p['ovr']}` **{p['name']}** {p['team']} · {p.get('inj') or 'Injured'}"
                              + (" · ⚠️ career-altering" if p.get("altered") else "") for p in hurt[:20]) \
        or "Nobody is hurt right now."
    log_rows = [r for r in save.get("inj_log", []) if not abbr or r["team"] == abbr][:10]
    if log_rows:
        e.add_field(name="This season", value="\n".join(
            f"{r['date']} · {r['name']} ({r['team']}): {r['injury']}, {r['games']} games"
            + (" · lost a step for good" if r.get("altered") else "") for r in log_rows)[:1024], inline=False)
    return e


def leaders_embed(save: dict) -> discord.Embed:
    e = discord.Embed(title=f"📊 League leaders · {cba.season_label(save['season'])}", color=E.ACCENT)
    for key, name in (("pts", "Points"), ("reb", "Rebounds"), ("ast", "Assists"), ("stl", "Steals"),
                      ("blk", "Blocks"), ("tpm", "3-pointers made")):
        rows = stats_mod.leaders(save, key)
        if rows:
            e.add_field(name=name, value="\n".join(f"{p['name']} ({p['team']}) **{v}**" for p, v in rows), inline=True)
    if not e.fields:
        e.description = "No games played yet this season."
    aw = save.get("awards")
    if aw:
        e.add_field(name="🏅 Awards", value="\n".join(f"**{k}:** {v}" for k, v in aw.items())[:1024], inline=False)
    return e


def ordinal(n) -> str:
    if not isinstance(n, int):
        return "?"
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def teamstats_embed(save: dict, abbr: str) -> discord.Embed:
    t = stats_mod.team_line(save, abbr)
    e = discord.Embed(title=f"📈 {cba.TEAMS[abbr].full} · {cba.season_label(save['season'])}", color=E.color(abbr))
    if not t:
        e.description = f"No games played yet.\n{style.identity(save, abbr)}"
        return e
    w, l = L.record(save, abbr)
    rk = lambda key, rev=True: stats_mod.team_ranks(save, key, rev).get(abbr)
    e.description = (f"**{w}-{l}** · Net rating **{t['net']:+.1f}** ({ordinal(rk('net'))})\n{style.identity(save, abbr)}")
    e.add_field(name="Offense", value=f"{t['pts']} PPG ({ordinal(rk('pts'))})\n{t['ast']} APG ({ordinal(rk('ast'))})\n"
                                      f"{t['tpm']} 3PM ({ordinal(rk('tpm'))})", inline=True)
    e.add_field(name="Defense", value=f"{t['opp']} opp PPG ({ordinal(rk('opp', False))})\n{t['stl']} SPG "
                                      f"({ordinal(rk('stl'))})\n{t['blk']} BPG ({ordinal(rk('blk'))})", inline=True)
    e.add_field(name="Boards", value=f"{t['reb']} RPG ({ordinal(rk('reb'))})", inline=True)
    top = sorted((p for p in L.roster(save, abbr) if stats_mod.line(p)), key=lambda p: -stats_mod.line(p)["pts"])
    if top:
        e.add_field(name="Player stats", value="\n".join(f"**{p['name']}** · {stats_mod.card_line(p)}"
                                                         for p in top[:10])[:1024], inline=False)
    return e


def draftboard_embed(save: dict) -> discord.Embed:
    at_draft = save["phase"] == "draft"
    prospects = (save.get("draft") or {}).get("prospects") if at_draft else draft_mod.ensure_class(save, random.Random())
    e = discord.Embed(title=f"🎟️ {save['season']} NBA Draft big board", color=E.color(save["team"]))
    lines = [f"`{p['big_board']:>2}` **{p['name']}** {p['pos']} · {p.get('school', '')} · {p['age']}\n"
             f"  {draft_mod.projection(save, p, precise=at_draft)}" for p in (prospects or [])[:14]]
    e.description = "\n".join(lines)[:4000] or "No prospects."
    e.add_field(name="Your picks this year", value="\n".join(draft_mod.my_projected_picks(save)) or
                "You don't own a pick in this draft.", inline=False)
    e.set_footer(text=("Draft-day scouting: tighter ranges. " if at_draft else "Early read only. ")
                      + "Not every prospect hits; some top picks bust and some late picks become stars.")
    return e


def home_embed(save: dict) -> discord.Embed:
    me = save["team"]
    ph = save["phase"]
    if ph == "fa":
        return fa_embed(save, "Make offers from **Free agents**, advance day by day, then finish the offseason.")
    if ph == "resign":
        e = discord.Embed(title="✍️ Re-signing period", color=E.color(me))
        e.add_field(name="Your free agents", value=_asks_text(save))
        return e
    if ph == "draft":
        e = discord.Embed(title=f"🎟️ {save['season']} NBA Draft", color=E.color(me))
        e.add_field(name="Latest picks", value=_draft_log(save), inline=False)
        return e
    if ph == "postseason":
        return E.season_embed(save, save["history"][-1])
    w, l = L.record(save, me)
    c = coaches.coach(save, me)
    e = discord.Embed(title=f"🏠 {cba.TEAMS[me].full} · {sim.calendar(save)}", color=E.color(me))
    seed = next((f" · #{o.index(me) + 1} in the {conf}" for conf, o in sim.seeds(save).items() if me in o), "") \
        if save.get("standings") else ""
    e.description = (f"**{cba.season_label(save['season'])}** · {L.PHASES[ph]}\n"
                     f"Record **{w}-{l}**{seed} · Power rank #{(save.get('ranks') or {}).get(me, '?')}\n"
                     f"Coach {c['name'] if c else '—'} · Chemistry {chem.label(chem.value(save, me))}\n"
                     f"{L.cap_status(save, me)[1]} {cba.money(L.payroll(save, me))} · {L.cap_status(save, me)[0]}")
    news = [n for n in save.get("news", [])[:6]]
    if news:
        e.add_field(name="Around the league", value="\n".join(news)[:1024], inline=False)
    hurt = [p for p in L.roster(save, me) if p.get("out", 0) > 0]
    unhappy = [p for p in L.roster(save, me) if p.get("wants_out")]
    alerts = [f"🚑 {p['name']}: {p['inj']}" for p in hurt[:4]] + [f"😠 {p['name']} wants a trade" for p in unhappy[:3]]
    if save.get("inbox"):
        alerts.append(f"📨 {len(save['inbox'])} trade offer(s) waiting (More → Offers inbox)")
    if alerts:
        e.add_field(name="Your team", value="\n".join(alerts)[:1024], inline=False)
    e.set_footer(text="Use the buttons below to sim · menu at the top for everything else")
    return e


# ------------------------------------------------------------------------------------------
# The hub
# ------------------------------------------------------------------------------------------

class HubView(OwnerView):
    def __init__(self, cog, user_id: int):
        super().__init__(cog, user_id)
        self.page_name = "home"
        self.view_team: str | None = None
        self.adopt(None)

    def nav(self) -> None:
        for key, label, emoji in MAIN:
            b = discord.ui.Button(label=label, emoji=emoji, row=0,
                                  style=discord.ButtonStyle.primary if key == self.page_name else discord.ButtonStyle.secondary)
            b.callback = self._go(key)
            self.add_item(b)
        sel = discord.ui.Select(placeholder="☰ More: stats, injuries, coach, draft board, rumors...", row=1,
                                options=[discord.SelectOption(label=lab, value=key, emoji=em, description=desc,
                                                              default=key == self.page_name)
                                         for key, lab, em, desc in MORE])

        async def cb(interaction: discord.Interaction):
            await self.show(interaction, interaction.data["values"][0])
        sel.callback = cb
        self.add_item(sel)

    def adopt(self, page: OwnerView | None) -> None:
        """Rebuild the hub: menu on top, then the page's own controls shifted below it."""
        self.clear_items()
        self.nav()
        if page is None:
            return
        page.hub = self
        items = list(page.children)
        for item in items:
            if not hasattr(item, "_hub_row"):
                item._hub_row = item.row if item.row is not None else 0
            item.row = min(4, item._hub_row + 2)
            page.remove_item(item)
            try:
                self.add_item(item)
            except ValueError:
                import logging
                logging.getLogger("rebuild").warning("Hub row full; dropped %r", item)

    def _go(self, key: str):
        async def cb(interaction: discord.Interaction):
            await self.show(interaction, key)
        return cb

    async def respond(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        if interaction.type == discord.InteractionType.application_command and not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=self)
        elif interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def show(self, interaction: discord.Interaction, key: str, **kw) -> None:
        cog = self.cog
        save = await cog.get_save(self.user_id)
        if save is None and key not in ("help", "leaderboard"):
            key = "newteam"
        self.page_name = key
        uid = self.user_id
        page: OwnerView | None = None
        embed: discord.Embed

        if key == "home":
            page = SimView(cog, uid, save)
            embed = home_embed(save)
        elif key == "roster":
            abbr = kw.get("team") or self.view_team or save["team"]
            self.view_team = abbr
            embed = E.team_embed(save, abbr)
            page = Page(cog, uid)
            ros = L.roster(save, abbr)[:25]
            sel = discord.ui.Select(placeholder="Open a player card", row=0, options=[
                discord.SelectOption(label=f"{p['name']} ({p['pos']}, {p['ovr']})"[:100], value=p["id"]) for p in ros])

            async def pcard(inter, sel=sel):
                s2 = await cog.get_save(uid)
                await self.show_player(inter, s2, inter.data["values"][0])
            sel.callback = pcard
            page.add_item(sel)

            async def pick(inter, team):
                await self.show(inter, "roster", team=team)
            team_selects(page, pick, rows=(1, 2), placeholder="View another team")
        elif key == "trade":
            partner = kw.get("team")
            if save["phase"] == "stretch":
                embed = discord.Embed(title="🔁 Trades are locked", color=E.color(save["team"]),
                                      description="The trade deadline has passed. Trades reopen after the playoffs. "
                                                  "You can still sign free agents.")
            elif save["phase"] == "draft":
                embed = discord.Embed(title="🔁 Trades are paused", description="Finish the draft first (Home).")
            elif not partner:
                embed = discord.Embed(title="🔁 Trade", color=E.color(save["team"]),
                                      description="Who do you want to call?")
                page = Page(cog, uid)

                async def pick(inter, team):
                    await self.show(inter, "trade", team=team)
                team_selects(page, pick, exclude=save["team"], placeholder="Trade with")
            else:
                t = ai.Trade(save["team"], partner)
                page = TradeView(cog, uid, t)
                page.build(save)
                embed = E.trade_embed(save, t)
        elif key == "fa":
            if save["phase"] in ("draft", "resign"):
                embed = home_embed(save)
                embed.add_field(name="Free agency", value="Opens after the draft and re-signing period (Home).", inline=False)
            else:
                page = FAView(cog, uid, save)
                embed = fa_embed(save, "Pick a player to make an offer (salary and years)." if save["phase"] != "fa" else
                                 "Make offers, then advance the day. Stars weigh competing offers.")
        elif key == "lineup":
            page = LineupView(cog, uid, save)
            embed = lineup_embed(save)
        elif key == "coach":
            page = CoachView(cog, uid, save)
            embed = coach_embed(save)
        elif key == "extend":
            if save["phase"] in ("draft", "resign", "fa"):
                embed = discord.Embed(title="📝 Extensions", description="Extensions open again once next season's "
                                                                        "contracts kick in (after free agency).")
            else:
                page = ExtendView(cog, uid, save)
                embed = page.embed(save)
        elif key == "inbox":
            offers = [o for o in save.get("inbox", []) if not ai.validate(save, ai.Trade.from_dict(o))]
            if offers:
                page = OffersView(cog, uid, offers, "inbox")
                embed = offers_embed(save, offers, "📨 Incoming trade offers", "Teams called about your players:")
            else:
                embed = discord.Embed(title="📨 Offers inbox", description="No open offers. Teams call around the trade deadline.")
        elif key == "shop":
            embed = discord.Embed(title="📞 Shop a player", color=E.color(save["team"]),
                                  description="Pick one of your players. Every team will tell you its best real offer.")
            page = Page(cog, uid)
            sel = discord.ui.Select(placeholder="Put a player on the block", row=0, options=[
                discord.SelectOption(label=f"{p['name']} ({p['pos']}, {p['ovr']})"[:100], value=p["id"],
                                     description=E.contract_text(p)[:100])
                for p in L.roster(save, save["team"], include_twoway=False)[:25]])

            async def shop_cb(inter, sel=sel):
                await self.shop(inter, inter.data["values"][0])
            sel.callback = shop_cb
            page.add_item(sel)
        elif key == "release":
            embed = discord.Embed(title="✂️ Release a player", color=E.color(save["team"]),
                                  description="Pick a player to waive. His remaining guaranteed money stays on your cap.")
            page = Page(cog, uid)
            sel = discord.ui.Select(placeholder="Waive...", row=0, options=[
                discord.SelectOption(label=f"{p['name']} ({p['pos']}, {p['ovr']})"[:100], value=p["id"],
                                     description=f"Dead money if waived: {cba.money(sum(p.get('salary') or []))}"[:100])
                for p in L.roster(save, save["team"])[:25]])

            async def rel_cb(inter, sel=sel):
                async with cog.lock(uid):
                    s2 = await cog.get_save(uid)
                    msg = fa.release(s2, inter.data["values"][0])
                    await cog.persist(s2, inter)
                await self.show(inter, "roster", note=msg)
            sel.callback = rel_cb
            page.add_item(sel)
        elif key == "player":
            return await interaction.response.send_modal(FindPlayerModal(self))
        elif key == "standings":
            embed = E.standings_embed(save)
        elif key == "stats":
            embed = leaders_embed(save)
        elif key == "teamstats":
            abbr = kw.get("team") or save["team"]
            embed = teamstats_embed(save, abbr)
            page = Page(cog, uid)

            async def pick(inter, team):
                await self.show(inter, "teamstats", team=team)
            team_selects(page, pick, placeholder="Another team")
        elif key == "injuries":
            abbr = kw.get("team")
            embed = injuries_embed(save, abbr)
            page = Page(cog, uid)

            async def pick(inter, team):
                await self.show(inter, "injuries", team=team)
            team_selects(page, pick, placeholder="One team")
        elif key == "draftboard":
            embed = draftboard_embed(save)
        elif key == "market":
            embed = market_embed(save)
        elif key == "rumors":
            embed = rumors_embed(cog, save)
        elif key == "history":
            embed = E.history_embed(save)
        elif key == "leaderboard":
            rows = await cog.store.leaderboard(interaction.guild_id)
            embed = discord.Embed(title="🥇 Rebuild leaderboard", color=E.ACCENT, description="\n".join(
                f"`{i}.` <@{u}> · **{team}** · {cba.season_label(season)} · 🏆×{titles} · {summ or ''}"
                for i, (u, team, season, titles, summ) in enumerate(rows, 1)) or "No GMs yet.")
        elif key == "help":
            embed = discord.Embed(title="❓ How it works", description=E.HELP, color=E.ACCENT)
        elif key == "newteam":
            embed = discord.Embed(title="🏀 Pick your team", color=E.ACCENT, description=(
                "Take over any NBA team with today's real rosters, contracts and 2K27 ratings."
                + (f"\n\n⚠️ This replaces your **{cba.TEAMS[save['team']].full}** save." if save else "")))
            if not cog.live:
                embed.description += "\n\nStill loading today's rosters. Try again in a minute."
            else:
                page = Page(cog, uid)

                async def pick(inter, team):
                    await self.start_team(inter, team)
                team_selects(page, pick, placeholder="Run the")
        else:
            embed = home_embed(save)
        if kw.get("note"):
            embed.add_field(name="Update", value=kw["note"][:1024], inline=False)
        if save:
            embed.set_footer(text=(embed.footer.text + " · " if embed.footer and embed.footer.text else "") + E.footer(save))
        self.adopt(page)
        await self.respond(interaction, embed)

    async def show_player(self, interaction: discord.Interaction, save: dict, pid: str) -> None:
        p = save["players"].get(pid)
        if not p:
            return await interaction.response.send_message("Couldn't find that player.", ephemeral=True)
        self.page_name = "player"
        page = Page(self.cog, self.user_id)
        if p.get("team") and p["team"] != save["team"] and save["phase"] not in ("stretch", "draft"):
            b = discord.ui.Button(label=f"Trade for {p['name']}"[:80], emoji="🔁", style=discord.ButtonStyle.success, row=0)

            async def trade_for(inter):
                s2 = await self.cog.get_save(self.user_id)
                t = ai.Trade(s2["team"], p["team"], [], [f"p:{pid}"])
                tv = TradeView(self.cog, self.user_id, t)
                tv.build(s2)
                self.adopt(tv)
                await self.respond(inter, E.trade_embed(s2, t))
            b.callback = trade_for
            page.add_item(b)
        if p.get("team") == save["team"]:
            b = discord.ui.Button(label="Shop him", emoji="📞", style=discord.ButtonStyle.secondary, row=0)

            async def shop_him(inter):
                await self.shop(inter, pid)
            b.callback = shop_him
            page.add_item(b)
        self.adopt(page)
        await self.respond(interaction, E.player_embed(save, p))

    async def shop(self, interaction: discord.Interaction, pid: str) -> None:
        await interaction.response.defer()
        save = await self.cog.get_save(self.user_id)
        p = save["players"][pid]
        offers = await asyncio.to_thread(ai.shop_player, save, pid, 3)
        if not offers:
            self.adopt(None)
            return await self.respond(interaction, discord.Embed(
                title=f"📞 {p['name']}", description="Nobody is offering real value right now (contract, age or fit)."))
        dicts = [t.to_dict() for t, _ in offers]
        page = OffersView(self.cog, self.user_id, dicts, "shop")
        self.adopt(page)
        await self.respond(interaction, offers_embed(save, dicts, f"📞 Offers for {p['name']}",
                                                     f"**{p['name']}** ({p['ovr']} OVR, {E.contract_text(p)}) is on the block. "
                                                     "Best real offers:"))

    async def start_team(self, interaction: discord.Interaction, abbr: str) -> None:
        cog = self.cog
        async with cog.lock(self.user_id):
            save = L.new_save(cog.live, abbr, self.user_id, cog.feed.heat)
            save["heat_at"] = cog.feed.heat.get("updated")
            ai.refresh_modes(save)
            await cog.persist(save, interaction)
        rum = []
        for p in L.roster(save, abbr):
            h = ai.heat_of(save, p["id"])
            if h.get("label") and h.get("label") != "Injury":
                rum.append(f"**{p['name']}**: {h['label']} ({h.get('reporter') or 'report'})")
        self.page_name = "home"
        page = SimView(cog, self.user_id, save)
        self.adopt(page)
        await self.respond(interaction, E.welcome_embed(save, rum[:5]))


class FindPlayerModal(discord.ui.Modal, title="Find a player"):
    name = discord.ui.TextInput(label="Player name (any team)", placeholder="e.g. Brunson", max_length=40)

    def __init__(self, hub: HubView):
        super().__init__()
        self.hub = hub

    async def on_submit(self, interaction: discord.Interaction) -> None:
        save = await self.hub.cog.get_save(self.hub.user_id)
        q = fold(str(self.name.value))
        hits = sorted((p for p in save["players"].values() if q and q in fold(p["name"])), key=lambda p: -p["ovr"])[:25]
        if not hits:
            return await interaction.response.send_message("No player by that name.", ephemeral=True)
        if len(hits) == 1:
            return await self.hub.show_player(interaction, save, hits[0]["id"])
        page = Page(self.hub.cog, self.hub.user_id)
        sel = discord.ui.Select(placeholder="Which one?", row=0, options=[
            discord.SelectOption(label=f"{p['name']} ({p.get('team') or 'FA'}, {p['ovr']})"[:100], value=p["id"]) for p in hits])

        async def cb(inter, sel=sel):
            s2 = await self.hub.cog.get_save(self.hub.user_id)
            await self.hub.show_player(inter, s2, inter.data["values"][0])
        sel.callback = cb
        page.add_item(sel)
        self.hub.adopt(page)
        await self.hub.respond(interaction, discord.Embed(title="🔎 Players found",
                                                          description=f"{len(hits)} matches for “{self.name.value}”."))
