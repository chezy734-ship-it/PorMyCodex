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
from typing import List, Dict, Any, Callable, Optional

MAX_CHAPTER_CHARS = 12000
PAGES_PER_CHUNK = 12

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


def _split_large_chapter(title: str, text: str, max_chars: int = MAX_CHAPTER_CHARS) -> List[Dict[str, str]]:
    """מפצל פרק ענק לתתי-חלקים נוחים כדי לשמור על זיכרון נמוך ומהירות תצוגה."""
    clean = text.strip()
    if len(clean) <= max_chars:
        return [{"title": title[:70], "text": clean}]

    parts: List[Dict[str, str]] = []
    paragraphs = [p.strip() for p in clean.split("\n") if p.strip()]
    buf: List[str] = []
    buf_len = 0
    part_idx = 1

    for para in paragraphs:
        if buf_len + len(para) + 1 > max_chars and buf:
            parts.append({
                "title": f"{title[:45]} (חלק {part_idx})",
                "text": "\n".join(buf),
            })
            part_idx += 1
            buf = [para]
            buf_len = len(para)
        else:
            buf.append(para)
            buf_len += len(para) + 1

    if buf:
        parts.append({
            "title": f"{title[:45]} (חלק {part_idx})" if part_idx > 1 else title[:70],
            "text": "\n".join(buf),
        })

    return parts


def _chapters_from_full_text(full_text: str) -> List[Dict[str, str]]:
    """מזהה פרקים בטקסט ומפצל פרקים ארוכים מדי אוטומטית."""
    clean = (full_text or "").strip()
    if not clean:
        return [{"title": "פרק א׳ (ריק)", "text": "לא נמצא טקסט לקריאה בקובץ."}]

    chapter_splits = re.split(
        r'\n(?=(?:פרק\s+[א-ת0-9"\'׳״]+|חלק\s+[א-ת0-9]+|Chapter\s+\d+|PART\s+\d+))',
        clean,
        flags=re.IGNORECASE,
    )

    chapters: List[Dict[str, str]] = []
    if len(chapter_splits) > 1:
        for idx, ch_text in enumerate(chapter_splits, 1):
            chunk = ch_text.strip()
            if not chunk:
                continue
            lines = [l.strip() for l in chunk.split('\n') if l.strip()]
            title = lines[0][:65] if lines else f"פרק {idx}"
            chapters.extend(_split_large_chapter(title, chunk))
    else:
        chapters.extend(_split_large_chapter("פרק א׳", clean))

    return chapters or [{"title": "פרק א׳", "text": clean}]


def _load_pdf_safe(
    file_path: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    קורא קובץ PDF בצורה עמידה לספרים גדולים:
    - מגדיל גבול רקורסיה עבור PDF עמוקים
    - מתעד במדויק כל עמוד שלא ניתן לחלץ או שדורש OCR (ללא דילוג שקט!)
    - מחלק לקבוצות עמודים/פרקים נוחות אוטומטית בלי חיתוך ספרים גדולים
    """
    import PyPDF2

    old_limit = sys.getrecursionlimit()
    if old_limit < 5000:
        sys.setrecursionlimit(5000)

    chapters: List[Dict[str, str]] = []
    empty_or_ocr_pages: List[int] = []
    failed_pages: List[Dict[str, Any]] = []
    extracted_pages: List[int] = []

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

            chunk_pages_text: List[str] = []
            chunk_start_page = 1

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
                    chunk_pages_text.append(page_txt)
                elif not any(fp["page"] == page_num for fp in failed_pages):
                    empty_or_ocr_pages.append(page_num)

                current_chars = sum(len(t) for t in chunk_pages_text)
                reached_chunk = (
                    (page_num - chunk_start_page + 1 >= PAGES_PER_CHUNK)
                    or current_chars >= MAX_CHAPTER_CHARS
                    or page_num == total_pages
                )

                if reached_chunk and chunk_pages_text:
                    combined = "\n\n".join(chunk_pages_text)
                    title = f"עמודים {chunk_start_page}–{page_num}"
                    chapters.extend(_split_large_chapter(title, combined))
                    chunk_pages_text = []
                    chunk_start_page = page_num + 1
                elif reached_chunk and not chunk_pages_text:
                    chunk_start_page = page_num + 1

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


def validate_book_completeness(book: Dict[str, Any]) -> Dict[str, Any]:
    """
    מאמת שכל עמודי ה-PDF (אם זהו PDF) חולצו בהצלחה ושהספר מכיל פרקים ומשפטים קריאים.
    """
    chapters = (book or {}).get("chapters") or []
    cov = (book or {}).get("pdf_coverage")
    if isinstance(cov, dict) and cov.get("source_type") == "pdf":
        if not cov.get("is_complete_extraction", False):
            warn = cov.get("warning_message") or "חלק מעמודי ה-PDF לא חולצו או דורשים OCR."
            return {
                "is_complete": False,
                "reason": warn,
                "pdf_coverage": cov,
                "chapter_count": len(chapters),
            }
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
    return {"is_complete": True, "reason": "", "chapter_count": len(chapters), "pdf_coverage": cov}


def load_file_content(
    file_path: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """טוען קובץ טקסט, PDF או Word בצורה בטוחה (כולל ספרים גדולים) ומחזיר מבנה ספר עם פרקים ודוח כיסוי."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"הקובץ אינו קיים: {file_path}")

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
        chapters = _chapters_from_full_text(full_text)

    elif ext == ".pdf":
        pdf_res = _load_pdf_safe(file_path, progress_cb=progress_cb)
        chapters = pdf_res["chapters"]
        pdf_coverage = pdf_res["pdf_coverage"]

    elif ext == ".docx":
        if progress_cb:
            progress_cb("טוען מסמך Word...")
        import docx
        doc = docx.Document(file_path)
        full_text = "\n".join([p.text for p in doc.paragraphs if p.text])
        chapters = _chapters_from_full_text(full_text)

    else:
        raise ValueError(f"סוג קובץ אינו נתמך: {ext}")

    desc = f"קובץ: {file_path} ({len(chapters)} חלקים/פרקים)"
    if pdf_coverage and not pdf_coverage.get("is_complete_extraction"):
        desc += f" — חלקי/נדרש OCR ({pdf_coverage.get('extracted_page_count')}/{pdf_coverage.get('total_pages')} עמודים)"

    result: Dict[str, Any] = {
        "id": f"imported_{os.path.basename(file_path)}",
        "title": base_name,
        "author": "מסמך שהועלה",
        "description": desc,
        "chapters": chapters,
    }
    if pdf_coverage is not None:
        result["pdf_coverage"] = pdf_coverage
    return result
