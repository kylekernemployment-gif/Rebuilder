# Rebuild: NBA Front Office Simulator (Discord bot)

Take over any of the 30 NBA teams with the real, current rosters and contracts, then trade, draft, sign and simulate your way to a title. AI front offices react to a live feed of NBA rumors while your save is still on the real timeline.

Everything runs on free data. Rosters, real contract numbers and player stats come from ESPN's public JSON feeds (the same source PC Pick Em' already uses for scores). Rumors come from RealGM's Wiretap RSS, ESPN's NBA news feed and r/nba insider posts. There are no API keys and no paid services, so the only cost is Render hosting.

## What makes it realistic

Ratings come from real production. Each player's overall is built from his last three seasons of ESPN box scores (weighted toward the most recent and shrunk toward his contract value when the sample is small, which handles injured stars and rookies), then mapped onto a 2K-style ratings curve. Potential comes from age and current level, and every player gets a scouting report (scoring, shooting, playmaking, rebounding, defense).

Every AI team has a direction. Contenders pay for production now, rebuilders pay for youth, upside and picks, and teams in the middle split the difference. Everyone cares about contracts, so a cheap good player is gold and bad money is a liability that takes a sweetener to move. Teams protect their franchise player and their young core, flip veterans when rebuilding, and hate paying luxury tax unless they're contending.

Trades follow the 2026-27 CBA with the official cap numbers ($164.961M cap, $200.428M tax, $209.015M first apron, $221.686M second apron). The builder checks salary matching, the "can't take back more salary" rule above the first apron, the ban on combining salaries above the second apron, roster limits, and the Stepien rule on consecutive first-round picks. Waived players leave dead money on the cap, so salary dumps aren't free.

The rumor engine reads every report, matches it to players and teams on the live rosters, and tags what it signals: trade request, on the block, unhappy, teams interested, off the table, extension talks, or a team leaning toward rebuilding or going all in. Each report is weighted by reporter credibility (Tier 1 national insiders like Shams Charania count most, established beat writers next, generic outlet reports less, unattributed posts barely at all), it fades over about two weeks, and it gets boosted when several independent insiders say the same thing. That "heat" directly changes how willing each AI team is to move a player and can shift a team's direction. Rumors can't truly be verified until something happens, so the bot is honest about it and shows the source tier on every report. Live news shapes your save until your first trade deadline; after that your league has diverged from reality and plays out on its own.

Seasons are fully simulated with a real-format 82-game schedule, injuries, standings, the play-in, four rounds of playoffs, and MVP, DPOY, ROY and Finals MVP. AI teams make their own trades at the deadline, and teams call you with offers. The offseason has aging and development, retirements, the real lottery odds, a 72-player draft class with scouting ranges instead of exact potential, rookie-scale contracts, re-signing with Bird rights (stars on losing teams may refuse), and free agency with cap space, the non-taxpayer and taxpayer mid-level exceptions and minimum deals. Free agents ask for more to join a losing team.

## New in this version

Real contracts from Basketball-Reference (so new deals like Jalen Duren's show up within a day or two) and NBA 2K27 overalls from 2kratings.com, both refreshed twice a day in the background, with ESPN and the bot's own ratings as a fallback. Real free agents (players from last season who are unsigned). Week-by-week and month-by-month simming that always pauses at the trade deadline, with trades locked afterward. Free agency you can negotiate any time: in the offseason it runs in days, stars weigh competing offers, and prices fall the longer players go unsigned. Contract extensions, releasing players, lineups and minutes, load management, team chemistry, player morale and trade requests, real head coaches you can fire and replace, real-format injuries with injury-prone and iron-man players and career-altering major injuries, simulated box scores, team stats, league leaders, regular-season awards and Finals MVP, style matchups, the real 2027 draft class with bust rates based on history, and AI trades at real-world volume (about 20 at the deadline).

## How to play

Type `/rebuild` once. It opens your front office as a single message with a menu pinned to the top: Home (sim the season week by week or month by month, the deadline, playoffs, draft and free agency), Roster, Trade, Free agents and Lineup as buttons, plus a "More" menu with shopping a player, the offers inbox, extensions, releasing players, your coach, standings, league leaders, team stats, the injury report, the draft board, the trade market, the rumor feed, history, the server leaderboard and switching teams. Every screen opens in place under the menu, and only you can press your own buttons.

## Setting it up in your private test server

First create the bot. In the Discord Developer Portal (discord.com/developers/applications), click New Application, open the Bot page, click Reset Token and copy the token. No privileged intents are needed. Then open OAuth2, URL Generator, tick the `bot` and `applications.commands` scopes, give it Send Messages, Embed Links and Use Slash Commands, and open the generated link to invite it to your test server. In Discord, turn on Developer Mode (Settings, Advanced), right-click your test server and Copy Server ID.

Next, set up the database. Saves need Postgres on Render because Render wipes the disk on every deploy. You can reuse the same free Neon database PC Pick Em' already uses, since this bot only creates its own `rebuild_saves` and `rebuild_cache` tables and never touches the Pick Em' tables. If you'd rather keep them separate, create a free Neon project and copy its connection string.

Then deploy to Render. Push this folder to a new GitHub repo, then in Render choose New, Blueprint and pick the repo (it reads `render.yaml`). Fill in `DISCORD_TOKEN`, `GUILD_ID` (your test server ID, so commands show up instantly) and `DATABASE_URL`. The blueprint uses Render's free web service plan, and the bot pings itself every 10 minutes so it doesn't fall asleep. Free instances share 750 hours a month across your account, so if you leave it running all month alongside another free service, switch this one to the Starter plan like your main bot.

On first boot the bot downloads all 30 rosters and three seasons of stats (about 20 seconds), then refreshes rosters every 6 hours and rumors every 10 minutes. Run `/rebuild help` to confirm it's loaded.

To run it locally instead, install Python 3.12, run `pip install -r requirements.txt`, set `DISCORD_TOKEN` and `GUILD_ID` in your environment, and run `python bot.py`. Without `DATABASE_URL` it stores saves in a local SQLite file.

## Merging into PC Pick Em' later

The game lives entirely in the `rebuild/` folder and uses the same libraries as PC Pick Em'. Copy `rebuild/` into the PC Pick Em' repo next to `features/`, add `from rebuild.cog import Rebuild` at the top of `bot.py`, and add this line inside `PickEmBot.load_features()`:

```python
await self.add_cog(Rebuild(self, DATABASE_URL), guild=guild)
```

That's it. The `/rebuild` commands sync with the rest on the next deploy, and saves carry over if both bots used the same database.

## Things to know

Contract years beyond what ESPN lists are estimated with 5% raises, and player and team options are treated as guaranteed years. Draft pick ownership comes from `rebuild/data/picks.json`, a hand-maintained list of first-round picks that have already been traded (swaps are left out and protections are simplified). It's my best read as of October 2026, so check it and edit it if anything is off; new saves pick up changes immediately. Two-way players don't count against the cap or the 15-man roster. The salary-matching brackets follow the 2023 CBA structure scaled to 2026-27 and are close approximations.

## Tests

`python -m pytest` runs the engine tests (contract parsing, CBA rules, AI negotiation, rumor heat, multi-season sims), drives every slash command and menu with fake Discord interactions, and runs the real fetch code against a local fake ESPN and RealGM server.
