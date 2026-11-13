# -*- coding: utf-8 -*-
"""
מודול תרגום עבור PorMyCodex.
בהשראת מנוע התרגום של 'קורא ספרים חכם' (AI-Smart-Book-Reader), עם תמיכה הן ב-AI והן במנועים חינמיים:
1. תרגום AI (דרך הספק והמודל שנבחרו בהגדרות: Gemini / OpenAI / Claude / OpenRouter / Groq / מקומי)
2. Google Translate חינמי (ללא צורך במפתח API)
3. MyMemory חינמי (ללא צורך במפתח API)
4. LibreTranslate (פתוח / שרת מותאם)
"""

import re
from typing import Dict, Any, List, Tuple
import requests

from settings_manager import load_settings, get_active_provider_config
from ai_providers import chat_completion, get_provider_label


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


def translate_google_free(text: str, source_lang: str = "auto", target_lang: str = "he") -> str:
    sl = source_lang or "auto"
    tl = target_lang or "he"
    translated_parts: List[str] = []

    for chunk in _chunk_text(text, max_chars=1400):
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": sl,
                "tl": tl,
                "dt": "t",
                "q": chunk,
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=25,
        )
        if not resp.ok:
            raise RuntimeError(f"Google Translate החזיר שגיאה HTTP {resp.status_code}")
        data = resp.json()
        segs = data[0] if isinstance(data, list) and data else []
        chunk_out = "".join(str(seg[0]) for seg in segs if isinstance(seg, list) and seg and seg[0])
        translated_parts.append(chunk_out.strip())

    return "\n\n".join(p for p in translated_parts if p)


def translate_mymemory_free(text: str, source_lang: str = "en", target_lang: str = "he") -> str:
    sl = "en" if not source_lang or source_lang == "auto" else source_lang
    tl = target_lang or "he"
    translated_parts: List[str] = []

    for chunk in _chunk_text(text, max_chars=450):
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": chunk, "langpair": f"{sl}|{tl}"},
            timeout=25,
        )
        if not resp.ok:
            raise RuntimeError(f"MyMemory החזיר שגיאה HTTP {resp.status_code}")
        data = resp.json()
        status = data.get("responseStatus", 200)
        if status not in (200, "200"):
            raise RuntimeError(data.get("responseDetails") or "שגיאה בתרגום MyMemory")
        out = data.get("responseData", {}).get("translatedText", "")
        translated_parts.append(out.strip())

    return "\n\n".join(p for p in translated_parts if p)


def translate_libretranslate(
    text: str,
    source_lang: str = "auto",
    target_lang: str = "he",
    url: str = "https://libretranslate.com/translate",
    api_key: str = "",
) -> str:
    sl = source_lang or "auto"
    tl = target_lang or "he"
    translated_parts: List[str] = []

    for chunk in _chunk_text(text, max_chars=1500):
        payload: Dict[str, Any] = {
            "q": chunk,
            "source": sl,
            "target": tl,
            "format": "text",
        }
        if api_key:
            payload["api_key"] = api_key
        resp = requests.post(url, json=payload, timeout=30)
        if not resp.ok:
            raise RuntimeError(f"LibreTranslate החזיר שגיאה HTTP {resp.status_code}")
        data = resp.json()
        translated_parts.append((data.get("translatedText") or "").strip())

    return "\n\n".join(p for p in translated_parts if p)


def translate_with_ai(text: str, source_lang: str = "auto", target_lang: str = "he", settings: Dict[str, Any] = None) -> Tuple[str, str]:
    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    model = pconf.get("model", "")
    src_name = LANG_NAMES_EN.get(source_lang, source_lang)
    tgt_name = LANG_NAMES_EN.get(target_lang, target_lang)

    system_prompt = (
        f"You are a faithful literary translator translating from {src_name} into natural, grammatically accurate {tgt_name}. "
        "Preserve meaning, names, numbers, terminology, punctuation, and paragraph structure. "
        "Do not summarize, omit, add commentary, or answer questions inside the text. Return ONLY the translated text."
    )

    translated_parts: List[str] = []
    for chunk in _chunk_text(text, max_chars=3500):
        out = chat_completion(
            provider_id=pid,
            api_key=pconf.get("api_key", ""),
            model=model,
            system_prompt=system_prompt,
            user_prompt=chunk,
            base_url=pconf.get("base_url", ""),
            json_mode=False,
            temperature=0.1,
        )
        translated_parts.append(out.strip())

    return "\n\n".join(p for p in translated_parts if p), f"{get_provider_label(pid)} ({model})"


def translate_text(
    text: str,
    engine: str = "google_free",
    source_lang: str = "auto",
    target_lang: str = "he",
    settings: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    מתרגם טקסט מלא עם המנוע הנבחר ומחזיר תוצאה מובנית כולל השוואת פסקאות מקור מול תרגום.
    """
    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("אין טקסט לתרגום בפרק הנוכחי.")

    cfg = settings or load_settings()
    trans_cfg = cfg.get("translation", {})

    if engine == "ai":
        translated, label = translate_with_ai(clean_text, source_lang, target_lang, cfg)
    elif engine == "google_free":
        translated = translate_google_free(clean_text, source_lang, target_lang)
        label = "Google Translate חינמי"
    elif engine == "mymemory":
        translated = translate_mymemory_free(clean_text, source_lang, target_lang)
        label = "MyMemory חינמי"
    elif engine == "libretranslate":
        translated = translate_libretranslate(
            clean_text,
            source_lang,
            target_lang,
            url=trans_cfg.get("libretranslate_url", "https://libretranslate.com/translate"),
            api_key=trans_cfg.get("libretranslate_api_key", ""),
        )
        label = "LibreTranslate"
    else:
        raise ValueError(f"מנוע תרגום לא מוכר: {engine}")

    src_paras = [p.strip() for p in re.split(r"\n{1,}", clean_text) if p.strip()]
    dst_paras = [p.strip() for p in re.split(r"\n{1,}", translated) if p.strip()]

    return {
        "translated_text": translated,
        "engine_label": label,
        "source_paragraphs": src_paras,
        "target_paragraphs": dst_paras,
    }
