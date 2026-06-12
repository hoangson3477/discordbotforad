from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
import aiohttp

from config import Config
from utils import embeds


class Utility(commands.Cog):
    """Utility commands: userinfo, serverinfo, avatar, ping, weather, translate."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._http: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self._http = aiohttp.ClientSession()

    async def cog_unload(self):
        if self._http:
            await self._http.close()

    # ── /ping ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="ping", description="Check bot latency")
    async def ping(self, interaction: discord.Interaction):
        ws_latency = round(self.bot.latency * 1000)

        before = datetime.now(timezone.utc)
        await interaction.response.defer()
        after = datetime.now(timezone.utc)
        api_latency = round((after - before).total_seconds() * 1000)

        e = embeds.info(
            f"🏓 **Pong!**\n"
            f"WebSocket: `{ws_latency}ms`\n"
            f"API: `{api_latency}ms`",
            title="Latency",
        )
        await interaction.followup.send(embed=e)

    # ── /userinfo ─────────────────────────────────────────────────────────────
    @app_commands.command(name="userinfo", description="Show info about a user")
    @app_commands.describe(member="Member to look up (leave empty for yourself)")
    async def userinfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        member = member or interaction.user

        created = discord.utils.format_dt(member.created_at, "D")
        joined  = discord.utils.format_dt(member.joined_at, "D") if member.joined_at else "Unknown"

        roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
        roles_str = " ".join(roles[:15]) or "None"
        if len(roles) > 15:
            roles_str += f" (+{len(roles) - 15} more)"

        badges = _get_badges(member)

        e = discord.Embed(color=member.color if member.color.value else embeds.Color.INFO)
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.set_thumbnail(url=member.display_avatar.url)

        e.add_field(name="🪪 Display Name", value=member.display_name, inline=True)
        e.add_field(name="🆔 User ID",       value=f"`{member.id}`",    inline=True)
        e.add_field(name="🤖 Bot",           value="Yes" if member.bot else "No", inline=True)

        e.add_field(name="📅 Account Created", value=created, inline=True)
        e.add_field(name="📥 Joined Server",   value=joined,  inline=True)

        if member.premium_since:
            e.add_field(
                name="💎 Boosting Since",
                value=discord.utils.format_dt(member.premium_since, "D"),
                inline=True,
            )

        if badges:
            e.add_field(name="🏅 Badges", value=" ".join(badges), inline=False)

        e.add_field(name=f"🎭 Roles ({len(roles)})", value=roles_str, inline=False)
        e.set_footer(text=f"Requested by {interaction.user}")

        await interaction.response.send_message(embed=e)

    # ── /serverinfo ───────────────────────────────────────────────────────────
    @app_commands.command(name="serverinfo", description="Show info about this server")
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        await g.chunk()  # ensure member cache is populated

        created = discord.utils.format_dt(g.created_at, "D")

        bots    = sum(1 for m in g.members if m.bot)
        humans  = g.member_count - bots

        text_ch    = len(g.text_channels)
        voice_ch   = len(g.voice_channels)
        categories = len(g.categories)

        e = discord.Embed(title=g.name, color=embeds.Color.INFO)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        if g.banner:
            e.set_image(url=g.banner.url)

        e.add_field(name="🆔 Server ID",   value=f"`{g.id}`", inline=True)
        e.add_field(name="👑 Owner",        value=g.owner.mention if g.owner else "Unknown", inline=True)
        e.add_field(name="📅 Created",      value=created, inline=True)

        e.add_field(name="👥 Members",      value=f"Total: {g.member_count}\nHumans: {humans} | Bots: {bots}", inline=True)
        e.add_field(name="💬 Channels",     value=f"Text: {text_ch} | Voice: {voice_ch}\nCategories: {categories}", inline=True)
        e.add_field(name="😀 Emojis",       value=f"{len(g.emojis)}/{g.emoji_limit}", inline=True)

        e.add_field(name="🎭 Roles",        value=str(len(g.roles) - 1), inline=True)
        e.add_field(name="💎 Boost Level",  value=f"Level {g.premium_tier} ({g.premium_subscription_count} boosts)", inline=True)
        e.add_field(name="🔒 Verification", value=str(g.verification_level).title(), inline=True)

        if g.description:
            e.add_field(name="📝 Description", value=g.description, inline=False)

        features = [f.replace("_", " ").title() for f in g.features[:8]]
        if features:
            e.add_field(name="✨ Features", value=", ".join(features), inline=False)

        e.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=e)

    # ── /avatar ───────────────────────────────────────────────────────────────
    @app_commands.command(name="avatar", description="Show a user's avatar")
    @app_commands.describe(
        member="Member to get avatar for (leave empty for yourself)",
        kind="Server avatar or global avatar",
    )
    @app_commands.choices(kind=[
        app_commands.Choice(name="Server (guild)", value="guild"),
        app_commands.Choice(name="Global",         value="global"),
    ])
    async def avatar(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        kind: str = "guild",
    ):
        member = member or interaction.user

        if kind == "guild" and member.guild_avatar:
            avatar = member.guild_avatar
            label  = "Server Avatar"
        else:
            avatar = member.avatar or member.default_avatar
            label  = "Global Avatar"

        formats = []
        for fmt in ("png", "jpg", "webp"):
            url = avatar.replace(format=fmt, size=1024).url
            formats.append(f"[{fmt.upper()}]({url})")
        if avatar.is_animated():
            formats.append(f"[GIF]({avatar.replace(format='gif', size=1024).url})")

        e = discord.Embed(
            title=f"{member.display_name} — {label}",
            description=" | ".join(formats),
            color=embeds.Color.INFO,
        )
        e.set_image(url=avatar.with_size(1024).url)
        await interaction.response.send_message(embed=e)

    # ── /weather ──────────────────────────────────────────────────────────────
    @app_commands.command(name="weather", description="Get current weather for a city")
    @app_commands.describe(city="City name (e.g. Hanoi, Tokyo, London)")
    async def weather(self, interaction: discord.Interaction, city: str):
        if not Config.WEATHER_API_KEY:
            return await interaction.response.send_message(
                embed=embeds.error("Weather API key not configured. Set `WEATHER_API_KEY` in `.env`."),
                ephemeral=True,
            )

        await interaction.response.defer()

        url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?q={city}&appid={Config.WEATHER_API_KEY}&units=metric"
        )

        async with self._http.get(url) as resp:
            if resp.status == 404:
                return await interaction.followup.send(
                    embed=embeds.error(f"City **{city}** not found."), ephemeral=True
                )
            if resp.status != 200:
                return await interaction.followup.send(
                    embed=embeds.error("Failed to fetch weather data. Try again later."), ephemeral=True
                )
            data = await resp.json()

        name        = data["name"]
        country     = data["sys"]["country"]
        description = data["weather"][0]["description"].title()
        icon_code   = data["weather"][0]["icon"]
        icon_url    = f"https://openweathermap.org/img/wn/{icon_code}@2x.png"

        temp        = data["main"]["temp"]
        feels_like  = data["main"]["feels_like"]
        temp_min    = data["main"]["temp_min"]
        temp_max    = data["main"]["temp_max"]
        humidity    = data["main"]["humidity"]
        wind_speed  = data["wind"]["speed"]
        visibility  = data.get("visibility", 0) // 1000  # m → km

        e = discord.Embed(
            title=f"🌤  Weather in {name}, {country}",
            description=description,
            color=embeds.Color.INFO,
        )
        e.set_thumbnail(url=icon_url)

        e.add_field(name="🌡️ Temperature",  value=f"`{temp:.1f}°C`  (feels like `{feels_like:.1f}°C`)", inline=False)
        e.add_field(name="📊 Min / Max",     value=f"`{temp_min:.1f}°C` / `{temp_max:.1f}°C`", inline=True)
        e.add_field(name="💧 Humidity",      value=f"`{humidity}%`",         inline=True)
        e.add_field(name="💨 Wind",          value=f"`{wind_speed} m/s`",    inline=True)
        e.add_field(name="👁️ Visibility",   value=f"`{visibility} km`",     inline=True)

        e.set_footer(text="Data from OpenWeatherMap")
        await interaction.followup.send(embed=e)

    # ── /translate ────────────────────────────────────────────────────────────
    @app_commands.command(name="translate", description="Translate text using MyMemory (free, no key needed)")
    @app_commands.describe(
        text="Text to translate",
        target="Target language code (e.g. vi, en, ja, ko, fr)",
        source="Source language code (default: auto-detect)",
    )
    async def translate(
        self,
        interaction: discord.Interaction,
        text: str,
        target: str = "en",
        source: str = "auto",
    ):
        await interaction.response.defer()

        lang_pair = f"{source}|{target}" if source != "auto" else f"autodetect|{target}"
        url = f"https://api.mymemory.translated.net/get?q={text}&langpair={lang_pair}"

        async with self._http.get(url) as resp:
            if resp.status != 200:
                return await interaction.followup.send(
                    embed=embeds.error("Translation failed. Try again later."), ephemeral=True
                )
            data = await resp.json()

        status = data.get("responseStatus", 500)
        if status != 200:
            return await interaction.followup.send(
                embed=embeds.error(f"Translation error: {data.get('responseDetails', 'Unknown error')}"),
                ephemeral=True,
            )

        translated   = data["responseData"]["translatedText"]
        detected     = data.get("responseData", {}).get("detectedLanguage") or source

        e = embeds.info(translated, title="🌐  Translation")
        e.add_field(name="Original", value=f"```{text[:500]}```", inline=False)
        e.add_field(name="From", value=f"`{detected}`", inline=True)
        e.add_field(name="To",   value=f"`{target}`",   inline=True)
        e.set_footer(text="Powered by MyMemory")

        await interaction.followup.send(embed=e)

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        msg = str(error)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embeds.error(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=embeds.error(msg), ephemeral=True)


# ── Badge helper ──────────────────────────────────────────────────────────────
def _get_badges(member: discord.Member) -> list[str]:
    badges = []
    flags = member.public_flags
    mapping = {
        flags.staff:                    "👨‍💼 Discord Staff",
        flags.partner:                  "🤝 Partnered",
        flags.hypesquad:                "🏠 HypeSquad Events",
        flags.bug_hunter:               "🐛 Bug Hunter",
        flags.hypesquad_bravery:        "🟠 Bravery",
        flags.hypesquad_brilliance:     "🟣 Brilliance",
        flags.hypesquad_balance:        "🔵 Balance",
        flags.early_supporter:          "⭐ Early Supporter",
        flags.bug_hunter_level_2:       "🐛 Bug Hunter Lv.2",
        flags.verified_bot_developer:   "🤖 Verified Bot Dev",
        flags.active_developer:         "💻 Active Developer",
    }
    for flag, label in mapping.items():
        if flag:
            badges.append(label)
    return badges


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))