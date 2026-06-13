from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.supabase_client import db
from utils import embeds

log = logging.getLogger("backup")

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_BACKUPS_PER_GUILD = 10          # maximum stored backups per server
AUTO_BACKUP_INTERVAL_HOURS = 24     # auto-backup every N hours


# ── Helpers ───────────────────────────────────────────────────────────────────

def _serialize_permissions(perms: discord.Permissions) -> int:
    """Convert Permissions object to integer value."""
    return perms.value


def _serialize_overwrites(overwrites: dict) -> list[dict]:
    """Serialize permission overwrites to a JSON-safe list."""
    result = []
    for target, overwrite in overwrites.items():
        allow, deny = overwrite.pair()
        result.append({
            "id": str(target.id),
            "type": "role" if isinstance(target, discord.Role) else "member",
            "allow": allow.value,
            "deny": deny.value,
        })
    return result


async def _collect_backup_data(guild: discord.Guild) -> dict:
    """
    Gather all backup-able data from a guild and return as a dict.
    This is the core snapshot function.
    """
    # ── Roles ────────────────────────────────────────────────────────────────
    roles_data = []
    for role in sorted(guild.roles, key=lambda r: r.position):
        if role.is_default():
            continue  # skip @everyone
        roles_data.append({
            "id": str(role.id),
            "name": role.name,
            "color": role.color.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "permissions": _serialize_permissions(role.permissions),
            "position": role.position,
            "managed": role.managed,  # bot-managed roles can't be restored
        })

    # ── Categories & Channels ────────────────────────────────────────────────
    categories_data = []
    for cat in guild.categories:
        categories_data.append({
            "id": str(cat.id),
            "name": cat.name,
            "position": cat.position,
            "overwrites": _serialize_overwrites(cat.overwrites),
            "channels": [],
        })

    # map category_id → index for quick lookup
    cat_index = {c["id"]: i for i, c in enumerate(categories_data)}

    channels_no_cat = []
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue  # already handled above

        ch_data: dict = {
            "id": str(ch.id),
            "name": ch.name,
            "type": str(ch.type),
            "position": ch.position,
            "overwrites": _serialize_overwrites(ch.overwrites),
            "category_id": str(ch.category_id) if ch.category_id else None,
        }

        if isinstance(ch, discord.TextChannel):
            ch_data.update({
                "topic": ch.topic,
                "slowmode_delay": ch.slowmode_delay,
                "nsfw": ch.is_nsfw(),
                "default_auto_archive_duration": ch.default_auto_archive_duration,
            })
        elif isinstance(ch, discord.VoiceChannel):
            ch_data.update({
                "bitrate": ch.bitrate,
                "user_limit": ch.user_limit,
            })
        elif isinstance(ch, discord.ForumChannel):
            ch_data.update({
                "topic": ch.topic,
                "slowmode_delay": ch.slowmode_delay,
                "nsfw": ch.is_nsfw(),
            })

        if ch.category_id and str(ch.category_id) in cat_index:
            categories_data[cat_index[str(ch.category_id)]]["channels"].append(ch_data)
        else:
            channels_no_cat.append(ch_data)

    # ── Emojis ───────────────────────────────────────────────────────────────
    emojis_data = []
    for emoji in guild.emojis:
        emojis_data.append({
            "id": str(emoji.id),
            "name": emoji.name,
            "animated": emoji.animated,
            "url": str(emoji.url),
            "roles": [str(r.id) for r in emoji.roles],  # role-restricted emojis
        })

    # ── Stickers ─────────────────────────────────────────────────────────────
    stickers_data = []
    for sticker in guild.stickers:
        stickers_data.append({
            "id": str(sticker.id),
            "name": sticker.name,
            "description": sticker.description,
            "emoji": sticker.emoji,
            "url": str(sticker.url),
            "format": str(sticker.format),
        })

    # ── Members (nickname + roles snapshot) ──────────────────────────────────
    members_data = []
    await guild.chunk()  # ensure member cache is full
    for member in guild.members:
        if member.bot:
            continue
        member_roles = [str(r.id) for r in member.roles if not r.is_default()]
        members_data.append({
            "id": str(member.id),
            "name": str(member),
            "nickname": member.nick,
            "roles": member_roles,
        })

    # ── Bans ─────────────────────────────────────────────────────────────────
    bans_data = []
    try:
        async for ban_entry in guild.bans():
            bans_data.append({
                "user_id": str(ban_entry.user.id),
                "user_name": str(ban_entry.user),
                "reason": ban_entry.reason,
            })
    except discord.Forbidden:
        log.warning(f"No permission to fetch bans for guild {guild.id}")

    # ── Guild-level settings ──────────────────────────────────────────────────
    guild_data = {
        "id": str(guild.id),
        "name": guild.name,
        "description": guild.description,
        "afk_timeout": guild.afk_timeout,
        "verification_level": str(guild.verification_level),
        "default_notifications": str(guild.default_notifications),
        "explicit_content_filter": str(guild.explicit_content_filter),
        "icon_url": str(guild.icon.url) if guild.icon else None,
    }

    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "guild": guild_data,
        "roles": roles_data,
        "categories": categories_data,
        "channels_no_category": channels_no_cat,
        "emojis": emojis_data,
        "stickers": stickers_data,
        "members": members_data,
        "bans": bans_data,
    }


# ── Restore logic ─────────────────────────────────────────────────────────────

async def _restore_to_guild(
    guild: discord.Guild,
    data: dict,
    progress_callback,   # async callable(str) — for live status updates
) -> dict:
    """
    Restore a backup snapshot to a guild.
    Returns a summary dict of what succeeded/failed.
    """
    summary = {"roles": 0, "categories": 0, "channels": 0,
                "emojis": 0, "stickers": 0, "bans": 0, "errors": []}

    old_role_id_to_new: dict[str, discord.Role] = {}

    # ── Step 1: Roles ────────────────────────────────────────────────────────
    await progress_callback("🎭 Restoring roles...")
    for role_data in sorted(data.get("roles", []), key=lambda r: r["position"]):
        if role_data.get("managed"):
            continue  # bot-managed roles are skipped
        try:
            new_role = await guild.create_role(
                name=role_data["name"],
                color=discord.Color(role_data["color"]),
                hoist=role_data["hoist"],
                mentionable=role_data["mentionable"],
                permissions=discord.Permissions(role_data["permissions"]),
                reason="Server restore",
            )
            old_role_id_to_new[role_data["id"]] = new_role
            summary["roles"] += 1
            await asyncio.sleep(0.5)  # rate-limit buffer
        except Exception as e:
            summary["errors"].append(f"Role '{role_data['name']}': {e}")

    def _build_overwrites(raw_overwrites: list[dict]) -> dict:
        overwrites = {}
        for ow in raw_overwrites:
            allow = discord.Permissions(ow["allow"])
            deny  = discord.Permissions(ow["deny"])
            overwrite = discord.PermissionOverwrite.from_pair(allow, deny)
            if ow["type"] == "role":
                role = old_role_id_to_new.get(ow["id"]) or guild.get_role(int(ow["id"]))
                if role:
                    overwrites[role] = overwrite
        return overwrites

    # ── Step 2: Categories ───────────────────────────────────────────────────
    await progress_callback("📁 Restoring categories...")
    old_cat_id_to_new: dict[str, discord.CategoryChannel] = {}
    for cat_data in sorted(data.get("categories", []), key=lambda c: c["position"]):
        try:
            new_cat = await guild.create_category(
                name=cat_data["name"],
                overwrites=_build_overwrites(cat_data.get("overwrites", [])),
                reason="Server restore",
            )
            old_cat_id_to_new[cat_data["id"]] = new_cat
            summary["categories"] += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            summary["errors"].append(f"Category '{cat_data['name']}': {e}")

    # ── Step 3: Channels ─────────────────────────────────────────────────────
    await progress_callback("💬 Restoring channels...")

    all_channels = []
    for cat_data in data.get("categories", []):
        for ch in cat_data.get("channels", []):
            ch["_parent_old_id"] = cat_data["id"]
            all_channels.append(ch)
    for ch in data.get("channels_no_category", []):
        ch["_parent_old_id"] = None
        all_channels.append(ch)

    for ch_data in sorted(all_channels, key=lambda c: c["position"]):
        try:
            parent = old_cat_id_to_new.get(ch_data.get("_parent_old_id") or "") if ch_data.get("_parent_old_id") else None
            overwrites = _build_overwrites(ch_data.get("overwrites", []))
            ch_type = ch_data["type"]

            if ch_type == "text":
                await guild.create_text_channel(
                    name=ch_data["name"],
                    category=parent,
                    topic=ch_data.get("topic"),
                    slowmode_delay=ch_data.get("slowmode_delay", 0),
                    nsfw=ch_data.get("nsfw", False),
                    overwrites=overwrites,
                    reason="Server restore",
                )
            elif ch_type == "voice":
                await guild.create_voice_channel(
                    name=ch_data["name"],
                    category=parent,
                    bitrate=min(ch_data.get("bitrate", 64000), guild.bitrate_limit),
                    user_limit=ch_data.get("user_limit", 0),
                    overwrites=overwrites,
                    reason="Server restore",
                )
            elif ch_type == "forum":
                await guild.create_forum(
                    name=ch_data["name"],
                    category=parent,
                    topic=ch_data.get("topic"),
                    overwrites=overwrites,
                    reason="Server restore",
                )
            summary["channels"] += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            summary["errors"].append(f"Channel '{ch_data['name']}': {e}")

    # ── Step 4: Emojis ───────────────────────────────────────────────────────
    await progress_callback("😀 Restoring emojis...")
    async with aiohttp.ClientSession() as session:
        for emoji_data in data.get("emojis", []):
            try:
                async with session.get(emoji_data["url"]) as resp:
                    if resp.status == 200:
                        image_bytes = await resp.read()
                        roles = [
                            r for old_id in emoji_data.get("roles", [])
                            if (r := old_role_id_to_new.get(old_id))
                        ]
                        await guild.create_custom_emoji(
                            name=emoji_data["name"],
                            image=image_bytes,
                            roles=roles or [],
                            reason="Server restore",
                        )
                        summary["emojis"] += 1
                        await asyncio.sleep(1)
            except Exception as e:
                summary["errors"].append(f"Emoji '{emoji_data['name']}': {e}")

        # ── Step 5: Stickers ─────────────────────────────────────────────────
        await progress_callback("🏷️ Restoring stickers...")
        for sticker_data in data.get("stickers", []):
            try:
                async with session.get(sticker_data["url"]) as resp:
                    if resp.status == 200:
                        image_bytes = await resp.read()
                        fmt = sticker_data.get("format", "png").lower()
                        file = discord.File(
                            io.BytesIO(image_bytes),
                            filename=f"{sticker_data['name']}.{fmt}",
                        )
                        await guild.create_sticker(
                            name=sticker_data["name"],
                            description=sticker_data.get("description") or sticker_data["name"],
                            emoji=sticker_data.get("emoji", "⭐"),
                            file=file,
                            reason="Server restore",
                        )
                        summary["stickers"] += 1
                        await asyncio.sleep(1)
            except Exception as e:
                summary["errors"].append(f"Sticker '{sticker_data['name']}': {e}")

    # ── Step 6: Bans ─────────────────────────────────────────────────────────
    await progress_callback("🔨 Restoring bans...")
    for ban_data in data.get("bans", []):
        try:
            user = discord.Object(id=int(ban_data["user_id"]))
            await guild.ban(user, reason=f"[Restored] {ban_data.get('reason') or 'No reason'}")
            summary["bans"] += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            summary["errors"].append(f"Ban '{ban_data['user_name']}': {e}")

    return summary


# ── Cog ───────────────────────────────────────────────────────────────────────

class Backup(commands.Cog):
    """
    Server backup & restore system.
    Backs up: roles, channels, categories, emojis, stickers, members, bans.
    Stores in: Supabase + exportable JSON file.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._auto_backup_guilds: set[int] = set()
        self.auto_backup_task.start()

    def cog_unload(self):
        self.auto_backup_task.cancel()

    # ── Permission check ──────────────────────────────────────────────────────
    @staticmethod
    def _admin_only():
        async def predicate(interaction: discord.Interaction) -> bool:
            if not interaction.guild:
                raise app_commands.CheckFailure("Server only.")
            if interaction.user.guild_permissions.administrator:
                return True
            raise app_commands.CheckFailure("You need **Administrator** permission.")
        return app_commands.check(predicate)

    # ── Command group ─────────────────────────────────────────────────────────
    backup_group = app_commands.Group(
        name="backup",
        description="Server backup & restore commands",
    )

    # ── /backup create ────────────────────────────────────────────────────────
    @backup_group.command(name="create", description="Create a new backup of this server")
    @app_commands.describe(label="Optional label/note for this backup")
    @_admin_only()
    async def backup_create(
        self,
        interaction: discord.Interaction,
        label: str = "",
    ):
        await interaction.response.defer(ephemeral=False)

        # Enforce max backup limit
        existing = await db.list_backups(interaction.guild.id)
        if len(existing) >= MAX_BACKUPS_PER_GUILD:
            oldest = existing[-1]  # sorted newest-first by DB
            await db.delete_backup(oldest["id"])
            await interaction.followup.send(
                embed=embeds.warning(
                    f"Max backup limit ({MAX_BACKUPS_PER_GUILD}) reached. "
                    f"Oldest backup `{oldest['id'][:8]}` was deleted automatically."
                ),
                ephemeral=True,
            )

        e_progress = embeds.info("⏳ Collecting server data…", title="🗄️ Creating Backup")
        msg = await interaction.followup.send(embed=e_progress)

        try:
            data = await _collect_backup_data(interaction.guild)
        except Exception as exc:
            log.exception("Backup collection failed")
            return await msg.edit(embed=embeds.error(f"Backup failed: {exc}"))

        backup_id = await db.save_backup(
            guild_id=interaction.guild.id,
            guild_name=interaction.guild.name,
            label=label or f"Backup {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
            data=data,
        )

        summary_lines = [
            f"🎭 Roles: **{len(data['roles'])}**",
            f"📁 Categories: **{len(data['categories'])}**",
            f"💬 Channels: **{sum(len(c['channels']) for c in data['categories']) + len(data['channels_no_category'])}**",
            f"😀 Emojis: **{len(data['emojis'])}**",
            f"🏷️ Stickers: **{len(data['stickers'])}**",
            f"👥 Members: **{len(data['members'])}**",
            f"🔨 Bans: **{len(data['bans'])}**",
        ]

        e = embeds.success("\n".join(summary_lines), title="✅ Backup Created")
        e.add_field(name="Backup ID", value=f"`{backup_id}`", inline=True)
        e.add_field(name="Label", value=label or "—", inline=True)
        e.set_footer(text=f"Use /backup restore {backup_id} to restore")
        await msg.edit(embed=e)

    # ── /backup list ──────────────────────────────────────────────────────────
    @backup_group.command(name="list", description="List all backups for this server")
    @_admin_only()
    async def backup_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        backups = await db.list_backups(interaction.guild.id)
        if not backups:
            return await interaction.followup.send(
                embed=embeds.info("No backups found. Use `/backup create` to make one.", title="🗄️ Backups"),
                ephemeral=True,
            )

        lines = []
        for i, b in enumerate(backups, 1):
            created = b.get("created_at", "")[:16].replace("T", " ")
            auto_tag = " `[auto]`" if b.get("auto") else ""
            lines.append(f"`{i}.` **{b['label']}**{auto_tag}\nID: `{b['id'][:8]}…` · {created} UTC")

        pages = embeds.paginate(lines, title=f"🗄️  Backups — {interaction.guild.name}", per_page=5)
        await interaction.followup.send(embed=pages[0], ephemeral=True)

    # ── /backup info ──────────────────────────────────────────────────────────
    @backup_group.command(name="info", description="Show details of a specific backup")
    @app_commands.describe(backup_id="First 8+ characters of the backup ID")
    @_admin_only()
    async def backup_info(self, interaction: discord.Interaction, backup_id: str):
        await interaction.response.defer(ephemeral=True)

        backup = await db.get_backup(interaction.guild.id, backup_id)
        if not backup:
            return await interaction.followup.send(
                embed=embeds.error("Backup not found."), ephemeral=True
            )

        data = backup["data"]
        created = backup.get("created_at", "")[:19].replace("T", " ")

        e = discord.Embed(
            title=f"🗄️  {backup['label']}",
            color=embeds.Color.INFO,
        )
        e.add_field(name="Backup ID",   value=f"`{backup['id']}`",        inline=False)
        e.add_field(name="Created",     value=f"`{created} UTC`",          inline=True)
        e.add_field(name="Server",      value=data["guild"]["name"],        inline=True)
        e.add_field(name="🎭 Roles",    value=str(len(data["roles"])),      inline=True)
        e.add_field(name="📁 Categories", value=str(len(data["categories"])), inline=True)

        total_channels = sum(len(c["channels"]) for c in data["categories"]) + len(data["channels_no_category"])
        e.add_field(name="💬 Channels", value=str(total_channels),          inline=True)
        e.add_field(name="😀 Emojis",   value=str(len(data["emojis"])),     inline=True)
        e.add_field(name="🏷️ Stickers", value=str(len(data["stickers"])),   inline=True)
        e.add_field(name="👥 Members",  value=str(len(data["members"])),     inline=True)
        e.add_field(name="🔨 Bans",     value=str(len(data["bans"])),        inline=True)

        await interaction.followup.send(embed=e, ephemeral=True)

    # ── /backup export ────────────────────────────────────────────────────────
    @backup_group.command(name="export", description="Export a backup as JSON file (sent to your DM)")
    @app_commands.describe(backup_id="First 8+ characters of the backup ID")
    @_admin_only()
    async def backup_export(self, interaction: discord.Interaction, backup_id: str):
        await interaction.response.defer(ephemeral=True)

        backup = await db.get_backup(interaction.guild.id, backup_id)
        if not backup:
            return await interaction.followup.send(
                embed=embeds.error("Backup not found."), ephemeral=True
            )

        json_bytes = json.dumps(backup["data"], indent=2, ensure_ascii=False).encode("utf-8")
        file = discord.File(
            io.BytesIO(json_bytes),
            filename=f"backup_{interaction.guild.id}_{backup_id[:8]}.json",
        )

        try:
            await interaction.user.send(
                content=f"📦 Backup export for **{interaction.guild.name}** (`{backup['label']}`):",
                file=file,
            )
            await interaction.followup.send(
                embed=embeds.success("Backup file sent to your DMs!"), ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=embeds.error("I couldn't DM you. Please enable DMs from server members."),
                ephemeral=True,
            )

    # ── /backup delete ────────────────────────────────────────────────────────
    @backup_group.command(name="delete", description="Delete a backup permanently")
    @app_commands.describe(backup_id="First 8+ characters of the backup ID")
    @_admin_only()
    async def backup_delete(self, interaction: discord.Interaction, backup_id: str):
        backup = await db.get_backup(interaction.guild.id, backup_id)
        if not backup:
            return await interaction.response.send_message(
                embed=embeds.error("Backup not found."), ephemeral=True
            )

        # Confirm via view
        view = _ConfirmView(interaction.user)
        await interaction.response.send_message(
            embed=embeds.warning(
                f"Are you sure you want to delete backup **{backup['label']}** (`{backup_id[:8]}`)?\n"
                "This action **cannot be undone**."
            ),
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if view.confirmed:
            await db.delete_backup(backup["id"])
            await interaction.edit_original_response(
                embed=embeds.success(f"Backup `{backup_id[:8]}` deleted."), view=None
            )
        else:
            await interaction.edit_original_response(
                embed=embeds.info("Deletion cancelled."), view=None
            )

    # ── /backup restore ───────────────────────────────────────────────────────
    @backup_group.command(
        name="restore",
        description="Restore a backup to this server or another server"
    )
    @app_commands.describe(
        backup_id="First 8+ characters of the backup ID",
        target_guild_id="Target server ID (leave empty = current server)",
        wipe_first="Delete existing roles/channels before restoring (default: False)",
    )
    @_admin_only()
    async def backup_restore(
        self,
        interaction: discord.Interaction,
        backup_id: str,
        target_guild_id: str = "",
        wipe_first: bool = False,
    ):
        backup = await db.get_backup(interaction.guild.id, backup_id)
        if not backup:
            return await interaction.response.send_message(
                embed=embeds.error("Backup not found."), ephemeral=True
            )

        # Resolve target guild
        if target_guild_id:
            target_guild = self.bot.get_guild(int(target_guild_id))
            if not target_guild:
                return await interaction.response.send_message(
                    embed=embeds.error("Target server not found. Make sure the bot is in that server."),
                    ephemeral=True,
                )
        else:
            target_guild = interaction.guild

        # Confirm
        view = _ConfirmView(interaction.user)
        warn_msg = (
            f"⚠️ You are about to restore backup **{backup['label']}** to **{target_guild.name}**.\n\n"
        )
        if wipe_first:
            warn_msg += "🔴 **WIPE MODE ON** — All existing roles and channels will be deleted first!\n\n"
        warn_msg += "This may take a few minutes. Continue?"

        await interaction.response.send_message(
            embed=embeds.warning(warn_msg), view=view, ephemeral=True
        )
        await view.wait()

        if not view.confirmed:
            return await interaction.edit_original_response(
                embed=embeds.info("Restore cancelled."), view=None
            )

        await interaction.edit_original_response(
            embed=embeds.info("⏳ Starting restore… this may take a few minutes.", title="🔄 Restoring"),
            view=None,
        )

        # Optional wipe
        if wipe_first:
            await _wipe_guild(target_guild)

        # Progress updates via followup edits
        status_messages = []

        async def progress(msg: str):
            status_messages.append(msg)
            e = embeds.info("\n".join(status_messages[-5:]), title="🔄 Restoring…")
            try:
                await interaction.edit_original_response(embed=e)
            except Exception:
                pass

        summary = await _restore_to_guild(target_guild, backup["data"], progress)

        # Build result embed
        result_lines = [
            f"🎭 Roles restored: **{summary['roles']}**",
            f"📁 Categories restored: **{summary['categories']}**",
            f"💬 Channels restored: **{summary['channels']}**",
            f"😀 Emojis restored: **{summary['emojis']}**",
            f"🏷️ Stickers restored: **{summary['stickers']}**",
            f"🔨 Bans restored: **{summary['bans']}**",
        ]
        if summary["errors"]:
            result_lines.append(f"\n⚠️ Errors ({len(summary['errors'])}):")
            result_lines.extend(f"  • {err}" for err in summary["errors"][:5])
            if len(summary["errors"]) > 5:
                result_lines.append(f"  … and {len(summary['errors']) - 5} more")

        e_done = embeds.success("\n".join(result_lines), title="✅ Restore Complete")
        e_done.add_field(name="Target Server", value=target_guild.name, inline=True)
        await interaction.edit_original_response(embed=e_done)

    # ── /backup auto ──────────────────────────────────────────────────────────
    @backup_group.command(name="auto", description="Toggle automatic daily backups for this server")
    @_admin_only()
    async def backup_auto(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        settings = await db.get_guild_settings(guild_id)
        current = settings.get("auto_backup", False)
        new_value = not current

        await db.update_guild_setting(guild_id, "auto_backup", new_value)

        if new_value:
            self._auto_backup_guilds.add(guild_id)
            msg = f"✅ Auto-backup **enabled**. The server will be backed up every {AUTO_BACKUP_INTERVAL_HOURS}h."
        else:
            self._auto_backup_guilds.discard(guild_id)
            msg = "⛔ Auto-backup **disabled**."

        await interaction.response.send_message(embed=embeds.info(msg, title="⚙️ Auto Backup"), ephemeral=True)

    # ── Auto-backup task loop ─────────────────────────────────────────────────
    @tasks.loop(hours=AUTO_BACKUP_INTERVAL_HOURS)
    async def auto_backup_task(self):
        """Runs every N hours; backs up any guild with auto_backup=True."""
        log.info("Running scheduled auto-backup check…")
        for guild in self.bot.guilds:
            try:
                settings = await db.get_guild_settings(guild.id)
                if not settings.get("auto_backup"):
                    continue

                log.info(f"Auto-backup: {guild.name} ({guild.id})")
                data = await _collect_backup_data(guild)

                # Enforce limit — remove oldest if needed
                existing = await db.list_backups(guild.id)
                auto_backups = [b for b in existing if b.get("auto")]
                if len(existing) >= MAX_BACKUPS_PER_GUILD:
                    await db.delete_backup(existing[-1]["id"])

                await db.save_backup(
                    guild_id=guild.id,
                    guild_name=guild.name,
                    label=f"Auto {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
                    data=data,
                    auto=True,
                )
            except Exception:
                log.exception(f"Auto-backup failed for guild {guild.id}")

    @auto_backup_task.before_loop
    async def before_auto_backup(self):
        await self.bot.wait_until_ready()

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        msg = str(error)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embeds.error(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=embeds.error(msg), ephemeral=True)


# ── Wipe helper ───────────────────────────────────────────────────────────────

async def _wipe_guild(guild: discord.Guild) -> None:
    """Delete all non-default roles and channels (use with extreme care)."""
    for channel in guild.channels:
        try:
            await channel.delete(reason="Server restore wipe")
            await asyncio.sleep(0.3)
        except Exception:
            pass

    for role in guild.roles:
        if role.is_default() or role.managed:
            continue
        try:
            await role.delete(reason="Server restore wipe")
            await asyncio.sleep(0.3)
        except Exception:
            pass


# ── Confirm View ──────────────────────────────────────────────────────────────

class _ConfirmView(discord.ui.View):
    def __init__(self, author: discord.Member):
        super().__init__(timeout=60)
        self.author = author
        self.confirmed: bool = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Not your button.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(Backup(bot))