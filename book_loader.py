# -*- coding: utf-8 -*-
"""
מודול לטעינה, חילוץ וחלוקת ספרים וטקסטים גדולים (PDF / DOCX / TXT / MD) לפרקים ומשפטים.
כולל מנגנון עמיד לספרים בני מאות ואלפי עמודים:
- קריאה בטוחה עמוד-אחר-עמוד עם דיווח התקדמות והגנה מרקורסיה/עמודים פגומים
- חלוקה אוטומטית לחלקים/פרקים נוחים (~12 עמודים או ~10,000 תווים לפרק) כדי למנוע עומס זיכרון או קריסה
"""

import os
import re
import sys
from typing import List, Dict, Any, Callable, Optional, Tuple

MAX_CHAPTER_CHARS = 12000
DEFAULT_SEGMENT_CHARS = 6000
PAGES_PER_CHUNK = 12

SEGMENT_PRESETS: Dict[str, Dict[str, Any]] = {
    "compact": {
        "label": "קצר ומדויק (~3,500 תווים למקטע — מהיר ל-AI ולהקראה)",
        "max_chars": 3500,
        "min_chars": 700,
        "pages_per_chunk": 5,
    },
    "balanced": {
        "label": "בינוני מאוזן (~6,000 תווים למקטע — מומלץ לספרים גדולים)",
        "max_chars": 6000,
        "min_chars": 1200,
        "pages_per_chunk": 8,
    },
    "chapter": {
        "label": "מורחב / לפי פרקים (~12,000 תווים למקטע)",
        "max_chars": 12000,
        "min_chars": 2000,
        "pages_per_chunk": 12,
    },
}

_MAJOR_HEADING_RE = re.compile(
    r"^(?:#{1,3}\s+\S|"
    r"(?:פרק|חלק|שער|ספר|מאמר|סימן)\s+[א-ת0-9\"'׳״IVXLCDM\-]+|"
    r"(?:chapter|part|book|section)\s+(?:\d+|[IVXLCDM]+)\b|"
    r"(?:הקדמה|מבוא|פתיחה|אחרית\s+דבר|סיכום|נספח|introduction|preface|prologue|epilogue|conclusion|appendix)\b)",
    re.IGNORECASE,
)

_SUB_HEADING_RE = re.compile(
    r"^(?:#{4,6}\s+\S|"
    r"(?:סעיף|הלכה|משנה|פסקה|נושא|תת[- ]פרק)\s+[א-ת0-9\"'׳״]+|"
    r"(?:\d{1,3}|[א-ת]{1,2})\.\s+\S)",
    re.IGNORECASE,
)

SAMPLE_BOOKS: List[Dict[str, Any]] = [
    {
        "id": "rambam_deot",
        "title": "הלכות דעות - משנה תורה",
        "author": "הרמב\"ם (רבי משה בן מימון)",
        "description": "פרקי יסוד על תיקון המידות, בריאות הגוף והנפש והנהגת האדם בדרך האמצעית.",
        "chapters": [
            {
                "title": "פרק א׳ - דרך האמצע ותיקון המידות",
                "text": """דֵּעוֹת הַרְבֵּה יֵשׁ לְכָל אֶחָד וְאֶחָד מִבְּנֵי אָדָם, וְזוֹ מְשֻׁנָּה מִזּוֹ וּרְחוֹקָה מִמֶּנָּה בְּיוֹתֵר.
יֵשׁ אָדָם שֶׁהוּא בַּעַל חֵמָה כּוֹעֵס תָּמִיד, וְיֵשׁ אָדָם שֶׁדַּעְתּוֹ מְיוּשֶׁבֶת עָלָיו וְאֵינוֹ כּוֹעֵס כְּלָל.
וְיֵשׁ אָדָם שֶׁהוּא גְּבַהּ לֵב בְּיוֹתֵר, וְיֵשׁ שֶׁהוּא שְׁפַל רוּחַ עַד מְאֹד.
וּשְׁתֵּי קְצָווֹת הָרְחוֹקוֹת זוֹ מִזּוֹ בְּכָל דֵּעָה וְדֵעָה אֵינָן דֶּרֶךְ טוֹבָה, וְאֵין רָאוּי לָאָדָם לָלֶכֶת בָּהֶן.
הַדֶּרֶךְ הַיְשָׁרָה הִיא מִדָּה בֵּינוֹנִית שֶׁבְּכָל דֵּעָה וְדֵעָה מִכָּל הַדֶּעוֹת שֶׁיֵּשׁ לָאָדָם; וְהִיא הַדֶּעָה שֶׁהִיא רְחוֹקָה מִשְּׁתֵּי הַקְּצָווֹת רִחוּק שָׁוֶה.
לְפִיכָךְ צִוּוּ חֲכָמִים הָרִאשׁוֹנִים שֶׁיְּהֵא אָדָם שָׁם דֵּעוֹתָיו תָּמִיד וּמְשַׁעֵר אוֹתָן וּמְכַוֵּן אוֹתָן בַּדֶּרֶךְ הָאֶמְצָעִית."""
            },
            {
                "title": "פרק ד׳ - הנהגת בריאות הגוף והתזונה",
                "text": """הוֹאִיל וֶהֱיוֹת הַגּוּף בָּרִיא וְשָׁלֵם מִדַּרְכֵי הַשֵּׁם הוּא, שֶׁאִי אֶפְשָׁר שֶׁיָּבִין אוֹ יֵדַע דָּבָר מִידִיעַת הַבּוֹרֵא וְהוּא חוֹלֶה.
לְפִיכָךְ צָרִיךְ לְהַרְחִיק עַצְמוֹ מִדְּבָרִים הַמְאַבְּדִין אֶת הַגּוּף וּלְהַנְהִיג עַצְמוֹ בִּדְבָרִים הַמַּבְרִיאִים.
לְעוֹלָם לֹא יֹאכַל אָדָם אֶלָּא כְּשֶׁהוּא רָעֵב, וְלֹא יִשְׁתֶּה אֶלָּא כְּשֶׁהוּא צָמֵא.
וְלֹא יִשְׁבַּע עַד שֶׁתִּתְמַלֵּא כְּרֵסוֹ, אֶלָּא יִפְחֹת כְּמוֹ רְבִיעַ מִשָּׂבְעָתוֹ.
כָּל זְמַן שֶׁאָדָם מִתְעַמֵּל וְיָגֵעַ הַרְבֵּה וְאֵינוֹ שָׂבֵעַ, אֵין חֹלִי בָּא עָלָיו וְכֹחוֹ מִתְחַזֵּק."""
            }
        ]
    },
    {
        "id": "mishlei",
        "title": "ספר משלי - חכמת שלמה המלך",
        "author": "שלמה המלך",
        "description": "פתגמי חכמה ולקחי חיים בשירה מקראית עשירה.",
        "chapters": [
            {
                "title": "פרק ג׳ - יקרת החכמה והנחיית החיים",
                "text": """בְּנִי, תּוֹרָתִי אַל תִּשְׁכָּח, וּמִצְוֺתַי יִצֹּר לִבֶּךָ.
כִּי אֹרֶךְ יָמִים וּשְׁנוֹת חַיִּים וְשָׁלוֹם יוֹסִיפוּ לָךְ.
חֶסֶד וֶאֱמֶת אַל יַעַזְבֻךָ, קָשְׁרֵם עַל גַּרְגְּרוֹתֶיךָ, כָּתְבֵם עַל לוּחַ לִבֶּךָ.
בְּטַח אֶל ה' בְּכָל לִבֶּךָ, וְאֶל בִּינָתְךָ אַל תִּשָּׁעֵן.
בְּכָל דְּרָכֶיךָ דָעֵהוּ, וְהוּא יְיַשֵּׁר אֹרְחֹתֶיךָ.
אַשְׁרֵי אָדָם מָצָא חָכְמָה, וְאָדָם יָפִיק תְּבוּנָה.
כִּי טוֹב סַחְרָהּ מִסְּחַר כָּסֶף, וּמֵחָרוּץ תְּבוּאָתָהּ.
עֵץ חַיִּים הִיא לַמַּחֲזִיקִים בָּהּ, וְתֹמְכֶיהָ מְאֻשָּׁר."""
            }
        ]
    }
]


def split_into_sentences(text: str) -> List[str]:
    """מחלק טקסט למשפטים נוחים להקראה, מסנן שורות נקודות/קווים ריקות מתוכן ומונע משפטים ארוכים מדי."""
    if not text:
        return []

    # ניקוי שרשראות נקודות/קווים מתוכן עניינים ב-PDF (כגון "פרק א ........ 12")
    cleaned = re.sub(r'\.{3,}', ' — ', text.strip())
    cleaned = re.sub(r'[-_•·]{4,}', ' ', cleaned)
    # איחוד שורות שבורות באמצע משפט ב-PDF, תוך שמירה על סוף משפט אמיתי
    cleaned = re.sub(r'(?<![.?!:\n])\n(?!\n)', ' ', cleaned)

    raw_parts = re.split(r'(?<=[.?!;:\n])\s+', cleaned)
    sentences: List[str] = []
    for part in raw_parts:
        clean = re.sub(r'\s+', ' ', part).strip()
        # דילוג על מקטעים שאין בהם אף אות או ספרה (מונע שורות "." ריקות ב-PDF)
        if not clean or not re.search(r'[0-9A-Za-z\u0590-\u05FF\u0600-\u06FF]', clean):
            continue
        # חלוקת משפט ארוך במיוחד (> 350 תווים) כדי למנוע עומס על TTS ורינדור
        if len(clean) > 350:
            words = clean.split(' ')
            buf = ""
            for w in words:
                if len(buf) + len(w) + 1 <= 280:
                    buf = f"{buf} {w}".strip()
                else:
                    if buf:
                        sentences.append(buf)
                    buf = w
            if buf:
                sentences.append(buf)
        else:
            sentences.append(clean)

    return sentences if sentences else ["אין טקסט קריא במקטע זה."]


def _classify_heading(line: str) -> str:
    """מסווג שורה ככותרת ראשית ('major'), כותרת משנה ('sub') או שורה רגילה ('')."""
    clean = (line or "").strip()
    if not clean or len(clean) > 90:
        return ""
    if _MAJOR_HEADING_RE.match(clean):
        return "major"
    if _SUB_HEADING_RE.match(clean) and not clean.endswith((".", "?", "!")):
        return "sub"
    return ""


def _split_oversized_paragraph(para: str, max_chars: int) -> List[str]:
    """מפצל פסקה בודדת ארוכה במיוחד בגבולות משפטים או מילים בלי לחתוך באמצע מילה."""
    clean = (para or "").strip()
    if len(clean) <= max_chars:
        return [clean] if clean else []

    sents = split_into_sentences(clean)
    parts: List[str] = []
    buf: List[str] = []
    buf_len = 0

    for sent in sents:
        s = sent.strip()
        if not s:
            continue
        if len(s) > max_chars:
            if buf:
                parts.append(" ".join(buf))
                buf = []
                buf_len = 0
            words = s.split(" ")
            w_buf: List[str] = []
            w_len = 0
            for w in words:
                if w_buf and w_len + len(w) + 1 > max_chars:
                    parts.append(" ".join(w_buf))
                    w_buf = [w]
                    w_len = len(w)
                else:
                    w_buf.append(w)
                    w_len += len(w) + (1 if w_buf[:-1] else 0)
            if w_buf:
                parts.append(" ".join(w_buf))
            continue

        add_len = len(s) + (1 if buf else 0)
        if buf and buf_len + add_len > max_chars:
            parts.append(" ".join(buf))
            buf = [s]
            buf_len = len(s)
        else:
            buf.append(s)
            buf_len += add_len

    if buf:
        parts.append(" ".join(buf))
    return parts


def _split_large_chapter(title: str, text: str, max_chars: int = MAX_CHAPTER_CHARS) -> List[Dict[str, str]]:
    """מפצל פרק/נושא ענק לתתי-מקטעים נוחים בגבולות פסקאות ומשפטים."""
    clean = (text or "").strip()
    limit = max(600, int(max_chars or DEFAULT_SEGMENT_CHARS))
    if len(clean) <= limit:
        return [{"title": (title or "מקטע")[:70], "text": clean}] if clean else []

    parts: List[Dict[str, str]] = []
    raw_paragraphs = [p.strip() for p in re.split(r"\n+", clean) if p.strip()]
    paragraphs: List[str] = []
    for p in raw_paragraphs:
        if len(p) > limit:
            paragraphs.extend(_split_oversized_paragraph(p, limit))
        else:
            paragraphs.append(p)

    buf: List[str] = []
    buf_len = 0
    part_idx = 1

    for para in paragraphs:
        add_len = len(para) + (1 if buf else 0)
        if buf and buf_len + add_len > limit:
            parts.append({
                "title": f"{title[:45]} (חלק {part_idx})",
                "text": "\n".join(buf),
            })
            part_idx += 1
            buf = [para]
            buf_len = len(para)
        else:
            buf.append(para)
            buf_len += add_len

    if buf:
        parts.append({
            "title": f"{title[:45]} (חלק {part_idx})" if part_idx > 1 else title[:70],
            "text": "\n".join(buf),
        })

    return parts


def segment_text_smartly(
    full_text: str,
    *,
    max_chars: int = DEFAULT_SEGMENT_CHARS,
    min_chars: int = 1000,
    base_title: str = "מקטע",
) -> List[Dict[str, str]]:
    """
    מפצל טקסט ארוך למקטעים חכמים לפי הצורך והעניין:
    1. מזהה כותרות פרקים/נושאים/סעיפים בעברית ובאנגלית ושומר על התחלת נושא כגבול מקטע.
    2. מאחד תתי-נושאים זעירים מתחת ל-min_chars (כשלא מדובר בכותרת פרק ראשית) כדי למנוע פיצול-יתר.
    3. מפצל מקטעים ארוכים מעבר ל-max_chars בגבולות פסקאות ומשפטים בלבד.
    """
    clean = (full_text or "").strip()
    if not clean:
        return [{"title": f"{base_title} א׳ (ריק)", "text": "לא נמצא טקסט לקריאה בקובץ."}]

    limit = max(600, int(max_chars or DEFAULT_SEGMENT_CHARS))
    floor = max(200, min(int(min_chars or 0), limit // 2))

    lines = clean.splitlines()
    raw_sections: List[Dict[str, Any]] = []
    cur_title = ""
    cur_kind = ""
    cur_lines: List[str] = []

    for ln in lines:
        stripped = ln.strip()
        kind = _classify_heading(stripped)
        if kind and cur_lines and any(x.strip() for x in cur_lines):
            raw_sections.append({
                "title": cur_title or f"{base_title} {len(raw_sections) + 1}",
                "kind": cur_kind or "sub",
                "text": "\n".join(cur_lines).strip(),
            })
            cur_title = stripped.lstrip("#").strip()[:65]
            cur_kind = kind
            cur_lines = [ln]
        else:
            if not cur_title and kind:
                cur_title = stripped.lstrip("#").strip()[:65]
                cur_kind = kind
            cur_lines.append(ln)

    if cur_lines and any(x.strip() for x in cur_lines):
        raw_sections.append({
            "title": cur_title or (base_title if not raw_sections else f"{base_title} {len(raw_sections) + 1}"),
            "kind": cur_kind or "sub",
            "text": "\n".join(cur_lines).strip(),
        })

    if not raw_sections:
        return _split_large_chapter(base_title, clean, max_chars=limit)

    # איחוד מקטעים קטנים סמוכים (כל עוד לא חוצים כותרת פרק ראשית ולא עוברים את max_chars)
    merged_sections: List[Dict[str, str]] = []
    for sec in raw_sections:
        sec_txt = sec["text"]
        if (
            merged_sections
            and sec.get("kind") != "major"
            and len(merged_sections[-1]["text"]) < floor
            and len(merged_sections[-1]["text"]) + len(sec_txt) + 2 <= limit
        ):
            merged_sections[-1]["text"] = f"{merged_sections[-1]['text']}\n\n{sec_txt}".strip()
        else:
            merged_sections.append({"title": sec["title"], "text": sec_txt})

    segments: List[Dict[str, str]] = []
    for idx, sec in enumerate(merged_sections, 1):
        sec_title = sec["title"] or f"{base_title} {idx}"
        segments.extend(_split_large_chapter(sec_title, sec["text"], max_chars=limit))

    return segments or [{"title": base_title, "text": clean}]


def _strip_part_suffix(title: str) -> str:
    return re.sub(r"\s*\(חלק\s+\d+\)\s*$", "", (title or "").strip()).strip()


def resegment_book(
    book: Dict[str, Any],
    *,
    preset: str = "balanced",
    max_chars: Optional[int] = None,
    min_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """
    מפצל מחדש ספר קיים למקטעים לפי רמת הפירוט המבוקשת ('compact' / 'balanced' / 'chapter' / מותאם אישית),
    תוך שמירה על כותרות נושאיות, גבולות פסקאות ומשפטים וללא איבוד אף אות מהמקור.
    """
    from copy import deepcopy

    preset_cfg = SEGMENT_PRESETS.get(preset, SEGMENT_PRESETS["balanced"])
    eff_max = max(600, int(max_chars or preset_cfg["max_chars"]))
    eff_min = max(200, int(min_chars if min_chars is not None else preset_cfg["min_chars"]))

    old_chapters = list((book or {}).get("chapters") or [])
    if not old_chapters:
        return deepcopy(book)

    # איחוד תתי-חלקים שנוצרו מפיצול קודם של אותו פרק ("... (חלק 1)", "... (חלק 2)")
    grouped_blocks: List[Dict[str, str]] = []
    for ch in old_chapters:
        t = (ch.get("title") or "").strip()
        txt = (ch.get("text") or "").strip()
        if not txt:
            continue
        base_t = _strip_part_suffix(t)
        is_part = "(חלק " in t
        if grouped_blocks and is_part and _strip_part_suffix(grouped_blocks[-1]["title"]) == base_t:
            grouped_blocks[-1]["text"] = f"{grouped_blocks[-1]['text']}\n\n{txt}".strip()
        else:
            grouped_blocks.append({"title": base_t or t or "מקטע", "text": txt})

    new_chapters: List[Dict[str, str]] = []
    for idx, blk in enumerate(grouped_blocks, 1):
        new_chapters.extend(
            segment_text_smartly(
                blk["text"],
                max_chars=eff_max,
                min_chars=eff_min,
                base_title=blk["title"] or f"מקטע {idx}",
            )
        )

    updated = deepcopy(book)
    updated["chapters"] = new_chapters or old_chapters
    updated["segment_preset"] = preset if max_chars is None else "custom"
    updated["segment_max_chars"] = eff_max
    return updated


def _chapters_from_full_text(
    full_text: str,
    max_chars: int = DEFAULT_SEGMENT_CHARS,
    min_chars: int = 1000,
) -> List[Dict[str, str]]:
    """מזהה פרקים ונושאים בטקסט ומפצל למקטעים נוחים לפי הצורך והעניין."""
    return segment_text_smartly(
        full_text,
        max_chars=max_chars,
        min_chars=min_chars,
        base_title="פרק א׳",
    )


def _load_pdf_safe(
    file_path: str,
    progress_cb: Optional[Callable[[str], None]] = None,
    max_chars: int = DEFAULT_SEGMENT_CHARS,
    min_chars: int = 1000,
    pages_per_chunk: int = PAGES_PER_CHUNK,
) -> Dict[str, Any]:
    """
    קורא קובץ PDF בצורה עמידה לספרים גדולים:
    - מגדיל גבול רקורסיה עבור PDF עמוקים
    - מתעד במדויק כל עמוד שלא ניתן לחלץ או שדורש OCR (ללא דילוג שקט!)
    - מחלק למקטעים חכמים לפי כותרות/פסקאות/גודל רצוי תוך תיוג טווח העמודים
    """
    import PyPDF2

    old_limit = sys.getrecursionlimit()
    if old_limit < 5000:
        sys.setrecursionlimit(5000)

    chapters: List[Dict[str, str]] = []
    empty_or_ocr_pages: List[int] = []
    failed_pages: List[Dict[str, Any]] = []
    extracted_pages: List[int] = []

    eff_max = max(800, int(max_chars or DEFAULT_SEGMENT_CHARS))
    flush_chars = max(eff_max * 2, 12000)

    def _flush_page_buffer(buf_pages: List[Tuple[int, str]]) -> None:
        if not buf_pages:
            return
        start_p = buf_pages[0][0]
        end_p = buf_pages[-1][0]
        combined = "\n\n".join(txt for _, txt in buf_pages)
        page_lbl = f"עמוד {start_p}" if start_p == end_p else f"עמודים {start_p}–{end_p}"
        segs = segment_text_smartly(
            combined,
            max_chars=eff_max,
            min_chars=min_chars,
            base_title=page_lbl,
        )
        for s in segs:
            t = s["title"]
            if page_lbl not in t and "עמוד" not in t:
                s["title"] = f"{t[:44]} ({page_lbl})"
        chapters.extend(segs)

    try:
        with open(file_path, "rb") as f:
            reader = PyPDF2.PdfReader(f, strict=False)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    raise RuntimeError("קובץ ה-PDF מוגן בסיסמה ואינו ניתן לקריאה.")

            total_pages = len(reader.pages)
            if total_pages == 0:
                raise RuntimeError("קובץ ה-PDF אינו מכיל עמודים.")

            chunk_pages: List[Tuple[int, str]] = []

            for idx in range(total_pages):
                page_num = idx + 1
                if progress_cb and (page_num == 1 or page_num % 10 == 0 or page_num == total_pages):
                    progress_cb(f"טוען PDF: עמוד {page_num} מתוך {total_pages}...")

                page_txt = ""
                try:
                    page_txt = (reader.pages[idx].extract_text() or "").strip()
                except Exception as exc:
                    failed_pages.append({"page": page_num, "error": str(exc)})
                    page_txt = ""

                if page_txt:
                    extracted_pages.append(page_num)
                    chunk_pages.append((page_num, page_txt))
                elif not any(fp["page"] == page_num for fp in failed_pages):
                    empty_or_ocr_pages.append(page_num)

                current_chars = sum(len(t) for _, t in chunk_pages)
                reached_flush = (
                    (len(chunk_pages) >= max(4, pages_per_chunk))
                    or current_chars >= flush_chars
                    or page_num == total_pages
                )
                if reached_flush and chunk_pages:
                    _flush_page_buffer(chunk_pages)
                    chunk_pages = []

            if len(extracted_pages) == 0:
                chapters = [{
                    "title": "ספר סרוק / ללא שכבת טקסט (נדרש OCR)",
                    "text": (
                        "לא נמצא טקסט ניתן לחילוץ באף עמוד בקובץ ה-PDF.\n"
                        "נדרש OCR לקובץ לפני עיבוד, תרגום או הפקת ספר קולי מלא."
                    ),
                }]

        missing_parts: List[str] = []
        if empty_or_ocr_pages:
            sample = ", ".join(str(p) for p in empty_or_ocr_pages[:15])
            suffix = f" (+עוד {len(empty_or_ocr_pages) - 15})" if len(empty_or_ocr_pages) > 15 else ""
            missing_parts.append(f"עמודים ללא שכבת טקסט (נדרש OCR): {sample}{suffix}")
        if failed_pages:
            sample_f = ", ".join(str(fp["page"]) for fp in failed_pages[:15])
            suffix_f = f" (+עוד {len(failed_pages) - 15})" if len(failed_pages) > 15 else ""
            missing_parts.append(f"עמודים שנכשלו בחילוץ: {sample_f}{suffix_f}")

        is_complete = (len(empty_or_ocr_pages) == 0 and len(failed_pages) == 0 and len(extracted_pages) == total_pages)
        coverage = {
            "source_type": "pdf",
            "total_pages": total_pages,
            "extracted_page_count": len(extracted_pages),
            "extracted_pages": extracted_pages,
            "empty_or_ocr_pages": empty_or_ocr_pages,
            "failed_pages": failed_pages,
            "is_complete_extraction": is_complete,
            "warning_message": (
                " | ".join(missing_parts)
                if missing_parts
                else ""
            ),
        }
        return {"chapters": chapters, "pdf_coverage": coverage}
    finally:
        try:
            sys.setrecursionlimit(old_limit)
        except Exception:
            pass


def validate_book_completeness(
    book: Dict[str, Any],
    allow_partial_pdf: bool = False,
) -> Dict[str, Any]:
    """
    מאמת שכל עמודי ה-PDF (אם זהו PDF) חולצו בהצלחה ושהספר מכיל פרקים ומשפטים קריאים.
    כאשר `allow_partial_pdf=True` וקיימים עמודי טקסט חילוצים, מאפשר להמשיך בעיבוד העמודים שחולצו
    תוך החזרת אזהרה ברורה על העמודים הריקים/סרוקים.
    """
    chapters = (book or {}).get("chapters") or []
    cov = (book or {}).get("pdf_coverage")
    warning = ""
    if isinstance(cov, dict) and cov.get("source_type") == "pdf":
        if not cov.get("is_complete_extraction", False):
            warn = cov.get("warning_message") or "חלק מעמודי ה-PDF לא חולצו או דורשים OCR."
            extracted_cnt = int(cov.get("extracted_page_count") or 0)
            if not allow_partial_pdf or extracted_cnt <= 0:
                return {
                    "is_complete": False,
                    "reason": warn,
                    "pdf_coverage": cov,
                    "chapter_count": len(chapters),
                }
            warning = warn
    if not chapters:
        return {"is_complete": False, "reason": "הספר אינו מכיל פרקים לעיבוד.", "chapter_count": 0}
    for idx, ch in enumerate(chapters):
        txt = (ch.get("text") or "").strip()
        if not txt:
            return {
                "is_complete": False,
                "reason": f"פרק {idx + 1} ('{ch.get('title', '')}') ריק מטקסט.",
                "chapter_count": len(chapters),
            }
    return {
        "is_complete": True,
        "reason": "",
        "warning": warning,
        "chapter_count": len(chapters),
        "pdf_coverage": cov,
    }


def load_file_content(
    file_path: str,
    progress_cb: Optional[Callable[[str], None]] = None,
    segment_preset: str = "balanced",
    max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """טוען קובץ טקסט, PDF או Word בצורה בטוחה (כולל ספרים גדולים) ומפצל למקטעים נוחים."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"הקובץ אינו קיים: {file_path}")

    preset_cfg = SEGMENT_PRESETS.get(segment_preset, SEGMENT_PRESETS["balanced"])
    eff_max = max(600, int(max_chars or preset_cfg["max_chars"]))
    eff_min = int(preset_cfg["min_chars"])
    eff_pages = int(preset_cfg["pages_per_chunk"])

    ext = os.path.splitext(file_path)[1].lower()
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    chapters: List[Dict[str, str]] = []
    pdf_coverage: Optional[Dict[str, Any]] = None

    if ext in [".txt", ".md"]:
        if progress_cb:
            progress_cb("טוען קובץ טקסט...")
        full_text = ""
        for enc in ["utf-8", "utf-8-sig", "windows-1255", "iso-8859-8"]:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    full_text = f.read()
                break
            except UnicodeDecodeError:
                continue
        if not full_text:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                full_text = f.read()
        chapters = _chapters_from_full_text(full_text, max_chars=eff_max, min_chars=eff_min)

    elif ext == ".pdf":
        pdf_res = _load_pdf_safe(
            file_path,
            progress_cb=progress_cb,
            max_chars=eff_max,
            min_chars=eff_min,
            pages_per_chunk=eff_pages,
        )
        chapters = pdf_res["chapters"]
        pdf_coverage = pdf_res["pdf_coverage"]

    elif ext == ".docx":
        if progress_cb:
            progress_cb("טוען מסמך Word...")
        import docx
        doc = docx.Document(file_path)
        full_text = "\n".join([p.text for p in doc.paragraphs if p.text])
        chapters = _chapters_from_full_text(full_text, max_chars=eff_max, min_chars=eff_min)

    else:
        raise ValueError(f"סוג קובץ אינו נתמך: {ext}")

    desc = f"קובץ: {file_path} ({len(chapters)} מקטעים/פרקים)"
    if pdf_coverage and not pdf_coverage.get("is_complete_extraction"):
        desc += f" — חלקי/נדרש OCR ({pdf_coverage.get('extracted_page_count')}/{pdf_coverage.get('total_pages')} עמודים)"

    result: Dict[str, Any] = {
        "id": f"imported_{os.path.basename(file_path)}",
        "title": base_name,
        "author": "מסמך שהועלה",
        "description": desc,
        "segment_preset": segment_preset if max_chars is None else "custom",
        "segment_max_chars": eff_max,
        "chapters": chapters,
    }
    if pdf_coverage is not None:
        result["pdf_coverage"] = pdf_coverage
    return result
