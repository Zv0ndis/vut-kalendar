# /// script
# requires-python = ">=3.11"
# dependencies = ["discord.py>=2.4", "google-api-python-client", "google-auth-oauthlib", "anthropic"]
# ///
"""ZHelper slash commands over the Discord gateway (runs as the zhelper systemd service, no inbound port needed).

/dnes, /sync, /tyden start the matching studis@ systemd run, which posts the result as usual; /motd replies with
today's digest; /zitra, /body, /terminy, /stav answer from the calendars and studis.json; /ucit adds a study block; /ai and plain DMs go to the agent in agent.py.
Slash commands are registered by the zhelper GitHub workflow. Needs DISCORD_BOT_TOKEN and DISCORD_USER_ID; the bot only answers that one user.
"""
import asyncio, json, os, subprocess, sys, time, traceback
from datetime import datetime
from pathlib import Path
import discord

sys.path.insert(0, str(Path(__file__).parent.parent))
import envutil  # noqa: E402,F401  loads .env
import digest  # noqa: E402
from sync import PRAGUE  # noqa: E402
import agent  # noqa: E402
from agent import MODES, fresh_digest  # noqa: E402

USER = envutil.num("DISCORD_USER_ID", 0)  # 0 = unset; checked before the gateway starts
intents = discord.Intents.none()
intents.dm_messages = True  # DM text needs no privileged intent
client = discord.Client(intents=intents)
LAST_CRASH = 0.0
agent.BOT.update(start=time.time(), latency=lambda: client.latency, error=None)  # /stav reads it (inf until the gateway connects)


def crash(e):
    """❌ DM through digest.send, at most one per hour so a crash loop can't spam."""
    global LAST_CRASH
    agent.BOT["error"] = (time.time(), f"{type(e).__name__}: {e}"[:200])
    if time.time() - LAST_CRASH < 3600:
        return
    LAST_CRASH = time.time()
    try:
        digest.send(f"❌ bot: {type(e).__name__}: {e}"[:1900])
    except Exception as x:
        print("crash DM failed:", x)


async def motd():
    """Today's digest as digest.py left it: message ids from digest-state.json, text from the bot's DM with the user.
    Before the morning run the state is yesterday's (or missing), so build it fresh."""
    state = json.loads(digest.STATE.read_text()) if digest.STATE.exists() else {}
    if state.get("date") != datetime.now(PRAGUE).date().isoformat() or not state.get("messages"):
        return await asyncio.to_thread(fresh_digest)
    dm = await (await client.fetch_user(USER)).create_dm()
    return "\n".join([(await dm.fetch_message(int(i))).content for i in state["messages"]])


ANSWERS = {**agent.ANSWERS, "ai": lambda o: agent.ask(o["otazka"])}


@client.event
async def on_message(m: discord.Message):
    if m.author.bot or m.guild or m.author.id != USER or not m.content:
        return
    async with m.channel.typing():
        try:
            text = await asyncio.to_thread(agent.ask, m.content)
        except Exception as e:
            text = f"❌ {e}"
            await asyncio.to_thread(crash, e)
    for part in digest.chunks(text):
        await m.channel.send(part)


@client.event
async def on_interaction(i: discord.Interaction):
    if i.type is not discord.InteractionType.application_command:
        return
    name = i.data.get("name")
    if i.user.id != USER:
        return await i.response.send_message("Tohle je osobní bot.", ephemeral=True)
    if name in MODES:
        r = subprocess.run(["sudo", "systemctl", "start", "--no-block", f"studis@{MODES[name]}"], capture_output=True, text=True)
        return await i.response.send_message("⏳ spuštěno, výsledek přijde do DM za chvíli" if r.returncode == 0 else f"❌ {r.stderr}", ephemeral=True)
    if name != "motd" and name not in ANSWERS:
        return await i.response.send_message("Neznámý příkaz.", ephemeral=True)
    await i.response.defer(ephemeral=True)  # Discord gives 3 s, calendar lookups may take longer
    opts = {o["name"]: o["value"] for o in i.data.get("options", [])}
    try:
        text = await motd() if name == "motd" else await asyncio.to_thread(ANSWERS[name], opts)
    except Exception as e:
        text = f"❌ {e}"
        await asyncio.to_thread(crash, e)
    for part in digest.chunks(text):
        await i.followup.send(part, ephemeral=True)


@client.event
async def on_error(event, *args, **kwargs):
    traceback.print_exc()  # what discord.py's default handler logs
    await asyncio.to_thread(crash, sys.exc_info()[1])


def selftest():
    global LAST_CRASH
    sent = []
    digest.send = sent.append
    crash(ValueError("a"))
    crash(ValueError("b"))
    assert sent == ["❌ bot: ValueError: a"], sent  # second one within the hour is dropped
    assert agent.BOT["error"][1] == "ValueError: b" and agent.BOT["start"] <= time.time() and callable(agent.BOT["latency"])  # kept for /stav even without a DM
    LAST_CRASH -= 3601
    crash(ValueError("c"))
    assert len(sent) == 2
    print("selftest ok")


if sys.argv[1:] == ["--selftest"]:
    selftest()
else:
    if not (os.environ.get("DISCORD_BOT_TOKEN") and USER):
        sys.exit("DISCORD_BOT_TOKEN and DISCORD_USER_ID must be set (repo-root .env, see deploy/bot.env.example)")
    client.run(os.environ["DISCORD_BOT_TOKEN"])
