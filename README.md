# MultiBot — Multi-Purpose Discord Bot

Built with `discord.py 2.x` · `Supabase` · `wavelink` (Lavalink)

## Quick Start

```bash
# 1. Clone & install
pip install -r requirements.txt

# 2. Copy env template
cp .env.example .env
# Fill in DISCORD_TOKEN, SUPABASE_URL, SUPABASE_KEY

# 3. Run Supabase migration
# Open Supabase dashboard → SQL Editor → paste database/schema.sql → Run

# 4. Start bot
python main.py
```

## Project Structure

```
bot/
├── main.py                  # Entry point, cog auto-loader
├── config.py                # Env config
├── requirements.txt
├── .env.example
├── database/
│   ├── supabase_client.py   # Singleton DB client (import `db`)
│   └── schema.sql           # Supabase table definitions
├── cogs/
│   ├── help.py              # /help command
│   ├── moderation.py        # /ban /kick /mute /warn ...
│   ├── utility.py           # /userinfo /serverinfo /weather ...
│   ├── fun.py               # /roll /8ball /meme ...
│   ├── economy.py           # /balance /daily /pay ...
│   └── music.py             # /play /skip /queue ... (Lavalink)
└── utils/
    └── embeds.py            # Embed factory helpers
```

## Adding a New Cog

1. Create `cogs/mycog.py`
2. Write your cog class extending `commands.Cog`
3. Add `async def setup(bot): await bot.add_cog(MyCog(bot))` at the bottom
4. Done — auto-discovered on next restart

## Notes

- **DEV_GUILD_ID** in `.env` = instant slash command sync (for development).
  Remove it when deploying to production (global sync takes up to 1 hour).
- Music requires a running **Lavalink** server. See: https://github.com/lavalink-devs/Lavalink
