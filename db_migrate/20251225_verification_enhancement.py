import sqlite3
import logging

logger = logging.getLogger()


def upgrade(db_path):
    with sqlite3.connect(db_path) as conn:
        db_cursor = conn.cursor()

        # Create verification_attempts table
        try:
            db_cursor.execute("""
                CREATE TABLE IF NOT EXISTS verification_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    attempt_count INTEGER DEFAULT 0,
                    last_attempt_time TIMESTAMP,
                    blocked_by_attempts BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            logger.info("Created verification_attempts table")
        except Exception as e:
            logger.error(f"Failed to create verification_attempts table: {e}")
            conn.rollback()

        # Create appeal_requests table
        try:
            db_cursor.execute("""
                CREATE TABLE IF NOT EXISTS appeal_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    appeal_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    appeal_message TEXT,
                    status TEXT DEFAULT 'pending',
                    admin_id INTEGER,
                    handled_at TIMESTAMP
                )
            """)
            conn.commit()
            logger.info("Created appeal_requests table")
        except Exception as e:
            logger.error(f"Failed to create appeal_requests table: {e}")
            conn.rollback()

        # Add block_reason column to blocked_users table
        try:
            # Check if column already exists
            db_cursor.execute("PRAGMA table_info(blocked_users)")
            columns = db_cursor.fetchall()
            has_block_reason = any(col[1] == 'block_reason' for col in columns)

            if not has_block_reason:
                db_cursor.execute("""
                    ALTER TABLE blocked_users
                    ADD COLUMN block_reason TEXT DEFAULT 'manual'
                """)
                conn.commit()
                logger.info("Added block_reason column to blocked_users table")
            else:
                logger.info("block_reason column already exists in blocked_users table")
        except Exception as e:
            logger.error(f"Failed to add block_reason column to blocked_users table: {e}")
            conn.rollback()

        # Add appeal mode setting
        try:
            db_cursor.execute("""
                INSERT OR IGNORE INTO settings (key, value)
                VALUES ('appeal_mode', 'manual')
            """)
            conn.commit()
            logger.info("Added appeal_mode setting")
        except Exception as e:
            logger.error(f"Failed to add appeal_mode setting: {e}")
            conn.rollback()

        # Add image captcha setting
        try:
            db_cursor.execute("""
                INSERT OR IGNORE INTO settings (key, value)
                VALUES ('captcha_image_enabled', 'disable')
            """)
            conn.commit()
            logger.info("Added captcha_image_enabled setting")
        except Exception as e:
            logger.error(f"Failed to add captcha_image_enabled setting: {e}")
            conn.rollback()
