from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database.supabase_client import db
from utils import embeds

log = logging.getLogger("lobby")

LOBBY_CATEGORY_NAME = "「🎮 𝙂𝙖𝙢𝙚 𝙃𝙖𝙡𝙡」"
MAX_GAMES_IN_DROPDOWN = 24          # Discord select limit is 25; reserve 1 slot for "Khác"
SIZE_OPTIONS = [2, 3, 4, 5, 6, 8, 10, 0]   # 0 == unlimited


# ── Slot helpers ──────────────────────────────────────────────────────────────
 
def _empty_slots(max_users: int | None) -> list:
    size = max_users or 5
    return [None] * size
 
 
def _slot_line(i: int, slot: dict | None) -> str:
    if slot is None:
        return f"`#{i+1}` │ Còn trống..."
    return f"`#{i+1}` │ ✅ <@{slot['user_id']}>"
 
 
def _find_empty_slot(slots: list) -> int | None:
    for i, s in enumerate(slots):
        if s is None:
            return i
    return None
 
 
def _user_in_slots(slots: list, user_id: int) -> bool:
    return any(s and int(s["user_id"]) == user_id for s in slots)
 
 
def _is_full(slots: list) -> bool:
    return bool(slots) and all(s is not None for s in slots)
 
 
# ── Embed builder ─────────────────────────────────────────────────────────────
 
def _type_label(lobby_type: str) -> str:
    return {
        "voice": "Kênh voice",
        "text":  "Kênh chat",
        "both":  "Cả voice + chat",
    }.get(lobby_type, lobby_type)
 
 
def _build_lobby_embed(guild: discord.Guild, lobby: dict) -> discord.Embed:
    slots: list = lobby.get("slots") or []
    is_full = _is_full(slots)
 
    color = embeds.Color.ERROR if is_full else embeds.Color.SUCCESS
    title = lobby["game_name"] + ("  🔴 ĐÃ ĐẦY" if is_full else "")
 
    e = discord.Embed(title=f"⭐  {title}", color=color)
    e.add_field(name="👤  Leader", value=f"<@{lobby['owner_id']}>", inline=True)
 
    if lobby.get("strategy"):
        e.add_field(name="🎮  Strategy", value=lobby["strategy"], inline=True)
    if lobby.get("lobby_type"):
        e.add_field(name="🔊  Loại", value=_type_label(lobby["lobby_type"]), inline=True)
 
    if slots:
        e.add_field(
            name="👥  Current Team",
            value="\n".join(_slot_line(i, s) for i, s in enumerate(slots)),
            inline=False,
        )
 
    if lobby.get("note"):
        e.add_field(name="💬  Note", value=lobby["note"], inline=False)
 
    links = []
    if lobby.get("voice_channel_id"):
        links.append(f"🔊 <#{lobby['voice_channel_id']}>")
    if lobby.get("text_channel_id"):
        links.append(f"💬 <#{lobby['text_channel_id']}>")
    if links:
        e.add_field(name="📍  Lobby", value="  ".join(links), inline=False)
 
    e.set_footer(text="Bấm Join để vào slot • Chủ phòng hoặc Mod có thể đóng lobby")
    return e
 
 
async def _refresh_lobby_embeds(guild: discord.Guild, lobby: dict) -> None:
    embed = _build_lobby_embed(guild, lobby)
 
    target_ch_id = lobby.get("text_channel_id") or lobby.get("voice_channel_id")
    if lobby.get("lobby_message_id") and target_ch_id:
        ch = guild.get_channel(int(target_ch_id))
        if ch:
            try:
                msg = await ch.fetch_message(int(lobby["lobby_message_id"]))
                await msg.edit(embed=embed)
            except (discord.NotFound, discord.Forbidden):
                pass
 
    if lobby.get("announcement_channel_id") and lobby.get("announcement_message_id"):
        ann_ch = guild.get_channel(int(lobby["announcement_channel_id"]))
        if ann_ch:
            try:
                ann_msg = await ann_ch.fetch_message(int(lobby["announcement_message_id"]))
                await ann_msg.edit(embed=embed)
            except (discord.NotFound, discord.Forbidden):
                pass
 
 
# ── Permission helpers ────────────────────────────────────────────────────────
 
def _admin_or_mod():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            raise app_commands.CheckFailure("Server only.")
        perms = interaction.user.guild_permissions
        if perms.manage_guild or perms.administrator:
            return True
        raise app_commands.CheckFailure("Bạn cần quyền **Manage Server**.")
    return app_commands.check(predicate)
 
 
def _can_close_lobby(member: discord.Member, owner_id: int) -> bool:
    if member.id == owner_id:
        return True
    return member.guild_permissions.manage_channels or member.guild_permissions.administrator
 
 
async def _get_or_create_lobby_category(guild: discord.Guild) -> discord.CategoryChannel:
    cat = discord.utils.get(guild.categories, name=LOBBY_CATEGORY_NAME)
    if cat is None:
        cat = await guild.create_category(LOBBY_CATEGORY_NAME, reason="Lobby system setup")
    return cat
 
 
# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENT VIEW — panel "Tạo Lobby"
# ══════════════════════════════════════════════════════════════════════════════
 
class LobbyPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
 
    @discord.ui.button(
        label="Tạo Lobby", emoji="🎮",
        style=discord.ButtonStyle.success,
        custom_id="lobby_panel:create",
    )
    async def create_lobby(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobbies = await db.list_active_lobbies(interaction.guild.id)
        existing = next((l for l in lobbies if str(l["owner_id"]) == str(interaction.user.id)), None)
        if existing:
            ch_id = existing.get("text_channel_id") or existing.get("voice_channel_id")
            return await interaction.response.send_message(
                embed=embeds.warning(
                    f"Bạn đang có lobby mở rồi!\n<#{ch_id}>\nĐóng lobby cũ trước khi tạo mới."
                ),
                ephemeral=True,
            )
 
        games = await db.list_lobby_games(interaction.guild.id)
        if not games:
            return await interaction.response.send_message(
                embed=embeds.warning("Chưa có game nào. Admin dùng `/lobby addgame` để thêm."),
                ephemeral=True,
            )
 
        await interaction.response.send_message(
            embed=embeds.info("Chọn game bạn muốn chơi:", title="🎮  Tạo Lobby — Bước 1/4"),
            view=GameSelectView(games),
            ephemeral=True,
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — chọn game
# ══════════════════════════════════════════════════════════════════════════════
 
class GameSelectView(discord.ui.View):
    def __init__(self, games: list[dict]):
        super().__init__(timeout=180)
        self.add_item(GameSelect(games))
 
 
class GameSelect(discord.ui.Select):
    def __init__(self, games: list[dict]):
        options = [
            discord.SelectOption(label=g["name"], value=g["name"], emoji=g.get("emoji") or "🎮")
            for g in games[:MAX_GAMES_IN_DROPDOWN]
        ]
        options.append(discord.SelectOption(label="Khác (tự nhập)", value="__custom__", emoji="✏️"))
        super().__init__(placeholder="Chọn một game...", min_values=1, max_values=1, options=options)
 
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__custom__":
            await interaction.response.send_modal(CustomGameModal())
            return
        await interaction.response.edit_message(
            embed=embeds.info(f"Game: **{self.values[0]}**\n\nChọn loại lobby:", title="🎮  Tạo Lobby — Bước 2/4"),
            view=LobbyTypeSelectView(game_name=self.values[0]),
        )
 
 
class CustomGameModal(discord.ui.Modal, title="Nhập tên game"):
    game_name = discord.ui.TextInput(label="Tên game", placeholder="VD: Valorant, Liên Quân...", max_length=50)
 
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=embeds.info(f"Game: **{self.game_name}**\n\nChọn loại lobby:", title="🎮  Tạo Lobby — Bước 2/4"),
            view=LobbyTypeSelectView(game_name=str(self.game_name)),
            ephemeral=True,
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — chọn loại lobby
# ══════════════════════════════════════════════════════════════════════════════
 
class LobbyTypeSelectView(discord.ui.View):
    def __init__(self, game_name: str):
        super().__init__(timeout=180)
        self.add_item(LobbyTypeSelect(game_name))
 
 
class LobbyTypeSelect(discord.ui.Select):
    def __init__(self, game_name: str):
        self.game_name = game_name
        options = [
            discord.SelectOption(label="Chỉ Voice",    value="voice", emoji="🔊"),
            discord.SelectOption(label="Chỉ Text",     value="text",  emoji="💬"),
            discord.SelectOption(label="Voice + Text", value="both",  emoji="🎙️"),
        ]
        super().__init__(placeholder="Chọn loại lobby...", min_values=1, max_values=1, options=options)
 
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=embeds.info(
                f"Game: **{self.game_name}**\nLoại: **{_type_label(self.values[0])}**\n\nChọn số người tối đa:",
                title="🎮  Tạo Lobby — Bước 3/4",
            ),
            view=SizeSelectView(game_name=self.game_name, lobby_type=self.values[0]),
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — chọn số người
# ══════════════════════════════════════════════════════════════════════════════
 
class SizeSelectView(discord.ui.View):
    def __init__(self, game_name: str, lobby_type: str):
        super().__init__(timeout=180)
        self.add_item(SizeSelect(game_name, lobby_type))
 
 
class SizeSelect(discord.ui.Select):
    def __init__(self, game_name: str, lobby_type: str):
        self.game_name  = game_name
        self.lobby_type = lobby_type
        options = [
            discord.SelectOption(label=f"{n} người", value=str(n)) if n else
            discord.SelectOption(label="Không giới hạn", value="0")
            for n in SIZE_OPTIONS
        ]
        super().__init__(placeholder="Chọn số người tối đa...", min_values=1, max_values=1, options=options)
 
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            LobbyDetailsModal(
                game_name=self.game_name,
                lobby_type=self.lobby_type,
                max_users=int(self.values[0]) or None,
            )
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — modal Strategy + Note
# ══════════════════════════════════════════════════════════════════════════════
 
class LobbyDetailsModal(discord.ui.Modal, title="Chi tiết Lobby"):
    strategy = discord.ui.TextInput(
        label="Strategy / Game mode",
        placeholder="VD: Ranked, Casual, Rush B, Stratless...",
        max_length=50, required=False,
    )
    note = discord.ui.TextInput(
        label="Note (yêu cầu thêm)",
        placeholder="VD: cần biết chơi Voidcore, có mic, rank Diamond+...",
        max_length=150, required=False,
    )
 
    def __init__(self, game_name: str, lobby_type: str, max_users: int | None):
        super().__init__()
        self.game_name  = game_name
        self.lobby_type = lobby_type
        self.max_users  = max_users
 
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await _create_lobby(
            interaction=interaction,
            game_name=self.game_name,
            lobby_type=self.lobby_type,
            max_users=self.max_users,
            strategy=str(self.strategy) or None,
            note=str(self.note) or None,
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Lobby creation
# ══════════════════════════════════════════════════════════════════════════════
 
async def _create_lobby(
    interaction: discord.Interaction,
    game_name: str,
    lobby_type: str,
    max_users: int | None,
    strategy: str | None,
    note: str | None,
):
    guild = interaction.guild
    owner = interaction.user
    category = await _get_or_create_lobby_category(guild)
 
    if len(category.channels) >= 48:
        return await interaction.followup.send(
            embed=embeds.error("Category lobby đã đầy. Hãy thử lại sau."), ephemeral=True
        )
 
    owner_ow = discord.PermissionOverwrite(
        manage_channels=True, manage_permissions=False,
        move_members=True, mute_members=True, deafen_members=True,
    )
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True),
        owner: owner_ow,
    }
 
    voice_channel: Optional[discord.VoiceChannel] = None
    text_channel:  Optional[discord.TextChannel]  = None
    channel_name = f"{game_name}・{owner.display_name}"[:90]
 
    try:
        if lobby_type in ("voice", "both"):
            voice_channel = await guild.create_voice_channel(
                name=channel_name, category=category,
                user_limit=max_users or 0, overwrites=overwrites,
                reason=f"Lobby by {owner}",
            )
        if lobby_type in ("text", "both"):
            text_channel = await guild.create_text_channel(
                name=channel_name, category=category,
                overwrites=overwrites, reason=f"Lobby by {owner}",
            )
    except discord.Forbidden:
        return await interaction.followup.send(
            embed=embeds.error("Bot thiếu quyền tạo channel."), ephemeral=True
        )
    except Exception as exc:
        log.exception("Lobby creation failed")
        return await interaction.followup.send(
            embed=embeds.error(f"Tạo lobby thất bại: {exc}"), ephemeral=True
        )
 
    # Slots — owner chiếm slot 0
    slots = _empty_slots(max_users)
    slots[0] = {"user_id": str(owner.id), "username": owner.display_name}
 
    lobby_id = await db.create_active_lobby(
        guild_id=guild.id, owner_id=owner.id,
        voice_channel_id=voice_channel.id if voice_channel else None,
        text_channel_id=text_channel.id if text_channel else None,
        game_name=game_name, max_users=max_users,
        lobby_type=lobby_type, strategy=strategy, note=note, slots=slots,
    )
 
    lobby_record = {
        "id": lobby_id, "guild_id": str(guild.id), "owner_id": str(owner.id),
        "voice_channel_id": str(voice_channel.id) if voice_channel else None,
        "text_channel_id":  str(text_channel.id)  if text_channel  else None,
        "game_name": game_name, "max_users": max_users, "lobby_type": lobby_type,
        "strategy": strategy, "note": note, "slots": slots,
    }
 
    embed      = _build_lobby_embed(guild, lobby_record)
    close_view = CloseLobbyView()
 
    # Post inside lobby
    target_channel = text_channel or voice_channel
    lobby_message_id = None
    try:
        msg = await target_channel.send(embed=embed, view=close_view)
        lobby_message_id = msg.id
    except discord.Forbidden:
        pass
 
    # Post in party-pings with @role ping
    announcement_channel_id = None
    announcement_message_id = None
    panel = await db.get_lobby_panel(guild.id)
    ann_ch_id = panel.get("announcement_channel_id") if panel else None
 
    if ann_ch_id:
        ann_ch = guild.get_channel(int(ann_ch_id))
        if ann_ch:
            try:
                game_info    = await db.get_lobby_game(guild.id, game_name)
                role_id      = game_info.get("role_id") if game_info else None
                ping_content = f"<@&{role_id}>" if role_id else None
                join_view    = JoinLobbyView(lobby_id=lobby_id)
                ann_msg      = await ann_ch.send(content=ping_content, embed=embed, view=join_view)
                announcement_channel_id = ann_ch.id
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
            "Lobby của bạn đã được tạo!\n"
            + (f"🔊 {voice_channel.mention}\n" if voice_channel else "")
            + (f"💬 {text_channel.mention}\n"  if text_channel  else "")
        ),
        ephemeral=True,
    )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Join / Jump buttons (announcement embed)
# ══════════════════════════════════════════════════════════════════════════════
 
class JoinLobbyView(discord.ui.View):
    def __init__(self, lobby_id: str):
        super().__init__(timeout=None)
        self.lobby_id = lobby_id
 
    @discord.ui.button(label="Join", emoji="💬", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobbies = await db.list_active_lobbies(interaction.guild.id)
        lobby   = next((l for l in lobbies if l["id"] == self.lobby_id), None)
 
        if not lobby:
            return await interaction.response.send_message(
                embed=embeds.error("Lobby này không còn tồn tại."), ephemeral=True
            )
 
        slots = lobby.get("slots") or []
 
        if _user_in_slots(slots, interaction.user.id):
            lines = ["Bạn đã có trong lobby này rồi!"]
            if lobby.get("voice_channel_id"): lines.append(f"🔊 <#{lobby['voice_channel_id']}>")
            if lobby.get("text_channel_id"):  lines.append(f"💬 <#{lobby['text_channel_id']}>")
            return await interaction.response.send_message(
                embed=embeds.info("\n".join(lines), title="🎮  Tham gia Lobby"), ephemeral=True
            )
 
        if _is_full(slots):
            return await interaction.response.send_message(
                embed=embeds.warning("Lobby này đã đầy rồi!"), ephemeral=True
            )
 
        idx = _find_empty_slot(slots)
        if idx is None:
            return await interaction.response.send_message(
                embed=embeds.warning("Lobby này đã đầy rồi!"), ephemeral=True
            )
 
        slots[idx] = {"user_id": str(interaction.user.id), "username": interaction.user.display_name}
        await db.update_lobby_slots(self.lobby_id, slots)
        lobby["slots"] = slots
        await _refresh_lobby_embeds(interaction.guild, lobby)
 
        # Ping owner if full
        if _is_full(slots):
            try:
                owner_member = interaction.guild.get_member(int(lobby["owner_id"]))
                if owner_member:
                    await owner_member.send(
                        embed=embeds.success(
                            f"Lobby **{lobby['game_name']}** của bạn đã đủ người!",
                            title="🎮  Lobby Đầy",
                        )
                    )
            except (discord.Forbidden, discord.HTTPException):
                pass
 
        lines = [f"Bạn đã vào slot `#{idx+1}` thành công!"]
        if lobby.get("voice_channel_id"): lines.append(f"🔊 <#{lobby['voice_channel_id']}>")
        if lobby.get("text_channel_id"):  lines.append(f"💬 <#{lobby['text_channel_id']}>")
        await interaction.response.send_message(
            embed=embeds.success("\n".join(lines), title="🎮  Đã tham gia Lobby"), ephemeral=True
        )
 
    @discord.ui.button(label="Jump to Lobby", emoji="🔗", style=discord.ButtonStyle.secondary)
    async def jump(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobbies = await db.list_active_lobbies(interaction.guild.id)
        lobby   = next((l for l in lobbies if l["id"] == self.lobby_id), None)
        if not lobby:
            return await interaction.response.send_message(
                embed=embeds.error("Lobby này không còn tồn tại."), ephemeral=True
            )
        lines = [f"Lobby **{lobby['game_name']}** đang ở đây:"]
        if lobby.get("voice_channel_id"): lines.append(f"🔊 <#{lobby['voice_channel_id']}>")
        if lobby.get("text_channel_id"):  lines.append(f"💬 <#{lobby['text_channel_id']}>")
        await interaction.response.send_message(
            embed=embeds.info("\n".join(lines), title="🔗  Jump to Lobby"), ephemeral=True
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Close lobby button (inside lobby channel) — PERSISTENT
# ══════════════════════════════════════════════════════════════════════════════
 
class CloseLobbyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
 
    @discord.ui.button(
        label="Đóng Lobby", emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="lobby_panel:close",
    )
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await db.get_active_lobby_by_channel(interaction.channel.id)
        if not lobby:
            return await interaction.response.send_message(
                embed=embeds.error("Không tìm thấy lobby (có thể đã đóng rồi)."), ephemeral=True
            )
        if not _can_close_lobby(interaction.user, int(lobby["owner_id"])):
            return await interaction.response.send_message(
                embed=embeds.error("Chỉ chủ phòng hoặc Admin/Mod mới được đóng lobby này."), ephemeral=True
            )
 
        await interaction.response.send_message(embed=embeds.info("⏳ Đang đóng lobby..."), ephemeral=True)
        await db.delete_active_lobby(lobby["id"])
 
        for key in ("voice_channel_id", "text_channel_id"):
            ch_id = lobby.get(key)
            if ch_id:
                ch = interaction.guild.get_channel(int(ch_id))
                if ch:
                    try: await ch.delete(reason=f"Lobby closed by {interaction.user}")
                    except discord.Forbidden: pass
 
        if lobby.get("announcement_channel_id") and lobby.get("announcement_message_id"):
            ann_ch = interaction.guild.get_channel(int(lobby["announcement_channel_id"]))
            if ann_ch:
                try:
                    ann_msg = await ann_ch.fetch_message(int(lobby["announcement_message_id"]))
                    await ann_msg.delete()
                except (discord.NotFound, discord.Forbidden): pass
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Gamemode roles panel — PERSISTENT
# ══════════════════════════════════════════════════════════════════════════════
 
class GamemodeRolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
 
    @discord.ui.select(
        placeholder="Chọn game để nhận/bỏ role ping...",
        min_values=0, max_values=25,
        options=[discord.SelectOption(label="Loading...", value="__loading__")],
        custom_id="lobby_panel:roles",
    )
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if "__loading__" in select.values or "__empty__" in select.values:
            return await interaction.response.send_message(
                embed=embeds.warning("Chưa có game nào được cấu hình."), ephemeral=True
            )
 
        games    = await db.list_lobby_games(interaction.guild.id)
        selected = set(select.values)
        added, removed = [], []
 
        for game in games:
            role_id = game.get("role_id")
            if not role_id:
                continue
            role     = interaction.guild.get_role(int(role_id))
            if not role:
                continue
            has_role = role in interaction.user.roles
            wants_it = game["name"] in selected
 
            if wants_it and not has_role:
                try:
                    await interaction.user.add_roles(role, reason="Lobby role self-assign")
                    added.append(role.mention)
                except discord.Forbidden: pass
            elif not wants_it and has_role:
                try:
                    await interaction.user.remove_roles(role, reason="Lobby role self-remove")
                    removed.append(role.mention)
                except discord.Forbidden: pass
 
        lines = []
        if added:   lines.append(f"✅ Đã thêm: {', '.join(added)}")
        if removed: lines.append(f"🗑️ Đã bỏ: {', '.join(removed)}")
        if not lines: lines.append("Không có thay đổi nào.")
 
        await interaction.response.send_message(
            embed=embeds.success("\n".join(lines), title="🎮  Roles đã cập nhật"), ephemeral=True
        )
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════════════════
 
class Lobby(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
 
    async def cog_load(self):
        self.bot.add_view(LobbyPanelView())
        self.bot.add_view(CloseLobbyView())
        self.bot.add_view(GamemodeRolesView())
 
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot: return
        changed = {ch.id for ch in (before.channel, after.channel) if ch}
        for ch_id in changed:
            lobby = await db.get_active_lobby_by_channel(ch_id)
            if lobby:
                await _refresh_lobby_embeds(member.guild, lobby)
 
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        lobby = await db.get_active_lobby_by_channel(channel.id)
        if not lobby: return
        log.info(f"Channel {channel.id} deleted manually — cleaning lobby {lobby['id']}")
        await db.delete_active_lobby(lobby["id"])
        if lobby.get("announcement_channel_id") and lobby.get("announcement_message_id"):
            ann_ch = channel.guild.get_channel(int(lobby["announcement_channel_id"]))
            if ann_ch:
                try:
                    msg = await ann_ch.fetch_message(int(lobby["announcement_message_id"]))
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden): pass
 
    lobby_group = app_commands.Group(name="lobby", description="Lobby system commands")
 
    @lobby_group.command(name="setup", description="Cấu hình hệ thống lobby (3 kênh)")
    @app_commands.describe(
        create_channel="Kênh panel Tạo Lobby (#create-party)",
        announcement_channel="Kênh thông báo lobby mới (#party-pings)",
        roles_channel="Kênh chọn role game (#gamemode-roles)",
    )
    @_admin_or_mod()
    async def lobby_setup(
        self, interaction: discord.Interaction,
        create_channel: discord.TextChannel,
        announcement_channel: discord.TextChannel,
        roles_channel: discord.TextChannel,
    ):
        await interaction.response.defer(ephemeral=True)
        category = await _get_or_create_lobby_category(interaction.guild)
 
        # Panel in create-party
        panel_embed = discord.Embed(
            title="🎮  Tạo Lobby Chơi Game",
            description=(
                "Bấm **Tạo Lobby** để mở phòng riêng cho game bạn muốn chơi.\n\n"
                "Bạn sẽ chọn:\n"
                "🎯 Game  ·  🔊 Loại phòng  ·  👥 Số người  ·  📝 Strategy & Note"
            ),
            color=embeds.Color.INFO,
        )
        panel_embed.set_footer(text="Mỗi user chỉ mở được 1 lobby tại 1 thời điểm.")
        panel_msg = await create_channel.send(embed=panel_embed, view=LobbyPanelView())
 
        # Roles panel in gamemode-roles
        games = await db.list_lobby_games(interaction.guild.id)
        options = [
            discord.SelectOption(label=g["name"], value=g["name"], emoji=g.get("emoji") or "🎮")
            for g in games
        ] or [discord.SelectOption(label="Chưa có game — dùng /lobby addgame", value="__empty__")]
 
        roles_view = GamemodeRolesView()
        roles_view.role_select.options    = options
        roles_view.role_select.max_values = max(1, len(options))
 
        roles_embed = discord.Embed(
            title="🎮  Chọn Role Ping Game",
            description=(
                "Chọn game bên dưới để **nhận role ping** tương ứng.\n"
                "Khi có lobby mới, bot sẽ ping role đó trong party-pings.\n\n"
                "Chọn lại lần nữa để **bỏ** role."
            ),
            color=embeds.Color.INFO,
        )
        roles_msg = await roles_channel.send(embed=roles_embed, view=roles_view)
 
        await db.set_lobby_panel(
            guild_id=interaction.guild.id,
            channel_id=create_channel.id,
            message_id=panel_msg.id,
            category_id=category.id,
            announcement_channel_id=announcement_channel.id,
            roles_channel_id=roles_channel.id,
            roles_message_id=roles_msg.id,
        )
 
        await interaction.followup.send(
            embed=embeds.success(
                f"✅ Panel tạo lobby → {create_channel.mention}\n"
                f"📢 Announcement → {announcement_channel.mention}\n"
                f"🎭 Roles → {roles_channel.mention}",
                title="⚙️  Lobby Setup hoàn tất",
            ),
            ephemeral=True,
        )
 
    @lobby_group.command(name="addgame", description="Thêm game + tạo role ping")
    @app_commands.describe(name="Tên game", emoji="Emoji (mặc định 🎮)")
    @_admin_or_mod()
    async def lobby_addgame(self, interaction: discord.Interaction, name: str, emoji: str = "🎮"):
        await interaction.response.defer(ephemeral=True)
 
        role = discord.utils.get(interaction.guild.roles, name=name)
        if not role:
            try:
                role = await interaction.guild.create_role(name=name, mentionable=True, reason=f"Lobby game: {name}")
            except discord.Forbidden:
                return await interaction.followup.send(embed=embeds.error("Bot thiếu quyền tạo role."), ephemeral=True)
 
        ok = await db.add_lobby_game(interaction.guild.id, name, emoji, role_id=role.id)
        if not ok:
            return await interaction.followup.send(embed=embeds.error(f"**{name}** đã có trong danh sách rồi."), ephemeral=True)
 
        await self._refresh_roles_panel(interaction.guild)
        await interaction.followup.send(
            embed=embeds.success(f"Đã thêm **{name}** với role {role.mention}.", title="✅  Game added"),
            ephemeral=True,
        )
 
    @lobby_group.command(name="removegame", description="Xoá game + xoá role ping")
    @app_commands.describe(name="Tên game cần xoá")
    @_admin_or_mod()
    async def lobby_removegame(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        game = await db.get_lobby_game(interaction.guild.id, name)
        if not game:
            return await interaction.followup.send(embed=embeds.error(f"Không tìm thấy **{name}**."), ephemeral=True)
 
        role_id = game.get("role_id")
        if role_id:
            role = interaction.guild.get_role(int(role_id))
            if role:
                try: await role.delete(reason=f"Lobby game removed: {name}")
                except discord.Forbidden: pass
 
        await db.remove_lobby_game(interaction.guild.id, name)
        await self._refresh_roles_panel(interaction.guild)
        await interaction.followup.send(embed=embeds.success(f"Đã xoá **{name}** và role tương ứng."), ephemeral=True)
 
    @lobby_group.command(name="listgames", description="Xem danh sách game đã cấu hình")
    async def lobby_listgames(self, interaction: discord.Interaction):
        games = await db.list_lobby_games(interaction.guild.id)
        if not games:
            return await interaction.response.send_message(
                embed=embeds.info("Chưa có game nào. Dùng `/lobby addgame` để thêm."), ephemeral=True
            )
        lines = [
            f"{g.get('emoji','🎮')} **{g['name']}**" + (f" → <@&{g['role_id']}>" if g.get("role_id") else "")
            for g in games
        ]
        await interaction.response.send_message(
            embed=embeds.info("\n".join(lines), title="🎮  Danh sách Game"), ephemeral=True
        )
 
    @lobby_group.command(name="list", description="Xem tất cả lobby đang mở")
    async def lobby_list(self, interaction: discord.Interaction):
        lobbies = await db.list_active_lobbies(interaction.guild.id)
        if not lobbies:
            return await interaction.response.send_message(
                embed=embeds.info("Hiện không có lobby nào đang mở."), ephemeral=True
            )
        lines = []
        for l in lobbies:
            slots  = l.get("slots") or []
            filled = sum(1 for s in slots if s)
            total  = len(slots) or "?"
            ch_id  = l.get("text_channel_id") or l.get("voice_channel_id")
            lines.append(f"🎮 **{l['game_name']}** — <@{l['owner_id']}>\n  👥 {filled}/{total}  ·  <#{ch_id}>")
        await interaction.response.send_message(
            embed=embeds.info("\n\n".join(lines), title=f"🎮  Lobbies đang mở ({len(lobbies)})"),
            ephemeral=True,
        )
 
    async def _refresh_roles_panel(self, guild: discord.Guild) -> None:
        panel = await db.get_lobby_panel(guild.id)
        if not panel: return
        roles_ch_id  = panel.get("roles_channel_id")
        roles_msg_id = panel.get("roles_message_id")
        if not roles_ch_id or not roles_msg_id: return
        ch = guild.get_channel(int(roles_ch_id))
        if not ch: return
 
        games = await db.list_lobby_games(guild.id)
        options = [
            discord.SelectOption(label=g["name"], value=g["name"], emoji=g.get("emoji") or "🎮")
            for g in games
        ] or [discord.SelectOption(label="Chưa có game — dùng /lobby addgame", value="__empty__")]
        new_view = GamemodeRolesView()
        new_view.role_select.options    = options
        new_view.role_select.max_values = max(1, len(options))
        try:
            msg = await ch.fetch_message(int(roles_msg_id))
            await msg.edit(view=new_view)
        except (discord.NotFound, discord.Forbidden): pass
 
    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        msg = str(error)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embeds.error(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=embeds.error(msg), ephemeral=True)
 
 
async def setup(bot: commands.Bot):
    await bot.add_cog(Lobby(bot))