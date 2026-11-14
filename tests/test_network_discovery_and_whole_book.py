# -*- coding: utf-8 -*-
"""
בדיקות מקיפות לתיקוני רשת/Timeout, גילוי מודלים, חילוץ PDF מלא וצינור הספר כולו (Whole-Book):
1. הפרדת connect/read timeout, deadline כולל, backoff מוגבל לכשלים זמניים בלבד, אי-החלפת מודל שקטה,
   ביטול מיידי גם בזמן המתנת backoff, חלוקה לאצוות בקלט גדול, ומניעת פלט חלקי כהצלחה.
2. גילוי מודלים חי מול endpoint רשמי של כל ספק (כולל pagination וסינון יכולות), דחיית תוצאות ריקות ללא רשימות מומצאות,
    והגנת פרטיות מוחלטת של מפתח ה-API בשגיאות ובריצות.
3. תמיכת ספר שלם: חילוץ PDF רב-עמודי ודחיית PDF חלקי/נדרש-OCR; התחלה כשהפרק הפעיל באמצע הספר
   ועדיין עיבוד של כל פרקי הספר (0..K-1) פעם אחת לפי הסדר; שער קשיח שמונע TTS לפני שכל הפרקים מתורגמים ומתוסרטים;
   כשל/ביטול באמצע + המשך בתהליך חדש תוך דילוג על פרקים תקינים ופסילה של פרקים שהתיישנו בלבד;
   ואימות אריזות סופיות (WAV מאוחד בהזרמה, MP3 מפוענח/מקודד ללא חיבור בינארי עיוור, ZIP/תיקייה + manifest מסודר).
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import wave
import zipfile
from copy import deepcopy
from unittest import mock

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from ai_linguist import vocalize_and_expand_hebrew  # noqa: E402
from ai_providers import (  # noqa: E402
    AICallError,
    AICancelledError,
    chat_completion,
    discover_models_detailed,
    execute_http_with_retry,
)
from audio_exporter import merge_mp3_files_decoded, package_book_audio  # noqa: E402
from book_loader import load_file_content, split_into_sentences, validate_book_completeness  # noqa: E402
from export_pipeline import (  # noqa: E402
    IncompleteArtifactError,
    assess_book_pipeline,
    run_whole_book_export_pipeline,
    run_whole_book_script_pipeline,
)
from library_store import LibraryStore  # noqa: E402
from settings_manager import DEFAULT_SETTINGS, compute_provider_fingerprint  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class _FakeReadTimeout(TimeoutError):
    """מדמה requests.exceptions.ReadTimeout גם בסביבה ללא requests מותקן."""


class _FakeConnectTimeout(TimeoutError):
    """מדמה requests.exceptions.ConnectTimeout."""


def _make_wav_bytes(frames: int = 400) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x02\x00" * frames)
    return buf.getvalue()


def _make_mp3_bytes(label: str) -> bytes:
    return b"ID3\x03\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x00" + label.encode("utf-8") * 4


class TestNetworkTimeoutsAndDiscovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_tmp = self.temp_dir.name
        self.prev_env = os.environ.get("PORMYCODEX_DATA_DIR")
        os.environ["PORMYCODEX_DATA_DIR"] = os.path.join(self.base_tmp, "AppDataUser")

    def tearDown(self):
        if self.prev_env is None:
            os.environ.pop("PORMYCODEX_DATA_DIR", None)
        else:
            os.environ["PORMYCODEX_DATA_DIR"] = self.prev_env
        self.temp_dir.cleanup()

    def test_01_repeated_read_timeout_respects_deadline_and_names_stage(self):
        secret = "AIzaSySecretKeyNeverLeak123456789"
        calls = []
        now = {"t": 0.0}

        def fake_clock():
            return now["t"]

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs["timeout"]))
            now["t"] += 2.2
            raise _FakeReadTimeout(
                f"HTTPSConnectionPool(host='generativelanguage.googleapis.com', port=443): "
                f"Read timed out. key={secret}"
            )

        with mock.patch("ai_providers._sleep_cancellable") as sleep_mock:
            with self.assertRaises(AICallError) as ctx:
                execute_http_with_retry(
                    "POST",
                    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
                    provider_name="Google Gemini",
                    stage="יצירת תסריט AI",
                    connect_timeout=5.0,
                    read_timeout=10.0,
                    total_deadline=6.0,
                    max_retries=4,
                    backoff_base=0.5,
                    secrets=[secret],
                    request_fn=fake_request,
                    clock_fn=fake_clock,
                )

        # חייב לעצור בתקציב זמן חסום בלי למצות את כל ה-4 retries
        self.assertLessEqual(len(calls), 3)
        self.assertEqual(ctx.exception.category, "timeout")
        msg = str(ctx.exception)
        self.assertIn("יצירת תסריט AI", msg)
        self.assertIn("Read Timeout", msg)
        self.assertNotIn(secret, msg)
        # הפסקת Connect vs Read timeout
        for _, _, t_tuple in calls:
            self.assertIsInstance(t_tuple, tuple)
            self.assertEqual(len(t_tuple), 2)
        self.assertTrue(sleep_mock.called or len(calls) >= 1)

    def test_02_transient_retries_succeed_while_auth_and_404_never_retry_or_swap_model(self):
        # 1. ניסיון ראשון 503 + שני הצלחה -> retries עובד רק לכשלים זמניים
        seen_urls = []

        def retry_then_ok(method, url, **kwargs):
            seen_urls.append((method, url, kwargs.get("json", {}).get("model")))
            if len(seen_urls) == 1:
                return _FakeResponse(503, {"error": {"message": "overloaded"}})
            return _FakeResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": "תשובה מלאה"}]}}]},
            )

        with mock.patch("ai_providers._sleep_cancellable"):
            out = chat_completion(
                provider_id="gemini",
                api_key="AIzaValidKeyForTest",
                model="gemini-2.5-pro",
                system_prompt="sys",
                user_prompt="usr",
                stage="תרגום AI",
                request_fn=retry_then_ok,
            )
        self.assertEqual(out, "תשובה מלאה")
        self.assertEqual(len(seen_urls), 2)
        self.assertTrue(all("gemini-2.5-pro:generateContent" in u[1] for u in seen_urls))

        # 2. HTTP 404 למודל שנבחר לעולם אינו מחליף מודל בשקט ואינו מנסה שוב
        urls_404 = []

        def always_404(method, url, **kwargs):
            urls_404.append(url)
            return _FakeResponse(404, {"error": {"message": "models/custom-x is not found"}})

        with self.assertRaises(AICallError) as ctx_404:
            chat_completion(
                provider_id="gemini",
                api_key="AIzaValidKeyForTest",
                model="custom-x",
                system_prompt="sys",
                user_prompt="usr",
                stage="ניקוד AI",
                max_retries=3,
                request_fn=always_404,
            )
        self.assertEqual(len(urls_404), 1)
        self.assertEqual(ctx_404.exception.category, "model_not_found")
        self.assertIn("custom-x", str(ctx_404.exception))

        # 3. HTTP 401 לעולם אינו מנסה שוב ואינו מדליף מפתח
        urls_401 = []
        secret = "sk-ant-top-secret-value-999"

        def always_401(method, url, **kwargs):
            urls_401.append(url)
            return _FakeResponse(401, {"error": {"message": f"Invalid x-api-key {secret}"}})

        with self.assertRaises(AICallError) as ctx_401:
            chat_completion(
                provider_id="anthropic",
                api_key=secret,
                model="claude-3-7-sonnet-latest",
                system_prompt="sys",
                user_prompt="usr",
                stage="יצירת תסריט AI",
                max_retries=3,
                request_fn=always_401,
            )
        self.assertEqual(len(urls_401), 1)
        self.assertEqual(ctx_401.exception.category, "auth")
        self.assertNotIn(secret, str(ctx_401.exception))

    def test_03_cancellation_interrupts_backoff_immediately(self):
        cancel_ev = threading.Event()
        calls = {"n": 0}

        def timeout_first(method, url, **kwargs):
            calls["n"] += 1
            return _FakeResponse(429, {"error": {"message": "rate limit"}}, headers={"Retry-After": "15"})

        def _trigger_cancel():
            cancel_ev.set()

        t = threading.Timer(0.1, _trigger_cancel)
        t.start()
        try:
            with self.assertRaises(AICancelledError) as ctx:
                execute_http_with_retry(
                    "GET",
                    "https://api.openai.com/v1/models",
                    provider_name="OpenAI",
                    stage="גילוי מודלים",
                    connect_timeout=2.0,
                    read_timeout=5.0,
                    total_deadline=30.0,
                    max_retries=3,
                    backoff_base=5.0,
                    cancel_event=cancel_ev,
                    request_fn=timeout_first,
                )
            self.assertEqual(calls["n"], 1)
            self.assertIn("גילוי מודלים", str(ctx.exception))
        finally:
            t.cancel()

    def test_04_model_discovery_pagination_capability_filter_and_no_invented_models(self):
        # Gemini: 2 עמודי pagination + סינון embedding/imagen/tts
        gem_calls = []

        def gemini_req(method, url, **kwargs):
            params = kwargs.get("params") or {}
            gem_calls.append(params.get("pageToken"))
            if not params.get("pageToken"):
                return _FakeResponse(
                    200,
                    {
                        "models": [
                            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
                            {"name": "models/imagen-3.0-generate-002", "supportedGenerationMethods": ["generateContent"]},
                            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
                        ],
                        "nextPageToken": "page_2_tok",
                    },
                )
            return _FakeResponse(
                200,
                {
                    "models": [
                        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
                    ]
                },
            )

        res_g = discover_models_detailed(
            "gemini",
            "AIzaFakeTestKey",
            capability="text",
            request_fn=gemini_req,
        )
        self.assertEqual(gem_calls, [None, "page_2_tok"])
        self.assertEqual(res_g["pages_fetched"], 2)
        self.assertIn("gemini-2.5-flash", res_g["models"])
        self.assertIn("gemini-2.5-pro", res_g["models"])
        self.assertNotIn("text-embedding-004", res_g["models"])
        self.assertNotIn("imagen-3.0-generate-002", res_g["models"])
        self.assertNotIn("gemini-2.5-flash-preview-tts", res_g["models"])

        # Anthropic pagination עם has_more + after_id
        ant_calls = []

        def anthropic_req(method, url, **kwargs):
            params = kwargs.get("params") or {}
            ant_calls.append(params.get("after_id"))
            if not params.get("after_id"):
                return _FakeResponse(
                    200,
                    {
                        "data": [{"id": "claude-3-5-haiku-latest"}],
                        "has_more": True,
                        "last_id": "claude-3-5-haiku-latest",
                    },
                )
            return _FakeResponse(
                200,
                {
                    "data": [{"id": "claude-3-7-sonnet-latest"}],
                    "has_more": False,
                },
            )

        res_a = discover_models_detailed("anthropic", "sk-ant-test", request_fn=anthropic_req)
        self.assertEqual(ant_calls, [None, "claude-3-5-haiku-latest"])
        self.assertEqual(res_a["pages_fetched"], 2)
        self.assertIn("claude-3-7-sonnet-latest", res_a["models"])

        # OpenAI תשובה שמכילה רק embedding/tts -> חייב לזרוק AICallError(category="empty"), אסור להמציא מודלים
        def openai_only_non_text(method, url, **kwargs):
            return _FakeResponse(
                200,
                {"data": [{"id": "text-embedding-3-large"}, {"id": "whisper-1"}, {"id": "tts-1"}]},
            )

        with self.assertRaises(AICallError) as ctx_empty:
            discover_models_detailed("openai", "sk-test", capability="text", request_fn=openai_only_non_text)
        self.assertEqual(ctx_empty.exception.category, "empty")

    def test_05_linguist_large_input_chunking_no_truncation_no_partial_success(self):
        # בניית טקסט של > 9000 תווים (בעבר נחתך ל-[:7000])
        paras = [f"פסקה מספר {i}. זוהי דוגמה טובה של רמב\"ם ועכ\"פ לבדיקת ניקוד מלא." for i in range(1, 160)]
        long_text = "\n\n".join(paras)
        self.assertGreater(len(long_text), 9000)

        seen_chunks = []
        settings = deepcopy(DEFAULT_SETTINGS)
        settings["providers"]["gemini"]["api_key"] = "AIzaFakeKey"

        def ok_chat(**kwargs):
            txt = kwargs["user_prompt"]
            seen_chunks.append(txt)
            sents = [s.strip() for s in txt.splitlines() if s.strip()]
            return json.dumps({"vocalized_text": txt, "sentences": sents, "glossary": []}, ensure_ascii=False)

        res = vocalize_and_expand_hebrew(long_text, settings=settings, chat_fn=ok_chat)
        self.assertGreaterEqual(len(seen_chunks), 3)
        self.assertEqual("\n\n".join(seen_chunks), long_text)
        self.assertGreaterEqual(len(res["sentences"]), 150)

        # כשל בחלק השני אינו מחזיר חלק ראשון כהצלחה
        n_calls = {"n": 0}

        def fail_second_chunk(**kwargs):
            n_calls["n"] += 1
            if n_calls["n"] >= 2:
                raise AICallError("חריגת זמן קריאה בחלק 2", stage="ניקוד", category="timeout")
            return json.dumps({"vocalized_text": "חלק א", "sentences": ["חלק א."], "glossary": []}, ensure_ascii=False)

        with self.assertRaises(AICallError):
            vocalize_and_expand_hebrew(long_text, settings=settings, chat_fn=fail_second_chunk)


class TestWholeBookPipelineAndPDFCoverage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_tmp = self.temp_dir.name
        self.user_data_dir = os.path.join(self.base_tmp, "AppDataUser")
        self.out_dir = os.path.join(self.base_tmp, "Outputs")
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

    def test_06_pdf_full_and_partial_ocr_coverage_detection(self):
        class _FakePage:
            def __init__(self, text=None, err=None):
                self._text = text
                self._err = err

            def extract_text(self):
                if self._err:
                    raise self._err
                return self._text

        class _FakePdfReader:
            is_encrypted = False

            def __init__(self, f, strict=False):
                self.pages = [
                    _FakePage("Page 1 sentence one. Page 1 sentence two."),
                    _FakePage("   "),  # עמוד סרוק / דורש OCR
                    _FakePage(err=RuntimeError("corrupt stream")),  # כשל חילוץ עמוד 3
                    _FakePage("Page 4 final sentence."),
                ]

        fake_pypdf2 = types.SimpleNamespace(PdfReader=_FakePdfReader)
        dummy_pdf = os.path.join(self.base_tmp, "partial_scan.pdf")
        with open(dummy_pdf, "wb") as f:
            f.write(b"%PDF-1.4 mock")

        with mock.patch.dict(sys.modules, {"PyPDF2": fake_pypdf2}):
            book = load_file_content(dummy_pdf)

        cov = book.get("pdf_coverage")
        self.assertIsNotNone(cov)
        self.assertEqual(cov["total_pages"], 4)
        self.assertEqual(cov["extracted_pages"], [1, 4])
        self.assertEqual(cov["empty_or_ocr_pages"], [2])
        self.assertEqual([fp["page"] for fp in cov["failed_pages"]], [3])
        self.assertFalse(cov["is_complete_extraction"])
        self.assertIn("OCR", cov["warning_message"])

        # אכיפה שהספר החלקי לא יוכרז שלם בעיבוד ספר מלא
        val = validate_book_completeness(book)
        self.assertFalse(val["is_complete"])
        res = run_whole_book_script_pipeline(book=book, settings=deepcopy(DEFAULT_SETTINGS), store=self.store)
        self.assertFalse(res["success"])
        self.assertEqual(res["stage"], "validation")

    def test_07_whole_book_all_chapters_ordered_strict_gate_and_midbook_start(self):
        # ספר בעל 4 פרקים (כולל פרק ארוך בן 40 משפטים!), מדמים שהפרק הפעיל במשתמש הוא chapter_index=2 (באמצע הספר)
        long_ch = " ".join(f"Long chapter sentence number {i} is meaningful." for i in range(1, 41))
        book = {
            "id": "multi_book_4ch",
            "title": "ספר מחקר רב פרקים",
            "chapters": [
                {"title": "פרק א׳ פתיחה", "text": "Opening line A1. Opening line A2."},
                {"title": "פרק ב׳ יסודות", "text": "Basics line B1. Basics line B2."},
                {"title": "פרק ג׳ הארוך", "text": long_ch},
                {"title": "פרק ד׳ סיום", "text": "Closing line D1. Closing line D2."},
            ],
        }
        settings = deepcopy(DEFAULT_SETTINGS)

        events = []

        def trans_fn(sent, eng, sl, tl):
            events.append(("trans", sent[:20]))
            return f"[תרגום] {sent}"

        def script_chat(provider_id, api_key, model, system_prompt, user_prompt, **kwargs):
            lines = []
            for raw in user_prompt.splitlines():
                if raw.startswith("[u"):
                    end_b = raw.find("]")
                    uid = raw[1:end_b]
                    txt = raw[end_b + 1:].strip()
                    idx_n = int(uid[1:])
                    lines.append({
                        "unitId": uid,
                        "speakerRole": "teacher" if idx_n % 2 == 1 else "student",
                        "style": "בהיר",
                        "text": txt,
                    })
            events.append(("script", len(lines)))
            return json.dumps({"title": "פרק", "lines": lines}, ensure_ascii=False)

        wav_calls = []

        def wav_fn(txt, voice, spd):
            wav_calls.append(txt[:25])
            events.append(("tts", len(txt)))
            return _make_wav_bytes(320)

        out_wav = os.path.join(self.out_dir, "whole_book.wav")
        res = run_whole_book_export_pipeline(
            book=book,
            output_path=out_wav,
            package_mode="unified_wav",
            settings=settings,
            tts_opts={"output_format": "wav", "default_voice": "sapi5", "speed": 1.0},
            store=self.store,
            single_translate_fn=trans_fn,
            chat_fn=script_chat,
            wav_synth_fn=wav_fn,
        )
        self.assertTrue(res["success"], res.get("error"))
        self.assertEqual(res["scope"], "book")
        self.assertEqual(res["chapters_processed"], [0, 1, 2, 3])
        self.assertTrue(os.path.exists(out_wav))

        # אימות סדר שערים קשיח: כל התרגומים והתסריטים מסתיימים לפני שמתחיל TTS ראשון!
        first_tts_idx = next(i for i, e in enumerate(events) if e[0] == "tts")
        last_script_idx = max(i for i, e in enumerate(events) if e[0] == "script")
        last_trans_idx = max(i for i, e in enumerate(events) if e[0] == "trans")
        self.assertGreater(first_tts_idx, last_script_idx)
        self.assertGreater(last_script_idx, last_trans_idx)

        # אימות שקובץ ה-WAV המאוחד קריא ותקני עם יותר מפריים אחד וכולל את כל הפרקים בהזרמה
        with wave.open(out_wav, "rb") as w_out:
            self.assertEqual(w_out.getnchannels(), 1)
            self.assertGreater(w_out.getnframes(), 320 * 4)

        # בדיקת מקרה כישלון בשער: אם פרק 2 נכשל בתסריט — אפס קריאות TTS מתבצעות!
        book2 = deepcopy(book)
        book2["id"] = "gate_fail_book"
        tts_should_stay_empty = []
        script_n = {"n": 0}

        def failing_script_ch2(**kwargs):
            script_n["n"] += 1
            if script_n["n"] == 2:
                raise AICallError("כשל זמני בפרק 2", stage="תסריט AI", category="timeout")
            return script_chat(**kwargs)

        res_gate = run_whole_book_export_pipeline(
            book=book2,
            output_path=os.path.join(self.out_dir, "never_written.wav"),
            package_mode="unified_wav",
            settings=settings,
            tts_opts={"output_format": "wav", "default_voice": "sapi5", "speed": 1.0},
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: f"[עב] {s}",
            chat_fn=failing_script_ch2,
            wav_synth_fn=lambda txt, v, spd: tts_should_stay_empty.append(txt) or _make_wav_bytes(),
        )
        self.assertFalse(res_gate["success"])
        self.assertEqual(res_gate["stage"], "script")
        self.assertEqual(len(tts_should_stay_empty), 0)
        self.assertFalse(os.path.exists(os.path.join(self.out_dir, "never_written.wav")))

    def test_08_midbook_failure_fresh_process_resume_and_partial_invalidation(self):
        book = {
            "id": "resume_book_3ch",
            "title": "ספר להמשך אחרי כשל",
            "chapters": [
                {"title": "פרק 1", "text": "One A. One B."},
                {"title": "פרק 2", "text": "Two A. Two B."},
                {"title": "פרק 3", "text": "Three A. Three B."},
            ],
        }
        settings = deepcopy(DEFAULT_SETTINGS)

        # הרצה 1 נכשלת בסינתזת פרק 2 (אחרי שתרגום+תסריט של 0,1,2 כבר נשמרו אטומית ו-TTS של 0 נשמר במטמון!)
        wav_seen_run1 = []

        def wav_fail_on_ch2(txt, v, spd):
            wav_seen_run1.append(txt)
            if "Two A" in txt:
                raise RuntimeError("השמעת TTS פרק 2 נקטעה")
            return _make_wav_bytes(300)

        script_chat, script_calls_run1 = self._make_chat_counter()
        out_zip = os.path.join(self.out_dir, "book_archive.zip")
        res1 = run_whole_book_export_pipeline(
            book=book,
            output_path=out_zip,
            package_mode="zip",
            settings=settings,
            tts_opts={"output_format": "wav", "default_voice": "sapi5", "speed": 1.0},
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: f"תרגום({s})",
            chat_fn=script_chat,
            wav_synth_fn=wav_fail_on_ch2,
        )
        self.assertFalse(res1["success"])
        self.assertEqual(res1["status"], "failed")
        self.assertEqual(res1["stage"], "tts_export")
        self.assertEqual(res1["chapter_number"], 2)
        self.assertFalse(os.path.exists(out_zip))

        # שלב 2: הפעלה בתהליך חדש לגמרי (subprocess) - אסור לתרגם/לתסרט/לסנתז שוב את פרק 0!
        probe_code = f"""
import json, os, sys, wave, io
sys.path.insert(0, {REPO_DIR!r})
os.environ['PORMYCODEX_DATA_DIR'] = {self.user_data_dir!r}
from copy import deepcopy
from settings_manager import DEFAULT_SETTINGS
from library_store import LibraryStore
from export_pipeline import run_whole_book_export_pipeline

def _wav_ok(frames=300):
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b'\\x01\\x00'*frames)
    return b.getvalue()

trans_calls, script_calls, wav_calls = [], [], []
def t_fn(s, e, sl, tl):
    trans_calls.append(s)
    return f'תרגום({{s}})'
def s_fn(provider_id, api_key, model, system_prompt, user_prompt, **kw):
    script_calls.append(user_prompt)
    lines = []
    for raw in user_prompt.splitlines():
        if raw.startswith('[u'):
            eb = raw.find(']')
            uid = raw[1:eb]
            lines.append({{'unitId': uid, 'speakerRole': 'narrator', 'style': 'ברור', 'text': raw[eb+1:].strip()}})
    return json.dumps({{'title': 'ספר', 'lines': lines}}, ensure_ascii=False)
def w_fn(txt, v, spd):
    wav_calls.append(txt)
    return _wav_ok()

book = {book!r}
st = LibraryStore()
res2 = run_whole_book_export_pipeline(
    book=book,
    output_path={out_zip!r},
    package_mode='zip',
    settings=deepcopy(DEFAULT_SETTINGS),
    tts_opts={{'output_format': 'wav', 'default_voice': 'sapi5', 'speed': 1.0}},
    store=st,
    single_translate_fn=t_fn,
    chat_fn=s_fn,
    wav_synth_fn=w_fn,
)
assert res2['success'], res2
# התרגום והתסריט של שלושת הפרקים כבר אומתו ונשמרו אטומית לפני הכשל -> 0 קריאות חוזרות!
assert len(trans_calls) == 0, trans_calls
assert len(script_calls) == 0, script_calls
# פרק 0 כבר היה במטמון ה-TTS התקף -> מסנתזים רק פרק 1 ופרק 2!
assert len(wav_calls) == 2, wav_calls
assert all('One A' not in x for x in wav_calls), wav_calls
print(json.dumps({{'resumed_ok': True, 'wav_calls': len(wav_calls)}}))
"""
        proc = subprocess.run([sys.executable, "-c", probe_code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        self.assertTrue(os.path.exists(out_zip))

        # אימות ZIP ומבנה ה-manifest.json
        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
            self.assertIn("manifest.json", names)
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest["scope"], "book")
            self.assertEqual(manifest["chapter_count"], 3)
            self.assertEqual([c["chapter_index"] for c in manifest["chapters"]], [0, 1, 2])
            for item in manifest["chapters"]:
                self.assertIn(item["filename"], names)

        # שלב 3: שינוי טקסט של פרק 1 בלבד פוסל רק את פרק 1 ולא מחדש פרק 0 או פרק 2!
        edited_book = deepcopy(book)
        edited_book["chapters"][1]["text"] = "Two edited A. Two edited B."
        assess_ed = assess_book_pipeline(
            edited_book,
            settings=settings,
            tts_opts={"output_format": "wav", "default_voice": "sapi5", "speed": 1.0},
            store=self.store,
        )
        self.assertTrue(assess_ed["chapters"][0]["tts_ready"])
        self.assertFalse(assess_ed["chapters"][1]["translation_ready"])
        self.assertFalse(assess_ed["chapters"][1]["tts_ready"])
        self.assertTrue(assess_ed["chapters"][2]["tts_ready"])

    def test_09_packaging_folder_and_mp3_refuses_blind_binary_concat(self):
        ch_files = []
        for i in range(3):
            p = os.path.join(self.base_tmp, f"ch_{i}.mp3")
            with open(p, "wb") as f:
                f.write(_make_mp3_bytes(f"CH{i}"))
            ch_files.append({
                "chapter_index": i,
                "chapter_title": f"פרק מספר {i + 1}",
                "audio_path": p,
                "line_count": 4,
            })

        # 1. ללא ffmpeg וללא mp3_merge_fn -> איחוד MP3 חייב לסרב לבצע חיבור בינארי עיוור!
        with mock.patch("audio_exporter.find_ffmpeg_executable", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                merge_mp3_files_decoded([c["audio_path"] for c in ch_files], os.path.join(self.out_dir, "bad.mp3"))
            self.assertIn("FFmpeg", str(ctx.exception))

        # 2. אריזת folder יוצרת קבצים ממוספרים + manifest מסודר
        out_folder = os.path.join(self.out_dir, "BookFolderExport")
        res_fold = package_book_audio(
            book_id="bk_fold",
            book_title="ספר תיקייה",
            chapter_entries=ch_files,
            output_path=out_folder,
            package_mode="folder",
        )
        self.assertTrue(os.path.isdir(res_fold["output_path"]))
        self.assertTrue(os.path.exists(res_fold["manifest_path"]))
        with open(res_fold["manifest_path"], "r", encoding="utf-8") as mf:
            manifest = json.load(mf)
        filenames = [c["filename"] for c in manifest["chapters"]]
        self.assertEqual(len(filenames), 3)
        self.assertTrue(filenames[0].startswith("001_"))
        self.assertTrue(filenames[1].startswith("002_"))
        self.assertTrue(filenames[2].startswith("003_"))
        for fn in filenames:
            self.assertTrue(os.path.exists(os.path.join(out_folder, fn)))

    def _make_chat_counter(self):
        calls = []

        def _chat(provider_id, api_key, model, system_prompt, user_prompt, **kwargs):
            calls.append(user_prompt)
            lines = []
            for raw in user_prompt.splitlines():
                if raw.startswith("[u"):
                    eb = raw.find("]")
                    uid = raw[1:eb]
                    lines.append({
                        "unitId": uid,
                        "speakerRole": "narrator",
                        "style": "רגוע",
                        "text": raw[eb + 1:].strip(),
                    })
            return json.dumps({"title": "פרק", "lines": lines}, ensure_ascii=False)

        return _chat, calls


if __name__ == "__main__":
    unittest.main(verbosity=2)
