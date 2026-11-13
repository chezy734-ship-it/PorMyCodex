# -*- coding: utf-8 -*-
"""
מודול לטעינה, חילוץ וחלוקת ספרים וטקסטים עבריים לפרקים ומשפטים.
תומך בקבצי TXT, MD, PDF, DOCX וספרי מופת מובנים.
"""

import os
import re
from typing import List, Dict, Any

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
הַדֶּרֶךְ הַיְשָׁרָה הִיא מִדָּה בֵּינוֹנִית שֶׁבְּכָל דֵּעָה וְדֵעָה מִכָּל הַדֵּעוֹת שֶׁיֵּשׁ לָאָדָם; וְהִיא הַדֵּעָה שֶׁהִיא רְחוֹקָה מִשְּׁתֵּי הַקְּצָווֹת רִחוּק שָׁוֶה.
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
    """מחלק טקסט עברי למשפטים לפי נקודות, סימני שאלה, קריאה וירידות שורה."""
    if not text:
        return []
    
    # חלוקה לפי סימני סיום משפט
    raw_parts = re.split(r'(?<=[.?!;:\n])\s+', text.strip())
    sentences = [p.strip() for p in raw_parts if p.strip()]
    return sentences if sentences else [text.strip()]


def load_file_content(file_path: str) -> Dict[str, Any]:
    """טוען קובץ טקסט, PDF או Word ומחזיר מבנה ספר עם פרקים."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"הקובץ אינו קיים: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    full_text = ""

    if ext in [".txt", ".md"]:
        for enc in ["utf-8", "utf-8-sig", "windows-1255", "iso-8859-8"]:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    full_text = f.read()
                break
            except UnicodeDecodeError:
                continue

    elif ext == ".pdf":
        try:
            import PyPDF2
            with open(file_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                pages = [page.extract_text() or "" for page in reader.pages]
                full_text = "\n\n".join(pages)
        except Exception as e:
            full_text = f"שגיאה בקריאת PDF: {e}"

    elif ext == ".docx":
        try:
            import docx
            doc = docx.Document(file_path)
            full_text = "\n".join([p.text for p in doc.paragraphs if p.text])
        except Exception as e:
            full_text = f"שגיאה בקריאת DOCX: {e}"

    else:
        raise ValueError(f"סוג קובץ אינו נתמך: {ext}")

    # חלוקה ראשונית לפרקים אם קיימים סמני פרק (למשל 'פרק א', 'פרק 1', 'Chapter')
    chapter_splits = re.split(r'\n(?=(?:פרק\s+[א-ת0-9]+|פרק\s+|Chapter\s+\d+))', full_text, flags=re.IGNORECASE)

    chapters = []
    if len(chapter_splits) > 1:
        for idx, ch_text in enumerate(chapter_splits, 1):
            lines = [l.strip() for l in ch_text.strip().split('\n') if l.strip()]
            title = lines[0] if lines else f"פרק {idx}"
            chapters.append({
                "title": title[:60],
                "text": ch_text.strip()
            })
    else:
        chapters.append({
            "title": "פרק א׳ (תוכן הקובץ)",
            "text": full_text.strip()
        })

    return {
        "id": f"imported_{os.path.basename(file_path)}",
        "title": base_name,
        "author": "מסמך שהועלה",
        "description": f"קובץ: {file_path}",
        "chapters": chapters
    }
