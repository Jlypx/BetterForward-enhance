"""Add expiry support for automatic ingress rate-limit blocks."""

import sqlite3


def upgrade(db_path):
    with sqlite3.connect(db_path) as connection:
        cursor = connection.cursor()
        columns = {column[1] for column in cursor.execute("PRAGMA table_info(blocked_users)")}

        if "blocked_until" not in columns:
            cursor.execute("ALTER TABLE blocked_users ADD COLUMN blocked_until TIMESTAMP")

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocked_users_until ON blocked_users(blocked_until)"
        )
        connection.commit()
