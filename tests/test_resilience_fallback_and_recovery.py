# -*- coding: utf-8 -*-
"""
בדיקות עמידות, התאוששות מחסימות (HTTP 418 / NetFree / HTML), שמירה אטומית הדרגתית ברמת יחידה ואצווה,
והמשך מדויק מתהליך חדש לחלוטין (Fresh Process Restart) ללא קריאות כפולות או פסילת עבודה קודמת.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from copy import deepcopy
from typing import Any, Dict
from unittest import mock

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from ai_providers import (
    AICallError,
    ProviderCircuitBreaker,
    chat_completion,
    detect_blocked_response,
)
from export_pipeline import (
    IncompleteArtifactError,
    MissingArtifactError,
    assess_book_pipeline,
    assess_chapter_pipeline,
    ensure_script_for_chapter,
    ensure_translation_for_chapter,
    run_whole_book_export_pipeline,
    run_whole_book_script_pipeline,
)
from library_store import LibraryStore
from settings_manager import DEFAULT_SETTINGS
from translator_engine import (
    TranslationCancelledError,
    TranslationProviderError,
    translate_required_sentences,
)


NETFREE_418_HTML = """<!DOCTYPE html>
<html>
<head><title>חסימת אינטרנט</title></head>
<body>
  <h1>הגישה לאתר זה נחסמה על ידי סינון netfree.link</h1>
  <p>כתובת translate.googleapis.com סוננה זמנית עקב מדיניות הסינון (418).</p>
</body>
</html>
"""


class _FakeHTTPResp:
    def __init__(self, status_code: int, body: Any, headers: Dict = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        if isinstance(body, str):
            self.text = body
        else:
            self.text = json.dumps(body, ensure_ascii=False)

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status_code) < 400

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("Invalid JSON response")


def _make_mock_script_chat_batch():
    calls = []

    def _chat(user_prompt: str = "", **kwargs):
        calls.append(user_prompt)
        uids = re.findall(r"\[(u\d+)\]", user_prompt)
        lines = []
        for i, uid in enumerate(uids):
            role = "teacher" if i % 2 == 0 else "student"
            style = "רגוע" if role == "teacher" else "סקרן"
            lines.append({
                "unitId": uid,
                "speakerRole": role,
                "style": style,
                "text": f"שורה לתסריט עבור {uid}",
            })
        return json.dumps({"title": "כותרת פרק", "lines": lines}, ensure_ascii=False)

    return _chat, calls


def _make_wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 160)
    return buf.getvalue()


class TestResilienceFallbackAndDurableRecovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.temp_dir.name, "UserData")
        os.makedirs(self.data_dir, exist_ok=True)
        self.state_path = os.path.join(self.data_dir, "library_state.json")
        self.store = LibraryStore(state_path=self.state_path)
        self.out_dir = os.path.join(self.temp_dir.name, "out")
        os.makedirs(self.out_dir, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_netfree_418_html_block_detected_and_masked_never_saved(self):
        b_info = detect_blocked_response(
            _FakeHTTPResp(418, NETFREE_418_HTML, {"Content-Type": "text/html"}),
        )
        self.assertIsNotNone(b_info)
        self.assertTrue(b_info["is_blocked"])
        self.assertIn("NetFree", b_info["filter_name"])

        # translate_required_sentences מזהה 418 / HTML, עוצר מיידית, ואינו מחזיר HTML כתרגום!
        def _req_418(method, url, params=None, headers=None, timeout=None, **kwargs):
            return _FakeHTTPResp(418, NETFREE_418_HTML, {"Content-Type": "text/html"})

        with self.assertRaises(TranslationProviderError) as ctx:
            translate_required_sentences(
                ["Hello world."],
                engine="google_free",
                source_lang="en",
                target_lang="he",
                request_fn=_req_418,
            )
        err = ctx.exception
        self.assertTrue(err.is_blocked)
        self.assertEqual(err.status_code, 418)
        err_msg = str(err)
        self.assertNotIn("<html", err_msg.lower())
        self.assertNotIn("<body>", err_msg.lower())
        self.assertIn("חסום", err_msg)

        # chat_completion מזהה HTML כשביקשנו JSON, ומסתיר מפתחות API
        settings = deepcopy(DEFAULT_SETTINGS)
        settings["active_provider"] = "openai"
        secret = "sk-SecretLeakCheck123456789"
        settings["providers"]["openai"]["api_key"] = secret
        settings["providers"]["openai"]["model"] = "gpt-4o"

        def _chat_html(method, url, *args, **kwargs):
            return _FakeHTTPResp(418, NETFREE_418_HTML, {"Content-Type": "text/html"})

        with self.assertRaises(AICallError) as ai_ctx:
            chat_completion(
                provider_id="openai",
                api_key=secret,
                model="gpt-4o",
                system_prompt="sys",
                user_prompt="Hello",
                request_fn=_chat_html,
                max_retries=0,
            )
        self.assertTrue(ai_ctx.exception.is_blocked)
        self.assertNotIn("<html", str(ai_ctx.exception).lower())
        self.assertNotIn(secret, str(ai_ctx.exception))

    def test_02_mid_chapter_failure_chapter_23_unit_61_and_fresh_subprocess_resume(self):
        # מדמה ספר עם 23 פרקים (פרקים 1..22 מושלמים, בפרק 23 יש 10 משפטים ונכשל ביחידה 7 לאחר 6/10)
        # בדיוק כמו תרחיש המשתמש: 22 פרקים שלמים + 60% מפרק 23 נשמרים אטומית;
        # הפעלה מחדש בתהליך Python חדש לגמרי תתרגם רק את 4 המשפטים החסרים בפרק 23 בלי אף קריאה חוזרת!
        chapters = []
        for idx in range(22):
            chapters.append({
                "title": f"פרק {idx + 1}",
                "text": f"Ch{idx + 1} S1. Ch{idx + 1} S2.",
            })
        ch23_sents = [f"Chapter23 unit {i} source." for i in range(1, 11)]
        chapters.append({"title": "פרק 23", "text": " ".join(ch23_sents)})
        book = {"id": "book_23ch", "title": "ספר רב פרקים", "chapters": chapters}

        settings = deepcopy(DEFAULT_SETTINGS)
        settings["translation"] = {
            "engine": "google_free",
            "source_lang": "en",
            "target_lang": "he",
            "fallback_to_ai_on_failure": False,
        }
        settings["providers"]["gemini"]["api_key"] = "AIzaDummy"
        settings["providers"]["gemini"]["model"] = "gemini-2.5-flash"

        http_calls = []

        def _req_block_ch23_u7(method, url, params=None, headers=None, timeout=None, **kwargs):
            q = (params or {}).get("q", "")
            http_calls.append(q)
            if "Chapter23 unit 7" in q:
                return _FakeHTTPResp(418, NETFREE_418_HTML, {"Content-Type": "text/html"})
            return _FakeHTTPResp(200, [[[f"תרגום({q})", q, None, None]]], {"Content-Type": "application/json"})

        chat_fn, _ = _make_mock_script_chat_batch()
        res1 = run_whole_book_script_pipeline(
            book=book,
            settings=settings,
            store=self.store,
            chat_fn=chat_fn,
            request_fn=_req_block_ch23_u7,
        )
        self.assertFalse(res1["success"])
        self.assertEqual(res1["status"], "blocked")
        self.assertEqual(res1["stage"], "translation")
        self.assertEqual(res1["chapter_index"], 22)
        self.assertEqual(res1["chapter_number"], 23)
        self.assertEqual(res1["unit_index"], 6)
        self.assertEqual(res1["unit_id"], "u7")

        # מוודאים: פרקים 0..21 הושלמו, בפרק 22 (פרק 23) נשמרו 6 יחידות, אך תרגום הפרק עצמו אינו מסומן כשלם!
        for c in range(22):
            art = self.store.get_chapter_artifacts("book_23ch", c)
            self.assertIsNotNone(art.get("translation"))
            self.assertTrue(art["translation"]["is_complete"])

        ch23_art = self.store.get_chapter_artifacts("book_23ch", 22)
        self.assertIsNone(ch23_art.get("translation"))
        tp23 = ch23_art.get("translation_progress") or {}
        self.assertEqual(tp23.get("completed_count"), 6)
        self.assertEqual(tp23.get("failed_count"), 1)
        self.assertEqual(tp23.get("next_unit_index"), 6)
        self.assertEqual(tp23.get("next_unit_id"), "u7")

        # ווידוא שאף תג HTML לא נשמר בדיסק!
        with open(self.store.state_path, "r", encoding="utf-8") as f:
            raw_disk = f.read()
        self.assertNotIn("<html", raw_disk.lower())
        self.assertNotIn("netfree.link</body>", raw_disk.lower())

        # הפעלת תהליך Python חדש לחלוטין (Fresh Subprocess) הממשיך מאותו state_path!
        book_json = json.dumps(book, ensure_ascii=False)
        script_code = f"""
import json, os, re, sys
sys.path.insert(0, {REPO_DIR!r})
from copy import deepcopy
from library_store import LibraryStore
from export_pipeline import run_whole_book_script_pipeline, assess_book_pipeline
from settings_manager import DEFAULT_SETTINGS

st = LibraryStore(state_path={self.state_path!r})
book = json.loads({book_json!r})
cfg = deepcopy(DEFAULT_SETTINGS)
cfg["translation"] = {{"engine": "google_free", "source_lang": "en", "target_lang": "he", "fallback_to_ai_on_failure": False}}
cfg["providers"]["gemini"]["api_key"] = "AIzaDummy"
cfg["providers"]["gemini"]["model"] = "gemini-2.5-flash"

pre = assess_book_pipeline(book=book, settings=cfg, store=st)
assert pre["translations_ready"] == 22, pre["translations_ready"]
assert pre["resume_point"]["chapter_number"] == 23, pre["resume_point"]
assert pre["resume_point"]["unit_id"] == "u7", pre["resume_point"]

trans_calls = []
def single_trans(text, engine, sl, tl):
    trans_calls.append(text)
    return "תרגום(" + text + ")"

def chat_fn(user_prompt="", **kwargs):
    uids = re.findall(r"\\[(u\\d+)\\]", user_prompt)
    lines = [{{"unitId": u, "speakerRole": "teacher", "style": "רגוע", "text": "קריינות " + u}} for u in uids]
    return json.dumps({{"title": "כ", "lines": lines}}, ensure_ascii=False)

res2 = run_whole_book_script_pipeline(
    book=book,
    settings=cfg,
    store=st,
    single_translate_fn=single_trans,
    chat_fn=chat_fn,
)
assert res2["success"], res2
t23 = st.get_chapter_artifacts("book_23ch", 22)["translation"]
assert len(t23["translated_sentences"]) == 10
assert len(trans_calls) == 4, f"Expected strictly 4 remaining unit calls, got {{len(trans_calls)}}: {{trans_calls}}"
expected = ["תרגום(" + s + ")" for s in book["chapters"][22]["text"].split(". ")[:-1]] + ["תרגום(Chapter23 unit 10 source.)"]
# Ensure exact ordering without duplicates
assert [t23["translated_sentences"][i] for i in range(10)] == [
    "תרגום(Chapter23 unit " + str(i) + " source.)" for i in range(1, 11)
], t23["translated_sentences"]
print(json.dumps({{"calls": len(trans_calls), "scripts": len(res2["scripts"])}}))
"""
        proc = subprocess.run(
            [sys.executable, "-c", script_code],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        parsed = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(parsed["calls"], 4)
        self.assertEqual(parsed["scripts"], 23)

    def test_03_circuit_breaker_blocks_hammering_and_provider_switch_preserves_prior_units(self):
        # בודק גם Circuit Breaker (0 תקיעות חוזרות אחרי חסימת 418) וגם החלפת ספק מפורשת באמצע פרק עם provenance לכל יחידה
        src = "Unit one. Unit two. Unit three. Unit four. Unit five."
        settings = deepcopy(DEFAULT_SETTINGS)
        settings["translation"] = {
            "engine": "google_free",
            "source_lang": "en",
            "target_lang": "he",
            "fallback_to_ai_on_failure": False,
        }
        settings["providers"]["gemini"]["api_key"] = "AIzaTestKey"
        settings["providers"]["gemini"]["model"] = "gemini-2.5-flash"

        breaker = ProviderCircuitBreaker()
        calls_log = []

        def _req_fail_u3(method, url, params=None, headers=None, timeout=None, **kwargs):
            q = (params or {}).get("q", "")
            calls_log.append(q)
            if "Unit three" in q:
                return _FakeHTTPResp(418, NETFREE_418_HTML, {"Content-Type": "text/html"})
            return _FakeHTTPResp(200, [[[f"עברית:{q}", q, None, None]]], {"Content-Type": "application/json"})

        with self.assertRaises(TranslationProviderError):
            ensure_translation_for_chapter(
                book_id="bk_switch",
                chapter_index=0,
                source_text=src,
                settings=settings,
                store=self.store,
                circuit_breaker=breaker,
                request_fn=_req_fail_u3,
            )

        # קריאה חוזרת עם google_free כשה-circuit breaker כבר פתוח -> נחסם ב-0 קריאות רשת חדשות!
        self.assertTrue(breaker.is_open("google_free"))
        before_cnt = len(calls_log)
        with self.assertRaises(TranslationProviderError):
            ensure_translation_for_chapter(
                book_id="bk_switch",
                chapter_index=0,
                source_text=src,
                settings=settings,
                store=self.store,
                circuit_breaker=breaker,
                request_fn=_req_fail_u3,
            )
        self.assertEqual(len(calls_log), before_cnt)

        # כעת המשתמש מחליף ספק מפורשות ל-'ai' וממשיך -> 2 היחידות הראשונות (עברית:Unit one / two) נשמרות!
        ai_calls = []

        def _ai_fallback(text, engine, sl, tl):
            ai_calls.append(text)
            return f"AI:{text}"

        t_rec = ensure_translation_for_chapter(
            book_id="bk_switch",
            chapter_index=0,
            source_text=src,
            settings=settings,
            translation_opts={"engine": "ai", "source_lang": "en", "target_lang": "he"},
            store=self.store,
            single_translate_fn=_ai_fallback,
        )
        self.assertTrue(t_rec["is_complete"])
        self.assertEqual(len(ai_calls), 3)  # רק יחידות 3, 4, 5!
        self.assertEqual(
            t_rec["translated_sentences"],
            ["עברית:Unit one.", "עברית:Unit two.", "AI:Unit three.", "AI:Unit four.", "AI:Unit five."],
        )
        prov = t_rec.get("unit_provenance") or {}
        self.assertEqual(prov["0"]["engine"], "google_free")
        self.assertEqual(prov["1"]["engine"], "google_free")
        self.assertEqual(prov["2"]["engine"], "ai")
        self.assertEqual(prov["4"]["engine"], "ai")

        # בדיקת Fallback מפורש שאושר על ידי המשתמש (fallback_to_ai_on_failure=True)
        ai_fb_calls = []

        def _fb_fn(text, engine, sl, tl):
            ai_fb_calls.append(text)
            return f"AI_FB:{text}"

        fb_breaker = ProviderCircuitBreaker()
        t_rec2 = ensure_translation_for_chapter(
            book_id="bk_switch_fb",
            chapter_index=0,
            source_text=src,
            settings=settings,
            translation_opts={
                "engine": "google_free",
                "source_lang": "en",
                "target_lang": "he",
                "fallback_to_ai_on_failure": True,
            },
            store=self.store,
            circuit_breaker=fb_breaker,
            request_fn=_req_fail_u3,
            fallback_translate_fn=_fb_fn,
        )
        self.assertTrue(t_rec2["is_complete"])
        self.assertTrue(t_rec2["fallback_used"])
        self.assertEqual(len(ai_fb_calls), 3)  # נשמרו 2 היחידות שכבר צלחו ב-google_free
        self.assertEqual(t_rec2["unit_provenance"]["0"]["engine"], "google_free")
        self.assertEqual(t_rec2["unit_provenance"]["2"]["engine"], "ai")

    def test_04_script_batch_failure_fresh_restart_and_ordered_dedup_merge(self):
        # 10 משפטים עם 4 יחידות באצווה = 3 אצוות: [u1..u4], [u5..u8], [u9..u10]
        src = " ".join(f"Line {i} text." for i in range(1, 11))
        settings = deepcopy(DEFAULT_SETTINGS)
        settings["providers"]["gemini"]["api_key"] = "AIzaKey"
        settings["providers"]["gemini"]["model"] = "gemini-2.5-flash"

        ensure_translation_for_chapter(
            book_id="bk_script_resume",
            chapter_index=0,
            source_text=src,
            settings=settings,
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: f"מתורגם {s}",
        )

        batch_calls_1 = []

        def _fail_batch_2(user_prompt: str = "", **kwargs):
            batch_calls_1.append(user_prompt)
            uids = re.findall(r"\[(u\d+)\]", user_prompt)
            if "u5" in uids:
                raise AICallError("Read Timeout in batch 2", stage="תסריט", category="timeout")
            lines = [
                {"unitId": u, "speakerRole": "teacher", "style": "רגוע", "text": f"נאמר {u}"}
                for u in uids
            ]
            return json.dumps({"title": "כותרת א", "lines": lines}, ensure_ascii=False)

        with self.assertRaises(AICallError):
            ensure_script_for_chapter(
                book_id="bk_script_resume",
                chapter_index=0,
                source_text=src,
                book_title="ספר תסריט",
                settings=settings,
                store=self.store,
                chat_fn=_fail_batch_2,
                units_per_batch=4,
            )

        # אצווה 1 נשמרה אטומית בדיסק, אולם art["script"] עדיין None!
        arts = self.store.get_chapter_artifacts("bk_script_resume", 0)
        self.assertIsNone(arts.get("script"))
        sp = arts.get("script_progress") or {}
        self.assertEqual(sp.get("completed_count"), 4)
        self.assertEqual(sp.get("next_batch_index"), 1)
        self.assertEqual(sp.get("next_unit_id"), "u5")

        # המשך בתהליך חדש (מוודא 0 קריאות עבור אצווה 1, ומחזיר בכוונה כפילות קטנה מאצווה 2 שתידחה במיזוג)
        fresh_store = LibraryStore(state_path=self.state_path)
        batch_calls_2 = []

        def _ok_remaining_batches(user_prompt: str = "", **kwargs):
            batch_calls_2.append(user_prompt)
            uids = re.findall(r"\[(u\d+)\]", user_prompt)
            lines = []
            # גם אם המודל פלט בטעות שורה השייכת כבר ליחידה מאצווה קודמת — המיזוג מסנן כפילויות בין אצוות!
            if "u5" in uids:
                lines.append({"unitId": "u4", "speakerRole": "narrator", "style": "ניטרלי", "text": "כפילות אסורה"})
            for idx_u, u in enumerate(uids):
                lines.append({
                    "unitId": u,
                    "speakerRole": "student" if idx_u % 2 else "teacher",
                    "style": "סקרן",
                    "text": f"נאמר {u}",
                })
            return json.dumps({"title": "כותרת ב", "lines": lines}, ensure_ascii=False)

        rec = ensure_script_for_chapter(
            book_id="bk_script_resume",
            chapter_index=0,
            source_text=src,
            book_title="ספר תסריט",
            settings=settings,
            store=fresh_store,
            chat_fn=_ok_remaining_batches,
            units_per_batch=4,
        )
        self.assertTrue(rec["is_complete"])
        self.assertEqual(len(batch_calls_2), 2)  # רק אצווה 2 ואצווה 3 (0 קריאות לאצווה 1!)
        got_uids = [ln["unitId"] for ln in rec["lines"]]
        self.assertEqual(got_uids, [f"u{i}" for i in range(1, 11)])
        self.assertEqual(len(got_uids), 10)
        self.assertNotIn("כפילות אסורה", [ln["text"] for ln in rec["lines"]])

    def test_05_interrupted_write_and_cancellation_mid_backoff_preserve_saved_units(self):
        src = "S1. S2. S3. S4."
        settings = deepcopy(DEFAULT_SETTINGS)
        orig_replace = os.replace
        calls = []

        def _t_ok(text, engine, sl, tl):
            calls.append(text)
            return f"ת:{text}"

        # מדמה כשל כתיבה פתאומי ב-os.replace בזמן שמירת היחידה השלישית
        def _flaky_replace(src_p, dst_p):
            if len(calls) >= 3 and str(dst_p).endswith("library_state.json"):
                raise OSError("Simulated power loss during atomic replace")
            return orig_replace(src_p, dst_p)

        with mock.patch("library_store.os.replace", side_effect=_flaky_replace):
            with self.assertRaises((OSError, TranslationProviderError)):
                ensure_translation_for_chapter(
                    book_id="bk_io",
                    chapter_index=0,
                    source_text=src,
                    settings=settings,
                    store=self.store,
                    single_translate_fn=_t_ok,
                )

        # יחידות 1 ו-2 שכבר נכתבו בהצלחה לפני הכשל נשמרות על הדיסק בלי פגם!
        tp = self.store.get_chapter_artifacts("bk_io", 0).get("translation_progress") or {}
        self.assertEqual(tp.get("completed_count"), 2)

        # בדיקת ביטול מהיר באמצע backoff/retry והמשך תקין
        cancel_ev = threading.Event()
        backoff_calls = []

        def _req_retry_then_cancel(method, url, params=None, headers=None, timeout=None, **kwargs):
            q = (params or {}).get("q", "")
            backoff_calls.append(q)
            if "S3" in q:
                # ביטול אחרי המתנה קצרצרה בזמן backoff
                threading.Timer(0.05, cancel_ev.set).start()
                return _FakeHTTPResp(503, {"error": "temporary"}, {"Content-Type": "application/json"})
            return _FakeHTTPResp(200, [[[f"ת:{q}", q, None, None]]], {"Content-Type": "application/json"})

        with self.assertRaises((TranslationCancelledError, Exception)):
            ensure_translation_for_chapter(
                book_id="bk_io",
                chapter_index=0,
                source_text=src,
                settings=settings,
                store=self.store,
                cancel_event=cancel_ev,
                request_fn=_req_retry_then_cancel,
            )

        # ממשיכים מהנקודה שנשמרה -> מתרגם רק את S3 ו-S4 (0 קריאות ל-S1 ו-S2!)
        final_calls = []
        rec = ensure_translation_for_chapter(
            book_id="bk_io",
            chapter_index=0,
            source_text=src,
            settings=settings,
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: (final_calls.append(s), f"ת:{s}")[1],
        )
        self.assertTrue(rec["is_complete"])
        self.assertEqual(final_calls, ["S3.", "S4."])

    def test_06_targeted_invalidation_and_partial_continuation_vs_full_export_gate(self):
        # 1. Targeted unit invalidation: edit sentence #2 out of 4 -> only sentence #2 re-translated + old version archived
        src_v1 = "One A. Two A. Three A. Four A."
        settings = deepcopy(DEFAULT_SETTINGS)
        settings["providers"]["gemini"]["api_key"] = "AIzaTest"
        settings["providers"]["gemini"]["model"] = "gemini-2.5-flash"

        calls1 = []
        ensure_translation_for_chapter(
            book_id="bk_target",
            chapter_index=0,
            source_text=src_v1,
            settings=settings,
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: (calls1.append(s), f"T1:{s}")[1],
        )
        self.assertEqual(len(calls1), 4)

        src_v2 = "One A. Two EDITED. Three A. Four A."
        calls2 = []
        t2 = ensure_translation_for_chapter(
            book_id="bk_target",
            chapter_index=0,
            source_text=src_v2,
            settings=settings,
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: (calls2.append(s), f"T2:{s}")[1],
        )
        self.assertEqual(calls2, ["Two EDITED."])
        self.assertEqual(
            t2["translated_sentences"],
            ["T1:One A.", "T2:Two EDITED.", "T1:Three A.", "T1:Four A."],
        )
        # הגרסה הקודמת נשמרה בהיסטוריה וניתנת לשחזור (אינה נמחקת!)
        arts_t = self.store.get_chapter_artifacts("bk_target", 0)
        self.assertEqual(len(arts_t.get("translation_history") or []), 1)

        # 2. Partial book continuation + strict export gate (0 TTS calls when any chapter incomplete)
        book = {
            "id": "bk_partial",
            "title": "ספר עם פרק כושל",
            "chapters": [
                {"title": "פרק 1", "text": "First chapter."},
                {"title": "פרק 2", "text": "Second BROKEN chapter."},
                {"title": "פרק 3", "text": "Third chapter."},
            ],
        }
        chat_fn, _ = _make_mock_script_chat_batch()

        def _fail_ch2(s, e, sl, tl):
            if "BROKEN" in s:
                raise TranslationProviderError("Invalid output on ch2", category="invalid_response", is_blocked=False)
            return f"תרגום:{s}"

        res_partial = run_whole_book_script_pipeline(
            book=book,
            settings=settings,
            store=self.store,
            single_translate_fn=_fail_ch2,
            chat_fn=chat_fn,
            continue_on_chapter_failure=True,
        )
        self.assertFalse(res_partial["success"])
        self.assertEqual(res_partial["status"], "partial")
        self.assertEqual(res_partial["completed_scripts"], [0, 2])
        self.assertEqual(res_partial["needs_attention_chapters"], [1])

        # ניסיון יצוא של הספר כולו בזמן שפרק 2 נכשל חייב להיחסם עם 0 קריאות TTS!
        tts_calls = []
        out_wav = os.path.join(self.out_dir, "blocked_partial.wav")
        res_exp_blocked = run_whole_book_export_pipeline(
            book=book,
            output_path=out_wav,
            package_mode="unified_wav",
            settings=settings,
            tts_opts={"output_format": "wav", "default_voice": "sapi5", "speed": 1.0},
            store=self.store,
            single_translate_fn=_fail_ch2,
            chat_fn=chat_fn,
            wav_synth_fn=lambda txt, v, spd: (tts_calls.append(txt), _make_wav_bytes())[1],
        )
        self.assertFalse(res_exp_blocked["success"])
        self.assertEqual(len(tts_calls), 0)
        self.assertFalse(os.path.exists(out_wav))

        # כעת מתקנים רק את פרק 2 -> היצוא מצליח בלי לתרגם או לייצר תסריט שוב לפרקים 1 ו-3!
        repair_trans_calls = []
        res_repair = run_whole_book_export_pipeline(
            book=book,
            output_path=out_wav,
            package_mode="unified_wav",
            settings=settings,
            tts_opts={"output_format": "wav", "default_voice": "sapi5", "speed": 1.0},
            store=self.store,
            single_translate_fn=lambda s, e, sl, tl: (repair_trans_calls.append(s), f"תוקן:{s}")[1],
            chat_fn=chat_fn,
            wav_synth_fn=lambda txt, v, spd: _make_wav_bytes(),
        )
        self.assertTrue(res_repair["success"], res_repair.get("error"))
        self.assertEqual(repair_trans_calls, ["Second BROKEN chapter."])
        self.assertTrue(os.path.exists(out_wav))

    def test_07_api_request_pacing_429_retry_delay_adaptive_slowdown_and_cancellation(self):
        from ai_providers import APIRequestPacer, AICancelledError, _parse_retry_after_seconds, _rank_gemini_model

        # 1. חילוץ זמן המתנה מ-Gemini 429 JSON (details.retryDelay וגם message וגם Retry-After header)
        r_details = _FakeHTTPResp(
            429,
            {
                "error": {
                    "code": 429,
                    "message": "Resource has been exhausted.",
                    "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "22s"}],
                }
            },
            {"Content-Type": "application/json"},
        )
        self.assertAlmostEqual(_parse_retry_after_seconds(r_details), 22.0)

        r_msg = _FakeHTTPResp(
            429,
            {"error": {"code": 429, "message": "Quota exceeded. Please retry in 18.5s."}},
            {"Content-Type": "application/json"},
        )
        self.assertAlmostEqual(_parse_retry_after_seconds(r_msg), 18.5)

        r_hdr = _FakeHTTPResp(429, {}, {"Retry-After": "15"})
        self.assertAlmostEqual(_parse_retry_after_seconds(r_hdr), 15.0)

        # 2. אכיפת מרווח מינימלי בין קריאות עוקבות + כיבוד 429 retryDelay > 12s + האטה אדפטיבית והרפיה הדרגתית
        pacer = APIRequestPacer()
        fake_now = [100.0]
        sleeps = []
        progress_msgs = []

        def fake_clock():
            return fake_now[0]

        def fake_sleep(sec, cancel_event, stage):
            if cancel_event is not None and cancel_event.is_set():
                raise AICancelledError(stage=stage)
            sleeps.append(round(sec, 2))
            fake_now[0] += sec

        captured_payloads = []
        call_num = [0]

        def fake_req(method, url, **kwargs):
            call_num[0] += 1
            captured_payloads.append(kwargs.get("json") or {})
            if call_num[0] == 2:
                # הקריאה השנייה מקבלת 429 עם retryDelay של 20 שניות -> חייב להמתין 20 שנ' (לא 12!) ולהצליח בניסיון הבא
                return _FakeHTTPResp(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "message": "Quota exceeded. Please retry in 20s.",
                            "details": [{"retryDelay": "20s"}],
                        }
                    },
                    {"Content-Type": "application/json"},
                )
            return _FakeHTTPResp(
                200,
                {"candidates": [{"content": {"parts": [{"text": f"ok_{call_num[0]}"}]}}]},
                {"Content-Type": "application/json"},
            )

        out1 = chat_completion(
            provider_id="gemini",
            api_key="AIzaTestPacing",
            model="gemini-4-hydrogen",
            system_prompt="sys",
            user_prompt="call 1",
            request_fn=fake_req,
            clock_fn=fake_clock,
            sleep_fn=fake_sleep,
            pacer=pacer,
            min_interval_sec=4.0,
            adaptive_slowdown=True,
            max_retries_429=4,
            progress_cb=progress_msgs.append,
        )
        self.assertEqual(out1, "ok_1")
        # דור Gemini 4 אינו שולח temperature ב-generationConfig
        self.assertNotIn("temperature", captured_payloads[0].get("generationConfig", {}))
        self.assertEqual(sleeps, [])  # קריאה ראשונה ללא המתנה

        # קריאה שנייה: תמתין 4.0 שנ' לפי הקצב -> תקבל 429 עם retryDelay=20s -> תמתין 20.0 שנ' -> תצליח!
        out2 = chat_completion(
            provider_id="gemini",
            api_key="AIzaTestPacing",
            model="gemini-4-hydrogen",
            system_prompt="sys",
            user_prompt="call 2",
            request_fn=fake_req,
            clock_fn=fake_clock,
            sleep_fn=fake_sleep,
            pacer=pacer,
            min_interval_sec=4.0,
            adaptive_slowdown=True,
            max_retries_429=4,
            progress_cb=progress_msgs.append,
        )
        self.assertEqual(out2, "ok_3")
        self.assertIn(4.0, sleeps)
        self.assertIn(20.0, sleeps)
        st = pacer.get_state("gemini:gemini-4-hydrogen")
        self.assertGreater(st["extra_delay_sec"], 0.0)
        self.assertTrue(any("HTTP 429" in m for m in progress_msgs))
        self.assertTrue(any("השהיית קצב API" in m for m in progress_msgs))

        # 3. ביטול באמצע המתנת קצב (pacing) זורק AICancelledError מיד
        cancel_ev = threading.Event()
        cancel_ev.set()
        with self.assertRaises(AICancelledError):
            pacer.wait_turn(
                "gemini:gemini-4-hydrogen",
                min_interval_sec=10.0,
                cancel_event=cancel_ev,
                clock_fn=fake_clock,
            )

        # 4. דירוג מודלי Gemini שם את gemini-4-hydrogen (Flash) מעל gemini-4-neon (Pro) ומעל דור 2.5
        ranked = sorted(
            ["gemini-2.5-pro", "gemini-4-neon", "gemini-2.5-flash", "gemini-4-hydrogen"],
            key=_rank_gemini_model,
        )
        self.assertEqual(ranked[0], "gemini-4-hydrogen")
        self.assertEqual(ranked[1], "gemini-4-neon")


if __name__ == "__main__":
    unittest.main(verbosity=2)

