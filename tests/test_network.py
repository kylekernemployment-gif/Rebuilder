"""Run the real fetch code against a local fake ESPN/RealGM server."""

import asyncio
import json

import aiohttp
from aiohttp import web

from rebuild import cba, data, rumors

RSS = """<rss><channel>
<item><title>Kings Open To Moving Zach LaVine</title><description>&lt;p&gt;Sacramento is listening to offers for
Zach LaVine, according to Jake Fischer.&lt;/p&gt;</description><link>https://x/1</link>
<pubDate>Fri, 09 Oct 2026 09:15:08 EDT</pubDate></item></channel></rss>"""


def roster_json(tid):
    abbr = cba.BY_ESPN_ID[tid]
    ath = []
    for i in range(14):
        pid = f"{tid}{i:02d}"
        name = "Zach LaVine" if (abbr == "SAC" and i == 0) else f"P{pid}"
        ath.append({"id": pid, "displayName": name, "age": 24 + i % 8, "position": {"abbreviation": "G"},
                    "experience": {"years": 3}, "contract": {"salary": 5_000_000 + i * 1_000_000, "yearsRemaining": 2},
                    "contracts": [], "status": {"type": "active"}})
    return {"athletes": ath}


def stats_page(page, per=150):
    ids = [f"{t}{i:02d}" for t in sorted(cba.BY_ESPN_ID) for i in range(14)]
    chunk = ids[(page - 1) * per: page * per]
    cats = [{"name": "general", "names": ["gamesPlayed", "avgMinutes"]},
            {"name": "offensive", "names": ["avgPoints", "avgAssists"]}]
    rows = [{"athlete": {"id": i}, "categories": [{"name": "general", "values": [60, 25.0]},
                                                  {"name": "offensive", "values": [12.0, 3.0]}]} for i in chunk]
    return {"categories": cats, "athletes": rows, "pagination": {"pages": (len(ids) + per - 1) // per}}


def test_fetch_league_and_rumors_against_fake_server(monkeypatch):
    async def flow():
        app = web.Application()

        async def blocked(_):
            return web.Response(status=403)

        async def roster(req):  # the ?enable=roster route wraps athletes in "team"
            return web.json_response({"team": roster_json(int(req.match_info["tid"]))})

        async def stats(req):
            return web.json_response(stats_page(int(req.query.get("page", 1))))

        async def rss(_):
            return web.Response(text=RSS, content_type="application/xml")

        async def espn_news(_):
            return web.json_response({"articles": [{"headline": "Bucks not shopping anyone", "description": "",
                                                    "published": "2026-10-09T12:00:00Z"}]})

        async def reddit(_):
            return web.Response(status=429)

        app.router.add_get("/roster/{tid}", blocked)
        app.router.add_get("/team/{tid}", roster)
        app.router.add_get("/stats", stats)
        app.router.add_get("/rss", rss)
        app.router.add_get("/news", espn_news)
        app.router.add_get("/reddit", reddit)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(data, "ROSTER_URL", base + "/roster/{tid}")
        monkeypatch.setattr(data, "ROSTER_FALLBACKS", [base + "/team/{tid}?enable=roster"])
        monkeypatch.setattr(data, "STATS_URL", base + "/stats")
        monkeypatch.setattr(rumors, "REALGM_RSS", base + "/rss")
        monkeypatch.setattr(rumors, "ESPN_NEWS", base + "/news")
        monkeypatch.setattr(rumors, "REDDIT_RSS", base + "/reddit")
        async with aiohttp.ClientSession() as s:
            league = await data.fetch_league(s, {"skip_payrolls": True})
            items = await rumors.fetch_items(s)
        await runner.cleanup()
        return league, items

    league, items = asyncio.run(flow())
    assert len(league["teams"]) == 30 and len(league["players"]) == 420
    assert all(p["hist"] for p in league["players"].values())  # every stats page was read
    assert len(items) == 2
    feed = rumors.RumorFeed()
    feed.set_players(league["players"])
    feed.ingest(items, now=items[0]["ts"] + 60)
    lavine = next(pid for pid, p in league["players"].items() if p["name"] == "Zach LaVine")
    assert feed.heat["players"][lavine]["h"] > 0.3
