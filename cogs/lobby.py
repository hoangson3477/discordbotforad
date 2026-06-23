from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database.supabase_client import db
from utils import embeds

log = logging.getLogger("lobby")

LOBBY_CATEGORY_NAME = "「🎮 𝙎ả𝙣𝙝 𝙜𝙖𝙢𝙚」"
MAX_GAMES_IN_DROPDOWN = 24          # Discord select limit is 25; reserve 1 slot for "Khác"
SIZE_OPTIONS = [2, 3, 4, 5, 6, 8, 10, 0]   # 0 == unlimited


# ── Helpers ───────────────────────────────────────────────────────────────────
 
async def _get_or_create_lobby_category(guild: discord.Guild) -> discord.CategoryChannel:
    """Find the fixed lobby category, or create it if missing."""
    category = discord.utils.get(guild.categories, name=LOBBY_CATEGORY_NAME)
    if category is None:
        category = await guild.create_category(LOBBY_CATEGORY_NAME, reason="Lobby system setup")
    return category
 
 
def _admin_or_mod():
    """Slash command check: requires Manage Guild or Administrator."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        perms = interaction.user.guild_permissions
        if perms.manage_guild or perms.administrator:
            return True
        raise app_commands.CheckFailure("You need **Manage Server** permission to use this command.")
    return app_commands.check(predicate)
 
 
def _can_close_lobby(member: discord.Member, owner_id: int) -> bool:
    """Owner OR anyone with Manage Channels / Administrator can close a lobby."""
    if member.id == owner_id:
        return True
    perms = member.guild_permissions
    return perms.manage_channels or perms.administrator
 
 
def _type_label(lobby_type: str) -> str:
    return {"voice": "Voice", "text": "Text", "both": "Voice + Text"}.get(lobby_type, lobby_type)
 
 
def _count_current_members(guild: discord.Guild, lobby: dict) -> int:
    """Count people currently sitting in the lobby's voice channel (if any)."""
    voice_id = lobby.get("voice_channel_id")
    if not voice_id:
        return 0
    channel = guild.get_channel(int(voice_id))
    if not isinstance(channel, discord.VoiceChannel):
        return 0
    return len([m for m in channel.members if not m.bot])
 
 
def _build_lobby_embed(guild: discord.Guild, lobby: dict, owner: discord.abc.User) -> discord.Embed:
    """Shared embed builder for both the in-lobby message and the announcement message."""
    max_users = lobby.get("max_users")
    current = _count_current_members(guild, lobby)
    size_label = f"{current}/{max_users}" if max_users else f"{current} (không giới hạn)"
    is_full = bool(max_users) and current >= max_users
 
    color = embeds.Color.ERROR if is_full else embeds.Color.SUCCESS
    title_suffix = "  🔴 ĐÃ ĐẦY" if is_full else ""
 
    e = discord.Embed(
        title=f"Lobby: {lobby['game_name']}{title_suffix}",
        description=f"Chủ phòng: <@{lobby['owner_id']}>",
        color=color,
    )
    e.add_field(name="Loại", value=_type_label(lobby["lobby_type"]) if lobby.get("lobby_type") else "—", inline=True)
    e.add_field(name="Số người", value=size_label, inline=True)
 
    voice_id = lobby.get("voice_channel_id")
    text_id = lobby.get("text_channel_id")
    if voice_id:
        e.add_field(name="Voice", value=f"<#{voice_id}>", inline=False)
    if text_id:
        e.add_field(name="Text", value=f"<#{text_id}>", inline=False)
 
    e.set_footer(text="Chủ phòng hoặc Admin/Mod có thể đóng lobby bằng nút bên dưới.")
    return e
 
 
async def _refresh_lobby_messages(bot: commands.Bot, guild: discord.Guild, lobby: dict) -> None:
    """
    Re-render the lobby embed both inside the lobby channel and in the
    announcement channel, so member counts stay accurate. Called from
    on_voice_state_update whenever someone joins/leaves a lobby voice channel.
    """
    owner = guild.get_member(int(lobby["owner_id"])) or discord.Object(id=int(lobby["owner_id"]))
    embed = _build_lobby_embed(guild, lobby, owner)
 
    # ── Update in-lobby message ───────────────────────────────────────────────
    in_lobby_msg_id = lobby.get("lobby_message_id")
    target_channel_id = lobby.get("text_channel_id") or lobby.get("voice_channel_id")
    if in_lobby_msg_id and target_channel_id:
        channel = guild.get_channel(int(target_channel_id))
        if channel:
            try:
                msg = await channel.fetch_message(int(in_lobby_msg_id))
                await msg.edit(embed=embed)
            except (discord.NotFound, discord.Forbidden):
                pass
 
    # ── Update announcement message ───────────────────────────────────────────
    ann_channel_id = lobby.get("announcement_channel_id")
    ann_message_id = lobby.get("announcement_message_id")
    if ann_channel_id and ann_message_id:
        ann_channel = guild.get_channel(int(ann_channel_id))
        if ann_channel:
            try:
                ann_msg = await ann_channel.fetch_message(int(ann_message_id))
                await ann_msg.edit(embed=embed)
            except (discord.NotFound, discord.Forbidden):
                pass
 
 
# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENT VIEW — the permanent "Create Lobby" panel
# ══════════════════════════════════════════════════════════════════════════════
 
class LobbyPanelView(discord.ui.View):
    """Posted once via /lobby setup. Survives bot restarts (timeout=None + custom_id)."""
 
    def __init__(self):
        super().__init__(timeout=None)
 
    @discord.ui.button(
        label="Tạo Lobby",
        emoji="🎮",
        style=discord.ButtonStyle.success,
        custom_id="lobby_panel:create",
    )
    async def create_lobby(self, interaction: discord.Interaction, button: discord.ui.Button):
        games = await db.list_lobby_games(interaction.guild.id)
        view = GameSelectView(games)
        await interaction.response.send_message(
            embed=embeds.info("Chọn game bạn muốn chơi:", title="🎮  Tạo Lobby — Bước 1/3"),
            view=view,
            ephemeral=True,
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — choose game
# ══════════════════════════════════════════════════════════════════════════════
 
class GameSelectView(discord.ui.View):
    def __init__(self, games: list[dict]):
        super().__init__(timeout=180)
        self.add_item(GameSelect(games))
 
 
class GameSelect(discord.ui.Select):
    def __init__(self, games: list[dict]):
        options = [
            discord.SelectOption(label=g["name"], emoji=g.get("emoji") or "🎮")
            for g in games[:MAX_GAMES_IN_DROPDOWN]
        ]
        options.append(discord.SelectOption(label="Khác (tự nhập)", value="__custom__", emoji="✏️"))
 
        super().__init__(
            placeholder="Chọn một game...",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="Khác (tự nhập)", value="__custom__", emoji="✏️")],
        )
 
    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
 
        if choice == "__custom__":
            await interaction.response.send_modal(CustomGameModal())
            return
 
        view = LobbyTypeSelectView(game_name=choice)
        await interaction.response.edit_message(
            embed=embeds.info(
                f"Game: **{choice}**\n\nChọn loại lobby bạn muốn tạo:",
                title="Tạo Lobby — Bước 2/3",
            ),
            view=view,
        )
 
 
class CustomGameModal(discord.ui.Modal, title="Nhập tên game"):
    game_name = discord.ui.TextInput(
        label="Tên game",
        placeholder="VD: Valorant, Liên Quân, Minecraft...",
        max_length=50,
        required=True,
    )
 
    async def on_submit(self, interaction: discord.Interaction):
        view = LobbyTypeSelectView(game_name=str(self.game_name))
        await interaction.response.send_message(
            embed=embeds.info(
                f"Game: **{self.game_name}**\n\nChọn loại lobby bạn muốn tạo:",
                title="Tạo Lobby — Bước 2/3",
            ),
            view=view,
            ephemeral=True,
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — choose lobby type (voice / text / both)
# ══════════════════════════════════════════════════════════════════════════════
 
class LobbyTypeSelectView(discord.ui.View):
    def __init__(self, game_name: str):
        super().__init__(timeout=180)
        self.game_name = game_name
        self.add_item(LobbyTypeSelect(game_name))
 
 
class LobbyTypeSelect(discord.ui.Select):
    def __init__(self, game_name: str):
        self.game_name = game_name
        options = [
            discord.SelectOption(label="Chỉ Voice", value="voice", emoji="🔊"),
            discord.SelectOption(label="Chỉ Text", value="text", emoji="💬"),
            discord.SelectOption(label="Cả Voice + Text", value="both", emoji="🎙️"),
        ]
        super().__init__(placeholder="Chọn loại lobby...", min_values=1, max_values=1, options=options)
 
    async def callback(self, interaction: discord.Interaction):
        lobby_type = self.values[0]
        view = SizeSelectView(game_name=self.game_name, lobby_type=lobby_type)
        await interaction.response.edit_message(
            embed=embeds.info(
                f"Game: **{self.game_name}**\nLoại: **{_type_label(lobby_type)}**\n\n"
                "Chọn số người tối đa:",
                title="Tạo Lobby — Bước 3/3",
            ),
            view=view,
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — choose max size, then CREATE the lobby
# ══════════════════════════════════════════════════════════════════════════════
 
class SizeSelectView(discord.ui.View):
    def __init__(self, game_name: str, lobby_type: str):
        super().__init__(timeout=180)
        self.add_item(SizeSelect(game_name, lobby_type))
 
 
class SizeSelect(discord.ui.Select):
    def __init__(self, game_name: str, lobby_type: str):
        self.game_name = game_name
        self.lobby_type = lobby_type
        options = [
            discord.SelectOption(label=f"{n} người", value=str(n)) if n else
            discord.SelectOption(label="Không giới hạn", value="0")
            for n in SIZE_OPTIONS
        ]
        super().__init__(placeholder="Chọn số người tối đa...", min_values=1, max_values=1, options=options)
 
    async def callback(self, interaction: discord.Interaction):
        max_users = int(self.values[0]) or None  # 0 -> None (unlimited)
        await interaction.response.defer(ephemeral=True)
        await _create_lobby(interaction, self.game_name, self.lobby_type, max_users)
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Lobby creation
# ══════════════════════════════════════════════════════════════════════════════
 
async def _create_lobby(
    interaction: discord.Interaction,
    game_name: str,
    lobby_type: str,
    max_users: int | None,
):
    guild = interaction.guild
    owner = interaction.user
    category = await _get_or_create_lobby_category(guild)
 
    if len(category.channels) >= 48:  # leave headroom under Discord's 50/category cap
        return await interaction.followup.send(
            embed=embeds.error("Category lobby đã đầy (quá nhiều lobby đang mở). Hãy thử lại sau."),
            ephemeral=True,
        )
 
    # Owner gets manage_channels on their own lobby so they can kick/rename/etc.
    owner_overwrite = discord.PermissionOverwrite(
        manage_channels=True,
        manage_permissions=False,
        move_members=True,
        mute_members=True,
        deafen_members=True,
    )
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True),
        owner: owner_overwrite,
    }
 
    voice_channel: Optional[discord.VoiceChannel] = None
    text_channel: Optional[discord.TextChannel] = None
 
    try:
        channel_name = f"{game_name}・{owner.display_name}"[:90]
 
        if lobby_type in ("voice", "both"):
            voice_channel = await guild.create_voice_channel(
                name=f"{channel_name}",
                category=category,
                user_limit=max_users or 0,
                overwrites=overwrites,
                reason=f"Lobby created by {owner}",
            )
 
        if lobby_type in ("text", "both"):
            text_channel = await guild.create_text_channel(
                name=f"{channel_name}",
                category=category,
                overwrites=overwrites,
                reason=f"Lobby created by {owner}",
            )
 
    except discord.Forbidden:
        return await interaction.followup.send(
            embed=embeds.error("Bot thiếu quyền tạo channel. Liên hệ admin."), ephemeral=True
        )
    except Exception as exc:
        log.exception("Lobby creation failed")
        return await interaction.followup.send(
            embed=embeds.error(f"Tạo lobby thất bại: {exc}"), ephemeral=True
        )
 
    lobby_id = await db.create_active_lobby(
        guild_id=guild.id,
        owner_id=owner.id,
        voice_channel_id=voice_channel.id if voice_channel else None,
        text_channel_id=text_channel.id if text_channel else None,
        game_name=game_name,
        max_users=max_users,
        lobby_type=lobby_type,
    )
 
    lobby_record = {
        "id": lobby_id,
        "guild_id": str(guild.id),
        "owner_id": str(owner.id),
        "voice_channel_id": str(voice_channel.id) if voice_channel else None,
        "text_channel_id": str(text_channel.id) if text_channel else None,
        "game_name": game_name,
        "max_users": max_users,
        "lobby_type": lobby_type,
    }
 
    info_embed = _build_lobby_embed(guild, lobby_record, owner)
    close_view = CloseLobbyView()
 
    # ── Post embed inside the lobby itself ────────────────────────────────────
    target_channel = text_channel or voice_channel
    lobby_message_id = None
    try:
        msg = await target_channel.send(embed=info_embed, view=close_view)
        lobby_message_id = msg.id
    except discord.Forbidden:
        pass
 
    # ── Post announcement so OTHER players can discover and join this lobby ───
    announcement_channel_id = None
    announcement_message_id = None
    panel = await db.get_lobby_panel(guild.id)
    if panel and panel.get("channel_id"):
        ann_channel = guild.get_channel(int(panel["channel_id"]))
        if ann_channel:
            try:
                join_view = JoinLobbyView(lobby_id=lobby_id)
                ann_msg = await ann_channel.send(embed=info_embed, view=join_view)
                announcement_channel_id = ann_channel.id
                announcement_message_id = ann_msg.id
            except discord.Forbidden:
                pass
 
    await db.update_active_lobby_messages(
        lobby_id=lobby_id,
        lobby_message_id=lobby_message_id,
        announcement_channel_id=announcement_channel_id,
        announcement_message_id=announcement_message_id,
    )
 
    await interaction.followup.send(
        embed=embeds.success(
            f"Lobby của bạn đã được tạo!\n"
            + (f"{voice_channel.mention}\n" if voice_channel else "")
            + (f"{text_channel.mention}\n" if text_channel else "")
        ),
        ephemeral=True,
    )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Join lobby button (posted in the announcement channel — discoverable by anyone)
# ══════════════════════════════════════════════════════════════════════════════
 
class JoinLobbyView(discord.ui.View):
    """
    NOT persistent (timeout=None but recreated per-message at creation time).
    Just points the clicker to the lobby's channels — does not move them
    automatically, since move_to() silently fails if they're not already in
    a voice channel and that's a confusing dead end.
    """
 
    def __init__(self, lobby_id: str):
        super().__init__(timeout=None)
        self.lobby_id = lobby_id
 
    @discord.ui.button(label="Đến Lobby", style=discord.ButtonStyle.primary)
    async def go_to_lobby(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobbies = await db.list_active_lobbies(interaction.guild.id)
        lobby = next((l for l in lobbies if l["id"] == self.lobby_id), None)
 
        if not lobby:
            return await interaction.response.send_message(
                embed=embeds.error("Lobby này không còn tồn tại (có thể đã bị đóng)."),
                ephemeral=True,
            )
 
        max_users = lobby.get("max_users")
        current = _count_current_members(interaction.guild, lobby)
        if max_users and current >= max_users:
            return await interaction.response.send_message(
                embed=embeds.warning("Lobby này đã đầy người rồi! Hãy thử lobby khác hoặc tạo lobby mới."),
                ephemeral=True,
            )
 
        lines = [f"Lobby **{lobby['game_name']}** đang ở đây, bấm vào để tham gia:"]
        if lobby.get("voice_channel_id"):
            lines.append(f"<#{lobby['voice_channel_id']}>")
        if lobby.get("text_channel_id"):
            lines.append(f"<#{lobby['text_channel_id']}>")
 
        await interaction.response.send_message(
            embed=embeds.info("\n".join(lines), title="🎮  Tham gia Lobby"),
            ephemeral=True,
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Close lobby button
# ══════════════════════════════════════════════════════════════════════════════
 
class CloseLobbyView(discord.ui.View):
    """
    Persistent view (fixed custom_id, timeout=None) — survives bot restarts
    because it's re-registered once via bot.add_view() in Lobby.cog_load().
    Looks up which lobby it belongs to by the channel the button was clicked
    in, rather than embedding the lobby_id in custom_id (which would require
    a different registered view per lobby — not how discord.py persistence works).
    """
 
    def __init__(self):
        super().__init__(timeout=None)
 
    @discord.ui.button(label="🔒 Đóng Lobby", style=discord.ButtonStyle.danger, custom_id="lobby_panel:close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await db.get_active_lobby_by_channel(interaction.channel.id)
 
        if not lobby:
            return await interaction.response.send_message(
                embed=embeds.error("Không tìm thấy thông tin lobby này (có thể đã bị đóng)."),
                ephemeral=True,
            )
 
        if not _can_close_lobby(interaction.user, int(lobby["owner_id"])):
            return await interaction.response.send_message(
                embed=embeds.error("Chỉ chủ phòng hoặc Admin/Mod mới có thể đóng lobby này."),
                ephemeral=True,
            )
 
        await interaction.response.send_message(
            embed=embeds.info("⏳ Đang đóng lobby..."), ephemeral=True
        )
 
        await db.delete_active_lobby(lobby["id"])
 
        for ch_id_key in ("voice_channel_id", "text_channel_id"):
            ch_id = lobby.get(ch_id_key)
            if ch_id:
                channel = interaction.guild.get_channel(int(ch_id))
                if channel:
                    try:
                        await channel.delete(reason=f"Lobby closed by {interaction.user}")
                    except discord.Forbidden:
                        pass
 
        # ── Clean up the announcement message so the browser channel stays tidy ──
        ann_channel_id = lobby.get("announcement_channel_id")
        ann_message_id = lobby.get("announcement_message_id")
        if ann_channel_id and ann_message_id:
            ann_channel = interaction.guild.get_channel(int(ann_channel_id))
            if ann_channel:
                try:
                    ann_msg = await ann_channel.fetch_message(int(ann_message_id))
                    await ann_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════════════════
 
class Lobby(commands.Cog):
    """Self-service voice/text lobby creation for gaming communities."""
 
    def __init__(self, bot: commands.Bot):
        self.bot = bot
 
    async def cog_load(self):
        # Re-register persistent views so existing buttons keep working after restart
        self.bot.add_view(LobbyPanelView())
        self.bot.add_view(CloseLobbyView())
 
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        """Refresh lobby embeds (member count) whenever someone joins/leaves a lobby voice channel."""
        if member.bot:
            return
 
        changed_channel_ids = {
            ch.id for ch in (before.channel, after.channel) if ch is not None
        }
        if not changed_channel_ids:
            return
 
        for ch_id in changed_channel_ids:
            lobby = await db.get_active_lobby_by_channel(ch_id)
            if lobby:
                await _refresh_lobby_messages(self.bot, member.guild, lobby)
 
    lobby_group = app_commands.Group(name="lobby", description="Lobby system commands")
 
    # ── /lobby setup ──────────────────────────────────────────────────────────
    @lobby_group.command(name="setup", description="Post the permanent 'Create Lobby' panel in this channel")
    @_admin_or_mod()
    async def lobby_setup(self, interaction: discord.Interaction):
        category = await _get_or_create_lobby_category(interaction.guild)
 
        embed = discord.Embed(
            title="🎮  Tạo Lobby chơi game",
            description=(
                "Bấm nút bên dưới để tạo phòng riêng (voice/text) cho game bạn muốn chơi.\n\n"
                "Bạn sẽ được chọn:\n"
                "- Game muốn chơi\n"
                "- Loại phòng (Voice / Text / Cả hai)\n"
                "- Số người tối đa"
            ),
            color=embeds.Color.INFO,
        )
        embed.set_footer(text="Lobby sẽ không tự xoá — chủ phòng hoặc Admin/Mod cần bấm Đóng Lobby.")
 
        view = LobbyPanelView()
        msg = await interaction.channel.send(embed=embed, view=view)
 
        await db.set_lobby_panel(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            message_id=msg.id,
            category_id=category.id,
        )
 
        await interaction.response.send_message(
            embed=embeds.success("Panel tạo lobby đã được đăng!"), ephemeral=True
        )
 
    # ── /lobby addgame ────────────────────────────────────────────────────────
    @lobby_group.command(name="addgame", description="Add a game to the lobby dropdown list")
    @app_commands.describe(name="Game name", emoji="Optional emoji to display")
    @_admin_or_mod()
    async def lobby_addgame(self, interaction: discord.Interaction, name: str, emoji: str = "🎮"):
        ok = await db.add_lobby_game(interaction.guild.id, name, emoji)
        if ok:
            await interaction.response.send_message(
                embed=embeds.success(f"Đã thêm **{name}** vào danh sách game."), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=embeds.error(f"**{name}** đã có trong danh sách hoặc xảy ra lỗi."), ephemeral=True
            )
 
    # ── /lobby removegame ─────────────────────────────────────────────────────
    @lobby_group.command(name="removegame", description="Remove a game from the lobby dropdown list")
    @app_commands.describe(name="Game name to remove")
    @_admin_or_mod()
    async def lobby_removegame(self, interaction: discord.Interaction, name: str):
        ok = await db.remove_lobby_game(interaction.guild.id, name)
        if ok:
            await interaction.response.send_message(
                embed=embeds.success(f"Đã xoá **{name}** khỏi danh sách game."), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=embeds.error(f"Không tìm thấy **{name}** trong danh sách."), ephemeral=True
            )
 
    # ── /lobby listgames ──────────────────────────────────────────────────────
    @lobby_group.command(name="listgames", description="View the configured game list")
    async def lobby_listgames(self, interaction: discord.Interaction):
        games = await db.list_lobby_games(interaction.guild.id)
        if not games:
            return await interaction.response.send_message(
                embed=embeds.info("Chưa có game nào trong danh sách. Dùng `/lobby addgame` để thêm."),
                ephemeral=True,
            )
        lines = [f"{g.get('emoji', '🎮')} {g['name']}" for g in games]
        await interaction.response.send_message(
            embed=embeds.info("\n".join(lines), title="🎮  Danh sách Game"), ephemeral=True
        )
 
    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        msg = str(error)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embeds.error(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=embeds.error(msg), ephemeral=True)
 
 
async def setup(bot: commands.Bot):
    await bot.add_cog(Lobby(bot))