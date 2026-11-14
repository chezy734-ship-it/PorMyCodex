# -*- coding: utf-8 -*-
"""
מודול תרגום עבור PorMyCodex.
כולל:
- אימות שלמות מלא לכל משפט נדרש (Zero Partial Success)
- הפרדת Connect/Read Timeout, Deadline כולל, וניסיונות חוזרים מוגבלים עם Backoff לכשלים זמניים בלבד
- תמיכה מלאה בביטול מיידי גם בזמן המתנה/Retry
- אצוות מוגבלות משפטים ותווים למניעת חריגות Read Timeout על פרקים גדולים
"""

import json
import re
from typing import Dict, Any, List, Tuple, Optional, Callable

from settings_manager import load_settings, get_active_provider_config
from ai_providers import (
    AICallError,
    AICancelledError,
    chat_completion,
    execute_http_with_retry,
    extract_json_object,
    get_provider_label,
)
from book_loader import split_into_sentences


class TranslationCancelledError(RuntimeError):
    """נזרק כאשר פעולת התרגום בוטלה במפורש."""


def _check_cancelled(cancel_event: Any = None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TranslationCancelledError("פעולת התרגום בוטלה על ידי המשתמש.")


TRANSLATION_ENGINES: List[Tuple[str, str]] = [
    ("google_free", "🌐 Google Translate חינמי (ללא מפתח API)"),
    ("mymemory", "💬 MyMemory חינמי (ללא מפתח API)"),
    ("ai", "🤖 תרגום AI חכם (הספק והמודל שבהגדרות)"),
    ("libretranslate", "🔓 LibreTranslate (שרת פתוח / מותאם)"),
]

LANGUAGE_OPTIONS: List[Tuple[str, str]] = [
    ("auto", "זיהוי שפה אוטומטי"),
    ("en", "אנגלית (English)"),
    ("he", "עברית (Hebrew)"),
    ("ar", "ערבית (Arabic)"),
    ("fr", "צרפתית (French)"),
    ("es", "ספרדית (Spanish)"),
    ("de", "גרמנית (German)"),
    ("ru", "רוסית (Russian)"),
    ("yi", "יידיש (Yiddish)"),
]

LANG_NAMES_EN = {
    "auto": "the detected source language",
    "en": "English",
    "he": "Hebrew",
    "ar": "Arabic",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "ru": "Russian",
    "yi": "Yiddish",
}


def _chunk_text(text: str, max_chars: int = 1200) -> List[str]:
    """מחלק טקסט ארוך למקטעים של פסקאות/משפטים בלי לשבור מילים."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text.strip()) if p.strip()]
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            sentences = re.split(r"(?<=[.?!;:\n])\s+", para)
            buf = ""
            for s in sentences:
                if len(buf) + len(s) + 1 <= max_chars:
                    buf = f"{buf} {s}".strip()
                else:
                    if buf:
                        chunks.append(buf)
                    buf = s[:max_chars]
            if buf:
                chunks.append(buf)
        elif len(current) + len(para) + 2 <= max_chars:
            current = f"{current}\n\n{para}".strip()
        else:
            if current:
                chunks.append(current)
            current = para

    if current:
        chunks.append(current)

    return chunks or [text.strip()]


def _build_sentence_batches(
    sentences: List[str],
    max_items: int = 12,
    max_chars: int = 2400,
) -> List[List[Tuple[int, str]]]:
    """מחלק משפטים לאצוות המוגבלות גם במספר משפטים וגם באורך תווים כולל למניעת Timeout."""
    batches: List[List[Tuple[int, str]]] = []
    current: List[Tuple[int, str]] = []
    cur_chars = 0

    for idx, sent in enumerate(sentences):
        s_len = len(sent)
        if current and (len(current) >= max_items or cur_chars + s_len > max_chars):
            batches.append(current)
            current = [(idx, sent)]
            cur_chars = s_len
        else:
            current.append((idx, sent))
            cur_chars += s_len
    if current:
        batches.append(current)
    return batches


def translate_google_free(
    text: str,
    source_lang: str = "auto",
    target_lang: str = "he",
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    sl = source_lang or "auto"
    tl = target_lang or "he"
    translated_parts: List[str] = []

    try:
        for chunk in _chunk_text(text, max_chars=1400):
            _check_cancelled(cancel_event)
            resp = execute_http_with_retry(
                "GET",
                "https://translate.googleapis.com/translate_a/single",
                provider_name="Google Translate חינמי",
                stage="תרגום (Google Translate)",
                params={"client": "gtx", "sl": sl, "tl": tl, "dt": "t", "q": chunk},
                headers={"User-Agent": "Mozilla/5.0"},
                connect_timeout=8.0,
                read_timeout=20.0,
                total_deadline=60.0,
                max_retries=2,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
            )
            data = resp.json()
            segs = data[0] if isinstance(data, list) and data else []
            chunk_out = "".join(str(seg[0]) for seg in segs if isinstance(seg, list) and seg and seg[0]).strip()
            if not chunk_out:
                raise RuntimeError("שלב 'תרגום (Google Translate)': התקבל תרגום ריק עבור אחד המקטעים.")
            translated_parts.append(chunk_out)
    except AICancelledError as e:
        raise TranslationCancelledError(str(e)) from e

    return "\n\n".join(translated_parts)


def translate_mymemory_free(
    text: str,
    source_lang: str = "en",
    target_lang: str = "he",
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    sl = "en" if not source_lang or source_lang == "auto" else source_lang
    tl = target_lang or "he"
    translated_parts: List[str] = []

    try:
        for chunk in _chunk_text(text, max_chars=450):
            _check_cancelled(cancel_event)
            resp = execute_http_with_retry(
                "GET",
                "https://api.mymemory.translated.net/get",
                provider_name="MyMemory חינמי",
                stage="תרגום (MyMemory)",
                params={"q": chunk, "langpair": f"{sl}|{tl}"},
                connect_timeout=8.0,
                read_timeout=20.0,
                total_deadline=60.0,
                max_retries=2,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
            )
            data = resp.json()
            status = data.get("responseStatus", 200)
            if status not in (200, "200"):
                raise RuntimeError(data.get("responseDetails") or "שגיאה בשלב 'תרגום (MyMemory)'")
            out = (data.get("responseData", {}).get("translatedText") or "").strip()
            if not out:
                raise RuntimeError("שלב 'תרגום (MyMemory)': התקבל תרגום ריק עבור אחד המקטעים.")
            translated_parts.append(out)
    except AICancelledError as e:
        raise TranslationCancelledError(str(e)) from e

    return "\n\n".join(translated_parts)


def translate_libretranslate(
    text: str,
    source_lang: str = "auto",
    target_lang: str = "he",
    url: str = "https://libretranslate.com/translate",
    api_key: str = "",
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    sl = source_lang or "auto"
    tl = target_lang or "he"
    translated_parts: List[str] = []
    secrets = [api_key] if api_key else []

    try:
        for chunk in _chunk_text(text, max_chars=1500):
            _check_cancelled(cancel_event)
            payload: Dict[str, Any] = {
                "q": chunk,
                "source": sl,
                "target": tl,
                "format": "text",
            }
            if api_key:
                payload["api_key"] = api_key
            resp = execute_http_with_retry(
                "POST",
                url,
                provider_name="LibreTranslate",
                stage="תרגום (LibreTranslate)",
                json_body=payload,
                connect_timeout=8.0,
                read_timeout=25.0,
                total_deadline=75.0,
                max_retries=2,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                secrets=secrets,
            )
            data = resp.json()
            out = (data.get("translatedText") or "").strip()
            if not out:
                raise RuntimeError("שלב 'תרגום (LibreTranslate)': התקבל תרגום ריק עבור אחד המקטעים.")
            translated_parts.append(out)
    except AICancelledError as e:
        raise TranslationCancelledError(str(e)) from e

    return "\n\n".join(translated_parts)


def _call_chat(caller: Callable[..., str], **kwargs) -> str:
    """מפעיל caller תוך תאימות לאחור לפונקציות מוק עם חתימה קצרה."""
    try:
        return caller(**kwargs)
    except TypeError:
        compact_keys = (
            "provider_id",
            "api_key",
            "model",
            "system_prompt",
            "user_prompt",
            "base_url",
            "json_mode",
            "temperature",
        )
        compact = {k: v for k, v in kwargs.items() if k in compact_keys}
        return caller(**compact)


def translate_with_ai(
    text: str,
    source_lang: str = "auto",
    target_lang: str = "he",
    settings: Dict[str, Any] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
) -> Tuple[str, str]:
    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    model = pconf.get("model", "")
    src_name = LANG_NAMES_EN.get(source_lang, source_lang)
    tgt_name = LANG_NAMES_EN.get(target_lang, target_lang)
    caller = chat_fn or chat_completion

    system_prompt = (
        f"You are a faithful literary translator translating from {src_name} into natural, grammatically accurate {tgt_name}. "
        "Preserve meaning, names, numbers, terminology, punctuation, and paragraph structure. "
        "Do not summarize, omit, add commentary, or answer questions inside the text. Return ONLY the translated text."
    )

    translated_parts: List[str] = []
    try:
        for chunk in _chunk_text(text, max_chars=2400):
            _check_cancelled(cancel_event)
            out = _call_chat(
                caller,
                provider_id=pid,
                api_key=pconf.get("api_key", ""),
                model=model,
                system_prompt=system_prompt,
                user_prompt=chunk,
                base_url=pconf.get("base_url", ""),
                json_mode=False,
                temperature=0.1,
                connect_timeout=10.0,
                read_timeout=35.0,
                total_deadline=105.0,
                max_retries=2,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                stage="תרגום AI",
            )
            cleaned_out = (out or "").strip()
            if not cleaned_out:
                raise RuntimeError("שלב 'תרגום AI': מודל ה-AI החזיר תרגום ריק עבור אחד המקטעים.")
            translated_parts.append(cleaned_out)
    except AICancelledError as e:
        raise TranslationCancelledError(str(e)) from e

    return "\n\n".join(translated_parts), f"{get_provider_label(pid)} ({model})"


def _translate_sentence_batch_ai(
    indexed_sentences: List[Tuple[int, str]],
    source_lang: str,
    target_lang: str,
    settings: Dict[str, Any],
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
) -> Dict[int, str]:
    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    model = pconf.get("model", "")
    src_name = LANG_NAMES_EN.get(source_lang, source_lang)
    tgt_name = LANG_NAMES_EN.get(target_lang, target_lang)
    caller = chat_fn or chat_completion

    sys_prompt = (
        f"Translate EVERY numbered item below from {src_name} into {tgt_name}. "
        "Do not merge, omit, or skip any item. Return valid JSON ONLY in the exact format:\n"
        '{"translations": [{"index": 0, "text": "..."}]}'
    )
    items_payload = [{"index": i, "text": s} for i, s in indexed_sentences]
    raw = _call_chat(
        caller,
        provider_id=pid,
        api_key=pconf.get("api_key", ""),
        model=model,
        system_prompt=sys_prompt,
        user_prompt=json.dumps({"items": items_payload}, ensure_ascii=False),
        base_url=pconf.get("base_url", ""),
        json_mode=True,
        temperature=0.1,
        connect_timeout=10.0,
        read_timeout=35.0,
        total_deadline=105.0,
        max_retries=2,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        stage="תרגום AI",
    )
    parsed = extract_json_object(raw)
    mapping: Dict[int, str] = {}
    for entry in parsed.get("translations", []):
        if isinstance(entry, dict) and "index" in entry:
            idx = int(entry["index"])
            val = str(entry.get("text") or "").strip()
            if val:
                mapping[idx] = val
    return mapping


def translate_required_sentences(
    sentences: List[str],
    engine: str = "google_free",
    source_lang: str = "auto",
    target_lang: str = "he",
    settings: Dict[str, Any] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
    single_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
) -> Dict[str, Any]:
    """
    מתרגם את כל המשפטים הדרושים (0..N-1) באצוות מבוקרות ומאמת שכל משפט בודד קיבל תרגום לא ריק.
    אינו מאפשר תוצאה חלקית: כשל, timeout או ביטול מפילים את השלב מיד עם הודעה ברורה.
    """
    clean_sentences = [(s or "").strip() for s in (sentences or []) if (s or "").strip()]
    if not clean_sentences:
        raise ValueError("שלב התרגום: אין משפטים לתרגום.")

    cfg = settings or load_settings()
    trans_cfg = cfg.get("translation", {})
    pconf = get_active_provider_config(cfg)

    if engine == "ai":
        engine_label = f"{get_provider_label(pconf['provider_id'])} ({pconf.get('model', '')})"
    elif engine == "google_free":
        engine_label = "Google Translate חינמי"
    elif engine == "mymemory":
        engine_label = "MyMemory חינמי"
    elif engine == "libretranslate":
        engine_label = "LibreTranslate"
    else:
        raise ValueError(f"שלב התרגום: מנוע תרגום לא מוכר '{engine}'.")

    total = len(clean_sentences)
    translated_list: List[str] = [""] * total

    def _translate_one(sent: str) -> str:
        _check_cancelled(cancel_event)
        if single_translate_fn is not None:
            out = (single_translate_fn(sent, engine, source_lang, target_lang) or "").strip()
            if not out:
                raise RuntimeError("שלב התרגום: התקבל תרגום ריק ממשפט המקור.")
            return out
        if engine == "ai":
            out, _ = translate_with_ai(
                sent,
                source_lang=source_lang,
                target_lang=target_lang,
                settings=cfg,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                chat_fn=chat_fn,
            )
            return out.strip()
        if engine == "google_free":
            return translate_google_free(
                sent, source_lang, target_lang, cancel_event=cancel_event, progress_cb=progress_cb
            ).strip()
        if engine == "mymemory":
            return translate_mymemory_free(
                sent, source_lang, target_lang, cancel_event=cancel_event, progress_cb=progress_cb
            ).strip()
        return translate_libretranslate(
            sent,
            source_lang,
            target_lang,
            url=trans_cfg.get("libretranslate_url", "https://libretranslate.com/translate"),
            api_key=trans_cfg.get("libretranslate_api_key", ""),
            cancel_event=cancel_event,
            progress_cb=progress_cb,
        ).strip()

    if engine == "ai" and single_translate_fn is None and total > 1:
        batches = _build_sentence_batches(clean_sentences, max_items=12, max_chars=2400)
        for b_idx, batch in enumerate(batches, 1):
            _check_cancelled(cancel_event)
            start_num = batch[0][0] + 1
            end_num = batch[-1][0] + 1
            if progress_cb:
                progress_cb(f"מתרגם אצווה {b_idx}/{len(batches)} (משפטים {start_num}–{end_num} מתוך {total})...")
            try:
                batch_map = _translate_sentence_batch_ai(
                    batch,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    settings=cfg,
                    cancel_event=cancel_event,
                    progress_cb=progress_cb,
                    chat_fn=chat_fn,
                )
            except (TranslationCancelledError, AICancelledError) as e:
                raise TranslationCancelledError(str(e)) from e
            except AICallError:
                # כשלי רשת / timeout / הרשאה / מודל לא נבלעים אלא מדווחים בבירור עם שם השלב!
                raise
            except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                # פלט JSON פגום בלבד — משלימים משפט-משפט בלי לדלג
                batch_map = {}

            for i, orig_sent in batch:
                _check_cancelled(cancel_event)
                val = (batch_map.get(i) or "").strip()
                if not val:
                    val = _translate_one(orig_sent)
                if not val:
                    raise RuntimeError(f"שלב התרגום נכשל במשפט {i + 1} מתוך {total}: התקבל פלט ריק.")
                translated_list[i] = val
    else:
        for i, sent in enumerate(clean_sentences):
            _check_cancelled(cancel_event)
            if progress_cb and (i == 0 or (i + 1) % 5 == 0 or i + 1 == total):
                progress_cb(f"מתרגם משפט {i + 1} מתוך {total}...")
            out = _translate_one(sent)
            if not out:
                raise RuntimeError(f"שלב התרגום נכשל במשפט {i + 1} מתוך {total}: התקבל פלט ריק.")
            translated_list[i] = out

    if len(translated_list) != total or any(not (t or "").strip() for t in translated_list):
        raise RuntimeError("שלב התרגום: התוצאה חלקית ואינה מכסה את כל המשפטים הדרושים.")

    translated_text = "\n".join(translated_list)
    return {
        "translated_text": translated_text,
        "engine": engine,
        "engine_label": engine_label,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "source_sentences": clean_sentences,
        "translated_sentences": translated_list,
        "sentence_count_source": total,
        "sentence_count_translated": len(translated_list),
        "source_paragraphs": clean_sentences,
        "target_paragraphs": translated_list,
        "is_complete": True,
    }


def translate_text(
    text: str,
    engine: str = "google_free",
    source_lang: str = "auto",
    target_lang: str = "he",
    settings: Dict[str, Any] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
    single_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
) -> Dict[str, Any]:
    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("שלב התרגום: אין טקסט לתרגום בפרק הנוכחי.")

    sentences = split_into_sentences(clean_text)
    return translate_required_sentences(
        sentences=sentences,
        engine=engine,
        source_lang=source_lang,
        target_lang=target_lang,
        settings=settings,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        chat_fn=chat_fn,
        single_translate_fn=single_translate_fn,
    )
