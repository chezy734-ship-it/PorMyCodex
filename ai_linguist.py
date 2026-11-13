# -*- coding: utf-8 -*-
"""
מודול בלשנות ובינה מלאכותית (AI Linguist) לסיוע בהקראה,
ניקוד מדויק, פתיחת ראשי תיבות, ביאור מילים וסיכום פרקים בעברית.
עובד מול כל ספק ומודל שהוגדר ב-settings_manager (Gemini, OpenAI, Claude, OpenRouter, Groq, שרת מותאם).
"""

from typing import Dict, Any
from settings_manager import load_settings, get_active_provider_config
from ai_providers import chat_completion, extract_json_object, get_provider_label


def vocalize_and_expand_hebrew(text: str, api_key: str = "", settings: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    מנקד טקסט עברי במדויק ופותח ראשי תיבות באמצעות הספק והמודל שנבחרו בהגדרות.
    אם אין מפתח API - מבצע חלוקה מקומית למשפטים כגיבוי ללא תלות ברשת.
    """
    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    key = api_key or pconf.get("api_key", "")
    model = pconf.get("model", "")
    base_url = pconf.get("base_url", "")

    if not key and pid != "custom":
        from book_loader import split_into_sentences
        sents = split_into_sentences(text)
        return {
            "vocalized_text": text,
            "sentences": sents,
            "glossary": [],
            "source": "offline-fallback",
            "note": f"לא הוגדר מפתח API עבור {get_provider_label(pid)} — בוצעה חלוקה מקומית ללא ניקוד AI.",
        }

    try:
        system_prompt = (
            "אתה מומחה בלשנות עברית, פונטיקה והכנת טקסטים לקריאה קולית רציפה (TTS). "
            "עבור הטקסט הבא:\n"
            "1. הוסף ניקוד מלא ומדויק לכל המילים (כולל דגשים, תנועות והבחנה בין שׁ ימנית לשׂ שמאלית).\n"
            "2. פתח ראשי תיבות וקיצורים בעברית למילים מלאות ומנוקדות (כגון רמב\"ם -> רַבֵּינוּ מֹשֶׁה בֶּן מַיְמוֹן, עכ\"פ -> עַל כָּל פָּנִים, וכד' -> וְכַדּוֹמֶה).\n"
            "3. נקה סימוני שוליים מפריעים כמו מספרי הערות שוליים ([1], (א)).\n"
            "4. חלק את הטקסט למשפטים ברורים המותאמים לקצב נשימה.\n"
            'החזר אך ורק JSON במבנה: {"vocalized_text": "...", "sentences": ["...", "..."], "glossary": [{"term": "...", "meaning": "..."}]}'
        )

        raw = chat_completion(
            provider_id=pid,
            api_key=key,
            model=model,
            system_prompt=system_prompt,
            user_prompt=text[:7000],
            base_url=base_url,
            json_mode=True,
            temperature=0.15,
        )

        parsed = extract_json_object(raw)
        vocalized = parsed.get("vocalized_text", text)
        sentences = parsed.get("sentences", [])
        if not sentences:
            from book_loader import split_into_sentences
            sentences = split_into_sentences(vocalized)

        return {
            "vocalized_text": vocalized,
            "sentences": sentences,
            "glossary": parsed.get("glossary", []),
            "source": f"{get_provider_label(pid)} ({model})",
        }
    except Exception as e:
        from book_loader import split_into_sentences
        sents = split_into_sentences(text)
        return {
            "vocalized_text": text,
            "sentences": sents,
            "glossary": [],
            "source": f"error-fallback: {e}",
            "note": str(e),
        }


def ask_companion(text: str, mode: str = "explain", book_title: str = "", api_key: str = "", settings: Dict[str, Any] = None) -> str:
    """ביאור פסקה, סיכום פרק או שאלות הבנה באמצעות הספק והמודל שנבחרו בהגדרות."""
    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    key = api_key or pconf.get("api_key", "")
    model = pconf.get("model", "")
    base_url = pconf.get("base_url", "")

    if not key and pid != "custom":
        return (
            f"כדי להשתמש בעוזר הספרותי עם **{get_provider_label(pid)}**, "
            "לחץ בסרגל הכלים על **⚙️ הגדרות AI ומודלים** והזן מפתח API (גוגל, OpenAI, Claude, OpenRouter או Groq)."
        )

    try:
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
            f'טקסט:\n"""\n{text[:6000]}\n"""'
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
        )
        return reply or "לא התקבלה תשובה מהמודל."
    except Exception as e:
        return f"שגיאה בהפעלת העוזר ({get_provider_label(pid)} / {model}): {e}"
