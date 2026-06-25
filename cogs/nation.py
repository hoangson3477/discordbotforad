from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.supabase_client import db
from utils import embeds

log = logging.getLogger("nation")

# ── Constants ─────────────────────────────────────────────────────────────────

TICK_HOURS       = 6
ATTACK_COOLDOWN  = timedelta(hours=2)
NEWBIE_PROTECT   = timedelta(hours=48)
MAX_BUILDING_LVL = 5

BUILDING_META = {
    "bank":          {"emoji": "🏦", "name": "Ngân hàng",          "produces": "money",    "base_prod": 200, "base_cost": {"money": 500,  "material": 200},             "base_time": 15},
    "oil":           {"emoji": "⛽", "name": "Giếng dầu",           "produces": "energy",   "base_prod": 150, "base_cost": {"money": 300,  "material": 400},             "base_time": 20},
    "factory":       {"emoji": "🏭", "name": "Nhà máy thép",        "produces": "material", "base_prod": 150, "base_cost": {"money": 400,  "energy": 200},               "base_time": 25},
    "farm":          {"emoji": "🌾", "name": "Nông nghiệp",         "produces": "food",     "base_prod": 200, "base_cost": {"money": 200,  "material": 100},             "base_time": 10},
    "parliament":    {"emoji": "🏛️", "name": "Nghị viện",          "produces": None,                         "base_cost": {"money": 800,  "material": 500},             "base_time": 45},
    "warehouse":     {"emoji": "🏗️", "name": "Kho chứa",           "produces": None,                         "base_cost": {"money": 300,  "material": 300},             "base_time": 15},
    "military_base": {"emoji": "⚔️", "name": "Căn cứ quân sự",     "produces": None,                         "base_cost": {"money": 600,  "material": 400, "energy": 300}, "base_time": 30},
    "defense":       {"emoji": "🛡️", "name": "Hệ thống phòng thủ", "produces": None,                         "base_cost": {"money": 700,  "material": 500, "energy": 400}, "base_time": 40},
}

UNIT_META = {
    "infantry": {"emoji": "🪖", "name": "Bộ binh",   "atk": 10, "def": 8,  "cost": {"money": 50,  "food": 20},             "upkeep": {"food": 2}},
    "tank":     {"emoji": "🚂", "name": "Xe tăng",   "atk": 40, "def": 35, "cost": {"money": 300, "material": 150, "energy": 50},  "upkeep": {"food": 5, "energy": 3}},
    "navy":     {"emoji": "🚢", "name": "Hải quân",  "atk": 30, "def": 45, "cost": {"money": 500, "material": 300, "energy": 100}, "upkeep": {"food": 8, "energy": 5}},
    "airforce": {"emoji": "✈️", "name": "Không quân","atk": 60, "def": 20, "cost": {"money": 800, "material": 200, "energy": 200}, "upkeep": {"food": 10,"energy": 10}},
}

RESOURCE_EMOJI = {"money": "💵", "energy": "⚡", "material": "🪨", "food": "🌾"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _buildings_dict(rows: list[dict]) -> dict[str, dict]:
    """Convert list of building rows to {type: row} dict."""
    return {r["type"]: r for r in rows}


def _calc_production(buildings: dict[str, dict]) -> dict[str, int]:
    """Calculate resource production per tick from buildings."""
    prod = {"money": 0, "energy": 0, "material": 0, "food": 0}
    for btype, meta in BUILDING_META.items():
        if not meta.get("produces"):
            continue
        level = buildings.get(btype, {}).get("level", 0)
        if level > 0:
            prod[meta["produces"]] += meta["base_prod"] * level
    return prod


def _calc_upkeep(nation: dict) -> dict[str, int]:
    """Calculate resource consumption per tick from army + population."""
    upkeep = {"money": 0, "energy": 0, "material": 0, "food": 0}
    for unit, meta in UNIT_META.items():
        count = nation.get(f"army_{unit}", 0)
        for res, cost in meta["upkeep"].items():
            upkeep[res] += count * cost
    # Population food consumption
    upkeep["food"] += nation.get("population", 0) // 100
    return upkeep


def _calc_defense_bonus(buildings: dict[str, dict]) -> int:
    defense_lvl = buildings.get("defense", {}).get("level", 0)
    castle_lvl  = buildings.get("military_base", {}).get("level", 0)
    return defense_lvl * 50 + castle_lvl * 20


def _calc_power_score(nation: dict, buildings: dict[str, dict]) -> int:
    score = 0
    score += nation.get("population", 0) * 0.1
    score += nation.get("money", 0) * 0.05
    score += nation.get("material", 0) * 0.1
    score += nation.get("energy", 0) * 0.1
    score += nation.get("food", 0) * 0.05
    score += nation.get("army_infantry", 0) * 10
    score += nation.get("army_tank", 0) * 40
    score += nation.get("army_navy", 0) * 35
    score += nation.get("army_airforce", 0) * 60
    total_levels = sum(b.get("level", 0) for b in buildings.values())
    score += total_levels * 150
    return int(score)


def _upgrade_cost(btype: str, next_level: int) -> dict[str, int]:
    base = BUILDING_META[btype]["base_cost"]
    multiplier = next_level * 1.8
    return {k: int(v * multiplier) for k, v in base.items()}


def _upgrade_time_minutes(btype: str, next_level: int) -> int:
    return int(BUILDING_META[btype]["base_time"] * (next_level ** 2))


async def _apply_finished_buildings(user_id: int, guild_id: int) -> list[str]:
    """Check and apply any buildings that finished upgrading. Returns list of finished names."""
    rows = await db.get_buildings(user_id, guild_id)
    now = datetime.now(timezone.utc)
    finished = []
    for row in rows:
        if row.get("is_upgrading") and row.get("finish_at"):
            finish_at = datetime.fromisoformat(row["finish_at"].replace("Z", "+00:00"))
            if finish_at <= now:
                new_level = row["level"] + 1
                await db.finish_upgrade(user_id, guild_id, row["type"], new_level)
                meta = BUILDING_META.get(row["type"], {})
                finished.append(f"{meta.get('emoji','🏗️')} **{meta.get('name', row['type'])}** → Level {new_level}")
    return finished


def _nation_embed(nation: dict, buildings: dict[str, dict], title: str | None = None) -> discord.Embed:
    prod   = _calc_production(buildings)
    upkeep = _calc_upkeep(nation)
    score  = _calc_power_score(nation, buildings)

    e = discord.Embed(
        title=title or f"🌍  {nation['name']}",
        color=embeds.Color.INFO,
    )
    e.add_field(
        name="👥 Dân số",
        value=f"`{nation['population']:,}`",
        inline=True,
    )
    e.add_field(
        name="⚡ Power Score",
        value=f"`{score:,}`",
        inline=True,
    )
    e.add_field(name="\u200b", value="\u200b", inline=True)

    # Resources
    res_lines = []
    for r in ("money", "energy", "material", "food"):
        val  = nation.get(r, 0)
        net  = prod.get(r, 0) - upkeep.get(r, 0)
        sign = "+" if net >= 0 else ""
        res_lines.append(f"{RESOURCE_EMOJI[r]} **{r.capitalize()}**: `{val:,}` ({sign}{net}/tick)")
    e.add_field(name="💰 Tài nguyên", value="\n".join(res_lines), inline=False)

    # Army
    army_lines = []
    for unit, meta in UNIT_META.items():
        count = nation.get(f"army_{unit}", 0)
        army_lines.append(f"{meta['emoji']} {meta['name']}: `{count:,}`")
    e.add_field(name="⚔️ Quân đội", value="\n".join(army_lines), inline=True)

    # Buildings
    b_lines = []
    for btype, meta in BUILDING_META.items():
        lvl = buildings.get(btype, {}).get("level", 0)
        upgrading = buildings.get(btype, {}).get("is_upgrading", False)
        suffix = " ⏳" if upgrading else ""
        b_lines.append(f"{meta['emoji']} {meta['name']}: `Lv{lvl}`{suffix}")
    e.add_field(name="🏗️ Công trình", value="\n".join(b_lines), inline=True)

    # Cooldown
    cooldown = nation.get("attack_cooldown")
    if cooldown:
        cd_dt = datetime.fromisoformat(cooldown.replace("Z", "+00:00"))
        if cd_dt > datetime.now(timezone.utc):
            e.set_footer(text=f"⚔️ Cooldown tấn công: {discord.utils.format_dt(cd_dt, 'R')}")

    return e


# ══════════════════════════════════════════════════════════════════════════════
# Diplomacy Views
# ══════════════════════════════════════════════════════════════════════════════

class DiplomacyView(discord.ui.View):
    """Accept/Decline view sent via DM for alliance/peace proposals."""

    def __init__(self, diplo_id: int, proposer_id: int):
        super().__init__(timeout=3600)  # 1 hour to respond
        self.diplo_id    = diplo_id
        self.proposer_id = proposer_id

    @discord.ui.button(label="✅ Chấp nhận", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        diplo = await db.get_diplomacy(self.diplo_id)
        if not diplo or diplo["status"] != "pending":
            return await interaction.response.send_message("Đề nghị này không còn hiệu lực.", ephemeral=True)

        await db.update_diplomacy_status(self.diplo_id, "accepted")
        self.stop()

        dtype = diplo["type"]
        label = "liên minh" if dtype == "alliance" else "hoà bình"

        await interaction.response.edit_message(
            embed=embeds.success(f"Bạn đã chấp nhận đề nghị **{label}**!"),
            view=None,
        )

        # Notify proposer
        proposer = interaction.client.get_user(int(diplo["from_user"]))
        if proposer:
            try:
                await proposer.send(
                    embed=embeds.success(
                        f"<@{diplo['to_user']}> đã chấp nhận đề nghị **{label}** của bạn!",
                        title="🤝  Ngoại giao",
                    )
                )
            except discord.Forbidden:
                pass

    @discord.ui.button(label="❌ Từ chối", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        diplo = await db.get_diplomacy(self.diplo_id)
        if not diplo or diplo["status"] != "pending":
            return await interaction.response.send_message("Đề nghị này không còn hiệu lực.", ephemeral=True)

        await db.update_diplomacy_status(self.diplo_id, "rejected")
        self.stop()

        await interaction.response.edit_message(
            embed=embeds.error("Bạn đã từ chối đề nghị."),
            view=None,
        )

        proposer = interaction.client.get_user(int(diplo["from_user"]))
        if proposer:
            try:
                await proposer.send(
                    embed=embeds.warning(
                        f"<@{diplo['to_user']}> đã từ chối đề nghị của bạn.",
                        title="❌  Ngoại giao",
                    )
                )
            except discord.Forbidden:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════════════════

class Nation(commands.Cog):
    """Modern nation-building game — per Discord server."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.resource_tick.start()

    def cog_unload(self):
        self.resource_tick.cancel()

    # ── Background tick ───────────────────────────────────────────────────────

    @tasks.loop(hours=TICK_HOURS)
    async def resource_tick(self):
        log.info("Nation resource tick running...")
        import random

        for guild in self.bot.guilds:
            nations = await db.list_nations(guild.id)
            if not nations:
                continue

            for nation in nations:
                uid  = int(nation["user_id"])
                gid  = int(nation["guild_id"])

                buildings = _buildings_dict(await db.get_buildings(uid, gid))
                prod      = _calc_production(buildings)
                upkeep    = _calc_upkeep(nation)

                new_money    = nation["money"]    + prod["money"]    - upkeep["money"]
                new_energy   = nation["energy"]   + prod["energy"]   - upkeep["energy"]
                new_material = nation["material"] + prod["material"] - upkeep["material"]
                new_food     = nation["food"]     + prod["food"]     - upkeep["food"]

                # Population tick
                pop = nation["population"]
                tax_income = int(pop * 0.5)
                new_money += tax_income

                if new_food >= 0:
                    new_pop = int(pop * 1.02)
                else:
                    new_pop = int(pop * 0.95)
                    # Starving army: reduce units proportionally
                    for unit in ("infantry", "tank", "navy", "airforce"):
                        current = nation.get(f"army_{unit}", 0)
                        if current > 0:
                            await db.update_nation(uid, gid, **{
                                f"army_{unit}": max(0, int(current * 0.9))
                            })

                # Clamp negatives to 0
                new_money    = max(0, new_money)
                new_energy   = max(0, new_energy)
                new_material = max(0, new_material)
                new_food     = max(0, new_food)
                new_pop      = max(0, new_pop)

                # Check extinction
                if new_pop <= 0:
                    user = self.bot.get_user(uid)
                    if user:
                        try:
                            await user.send(
                                embed=embeds.error(
                                    f"Quốc gia **{nation['name']}** của bạn đã bị diệt vong do nạn đói!\n"
                                    "Dùng `/nation create` để lập quốc gia mới.",
                                    title="💀  Diệt vong",
                                )
                            )
                        except discord.Forbidden:
                            pass
                    await db.delete_nation(uid, gid)
                    continue

                await db.update_nation(uid, gid,
                    money=new_money,
                    energy=new_energy,
                    material=new_material,
                    food=new_food,
                    population=new_pop,
                )

            log.info(f"Ticked {len(nations)} nations in {guild.name}")

    @resource_tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()

    # ── Shared logic ──────────────────────────────────────────────────────────

    async def _get_nation_or_error(
        self, ctx_or_interaction, user_id: int | None = None
    ) -> tuple[dict | None, dict | None]:
        """
        Fetch nation + buildings for a user.
        Returns (None, None) and sends error if not found.
        Works for both Interaction and Context.
        """
        is_interaction = isinstance(ctx_or_interaction, discord.Interaction)
        guild_id = ctx_or_interaction.guild.id
        if user_id is None:
            user_id = ctx_or_interaction.user.id if is_interaction else ctx_or_interaction.author.id

        nation = await db.get_nation(user_id, guild_id)
        if not nation:
            msg = "Quốc gia không tồn tại. Dùng `/nation create` hoặc `!ncreate` để lập quốc gia."
            err = embeds.error(msg)
            if is_interaction:
                if ctx_or_interaction.response.is_done():
                    await ctx_or_interaction.followup.send(embed=err, ephemeral=True)
                else:
                    await ctx_or_interaction.response.send_message(embed=err, ephemeral=True)
            else:
                await ctx_or_interaction.send(embed=err)
            return None, None

        buildings = _buildings_dict(await db.get_buildings(user_id, guild_id))
        return nation, buildings

    async def _send(self, target, **kwargs):
        """Send to either Interaction or Context."""
        if isinstance(target, discord.Interaction):
            if target.response.is_done():
                await target.followup.send(**kwargs)
            else:
                await target.response.send_message(**kwargs)
        else:
            await target.send(**kwargs)

    # ══════════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════════

    nation_group = app_commands.Group(name="nation", description="Nation building game")

    # /nation create
    @nation_group.command(name="create", description="Lập quốc gia của bạn")
    @app_commands.describe(name="Tên quốc gia (tối đa 32 ký tự)")
    async def slash_create(self, interaction: discord.Interaction, name: str):
        await self._cmd_create(interaction, name)

    # /nation info
    @nation_group.command(name="info", description="Xem thông tin quốc gia")
    @app_commands.describe(user="Xem quốc gia của user khác (bỏ trống = bản thân)")
    async def slash_info(self, interaction: discord.Interaction, user: discord.Member | None = None):
        await self._cmd_info(interaction, user)

    # /nation resources
    @nation_group.command(name="resources", description="Xem tài nguyên + tốc độ sản xuất")
    async def slash_resources(self, interaction: discord.Interaction):
        await self._cmd_resources(interaction)

    # /nation build
    @nation_group.command(name="build", description="Xây / nâng cấp công trình")
    @app_commands.describe(building="Loại công trình")
    @app_commands.choices(building=[
        app_commands.Choice(name=f"{m['emoji']} {m['name']}", value=k)
        for k, m in BUILDING_META.items()
    ])
    async def slash_build(self, interaction: discord.Interaction, building: str):
        await self._cmd_build(interaction, building)

    # /nation buildstatus
    @nation_group.command(name="buildstatus", description="Xem tiến độ xây dựng")
    async def slash_buildstatus(self, interaction: discord.Interaction):
        await self._cmd_buildstatus(interaction)

    # /nation recruit
    @nation_group.command(name="recruit", description="Tuyển quân")
    @app_commands.describe(unit="Loại quân", amount="Số lượng")
    @app_commands.choices(unit=[
        app_commands.Choice(name=f"{m['emoji']} {m['name']}", value=k)
        for k, m in UNIT_META.items()
    ])
    async def slash_recruit(self, interaction: discord.Interaction, unit: str, amount: int):
        await self._cmd_recruit(interaction, unit, amount)

    # /nation army
    @nation_group.command(name="army", description="Xem quân đội + upkeep")
    async def slash_army(self, interaction: discord.Interaction):
        await self._cmd_army(interaction)

    # /nation attack
    @nation_group.command(name="attack", description="Tấn công quốc gia khác")
    @app_commands.describe(user="User bạn muốn tấn công")
    async def slash_attack(self, interaction: discord.Interaction, user: discord.Member):
        await self._cmd_attack(interaction, user)

    # /nation ally
    @nation_group.command(name="ally", description="Gửi đề nghị liên minh")
    @app_commands.describe(user="User bạn muốn liên minh")
    async def slash_ally(self, interaction: discord.Interaction, user: discord.Member):
        await self._cmd_ally(interaction, user)

    # /nation aid
    @nation_group.command(name="aid", description="Viện trợ tài nguyên cho quốc gia khác")
    @app_commands.describe(user="Người nhận", resource="Loại tài nguyên", amount="Số lượng")
    @app_commands.choices(resource=[app_commands.Choice(name=f"{v} {k}", value=k) for k, v in RESOURCE_EMOJI.items()])
    async def slash_aid(self, interaction: discord.Interaction, user: discord.Member, resource: str, amount: int):
        await self._cmd_aid(interaction, user, resource, amount)

    # /nation war
    @nation_group.command(name="war", description="Tuyên chiến với quốc gia khác")
    @app_commands.describe(user="User bạn muốn tuyên chiến")
    async def slash_war(self, interaction: discord.Interaction, user: discord.Member):
        await self._cmd_war(interaction, user)

    # /nation peace
    @nation_group.command(name="peace", description="Đề nghị hoà bình")
    @app_commands.describe(user="User bạn muốn hoà bình")
    async def slash_peace(self, interaction: discord.Interaction, user: discord.Member):
        await self._cmd_peace(interaction, user)

    # /nation leaderboard
    @nation_group.command(name="leaderboard", description="Bảng xếp hạng Power Score")
    async def slash_leaderboard(self, interaction: discord.Interaction):
        await self._cmd_leaderboard(interaction)

    # /nation delete
    @nation_group.command(name="delete", description="Tự giải tán quốc gia (không thể hoàn tác)")
    async def slash_delete(self, interaction: discord.Interaction):
        await self._cmd_delete(interaction)

    # ══════════════════════════════════════════════════════════════════════════
    # PREFIX COMMANDS
    # ══════════════════════════════════════════════════════════════════════════

    @commands.command(name="ncreate")
    async def prefix_create(self, ctx: commands.Context, *, name: str):
        """!ncreate <tên> — Lập quốc gia"""
        await self._cmd_create(ctx, name)

    @commands.command(name="ninfo")
    async def prefix_info(self, ctx: commands.Context, user: discord.Member | None = None):
        """!ninfo [@user] — Xem thông tin quốc gia"""
        await self._cmd_info(ctx, user)

    @commands.command(name="nres", aliases=["nresources"])
    async def prefix_resources(self, ctx: commands.Context):
        """!nres — Xem tài nguyên"""
        await self._cmd_resources(ctx)

    @commands.command(name="nbuild")
    async def prefix_build(self, ctx: commands.Context, *, building: str):
        """!nbuild <tên công trình> — Xây / nâng cấp"""
        # Map tên tiếng Việt hoặc key tiếng Anh
        key = _resolve_building_key(building)
        if not key:
            valid = ", ".join(f"`{k}`" for k in BUILDING_META)
            return await ctx.send(embed=embeds.error(f"Không tìm thấy công trình. Hợp lệ: {valid}"))
        await self._cmd_build(ctx, key)

    @commands.command(name="nbuildstatus", aliases=["nbs"])
    async def prefix_buildstatus(self, ctx: commands.Context):
        """!nbs — Xem tiến độ xây dựng"""
        await self._cmd_buildstatus(ctx)

    @commands.command(name="nrecruit", aliases=["nr"])
    async def prefix_recruit(self, ctx: commands.Context, unit: str, amount: int):
        """!nrecruit <unit> <số> — Tuyển quân"""
        key = _resolve_unit_key(unit)
        if not key:
            valid = ", ".join(f"`{k}`" for k in UNIT_META)
            return await ctx.send(embed=embeds.error(f"Loại quân không hợp lệ. Hợp lệ: {valid}"))
        await self._cmd_recruit(ctx, key, amount)

    @commands.command(name="narmy", aliases=["na"])
    async def prefix_army(self, ctx: commands.Context):
        """!narmy — Xem quân đội"""
        await self._cmd_army(ctx)

    @commands.command(name="nattack")
    async def prefix_attack(self, ctx: commands.Context, user: discord.Member):
        """!nattack @user — Tấn công"""
        await self._cmd_attack(ctx, user)

    @commands.command(name="nally")
    async def prefix_ally(self, ctx: commands.Context, user: discord.Member):
        """!nally @user — Đề nghị liên minh"""
        await self._cmd_ally(ctx, user)

    @commands.command(name="naid")
    async def prefix_aid(self, ctx: commands.Context, user: discord.Member, resource: str, amount: int):
        """!naid @user <resource> <số> — Viện trợ"""
        if resource not in RESOURCE_EMOJI:
            return await ctx.send(embed=embeds.error(f"Tài nguyên không hợp lệ. Hợp lệ: {', '.join(RESOURCE_EMOJI)}"))
        await self._cmd_aid(ctx, user, resource, amount)

    @commands.command(name="nwar")
    async def prefix_war(self, ctx: commands.Context, user: discord.Member):
        """!nwar @user — Tuyên chiến"""
        await self._cmd_war(ctx, user)

    @commands.command(name="npeace")
    async def prefix_peace(self, ctx: commands.Context, user: discord.Member):
        """!npeace @user — Đề nghị hoà bình"""
        await self._cmd_peace(ctx, user)

    @commands.command(name="nlb", aliases=["nleaderboard"])
    async def prefix_leaderboard(self, ctx: commands.Context):
        """!nlb — Bảng xếp hạng"""
        await self._cmd_leaderboard(ctx)

    @commands.command(name="ndelete")
    async def prefix_delete(self, ctx: commands.Context):
        """!ndelete — Tự giải tán quốc gia"""
        await self._cmd_delete(ctx)

    # ══════════════════════════════════════════════════════════════════════════
    # SHARED IMPLEMENTATION
    # ══════════════════════════════════════════════════════════════════════════

    async def _cmd_create(self, target, name: str):
        is_interaction = isinstance(target, discord.Interaction)
        guild_id = target.guild.id
        user_id  = target.user.id if is_interaction else target.author.id

        if len(name) > 32:
            return await self._send(target, embed=embeds.error("Tên quốc gia tối đa 32 ký tự."))

        existing = await db.get_nation(user_id, guild_id)
        if existing:
            return await self._send(target,
                embed=embeds.error(f"Bạn đã có quốc gia **{existing['name']}** rồi!"))

        ok = await db.create_nation(user_id, guild_id, name)
        if not ok:
            return await self._send(target, embed=embeds.error("Tạo quốc gia thất bại, thử lại sau."))

        e = embeds.success(
            f"Quốc gia **{name}** đã được thành lập!\n\n"
            "Tài nguyên ban đầu:\n"
            "💵 1,000  ⚡ 200  🪨 150  🌾 500\n\n"
            "Dùng `/nation build` hoặc `!nbuild` để xây dựng công trình đầu tiên.",
            title="🌍  Quốc gia mới",
        )
        await self._send(target, embed=e)

    async def _cmd_info(self, target, user=None):
        is_interaction = isinstance(target, discord.Interaction)
        author_id = target.user.id if is_interaction else target.author.id
        target_user = user
        lookup_id = target_user.id if target_user else author_id

        # Apply finished buildings for own nation
        if lookup_id == author_id:
            finished = await _apply_finished_buildings(lookup_id, target.guild.id)

        nation, buildings = await self._get_nation_or_error(target, lookup_id)
        if not nation:
            return

        e = _nation_embed(nation, buildings)
        if lookup_id == author_id and finished:
            e.add_field(
                name="✅ Xây xong",
                value="\n".join(finished),
                inline=False,
            )
        await self._send(target, embed=e)

    async def _cmd_resources(self, target):
        is_interaction = isinstance(target, discord.Interaction)
        user_id  = target.user.id if is_interaction else target.author.id
        guild_id = target.guild.id

        nation, buildings = await self._get_nation_or_error(target)
        if not nation:
            return

        prod   = _calc_production(buildings)
        upkeep = _calc_upkeep(nation)

        e = discord.Embed(title=f"💰  Tài nguyên — {nation['name']}", color=embeds.Color.ECONOMY)
        for r in ("money", "energy", "material", "food"):
            val  = nation.get(r, 0)
            p    = prod.get(r, 0)
            u    = upkeep.get(r, 0)
            net  = p - u
            sign = "+" if net >= 0 else ""
            e.add_field(
                name=f"{RESOURCE_EMOJI[r]} {r.capitalize()}",
                value=f"Hiện có: `{val:,}`\nSản xuất: `+{p}`  Tiêu thụ: `-{u}`\nNet: `{sign}{net}`/tick",
                inline=True,
            )
        e.set_footer(text=f"Tick mỗi {TICK_HOURS} giờ")
        await self._send(target, embed=e)

    async def _cmd_build(self, target, building: str):
        is_interaction = isinstance(target, discord.Interaction)
        user_id  = target.user.id if is_interaction else target.author.id
        guild_id = target.guild.id

        finished = await _apply_finished_buildings(user_id, guild_id)
        nation, buildings = await self._get_nation_or_error(target)
        if not nation:
            return

        meta = BUILDING_META[building]
        b    = buildings.get(building, {"level": 0, "is_upgrading": False})

        if b.get("is_upgrading"):
            return await self._send(target,
                embed=embeds.warning(f"{meta['emoji']} **{meta['name']}** đang được nâng cấp rồi!"))

        if b.get("level", 0) >= MAX_BUILDING_LVL:
            return await self._send(target,
                embed=embeds.warning(f"{meta['emoji']} **{meta['name']}** đã đạt level tối đa (Lv{MAX_BUILDING_LVL})!"))

        # Check if anything else is being built
        for btype, row in buildings.items():
            if row.get("is_upgrading"):
                other = BUILDING_META[btype]
                return await self._send(target,
                    embed=embeds.error(
                        f"Đang xây **{other['name']}** rồi. Chỉ xây được 1 công trình tại 1 thời điểm."
                    ))

        next_level = b.get("level", 0) + 1
        cost       = _upgrade_cost(building, next_level)
        time_mins  = _upgrade_time_minutes(building, next_level)

        # Check resources
        for res, amount in cost.items():
            if nation.get(res, 0) < amount:
                cost_str = "  ".join(f"{RESOURCE_EMOJI[r]} {v:,}" for r, v in cost.items())
                return await self._send(target,
                    embed=embeds.error(f"Không đủ tài nguyên!\nCần: {cost_str}"))

        # Deduct
        updates = {res: nation.get(res, 0) - amount for res, amount in cost.items()}
        await db.update_nation(user_id, guild_id, **updates)

        finish_at = datetime.now(timezone.utc) + timedelta(minutes=time_mins)
        await db.start_upgrade(user_id, guild_id, building, finish_at.isoformat())

        cost_str = "  ".join(f"{RESOURCE_EMOJI[r]} {v:,}" for r, v in cost.items())
        e = embeds.success(
            f"{meta['emoji']} **{meta['name']}** đang được nâng cấp lên **Lv{next_level}**!\n\n"
            f"Chi phí: {cost_str}\n"
            f"Hoàn thành: {discord.utils.format_dt(finish_at, 'R')}",
            title="🏗️  Đang xây dựng",
        )
        if finished:
            e.add_field(name="✅ Vừa xong", value="\n".join(finished), inline=False)
        await self._send(target, embed=e)

    async def _cmd_buildstatus(self, target):
        is_interaction = isinstance(target, discord.Interaction)
        user_id  = target.user.id if is_interaction else target.author.id
        guild_id = target.guild.id

        finished = await _apply_finished_buildings(user_id, guild_id)
        nation, buildings = await self._get_nation_or_error(target)
        if not nation:
            return

        upgrading = [(btype, row) for btype, row in buildings.items() if row.get("is_upgrading")]

        if not upgrading and not finished:
            return await self._send(target,
                embed=embeds.info("Không có công trình nào đang xây.", title="🏗️  Build Status"))

        e = discord.Embed(title="🏗️  Trạng thái xây dựng", color=embeds.Color.INFO)
        for btype, row in upgrading:
            meta      = BUILDING_META[btype]
            finish_at = datetime.fromisoformat(row["finish_at"].replace("Z", "+00:00"))
            e.add_field(
                name=f"{meta['emoji']} {meta['name']}",
                value=f"Lv{row['level']} → Lv{row['level']+1}\n⏳ {discord.utils.format_dt(finish_at, 'R')}",
                inline=True,
            )
        if finished:
            e.add_field(name="✅ Vừa hoàn thành", value="\n".join(finished), inline=False)
        await self._send(target, embed=e)

    async def _cmd_recruit(self, target, unit: str, amount: int):
        is_interaction = isinstance(target, discord.Interaction)
        user_id  = target.user.id if is_interaction else target.author.id
        guild_id = target.guild.id

        if amount <= 0:
            return await self._send(target, embed=embeds.error("Số lượng phải lớn hơn 0."))

        nation, buildings = await self._get_nation_or_error(target)
        if not nation:
            return

        meta = UNIT_META[unit]
        cost = {r: v * amount for r, v in meta["cost"].items()}

        # Check resource
        for res, total in cost.items():
            if nation.get(res, 0) < total:
                cost_str = "  ".join(f"{RESOURCE_EMOJI[r]} {v:,}" for r, v in cost.items())
                return await self._send(target,
                    embed=embeds.error(f"Không đủ tài nguyên!\nCần: {cost_str}"))

        # Check population cap
        total_army = sum(nation.get(f"army_{u}", 0) for u in UNIT_META) + amount
        max_army   = int(nation["population"] * 0.3)
        if total_army > max_army:
            return await self._send(target,
                embed=embeds.error(
                    f"Vượt giới hạn quân đội!\n"
                    f"Tối đa: `{max_army:,}` (30% dân số)\nHiện tại: `{total_army - amount:,}`"
                ))

        updates = {res: nation.get(res, 0) - total for res, total in cost.items()}
        updates[f"army_{unit}"] = nation.get(f"army_{unit}", 0) + amount
        await db.update_nation(user_id, guild_id, **updates)

        cost_str = "  ".join(f"{RESOURCE_EMOJI[r]} {v:,}" for r, v in cost.items())
        await self._send(target,
            embed=embeds.success(
                f"Đã tuyển **{amount:,}** {meta['emoji']} {meta['name']}!\nChi phí: {cost_str}",
                title="⚔️  Tuyển quân",
            ))

    async def _cmd_army(self, target):
        nation, buildings = await self._get_nation_or_error(target)
        if not nation:
            return

        upkeep = _calc_upkeep(nation)
        e = discord.Embed(title=f"⚔️  Quân đội — {nation['name']}", color=embeds.Color.MOD)

        total_atk = total_def = 0
        for unit, meta in UNIT_META.items():
            count = nation.get(f"army_{unit}", 0)
            total_atk += count * meta["atk"]
            total_def += count * meta["def"]
            upkeep_str = "  ".join(f"{RESOURCE_EMOJI[r]} {v*count:,}" for r, v in meta["upkeep"].items())
            e.add_field(
                name=f"{meta['emoji']} {meta['name']}",
                value=f"Số lượng: `{count:,}`\nATK: `{meta['atk']*count:,}`  DEF: `{meta['def']*count:,}`\nUpkeep: {upkeep_str or '`0`'}/tick",
                inline=True,
            )

        def_bonus = _calc_defense_bonus(buildings)
        e.add_field(
            name="📊 Tổng",
            value=f"ATK: `{total_atk:,}`\nDEF: `{total_def + def_bonus:,}` (+{def_bonus} từ công trình)",
            inline=False,
        )
        upkeep_str = "  ".join(f"{RESOURCE_EMOJI[r]} {v:,}" for r, v in upkeep.items() if v > 0)
        e.set_footer(text=f"Upkeep/tick: {upkeep_str or '0'}")
        await self._send(target, embed=e)

    async def _cmd_attack(self, target, user: discord.Member):
        import random

        is_interaction = isinstance(target, discord.Interaction)
        attacker_id = target.user.id if is_interaction else target.author.id
        guild_id    = target.guild.id

        if is_interaction:
            await target.response.defer()

        if attacker_id == user.id:
            return await self._send(target, embed=embeds.error("Không thể tấn công chính mình."))

        attacker, att_buildings = await self._get_nation_or_error(target, attacker_id)
        if not attacker:
            return

        defender, def_buildings = await self._get_nation_or_error(target, user.id)
        if not defender:
            return await self._send(target, embed=embeds.error(f"**{user.display_name}** chưa có quốc gia."))

        # Cooldown check
        if attacker.get("attack_cooldown"):
            cd = datetime.fromisoformat(attacker["attack_cooldown"].replace("Z", "+00:00"))
            if cd > datetime.now(timezone.utc):
                return await self._send(target,
                    embed=embeds.error(f"Còn cooldown tấn công! {discord.utils.format_dt(cd, 'R')}"))

        # Newbie protection
        created = datetime.fromisoformat(defender["created_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - created < NEWBIE_PROTECT:
            return await self._send(target,
                embed=embeds.error(
                    f"**{defender['name']}** đang được bảo vệ newbie ({NEWBIE_PROTECT.hours}h đầu)."
                ))

        # Alliance check
        if await db.are_allies(attacker_id, user.id, guild_id):
            return await self._send(target,
                embed=embeds.error(f"**{defender['name']}** là đồng minh của bạn! Hãy tuyên chiến trước."))

        # Combat
        atk_power = (
            attacker.get("army_infantry", 0) * 10 +
            attacker.get("army_tank",     0) * 40 +
            attacker.get("army_airforce", 0) * 60
        ) * random.uniform(0.85, 1.15)

        def_power = (
            defender.get("army_infantry", 0) * 8  +
            defender.get("army_tank",     0) * 35 +
            defender.get("army_navy",     0) * 45
        ) * random.uniform(0.85, 1.15) + _calc_defense_bonus(def_buildings)

        attacker_wins = atk_power > def_power

        # Casualties
        for unit in UNIT_META:
            att_count = attacker.get(f"army_{unit}", 0)
            def_count = defender.get(f"army_{unit}", 0)
            if attacker_wins:
                await db.update_nation(attacker_id, guild_id, **{f"army_{unit}": max(0, int(att_count * 0.8))})
                await db.update_nation(user.id, guild_id,      **{f"army_{unit}": max(0, int(def_count * 0.65))})
            else:
                await db.update_nation(attacker_id, guild_id, **{f"army_{unit}": max(0, int(att_count * 0.6))})
                await db.update_nation(user.id, guild_id,      **{f"army_{unit}": max(0, int(def_count * 0.9))})

        # Loot
        loot = {}
        if attacker_wins:
            loot = {
                "money":    int(defender.get("money", 0)    * 0.20),
                "material": int(defender.get("material", 0) * 0.15),
                "energy":   int(defender.get("energy", 0)   * 0.10),
            }
            await db.update_nation(attacker_id, guild_id,
                money=attacker.get("money", 0)       + loot["money"],
                material=attacker.get("material", 0) + loot["material"],
                energy=attacker.get("energy", 0)     + loot["energy"],
            )
            await db.update_nation(user.id, guild_id,
                money=max(0,    defender.get("money", 0)    - loot["money"]),
                material=max(0, defender.get("material", 0) - loot["material"]),
                energy=max(0,   defender.get("energy", 0)   - loot["energy"]),
            )

            # Check defender extinction
            def_updated = await db.get_nation(user.id, guild_id)
            if def_updated and def_updated.get("population", 1) <= 0:
                await db.delete_nation(user.id, guild_id)
                try:
                    await user.send(embed=embeds.error(
                        f"Quốc gia **{defender['name']}** của bạn đã bị **{attacker['name']}** tiêu diệt!",
                        title="💀  Diệt vong",
                    ))
                except discord.Forbidden:
                    pass

        # Set cooldown
        new_cooldown = datetime.now(timezone.utc) + ATTACK_COOLDOWN
        await db.update_nation(attacker_id, guild_id, attack_cooldown=new_cooldown.isoformat())

        # Result embed
        color = embeds.Color.SUCCESS if attacker_wins else embeds.Color.ERROR
        result = "🏆 THẮNG" if attacker_wins else "💀 THUA"
        e = discord.Embed(
            title=f"⚔️  {attacker['name']} vs {defender['name']}",
            description=f"Kết quả: **{result}**",
            color=color,
        )
        e.add_field(name="ATK Power", value=f"`{int(atk_power):,}`", inline=True)
        e.add_field(name="DEF Power", value=f"`{int(def_power):,}`", inline=True)
        if loot:
            loot_str = "  ".join(f"{RESOURCE_EMOJI[r]} {v:,}" for r, v in loot.items() if v > 0)
            e.add_field(name="💰 Chiến lợi phẩm", value=loot_str, inline=False)
        e.set_footer(text=f"Cooldown tiếp theo: {discord.utils.format_dt(new_cooldown, 'R')}")

        await self._send(target, embed=e)

        # Notify defender
        try:
            await user.send(embed=discord.Embed(
                title=f"🚨  Bị tấn công!",
                description=(
                    f"**{attacker['name']}** đã tấn công **{defender['name']}**!\n"
                    f"Kết quả: {'Họ thắng 💀' if attacker_wins else 'Họ thua 🏆'}"
                    + (f"\nBạn mất: " + "  ".join(f"{RESOURCE_EMOJI[r]} {v:,}" for r, v in loot.items()) if loot else "")
                ),
                color=embeds.Color.ERROR if attacker_wins else embeds.Color.SUCCESS,
            ))
        except discord.Forbidden:
            pass

    async def _cmd_ally(self, target, user: discord.Member):
        is_interaction = isinstance(target, discord.Interaction)
        from_id  = target.user.id if is_interaction else target.author.id
        guild_id = target.guild.id

        if from_id == user.id:
            return await self._send(target, embed=embeds.error("Không thể liên minh với chính mình."))

        nation, _ = await self._get_nation_or_error(target)
        if not nation:
            return
        target_nation = await db.get_nation(user.id, guild_id)
        if not target_nation:
            return await self._send(target, embed=embeds.error(f"{user.display_name} chưa có quốc gia."))

        if await db.are_allies(from_id, user.id, guild_id):
            return await self._send(target, embed=embeds.warning("Hai bên đã là đồng minh rồi."))

        diplo_id = await db.create_diplomacy(from_id, user.id, guild_id, "alliance")

        try:
            view = DiplomacyView(diplo_id=diplo_id, proposer_id=from_id)
            await user.send(
                embed=discord.Embed(
                    title="🤝  Đề nghị Liên minh",
                    description=(
                        f"**{nation['name']}** (<@{from_id}>) muốn liên minh với **{target_nation['name']}**!\n\n"
                        "Chấp nhận để nhận +5% sản xuất và không bị tấn công."
                    ),
                    color=embeds.Color.INFO,
                ),
                view=view,
            )
            await self._send(target, embed=embeds.success(
                f"Đã gửi đề nghị liên minh đến **{target_nation['name']}**! Đợi họ trả lời trong DM.",
                title="🤝  Liên minh",
            ))
        except discord.Forbidden:
            await self._send(target, embed=embeds.error(f"Không thể DM {user.display_name}. Họ cần bật DM."))

    async def _cmd_aid(self, target, user: discord.Member, resource: str, amount: int):
        is_interaction = isinstance(target, discord.Interaction)
        from_id  = target.user.id if is_interaction else target.author.id
        guild_id = target.guild.id

        if amount <= 0:
            return await self._send(target, embed=embeds.error("Số lượng phải lớn hơn 0."))
        if from_id == user.id:
            return await self._send(target, embed=embeds.error("Không thể viện trợ cho chính mình."))

        nation, _ = await self._get_nation_or_error(target)
        if not nation:
            return
        target_nation = await db.get_nation(user.id, guild_id)
        if not target_nation:
            return await self._send(target, embed=embeds.error(f"{user.display_name} chưa có quốc gia."))

        if nation.get(resource, 0) < amount:
            return await self._send(target,
                embed=embeds.error(f"Không đủ {RESOURCE_EMOJI[resource]} {resource}. Hiện có: `{nation.get(resource,0):,}`"))

        await db.update_nation(from_id, guild_id, **{resource: nation.get(resource, 0) - amount})
        await db.update_nation(user.id,  guild_id, **{resource: target_nation.get(resource, 0) + amount})

        await self._send(target, embed=embeds.success(
            f"Đã viện trợ {RESOURCE_EMOJI[resource]} **{amount:,}** {resource} cho **{target_nation['name']}**!",
            title="💰  Viện trợ",
        ))
        try:
            await user.send(embed=embeds.info(
                f"**{nation['name']}** đã viện trợ cho bạn {RESOURCE_EMOJI[resource]} **{amount:,}** {resource}!",
                title="💰  Nhận viện trợ",
            ))
        except discord.Forbidden:
            pass

    async def _cmd_war(self, target, user: discord.Member):
        is_interaction = isinstance(target, discord.Interaction)
        from_id  = target.user.id if is_interaction else target.author.id
        guild_id = target.guild.id

        nation, _ = await self._get_nation_or_error(target)
        if not nation:
            return
        target_nation = await db.get_nation(user.id, guild_id)
        if not target_nation:
            return await self._send(target, embed=embeds.error(f"{user.display_name} chưa có quốc gia."))

        # Cancel any existing alliance
        alliances = await db.get_active_diplomacy(from_id, guild_id, "alliance")
        for a in alliances:
            if str(a["from_user"]) == str(user.id) or str(a["to_user"]) == str(user.id):
                await db.update_diplomacy_status(a["id"], "cancelled")

        await db.create_diplomacy(from_id, user.id, guild_id, "war", payload={"status": "active"})

        e = discord.Embed(
            title="📢  Tuyên chiến!",
            description=f"**{nation['name']}** đã tuyên chiến với **{target_nation['name']}**!",
            color=embeds.Color.ERROR,
        )
        await self._send(target, embed=e)
        try:
            await user.send(embed=e)
        except discord.Forbidden:
            pass

    async def _cmd_peace(self, target, user: discord.Member):
        is_interaction = isinstance(target, discord.Interaction)
        from_id  = target.user.id if is_interaction else target.author.id
        guild_id = target.guild.id

        nation, _ = await self._get_nation_or_error(target)
        if not nation:
            return
        target_nation = await db.get_nation(user.id, guild_id)
        if not target_nation:
            return await self._send(target, embed=embeds.error(f"{user.display_name} chưa có quốc gia."))

        diplo_id = await db.create_diplomacy(from_id, user.id, guild_id, "peace")
        try:
            view = DiplomacyView(diplo_id=diplo_id, proposer_id=from_id)
            await user.send(
                embed=discord.Embed(
                    title="☮️  Đề nghị Hoà bình",
                    description=f"**{nation['name']}** (<@{from_id}>) muốn hoà bình với **{target_nation['name']}**.",
                    color=embeds.Color.SUCCESS,
                ),
                view=view,
            )
            await self._send(target, embed=embeds.success(
                f"Đã gửi đề nghị hoà bình đến **{target_nation['name']}**.",
                title="☮️  Hoà bình",
            ))
        except discord.Forbidden:
            await self._send(target, embed=embeds.error(f"Không thể DM {user.display_name}."))

    async def _cmd_leaderboard(self, target):
        guild_id = target.guild.id
        nations  = await db.list_nations(guild_id)
        if not nations:
            return await self._send(target, embed=embeds.info("Chưa có quốc gia nào trong server này."))

        scores = []
        for n in nations:
            buildings = _buildings_dict(await db.get_buildings(int(n["user_id"]), guild_id))
            scores.append((n, _calc_power_score(n, buildings)))

        scores.sort(key=lambda x: x[1], reverse=True)

        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, (n, score) in enumerate(scores[:10]):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            lines.append(f"{medal} **{n['name']}** — <@{n['user_id']}>\n    ⚡ Power Score: `{score:,}`")

        e = embeds.info("\n\n".join(lines), title=f"🏆  Leaderboard — {target.guild.name}")
        await self._send(target, embed=e)

    async def _cmd_delete(self, target):
        is_interaction = isinstance(target, discord.Interaction)
        user_id  = target.user.id if is_interaction else target.author.id
        guild_id = target.guild.id

        nation, _ = await self._get_nation_or_error(target)
        if not nation:
            return

        # Simple confirm via message
        confirm_msg = await self._send_and_return(target,
            embed=embeds.warning(
                f"Bạn chắc chắn muốn giải tán **{nation['name']}**?\n"
                "Gõ `yes` để xác nhận (hết 30 giây).",
                title="⚠️  Xác nhận giải tán",
            ))

        def check(m):
            uid = target.user.id if is_interaction else target.author.id
            return m.author.id == uid and m.channel == (target.channel if not is_interaction else target.channel) and m.content.lower() == "yes"

        try:
            await self.bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            return await self._send(target, embed=embeds.info("Hủy giải tán."))

        await db.delete_nation(user_id, guild_id)
        await self._send(target, embed=embeds.success(
            f"Quốc gia **{nation['name']}** đã được giải tán.", title="💀  Giải tán"
        ))

    async def _send_and_return(self, target, **kwargs):
        if isinstance(target, discord.Interaction):
            if target.response.is_done():
                return await target.followup.send(**kwargs)
            else:
                await target.response.send_message(**kwargs)
                return await target.original_response()
        else:
            return await target.send(**kwargs)

    # ── Error handler ─────────────────────────────────────────────────────────
    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        msg = str(error)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embeds.error(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=embeds.error(msg), ephemeral=True)


# ── Key resolvers for prefix commands ────────────────────────────────────────

def _resolve_building_key(text: str) -> str | None:
    text = text.lower().strip()
    # Direct key match
    if text in BUILDING_META:
        return text
    # Match by name
    for k, m in BUILDING_META.items():
        if text in m["name"].lower():
            return k
    return None


def _resolve_unit_key(text: str) -> str | None:
    text = text.lower().strip()
    if text in UNIT_META:
        return text
    for k, m in UNIT_META.items():
        if text in m["name"].lower():
            return k
    return None


async def setup(bot: commands.Bot):
    await bot.add_cog(Nation(bot))