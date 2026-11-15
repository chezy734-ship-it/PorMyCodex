# -*- coding: utf-8 -*-
"""
מודול תרגום עבור PorMyCodex.
כולל:
- אימות שלמות מלא לכל משפט נדרש (Zero Partial Success)
- הפרדת Connect/Read Timeout, Deadline כולל, וניסיונות חוזרים מוגבלים עם Backoff לכשלים זמניים בלבד
- זיהוי דפי חסימה / HTML / NetFree (כולל HTTP 418) ללא שמירה או הצגה של HTML גולמי
- מפסק זרם (Circuit Breaker) לפי ספק למניעת לולאת בקשות מול ספק חסום
- מדיניות מעבר לספק חלופי (Fallback) מפורשת באישור המשתמש בלבד (לעולם לא בחירה שרירותית)
- תמיכה מלאה בביטול מיידי גם בזמן המתנה/Retry/Fallback
"""

import json
import re
from typing import Dict, Any, List, Tuple, Optional, Callable

from settings_manager import load_settings, get_active_provider_config
from ai_providers import (
    PROVIDER_CATALOG,
    AICallError,
    AICancelledError,
    ProviderCircuitBreaker,
    _sanitize_error_text,
    chat_completion,
    detect_blocked_response,
    execute_http_with_retry,
    extract_json_object,
    get_provider_label,
)
from book_loader import split_into_sentences


class TranslationCancelledError(RuntimeError):
    """נזרק כאשר פעולת התרגום בוטלה במפורש."""


class TranslationProviderError(RuntimeError):
    """שגיאת ספק תרגום מובחנת עם פרטי חסימה/קטגוריה ואפשרויות התאוששות (ללא HTML גולמי)."""

    def __init__(
        self,
        message: str,
        *,
        stage: str = "תרגום",
        category: str = "error",
        provider_id: str = "",
        provider_label: str = "",
        status_code: Optional[int] = None,
        blocked_filter: str = "",
        is_blocked: bool = False,
        retryable: bool = False,
        chapter_index: Optional[int] = None,
        unit_index: Optional[int] = None,
        unit_id: str = "",
        failed_providers: Optional[List[str]] = None,
    ):
        super().__init__(message)
        self.stage = stage
        self.category = category
        self.provider_id = provider_id
        self.provider_label = provider_label or provider_id
        self.status_code = status_code
        self.blocked_filter = blocked_filter
        self.is_blocked = bool(is_blocked or category == "blocked" or status_code in (418, 451))
        self.retryable = retryable
        self.chapter_index = chapter_index
        self.unit_index = unit_index
        self.unit_id = unit_id
        self.failed_providers = list(failed_providers or [])


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


def get_translation_engine_label(engine: str, settings: Optional[Dict[str, Any]] = None) -> str:
    cfg = settings or {}
    if engine == "ai":
        pconf = get_active_provider_config(cfg)
        return f"{get_provider_label(pconf['provider_id'])} ({pconf.get('model', '') or 'לא נבחר'})"
    if engine == "google_free":
        return "Google Translate חינמי"
    if engine == "mymemory":
        return "MyMemory חינמי"
    if engine == "libretranslate":
        return "LibreTranslate"
    return engine


def is_translation_engine_configured(engine: str, settings: Optional[Dict[str, Any]] = None) -> bool:
    cfg = settings or {}
    if engine in ("google_free", "mymemory"):
        return True
    if engine == "libretranslate":
        url = (cfg.get("translation", {}).get("libretranslate_url") or "").strip()
        return bool(url)
    if engine == "ai":
        pconf = get_active_provider_config(cfg)
        pid = pconf.get("provider_id", "gemini")
        meta = PROVIDER_CATALOG.get(pid, {})
        model = (pconf.get("model") or "").strip()
        key = (pconf.get("api_key") or "").strip()
        if not model:
            return False
        if meta.get("requires_key", True) and not key:
            return False
        return True
    return False


def resolve_fallback_candidates(
    primary_engine: str,
    settings: Optional[Dict[str, Any]] = None,
    fallback_policy: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    מחזיר רשימת מנועי התאוששות חלופיים רק לפי מדיניות מפורשת שהמשתמש אישר והגדיר.
    לעולם אינו ממציא ספק או מודל ואינו עובר ל-AI ללא אישור מפורש (`fallback_to_ai_on_failure=True`).
    """
    cfg = settings or {}
    t_cfg = dict(cfg.get("translation", {}))
    if fallback_policy:
        t_cfg.update({k: v for k, v in fallback_policy.items() if v is not None})

    candidates: List[str] = []
    sec = (t_cfg.get("fallback_secondary_engine") or "").strip()
    if sec and sec != primary_engine and sec in ("google_free", "mymemory", "libretranslate"):
        if is_translation_engine_configured(sec, cfg):
            candidates.append(sec)

    allow_ai = bool(t_cfg.get("fallback_to_ai_on_failure", False))
    if allow_ai and primary_engine != "ai" and is_translation_engine_configured("ai", cfg):
        candidates.append("ai")

    return list(dict.fromkeys(candidates))


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


def _safe_parse_json_response(
    resp: Any,
    *,
    provider_name: str,
    provider_id: str,
    stage: str,
    secrets: Optional[List[str]] = None,
) -> Any:
    blk = detect_blocked_response(resp, secrets=secrets)
    if blk is not None:
        code = int(blk.get("status_code") or 0)
        f_name = blk.get("filter_name") or ""
        f_note = f" — זוהה: {f_name}" if f_name else ""
        code_txt = f"HTTP {code}" if code else "תגובת HTML/חסימה"
        raise AICallError(
            f"השירות '{provider_name}' חסום בשלב '{stage}' ({code_txt}{f_note}): "
            "התקבל דף חסימה במקום תשובת JSON תקינה.",
            stage=stage,
            category="blocked",
            retryable=False,
            status_code=code or None,
            provider_id=provider_id,
            blocked_filter=f_name,
        )

    try:
        return resp.json()
    except Exception as exc:
        raw_sample = str(getattr(resp, "text", "") or "")[:500].lower()
        if any(tok in raw_sample for tok in ("<html", "<!doctype", "netfree", "נטפרי", "סינון")):
            raise AICallError(
                f"השירות '{provider_name}' החזיר דף חסימה/HTML במקום תשובת תרגום תקינה בשלב '{stage}'.",
                stage=stage,
                category="blocked",
                retryable=False,
                status_code=int(getattr(resp, "status_code", 0) or 0) or None,
                provider_id=provider_id,
                blocked_filter="דף HTML במקום JSON",
            ) from exc
        raise AICallError(
            f"שלב '{stage}': הספק {provider_name} החזיר תשובה שאינה JSON תקין ({type(exc).__name__}).",
            stage=stage,
            category="invalid_response",
            retryable=False,
            provider_id=provider_id,
        ) from exc


def translate_google_free(
    text: str,
    source_lang: str = "auto",
    target_lang: str = "he",
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    request_fn: Optional[Callable[..., Any]] = None,
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
                request_fn=request_fn,
                expect_json=True,
            )
            data = _safe_parse_json_response(
                resp,
                provider_name="Google Translate חינמי",
                provider_id="google_free",
                stage="תרגום (Google Translate)",
            )
            segs = data[0] if isinstance(data, list) and data else []
            chunk_out = "".join(str(seg[0]) for seg in segs if isinstance(seg, list) and seg and seg[0]).strip()
            if not chunk_out:
                raise AICallError(
                    "שלב 'תרגום (Google Translate)': התקבל תרגום ריק עבור אחד המקטעים.",
                    stage="תרגום (Google Translate)",
                    category="invalid_response",
                    retryable=False,
                    provider_id="google_free",
                )
            low_out = chunk_out.lower()
            if "<html" in low_out or "<!doctype" in low_out or "netfree.link" in low_out:
                raise AICallError(
                    "השירות 'Google Translate חינמי' חסום (זוהה דף HTML/NetFree במקום תרגום).",
                    stage="תרגום (Google Translate)",
                    category="blocked",
                    retryable=False,
                    provider_id="google_free",
                    blocked_filter="דף HTML במקום תרגום",
                )
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
    request_fn: Optional[Callable[..., Any]] = None,
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
                request_fn=request_fn,
                expect_json=True,
            )
            data = _safe_parse_json_response(
                resp,
                provider_name="MyMemory חינמי",
                provider_id="mymemory",
                stage="תרגום (MyMemory)",
            )
            status = data.get("responseStatus", 200)
            if status not in (200, "200"):
                raise AICallError(
                    _sanitize_error_text(str(data.get("responseDetails") or "שגיאה בשלב 'תרגום (MyMemory)'")),
                    stage="תרגום (MyMemory)",
                    category="quota" if str(status) == "429" else "error",
                    retryable=str(status) == "429",
                    provider_id="mymemory",
                )
            out = (data.get("responseData", {}).get("translatedText") or "").strip()
            if not out:
                raise AICallError(
                    "שלב 'תרגום (MyMemory)': התקבל תרגום ריק עבור אחד המקטעים.",
                    stage="תרגום (MyMemory)",
                    category="invalid_response",
                    retryable=False,
                    provider_id="mymemory",
                )
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
    request_fn: Optional[Callable[..., Any]] = None,
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
                request_fn=request_fn,
                expect_json=True,
            )
            data = _safe_parse_json_response(
                resp,
                provider_name="LibreTranslate",
                provider_id="libretranslate",
                stage="תרגום (LibreTranslate)",
                secrets=secrets,
            )
            out = (data.get("translatedText") or "").strip()
            if not out:
                raise AICallError(
                    "שלב 'תרגום (LibreTranslate)': התקבל תרגום ריק עבור אחד המקטעים.",
                    stage="תרגום (LibreTranslate)",
                    category="invalid_response",
                    retryable=False,
                    provider_id="libretranslate",
                )
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
    request_fn: Optional[Callable[..., Any]] = None,
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
                request_fn=request_fn,
            )
            cleaned_out = (out or "").strip()
            if not cleaned_out:
                raise AICallError(
                    "שלב 'תרגום AI': מודל ה-AI החזיר תרגום ריק עבור אחד המקטעים.",
                    stage="תרגום AI",
                    category="invalid_response",
                    retryable=False,
                    provider_id=f"ai:{pid}",
                )
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
    request_fn: Optional[Callable[..., Any]] = None,
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
        request_fn=request_fn,
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


def _extract_error_meta(err: Exception, engine_id: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    raw_msg = str(err or "")
    secrets: List[str] = []
    for p in (settings.get("providers") or {}).values():
        k = (p.get("api_key") or "").strip()
        if k:
            secrets.append(k)
    lk = (settings.get("translation", {}).get("libretranslate_api_key") or "").strip()
    if lk:
        secrets.append(lk)

    safe_msg = _sanitize_error_text(raw_msg, secrets=secrets)
    low = safe_msg.lower()
    if "<html" in low or "<!doctype" in low:
        safe_msg = (
            f"השירות '{get_translation_engine_label(engine_id, settings)}' חסום "
            "(התקבל דף חסימה/HTML במקום תשובת תרגום תקינה)."
        )

    cat = str(getattr(err, "category", "") or "").lower()
    code = getattr(err, "status_code", None)
    b_filter = str(getattr(err, "blocked_filter", "") or "")
    is_blocked = bool(
        getattr(err, "is_blocked", False)
        or cat == "blocked"
        or code in (418, 451)
        or "netfree" in low
        or "418" in low
        or bool(b_filter)
    )
    if is_blocked and not b_filter and "netfree" in low:
        b_filter = "סינון NetFree (netfree.link)"
    if is_blocked and not cat:
        cat = "blocked"

    return {
        "category": cat or "error",
        "status_code": code,
        "blocked_filter": b_filter,
        "is_blocked": is_blocked,
        "message": safe_msg,
    }


def _run_single_engine_translation(
    *,
    clean_sentences: List[str],
    cand_engine: str,
    primary_engine: str,
    source_lang: str,
    target_lang: str,
    settings: Dict[str, Any],
    cancel_event: Any,
    progress_cb: Optional[Callable[[str], None]],
    chat_fn: Optional[Callable[..., str]],
    single_translate_fn: Optional[Callable[[str, str, str, str], str]],
    fallback_translate_fn: Optional[Callable[[str, str, str, str], str]],
    request_fn: Optional[Callable[..., Any]],
    completed_units_map: Optional[Dict[int, Dict[str, Any]]] = None,
    unit_progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    trans_cfg = settings.get("translation", {})
    pconf = get_active_provider_config(settings)
    total = len(clean_sentences)
    cand_label = get_translation_engine_label(cand_engine, settings)
    ai_prov = pconf.get("provider_id", "") if cand_engine == "ai" else ""
    ai_mod = pconf.get("model", "") if cand_engine == "ai" else ""
    is_fb = cand_engine != primary_engine

    translated_list: List[str] = [""] * total
    provenance_map: Dict[str, Dict[str, Any]] = {}
    pending_indices: List[int] = []
    units_store = completed_units_map if completed_units_map is not None else {}

    for i in range(total):
        prev = units_store.get(i)
        if prev and (prev.get("translated_text") or "").strip():
            translated_list[i] = str(prev["translated_text"]).strip()
            provenance_map[str(i)] = {
                "engine": prev.get("engine") or primary_engine,
                "engine_label": prev.get("engine_label") or get_translation_engine_label(prev.get("engine") or primary_engine, settings),
                "ai_provider": prev.get("ai_provider", ""),
                "ai_model": prev.get("ai_model", ""),
                "fallback_used": bool(prev.get("fallback_used", False)),
            }
        else:
            pending_indices.append(i)

    if not pending_indices:
        return translated_list, provenance_map

    def _emit_unit(ev: Dict[str, Any]) -> None:
        if unit_progress_cb is not None:
            unit_progress_cb(ev)

    def _record_unit_ok(i: int, text_val: str) -> None:
        clean_val = text_val.strip()
        translated_list[i] = clean_val
        prov = {
            "engine": cand_engine,
            "engine_label": cand_label,
            "ai_provider": ai_prov,
            "ai_model": ai_mod,
            "fallback_used": is_fb,
        }
        provenance_map[str(i)] = prov
        units_store[i] = {
            "unit_id": f"u{i + 1}",
            "unit_index": i,
            "translated_text": clean_val,
            **prov,
        }
        _emit_unit({
            "event": "unit_done",
            "unit_index": i,
            "unit_id": f"u{i + 1}",
            "translated_text": clean_val,
            **prov,
        })

    def _use_fn() -> Optional[Callable[[str, str, str, str], str]]:
        if cand_engine != primary_engine and fallback_translate_fn is not None:
            return fallback_translate_fn
        if cand_engine == "ai":
            if cand_engine != primary_engine and chat_fn is not None and single_translate_fn is None:
                return None
            if single_translate_fn is not None:
                return single_translate_fn
            return None
        return single_translate_fn

    direct_fn = _use_fn()

    def _translate_one(sent: str) -> str:
        _check_cancelled(cancel_event)
        if direct_fn is not None:
            out = (direct_fn(sent, cand_engine, source_lang, target_lang) or "").strip()
            if not out:
                raise AICallError(
                    "שלב התרגום: התקבל תרגום ריק ממשפט המקור.",
                    stage="תרגום",
                    category="invalid_response",
                    provider_id=cand_engine,
                )
            low_out = out.lower()
            if "<html" in low_out or "<!doctype" in low_out or "netfree.link" in low_out:
                raise AICallError(
                    f"השירות '{get_translation_engine_label(cand_engine, settings)}' החזיר דף חסימה/HTML במקום תרגום.",
                    stage="תרגום",
                    category="blocked",
                    status_code=418,
                    provider_id=cand_engine,
                    blocked_filter="סינון NetFree / HTML" if "netfree" in low_out else "דף HTML במקום תרגום",
                )
            return out
        if cand_engine == "ai":
            out, _ = translate_with_ai(
                sent,
                source_lang=source_lang,
                target_lang=target_lang,
                settings=settings,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                chat_fn=chat_fn,
                request_fn=request_fn,
            )
            return out.strip()
        if cand_engine == "google_free":
            return translate_google_free(
                sent,
                source_lang,
                target_lang,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                request_fn=request_fn,
            ).strip()
        if cand_engine == "mymemory":
            return translate_mymemory_free(
                sent,
                source_lang,
                target_lang,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                request_fn=request_fn,
            ).strip()
        return translate_libretranslate(
            sent,
            source_lang,
            target_lang,
            url=trans_cfg.get("libretranslate_url", "https://libretranslate.com/translate"),
            api_key=trans_cfg.get("libretranslate_api_key", ""),
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            request_fn=request_fn,
        ).strip()

    if cand_engine == "ai" and direct_fn is None and len(pending_indices) > 1:
        pending_pairs = [(i, clean_sentences[i]) for i in pending_indices]
        batches: List[List[Tuple[int, str]]] = []
        cur_b: List[Tuple[int, str]] = []
        cur_ch = 0
        for idx_u, sent_u in pending_pairs:
            slen = len(sent_u)
            if cur_b and (len(cur_b) >= 12 or cur_ch + slen > 2400):
                batches.append(cur_b)
                cur_b = [(idx_u, sent_u)]
                cur_ch = slen
            else:
                cur_b.append((idx_u, sent_u))
                cur_ch += slen
        if cur_b:
            batches.append(cur_b)

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
                    settings=settings,
                    cancel_event=cancel_event,
                    progress_cb=progress_cb,
                    chat_fn=chat_fn,
                    request_fn=request_fn,
                )
            except (TranslationCancelledError, AICancelledError) as e:
                first_idx = batch[0][0]
                _emit_unit({
                    "event": "unit_cancelled",
                    "unit_index": first_idx,
                    "unit_id": f"u{first_idx + 1}",
                    "engine": cand_engine,
                })
                raise TranslationCancelledError(str(e)) from e
            except AICallError as exc:
                first_idx = batch[0][0]
                setattr(exc, "unit_index", first_idx)
                setattr(exc, "unit_id", f"u{first_idx + 1}")
                _emit_unit({
                    "event": "unit_failed",
                    "unit_index": first_idx,
                    "unit_id": f"u{first_idx + 1}",
                    "engine": cand_engine,
                    "error": _sanitize_error_text(str(exc)),
                })
                raise
            except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                batch_map = {}

            for i, orig_sent in batch:
                try:
                    _check_cancelled(cancel_event)
                except TranslationCancelledError:
                    _emit_unit({"event": "unit_cancelled", "unit_index": i, "unit_id": f"u{i + 1}", "engine": cand_engine})
                    raise
                val = (batch_map.get(i) or "").strip()
                if not val:
                    try:
                        val = _translate_one(orig_sent)
                    except (TranslationCancelledError, AICancelledError) as ce:
                        _emit_unit({"event": "unit_cancelled", "unit_index": i, "unit_id": f"u{i + 1}", "engine": cand_engine})
                        raise TranslationCancelledError(str(ce)) from ce
                    except Exception as exc:
                        setattr(exc, "unit_index", i)
                        setattr(exc, "unit_id", f"u{i + 1}")
                        _emit_unit({
                            "event": "unit_failed",
                            "unit_index": i,
                            "unit_id": f"u{i + 1}",
                            "engine": cand_engine,
                            "error": _sanitize_error_text(str(exc)),
                        })
                        raise
                if not val:
                    err = AICallError(
                        f"שלב התרגום נכשל במשפט {i + 1} מתוך {total}: התקבל פלט ריק.",
                        stage="תרגום AI",
                        category="invalid_response",
                        provider_id="ai",
                    )
                    setattr(err, "unit_index", i)
                    setattr(err, "unit_id", f"u{i + 1}")
                    _emit_unit({
                        "event": "unit_failed",
                        "unit_index": i,
                        "unit_id": f"u{i + 1}",
                        "engine": cand_engine,
                        "error": str(err),
                    })
                    raise err
                _record_unit_ok(i, val)
    else:
        for i in pending_indices:
            sent = clean_sentences[i]
            try:
                _check_cancelled(cancel_event)
            except TranslationCancelledError:
                _emit_unit({"event": "unit_cancelled", "unit_index": i, "unit_id": f"u{i + 1}", "engine": cand_engine})
                raise
            if progress_cb and (i == pending_indices[0] or (i + 1) % 5 == 0 or i + 1 == total):
                progress_cb(f"מתרגם משפט {i + 1} מתוך {total}...")
            try:
                out = _translate_one(sent)
            except (TranslationCancelledError, AICancelledError) as ce:
                _emit_unit({"event": "unit_cancelled", "unit_index": i, "unit_id": f"u{i + 1}", "engine": cand_engine})
                raise TranslationCancelledError(str(ce)) from ce
            except Exception as exc:
                setattr(exc, "unit_index", i)
                setattr(exc, "unit_id", f"u{i + 1}")
                _emit_unit({
                    "event": "unit_failed",
                    "unit_index": i,
                    "unit_id": f"u{i + 1}",
                    "engine": cand_engine,
                    "error": _sanitize_error_text(str(exc)),
                })
                raise
            if not out:
                err = AICallError(
                    f"שלב התרגום נכשל במשפט {i + 1} מתוך {total}: התקבל פלט ריק.",
                    stage="תרגום",
                    category="invalid_response",
                    provider_id=cand_engine,
                )
                setattr(err, "unit_index", i)
                setattr(err, "unit_id", f"u{i + 1}")
                _emit_unit({
                    "event": "unit_failed",
                    "unit_index": i,
                    "unit_id": f"u{i + 1}",
                    "engine": cand_engine,
                    "error": str(err),
                })
                raise err
            _record_unit_ok(i, out)

    if len(translated_list) != total or any(not (t or "").strip() for t in translated_list):
        raise AICallError(
            "שלב התרגום: התוצאה חלקית ואינה מכסה את כל המשפטים הדרושים.",
            stage="תרגום",
            category="invalid_response",
            provider_id=cand_engine,
        )

    return translated_list, provenance_map


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
    fallback_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
    circuit_breaker: Optional[ProviderCircuitBreaker] = None,
    fallback_policy: Optional[Dict[str, Any]] = None,
    request_fn: Optional[Callable[..., Any]] = None,
    chapter_index: Optional[int] = None,
    existing_units: Optional[Dict[int, Dict[str, Any]]] = None,
    unit_progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    מתרגם את כל המשפטים הדרושים (0..N-1) באצוות מבוקרות ומאמת שכל משפט בודד קיבל תרגום לא ריק.
    - מדלג על יחידות שכבר הושלמו ותקפות (`existing_units`) ללא קריאות ספק כפולות
    - משמר אטומית דרך `unit_progress_cb` כל משפט שהושלם מיד עם סיומו
    - תומך ב-Circuit Breaker לפי ספק כדי לא לחזור על בקשות מול שירות חסום (418 / NetFree)
    - עובר לספק חלופי אך ורק אם מדיניות המשתמש אישרה והגדירה זאת במפורש (וממשיך מהיחידה החסרה הבאה!)
    """
    clean_sentences = [(s or "").strip() for s in (sentences or []) if (s or "").strip()]
    if not clean_sentences:
        raise ValueError("שלב התרגום: אין משפטים לתרגום.")

    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    if engine not in ("ai", "google_free", "mymemory", "libretranslate"):
        raise ValueError(f"שלב התרגום: מנוע תרגום לא מוכר '{engine}'.")

    breaker = circuit_breaker or ProviderCircuitBreaker()
    fallbacks = resolve_fallback_candidates(engine, cfg, fallback_policy=fallback_policy)
    candidates = [engine] + fallbacks

    total = len(clean_sentences)
    failed_info: List[Dict[str, Any]] = []
    shared_completed_map: Dict[int, Dict[str, Any]] = dict(existing_units or {})
    last_unit_index: Optional[int] = None
    last_unit_id = ""

    for idx_cand, cand in enumerate(candidates):
        _check_cancelled(cancel_event)
        cand_label = get_translation_engine_label(cand, cfg)

        if breaker.is_open(cand):
            st = breaker.get_state(cand) or {}
            first_missing = next((i for i in range(total) if i not in shared_completed_map), 0)
            last_unit_index = first_missing
            last_unit_id = f"u{first_missing + 1}"
            failed_info.append({
                "engine": cand,
                "label": cand_label,
                "category": st.get("category") or "blocked",
                "status_code": st.get("status_code"),
                "blocked_filter": st.get("blocked_filter", ""),
                "is_blocked": True,
                "unit_index": first_missing,
                "unit_id": last_unit_id,
                "message": st.get("reason") or f"הספק '{cand_label}' פתוח במפסק הזרם עקב חסימה קודמת.",
            })
            continue

        if idx_cand > 0 and progress_cb:
            ch_txt = f" (פרק {chapter_index + 1})" if chapter_index is not None else ""
            progress_cb(
                f"הספק הראשי חסום/נכשל{ch_txt} — עובר לפי המדיניות המורשית אל: {cand_label} (משפטים שטרם הושלמו)..."
            )

        try:
            translated_list, unit_prov = _run_single_engine_translation(
                clean_sentences=clean_sentences,
                cand_engine=cand,
                primary_engine=engine,
                source_lang=source_lang,
                target_lang=target_lang,
                settings=cfg,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                chat_fn=chat_fn,
                single_translate_fn=single_translate_fn,
                fallback_translate_fn=fallback_translate_fn,
                request_fn=request_fn,
                completed_units_map=shared_completed_map,
                unit_progress_cb=unit_progress_cb,
            )
            breaker.record_success(cand)
            translated_text = "\n".join(translated_list)
            any_fallback = any(bool(v.get("fallback_used")) for v in unit_prov.values()) or (cand != engine)
            return {
                "translated_text": translated_text,
                "engine": cand,
                "engine_label": cand_label,
                "primary_engine": engine,
                "fallback_used": any_fallback,
                "ai_provider": pconf.get("provider_id", "") if cand == "ai" else "",
                "ai_model": pconf.get("model", "") if cand == "ai" else "",
                "unit_provenance": unit_prov,
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
        except (TranslationCancelledError, AICancelledError) as e:
            raise TranslationCancelledError(str(e)) from e
        except Exception as exc:
            _check_cancelled(cancel_event)
            meta = _extract_error_meta(exc, cand, cfg)
            u_idx = getattr(exc, "unit_index", None)
            u_id = getattr(exc, "unit_id", "") or (f"u{u_idx + 1}" if u_idx is not None else "")
            if u_idx is not None:
                last_unit_index = u_idx
                last_unit_id = u_id
            breaker.record_failure(cand, exc)
            failed_info.append({
                "engine": cand,
                "label": cand_label,
                "unit_index": u_idx,
                "unit_id": u_id,
                **meta,
            })

    # כל הספקים המורשים נכשלו או נחסמו
    primary_fail = failed_info[0] if failed_info else {
        "engine": engine,
        "label": get_translation_engine_label(engine, cfg),
        "category": "error",
        "status_code": None,
        "blocked_filter": "",
        "is_blocked": False,
        "unit_index": last_unit_index,
        "unit_id": last_unit_id,
        "message": "שגיאה לא ידועה בתרגום.",
    }
    fail_uid = primary_fail.get("unit_index")
    ch_prefix = f"בפרק {chapter_index + 1} " if chapter_index is not None else ""
    unit_prefix = f"יחידה {fail_uid + 1}/{total} " if fail_uid is not None else ""
    if primary_fail.get("is_blocked"):
        f_txt = f" ({primary_fail['blocked_filter']})" if primary_fail.get("blocked_filter") else ""
        code_txt = f"HTTP {primary_fail['status_code']}" if primary_fail.get("status_code") else "חסימת HTML/תוכן"
        summary_lines = [
            f"הספק '{primary_fail['label']}' חסום {ch_prefix}{unit_prefix}בשלב translation ({code_txt}{f_txt}).",
        ]
    else:
        summary_lines = [
            f"כשל {ch_prefix}{unit_prefix}בשלב translation מול '{primary_fail['label']}': {primary_fail['message']}",
        ]

    if len(failed_info) > 1:
        fb_notes = ", ".join(f"{fi['label']}: {fi['message']}" for fi in failed_info[1:])
        summary_lines.append(f"מעבר Fallback מורשה נכשל אף הוא ({fb_notes}).")
    else:
        policy_cfg = dict(cfg.get("translation", {}))
        if fallback_policy:
            policy_cfg.update({k: v for k, v in fallback_policy.items() if v is not None})
        if not bool(policy_cfg.get("fallback_to_ai_on_failure", False)):
            summary_lines.append(
                "מדיניות מעבר אוטומטי לספק ה-AI כבויה (כדי למנוע צריכת מכסה/תשלום ללא אישור)."
            )
        elif not is_translation_engine_configured("ai", cfg):
            summary_lines.append(
                "מדיניות AI Fallback מסומנת, אך טרם הוגדרו מפתח API ומודל פעיל לספק ה-AI."
            )

    summary_lines.append(
        "אפשרויות התאוששות: 'נסה שוב', 'החלף ספק והמשך', או 'המשך מהשמירה' (כל היחידות והפרקים שהושלמו נשמרו)."
    )

    raise TranslationProviderError(
        "\n".join(summary_lines),
        stage="translation",
        category="blocked" if primary_fail.get("is_blocked") else primary_fail.get("category", "error"),
        provider_id=primary_fail.get("engine", engine),
        provider_label=primary_fail.get("label", engine),
        status_code=primary_fail.get("status_code"),
        blocked_filter=primary_fail.get("blocked_filter", ""),
        is_blocked=bool(primary_fail.get("is_blocked")),
        retryable=bool(not primary_fail.get("is_blocked")),
        chapter_index=chapter_index,
        unit_index=fail_uid,
        unit_id=primary_fail.get("unit_id", last_unit_id),
        failed_providers=[fi["engine"] for fi in failed_info],
    )


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
    fallback_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
    circuit_breaker: Optional[ProviderCircuitBreaker] = None,
    fallback_policy: Optional[Dict[str, Any]] = None,
    request_fn: Optional[Callable[..., Any]] = None,
    chapter_index: Optional[int] = None,
    existing_units: Optional[Dict[int, Dict[str, Any]]] = None,
    unit_progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
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
        fallback_translate_fn=fallback_translate_fn,
        circuit_breaker=circuit_breaker,
        fallback_policy=fallback_policy,
        request_fn=request_fn,
        chapter_index=chapter_index,
        existing_units=existing_units,
        unit_progress_cb=unit_progress_cb,
    )
