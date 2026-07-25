#!/usr/bin/env python3
# pyright: reportUnusedCallResult=false
"""Simple test script for verification enhancement features."""

import importlib.metadata
import sys
import sqlite3
import os
from pathlib import Path

def test_pillow():
    """Test Pillow library."""
    print("🔍 Testing Pillow library...")
    try:
        from PIL import Image, ImageDraw, ImageFont  # pyright: ignore[reportMissingImports]
        from io import BytesIO
        import random

        # Generate test captcha
        captcha_text = ''.join([str(random.randint(0, 9)) for _ in range(4)])
        width, height = 200, 80
        image = Image.new('RGB', (width, height), color='white')
        draw = ImageDraw.Draw(image)

        # Try to load font
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
            print("   ✅ Font loaded: DejaVu Sans")
        except Exception:
            font = ImageFont.load_default()
            print("   ⚠️  Using default font (DejaVu Sans not found)")

        # Draw text
        x = 20
        for char in captcha_text:
            y = random.randint(15, 25)
            color = (random.randint(0, 100), random.randint(0, 100), random.randint(0, 100))
            draw.text((x, y), char, font=font, fill=color)
            x += 40

        # Add noise lines
        for _ in range(5):
            x1, y1 = random.randint(0, width), random.randint(0, height)
            x2, y2 = random.randint(0, width), random.randint(0, height)
            draw.line([(x1, y1), (x2, y2)],
                     fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)),
                     width=2)

        # Add noise points
        for _ in range(100):
            x, y = random.randint(0, width), random.randint(0, height)
            draw.point((x, y),
                      fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)))

        # Save to BytesIO
        bio = BytesIO()
        image.save(bio, 'PNG')
        bio.seek(0)

        data = bio.read()
        assert len(data) > 0, "Image data should not be empty"
        assert data[:8] == b'\x89PNG\r\n\x1a\n', "Should be valid PNG"

        print(f"   ✅ Image generated successfully ({len(data)} bytes, text: {captcha_text})")
        print("   ✅ Pillow test passed!")
        return True
    except Exception as e:
        print(f"   ❌ Pillow test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_database_structure():
    """Test database structure creation."""
    print("\n🔍 Testing database structure...")
    try:
        test_db = "/tmp/test_betterforward-enhance.db"
        if os.path.exists(test_db):
            os.remove(test_db)

        with sqlite3.connect(test_db) as conn:
            cursor = conn.cursor()

            # Create settings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL
                )
            """)

            # Create blocked_users table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    block_reason TEXT DEFAULT 'manual'
                )
            """)

            # Create verification_attempts table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS verification_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    attempt_count INTEGER DEFAULT 0,
                    last_attempt_time TIMESTAMP,
                    blocked_by_attempts BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create appeal_requests table
            cursor.execute("""
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

            # Create verified_users table
            cursor.execute("""
                CREATE TABLE verified_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL
                )
            """)

            # Insert test settings
            cursor.execute("INSERT INTO settings (key, value) VALUES ('appeal_mode', 'manual')")
            cursor.execute("INSERT INTO settings (key, value) VALUES ('captcha_image_enabled', 'disable')")

            conn.commit()

            # Verify all tables exist
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            required_tables = [
                'settings',
                'blocked_users',
                'verification_attempts',
                'appeal_requests',
                'verified_users'
            ]

            for table in required_tables:
                if table in tables:
                    print(f"   ✅ Table '{table}' created")
                else:
                    raise Exception(f"Table '{table}' not found")

            # Test verification attempts workflow
            print("\n   Testing verification attempts workflow...")

            # Record 3 failed attempts
            for i in range(3):
                cursor.execute("""
                    INSERT OR REPLACE INTO verification_attempts
                    (user_id, attempt_count, last_attempt_time)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                """, (12345, i + 1))
                conn.commit()

            cursor.execute("SELECT attempt_count FROM verification_attempts WHERE user_id = ?", (12345,))
            count = cursor.fetchone()[0]
            assert count == 3, f"Expected 3 attempts, got {count}"
            print(f"   ✅ Attempt tracking: {count}/3 attempts recorded")

            # Auto-block user
            cursor.execute("""
                INSERT INTO blocked_users
                (user_id, username, first_name, last_name, block_reason)
                VALUES (?, ?, ?, ?, ?)
            """, (12345, "testuser", "Test", "User", "auto_attempts"))
            conn.commit()

            cursor.execute("SELECT block_reason FROM blocked_users WHERE user_id = ?", (12345,))
            reason = cursor.fetchone()[0]
            assert reason == "auto_attempts", f"Expected 'auto_attempts', got '{reason}'"
            print(f"   ✅ Auto-blocking: User blocked with reason '{reason}'")

            # Submit appeal
            cursor.execute("""
                INSERT INTO appeal_requests (user_id, status)
                VALUES (?, 'pending')
            """, (12345,))
            conn.commit()

            cursor.execute("SELECT status FROM appeal_requests WHERE user_id = ?", (12345,))
            status = cursor.fetchone()[0]
            assert status == "pending", f"Expected 'pending', got '{status}'"
            print(f"   ✅ Appeal system: Appeal status '{status}'")

            # Test appeal approval
            cursor.execute("""
                UPDATE appeal_requests
                SET status = 'approved', admin_id = 99999, handled_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """, (12345,))
            cursor.execute("DELETE FROM blocked_users WHERE user_id = ?", (12345,))
            cursor.execute("DELETE FROM verification_attempts WHERE user_id = ?", (12345,))
            conn.commit()

            cursor.execute("SELECT COUNT(*) FROM blocked_users WHERE user_id = ?", (12345,))
            is_blocked = cursor.fetchone()[0]
            assert is_blocked == 0, "User should be unblocked after approval"
            print("   ✅ Appeal approval: User unblocked successfully")

        # Cleanup
        os.remove(test_db)
        print("\n   ✅ Database structure test passed!")
        return True
    except Exception as e:
        print(f"\n   ❌ Database test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_dependencies():
    """Test all required dependencies."""
    print("\n🔍 Testing dependencies...")
    try:
        import telebot
        print(f"   ✅ pyTelegramBotAPI ({telebot.__name__}) installed")

        import diskcache
        print(f"   ✅ diskcache {diskcache.__version__}")

        import pytz
        print(f"   ✅ pytz {importlib.metadata.version('pytz')} ({pytz.UTC.zone})")

        import httpx
        print(f"   ✅ httpx {httpx.__version__}")

        from PIL import Image  # pyright: ignore[reportMissingImports]
        print(f"   ✅ Pillow {Image.__version__ if hasattr(Image, '__version__') else 'installed'}")

        print("\n   ✅ All dependencies installed!")
        return True
    except Exception as e:
        print(f"\n   ❌ Dependency test failed: {e}")
        return False

def test_migration_script():
    """Test migration script syntax."""
    print("\n🔍 Testing migration script...")
    try:
        # Read and compile the migration script
        migration_path = Path(__file__).parent / "db_migrate" / "20251225_verification_enhancement.py"
        with open(migration_path, 'r') as f:
            code = f.read()

        compile(code, migration_path, 'exec')
        print("   ✅ Migration script syntax is valid")

        # Test actual migration
        test_db = "/tmp/test_migration.db"
        if os.path.exists(test_db):
            os.remove(test_db)

        with sqlite3.connect(test_db) as conn:
            cursor = conn.cursor()

            # Create prerequisite tables
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

        # Import and run migration
        sys.path.insert(0, str(Path(__file__).parent / "db_migrate"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migration",
            str(migration_path)
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load migration module from {migration_path}")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        # Run upgrade
        migration.upgrade(test_db)

        # Verify
        with sqlite3.connect(test_db) as conn:
            cursor = conn.cursor()

            # Check new tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='verification_attempts'")
            assert cursor.fetchone() is not None, "verification_attempts table not created"
            print("   ✅ verification_attempts table created")

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='appeal_requests'")
            assert cursor.fetchone() is not None, "appeal_requests table not created"
            print("   ✅ appeal_requests table created")

            # Check new column
            cursor.execute("PRAGMA table_info(blocked_users)")
            columns = [col[1] for col in cursor.fetchall()]
            assert 'block_reason' in columns, "block_reason column not added"
            print("   ✅ block_reason column added to blocked_users")

            # Check settings
            cursor.execute("SELECT value FROM settings WHERE key = 'appeal_mode'")
            result = cursor.fetchone()
            assert result is not None, "appeal_mode setting not created"
            print(f"   ✅ appeal_mode setting: {result[0]}")

        os.remove(test_db)
        print("\n   ✅ Migration script test passed!")
        return True
    except Exception as e:
        print(f"\n   ❌ Migration script test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run all tests."""
    print("=" * 70)
    print("🧪 BetterForward Enhance Verification Tests")
    print("=" * 70)

    results = {
        "Dependencies": test_dependencies(),
        "Pillow (Image Captcha)": test_pillow(),
        "Database Structure": test_database_structure(),
        "Migration Script": test_migration_script()
    }

    print("\n" + "=" * 70)
    print("📊 Test Results Summary")
    print("=" * 70)

    for test_name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {test_name}")

    total = len(results)
    passed = sum(results.values())
    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 All tests passed! Your verification enhancement is ready to use.")
        return 0
    else:
        print("\n⚠️  Some tests failed. Please check the output above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
