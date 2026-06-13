from __future__ import annotations
from supabase import create_client, Client
from config import Config
import logging

log = logging.getLogger("database")


class Database:
    """
    Singleton Supabase client — lazy init so .env loads before connection.
    All DB calls go through here — cogs import `db` at the bottom of this file.
    """

    def __init__(self):
        self._client: Client | None = None

    def _get_client(self) -> Client:
        if self._client is None:
            self._client = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)
        return self._client

    # ── Guild settings ────────────────────────────────────────────────────────

    async def init_guild(self, guild_id: int, guild_name: str) -> None:
        """Insert default guild row if it doesn't exist yet."""
        try:
            (
                self._get_client().table("guild_settings")
                .upsert(
                    {
                        "guild_id": str(guild_id),
                        "guild_name": guild_name,
                        "prefix": "!",
                        "mod_log_channel": None,
                        "welcome_channel": None,
                        "mute_role": None,
                    },
                    on_conflict="guild_id",
                    ignore_duplicates=True,
                )
                .execute()
            )
        except Exception as e:
            log.error(f"init_guild error: {e}")

    async def get_guild_settings(self, guild_id: int) -> dict:
        try:
            res = (
                self._get_client().table("guild_settings")
                .select("*")
                .eq("guild_id", str(guild_id))
                .single()
                .execute()
            )
            return res.data or {}
        except Exception:
            return {}

    async def update_guild_setting(self, guild_id: int, key: str, value) -> None:
        try:
            (
                self._get_client().table("guild_settings")
                .update({key: value})
                .eq("guild_id", str(guild_id))
                .execute()
            )
        except Exception as e:
            log.error(f"update_guild_setting error: {e}")

    # ── Moderation: warnings ──────────────────────────────────────────────────

    async def add_warn(self, guild_id: int, user_id: int, mod_id: int, reason: str) -> int:
        """Add a warning, return total warn count for this user."""
        try:
            self._get_client().table("warnings").insert(
                {
                    "guild_id": str(guild_id),
                    "user_id": str(user_id),
                    "mod_id": str(mod_id),
                    "reason": reason,
                }
            ).execute()
            count = (
                self._get_client().table("warnings")
                .select("id", count="exact")
                .eq("guild_id", str(guild_id))
                .eq("user_id", str(user_id))
                .execute()
            )
            return count.count or 0
        except Exception as e:
            log.error(f"add_warn error: {e}")
            return 0

    async def get_warns(self, guild_id: int, user_id: int) -> list[dict]:
        try:
            res = (
                self._get_client().table("warnings")
                .select("*")
                .eq("guild_id", str(guild_id))
                .eq("user_id", str(user_id))
                .order("created_at", desc=False)
                .execute()
            )
            return res.data or []
        except Exception:
            return []

    async def clear_warns(self, guild_id: int, user_id: int) -> int:
        """Delete all warnings for a user, return deleted count."""
        try:
            res = (
                self._get_client().table("warnings")
                .delete()
                .eq("guild_id", str(guild_id))
                .eq("user_id", str(user_id))
                .execute()
            )
            return len(res.data) if res.data else 0
        except Exception as e:
            log.error(f"clear_warns error: {e}")
            return 0

    # ── Economy ───────────────────────────────────────────────────────────────

    async def get_wallet(self, guild_id: int, user_id: int) -> dict:
        try:
            res = (
                self._get_client().table("economy")
                .select("*")
                .eq("guild_id", str(guild_id))
                .eq("user_id", str(user_id))
                .single()
                .execute()
            )
            return res.data or {}
        except Exception:
            return {"balance": 0, "bank": 0, "last_daily": None}

    async def upsert_wallet(self, guild_id: int, user_id: int, balance: int, bank: int, last_daily: str | None = None) -> None:
        payload = {
            "guild_id": str(guild_id),
            "user_id": str(user_id),
            "balance": balance,
            "bank": bank,
        }
        if last_daily is not None:
            payload["last_daily"] = last_daily
        try:
            self._get_client().table("economy").upsert(payload, on_conflict="guild_id,user_id").execute()
        except Exception as e:
            log.error(f"upsert_wallet error: {e}")

    async def get_economy_leaderboard(self, guild_id: int, limit: int = 10) -> list[dict]:
        try:
            res = (
                self._get_client().table("economy")
                .select("user_id, balance, bank")
                .eq("guild_id", str(guild_id))
                .order("balance", desc=True)
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception:
            return []

    # ── Backups ───────────────────────────────────────────────────────────────

    async def save_backup(
        self,
        guild_id: int,
        guild_name: str,
        label: str,
        data: dict,
        auto: bool = False,
    ) -> str:
        """Save a backup snapshot, return the generated UUID."""
        import uuid, json
        backup_id = str(uuid.uuid4())
        try:
            self._get_client().table("server_backups").insert({
                "id": backup_id,
                "guild_id": str(guild_id),
                "guild_name": guild_name,
                "label": label,
                "auto": auto,
                "data": json.dumps(data),   # store as text; cast back on read
            }).execute()
        except Exception as e:
            log.error(f"save_backup error: {e}")
        return backup_id

    async def list_backups(self, guild_id: int) -> list[dict]:
        """Return all backups for a guild, newest first (data field excluded)."""
        try:
            res = (
                self._get_client().table("server_backups")
                .select("id, guild_id, guild_name, label, auto, created_at")
                .eq("guild_id", str(guild_id))
                .order("created_at", desc=True)
                .execute()
            )
            return res.data or []
        except Exception as e:
            log.error(f"list_backups error: {e}")
            return []

    async def get_backup(self, guild_id: int, backup_id_prefix: str) -> dict | None:
        """
        Fetch a single backup by full or partial ID.
        Returns dict with 'data' as parsed Python dict, or None.
        """
        import json as _json
        try:
            res = (
                self._get_client().table("server_backups")
                .select("*")
                .eq("guild_id", str(guild_id))
                .ilike("id", f"{backup_id_prefix}%")
                .limit(1)
                .execute()
            )
            if not res.data:
                return None
            row = res.data[0]
            # data column may come back as string or dict depending on Supabase config
            if isinstance(row["data"], str):
                row["data"] = _json.loads(row["data"])
            return row
        except Exception as e:
            log.error(f"get_backup error: {e}")
            return None

    async def delete_backup(self, backup_id: str) -> None:
        """Delete a backup by full UUID."""
        try:
            self._get_client().table("server_backups").delete().eq("id", backup_id).execute()
        except Exception as e:
            log.error(f"delete_backup error: {e}")


# Singleton — import this everywhere
db = Database()
