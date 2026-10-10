"""Drive the real slash-command callbacks and menus with fake Discord interactions."""

import asyncio
import types

import discord
import pytest

from fixtures import fake_espn
from rebuild import cba
from rebuild import league as L
from rebuild.cog import Rebuild


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

    async def send_modal(self, modal):
        self.done = True
        self.inter.sent.append(("modal", None, None, modal))


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


def find(view, kind, label_start=None, placeholder_start=None):
    for item in view.children:
        if kind == "button" and isinstance(item, discord.ui.Button) and item.label and item.label.startswith(label_start):
            return item
        if kind == "select" and isinstance(item, discord.ui.Select) and (
                placeholder_start is None or (item.placeholder or "").startswith(placeholder_start)):
            return item
    raise AssertionError(f"no {kind} {label_start or placeholder_start}: "
                         f"{[getattr(c, 'label', None) or getattr(c, 'placeholder', None) for c in view.children]}")


async def click(view, label):
    x = FakeInteraction(component=True)
    await find(view, "button", label).callback(x)
    if x.last_embed():
        check_embed(x.last_embed())
    return x


async def choose(view, placeholder, value):
    x = FakeInteraction(component=True, values=[value])
    await find(view, "select", placeholder_start=placeholder).callback(x)
    if x.last_embed():
        check_embed(x.last_embed())
    return x


async def menu(view, key):
    return await choose(view, "☰ More", key)


def test_everything_runs_from_the_hub(cog):
    async def flow():
        i = FakeInteraction()
        await Rebuild.rebuild_cmd.callback(cog, i)
        hub = i.sent[-1][3]
        assert "Pick your team" in i.last_embed().title
        x = await choose(hub, "Run the (East)", "NYK")
        assert "Knicks" in x.last_embed().title
        save = await cog.get_save(1)
        # menu layout: 5 nav buttons + the More menu on top
        assert [c.label for c in hub.children[:5]] == ["Home", "Roster", "Trade", "Free agents", "Lineup"]
        for key in ("roster", "lineup", "fa"):
            await click(hub, {"roster": "Roster", "lineup": "Lineup", "fa": "Free agents"}[key])
        for key in ("coach", "standings", "stats", "teamstats", "injuries", "draftboard", "market", "rumors",
                    "history", "help", "extend", "inbox", "shop", "release"):
            await menu(hub, key)
        # roster -> player card -> trade for him -> trade finder -> propose
        await click(hub, "Roster")
        await choose(hub, "View another team (East)", "BOS")
        target = L.roster(save, "BOS", include_twoway=False)[4]
        await choose(hub, "Open a player card", target["id"])
        await click(hub, "Trade for")
        x = await click(hub, "What would it take?")
        await click(hub, "Propose trade")
        # shop
        await menu(hub, "shop")
        mine = L.roster(save, "NYK", include_twoway=False)[1]
        x = await choose(hub, "Put a player on the block", mine["id"])
        if any(getattr(c, "label", "") == "Accept this offer" for c in hub.children):
            assert "Offer 1 of" in x.last_embed().fields[0].name
            nxt = find(hub, "button", "Next offer")
            if not nxt.disabled:
                y = await click(hub, "Next offer")
                assert "Offer 2 of" in y.last_embed().fields[0].name
                await click(hub, "Previous")
        # season: week, deadline, end, playoffs, offseason
        await click(hub, "Home")
        await click(hub, "Sim 1 week")
        await click(hub, "Sim to trade deadline")
        assert save["phase"] == "deadline"
        await click(hub, "Sim to end of season")
        assert save["phase"] == "stretch" and save.get("awards")
        x = await click(hub, "Trade")
        assert "locked" in x.last_embed().title
        await click(hub, "Home")
        await click(hub, "Play the playoffs")
        await click(hub, "Start offseason")
        save = await cog.get_save(1)
        while save["phase"] == "draft":
            try:
                await choose(hub, "You're on the clock", save["draft"]["prospects"][0]["id"])
            except AssertionError:
                await click(hub, "Auto-draft the rest")
        assert save["phase"] == "resign"
        await click(hub, "Open free agency")
        await click(hub, "Next free agency day")
        await click(hub, "Free agents")
        await click(hub, "Home")
        await click(hub, "Finish offseason")
        assert save["phase"] == "preseason" and save["season"] == 2028
        await menu(hub, "history")

    run(flow())


def test_other_users_cannot_press_buttons(cog):
    async def flow():
        i = FakeInteraction()
        await Rebuild.rebuild_cmd.callback(cog, i)
        hub = i.sent[-1][3]
        stranger = FakeInteraction(user_id=2, component=True)
        assert await hub.interaction_check(stranger) is False

    run(flow())


def test_saves_survive_restart(cog, tmp_path):
    async def flow():
        i = FakeInteraction()
        await Rebuild.rebuild_cmd.callback(cog, i)
        await choose(i.sent[-1][3], "Run the (West)", "SAS")
        cog.saves.clear()
        save = await cog.get_save(1)
        assert save["team"] == "SAS"
        rows = await cog.store.leaderboard(99)
        assert rows and rows[0][1] == "SAS"

    run(flow())
