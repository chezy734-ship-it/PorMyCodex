# -*- coding: utf-8 -*-
"""
מודול בלשנות ובינה מלאכותית (AI Linguist) לסיוע בהקראה,
ניקוד מדויק, פתיחת ראשי תיבות, ביאור מילים וסיכום פרקים בעברית.
- מחלק קלט גדול לאצוות בטוחות במקום חיתוך שקט (text[:7000])
- תומך בביטול מיידי ודיווח התקדמות
- מציג שגיאת שלב מובחנת ואינו מסמן פלט חלקי/כשל כהצלחה
"""

import re
from typing import Any, Callable, Dict, List, Optional

from settings_manager import load_settings, get_active_provider_config
from ai_providers import (
    AICallError,
    AICancelledError,
    chat_completion,
    extract_json_object,
    get_provider_label,
)


def _chunk_text_for_linguist(text: str, max_chars: int = 2400) -> List[str]:
    clean = (text or "").strip()
    if not clean:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", clean) if p.strip()]
    chunks: List[str] = []
    cur = ""
    for para in paragraphs:
        if len(para) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            sents = re.split(r"(?<=[.?!;:\n])\s+", para)
            buf = ""
            for s in sents:
                if len(buf) + len(s) + 1 <= max_chars:
                    buf = f"{buf} {s}".strip()
                else:
                    if buf:
                        chunks.append(buf)
                    buf = s[:max_chars]
            if buf:
                chunks.append(buf)
        elif len(cur) + len(para) + 2 <= max_chars:
            cur = f"{cur}\n\n{para}".strip()
        else:
            if cur:
                chunks.append(cur)
            cur = para
    if cur:
        chunks.append(cur)
    return chunks


def vocalize_and_expand_hebrew(
    text: str,
    api_key: str = "",
    settings: Dict[str, Any] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
) -> Dict[str, Any]:
    """
    מנקד טקסט עברי במדויק ופותח ראשי תיבות באצוות מבוקרות.
    אם לא הוגדר מפתח API עבור ספק שדורש מפתח - מחזיר במפורש חלוקה מקומית (offline-fallback).
    בשגיאת רשת/Timeout/מכסה של ספק AI - זורק שגיאה מפורשת עם שם השלב ולא מחזיר פלט חלקי כהצלחה.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise AICancelledError(stage="ניקוד ופתיחת קיצורים (AI)")

    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    key = (api_key or pconf.get("api_key", "")).strip()
    model = pconf.get("model", "")
    base_url = pconf.get("base_url", "")
    caller = chat_fn or chat_completion

    if chat_fn is None and not key and pid != "custom":
        from book_loader import split_into_sentences
        sents = split_into_sentences(text)
        return {
            "vocalized_text": text,
            "sentences": sents,
            "glossary": [],
            "source": "offline-fallback",
            "note": f"לא הוגדר מפתח API עבור {get_provider_label(pid)} — בוצעה חלוקה מקומית ללא ניקוד AI.",
        }

    chunks = _chunk_text_for_linguist(text, max_chars=2400)
    if not chunks:
        raise ValueError("שלב 'ניקוד ופתיחת קיצורים (AI)': אין טקסט לעיבוד.")

    system_prompt = (
        "אתה מומחה בלשנות עברית, פונטיקה והכנת טקסטים לקריאה קולית רציפה (TTS). "
        "עבור הטקסט הבא:\n"
        "1. הוסף ניקוד מלא ומדויק לכל המילים (כולל דגשים, תנועות והבחנה בין שׁ ימנית לשׂ שמאלית).\n"
        "2. פתח ראשי תיבות וקיצורים בעברית למילים מלאות ומנוקדות (כגון רמב\"ם -> רַבֵּינוּ מֹשֶׁה בֶּן מַיְמוֹן, עכ\"פ -> עַל כָּל פָּנִים, וכד' -> וְכַדּוֹמֶה).\n"
        "3. נקה סימוני שוליים מפריעים כמו מספרי הערות שוליים ([1], (א)).\n"
        "4. חלק את הטקסט למשפטים ברורים המותאמים לקצב נשימה.\n"
        'החזר אך ורק JSON במבנה: {"vocalized_text": "...", "sentences": ["...", "..."], "glossary": [{"term": "...", "meaning": "..."}]}'
    )

    all_vocalized_parts: List[str] = []
    all_sentences: List[str] = []
    all_glossary: List[Dict[str, str]] = []

    for idx, chunk in enumerate(chunks, 1):
        if cancel_event is not None and cancel_event.is_set():
            raise AICancelledError(stage="ניקוד ופתיחת קיצורים (AI)")
        if progress_cb:
            progress_cb(f"מנקד טקסט AI (חלק {idx} מתוך {len(chunks)})...")

        kwargs: Dict[str, Any] = {
            "provider_id": pid,
            "api_key": key,
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": chunk,
            "base_url": base_url,
            "json_mode": True,
            "temperature": 0.15,
            "connect_timeout": 10.0,
            "read_timeout": 35.0,
            "total_deadline": 105.0,
            "max_retries": 2,
            "cancel_event": cancel_event,
            "progress_cb": progress_cb,
            "stage": f"ניקוד ופתיחת קיצורים (חלק {idx}/{len(chunks)})",
        }
        try:
            raw = caller(**kwargs)
        except TypeError:
            raw = caller(
                provider_id=pid,
                api_key=key,
                model=model,
                system_prompt=system_prompt,
                user_prompt=chunk,
                base_url=base_url,
                json_mode=True,
                temperature=0.15,
            )

        parsed = extract_json_object(raw)
        voc = (parsed.get("vocalized_text") or "").strip()
        sents = [str(s).strip() for s in (parsed.get("sentences") or []) if str(s).strip()]
        if not voc and not sents:
            raise AICallError(
                f"שלב 'ניקוד ופתיחת קיצורים': התקבל פלט ריק מהמודל בחלק {idx} מתוך {len(chunks)}.",
                stage="ניקוד ופתיחת קיצורים",
                category="invalid_response",
            )
        if not sents:
            from book_loader import split_into_sentences
            sents = split_into_sentences(voc)
        if not voc:
            voc = " ".join(sents)

        all_vocalized_parts.append(voc)
        all_sentences.extend(sents)
        for g in (parsed.get("glossary") or []):
            if isinstance(g, dict) and g.get("term"):
                all_glossary.append(g)

    return {
        "vocalized_text": "\n\n".join(all_vocalized_parts),
        "sentences": all_sentences,
        "glossary": all_glossary,
        "source": f"{get_provider_label(pid)} ({model})",
    }


def ask_companion(
    text: str,
    mode: str = "explain",
    book_title: str = "",
    api_key: str = "",
    settings: Dict[str, Any] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """ביאור פסקה, סיכום פרק או שאלות הבנה באמצעות הספק והמודל שנבחרו בהגדרות."""
    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    key = (api_key or pconf.get("api_key", "")).strip()
    model = pconf.get("model", "")
    base_url = pconf.get("base_url", "")

    if not key and pid != "custom":
        raise AICallError(
            f"כדי להשתמש בעוזר הספרותי עם {get_provider_label(pid)}, "
            "לחץ בסרגל הכלים על '⚙️ הגדרות AI ומודלים' והזן מפתח API ובחר מודל.",
            stage="עוזר ספרותי AI",
            category="auth",
        )

    mode_instructions = {
        "explain": "באר את הטקסט בעברית בהירה, הסבר מילים קשות, הקשר היסטורי ורעיונות מרכזיים.",
        "summary": "כתוב סיכום מתומצת ומאיר עיניים של הקטע בנקודות (Bullet points), עם מסקנות ורעיונות מרכזיים.",
        "quiz": "חבר 3 שאלות הבנה מעניינות עם תשובות קצרות לבדיקת הבנת הנקרא.",
        "simplify": "נסח מחדש בשפה עברית מודרנית ובהירה, פשוטה לקריאה.",
    }

    instruction = mode_instructions.get(mode, mode_instructions["explain"])
    system_prompt = (
        "אתה עוזר ספרותי אישי ומקצועי בתוכנת PorMyCodex. "
        "ענה בעברית עשירה, מדויקת ובהירה."
    )
    title_line = f'שם הספר: "{book_title}"\n' if book_title else ""
    user_prompt = (
        f"{title_line}"
        f"משימה: {instruction}\n\n"
        f'טקסט:\n"""\n{text[:4500]}\n"""'
    )

    reply = chat_completion(
        provider_id=pid,
        api_key=key,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        base_url=base_url,
        json_mode=False,
        temperature=0.3,
        connect_timeout=10.0,
        read_timeout=35.0,
        total_deadline=95.0,
        max_retries=2,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        stage=f"עוזר ספרותי AI ({mode})",
    )
    if not reply:
        raise AICallError(
            f"המודל ({model}) החזיר תשובה ריקה בשלב 'עוזר ספרותי AI'. נסה שוב.",
            stage="עוזר ספרותי AI",
            category="invalid_response",
        )
    return reply
