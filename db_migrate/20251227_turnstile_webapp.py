"""Add persistent Telegram WebApp and Turnstile settings."""

import sqlite3


DEFAULTS = {
    "webapp_configured": "no",
    "webapp_enabled": "disable",
    "webapp_public_url": "",
    "turnstile_site_key": "",
    "turnstile_secret_key": "",
    "turnstile_hostname": "",
    "webapp_host": "0.0.0.0",
    "webapp_port": "8080",
    "webapp_auth_max_age": "300",
}


def upgrade(db_path):
    with sqlite3.connect(db_path) as connection:
        cursor = connection.cursor()
        for key, value in DEFAULTS.items():
            cursor.execute(
                "INSERT INTO settings (key, value) "
                "SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key = ?)",
                (key, value, key),
            )
        connection.commit()
