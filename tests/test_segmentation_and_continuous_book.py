# -*- coding: utf-8 -*-
"""
בדיקות פיצול מקטעים חכם (Segmentation), ביצועי אצוות בספרים גדולים, התאוששות תסריט אדפטיבית,
טווח מקטעים, דילוג תרגום בשפה זהה, והקראה רציפה לכל הספר ב-MainWindow.
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
import wave
from copy import deepcopy
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from book_loader import (  # noqa: E402
    resegment_book,
    segment_text_smartly,
    validate_book_completeness,
)
from export_pipeline import (  # noqa: E402
    ensure_translation_for_chapter,
    run_whole_book_export_pipeline,
)
from library_store import LibraryStore  # noqa: E402
from script_engine import generate_narration_script  # noqa: E402
from settings_manager import DEFAULT_SETTINGS  # noqa: E402
from translator_engine import TranslationProviderError, translate_required_sentences  # noqa: E402

try:
    from PyQt6.QtWidgets import QApplication
    HAS_PYQT6 = True
except ImportError:
    HAS_PYQT6 = False


class _FakeHTTPResp:
    def __init__(self, status_code: int, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status_code) < 400

    def json(self):
        return self._body


def _make_wav_bytes(frames: int = 320) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


class TestSmartSegmentationAndLargeBookPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_tmp = self.temp_dir.name
        self.prev_env = os.environ.get("PORMYCODEX_DATA_DIR")
        os.environ["PORMYCODEX_DATA_DIR"] = os.path.join(self.base_tmp, "AppData")
        self.store = LibraryStore()

    def tearDown(self):
        if self.prev_env is None:
            os.environ.pop("PORMYCODEX_DATA_DIR", None)
        else:
            os.environ["PORMYCODEX_DATA_DIR"] = self.prev_env
        self.temp_dir.cleanup()

    def test_01_smart_topic_segmentation_and_resegment_book(self):
        para_a = " ".join(f"משפט פתיחה מספר {i} בנושא הראשון." for i in range(1, 35))
        para_b = " ".join(f"משפט המשך מספר {i} בפרק השני של הספר." for i in range(1, 45))
        full_text = f"פרק א׳ - מבוא לחכמה\n{para_a}\n\nפרק ב׳ - דרכי הלימוד\n{para_b}"

        segs = segment_text_smartly(full_text, max_chars=900, min_chars=250)
        self.assertGreaterEqual(len(segs), 3)
        self.assertTrue(any("פרק א" in s["title"] for s in segs))
        self.assertTrue(any("פרק ב" in s["title"] for s in segs))
        for s in segs:
            self.assertLessEqual(len(s["text"]), 950)
            self.assertTrue(s["text"].strip())

        book = {
            "id": "big_book_test",
            "title": "ספר גדול",
            "chapters": [{"title": "גוש יחיד", "text": full_text}],
        }
        compact_book = resegment_book(book, preset="compact")
        self.assertGreaterEqual(len(compact_book["chapters"]), 2)
        self.assertEqual(compact_book["segment_preset"], "compact")

        expanded_book = resegment_book(compact_book, preset="chapter")
        self.assertLessEqual(len(expanded_book["chapters"]), len(compact_book["chapters"]))
        self.assertEqual(expanded_book["segment_preset"], "chapter")

    def test_02_partial_pdf_opt_in_validation(self):
        partial_book = {
            "id": "pdf_partial",
            "title": "PDF עם כריכה סרוקה",
            "chapters": [{"title": "עמוד 2", "text": "טקסט אמיתי בעמוד השני."}],
            "pdf_coverage": {
                "source_type": "pdf",
                "total_pages": 2,
                "extracted_page_count": 1,
                "extracted_pages": [2],
                "empty_or_ocr_pages": [1],
                "failed_pages": [],
                "is_complete_extraction": False,
                "warning_message": "עמודים ללא שכבת טקסט: 1",
            },
        }
        self.assertFalse(validate_book_completeness(partial_book, allow_partial_pdf=False)["is_complete"])
        ok_res = validate_book_completeness(partial_book, allow_partial_pdf=True)
        self.assertTrue(ok_res["is_complete"])
        self.assertIn("1", ok_res["warning"])

    def test_03_free_google_batching_and_pinpoint_fallback_on_418(self):
        # 1. הצלחה מלאה: 12 משפטים נשלחים בפחות מ-12 בקשות HTTP ונשמרים באצווה
        sents = [f"Sentence number {i} for batch translation." for i in range(1, 13)]
        http_calls = []

        def ok_batch_req(method, url, params=None, headers=None, timeout=None, **kwargs):
            q = (params or {}).get("q", "")
            http_calls.append(q)
            # מדמה החזרת מחרוזת מאוחדת עם התגים [[#k#]] מתורגמת
            out_lines = []
            for line in q.splitlines():
                m = re.match(r"(\[\[#\d+#\]\])\s*(.*)", line)
                if m:
                    out_lines.append(f"{m.group(1)} תרגום({m.group(2)})")
                else:
                    out_lines.append(f"תרגום({line})")
            joined = "\n".join(out_lines)
            return _FakeHTTPResp(200, [[[joined, q, None, None]]], {"Content-Type": "application/json"})

        res = translate_required_sentences(
            sents,
            engine="google_free",
            source_lang="en",
            target_lang="he",
            request_fn=ok_batch_req,
        )
        self.assertTrue(res["is_complete"])
        self.assertEqual(len(res["translated_sentences"]), 12)
        self.assertLessEqual(len(http_calls), 3)

        # 2. כשל 418 במשפט 5 -> ירידה לאצווה פרטנית, שמירת 1..4 ועצירה מדויקת ב-u5
        saved_units = []

        def on_ev(ev):
            if ev.get("event") == "unit_done":
                saved_units.append(ev["unit_id"])
            elif ev.get("event") == "batch_done":
                for it in ev.get("items", []):
                    saved_units.append(it["unit_id"])

        def fail_u5_req(method, url, params=None, headers=None, timeout=None, **kwargs):
            q = (params or {}).get("q", "")
            if "Sentence number 5" in q:
                return _FakeHTTPResp(418, "<html><body>netfree.link</body></html>", {"Content-Type": "text/html"})
            return ok_batch_req(method, url, params=params)

        with self.assertRaises(TranslationProviderError) as ctx:
            translate_required_sentences(
                sents,
                engine="google_free",
                source_lang="en",
                target_lang="he",
                request_fn=fail_u5_req,
                unit_progress_cb=on_ev,
            )
        self.assertTrue(ctx.exception.is_blocked)
        self.assertEqual(ctx.exception.unit_id, "u5")
        self.assertEqual(saved_units, ["u1", "u2", "u3", "u4"])

    def test_04_adaptive_script_batch_splitting_recovers_missing_or_oversized_batch(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        settings["providers"]["gemini"]["api_key"] = "AIzaTestAdaptive"
        text = " ".join(f"משפט מקור {i}." for i in range(1, 9))

        call_counts = []

        def flaky_chat(user_prompt: str = "", **kwargs):
            uids = re.findall(r"\[(u\d+)\]", user_prompt)
            call_counts.append(uids)
            # בקריאה הראשונה עם כל 8 היחידות — המודל מחזיר רק 6 ומשמיט 2 -> המנוע אמור להשלים רק את ה-2 החסרות!
            if len(uids) == 8:
                partial_lines = [
                    {"unitId": u, "speakerRole": "teacher", "style": "רגוע", "text": f"תסריט {u}"}
                    for u in uids[:6]
                ]
                return json.dumps({"title": "תסריט אדפטיבי", "lines": partial_lines}, ensure_ascii=False)
            lines = [
                {"unitId": u, "speakerRole": "student", "style": "בהיר", "text": f"תסריט {u}"}
                for u in uids
            ]
            return json.dumps({"title": "השלמה", "lines": lines}, ensure_ascii=False)

        res = generate_narration_script(
            text=text,
            settings=settings,
            chat_fn=flaky_chat,
            units_per_batch=10,
        )
        self.assertTrue(res["is_complete"])
        self.assertEqual(res["unit_count"], 8)
        self.assertEqual(len(res["lines"]), 8)
        self.assertEqual(call_counts[0], [f"u{i}" for i in range(1, 9)])
        self.assertEqual(call_counts[1], ["u7", "u8"])

    def test_05_same_language_bypass_and_target_chapters_range_export(self):
        book = {
            "id": "he_range_book",
            "title": "ספר עברי רב מקטעים",
            "chapters": [
                {"title": "מקטע 1", "text": "שורה ראשונה במקטע ראשון."},
                {"title": "מקטע 2", "text": "שורה שנייה במקטע שני."},
                {"title": "מקטע 3", "text": "שורה שלישית במקטע שלישי."},
            ],
        }
        settings = deepcopy(DEFAULT_SETTINGS)

        def fail_if_external_translate(*args, **kwargs):
            raise AssertionError("אסור לקרוא לתרגום חיצוני כאשר שפת המקור והיעד זהות!")

        def mock_chat(user_prompt: str = "", **kwargs):
            uids = re.findall(r"\[(u\d+)\]", user_prompt)
            return json.dumps({
                "title": "תסריט",
                "lines": [{"unitId": u, "speakerRole": "narrator", "style": "רגוע", "text": f"טקסט {u}"} for u in uids],
            }, ensure_ascii=False)

        out_wav = os.path.join(self.base_tmp, "range_1_2.wav")
        res = run_whole_book_export_pipeline(
            book=book,
            output_path=out_wav,
            package_mode="unified_wav",
            settings=settings,
            translation_opts={"source_lang": "he", "target_lang": "he"},
            tts_opts={"output_format": "wav", "default_voice": "sapi5", "speed": 1.0},
            store=self.store,
            single_translate_fn=fail_if_external_translate,
            chat_fn=mock_chat,
            wav_synth_fn=lambda txt, v, spd: _make_wav_bytes(200),
            target_chapters=[0, 1],
        )
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["scope"], "range")
        self.assertEqual(res["chapters_processed"], [0, 1])
        self.assertTrue(os.path.exists(out_wav))
        # מקטע 3 לא עובד כי לא היה בטווח המבוקש
        self.assertIsNone(self.store.get_chapter_artifacts("he_range_book", 2).get("script"))

        # לוודא ש-bypass_same_language ישיר עובד גם ברמת פרק בודד
        t_rec = ensure_translation_for_chapter(
            book_id="he_range_book",
            chapter_index=2,
            source_text=book["chapters"][2]["text"],
            settings=settings,
            translation_opts={"bypass_same_language": True},
            store=self.store,
            single_translate_fn=fail_if_external_translate,
        )
        self.assertTrue(t_rec["is_complete"])
        self.assertEqual(t_rec["engine"], "identity")


@unittest.skipUnless(HAS_PYQT6, "PyQt6 not installed")
class TestContinuousPlaybackAndSegmentationUI(unittest.TestCase):
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

    def test_01_continuous_whole_book_playback_advances_across_chapters_and_resegment_ui(self):
        from dialogs import SegmentBookDialog
        from main_window import MainWindow

        book = {
            "id": "cont_book",
            "title": "ספר להקראה רציפה",
            "chapters": [
                {"title": "מקטע 1", "text": "משפט ראשון פרק א. משפט שני פרק א."},
                {"title": "מקטע 2", "text": "משפט ראשון פרק ב. משפט שני פרק ב."},
            ],
        }
        win = MainWindow()
        win.books = [book]
        win.load_book_to_ui(0)
        self.assertTrue(win.chk_continuous_book.isChecked())
        self.assertEqual(win.current_chapter_index, 0)

        with mock.patch.object(win.audio, "play") as mock_play:
            win._on_chapter_audio_completed()
            self.assertEqual(win.current_chapter_index, 1)
            mock_play.assert_called_once()
            played_sents, start_idx = mock_play.call_args[0][0], mock_play.call_args[0][1]
            self.assertEqual(start_idx, 0)
            self.assertIn("פרק ב", played_sents[0])

        # בדיקת SegmentBookDialog
        sdlg = SegmentBookDialog(win, book)
        sdlg.radio_compact.setChecked(True)
        self.assertEqual(sdlg.get_options()["preset"], "compact")
        sdlg.close()
        win.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
