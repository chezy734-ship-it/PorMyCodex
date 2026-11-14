# -*- coding: utf-8 -*-
"""
בדיקות אינטגרציה ויחידה עבור PorMyCodex:
1. שמירה אטומית וטעינה בתהליך חדש (Fresh Process) עם CWD שונה ודימוי סביבת PyInstaller EXE (sys.frozen / _MEIPASS).
2. אכיפת צינור קשיח: תרגום כל המשפטים הדרושים -> תסריט מלא ועדכני -> TTS -> יצוא (ומניעת עקיפה כשחסר תרגום או תסריט).
3. פסילת תוצרים שהתיישנו בעקבות שינוי טקסט מקור או הגדרות (תרגום / ספק AI / מודל / סגנון / ויקיפדיה).
4. ביטול נקי באמצע כל שלב (ללא סימון הצלחה וללא יצירת קובץ חלקי).
5. כשל חלקי בתרגום, בתסריט או ב-TTS (אי-דילוג על מקטעים ואי-סימון כשל/תוצר חלקי כהצלחה).
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from copy import deepcopy
from unittest import mock

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from book_loader import SAMPLE_BOOKS  # noqa: E402
from export_pipeline import (  # noqa: E402
    IncompleteArtifactError,
    MissingArtifactError,
    StaleArtifactError,
    assess_chapter_pipeline,
    ensure_script_for_chapter,
    ensure_translation_for_chapter,
    run_enforced_export_pipeline,
    run_tts_and_export_strict,
)
from library_store import (  # noqa: E402
    LibraryStore,
    atomic_write_json,
    get_user_data_dir,
)
from settings_manager import DEFAULT_SETTINGS, load_settings, save_settings  # noqa: E402


def _make_valid_wav_bytes(duration_frames: int = 800) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * duration_frames)
    return buf.getvalue()


def _make_mock_mp3_bytes(tag: str = "CHUNK") -> bytes:
    # כותרת ID3v2 מינימלית באורך 10 בתים (size=0) + פריים נתונים מדומה
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + tag.encode("utf-8") * 8


def _make_mock_script_chat(speaker_prefix: str = "שיעור"):
    call_log = []

    def _chat_fn(
        provider_id: str,
        api_key: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        **kwargs,
    ) -> str:
        call_log.append((provider_id, model, user_prompt))
        lines = []
        for raw_line in user_prompt.splitlines():
            if raw_line.startswith("[u"):
                end_br = raw_line.find("]")
                uid = raw_line[1:end_br]
                txt = raw_line[end_br + 1:].strip()
                idx_num = int(uid[1:])
                role = "teacher" if idx_num % 2 == 1 else "student"
                lines.append({
                    "unitId": uid,
                    "speakerRole": role,
                    "style": "ברור",
                    "text": f"{speaker_prefix} ({uid}): {txt}",
                    "needsReview": False,
                })
        return json.dumps({"title": f"תסריט {model}", "lines": lines}, ensure_ascii=False)

    return _chat_fn, call_log


class TestPersistenceAndEnforcedPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_tmp = self.temp_dir.name
        self.user_data_dir = os.path.join(self.base_tmp, "AppDataUser", "PorMyCodex")
        self.cwd_a = os.path.join(self.base_tmp, "cwd_a")
        self.cwd_b = os.path.join(self.base_tmp, "cwd_b")
        self.out_dir = os.path.join(self.base_tmp, "exports")
        os.makedirs(self.cwd_a, exist_ok=True)
        os.makedirs(self.cwd_b, exist_ok=True)
        os.makedirs(self.out_dir, exist_ok=True)

        self.prev_env = os.environ.get("PORMYCODEX_DATA_DIR")
        os.environ["PORMYCODEX_DATA_DIR"] = self.user_data_dir
        self.store = LibraryStore()

    def tearDown(self):
        if self.prev_env is None:
            os.environ.pop("PORMYCODEX_DATA_DIR", None)
        else:
            os.environ["PORMYCODEX_DATA_DIR"] = self.prev_env
        self.temp_dir.cleanup()

    def test_01_atomic_write_preserves_existing_file_on_failure(self):
        target = os.path.join(self.user_data_dir, "library_state.json")
        atomic_write_json(target, {"ok": 1, "books": ["keep_me"]})

        with mock.patch("library_store.os.replace", side_effect=OSError("disk interrupted")):
            with self.assertRaises(OSError):
                atomic_write_json(target, {"ok": 2, "corrupted": True})

        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data, {"ok": 1, "books": ["keep_me"]})
        leftover_tmps = [fn for fn in os.listdir(self.user_data_dir) if fn.startswith(".tmp_")]
        self.assertEqual(leftover_tmps, [])

    def test_02_fresh_process_restore_from_different_cwd_and_frozen_exe(self):
        # שלב א: תהליך ראשון מתוך cwd_a שומר ספר מיובא, הגדרות, תרגום, תסריט והתקדמות קריאה
        settings = deepcopy(DEFAULT_SETTINGS)
        settings["active_provider"] = "gemini"
        settings["providers"]["gemini"]["model"] = "gemini-3.8-flash"
        save_settings(settings)

        custom_book = {
            "id": "imported_book_1",
            "title": "ספר מחקר לדוגמה",
            "author": "מחבר בדיקה",
            "description": "נשמר בין הפעלות",
            "chapters": [
                {
                    "title": "פרק ראשון",
                    "text": "First sentence about physics. Second sentence about history. Third sentence concludes.",
                }
            ],
        }
        books = list(SAMPLE_BOOKS) + [custom_book]
        self.store.save_books_and_progress(
            books,
            {
                "active_book_id": "imported_book_1",
                "active_chapter_index": 0,
                "active_sentence_index": 2,
                "font_size": 24,
                "voice_type": "edge_hila",
                "speed": 1.25,
            },
        )
        self.store.update_reading_progress(
            book_id="imported_book_1",
            chapter_index=0,
            sentence_index=2,
            font_size=24,
            voice_type="edge_hila",
            speed=1.25,
        )

        chat_fn, _ = _make_mock_script_chat()
        out_mp3 = os.path.join(self.out_dir, "persisted.mp3")
        res = run_enforced_export_pipeline(
            book_id="imported_book_1",
            chapter_index=0,
            source_text=custom_book["chapters"][0]["text"],
            output_path=out_mp3,
            book_title=custom_book["title"],
            settings=settings,
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: f"[עברית] {s}",
            chat_fn=chat_fn,
            mp3_synth_fn=lambda txt, v, spd: _make_mock_mp3_bytes("OK"),
        )
        self.assertTrue(res["success"])
        self.assertTrue(os.path.exists(out_mp3))

        # שלב ב: הפעלה בתהליך Python חדש לגמרי מתיקיית עבודה אחרת (cwd_b) עם דימוי sys.frozen/_MEIPASS
        fake_meipass = os.path.join(self.base_tmp, "_MEI_BUNDLE")
        os.makedirs(fake_meipass, exist_ok=True)
        env = os.environ.copy()
        env.pop("PORMYCODEX_DATA_DIR", None)
        # מדמים סביבת נתוני משתמש מנותקת (XDG / APPDATA) כדי לבדוק שאין שום שימוש ב-cwd או _MEIPASS
        fake_xdg = os.path.join(self.base_tmp, "IsolatedXDG")
        os.makedirs(fake_xdg, exist_ok=True)
        # נעביר במבחן דרך PORMYCODEX_DATA_DIR ובנוסף נבדוק גם את נתיב ברירת המחדל ללא override
        probe_script = f"""
import json, os, sys
sys.path.insert(0, {REPO_DIR!r})
sys.frozen = True
sys._MEIPASS = {fake_meipass!r}
os.environ['XDG_DATA_HOME'] = {fake_xdg!r}
os.environ['XDG_CONFIG_HOME'] = {fake_xdg!r}
# 1) בדיקה שברירת המחדל של תיקיית הנתונים אינה cwd ואינה _MEIPASS
from library_store import get_user_data_dir, LibraryStore
from book_loader import SAMPLE_BOOKS
from settings_manager import load_settings
from export_pipeline import assess_chapter_pipeline

os.environ.pop('PORMYCODEX_DATA_DIR', None)
default_dir = get_user_data_dir()
assert os.getcwd() not in default_dir and sys._MEIPASS not in default_dir, default_dir

# 2) בדיקת שחזור מלא מהמאגר שנכתב בתהליך הקודם
os.environ['PORMYCODEX_DATA_DIR'] = {self.user_data_dir!r}
cfg = load_settings()
st = LibraryStore()
loaded = st.load_library(SAMPLE_BOOKS)
book_ids = [b['id'] for b in loaded['books']]
assert 'imported_book_1' in book_ids, book_ids
imp = [b for b in loaded['books'] if b['id'] == 'imported_book_1'][0]
prog = loaded['reading_progress']
assert prog['active_book_id'] == 'imported_book_1'
assert prog['active_sentence_index'] == 2
assert prog['font_size'] == 24
assert prog['voice_type'] == 'edge_hila'
assert abs(prog['speed'] - 1.25) < 0.001
status = assess_chapter_pipeline('imported_book_1', 0, imp['chapters'][0]['text'], settings=cfg, store=st)
assert status['translation_ready'] is True, status
assert status['script_ready'] is True, status
print(json.dumps({{'restored': True, 'books': len(book_ids), 'lines': len(status['script_record']['lines'])}}))
"""
        proc = subprocess.run(
            [sys.executable, "-c", probe_script],
            cwd=self.cwd_b,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        parsed_out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(parsed_out["restored"])
        self.assertEqual(parsed_out["lines"], 3)

    def test_03_missing_translation_and_script_cannot_be_bypassed(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        src = "Sentence one. Sentence two."

        # ניסיון ליצור תסריט בלי תרגום -> חייב להיחסם
        chat_fn, _ = _make_mock_script_chat()
        with self.assertRaises(MissingArtifactError):
            ensure_script_for_chapter(
                book_id="b1",
                chapter_index=0,
                source_text=src,
                settings=settings,
                store=self.store,
                chat_fn=chat_fn,
            )

        # ניסיון לקפוץ ישירות ל-TTS/יצוא בלי תרגום -> חייב להיחסם
        out_path = os.path.join(self.out_dir, "bypass_attempt.mp3")
        with self.assertRaises(MissingArtifactError):
            run_tts_and_export_strict(
                book_id="b1",
                chapter_index=0,
                source_text=src,
                output_path=out_path,
                settings=settings,
                store=self.store,
                mp3_synth_fn=lambda txt, v, spd: _make_mock_mp3_bytes(),
            )
        self.assertFalse(os.path.exists(out_path))

        # עכשיו מתרגמים שלב 1 בלבד — ניסיון לקפוץ ל-TTS/יצוא בלי תסריט -> חייב להיחסם
        ensure_translation_for_chapter(
            book_id="b1",
            chapter_index=0,
            source_text=src,
            settings=settings,
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: f"תרגום: {s}",
        )
        with self.assertRaises(MissingArtifactError):
            run_tts_and_export_strict(
                book_id="b1",
                chapter_index=0,
                source_text=src,
                output_path=out_path,
                settings=settings,
                store=self.store,
                mp3_synth_fn=lambda txt, v, spd: _make_mock_mp3_bytes(),
            )
        self.assertFalse(os.path.exists(out_path))

    def test_04_stale_artifacts_invalidated_on_source_or_settings_change(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        src_v1 = "Alpha sentence. Beta sentence."
        trans_calls = []
        chat_fn, script_calls = _make_mock_script_chat()
        tts_calls = []

        def _t_fn(s, e, sl, tl):
            trans_calls.append((s, e, sl, tl))
            return f"תרגום[{tl}]: {s}"

        def _mp3_fn(txt, v, spd):
            tts_calls.append((txt, v, spd))
            return _make_mock_mp3_bytes("A")

        out_1 = os.path.join(self.out_dir, "v1.mp3")
        res1 = run_enforced_export_pipeline(
            book_id="b_stale",
            chapter_index=0,
            source_text=src_v1,
            output_path=out_1,
            settings=settings,
            store=self.store,
            single_translate_fn=_t_fn,
            chat_fn=chat_fn,
            mp3_synth_fn=_mp3_fn,
        )
        self.assertTrue(res1["success"])
        self.assertEqual(len(trans_calls), 2)
        self.assertEqual(len(script_calls), 1)
        first_tts_count = len(tts_calls)
        self.assertGreater(first_tts_count, 0)

        # שינוי א: מקור הטקסט השתנה -> תוצר ישן נפסל, מעבר ישיר ל-TTS נחסם עם StaleArtifactError
        src_v2 = "Alpha sentence edited. Beta sentence."
        with self.assertRaises(StaleArtifactError):
            run_tts_and_export_strict(
                book_id="b_stale",
                chapter_index=0,
                source_text=src_v2,
                output_path=out_1,
                settings=settings,
                store=self.store,
                mp3_synth_fn=_mp3_fn,
            )

        # הרצת הצינור המלא עבור src_v2 מחדשת תרגום + תסריט
        res2 = run_enforced_export_pipeline(
            book_id="b_stale",
            chapter_index=0,
            source_text=src_v2,
            output_path=out_1,
            settings=settings,
            store=self.store,
            single_translate_fn=_t_fn,
            chat_fn=chat_fn,
            mp3_synth_fn=_mp3_fn,
        )
        self.assertTrue(res2["success"])
        self.assertEqual(len(trans_calls), 4)
        self.assertEqual(len(script_calls), 2)

        # שינוי ב: הגדרות ספק/מודל AI לתסריט השתנו -> התסריט הישן נפסל!
        settings_mod = deepcopy(settings)
        settings_mod["providers"]["gemini"]["model"] = "gemini-3.8-pro"
        with self.assertRaises(StaleArtifactError):
            run_tts_and_export_strict(
                book_id="b_stale",
                chapter_index=0,
                source_text=src_v2,
                output_path=out_1,
                settings=settings_mod,
                store=self.store,
                mp3_synth_fn=_mp3_fn,
            )

        # בהרצה מחודשת, התרגום נשאר תקף (לא נקרא שוב) ורק התסריט+TTS רצים!
        res3 = run_enforced_export_pipeline(
            book_id="b_stale",
            chapter_index=0,
            source_text=src_v2,
            output_path=out_1,
            settings=settings_mod,
            store=self.store,
            single_translate_fn=_t_fn,
            chat_fn=chat_fn,
            mp3_synth_fn=_mp3_fn,
        )
        self.assertTrue(res3["success"])
        self.assertEqual(len(trans_calls), 4)   # התרגום לא דרש הרצה מיותרת
        self.assertEqual(len(script_calls), 3)  # התסריט נוצר מחדש למודל החדש

        # שינוי ג: שפת יעד/מנוע תרגום השתנו -> גם התרגום וגם התסריט נפסלים ומחודשים
        res4 = run_enforced_export_pipeline(
            book_id="b_stale",
            chapter_index=0,
            source_text=src_v2,
            output_path=out_1,
            settings=settings_mod,
            translation_opts={"engine": "mymemory", "source_lang": "en", "target_lang": "fr"},
            store=self.store,
            single_translate_fn=_t_fn,
            chat_fn=chat_fn,
            mp3_synth_fn=_mp3_fn,
        )
        self.assertTrue(res4["success"])
        self.assertEqual(len(trans_calls), 6)
        self.assertEqual(len(script_calls), 4)

    def test_05_long_chapter_script_is_not_truncated(self):
        # אימות שמחיקת החיתוך [:80] מייצרת תסריט מלא גם מעבר ל-95 משפטים
        sentences = [f"Sentence number {i} in a long chapter." for i in range(1, 96)]
        long_text = " ".join(sentences)
        settings = deepcopy(DEFAULT_SETTINGS)
        chat_fn, script_calls = _make_mock_script_chat()
        out_wav = os.path.join(self.out_dir, "long.wav")

        res = run_enforced_export_pipeline(
            book_id="long_book",
            chapter_index=0,
            source_text=long_text,
            output_path=out_wav,
            settings=settings,
            tts_opts={"output_format": "wav", "default_voice": "sapi5", "speed": 1.0},
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: f"משפט {s}",
            chat_fn=chat_fn,
            wav_synth_fn=lambda txt, v, spd: _make_valid_wav_bytes(),
        )
        self.assertTrue(res["success"])
        self.assertGreaterEqual(len(script_calls), 3)  # 95 משפטים / 45 לאצווה = 3 אצוות
        self.assertEqual(res["script"]["unit_count"], 95)
        self.assertEqual(len(res["script"]["covered_units"]), 95)
        self.assertTrue(os.path.exists(out_wav))

    def test_06_cancellation_mid_pipeline_leaves_no_partial_artifacts(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        src = "One. Two. Three. Four."
        cancel_ev = threading.Event()
        out_path = os.path.join(self.out_dir, "cancelled.mp3")

        # 1. ביטול באמצע התרגום (במשפט השני)
        seen = []

        def _cancelling_trans(s, e, sl, tl):
            seen.append(s)
            if len(seen) >= 2:
                cancel_ev.set()
            return f"תרגום: {s}"

        res_c1 = run_enforced_export_pipeline(
            book_id="cancel_book",
            chapter_index=0,
            source_text=src,
            output_path=out_path,
            settings=settings,
            store=self.store,
            cancel_event=cancel_ev,
            single_translate_fn=_cancelling_trans,
            chat_fn=_make_mock_script_chat()[0],
            mp3_synth_fn=lambda txt, v, spd: _make_mock_mp3_bytes(),
        )
        self.assertFalse(res_c1["success"])
        self.assertEqual(res_c1["status"], "cancelled")
        self.assertEqual(res_c1["stage"], "translation")
        self.assertFalse(os.path.exists(out_path))
        self.assertIsNone(self.store.get_chapter_artifacts("cancel_book", 0).get("translation"))

        # 2. ביטול באמצע ה-TTS (לאחר סגמנט ראשון, לפני יצוא קובץ)
        cancel_ev2 = threading.Event()
        tts_seen = []

        def _cancelling_tts(txt, v, spd):
            tts_seen.append(txt)
            cancel_ev2.set()
            return _make_mock_mp3_bytes()

        res_c2 = run_enforced_export_pipeline(
            book_id="cancel_book2",
            chapter_index=0,
            source_text=src,
            output_path=out_path,
            settings=settings,
            store=self.store,
            cancel_event=cancel_ev2,
            single_translate_fn=lambda s, e, sl, tl: f"תרגום: {s}",
            chat_fn=_make_mock_script_chat()[0],
            mp3_synth_fn=_cancelling_tts,
        )
        self.assertFalse(res_c2["success"])
        self.assertEqual(res_c2["status"], "cancelled")
        self.assertEqual(res_c2["stage"], "tts_export")
        self.assertFalse(os.path.exists(out_path))

    def test_07_partial_failures_never_marked_as_success(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        src = "First line. Second line. Third line."

        # מקרה 1: תרגום מחזיר מחרוזת ריקה במשפט השני
        out_fail1 = os.path.join(self.out_dir, "fail_trans.mp3")
        call_n = {"n": 0}

        def _partial_trans(s, e, sl, tl):
            call_n["n"] += 1
            if call_n["n"] == 2:
                return "   "  # כשל חלקי
            return f"תרגום: {s}"

        res_f1 = run_enforced_export_pipeline(
            book_id="fail_book",
            chapter_index=0,
            source_text=src,
            output_path=out_fail1,
            settings=settings,
            store=self.store,
            single_translate_fn=_partial_trans,
            chat_fn=_make_mock_script_chat()[0],
            mp3_synth_fn=lambda txt, v, spd: _make_mock_mp3_bytes(),
        )
        self.assertFalse(res_f1["success"])
        self.assertEqual(res_f1["status"], "failed")
        self.assertEqual(res_f1["stage"], "translation")
        self.assertFalse(os.path.exists(out_fail1))
        self.assertIsNone(self.store.get_chapter_artifacts("fail_book", 0).get("translation"))

        # מקרה 2: התסריט מדלג על יחידה u3 (תוצאה חלקית)
        out_fail2 = os.path.join(self.out_dir, "fail_script.mp3")

        def _partial_script_chat(*args, **kwargs):
            return json.dumps({
                "title": "חלקי",
                "lines": [
                    {"unitId": "u1", "speakerRole": "teacher", "style": "רגוע", "text": "שורה 1"},
                ],
            }, ensure_ascii=False)

        res_f2 = run_enforced_export_pipeline(
            book_id="fail_book",
            chapter_index=0,
            source_text=src,
            output_path=out_fail2,
            settings=settings,
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: f"תרגום: {s}",
            chat_fn=_partial_script_chat,
            mp3_synth_fn=lambda txt, v, spd: _make_mock_mp3_bytes(),
        )
        self.assertFalse(res_f2["success"])
        self.assertEqual(res_f2["status"], "failed")
        self.assertEqual(res_f2["stage"], "script")
        self.assertFalse(os.path.exists(out_fail2))
        self.assertIsNone(self.store.get_chapter_artifacts("fail_book", 0).get("script"))

        # מקרה 3: TTS מצליח בדובר הראשון ונכשל (מחזיר ריק) בדובר השני -> לעולם לא שומר WAV/MP3 חלקי!
        out_fail3 = os.path.join(self.out_dir, "fail_tts.wav")
        seg_n = {"n": 0}

        def _partial_wav_tts(txt, v, spd):
            seg_n["n"] += 1
            if seg_n["n"] >= 2:
                return b""  # כשל במקטע שני
            return _make_valid_wav_bytes()

        res_f3 = run_enforced_export_pipeline(
            book_id="fail_book_tts",
            chapter_index=0,
            source_text=src,
            output_path=out_fail3,
            settings=settings,
            tts_opts={"output_format": "wav", "default_voice": "sapi5", "speed": 1.0},
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: f"תרגום: {s}",
            chat_fn=_make_mock_script_chat()[0],
            wav_synth_fn=_partial_wav_tts,
        )
        self.assertFalse(res_f3["success"])
        self.assertEqual(res_f3["status"], "failed")
        self.assertEqual(res_f3["stage"], "tts_export")
        self.assertFalse(os.path.exists(out_fail3))
        self.assertIsNone(self.store.get_chapter_artifacts("fail_book_tts", 0).get("last_export"))

        # מקרה 4: רשומת תרגום/תסריט חלקית שהושתלה במאגר ידנית נדחית
        raw = self.store.load_raw_state()
        raw.setdefault("chapter_artifacts", {})["fail_book_tts::0"] = {
            "translation": {
                "is_complete": False,
                "source_hash": "x",
                "config_hash": "y",
                "translated_sentences": ["only one"],
                "sentence_count_source": 3,
            }
        }
        atomic_write_json(self.store.state_path, raw)
        with self.assertRaises((IncompleteArtifactError, MissingArtifactError, StaleArtifactError)):
            run_tts_and_export_strict(
                book_id="fail_book_tts",
                chapter_index=0,
                source_text=src,
                output_path=out_fail3,
                settings=settings,
                store=self.store,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
