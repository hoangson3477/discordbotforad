from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands
from datetime import timedelta
import asyncio

from database.supabase_client import db
from utils import embeds


# ── Permission check helper ───────────────────────────────────────────────────
def mod_only():
    """Slash command check: requires Manage Guild or Administrator."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        perms = interaction.user.guild_permissions
        if perms.manage_guild or perms.administrator:
            return True
        raise app_commands.CheckFailure("You need **Manage Server** permission to use this command.")
    return app_commands.check(predicate)


async def send_mod_log(guild: discord.Guild, embed: discord.Embed) -> None:
    """Send embed to the guild's configured mod-log channel (if set)."""
    settings = await db.get_guild_settings(guild.id)
    channel_id = settings.get("mod_log_channel")
    if channel_id:
        channel = guild.get_channel(int(channel_id))
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                pass


# ── Cog ───────────────────────────────────────────────────────────────────────
class Moderation(commands.Cog):
    """Moderation tools: ban, kick, mute, warn, purge, timeout, slowmode."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /ban ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(
        member="Member to ban",
        reason="Reason for ban",
        delete_days="Delete messages from last N days (0-7)",
    )
    @mod_only()
    @app_commands.default_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ):
        if member.top_role >= interaction.user.top_role:
            return await interaction.response.send_message(
                embed=embeds.error("You can't ban someone with an equal or higher role."), ephemeral=True
            )

        try:
            await member.send(
                embed=embeds.warning(
                    f"You have been **banned** from **{interaction.guild.name}**.\nReason: {reason}"
                )
            )
        except discord.HTTPException:
            pass

        await member.ban(reason=f"{interaction.user} | {reason}", delete_message_days=delete_days)

        log_embed = embeds.mod_action("Ban", member, interaction.user, reason)
        await interaction.response.send_message(embed=log_embed)
        await send_mod_log(interaction.guild, log_embed)

    # ── /unban ────────────────────────────────────────────────────────────────
    @app_commands.command(name="unban", description="Unban a user by ID")
    @app_commands.describe(user_id="User ID to unban", reason="Reason")
    @mod_only()
    @app_commands.default_permissions(ban_members=True)
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: str = "No reason provided",
    ):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=f"{interaction.user} | {reason}")
            e = embeds.success(f"**{user}** has been unbanned.", title="🔓  Unban")
            await interaction.response.send_message(embed=e)
            await send_mod_log(interaction.guild, e)
        except discord.NotFound:
            await interaction.response.send_message(
                embed=embeds.error("User not found or not banned."), ephemeral=True
            )
        except ValueError:
            await interaction.response.send_message(
                embed=embeds.error("Invalid user ID."), ephemeral=True
            )

    # ── /kick ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason for kick")
    @mod_only()
    @app_commands.default_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ):
        if member.top_role >= interaction.user.top_role:
            return await interaction.response.send_message(
                embed=embeds.error("You can't kick someone with an equal or higher role."), ephemeral=True
            )

        try:
            await member.send(
                embed=embeds.warning(
                    f"You have been **kicked** from **{interaction.guild.name}**.\nReason: {reason}"
                )
            )
        except discord.HTTPException:
            pass

        await member.kick(reason=f"{interaction.user} | {reason}")

        log_embed = embeds.mod_action("Kick", member, interaction.user, reason)
        await interaction.response.send_message(embed=log_embed)
        await send_mod_log(interaction.guild, log_embed)

    # ── /timeout ──────────────────────────────────────────────────────────────
    @app_commands.command(name="timeout", description="Timeout a member (max 28 days)")
    @app_commands.describe(
        member="Member to timeout",
        minutes="Duration in minutes",
        reason="Reason",
    )
    @mod_only()
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320],
        reason: str = "No reason provided",
    ):
        if member.top_role >= interaction.user.top_role:
            return await interaction.response.send_message(
                embed=embeds.error("You can't timeout someone with an equal or higher role."), ephemeral=True
            )

        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        await member.timeout(until, reason=f"{interaction.user} | {reason}")

        duration_str = f"{minutes} minute(s)"
        log_embed = embeds.mod_action(
            "Timeout", member, interaction.user, reason,
            extra={"Duration": duration_str, "Expires": discord.utils.format_dt(until, "R")}
        )
        await interaction.response.send_message(embed=log_embed)
        await send_mod_log(interaction.guild, log_embed)

    # ── /untimeout ────────────────────────────────────────────────────────────
    @app_commands.command(name="untimeout", description="Remove timeout from a member")
    @app_commands.describe(member="Member to untimeout", reason="Reason")
    @mod_only()
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ):
        await member.timeout(None, reason=f"{interaction.user} | {reason}")
        e = embeds.success(f"Timeout removed from {member.mention}.", title="⏱️  Untimeout")
        await interaction.response.send_message(embed=e)
        await send_mod_log(interaction.guild, e)

    # ── /mute (role-based) ────────────────────────────────────────────────────
    @app_commands.command(name="mute", description="Mute a member using the configured mute role")
    @app_commands.describe(member="Member to mute", reason="Reason")
    @mod_only()
    @app_commands.default_permissions(manage_roles=True)
    async def mute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ):
        settings = await db.get_guild_settings(interaction.guild.id)
        mute_role_id = settings.get("mute_role")
        if not mute_role_id:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "No mute role configured. Use `/setmuterole @role` first."
                ),
                ephemeral=True,
            )

        mute_role = interaction.guild.get_role(int(mute_role_id))
        if not mute_role:
            return await interaction.response.send_message(
                embed=embeds.error("Configured mute role not found. Please re-set it."), ephemeral=True
            )

        if mute_role in member.roles:
            return await interaction.response.send_message(
                embed=embeds.warning(f"{member.mention} is already muted."), ephemeral=True
            )

        await member.add_roles(mute_role, reason=f"{interaction.user} | {reason}")
        log_embed = embeds.mod_action("Mute", member, interaction.user, reason)
        await interaction.response.send_message(embed=log_embed)
        await send_mod_log(interaction.guild, log_embed)

    # ── /unmute ───────────────────────────────────────────────────────────────
    @app_commands.command(name="unmute", description="Unmute a member")
    @app_commands.describe(member="Member to unmute", reason="Reason")
    @mod_only()
    @app_commands.default_permissions(manage_roles=True)
    async def unmute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ):
        settings = await db.get_guild_settings(interaction.guild.id)
        mute_role_id = settings.get("mute_role")
        if not mute_role_id:
            return await interaction.response.send_message(
                embed=embeds.error("No mute role configured."), ephemeral=True
            )

        mute_role = interaction.guild.get_role(int(mute_role_id))
        if not mute_role or mute_role not in member.roles:
            return await interaction.response.send_message(
                embed=embeds.warning(f"{member.mention} is not muted."), ephemeral=True
            )

        await member.remove_roles(mute_role, reason=f"{interaction.user} | {reason}")
        e = embeds.success(f"{member.mention} has been unmuted.", title="🔊  Unmute")
        await interaction.response.send_message(embed=e)
        await send_mod_log(interaction.guild, e)

    # ── /warn ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason for warn")
    @mod_only()
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
    ):
        total = await db.add_warn(interaction.guild.id, member.id, interaction.user.id, reason)

        try:
            await member.send(
                embed=embeds.warning(
                    f"You received a warning in **{interaction.guild.name}**.\n"
                    f"Reason: {reason}\nTotal warnings: **{total}**"
                )
            )
        except discord.HTTPException:
            pass

        log_embed = embeds.mod_action(
            "Warn", member, interaction.user, reason,
            extra={"Total Warnings": str(total)}
        )
        await interaction.response.send_message(embed=log_embed)
        await send_mod_log(interaction.guild, log_embed)

    # ── /warns ────────────────────────────────────────────────────────────────
    @app_commands.command(name="warns", description="View warnings for a member")
    @app_commands.describe(member="Member to check")
    @mod_only()
    async def warns(self, interaction: discord.Interaction, member: discord.Member):
        warns = await db.get_warns(interaction.guild.id, member.id)

        if not warns:
            return await interaction.response.send_message(
                embed=embeds.info(f"{member.mention} has no warnings.", title="📋  Warnings"),
                ephemeral=True,
            )

        e = discord.Embed(
            title=f"📋  Warnings — {member.display_name}",
            color=embeds.Color.WARNING,
        )
        for i, w in enumerate(warns, 1):
            mod = interaction.guild.get_member(int(w["mod_id"]))
            mod_str = mod.mention if mod else f"`{w['mod_id']}`"
            e.add_field(
                name=f"#{i} — {w['created_at'][:10]}",
                value=f"**Reason:** {w['reason']}\n**Mod:** {mod_str}",
                inline=False,
            )
        e.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ── /clearwarns ───────────────────────────────────────────────────────────
    @app_commands.command(name="clearwarns", description="Clear all warnings for a member")
    @app_commands.describe(member="Member to clear warnings for")
    @mod_only()
    async def clearwarns(self, interaction: discord.Interaction, member: discord.Member):
        count = await db.clear_warns(interaction.guild.id, member.id)
        e = embeds.success(
            f"Cleared **{count}** warning(s) from {member.mention}.",
            title="🧹  Warnings Cleared",
        )
        await interaction.response.send_message(embed=e)
        await send_mod_log(interaction.guild, e)

    # ── /purge ────────────────────────────────────────────────────────────────
    @app_commands.command(name="purge", description="Delete messages in bulk (max 100)")
    @app_commands.describe(
        amount="Number of messages to delete (1-100)",
        member="Only delete messages from this member (optional)",
    )
    @mod_only()
    @app_commands.default_permissions(manage_messages=True)
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        member: discord.Member | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        def check(m: discord.Message) -> bool:
            return member is None or m.author == member

        deleted = await interaction.channel.purge(limit=amount, check=check)
        e = embeds.success(
            f"Deleted **{len(deleted)}** message(s)"
            + (f" from {member.mention}" if member else "") + ".",
            title="🗑️  Purge",
        )
        await interaction.followup.send(embed=e, ephemeral=True)

    # ── /slowmode ─────────────────────────────────────────────────────────────
    @app_commands.command(name="slowmode", description="Set slowmode for this channel (0 to disable)")
    @app_commands.describe(seconds="Slowmode duration in seconds (0-21600)")
    @mod_only()
    @app_commands.default_permissions(manage_channels=True)
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
    ):
        await interaction.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            e = embeds.success("Slowmode **disabled** for this channel.", title="⏱️  Slowmode")
        else:
            e = embeds.success(f"Slowmode set to **{seconds}s** for this channel.", title="⏱️  Slowmode")
        await interaction.response.send_message(embed=e)

    # ── /setmodlog ────────────────────────────────────────────────────────────
    @app_commands.command(name="setmodlog", description="Set the mod-log channel")
    @app_commands.describe(channel="Channel to send mod logs to")
    @mod_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setmodlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.update_guild_setting(interaction.guild.id, "mod_log_channel", str(channel.id))
        await interaction.response.send_message(
            embed=embeds.success(f"Mod log channel set to {channel.mention}."), ephemeral=True
        )

    # ── /setmuterole ──────────────────────────────────────────────────────────
    @app_commands.command(name="setmuterole", description="Set the mute role for this server")
    @app_commands.describe(role="Role to use as mute role")
    @mod_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setmuterole(self, interaction: discord.Interaction, role: discord.Role):
        await db.update_guild_setting(interaction.guild.id, "mute_role", str(role.id))
        await interaction.response.send_message(
            embed=embeds.success(f"Mute role set to {role.mention}."), ephemeral=True
        )

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        msg = str(error)
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error)
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "You don't have permission to use this command."
        elif isinstance(error, discord.Forbidden):
            msg = "I don't have permission to do that. Check my role hierarchy."

        if interaction.response.is_done():
            await interaction.followup.send(embed=embeds.error(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=embeds.error(msg), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
