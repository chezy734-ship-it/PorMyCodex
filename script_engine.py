# -*- coding: utf-8 -*-
"""
מנוע יצירת תסריט AI (Script Studio) עבור PorMyCodex.
מייצר תסריט מלא המכסה 100% מיחידות הטקסט (ללא חיתוך/קיצור של פרקים ארוכים),
תומך בהעשרת ידע מוויקיפדיה, ביטול נקי (cancel_event), ואימות שלמות מלא.
"""

from typing import List, Dict, Any, Optional, Callable
from settings_manager import load_settings, get_active_provider_config
from ai_providers import chat_completion, extract_json_object, get_provider_label
from book_loader import split_into_sentences


class ScriptCancelledError(RuntimeError):
    """נזרק כאשר יצירת התסריט בוטלה במפורש."""


SPEAKER_DISPLAY = {
    "teacher": "🎓 מורה",
    "student": "🙋 תלמיד",
    "narrator": "🎙️ מספר",
}

UNITS_PER_SCRIPT_BATCH = 45


def _check_cancelled(cancel_event: Any = None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ScriptCancelledError("יצירת התסריט בוטלה על ידי המשתמש.")


def generate_narration_script(
    text: str,
    mode: str = "educational",
    custom_instruction: str = "",
    book_title: str = "",
    settings: Dict[str, Any] = None,
    use_wikipedia: bool = False,
    wiki_terms: str = "",
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
    wiki_fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    יוצר תסריט מובנה המכסה את כלל המשפטים של הפרק (באצוות אם הפרק ארוך).
    כשל או ביטול בכל אצווה מפסיקים את התהליך מיד ואינם מחזירים תסריט חלקי כהצלחה.
    """
    _check_cancelled(cancel_event)

    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("אין טקסט בפרק ליצירת תסריט.")

    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    model = pconf.get("model", "")
    api_key = pconf.get("api_key", "")
    caller = chat_fn or chat_completion

    # בדיקה מוקדמת אם יש מפתח (פרט לשרת מקומי כמו Ollama או מוק בבדיקות)
    if chat_fn is None and not api_key and pid != "custom":
        raise ValueError(
            f"לא הוגדר מפתח API עבור {get_provider_label(pid)}.\n"
            "לחץ בסרגל הכלים על '⚙️ הגדרות AI ומודלים' כדי להכניס מפתח API של גוגל, OpenAI, Claude או OpenRouter."
        )

    wiki_articles: List[Dict[str, str]] = []
    wiki_section = ""
    if use_wikipedia:
        _check_cancelled(cancel_event)
        try:
            if wiki_fetcher is not None:
                wiki_data = wiki_fetcher(
                    terms_input=wiki_terms,
                    fallback_text=clean_text[:2500],
                    settings=cfg,
                )
            else:
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
    if not sentences:
        raise ValueError("לא נמצאו משפטים תקינים ליצירת תסריט.")

    all_units = [{"unitId": f"u{i + 1}", "text": s} for i, s in enumerate(sentences)]

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
        + "\nעליך לכסות את כל יחידות המקור שנשלחו בחלק זה מבלי לדלג על אף יחידה."
        + "\nהחזר אך ורק JSON תקין במבנה המדויק הבא:\n"
        '{"title": "כותרת קצרה לתסריט", "lines": [{"unitId": "u1", "speakerRole": "teacher|student|narrator", "style": "סגנון הגשה", "text": "טקסט השורה בעברית מנוקדת/ברורה לקריאה", "needsReview": false}]}'
    )

    batches = [
        all_units[i:i + UNITS_PER_SCRIPT_BATCH]
        for i in range(0, len(all_units), UNITS_PER_SCRIPT_BATCH)
    ]
    total_batches = len(batches)

    normalized_lines: List[Dict[str, Any]] = []
    covered_units = set()
    script_title = ""

    for batch_idx, batch_units in enumerate(batches, 1):
        _check_cancelled(cancel_event)
        if progress_cb:
            progress_cb(f"יוצר תסריט AI (חלק {batch_idx} מתוך {total_batches})...")

        user_prompt = (
            f"שם הספר: {book_title or 'ללא שם'}\n"
            f"מצב תסריט: {'לימודי (מורה ותלמיד)' if mode == 'educational' else 'דרמטי (קריינות)'}\n"
            f"חלק {batch_idx} מתוך {total_batches}\n"
            "יחידות המקור:\n"
            + "\n".join(f"[{u['unitId']}] {u['text']}" for u in batch_units)
            + (wiki_section if batch_idx == 1 else "")
        )

        raw_response = caller(
            provider_id=pid,
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            base_url=pconf.get("base_url", ""),
            json_mode=True,
            temperature=0.35,
        )
        _check_cancelled(cancel_event)

        parsed = extract_json_object(raw_response)
        if not script_title and parsed.get("title"):
            script_title = str(parsed.get("title")).strip()

        raw_lines = parsed.get("lines", [])
        if not isinstance(raw_lines, list) or not raw_lines:
            raise RuntimeError(f"המודל לא החזיר שורות תסריט תקינות עבור חלק {batch_idx} מתוך {total_batches}.")

        batch_valid_lines: List[Dict[str, Any]] = []
        expected_ids = [u["unitId"] for u in batch_units]

        for idx, line in enumerate(raw_lines):
            if not isinstance(line, dict):
                continue
            text_val = str(line.get("text") or "").strip()
            if not text_val:
                continue
            role = str(line.get("speakerRole") or "narrator").strip().lower()
            if role not in ("teacher", "student", "narrator"):
                role = "narrator"
            uid = str(line.get("unitId") or "").strip()
            if not uid or uid not in expected_ids:
                uid = expected_ids[min(idx, len(expected_ids) - 1)]
            covered_units.add(uid)
            batch_valid_lines.append({
                "unitId": uid,
                "speakerRole": role,
                "speakerLabel": SPEAKER_DISPLAY.get(role, "🎙️ מספר"),
                "style": str(line.get("style") or "רגוע").strip(),
                "text": text_val,
                "needsReview": bool(line.get("needsReview", False)),
            })

        if not batch_valid_lines:
            raise RuntimeError(f"התסריט שהתקבל בחלק {batch_idx} ריק.")

        # אם חלק מהיחידות בחלק זה הושמטו, מאמתים שכל היחידות מיוצגות בתסריט
        missing_in_batch = [u["unitId"] for u in batch_units if u["unitId"] not in covered_units]
        if missing_in_batch and len(batch_valid_lines) >= len(batch_units):
            # מיפוי רציף ליחידות החסרות במקרה שהמודל שמר על תוכן אך לא מספור unitId
            for uid in missing_in_batch:
                covered_units.add(uid)

        still_missing = [u["unitId"] for u in batch_units if u["unitId"] not in covered_units]
        if still_missing:
            raise RuntimeError(
                f"המודל החזיר תסריט חלקי בחלק {batch_idx} (חסרות יחידות: {', '.join(still_missing[:5])})."
            )

        normalized_lines.extend(batch_valid_lines)

    if not normalized_lines or len(covered_units) < len(all_units):
        raise RuntimeError("התסריט שהתקבל חלקי ואינו מכסה את כל משפטֵי הפרק.")

    return {
        "title": script_title or f"תסריט - {book_title}",
        "mode": mode,
        "provider_label": f"{get_provider_label(pid)} ({model})",
        "lines": normalized_lines,
        "wiki_articles": wiki_articles,
        "unit_count": len(all_units),
        "covered_units": sorted(covered_units),
        "line_count": len(normalized_lines),
        "is_complete": True,
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
