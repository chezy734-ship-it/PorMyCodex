# -*- coding: utf-8 -*-
"""
מנוע יצירת תסריט AI (Script Studio) עבור PorMyCodex.
בהשראת NarrationScriptService של 'קורא ספרים חכם' (AI-Smart-Book-Reader):
1. תסריט לימודי (Educational): דיאלוג חי בין מורה ותלמיד המבאר את הטקסט מבלי להמציא עובדות.
2. תסריט דרמטי (Dramatic): קריינות ספרותית אקספרסיבית עם סימני הפסקה, הדגשה וסגנון קולי.
תומך בהפקת אודיו רב-קולי (מורה בקול אברי, תלמיד בקול הילה).
"""

from typing import List, Dict, Any
from settings_manager import load_settings, get_active_provider_config
from ai_providers import chat_completion, extract_json_object, get_provider_label
from book_loader import split_into_sentences


SPEAKER_DISPLAY = {
    "teacher": "🎓 מורה",
    "student": "🙋 תלמיד",
    "narrator": "🎙️ מספר",
}


def generate_narration_script(
    text: str,
    mode: str = "educational",
    custom_instruction: str = "",
    book_title: str = "",
    settings: Dict[str, Any] = None,
    use_wikipedia: bool = False,
    wiki_terms: str = "",
) -> Dict[str, Any]:
    """
    יוצר תסריט מובנה (רשימת שורות עם דובר, טקסט, סגנון והערת בדיקה) מהטקסט הנוכחי.
    אופציונלי: שולף מידע מאומת מוויקיפדיה כדי שהמורה/מספר יעשיר את ההסברים בידע אנציקלופדי.
    """
    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("אין טקסט בפרק ליצירת תסריט.")

    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    model = pconf.get("model", "")
    api_key = pconf.get("api_key", "")

    # בדיקה מוקדמת אם יש מפתח (פרט לשרת מקומי כמו Ollama)
    if not api_key and pid != "custom":
        raise ValueError(
            f"לא הוגדר מפתח API עבור {get_provider_label(pid)}.\n"
            "לחץ בסרגל הכלים על '⚙️ הגדרות AI ומודלים' כדי להכניס מפתח API של גוגל, OpenAI, Claude או OpenRouter."
        )

    wiki_articles: List[Dict[str, str]] = []
    wiki_section = ""
    if use_wikipedia:
        try:
            from wiki_engine import fetch_wikipedia_context_for_terms
            wiki_data = fetch_wikipedia_context_for_terms(
                terms_input=wiki_terms,
                fallback_text=clean_text[:2500],
                settings=cfg,
            )
            wiki_articles = wiki_data.get("articles", [])
            if wiki_data.get("context_text"):
                wiki_section = (
                    "\n\n--- רקע אנציקלופדי מאומת מוויקיפדיה להעשרת ההסברים (שלב עובדות רלוונטיות בלבד) ---\n"
                    + wiki_data["context_text"][:4000]
                )
        except Exception:
            wiki_articles = []

    sentences = split_into_sentences(clean_text)
    units = [{"unitId": f"u{i+1}", "text": s} for i, s in enumerate(sentences[:80])]

    if mode == "dramatic":
        style_guide = (
            "צור תסריט הקראה דרמטי ואקספרסיבי בעברית. "
            "שמור על כל העובדות, השמות, המספרים והרעיונות מהמקור מבלי להוסיף עלילה מומצאת. "
            "השתמש בעיקר בתפקיד 'narrator' (ואפשר גם 'teacher'/'student' בציטוטים מתאימים), "
            "וציין בשדה 'style' הנחיית הגשה קצרה בעברית כגון: 'רגוע', 'מודגש', 'דרמטי', 'השהיה קצרה'."
        )
    else:
        style_guide = (
            "צור תסריט לימודי מרתק בעברית בצורת דיאלוג בין מורה ('teacher') לתלמיד ('student'). "
            "התלמיד שואל שאלות סקרנות טבעיות על הכתוב, והמורה מסביר ומדייק בבהירות לפי המקור (ואם סופק מידע מוויקיפדיה — שלב אותו בהסבר המורה). "
            "אל תמציא עובדות, חישובים או ציטוטים שלא סופקו. אם קטע אינו ברור דיו, סמן needsReview=true."
        )

    extra = f"\nהנחיה נוספת מהמשתמש: {custom_instruction.strip()}" if custom_instruction.strip() else ""

    system_prompt = (
        "אתה עורך תסריטים וקריינות מקצועי בעברית עבור תוכנת PorMyCodex. "
        + style_guide
        + extra
        + "\nהחזר אך ורק JSON תקין במבנה המדויק הבא:\n"
        '{"title": "כותרת קצרה לתסריט", "lines": [{"unitId": "u1", "speakerRole": "teacher|student|narrator", "style": "סגנון הגשה", "text": "טקסט השורה בעברית מנוקדת/ברורה לקריאה", "needsReview": false}]}'
    )

    user_prompt = (
        f"שם הספר: {book_title or 'ללא שם'}\n"
        f"מצב תסריט: {'לימודי (מורה ותלמיד)' if mode == 'educational' else 'דרמטי (קריינות)'}\n"
        "יחידות המקור:\n"
        + "\n".join(f"[{u['unitId']}] {u['text']}" for u in units)
        + wiki_section
    )

    raw_response = chat_completion(
        provider_id=pid,
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        base_url=pconf.get("base_url", ""),
        json_mode=True,
        temperature=0.35,
    )

    parsed = extract_json_object(raw_response)
    raw_lines = parsed.get("lines", [])
    if not isinstance(raw_lines, list) or not raw_lines:
        raise RuntimeError("המודל לא החזיר שורות תסריט תקינות.")

    normalized_lines: List[Dict[str, Any]] = []
    for idx, line in enumerate(raw_lines):
        if not isinstance(line, dict):
            continue
        text_val = str(line.get("text") or "").strip()
        if not text_val:
            continue
        role = str(line.get("speakerRole") or "narrator").strip().lower()
        if role not in ("teacher", "student", "narrator"):
            role = "narrator"
        normalized_lines.append({
            "unitId": str(line.get("unitId") or f"u{idx+1}"),
            "speakerRole": role,
            "speakerLabel": SPEAKER_DISPLAY.get(role, "🎙️ מספר"),
            "style": str(line.get("style") or "רגוע").strip(),
            "text": text_val,
            "needsReview": bool(line.get("needsReview", False)),
        })

    if not normalized_lines:
        raise RuntimeError("התסריט שהתקבל ריק.")

    return {
        "title": str(parsed.get("title") or f"תסריט - {book_title}"),
        "mode": mode,
        "provider_label": f"{get_provider_label(pid)} ({model})",
        "lines": normalized_lines,
        "wiki_articles": wiki_articles,
    }


def format_script_as_text(script_data: Dict[str, Any]) -> str:
    header = f"=== {script_data.get('title', 'תסריט')} ===\nספק: {script_data.get('provider_label', '')}\n\n"
    body = []
    for line in script_data.get("lines", []):
        style = f" [{line['style']}]" if line.get("style") else ""
        flag = " ⚠️ (דרוש עיון)" if line.get("needsReview") else ""
        body.append(f"{line.get('speakerLabel', '🎙️ מספר')}{style}: {line.get('text', '')}{flag}")
    wiki_articles = script_data.get("wiki_articles") or []
    if wiki_articles:
        body.append("\n--- מקורות ויקיפדיה ששולבו בתסריט ---")
        for art in wiki_articles:
            body.append(f"• {art.get('title', '')} ({art.get('lang', '')}): {art.get('url', '')}")
    return header + "\n\n".join(body)
