from __future__ import annotations
import discord
from datetime import datetime


# ── Colour palette ────────────────────────────────────────────────────────────
class Color:
    SUCCESS = 0x57F287   # green
    ERROR   = 0xED4245   # red
    WARNING = 0xFEE75C   # yellow
    INFO    = 0x5865F2   # blurple
    MOD     = 0xEB459E   # pink
    ECONOMY = 0xF0B232   # gold
    MUSIC   = 0x1DB954   # spotify green
    NEUTRAL = 0x2B2D31   # dark


def _base(color: int, title: str | None = None) -> discord.Embed:
    e = discord.Embed(color=color, timestamp=datetime.utcnow())
    if title:
        e.title = title
    return e


# ── Factory functions ─────────────────────────────────────────────────────────

def success(description: str, title: str | None = None) -> discord.Embed:
    e = _base(Color.SUCCESS, title)
    e.description = f"✅  {description}"
    return e


def error(description: str, title: str | None = None) -> discord.Embed:
    e = _base(Color.ERROR, title)
    e.description = f"❌  {description}"
    return e


def warning(description: str, title: str | None = None) -> discord.Embed:
    e = _base(Color.WARNING, title)
    e.description = f"⚠️  {description}"
    return e


def info(description: str, title: str | None = None) -> discord.Embed:
    e = _base(Color.INFO, title)
    e.description = description
    return e


def mod_action(
    action: str,
    target: discord.Member,
    moderator: discord.Member,
    reason: str,
    extra: dict | None = None,
) -> discord.Embed:
    """Standard moderation log embed."""
    e = _base(Color.MOD, f"🔨  {action}")
    e.add_field(name="User", value=f"{target.mention} (`{target.id}`)", inline=True)
    e.add_field(name="Moderator", value=f"{moderator.mention}", inline=True)
    e.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    if extra:
        for k, v in extra.items():
            e.add_field(name=k, value=str(v), inline=True)
    e.set_thumbnail(url=target.display_avatar.url)
    return e


def paginate(items: list[str], title: str, color: int = Color.INFO, per_page: int = 10) -> list[discord.Embed]:
    """Split a list of strings into multiple embeds (for pagination)."""
    pages = []
    chunks = [items[i : i + per_page] for i in range(0, len(items), per_page)]
    for i, chunk in enumerate(chunks):
        e = _base(color, title)
        e.description = "\n".join(chunk)
        e.set_footer(text=f"Page {i+1}/{len(chunks)}")
        pages.append(e)
    return pages or [_base(color, title)]
