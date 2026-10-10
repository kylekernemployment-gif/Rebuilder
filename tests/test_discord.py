"""Drive the real slash-command callbacks and menus with fake Discord interactions."""

import asyncio
import types

import discord
import pytest

from fixtures import fake_espn
from rebuild import cba
from rebuild import league as L
from rebuild.cog import OffersView, Rebuild, SimView, TradeView


class FakeResponse:
    def __init__(self, inter):
        self.inter = inter
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False, **kw):
        self.done = True
        self.inter.sent.append(("send", content, embed, view))

    async def edit_message(self, *, content=None, embed=None, view=None, **kw):
        self.done = True
        self.inter.sent.append(("edit", content, embed, view))

    async def defer(self, **kw):
        self.done = True


class FakeFollowup:
    def __init__(self, inter):
        self.inter = inter

    async def send(self, content=None, *, embed=None, view=None, wait=False, **kw):
        self.inter.sent.append(("followup", content, embed, view))
        return types.SimpleNamespace(id=1)


class FakeInteraction:
    def __init__(self, user_id=1, component=False, values=None, namespace=None):
        self.user = types.SimpleNamespace(id=user_id)
        self.guild_id = 99
        self.sent = []
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.type = discord.InteractionType.component if component else discord.InteractionType.application_command
        self.message = None
        self.data = {"values": values or []}
        self.namespace = namespace or types.SimpleNamespace()

    async def original_response(self):
        return types.SimpleNamespace(id=1)

    async def edit_original_response(self, *, content=None, embed=None, view=None, **kw):
        self.sent.append(("edit_original", content, embed, view))

    def last_embed(self):
        for kind, _, embed, _ in reversed(self.sent):
            if embed is not None:
                return embed
        return None


def check_embed(e: discord.Embed):
    assert len(e) <= 6000
    assert len(e.description or "") <= 4096
    for f in e.fields:
        assert len(f.value) <= 1024 and len(f.name) <= 256
    assert len(e.fields) <= 25


@pytest.fixture
def cog(tmp_path):
    bot = types.SimpleNamespace()
    c = Rebuild(bot, None, str(tmp_path / "t.db"))
    asyncio.run(c.store.init())
    c.live = fake_espn()
    c.feed.set_players(c.live["players"])
    return c


def run(coro):
    return asyncio.run(coro)


def test_full_flow_through_commands(cog):
    async def flow():
        i = FakeInteraction()
        await Rebuild.start.callback(cog, i, "Knicks")
        check_embed(i.last_embed())
        assert "Knicks" in i.last_embed().title

        i = FakeInteraction()
        await Rebuild.team.callback(cog, i, None)
        check_embed(i.last_embed())
        i = FakeInteraction()
        await Rebuild.team.callback(cog, i, "Celtics")
        check_embed(i.last_embed())

        save = await cog.get_save(1)
        star = L.roster(save, "BOS")[0]
        i = FakeInteraction()
        await Rebuild.player.callback(cog, i, star["id"])
        check_embed(i.last_embed())

        # trade builder: ask for a Celtic, use the trade finder, propose
        i = FakeInteraction()
        target = L.roster(save, "BOS", include_twoway=False)[4]
        await Rebuild.trade.callback(cog, i, "BOS", target["id"])
        view: TradeView = i.sent[-1][3]
        check_embed(i.last_embed())
        assert len(view.children) == 5
        for item in view.children:
            if isinstance(item, discord.ui.Select):
                assert len(item.options) <= 25
        b = FakeInteraction(component=True)
        await view.finder.callback(b)
        check_embed(b.last_embed())
        if view.trade.a_out:
            p = FakeInteraction(component=True)
            await view.propose.callback(p)
            e = p.last_embed()
            check_embed(e)
            assert e.title.startswith("✅")

        # shop a player
        mine = L.roster(await cog.get_save(1), "NYK", include_twoway=False)[1]
        i = FakeInteraction()
        await Rebuild.shop.callback(cog, i, mine["id"])
        check_embed(i.last_embed())
        if i.sent[-1][3] is not None:
            ov: OffersView = i.sent[-1][3]
            a = FakeInteraction(component=True)
            await ov.children[0].callback(a)
            assert a.last_embed().title.startswith("✅")

        for cmd in (Rebuild.market, Rebuild.standings, Rebuild.history, Rebuild.leaderboard, Rebuild.inbox):
            i = FakeInteraction()
            await cmd.callback(cog, i)
            if i.last_embed():
                check_embed(i.last_embed())
        i = FakeInteraction()
        await Rebuild.rumors.callback(cog, i, None)
        check_embed(i.last_embed())

        # season: deadline -> rest -> offseason -> draft -> re-sign -> FA -> next season
        i = FakeInteraction()
        await Rebuild.sim_cmd.callback(cog, i)
        sv: SimView = i.sent[-1][3]

        async def press(label_start, values=None):
            for item in sv.children:
                if isinstance(item, discord.ui.Button) and item.label.startswith(label_start):
                    x = FakeInteraction(component=True)
                    await item.callback(x)
                    check_embed(x.last_embed())
                    return x
            raise AssertionError(f"no button {label_start}: {[getattr(c, 'label', None) for c in sv.children]}")

        async def choose(values):
            sel = next(c for c in sv.children if isinstance(c, discord.ui.Select))
            x = FakeInteraction(component=True, values=values)
            await sel.callback(x)
            check_embed(x.last_embed())
            return x

        await press("Sim 1 week")
        await press("Sim to trade deadline")
        save = await cog.get_save(1)
        assert save["phase"] == "deadline"
        for cmd in (Rebuild.lineup, Rebuild.coach, Rebuild.freeagents, Rebuild.extend, Rebuild.draftboard):
            i = FakeInteraction()
            await cmd.callback(cog, i)
            check_embed(i.last_embed())
        await press("Sim to end of season")
        assert save["phase"] == "stretch" and save.get("awards")
        i = FakeInteraction()
        await Rebuild.trade.callback(cog, i, "BOS", None)
        assert "deadline has passed" in (i.sent[-1][1] or "")
        for cmd in (Rebuild.injuries, Rebuild.stats_cmd):
            i = FakeInteraction()
            await cmd.callback(cog, i, None)
            check_embed(i.last_embed())
        await press("Play the playoffs")
        await press("Start offseason")
        save = await cog.get_save(1)
        while save["phase"] == "draft":
            if any(isinstance(c, discord.ui.Select) for c in sv.children):
                await choose([save["draft"]["prospects"][0]["id"]])
            else:
                await press("Auto-draft the rest")
        assert save["phase"] == "resign"
        if save.get("asks"):
            await choose([next(iter(save["asks"]))])
        await press("Open free agency")
        assert save["phase"] == "fa"
        await press("Next free agency day")
        await press("Finish offseason")
        assert save["phase"] == "preseason" and save["season"] == 2028
        i = FakeInteraction()
        await Rebuild.history.callback(cog, i)
        assert "2026-27" in i.last_embed().description

    run(flow())


def test_other_users_cannot_press_buttons(cog):
    async def flow():
        await Rebuild.start.callback(cog, FakeInteraction(), "Nets")
        i = FakeInteraction()
        await Rebuild.sim_cmd.callback(cog, i)
        view = i.sent[-1][3]
        stranger = FakeInteraction(user_id=2, component=True)
        assert await view.interaction_check(stranger) is False

    run(flow())


def test_saves_survive_restart(cog, tmp_path):
    async def flow():
        await Rebuild.start.callback(cog, FakeInteraction(), "Spurs")
        cog.saves.clear()
        save = await cog.get_save(1)
        assert save["team"] == "SAS"
        rows = await cog.store.leaderboard(99)
        assert rows and rows[0][1] == "SAS"

    run(flow())
