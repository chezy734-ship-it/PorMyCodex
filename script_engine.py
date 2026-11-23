# -*- coding: utf-8 -*-
"""
מנוע יצירת תסריט AI (Script Studio) עבור PorMyCodex.
- מייצר תסריט מלא המכסה 100% מיחידות הטקסט (ללא חיתוך [:80] וללא דילוג שקט)
- מחלק לאצוות מוגבלות משפטים וגם תווים כדי למנוע Read Timeout בפרקים ארוכים
- תומך בהעשרת ידע מוויקיפדיה, ביטול מיידי (גם באמצע Retry/Backoff), ואימות שלמות מלא
"""

from typing import List, Dict, Any, Optional, Callable, Tuple
from settings_manager import load_settings, get_active_provider_config
from ai_providers import (
    AICallError,
    AICancelledError,
    chat_completion,
    extract_json_object,
    get_provider_label,
)
from book_loader import split_into_sentences


class ScriptCancelledError(RuntimeError):
    """נזרק כאשר יצירת התסריט בוטלה במפורש."""


SPEAKER_DISPLAY = {
    "teacher": "🎓 מורה",
    "student": "🙋 תלמיד",
    "narrator": "🎙️ מספר",
}

UNITS_PER_SCRIPT_BATCH = 18
MAX_CHARS_PER_SCRIPT_BATCH = 2200


def _check_cancelled(cancel_event: Any = None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ScriptCancelledError("שלב 'יצירת תסריט AI': הפעולה בוטלה על ידי המשתמש.")


def _build_script_batches(
    units: List[Dict[str, str]],
    max_units: int = UNITS_PER_SCRIPT_BATCH,
    max_chars: int = MAX_CHARS_PER_SCRIPT_BATCH,
) -> List[List[Dict[str, str]]]:
    batches: List[List[Dict[str, str]]] = []
    cur: List[Dict[str, str]] = []
    cur_chars = 0
    for u in units:
        u_len = len(u["text"])
        if cur and (len(cur) >= max_units or cur_chars + u_len > max_chars):
            batches.append(cur)
            cur = [u]
            cur_chars = u_len
        else:
            cur.append(u)
            cur_chars += u_len
    if cur:
        batches.append(cur)
    return batches


def _call_chat(caller: Callable[..., str], **kwargs) -> str:
    try:
        return caller(**kwargs)
    except TypeError:
        compact_keys = (
            "provider_id",
            "api_key",
            "model",
            "system_prompt",
            "user_prompt",
            "base_url",
            "json_mode",
            "temperature",
        )
        return caller(**{k: v for k, v in kwargs.items() if k in compact_keys})


def build_script_batches_spec(
    text: str,
    units_per_batch: int = UNITS_PER_SCRIPT_BATCH,
    max_chars_per_batch: int = MAX_CHARS_PER_SCRIPT_BATCH,
) -> List[List[Dict[str, str]]]:
    clean_text = (text or "").strip()
    if not clean_text:
        return []
    sentences = split_into_sentences(clean_text)
    if not sentences:
        return []
    all_units = [{"unitId": f"u{i + 1}", "text": s} for i, s in enumerate(sentences)]
    return _build_script_batches(all_units, max_units=units_per_batch, max_chars=max_chars_per_batch)


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
    units_per_batch: int = UNITS_PER_SCRIPT_BATCH,
    max_chars_per_batch: int = MAX_CHARS_PER_SCRIPT_BATCH,
    existing_batches: Optional[Dict[int, Dict[str, Any]]] = None,
    batch_progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    יוצר תסריט מובנה המכסה 100% ממשפטי הפרק באצוות בטוחות מוגבלות זיכרון וזמן.
    - מדלג על אצוות מאומתות שכבר נשמרו (`existing_batches`) ללא קריאות AI כפולות
    - מדווח מיד לאחר כל אצווה דרך `batch_progress_cb` לשמירה אטומית הדרגתית
    - מאחד אצוות לפי הסדר (ללא כפילויות) ומאמת כיסוי מלא ורציף
    """
    _check_cancelled(cancel_event)

    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("שלב 'יצירת תסריט AI': אין טקסט בפרק ליצירת תסריט.")

    cfg = settings or load_settings()
    pconf = get_active_provider_config(cfg)
    pid = pconf["provider_id"]
    model = pconf.get("model", "")
    api_key = pconf.get("api_key", "")
    caller = chat_fn or chat_completion

    if chat_fn is None and not api_key and pid != "custom":
        raise ValueError(
            f"שלב 'יצירת תסריט AI': לא הוגדר מפתח API עבור {get_provider_label(pid)}.\n"
            "לחץ בסרגל הכלים על '⚙️ הגדרות AI ומודלים' כדי להזין מפתח ולבחור מודל."
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
                    + wiki_data["context_text"][:3500]
                )
        except Exception:
            wiki_articles = []

    sentences = split_into_sentences(clean_text)
    if not sentences:
        raise ValueError("שלב 'יצירת תסריט AI': לא נמצאו משפטים תקינים ליצירת תסריט.")

    all_units = [{"unitId": f"u{i + 1}", "text": s} for i, s in enumerate(sentences)]
    all_unit_ids_set = {u["unitId"] for u in all_units}

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

    batches = _build_script_batches(all_units, max_units=units_per_batch, max_chars=max_chars_per_batch)
    total_batches = len(batches)
    saved_batches = existing_batches or {}
    batch_results: Dict[int, List[Dict[str, Any]]] = {}
    covered_units = set()
    script_title = ""
    current_prov_label = f"{get_provider_label(pid)} ({model})"

    for b_idx0, batch_units in enumerate(batches):
        batch_num = b_idx0 + 1
        expected_ids = [u["unitId"] for u in batch_units]
        expected_set = set(expected_ids)
        first_uid = expected_ids[0] if expected_ids else ""

        try:
            _check_cancelled(cancel_event)
        except ScriptCancelledError:
            if batch_progress_cb is not None:
                batch_progress_cb({
                    "event": "batch_cancelled",
                    "batch_index": b_idx0,
                    "unit_id": first_uid,
                })
            raise

        cached_b = saved_batches.get(b_idx0)
        if cached_b and cached_b.get("lines"):
            reused_lines: List[Dict[str, Any]] = []
            for ln in cached_b["lines"]:
                if isinstance(ln, dict) and (ln.get("text") or "").strip():
                    uid = str(ln.get("unitId") or "").strip()
                    if uid in expected_set:
                        covered_units.add(uid)
                        reused_lines.append(ln)
            if reused_lines and expected_set.issubset(covered_units):
                if not script_title and cached_b.get("title"):
                    script_title = str(cached_b.get("title")).strip()
                batch_results[b_idx0] = reused_lines
                if progress_cb:
                    progress_cb(
                        f"תסריט AI: משתמש בחלק שמור מאומת ({batch_num}/{total_batches})..."
                    )
                continue

        if progress_cb:
            progress_cb(
                f"יוצר תסריט AI (חלק {batch_num} מתוך {total_batches}, {len(batch_units)} משפטים)..."
            )

        def _call_and_parse_subbatch(sub_units: List[Dict[str, str]], label_suffix: str = "") -> Dict[str, Any]:
            sub_ids = [u["unitId"] for u in sub_units]
            sub_set = set(sub_ids)
            user_prompt = (
                f"שם הספר: {book_title or 'ללא שם'}\n"
                f"מצב תסריט: {'לימודי (מורה ותלמיד)' if mode == 'educational' else 'דרמטי (קריינות)'}\n"
                f"חלק {batch_num} מתוך {total_batches}{label_suffix}\n"
                "יחידות המקור:\n"
                + "\n".join(f"[{u['unitId']}] {u['text']}" for u in sub_units)
                + (wiki_section if batch_num == 1 and not label_suffix else "")
            )
            raw_response = _call_chat(
                caller,
                provider_id=pid,
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                base_url=pconf.get("base_url", ""),
                json_mode=True,
                temperature=0.35,
                connect_timeout=10.0,
                read_timeout=45.0,
                total_deadline=135.0,
                max_retries=2,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                stage=f"יצירת תסריט AI (חלק {batch_num}/{total_batches})",
            )
            _check_cancelled(cancel_event)
            parsed = extract_json_object(raw_response)
            b_title_local = str(parsed.get("title") or "").strip()
            raw_lines = parsed.get("lines", [])
            if not isinstance(raw_lines, list) or not raw_lines:
                raise RuntimeError(
                    f"שלב 'יצירת תסריט AI': המודל לא החזיר שורות תסריט תקינות עבור חלק {batch_num} מתוך {total_batches}."
                )

            sub_valid_lines: List[Dict[str, Any]] = []
            sub_covered = set()
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
                if uid and uid in all_unit_ids_set and uid not in sub_set:
                    continue
                if not uid or uid not in sub_set:
                    uid = sub_ids[min(idx, len(sub_ids) - 1)]
                sub_covered.add(uid)
                sub_valid_lines.append({
                    "unitId": uid,
                    "speakerRole": role,
                    "speakerLabel": SPEAKER_DISPLAY.get(role, "🎙️ מספר"),
                    "style": str(line.get("style") or "רגוע").strip(),
                    "text": text_val,
                    "needsReview": bool(line.get("needsReview", False)),
                })

            if not sub_valid_lines:
                raise RuntimeError(f"שלב 'יצירת תסריט AI': התסריט שהתקבל בחלק {batch_num} ריק.")

            missing = [uid for uid in sub_ids if uid not in sub_covered]
            if missing and len(sub_valid_lines) >= len(sub_units):
                for uid in missing:
                    sub_covered.add(uid)
                missing = []

            return {
                "title": b_title_local,
                "lines": sub_valid_lines,
                "covered": sub_covered,
                "missing": missing,
            }

        def _run_adaptive_subbatch(sub_units: List[Dict[str, str]], depth: int = 0) -> Tuple[str, List[Dict[str, Any]]]:
            _check_cancelled(cancel_event)
            try:
                res_sub = _call_and_parse_subbatch(
                    sub_units,
                    label_suffix=f" (תת-מקטע {len(sub_units)} יח')" if depth > 0 else "",
                )
            except AICancelledError:
                raise
            except ScriptCancelledError:
                raise
            except Exception as err:
                cat = str(getattr(err, "category", "") or "").lower()
                is_hard = bool(
                    getattr(err, "is_blocked", False)
                    or cat in ("blocked", "auth", "model_not_found")
                )
                # תקלת timeout/רשת מפוצלת רק באצוות גדולות (>=6 יחידות), כדי שמקטע קטן שנכשל ידווח ככשל אמיתי
                min_split_size = 6 if cat in ("timeout", "network", "quota", "server") else 2
                if is_hard or len(sub_units) < min_split_size or depth >= 4:
                    raise
                mid = len(sub_units) // 2
                if progress_cb:
                    progress_cb(
                        f"תסריט AI (חלק {batch_num}/{total_batches}): מפצל מקטע של {len(sub_units)} יחידות לתתי-מקטעים קטנים יותר..."
                    )
                t1, l1 = _run_adaptive_subbatch(sub_units[:mid], depth=depth + 1)
                t2, l2 = _run_adaptive_subbatch(sub_units[mid:], depth=depth + 1)
                return (t1 or t2), (l1 + l2)

            missing = res_sub["missing"]
            lines_out = list(res_sub["lines"])
            b_title_out = res_sub["title"]
            if not missing:
                return b_title_out, lines_out

            # אם נותרו רק מעט יחידות חסרות — נשלים אותן ממוקדת או נפצל לחצי
            if len(missing) < len(sub_units):
                missing_units = [u for u in sub_units if u["unitId"] in set(missing)]
                if progress_cb:
                    progress_cb(
                        f"תסריט AI (חלק {batch_num}/{total_batches}): משלים {len(missing_units)} יחידות שחסרו במענה..."
                    )
                try:
                    t_rec, l_rec = _run_adaptive_subbatch(missing_units, depth=depth + 1)
                    if not b_title_out and t_rec:
                        b_title_out = t_rec
                    # מיזוג לפי סדר היחידות המקורי
                    by_uid: Dict[str, List[Dict[str, Any]]] = {}
                    for ln in lines_out + l_rec:
                        by_uid.setdefault(ln["unitId"], []).append(ln)
                    merged: List[Dict[str, Any]] = []
                    for u in sub_units:
                        merged.extend(by_uid.get(u["unitId"], []))
                    if all(u["unitId"] in by_uid for u in sub_units):
                        return b_title_out, merged
                except (AICancelledError, ScriptCancelledError):
                    raise
                except Exception:
                    pass

            if len(sub_units) > 1 and depth < 4:
                mid = len(sub_units) // 2
                t1, l1 = _run_adaptive_subbatch(sub_units[:mid], depth=depth + 1)
                t2, l2 = _run_adaptive_subbatch(sub_units[mid:], depth=depth + 1)
                return (t1 or t2), (l1 + l2)

            raise RuntimeError(
                f"שלב 'יצירת תסריט AI': המודל החזיר תסריט חלקי בחלק {batch_num} (חסרות יחידות: {', '.join(missing[:5])})."
            )

        try:
            b_title, batch_valid_lines = _run_adaptive_subbatch(batch_units, depth=0)
            if not script_title and b_title:
                script_title = b_title
            for ln in batch_valid_lines:
                covered_units.add(ln["unitId"])
        except AICancelledError as e:
            if batch_progress_cb is not None:
                batch_progress_cb({
                    "event": "batch_cancelled",
                    "batch_index": b_idx0,
                    "unit_id": first_uid,
                })
            raise ScriptCancelledError(str(e)) from e
        except ScriptCancelledError:
            if batch_progress_cb is not None:
                batch_progress_cb({
                    "event": "batch_cancelled",
                    "batch_index": b_idx0,
                    "unit_id": first_uid,
                })
            raise
        except Exception as exc:
            setattr(exc, "batch_index", b_idx0)
            setattr(exc, "unit_id", first_uid)
            if batch_progress_cb is not None:
                batch_progress_cb({
                    "event": "batch_failed",
                    "batch_index": b_idx0,
                    "unit_id": first_uid,
                    "error": str(exc),
                    "provider_label": current_prov_label,
                })
            raise

        batch_results[b_idx0] = batch_valid_lines
        if batch_progress_cb is not None:
            batch_progress_cb({
                "event": "batch_done",
                "batch_index": b_idx0,
                "unit_ids": expected_ids,
                "lines": batch_valid_lines,
                "title": script_title or f"תסריט - {book_title}",
                "wiki_articles": wiki_articles if b_idx0 == 0 else [],
                "provider_label": current_prov_label,
            })

    # מיזוג מסודר של כל האצוות לפי סדר batch_index (0..M-1) וללא כפילויות יחידות בין אצוות
    normalized_lines: List[Dict[str, Any]] = []
    seen_batch_uids = set()
    for b_idx0, batch_units in enumerate(batches):
        b_expected = {u["unitId"] for u in batch_units}
        for line in batch_results.get(b_idx0, []):
            uid = line["unitId"]
            if uid in b_expected and uid not in seen_batch_uids:
                normalized_lines.append(line)
        seen_batch_uids.update(b_expected)

    if not normalized_lines or len(covered_units) < len(all_units):
        raise RuntimeError("שלב 'יצירת תסריט AI': התסריט שהתקבל חלקי ואינו מכסה את כל משפטֵי הפרק.")

    return {
        "title": script_title or f"תסריט - {book_title}",
        "mode": mode,
        "provider_label": current_prov_label,
        "lines": normalized_lines,
        "wiki_articles": wiki_articles,
        "unit_count": len(all_units),
        "covered_units": sorted(covered_units, key=lambda x: int(x[1:]) if x[1:].isdigit() else 999999),
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
