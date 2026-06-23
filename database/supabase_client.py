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
        UUID columns don't support ilike — we fetch all for this guild
        then filter by startswith() in Python.
        Returns dict with 'data' as parsed Python dict, or None.
        """
        import json as _json
        try:
            client = self._get_client()
            backup_id_prefix = backup_id_prefix.lower().strip()

            # ── Full UUID: exact match is cheapest ────────────────────────────
            if len(backup_id_prefix) == 36:
                res = (
                    client.table("server_backups")
                    .select("*")
                    .eq("guild_id", str(guild_id))
                    .eq("id", backup_id_prefix)
                    .limit(1)
                    .execute()
                )
            else:
                # ── Partial ID: fetch all for guild, filter in Python ─────────
                # ilike on UUID type columns silently returns nothing in Supabase,
                # so we pull id+label+auto+created_at first (no heavy data column),
                # find the match, then fetch the full row by exact id.
                meta_res = (
                    client.table("server_backups")
                    .select("id, guild_id, guild_name, label, auto, created_at")
                    .eq("guild_id", str(guild_id))
                    .order("created_at", desc=True)
                    .execute()
                )
                rows = meta_res.data or []
                match = next(
                    (r for r in rows if r["id"].lower().startswith(backup_id_prefix)),
                    None,
                )
                if not match:
                    return None

                # Now fetch full row (including data) by exact UUID
                res = (
                    client.table("server_backups")
                    .select("*")
                    .eq("guild_id", str(guild_id))
                    .eq("id", match["id"])
                    .limit(1)
                    .execute()
                )

            if not res.data:
                return None

            row = res.data[0]
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

    # ── Lobby: games list ────────────────────────────────────────────────────

    async def get_lobby_game(self, guild_id: int, name: str) -> dict | None:
        """Fetch a single game record by name."""
        try:
            res = (
                self._get_client().table("lobby_games")
                .select("*")
                .eq("guild_id", str(guild_id))
                .eq("name", name)
                .single()
                .execute()
            )
            return res.data or None
        except Exception:
            return None
 
    async def add_lobby_game(
        self, guild_id: int, name: str, emoji: str = "🎮", role_id: int | None = None
    ) -> bool:
        """Add a game to the dropdown list. Returns False if it already exists."""
        try:
            self._get_client().table("lobby_games").insert({
                "guild_id": str(guild_id),
                "name":     name,
                "emoji":    emoji,
                "role_id":  str(role_id) if role_id else None,
            }).execute()
            return True
        except Exception as e:
            log.error(f"add_lobby_game error: {e}")
            return False

    async def remove_lobby_game(self, guild_id: int, name: str) -> bool:
        try:
            res = (
                self._get_client().table("lobby_games")
                .delete()
                .eq("guild_id", str(guild_id))
                .eq("name", name)
                .execute()
            )
            return bool(res.data)
        except Exception as e:
            log.error(f"remove_lobby_game error: {e}")
            return False

    async def list_lobby_games(self, guild_id: int) -> list[dict]:
        try:
            res = (
                self._get_client().table("lobby_games")
                .select("*")
                .eq("guild_id", str(guild_id))
                .order("name")
                .execute()
            )
            return res.data or []
        except Exception as e:
            log.error(f"list_lobby_games error: {e}")
            return []

    # ── Lobby: panel (permanent embed message) ──────────────────────────────

    async def set_lobby_panel(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        category_id: int | None,
        announcement_channel_id: int | None = None,
        roles_channel_id: int | None = None,
        roles_message_id: int | None = None,
    ) -> None:
        try:
            self._get_client().table("lobby_panels").upsert({
                "guild_id":                str(guild_id),
                "channel_id":              str(channel_id),
                "message_id":              str(message_id),
                "category_id":             str(category_id) if category_id else None,
                "announcement_channel_id": str(announcement_channel_id) if announcement_channel_id else None,
                "roles_channel_id":        str(roles_channel_id) if roles_channel_id else None,
                "roles_message_id":        str(roles_message_id) if roles_message_id else None,
            }, on_conflict="guild_id").execute()
        except Exception as e:
            log.error(f"set_lobby_panel error: {e}")
 
    async def get_lobby_panel(self, guild_id: int) -> dict | None:
        try:
            res = (
                self._get_client().table("lobby_panels")
                .select("*")
                .eq("guild_id", str(guild_id))
                .single()
                .execute()
            )
            return res.data or None
        except Exception:
            return None

    async def get_all_lobby_panels(self) -> list[dict]:
        """Used on bot startup to re-register persistent Views for every guild."""
        try:
            res = self._get_client().table("lobby_panels").select("*").execute()
            return res.data or []
        except Exception as e:
            log.error(f"get_all_lobby_panels error: {e}")
            return []

    # ── Lobby: active lobby tracking ─────────────────────────────────────────

    async def update_lobby_slots(self, lobby_id: str, slots: list) -> None:
        """Update the slots JSON for an active lobby."""
        import json
        try:
            self._get_client().table("active_lobbies").update({
                "slots": slots,   # Supabase handles list→jsonb automatically
            }).eq("id", lobby_id).execute()
        except Exception as e:
            log.error(f"update_lobby_slots error: {e}")
 
    async def create_active_lobby(
        self,
        guild_id: int,
        owner_id: int,
        voice_channel_id: int | None,
        text_channel_id: int | None,
        game_name: str,
        max_users: int | None,
        lobby_type: str | None = None,
        strategy: str | None = None,
        note: str | None = None,
        slots: list | None = None,
    ) -> str:
        import uuid
        lobby_id = str(uuid.uuid4())
        try:
            self._get_client().table("active_lobbies").insert({
                "id":               lobby_id,
                "guild_id":         str(guild_id),
                "owner_id":         str(owner_id),
                "voice_channel_id": str(voice_channel_id) if voice_channel_id else None,
                "text_channel_id":  str(text_channel_id)  if text_channel_id  else None,
                "game_name":        game_name,
                "max_users":        max_users,
                "lobby_type":       lobby_type,
                "strategy":         strategy,
                "note":             note,
                "slots":            slots or [],
            }).execute()
        except Exception as e:
            log.error(f"create_active_lobby error: {e}")
        return lobby_id
    
    async def update_active_lobby_messages(
        self,
        lobby_id: str,
        lobby_message_id: int | None,
        announcement_channel_id: int | None,
        announcement_message_id: int | None,
    ) -> None:
        """Store message IDs of the in-lobby embed and the announcement embed
        so they can be edited later (e.g. to refresh the live member count)."""
        try:
            self._get_client().table("active_lobbies").update({
                "lobby_message_id": str(lobby_message_id) if lobby_message_id else None,
                "announcement_channel_id": str(announcement_channel_id) if announcement_channel_id else None,
                "announcement_message_id": str(announcement_message_id) if announcement_message_id else None,
            }).eq("id", lobby_id).execute()
        except Exception as e:
            log.error(f"update_active_lobby_messages error: {e}")

    async def delete_active_lobby(self, lobby_id: str) -> None:
        try:
            self._get_client().table("active_lobbies").delete().eq("id", lobby_id).execute()
        except Exception as e:
            log.error(f"delete_active_lobby error: {e}")

    async def get_active_lobby_by_channel(self, channel_id: int) -> dict | None:
        """Find a lobby record by either its voice or text channel ID."""
        try:
            client = self._get_client()
            res = (
                client.table("active_lobbies")
                .select("*")
                .eq("voice_channel_id", str(channel_id))
                .limit(1)
                .execute()
            )
            if res.data:
                return res.data[0]
            res = (
                client.table("active_lobbies")
                .select("*")
                .eq("text_channel_id", str(channel_id))
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            log.error(f"get_active_lobby_by_channel error: {e}")
            return None

    async def list_active_lobbies(self, guild_id: int) -> list[dict]:
        try:
            res = (
                self._get_client().table("active_lobbies")
                .select("*")
                .eq("guild_id", str(guild_id))
                .execute()
            )
            return res.data or []
        except Exception as e:
            log.error(f"list_active_lobbies error: {e}")
            return []

# Singleton — import this everywhere
db = Database()
