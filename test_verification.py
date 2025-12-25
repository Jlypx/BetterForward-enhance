#!/usr/bin/env python3
"""Test script for verification enhancement features."""

import sys
import sqlite3
import os
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

def test_imports():
    """Test that all modules can be imported."""
    print("🔍 Testing imports...")
    try:
        # Import only the modules we need, not the whole bot
        sys.path.insert(0, str(Path(__file__).parent / "src"))
        from utils.captcha import CaptchaManager
        from PIL import Image, ImageDraw, ImageFont
        print("✅ All imports successful!")
        return True
    except Exception as e:
        print(f"❌ Import failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_database_migration():
    """Test database migration script."""
    print("\n🔍 Testing database migration...")
    try:
        # Create test database
        test_db = "/tmp/test_betterforward.db"
        if os.path.exists(test_db):
            os.remove(test_db)

        # Initialize basic tables
        with sqlite3.connect(test_db) as conn:
            cursor = conn.cursor()

            # Create settings table (prerequisite)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL
                )
            """)

            # Create blocked_users table (prerequisite)
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

        # Run migration
        from db_migrate import migration_20251225_verification_enhancement
        migration_20251225_verification_enhancement.upgrade(test_db)

        # Verify tables were created
        with sqlite3.connect(test_db) as conn:
            cursor = conn.cursor()

            # Check verification_attempts table
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='verification_attempts'")
            if not cursor.fetchone():
                raise Exception("verification_attempts table not created")

            # Check appeal_requests table
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='appeal_requests'")
            if not cursor.fetchone():
                raise Exception("appeal_requests table not created")

            # Check block_reason column
            cursor.execute("PRAGMA table_info(blocked_users)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'block_reason' not in columns:
                raise Exception("block_reason column not added")

            print("✅ Database migration successful!")
            print("   - verification_attempts table created")
            print("   - appeal_requests table created")
            print("   - block_reason column added")

        # Cleanup
        os.remove(test_db)
        return True
    except Exception as e:
        print(f"❌ Database migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_captcha_manager():
    """Test CaptchaManager functionality."""
    print("\n🔍 Testing CaptchaManager...")
    try:
        sys.path.insert(0, str(Path(__file__).parent / "src"))
        from utils.captcha import CaptchaManager
        from diskcache import Cache

        # Create mock bot
        class MockBot:
            def send_message(self, *args, **kwargs):
                pass
            def send_photo(self, *args, **kwargs):
                pass

        cache = Cache("/tmp/test_cache")
        bot = MockBot()
        manager = CaptchaManager(bot, cache)

        # Test math captcha
        captcha = manager.generate_captcha(12345, "math")
        assert captcha is not None, "Math captcha should return a question"
        assert "+" in captcha, "Math captcha should contain +"
        assert "?" in captcha, "Math captcha should contain ?"
        print("✅ Math captcha generation works")

        # Test captcha verification
        cache.set("captcha_12345", 50, 300)
        assert manager.verify_captcha(12345, "50") == True, "Verification should work"
        assert manager.verify_captcha(12345, "49") == False, "Wrong answer should fail"
        print("✅ Captcha verification works")

        # Test attempt tracking
        test_db = "/tmp/test_attempts.db"
        if os.path.exists(test_db):
            os.remove(test_db)

        with sqlite3.connect(test_db) as db:
            cursor = db.cursor()
            cursor.execute("""
                CREATE TABLE verification_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    attempt_count INTEGER DEFAULT 0,
                    last_attempt_time TIMESTAMP,
                    blocked_by_attempts BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE blocked_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    block_reason TEXT,
                    blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE verified_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL
                )
            """)
            db.commit()

            # Test record_attempt
            count = manager.record_attempt(99999, db)
            assert count == 1, f"First attempt should be 1, got {count}"

            count = manager.record_attempt(99999, db)
            assert count == 2, f"Second attempt should be 2, got {count}"

            count = manager.record_attempt(99999, db)
            assert count == 3, f"Third attempt should be 3, got {count}"
            print("✅ Attempt tracking works")

            # Test get_attempt_count
            stored_count = manager.get_attempt_count(99999, db)
            assert stored_count == 3, f"Stored count should be 3, got {stored_count}"
            print("✅ Get attempt count works")

            # Test reset_attempts
            manager.reset_attempts(99999, db)
            stored_count = manager.get_attempt_count(99999, db)
            assert stored_count == 0, f"After reset, count should be 0, got {stored_count}"
            print("✅ Reset attempts works")

            # Test block_user_by_attempts
            manager.record_attempt(88888, db)
            manager.record_attempt(88888, db)
            manager.record_attempt(88888, db)
            result = manager.block_user_by_attempts(88888, "testuser", "Test", "User", db)
            assert result == True, "Block should succeed"

            cursor.execute("SELECT block_reason FROM blocked_users WHERE user_id = ?", (88888,))
            reason = cursor.fetchone()
            assert reason and reason[0] == "auto_attempts", "Block reason should be auto_attempts"
            print("✅ Auto-blocking works")

        # Cleanup
        os.remove(test_db)
        cache.close()

        return True
    except Exception as e:
        print(f"❌ CaptchaManager test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_image_captcha():
    """Test image captcha generation."""
    print("\n🔍 Testing image captcha generation...")
    try:
        from PIL import Image, ImageDraw, ImageFont
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
            print("✅ Font loaded successfully")
        except Exception:
            font = ImageFont.load_default()
            print("⚠️  Using default font (DejaVu Sans not found)")

        # Draw text
        x = 20
        for char in captcha_text:
            y = random.randint(15, 25)
            color = (random.randint(0, 100), random.randint(0, 100), random.randint(0, 100))
            draw.text((x, y), char, font=font, fill=color)
            x += 40

        # Add noise
        for _ in range(5):
            x1, y1 = random.randint(0, width), random.randint(0, height)
            x2, y2 = random.randint(0, width), random.randint(0, height)
            draw.line([(x1, y1), (x2, y2)],
                     fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)),
                     width=2)

        # Save to BytesIO
        bio = BytesIO()
        image.save(bio, 'PNG')
        bio.seek(0)

        assert bio.tell() == 0, "BytesIO should be at position 0"
        data = bio.read()
        assert len(data) > 0, "Image data should not be empty"
        assert data[:8] == b'\x89PNG\r\n\x1a\n', "Should be valid PNG"

        print(f"✅ Image captcha generated successfully ({len(data)} bytes)")
        return True
    except Exception as e:
        print(f"❌ Image captcha test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run all tests."""
    print("=" * 60)
    print("🧪 BetterForward Verification Enhancement Tests")
    print("=" * 60)

    results = {
        "Imports": test_imports(),
        "Database Migration": test_database_migration(),
        "CaptchaManager": test_captcha_manager(),
        "Image Captcha": test_image_captcha()
    }

    print("\n" + "=" * 60)
    print("📊 Test Results Summary")
    print("=" * 60)

    for test_name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {test_name}")

    total = len(results)
    passed = sum(results.values())
    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print("\n⚠️  Some tests failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())
