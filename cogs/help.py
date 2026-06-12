from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands
from utils import embeds


class Help(commands.Cog):
    """Shows help for all available commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show all available commands")
    async def help(self, interaction: discord.Interaction):
        e = embeds.info(
            "Use `/help <category>` for detailed info on each module.",
            title="📖  MultiBot — Command List",
        )

        modules = {
            "🔨 Moderation": (
                "`/ban` `/unban` `/kick`\n"
                "`/timeout` `/untimeout`\n"
                "`/mute` `/unmute`\n"
                "`/warn` `/warns` `/clearwarns`\n"
                "`/purge` `/slowmode`\n"
                "`/setmodlog` `/setmuterole`"
            ),
            "🛠️ Utility": "`/userinfo` `/serverinfo` `/avatar` `/ping` `/weather` `/translate`",
            "🎮 Fun":     "`/roll` `/flip` `/8ball` `/meme` `/joke`",
            "💰 Economy": "`/balance` `/daily` `/pay` `/leaderboard`",
            "🎵 Music":   "`/play` `/skip` `/queue` `/pause` `/resume` `/stop` `/nowplaying`",
        }

        for name, cmds in modules.items():
            e.add_field(name=name, value=cmds, inline=False)

        e.set_footer(text=f"Bot by {self.bot.application.owner if self.bot.application else 'Dev'}")
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))