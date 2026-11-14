# -*- coding: utf-8 -*-
"""
מנוע חיפוש והעשרת ידע מוויקיפדיה (Wikipedia Knowledge Grounding) עבור PorMyCodex.
שולף תקצירים מאומתים מוויקיפדיה העברית (he.wikipedia.org) והאנגלית (en.wikipedia.org),
ומאפשר למודל ה-AI להרחיב ביאורים ותסריטים על בסיס מידע אנציקלופדי אמיתי.
"""

import re
from typing import List, Dict, Any
from urllib.parse import quote
try:
    import requests
except ImportError:
    requests = None  # type: ignore

from settings_manager import load_settings, get_active_provider_config
from ai_providers import chat_completion, get_provider_label

WIKI_HEADERS = {
    "User-Agent": "PorMyCodex/1.0 (https://github.com/chezy734-ship-it/PorMyCodex)"
}


def _has_hebrew(text: str) -> bool:
    return bool(re.search(r"[\u0590-\u05FF]", text or ""))


def search_wikipedia_articles(query: str, lang: str = "auto", limit: int = 3, timeout: int = 12) -> List[Dict[str, str]]:
    """
    מחפש ערכים בוויקיפדיה ושולף את תקציר הפתיחה של הערכים הרלוונטיים ביותר.
    """
    clean_q = (query or "").strip()
    if not clean_q:
        return []

    if lang == "auto":
        langs_to_try = ["he", "en"] if _has_hebrew(clean_q) else ["en", "he"]
    else:
        langs_to_try = [lang]

    results: List[Dict[str, str]] = []
    if requests is None:
        return results
    for lg in langs_to_try:
        api_url = f"https://{lg}.wikipedia.org/w/api.php"
        try:
            search_resp = requests.get(
                api_url,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": clean_q,
                    "srlimit": limit,
                    "format": "json",
                    "utf8": 1,
                },
                headers=WIKI_HEADERS,
                timeout=timeout,
            )
            if not search_resp.ok:
                continue
            hits = search_resp.json().get("query", {}).get("search", [])
            if not hits:
                continue

            titles = [h.get("title", "") for h in hits if h.get("title")]
            if not titles:
                continue

            extract_resp = requests.get(
                api_url,
                params={
                    "action": "query",
                    "prop": "extracts",
                    "exintro": 1,
                    "explaintext": 1,
                    "exchars": 1500,
                    "redirects": 1,
                    "titles": "|".join(titles[:limit]),
                    "format": "json",
                    "utf8": 1,
                },
                headers=WIKI_HEADERS,
                timeout=timeout,
            )
            pages = extract_resp.json().get("query", {}).get("pages", {}) if extract_resp.ok else {}
            for page in pages.values():
                title = (page.get("title") or "").strip()
                extract = (page.get("extract") or "").strip()
                if not title or not extract:
                    continue
                results.append({
                    "title": title,
                    "lang": lg,
                    "extract": extract,
                    "url": f"https://{lg}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                })
            if results:
                break
        except Exception:
            continue

    return results[:limit]


def extract_search_terms(text: str, settings: Dict[str, Any] = None) -> List[str]:
    """
    מחלץ 2–3 מושגי מפתח עיקריים מהטקסט לחיפוש בוויקיפדיה.
    מנסה להשתמש ב-AI אם מוגדר מפתח, ואם לא — שולף ביטויים משמעותיים מקומית.
    """
    clean = (text or "").strip()
    if not clean:
        return []

    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    key = pconf.get("api_key", "")

    if key or pid == "custom":
        try:
            raw = chat_completion(
                provider_id=pid,
                api_key=key,
                model=pconf.get("model", ""),
                system_prompt=(
                    "חלץ מהקטע הבא עד 3 מושגים, שמות אישים, מקומות או נושאים מרכזיים שכדאי לחפש בוויקיפדיה כדי להעמיק הבנה. "
                    "החזר רק רשימת מושגים מופרדת בפסיקים, ללא מספור וללא הסברים."
                ),
                user_prompt=clean[:2500],
                base_url=pconf.get("base_url", ""),
                json_mode=False,
                temperature=0.1,
                max_tokens=150,
                timeout=20,
            )
            terms = [t.strip(" .•-\"'") for t in re.split(r"[,;\n]+", raw) if len(t.strip()) >= 2]
            if terms:
                return terms[:3]
        except Exception:
            pass

    # גיבוי מקומי ללא תלות ברשת: בחירת מילים ארוכות/שמות בולטים
    words = re.findall(r"[A-Za-z\u0590-\u05FF\"׳״]{3,}", clean[:1200])
    stop = {"אבל", "אשר", "הוא", "היא", "הם", "הן", "של", "על", "עם", "אל", "מן", "כל", "כדי", "כמו", "זה", "זו", "זאת", "אלו", "the", "and", "for", "that", "with"}
    candidates: List[str] = []
    for w in words:
        w_clean = w.strip('"\'')
        if w_clean.lower() not in stop and w_clean not in candidates and len(w_clean) >= 4:
            candidates.append(w_clean)
        if len(candidates) >= 3:
            break
    return candidates


def fetch_wikipedia_context_for_terms(
    terms_input: str,
    fallback_text: str = "",
    settings: Dict[str, Any] = None,
    lang: str = "auto",
) -> Dict[str, Any]:
    """
    מקבל מחרוזת מושגים (מופרדת בפסיקים) או טקסט פרק ומחזיר ערכי ויקיפדיה + טקסט הקשר מוכן להזנה למודל AI.
    """
    raw_terms = [t.strip() for t in re.split(r"[,;\n]+", terms_input or "") if t.strip()]
    if not raw_terms and fallback_text:
        raw_terms = extract_search_terms(fallback_text, settings=settings)

    articles: List[Dict[str, str]] = []
    seen_titles = set()
    for term in raw_terms[:4]:
        hits = search_wikipedia_articles(term, lang=lang, limit=2)
        for h in hits:
            if h["title"] not in seen_titles:
                seen_titles.add(h["title"])
                articles.append(h)

    if not articles:
        return {
            "terms": raw_terms,
            "articles": [],
            "context_text": "",
        }

    blocks = []
    for art in articles:
        blocks.append(f"### ויקיפדיה: {art['title']} ({art['lang']})\n{art['extract']}\nמקור: {art['url']}")

    return {
        "terms": raw_terms,
        "articles": articles,
        "context_text": "\n\n".join(blocks),
    }


def enrich_with_wikipedia_and_ai(
    query_terms: str,
    book_context: str = "",
    book_title: str = "",
    settings: Dict[str, Any] = None,
    lang: str = "auto",
) -> Dict[str, Any]:
    """
    מחפש בוויקיפדיה ולאחר מכן משתמש במודל ה-AI הפעיל כדי להרחיב, לבאר ולקשר את הידע לספר.
    """
    cfg = settings or load_settings()
    wiki_data = fetch_wikipedia_context_for_terms(
        query_terms,
        fallback_text=book_context,
        settings=cfg,
        lang=lang,
    )
    articles = wiki_data["articles"]

    if not articles:
        raise RuntimeError(
            f"לא נמצאו ערכים מתאימים בוויקיפדיה עבור: {', '.join(wiki_data['terms']) or query_terms}"
        )

    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    key = pconf.get("api_key", "")
    model = pconf.get("model", "")

    ai_expansion = ""
    if key or pid == "custom":
        try:
            sys_prompt = (
                "אתה חוקר ופרשן אינטלקטואלי בעברית בתוכנת PorMyCodex. "
                "סופק לך מידע אמיתי מוויקיפדיה וקטע מהספר. "
                "הרחב והסבר בעברית בהירה ומרתקת את הרקע ההיסטורי, המושגי או המדעי מתוך ערכי הוויקיפדיה, "
                "וקשר אותם להבנת הקטע מהספר. אל תמציא עובדות שלא נתמכות."
            )
            usr_prompt = (
                f"שם הספר: {book_title or 'ללא שם'}\n"
                f"מושגים שנחקרו: {', '.join(wiki_data['terms'])}\n\n"
                f"--- מידע מוויקיפדיה ---\n{wiki_data['context_text'][:5000]}\n\n"
                f"--- הקשר מתוך הספר ---\n{book_context[:2500]}"
            )
            ai_expansion = chat_completion(
                provider_id=pid,
                api_key=key,
                model=model,
                system_prompt=sys_prompt,
                user_prompt=usr_prompt,
                base_url=pconf.get("base_url", ""),
                json_mode=False,
                temperature=0.3,
            )
        except Exception as e:
            ai_expansion = f"*(הרחבת AI לא הושלמה: {e} — מוצג תקציר ויקיפדיה ישיר)*"
    else:
        ai_expansion = f"*(הגדר מפתח API עבור {get_provider_label(pid)} כדי לקבל גם סיכום והרחבה מותאמת מה-AI לצד ויקיפדיה)*"

    return {
        "terms": wiki_data["terms"],
        "articles": articles,
        "ai_expansion": ai_expansion,
    }
