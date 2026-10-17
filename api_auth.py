"""Серверные токены доступа для настольного клиента: хранится только SHA-256 токена."""
import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

TOKEN_TTL_DAYS = 30
_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_token_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            device_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked INTEGER NOT NULL DEFAULT 0
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id)")


def issue_token(connection, user_id, device_name="", ttl_days=TOKEN_TTL_DAYS):
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(days=ttl_days)
    connection.execute(
        "DELETE FROM api_tokens WHERE revoked = 1 OR expires_at < ?", (now.strftime(_FORMAT),)
    )
    connection.execute(
        "INSERT INTO api_tokens (token_hash, user_id, device_name, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (hash_token(token), user_id, str(device_name)[:100], now.strftime(_FORMAT), expires.strftime(_FORMAT)),
    )
    connection.commit()
    return token, expires.strftime(_FORMAT)


def lookup_token(connection, token):
    """Возвращает пользователя активного токена или None."""
    if not token or len(token) > 200:
        return None
    now = _now().strftime(_FORMAT)
    row = connection.execute(
        """SELECT t.id AS token_id, u.id AS user_id, u.username, u.role, u.employee_id
           FROM api_tokens t JOIN users u ON u.id = t.user_id
           WHERE t.token_hash = ? AND t.revoked = 0 AND t.expires_at > ? AND u.is_active = 1""",
        (hash_token(token), now),
    ).fetchone()
    if row is None:
        return None
    connection.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now, row["token_id"]))
    connection.commit()
    return dict(row)


def revoke_token(connection, token):
    cursor = connection.execute("UPDATE api_tokens SET revoked = 1 WHERE token_hash = ?", (hash_token(token or ""),))
    connection.commit()
    return cursor.rowcount > 0


class LoginThrottle:
    """Ограничение подбора пароля: не более limit неудач за window секунд на ключ."""

    def __init__(self, limit=8, window=300):
        self.limit = limit
        self.window = window
        self._failures = {}
        self._lock = threading.Lock()

    def _recent(self, key):
        cutoff = time.monotonic() - self.window
        recent = [stamp for stamp in self._failures.get(key, []) if stamp > cutoff]
        self._failures[key] = recent
        return recent

    def blocked(self, key):
        with self._lock:
            return len(self._recent(key)) >= self.limit

    def record_failure(self, key):
        with self._lock:
            self._recent(key).append(time.monotonic())

    def reset(self, key):
        with self._lock:
            self._failures.pop(key, None)
