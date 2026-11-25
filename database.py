import time
import aiosqlite
from typing import List, Dict, Optional, Tuple


class Database:
    def __init__(self, db_path: str = "bot_data.db"):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    async def init_db(self):
        self.conn = await aiosqlite.connect(self.db_path, timeout=30.0)
        self.conn.row_factory = aiosqlite.Row

        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA synchronous=NORMAL;")
        await self.conn.execute("PRAGMA busy_timeout=5000;")
        await self.conn.execute("PRAGMA cache_size=-32000;")

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                max_slots INTEGER DEFAULT 3,
                plan_expiry REAL DEFAULT NULL,
                agreed INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                created_at REAL
            )
        """)

        for col_sql in [
            "ALTER TABLE users ADD COLUMN agreed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0",
        ]:
            try:
                await self.conn.execute(col_sql)
            except Exception:
                pass

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS active_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL UNIQUE,
                alias_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_active_user ON active_emails(user_id);"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_active_email ON active_emails(email);"
        )

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ddg_tokens (
                token_id TEXT PRIMARY KEY,
                bearer_token TEXT NOT NULL,
                exhausted_until REAL DEFAULT 0,
                is_valid INTEGER DEFAULT 1
            )
        """)

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS inbox_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_uid TEXT NOT NULL UNIQUE,
                alias TEXT NOT NULL,
                sender TEXT,
                subject TEXT,
                otp TEXT,
                body TEXT,
                received_at REAL NOT NULL
            )
        """)
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbox_alias ON inbox_messages(alias, received_at DESC);"
        )

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                event_type TEXT NOT NULL,
                alias_email TEXT,
                details TEXT,
                created_at REAL NOT NULL
            )
        """)

        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def register_user(self, user_id: int, username: Optional[str]):
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, max_slots, agreed, is_banned, created_at)
            VALUES (?, ?, 3, 0, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """,
            (user_id, username or "", time.time()),
        )
        await self.conn.commit()

    async def get_security_status(self, user_id: int) -> Tuple[bool, bool]:
        """Returns (has_agreed, is_banned)."""
        cursor = await self.conn.execute(
            "SELECT agreed, is_banned FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False, False
        return bool(row["agreed"]), bool(row["is_banned"])

    async def set_user_agreed(self, user_id: int, username: Optional[str] = ""):
        await self.register_user(user_id, username)
        await self.conn.execute(
            "UPDATE users SET agreed = 1 WHERE user_id = ?", (user_id,)
        )
        await self.conn.commit()

    async def set_ban_status(self, user_id: int, banned: bool):
        await self.register_user(user_id, "")
        await self.conn.execute(
            "UPDATE users SET is_banned = ? WHERE user_id = ?",
            (1 if banned else 0, user_id),
        )
        if banned:
            await self.conn.execute(
                "DELETE FROM active_emails WHERE user_id = ?", (user_id,)
            )
        await self.conn.commit()

    async def log_audit_event(
        self,
        user_id: int,
        username: str,
        event_type: str,
        alias_email: str = "",
        details: str = "",
    ):
        await self.conn.execute(
            """
            INSERT INTO audit_logs (user_id, username, event_type, alias_email, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, event_type, alias_email.lower(), details, time.time()),
        )
        await self.conn.commit()

    async def get_user_quota(self, user_id: int) -> Tuple[int, int, Optional[float]]:
        now = time.time()
        cursor = await self.conn.execute(
            "SELECT max_slots, plan_expiry FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            await self.register_user(user_id, "")
            max_slots, expiry = 3, None
        else:
            max_slots, expiry = row["max_slots"], row["plan_expiry"]

        if expiry is not None and expiry < now:
            max_slots, expiry = 3, None
            await self.conn.execute(
                "UPDATE users SET max_slots = 3, plan_expiry = NULL WHERE user_id = ?",
                (user_id,),
            )
            await self.conn.commit()

        c2 = await self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM active_emails WHERE user_id = ?", (user_id,)
        )
        active_count = (await c2.fetchone())["cnt"]
        return active_count, max_slots, expiry

    async def set_subscription(self, user_id: int, slots: int, days: int):
        slots = max(3, min(50, slots))
        expiry = time.time() + (days * 86400)
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, max_slots, plan_expiry, created_at)
            VALUES (?, '', ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                max_slots = excluded.max_slots,
                plan_expiry = excluded.plan_expiry
            """,
            (user_id, slots, expiry, time.time()),
        )
        await self.conn.commit()

    async def remove_subscription(self, user_id: int):
        await self.conn.execute(
            "UPDATE users SET max_slots = 3, plan_expiry = NULL WHERE user_id = ?",
            (user_id,),
        )
        await self.conn.commit()

    async def add_email(self, user_id: int, email: str, alias_id: str, token_id: str):
        await self.conn.execute(
            """
            INSERT INTO active_emails (user_id, email, alias_id, token_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, email.lower(), alias_id, token_id, time.time()),
        )
        await self.conn.commit()

    async def email_exists(self, email: str) -> bool:
        c = await self.conn.execute(
            "SELECT 1 FROM active_emails WHERE email = ?", (email.lower(),)
        )
        return (await c.fetchone()) is not None

    async def get_user_emails(self, user_id: int) -> List[Dict]:
        cursor = await self.conn.execute(
            """
            SELECT id, email, alias_id, token_id, created_at
            FROM active_emails
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_all_active_alias_map(self) -> Dict[str, int]:
        cursor = await self.conn.execute("SELECT email, user_id FROM active_emails")
        return {r["email"]: r["user_id"] for r in await cursor.fetchall()}

    async def get_email_by_id(self, row_id: int, user_id: int) -> Optional[Dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM active_emails WHERE id = ? AND user_id = ?",
            (row_id, user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_email_record(self, row_id: int, user_id: int):
        await self.conn.execute(
            "DELETE FROM active_emails WHERE id = ? AND user_id = ?",
            (row_id, user_id),
        )
        await self.conn.commit()

    async def save_inbox_message(
        self, msg_uid: str, alias: str, sender: str, subject: str, otp: str, body: str
    ) -> bool:
        cursor = await self.conn.execute(
            """
            INSERT OR IGNORE INTO inbox_messages
            (msg_uid, alias, sender, subject, otp, body, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (msg_uid, alias.lower(), sender, subject, otp, body, time.time()),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_messages_for_aliases(self, aliases: List[str], limit: int = 5) -> List[Dict]:
        if not aliases:
            return []
        placeholders = ",".join("?" for _ in aliases)
        query = f"""
            SELECT alias, sender, subject, otp, body, received_at
            FROM inbox_messages
            WHERE alias IN ({placeholders})
            ORDER BY received_at DESC
            LIMIT ?
        """
        params = [a.lower() for a in aliases] + [limit]
        cursor = await self.conn.execute(query, params)
        return [dict(r) for r in await cursor.fetchall()]

    async def purge_old_messages(self, max_age_seconds: int = 86400):
        cutoff = time.time() - max_age_seconds
        await self.conn.execute(
            "DELETE FROM inbox_messages WHERE received_at < ?", (cutoff,)
        )
        await self.conn.commit()

    async def upsert_token(self, token_id: str, bearer_token: str):
        await self.conn.execute(
            """
            INSERT INTO ddg_tokens (token_id, bearer_token, exhausted_until, is_valid)
            VALUES (?, ?, 0, 1)
            ON CONFLICT(token_id) DO UPDATE SET
                bearer_token = excluded.bearer_token,
                is_valid = 1
            """,
            (token_id, bearer_token),
        )
        await self.conn.commit()

    async def get_all_tokens(self) -> List[Dict]:
        cursor = await self.conn.execute("SELECT * FROM ddg_tokens")
        return [dict(r) for r in await cursor.fetchall()]

    async def update_token_status(
        self, token_id: str, exhausted_until: Optional[float] = None, is_valid: Optional[int] = None
    ):
        if exhausted_until is not None:
            await self.conn.execute(
                "UPDATE ddg_tokens SET exhausted_until = ? WHERE token_id = ?",
                (exhausted_until, token_id),
            )
        if is_valid is not None:
            await self.conn.execute(
                "UPDATE ddg_tokens SET is_valid = ? WHERE token_id = ?",
                (is_valid, token_id),
            )
        await self.conn.commit()

    async def get_global_stats(self) -> Dict[str, int]:
        (users,) = await (await self.conn.execute("SELECT COUNT(*) FROM users")).fetchone()
        (prem,) = await (
            await self.conn.execute("SELECT COUNT(*) FROM users WHERE max_slots > 3")
        ).fetchone()
        (emails,) = await (
            await self.conn.execute("SELECT COUNT(*) FROM active_emails")
        ).fetchone()
        (banned,) = await (
            await self.conn.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
        ).fetchone()
        return {
            "users": users,
            "premium_users": prem,
            "active_emails": emails,
            "banned_users": banned,
        }
