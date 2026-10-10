"""Rebuild: NBA front office simulator (standalone test bot).

To merge into PC Pick Em' later, copy the `rebuild/` folder next to `features/` and add one line
to PickEmBot.load_features():  await self.add_cog(Rebuild(self, DATABASE_URL), guild=guild)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands, tasks

from rebuild.cog import Rebuild

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rebuild")


def _env(name: str, required: bool = True) -> str:
    value = os.getenv(name, "").strip().strip("'\"").strip()
    if required and not value:
        raise SystemExit(f"Missing environment variable {name}. Add it in Render -> Environment.")
    return value


def _database_url() -> str | None:
    url = _env("DATABASE_URL", required=False)
    if url.lower().startswith("psql"):
        url = url[4:].strip().strip("'\"")
    return url or None


TOKEN = _env("DISCORD_TOKEN")
GUILD_ID = _env("GUILD_ID", required=False)
SELF_PING_EVERY = 10 * 60


class RebuildBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default(), help_command=None)
        self.last_ping = 0.0
        self.synced: set[int] = set()

    async def setup_hook(self) -> None:
        await self.add_cog(Rebuild(self, _database_url(), os.getenv("DB_PATH", "rebuild.db")))
        self.keep_awake.start()

    async def sync_to(self, guild: discord.abc.Snowflake, name: str = "") -> None:
        """Register the slash commands in one server (shows up instantly, unlike global sync)."""
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash command group(s) to server %s %s", len(synced), guild.id, name)
        except discord.Forbidden:
            log.error("Couldn't register slash commands in server %s %s. Re-invite the bot with BOTH the 'bot' "
                      "and 'applications.commands' scopes checked.", guild.id, name)
        except discord.HTTPException as e:
            log.error("Slash command sync failed in server %s: %r", guild.id, e)

    @tasks.loop(minutes=5)
    async def keep_awake(self) -> None:
        """Free Render web services sleep after 15 idle minutes; pinging our own URL prevents that."""
        url = os.getenv("RENDER_EXTERNAL_URL")
        if not url or time.time() - self.last_ping < SELF_PING_EVERY:
            return
        self.last_ping = time.time()
        try:
            async with aiohttp.ClientSession() as s, s.get(url.rstrip("/") + "/health") as r:
                await r.read()
        except Exception as e:
            log.debug("Self-ping failed: %r", e)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s), in %d server(s)", self.user, self.user.id, len(self.guilds))
        if not self.guilds:
            log.error("The bot isn't in any server yet. Open the invite link from the Developer Portal "
                      "(OAuth2 > URL Generator, scopes 'bot' + 'applications.commands') and add it to your server.")
        for g in self.guilds:
            if g.id not in self.synced:  # on_ready fires again on reconnects
                await self.sync_to(g, f"({g.name})")
                self.synced.add(g.id)
        if GUILD_ID.isdigit() and int(GUILD_ID) not in {g.id for g in self.guilds}:
            log.warning("GUILD_ID %s isn't a server this bot is in. Commands were registered in the servers above.", GUILD_ID)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.sync_to(guild, f"({guild.name})")


async def start_web_server() -> None:
    port = os.getenv("PORT")
    if not port:
        return

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="Rebuild bot is running")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Health server listening on :%s", port)


async def main() -> None:
    await start_web_server()
    bot = RebuildBot()
    async with bot:
        try:
            await bot.start(TOKEN)
        except discord.LoginFailure:
            raise SystemExit("DISCORD_TOKEN is invalid. Reset it in the Discord developer portal and paste the new one into Render.")


if __name__ == "__main__":
    asyncio.run(main())
