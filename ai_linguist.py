# -*- coding: utf-8 -*-
"""
מודול בלשנות ובינה מלאכותית (AI Linguist) לסיוע בהקראה,
ניקוד מדויק, פתיחת ראשי תיבות, ביאור מילים וסיכום פרקים בעברית.
"""

import os
import json
from typing import Dict, Any, List

def get_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "")

def vocalize_and_expand_hebrew(text: str, api_key: str = "") -> Dict[str, Any]:
    """
    מנקד טקסט עברי במדויק ופותח ראשי תיבות באמצעות Gemini 3.8 Flash.
    אם אין מפתח API - מבצע חלוקה מקומית למשפטים כגיבוי ללא תלות ברשת.
    """
    key = api_key or get_api_key()
    if not key:
        from book_loader import split_into_sentences
        sents = split_into_sentences(text)
        return {
            "vocalized_text": text,
            "sentences": sents,
            "glossary": [],
            "source": "offline-fallback"
        }

    try:
        from google import genai
        client = genai.Client(api_key=key)
        
        prompt = f"""
אתה מומחה בלשנות עברית, פונטיקה והכנת טקסטים לקריאה קולית רציפה (TTS).
עבור הטקסט הבא, בצע הכנה מושלמת לקריאה שוטפת ומדויקת:
1. הוסף ניקוד מלא ומדויק לכל המילים (כולל דגשים, תנועות והבחנה בין שׁ ימנית לשׂ שמאלית), כדי שמנוע דיבור יקריא ללא שיבושים.
2. פתח ראשי תיבות וקיצורים בעברית למילים מלאות ומנוקדות (למשל: רמב"ם -> רַבֵּינוּ מֹשֶׁה בֶּן מַיְמוֹן, עכ"פ -> עַל כָּל פָּנִים, אחה"צ -> אַחַר הַצָּהֳרַיִם, וכד' -> וְכַדּוֹמֶה, ביהמ"ש -> בֵּית הַמִּשְׁפָּט, זצ"ל -> זֵכֶר צַדִּיק לִבְרָכָה).
3. נקה סימוני שוליים מפריעים כגון מספרי הערות שוליים ([1], (א)).
4. חלק את הטקסט למשפטים ברורים המותאמים לקצב נשימה.

טקסט המקור:
\"\"\"
{text[:7000]}
\"\"\"

החזר אך ורק JSON במבנה:
{{
  "vocalized_text": "הטקסט המלא מנוקד",
  "sentences": ["משפט ראשון מנוקד", "משפט שני מנוקד"],
  "glossary": [
    {{"term": "מילה קשה", "meaning": "הסבר קצר"}}
  ]
}}
"""
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        
        parsed = json.loads(response.text or "{}")
        vocalized = parsed.get("vocalized_text", text)
        sentences = parsed.get("sentences", [])
        if not sentences:
            from book_loader import split_into_sentences
            sentences = split_into_sentences(vocalized)
            
        return {
            "vocalized_text": vocalized,
            "sentences": sentences,
            "glossary": parsed.get("glossary", []),
            "source": "gemini-3.8-flash"
        }
    except Exception as e:
        from book_loader import split_into_sentences
        sents = split_into_sentences(text)
        return {
            "vocalized_text": text,
            "sentences": sents,
            "glossary": [],
            "source": f"error-fallback: {e}"
        }

def ask_companion(text: str, mode: str = "explain", book_title: str = "", api_key: str = "") -> str:
    """ביאור פסקה, סיכום פרק או שאלות הבנה באמצעות AI."""
    key = api_key or get_api_key()
    if not key:
        return "כדי להשתמש בעוזר הספרותי, הגדר מפתח Gemini API בהגדרות או במשתני הסביבה."

    try:
        from google import genai
        client = genai.Client(api_key=key)

        mode_instructions = {
            "explain": "באר את הטקסט בעברית בהירה, הסבר מילים קשות, הקשר היסטורי ורעיונות מרכזיים.",
            "summary": "כתוב סיכום מתומצת ומאיר עיניים של הקטע בנקודות (Bullet points), עם מסקנות ורעיונות מרכזיים.",
            "quiz": "חבר 3 שאלות הבנה מעניינות עם תשובות קצרות לבדיקת הבנת הנקרא.",
            "simplify": "נסח מחדש בשפה עברית מודרנית ובהירה, פשוטה לקריאה."
        }

        instruction = mode_instructions.get(mode, mode_instructions["explain"])
        prompt = f"""
אתה עוזר ספרותי אישי ומקצועי למקריא הספרים PorMyCodex.
{f'שם הספר: "{book_title}"' if book_title else ''}

משימה: {instruction}

טקסט:
\"\"\"
{text[:5000]}
\"\"\"

ענה בעברית עשירה ובהירה.
"""
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt
        )
        return response.text or "לא התקבלה תשובה מהמודל."
    except Exception as e:
        return f"שגיאה בהפעלת העוזר: {e}"
