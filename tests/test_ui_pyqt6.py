# -*- coding: utf-8 -*-
"""
בדיקות ממשק PyQt6 אמיתיות (רצות אוטומטית על Windows CI וכל סביבה עם PyQt6 מותקן):
1. דיאלוג ההגדרות (SettingsDialog):
   - הזנת/הדבקת מפתח מוסתרת (Password), מפעילה גילוי מודלים עם debounce וממלאת QComboBox גלוי ושמיש
   - החלפת חברת AI / החלפת מפתח מבטלת גילוי קודם ואינה מאפשרת לתוצאה ישנה לדרוס מצב עדכני
   - רענון וניסיון חוזר; הודעות מובחנות לשגיאת מפתח/מכסה/רשת/Timeout/תשובה ריקה ללא דליפת מפתח API
2. חלון ראשי ודיאלוגים של הספר כולו (MainWindow / ScriptStudioDialog / ExportPipelineDialog):
   - בחירת היקף 'הפרק הנוכחי' / 'הספר כולו' וכפתורי 'תסריט לכל הספר' / 'שמע לכל הספר'
   - בדיקת חיבור הפעולות מהכפתורים אל צינור הספר השלם
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QLineEdit
    HAS_PYQT6 = True
except ImportError:
    HAS_PYQT6 = False

if HAS_PYQT6:
    from ai_providers import AICallError
    from dialogs import ExportPipelineDialog, ScriptStudioDialog, SettingsDialog
    from main_window import MainWindow
    from settings_manager import DEFAULT_SETTINGS


@unittest.skipUnless(HAS_PYQT6, "PyQt6 not installed in headless container environment")
class TestPyQt6ModelDiscoveryAndWholeBookUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.prev_env = os.environ.get("PORMYCODEX_DATA_DIR")
        os.environ["PORMYCODEX_DATA_DIR"] = os.path.join(self.temp_dir.name, "UserData")

    def tearDown(self):
        if self.prev_env is None:
            os.environ.pop("PORMYCODEX_DATA_DIR", None)
        else:
            os.environ["PORMYCODEX_DATA_DIR"] = self.prev_env
        self.temp_dir.cleanup()

    def _pump_events_until(self, predicate, timeout_sec: float = 4.0):
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        self.app.processEvents()
        return predicate()

    def test_01_settings_dialog_debounced_discovery_password_masking_and_stale_response_guard(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        secret_key = "AIzaSecretUIKey_99887766"
        discovery_calls = []

        def fake_discover(provider_id, api_key, base_url, cancel_event=None):
            discovery_calls.append((provider_id, api_key, base_url))
            if provider_id == "gemini":
                if api_key == "OLD_KEY":
                    time.sleep(0.25)
                    return {"models": ["stale-gemini-old"], "pages_fetched": 1}
                return {"models": ["gemini-2.5-flash", "gemini-2.5-pro"], "pages_fetched": 1}
            if provider_id == "openai":
                return {"models": ["gpt-4o", "gpt-4o-mini"], "pages_fetched": 1}
            raise AICallError("Auth failed", stage="גילוי מודלים", category="auth")

        dlg = SettingsDialog(None, settings, discovery_fn=fake_discover)
        dlg.show()
        self.app.processEvents()

        # הזנת מפתח API מוסתרת (EchoMode.Password) ו-QComboBox גלוי ולא נעול לעריכה חופשית כפויה
        self.assertEqual(dlg.txt_api_key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertTrue(dlg.combo_model.isVisible())
        self.assertFalse(dlg.combo_model.isEditable())

        # הקלדה/הדבקה של מפתח ישן -> גילוי -> תוך כדי מחליפים מפתח מהר למפתח החדש!
        dlg.txt_api_key.setText("OLD_KEY")
        dlg.btn_discover.click()
        dlg.txt_api_key.setText(secret_key)
        dlg._run_model_discovery()

        ok = self._pump_events_until(
            lambda: "gemini-2.5-flash" in [dlg.combo_model.itemText(i) for i in range(dlg.combo_model.count())],
            timeout_sec=3.0,
        )
        self.assertTrue(ok)
        # ממתין גם אחרי שה-OLD_KEY thread הישן סיים כדי לוודא שלעולם לא ידרוס את הרשימה העדכנית!
        self._pump_events_until(lambda: False, timeout_sec=0.35)
        items = [dlg.combo_model.itemText(i) for i in range(dlg.combo_model.count())]
        self.assertEqual(items, ["gemini-2.5-flash", "gemini-2.5-pro"])
        self.assertNotIn("stale-gemini-old", items)
        self.assertTrue(dlg.combo_model.isEnabled())

        # בדיקת רענון עם כפתור הרענון והחלפת חברה (OpenAI) + debounce אוטומטי
        openai_idx = dlg.combo_provider.findData("openai")
        dlg.combo_provider.setCurrentIndex(openai_idx)
        dlg.txt_api_key.setText("sk-live-openai-test-123456")
        ok_oa = self._pump_events_until(
            lambda: "gpt-4o" in [dlg.combo_model.itemText(i) for i in range(dlg.combo_model.count())],
            timeout_sec=3.0,
        )
        self.assertTrue(ok_oa, f"OpenAI models not discovered automatically. Items: {[dlg.combo_model.itemText(i) for i in range(dlg.combo_model.count())]}")
        dlg.close()

    def test_02_settings_dialog_distinguished_errors_and_never_leaks_api_key(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        secret_key = "AIzaNeverAppearInErrorText_ABC123"

        def error_discover(provider_id, api_key, base_url, cancel_event=None):
            raise AICallError(
                "השרת Google Gemini לא החזיר תשובה בזמן (Read Timeout=15s)",
                stage="גילוי מודלים",
                category="timeout",
            )

        dlg = SettingsDialog(None, settings, discovery_fn=error_discover)
        dlg.show()
        dlg.txt_api_key.setText(secret_key)
        dlg.btn_discover.click()

        ok = self._pump_events_until(lambda: "Timeout" in dlg.lbl_discovery_status.text(), timeout_sec=3.0)
        self.assertTrue(ok)
        status_txt = dlg.lbl_discovery_status.text()
        self.assertIn("תקלת רשת/עומס", status_txt)
        self.assertNotIn(secret_key, status_txt)
        self.assertEqual(dlg.combo_model.count(), 0)
        self.assertTrue(dlg.btn_discover.isEnabled())
        dlg.close()

    def test_03_whole_book_dialogs_and_mainwindow_scope_wiring(self):
        # 1. בדיקת ScriptStudioDialog עם בחירת היקף הספר כולו
        s_dlg = ScriptStudioDialog(
            None,
            deepcopy(DEFAULT_SETTINGS),
            default_scope="book",
            book_title="ספר בדיקה",
            chapter_count=4,
            current_chapter_title="פרק ג׳",
        )
        self.assertTrue(s_dlg.radio_scope_book.isChecked())
        self.assertEqual(s_dlg.get_options()["scope"], "book")
        s_dlg.radio_scope_chapter.setChecked(True)
        self.assertEqual(s_dlg.get_options()["scope"], "chapter")
        s_dlg.close()

        # 2. בדיקת ExportPipelineDialog במצב הספר כולו לעומת הפרק הנוכחי
        book = {
            "id": "ui_book_1",
            "title": "ספר בדיקה ראשי",
            "chapters": [
                {"title": "פרק 1", "text": "Sentence 1A. Sentence 1B."},
                {"title": "פרק 2", "text": "Sentence 2A. Sentence 2B."},
                {"title": "פרק 3", "text": "Sentence 3A. Sentence 3B."},
            ],
        }
        e_dlg = ExportPipelineDialog(
            None,
            book_id=book["id"],
            chapter_index=2,
            source_text=book["chapters"][2]["text"],
            settings=deepcopy(DEFAULT_SETTINGS),
            book=book,
            default_scope="book",
        )
        self.assertTrue(e_dlg.radio_scope_book.isChecked())
        opts_b = e_dlg.get_options()
        self.assertEqual(opts_b["scope"], "book")
        self.assertIn(opts_b["package_mode"], ("zip", "folder", "unified_wav", "unified_mp3"))

        e_dlg.radio_scope_chapter.setChecked(True)
        opts_c = e_dlg.get_options()
        self.assertEqual(opts_c["scope"], "chapter")
        self.assertEqual(opts_c["package_mode"], "single_chapter")
        e_dlg.close()

        # 3. בדיקת חיבור כפתורי MainWindow ישירות לצינור הספר השלם
        win = MainWindow()
        win.books = [book]
        win.load_book_to_ui(0)
        win.chapter_list.setCurrentRow(1)  # המשתמש עומד באמצע הספר (פרק 2 מתוך 3)!

        # ווידוא נוכחות combo_scope וכפתורי כל הספר ב-toolbar
        self.assertEqual(win.combo_scope.count(), 2)
        self.assertIsNotNone(win.action_script_whole_book)
        self.assertIsNotNone(win.action_export_whole_book)

        # חיווט של action_script_whole_book -> _start_whole_book_script_job דרך ScriptStudioDialog
        with mock.patch("main_window.ScriptStudioDialog.exec", return_value=1), \
             mock.patch("main_window.ScriptStudioDialog.get_options", return_value={
                 "scope": "book", "mode": "educational", "instruction": "", "use_wikipedia": False, "wiki_terms": ""
             }), \
             mock.patch.object(win, "_start_whole_book_script_job") as mock_script_book:
            win.action_script_whole_book.trigger()
            self.assertEqual(win.combo_scope.currentData(), "book")
            mock_script_book.assert_called_once()
            called_book = mock_script_book.call_args[0][0]
            self.assertEqual(called_book["id"], "ui_book_1")
            self.assertEqual(len(called_book["chapters"]), 3)

        # חיווט של action_export_whole_book -> PipelineExportThread במצב book
        out_zip = os.path.join(self.temp_dir.name, "ui_whole_book.zip")
        with mock.patch("main_window.ExportPipelineDialog.exec", return_value=1), \
             mock.patch("main_window.ExportPipelineDialog.get_options", return_value={
                 "scope": "book",
                 "package_mode": "zip",
                 "translation_opts": {"engine": "google_free", "source_lang": "auto", "target_lang": "he"},
                 "script_opts": {"mode": "educational", "instruction": "", "use_wikipedia": False, "wiki_terms": ""},
                 "tts_opts": {"output_format": "wav", "default_voice": "sapi5", "speed": 1.0},
             }), \
             mock.patch("main_window.QFileDialog.getSaveFileName", return_value=(out_zip, "")), \
             mock.patch("main_window.PipelineExportThread") as mock_pipe_cls:
            inst = mock_pipe_cls.return_value
            inst.isRunning.return_value = False
            win.action_export_whole_book.trigger()
            mock_pipe_cls.assert_called_once()
            _, kw = mock_pipe_cls.call_args
            self.assertEqual(kw.get("scope"), "book")

        win.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
