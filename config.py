import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Required ──────────────────────────────────────────────────────────────
    TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

    # ── Optional ──────────────────────────────────────────────────────────────
    # Set this during development for instant slash command sync.
    # Remove (or set to None) in production for global sync.
    GUILD_ID: int | None = int(os.getenv("DEV_GUILD_ID")) if os.getenv("DEV_GUILD_ID") else None

    # Lavalink (for music cog)
    LAVALINK_HOST: str = os.getenv("LAVALINK_HOST", "localhost")
    LAVALINK_PORT: int = int(os.getenv("LAVALINK_PORT", 2333))
    LAVALINK_PASSWORD: str = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")

    # Misc APIs
    WEATHER_API_KEY: str = os.getenv("WEATHER_API_KEY", "")   # OpenWeatherMap
    DEEPL_API_KEY: str = os.getenv("DEEPL_API_KEY", "")       # DeepL translate

    # Validate required fields on import
    @classmethod
    def validate(cls):
        missing = []
        for field in ("TOKEN", "SUPABASE_URL", "SUPABASE_KEY"):
            if not getattr(cls, field):
                missing.append(field)
        if missing:
            raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")
