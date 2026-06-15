import discord
from discord.ext import commands
import asyncio
import os
import logging
from pathlib import Path
from config import Config

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


# ── Bot class ─────────────────────────────────────────────────────────────────
class MultiBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True

        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,  # custom help command in cog
        )

    # ── Load all cogs automatically ───────────────────────────────────────────
    async def load_cogs(self):
        cog_dir = Path(__file__).parent / "cogs"
        for file in sorted(cog_dir.glob("*.py")):
            if file.stem.startswith("_"):
                continue
            ext = f"cogs.{file.stem}"
            try:
                await self.load_extension(ext)
                log.info(f"✅ Loaded: {ext}")
            except Exception as e:
                log.error(f"❌ Failed to load {ext}: {e}")

    # ── Setup hook (runs before bot connects) ─────────────────────────────────
    async def setup_hook(self):
        await self.load_cogs()
        # Không sync ở đây vì self.guilds chưa có khi setup_hook chạy

    async def on_ready(self):
        log.info(f"🤖 Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"📡 Connected to {len(self.guilds)} guild(s)")

        # Sync instant vào tất cả guild
        synced_count = 0
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                synced_count += 1
                log.info(f"✅ Synced: {guild.name} ({guild.id})")
                await asyncio.sleep(0.5)
            except discord.Forbidden:
                log.warning(f"⚠️ No permission: {guild.name}")
            except discord.HTTPException as e:
                log.error(f"❌ HTTPException {guild.name}: {e.status} {e.text}")
            except Exception as e:
                log.error(f"❌ Failed {guild.name}: {e}")

        log.info(f"🔄 Synced {synced_count}/{len(self.guilds)} guilds")

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{len(self.guilds)} servers | /help",
            )
        )

    async def on_guild_join(self, guild: discord.Guild):
        log.info(f"➕ Joined: {guild.name} ({guild.id})")
        # Sync instant khi bot vào server mới
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f"✅ Synced new guild: {guild.name}")
        except Exception as e:
            log.error(f"❌ Sync failed for new guild {guild.name}: {e}")
        # Init DB
        from database.supabase_client import db
        await db.init_guild(guild.id, guild.name)

    async def on_guild_remove(self, guild: discord.Guild):
        log.info(f"➖ Left guild: {guild.name} (ID: {guild.id})")


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with MultiBot() as bot:
        await bot.start(Config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
