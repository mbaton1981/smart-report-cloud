"""Shared, fail-closed web PIN attempt budget; no credentials are persisted."""
import hashlib
import time
import uuid


class SQLiteLoginThrottle:
    def __init__(self, connection_factory, limit=8, window=300):
        self.connection_factory = connection_factory
        self.limit = limit
        self.window = window

    @staticmethod
    def _key(scope, *values):
        digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()
        return scope + ":" + digest

    def reserve(self, username, address):
        """Reserve before password verification so concurrent workers cannot overrun."""
        account = username.casefold()
        keys = (
            self._key("account", account),
            self._key("account-ip", account, address),
            self._key("ip", address),
        )
        connection = self.connection_factory()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS web_login_attempts (
                    attempt_id TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    attempted_at REAL NOT NULL,
                    PRIMARY KEY (attempt_id, bucket)
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_web_login_bucket ON web_login_attempts(bucket, attempted_at)"
            )
            now = time.time()
            connection.execute("DELETE FROM web_login_attempts WHERE attempted_at <= ?", (now - self.window,))
            for key in keys:
                count = connection.execute(
                    "SELECT COUNT(*) FROM web_login_attempts WHERE bucket = ?", (key,)
                ).fetchone()[0]
                if count >= self.limit:
                    connection.commit()
                    return None
            attempt_id = uuid.uuid4().hex
            connection.executemany(
                "INSERT INTO web_login_attempts(attempt_id,bucket,attempted_at) VALUES(?,?,?)",
                [(attempt_id, key, now) for key in keys],
            )
            connection.commit()
            return attempt_id
        finally:
            connection.close()

    def complete_success(self, attempt_id):
        # Remove only this reservation, never concurrent failures or account history.
        connection = self.connection_factory()
        try:
            connection.execute("DELETE FROM web_login_attempts WHERE attempt_id = ?", (attempt_id,))
            connection.commit()
        finally:
            connection.close()
