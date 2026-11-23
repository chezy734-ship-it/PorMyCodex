# -*- coding: utf-8 -*-
"""
בדיקות יציבות ומניעת קריסות (Stability & Crash Protection Regression Tests):
1. Windows-safe atomic_write_bytes & save_settings retry בעת PermissionError / WinError 5 חולף
2. Thread-safety של LibraryStore תחת קריאה וכתיבה מקביליות מרובות חוטים
3. ביצועי O(1) ב-assess_book_pipeline על ספר גדול (140 פרקים) ללא קפיאת GUI
4. ניקוי מפתחות API ותיעוד נקי ב-crash.log דרך main.py exception hooks
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from unittest import mock

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from export_pipeline import assess_book_pipeline  # noqa: E402
from library_store import LibraryStore, atomic_write_bytes  # noqa: E402
from main import (  # noqa: E402
    get_crash_log_path,
    log_unhandled_exception,
    sanitize_crash_text,
)
from settings_manager import DEFAULT_SETTINGS, load_settings, save_settings  # noqa: E402


class TestStabilityAndCrashProtection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.prev_env = os.environ.get("PORMYCODEX_DATA_DIR")
        os.environ["PORMYCODEX_DATA_DIR"] = os.path.join(self.temp_dir.name, "UserData")
        self.store = LibraryStore()

    def tearDown(self):
        if self.prev_env is None:
            os.environ.pop("PORMYCODEX_DATA_DIR", None)
        else:
            os.environ["PORMYCODEX_DATA_DIR"] = self.prev_env
        self.temp_dir.cleanup()

    def test_01_atomic_write_and_settings_retry_on_windows_permission_error(self):
        target = os.path.join(self.temp_dir.name, "locked_state.json")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PermissionError(13, "Access is denied (simulated WinError 5)")
            return real_replace(src, dst)

        with mock.patch("library_store.os.replace", side_effect=flaky_replace), \
             mock.patch("library_store.time.sleep"):
            atomic_write_bytes(target, b'{"ok": true}')

        self.assertEqual(calls["n"], 4)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b'{"ok": true}')

        # גם save_settings חייב להתאושש מ-PermissionError חולף על Windows
        calls_s = {"n": 0}

        def flaky_replace_settings(src, dst):
            calls_s["n"] += 1
            if calls_s["n"] <= 2:
                raise PermissionError(13, "Sharing violation")
            return real_replace(src, dst)

        cfg = deepcopy(DEFAULT_SETTINGS)
        cfg["active_provider"] = "openai"
        with mock.patch("settings_manager.os.replace", side_effect=flaky_replace_settings), \
             mock.patch("settings_manager.time.sleep"):
            save_settings(cfg)

        self.assertEqual(calls_s["n"], 3)
        self.assertEqual(load_settings()["active_provider"], "openai")

    def test_02_library_store_concurrent_read_write_thread_safety(self):
        book = {
            "id": "conc_book",
            "title": "ספר מקבילי",
            "chapters": [{"title": "פרק 1", "text": "One. Two. Three. Four."}],
        }
        self.store.save_books_and_progress([book], {"active_book_id": "conc_book"})
        errors = []

        def writer_progress():
            try:
                for i in range(15):
                    self.store.update_reading_progress("conc_book", 0, i % 4, font_size=18 + (i % 3))
            except Exception as exc:
                errors.append(exc)

        def writer_units():
            try:
                sents = ["One.", "Two.", "Three.", "Four."]
                for idx in range(4):
                    self.store.save_translation_unit_progress(
                        book_id="conc_book",
                        chapter_index=0,
                        source_sentences=sents,
                        source_hash="h_conc",
                        config_hash="c_conc",
                        source_lang="en",
                        target_lang="he",
                        unit_index=idx,
                        unit_status="completed",
                        translated_text=f"תרגום {idx}",
                    )
            except Exception as exc:
                errors.append(exc)

        def reader_loop():
            try:
                for _ in range(25):
                    _ = self.store.get_reading_progress()
                    _ = self.store.get_chapter_artifacts("conc_book", 0)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer_progress),
            threading.Thread(target=writer_units),
            threading.Thread(target=reader_loop),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [])
        art = self.store.get_chapter_artifacts("conc_book", 0)
        tp = art.get("translation_progress") or {}
        self.assertEqual(tp.get("completed_count"), 4)

    def test_03_assess_140_chapter_book_is_fast_and_non_blocking(self):
        chapters = [
            {"title": f"פרק {i}", "text": " ".join(f"Sentence {j} in chapter {i}." for j in range(1, 21))}
            for i in range(1, 141)
        ]
        big_book = {"id": "pdf_140", "title": "ספר 140 פרקים", "chapters": chapters}
        self.store.save_books_and_progress([big_book], {"active_book_id": "pdf_140"})

        # הערכה ראשונה + חוזרת על 140 פרקים חייבת להיות מהירה (ללא 280 deepcopy של כל הקובץ)
        t0 = time.monotonic()
        res1 = assess_book_pipeline(book=big_book, settings=deepcopy(DEFAULT_SETTINGS), store=self.store)
        res2 = assess_book_pipeline(book=big_book, settings=deepcopy(DEFAULT_SETTINGS), store=self.store)
        elapsed = time.monotonic() - t0

        self.assertEqual(res1["chapter_count"], 140)
        self.assertEqual(res2["total_translation_units"], 140 * 20)
        self.assertLess(elapsed, 4.0, f"assess_book_pipeline on 140 chapters took too long: {elapsed:.2f}s")

    def test_04_crash_logger_sanitizes_api_keys_and_writes_crash_log(self):
        secret_gem = "AIza" + "SyDummyTestGeminiKey_1234567890"
        secret_oai = "sk-" + "proj-DummyTestOpenAIKey987654321"
        raw = f"Boom with key={secret_gem} and Bearer {secret_oai}"
        cleaned = sanitize_crash_text(raw)
        self.assertNotIn(secret_gem, cleaned)
        self.assertNotIn(secret_oai, cleaned)
        self.assertIn("[REDACTED]", cleaned)

        try:
            raise RuntimeError(f"Failure exposing {secret_gem}")
        except RuntimeError:
            et, ev, tb = sys.exc_info()
            msg = log_unhandled_exception(et, ev, tb, context="TestSlot")

        self.assertNotIn(secret_gem, msg)
        log_path = get_crash_log_path()
        self.assertTrue(os.path.exists(log_path))
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Unhandled TestSlot Exception", content)
        self.assertNotIn(secret_gem, content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
