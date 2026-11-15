# -*- coding: utf-8 -*-
"""
שירות יצוא ותסריט נאכף (Enforced Export & Script Pipeline Service) עבור PorMyCodex.

תומך בשני היקפים מפורשים:
1. 'chapter' (הפרק הנוכחי בלבד — לעולם אינו מכונה 'הספר כולו')
2. 'book' (הספר כולו — עובד על כל פרקי הספר 0..K-1 לפי הסדר, בלי תלות בפרק המוצג כרגע)

סדר אכיפה קשיח (לפרק יחיד וגם לספר שלם):
  שלב 1: תרגום כל המשפטים הדרושים של כל הפרקים הנדרשים (לפני תסריט)
  שלב 2: תסריט AI מלא ועדכני של כל הפרקים הנדרשים (נשמר אטומית לכל פרק)
  [שער אימות קשיח: 0 קריאות TTS לפני ש-100% מתרגומי ותסריטי הספר מאומתים ועדכניים!]
  שלב 3: סינתזת TTS לכל פרק לקובץ מטמון ייעודי בדיסק (זיכרון חסום, תומך המשך אחרי ביטול/אתחול)
  שלב 4: אריזה אטומית (קובץ WAV/MP3 מאוחד בפענוח/קידוד תקינים, או ZIP/תיקייה ממוספרת + manifest.json)
"""

import os
from typing import Any, Callable, Dict, List, Optional

from ai_providers import ProviderCircuitBreaker, _sanitize_error_text
from audio_exporter import (
    ExportCancelledError,
    export_audiobook_file,
    package_book_audio,
)
from book_loader import split_into_sentences, validate_book_completeness
from library_store import (
    LibraryStore,
    compute_script_config_hash,
    compute_source_hash,
    compute_translation_config_hash,
    compute_tts_config_hash,
    compute_unit_source_hash,
    get_chapter_audio_cache_path,
)
from script_engine import (
    MAX_CHARS_PER_SCRIPT_BATCH,
    UNITS_PER_SCRIPT_BATCH,
    ScriptCancelledError,
    build_script_batches_spec,
    generate_narration_script,
)
from settings_manager import load_settings
from translator_engine import (
    TranslationCancelledError,
    TranslationProviderError,
    translate_required_sentences,
)


class PipelineError(RuntimeError):
    """שגיאת בסיס בשירות היצוא."""


class MissingArtifactError(PipelineError):
    """נזרק בעת ניסיון עקיפה כאשר תוצר נדרש (תרגום או תסריט) חסר."""


class StaleArtifactError(PipelineError):
    """נזרק בעת ניסיון שימוש בתוצר ישן לאחר שינוי מקור או הגדרות."""


class IncompleteArtifactError(PipelineError):
    """נזרק כאשר תוצר, חילוץ PDF או תסריט הם חלקיים בלבד."""


class PipelineCancelledError(PipelineError):
    """נזרק כאשר הצינור בוטל על ידי המשתמש."""


def _is_cancelled(cancel_event: Any = None) -> bool:
    return cancel_event is not None and bool(cancel_event.is_set())


def _raise_if_cancelled(cancel_event: Any = None, stage: str = "") -> None:
    if _is_cancelled(cancel_event):
        raise PipelineCancelledError(f"התהליך בוטל בשלב '{stage or 'לא ידוע'}'.")


def assess_chapter_pipeline(
    book_id: str,
    chapter_index: int,
    source_text: str,
    settings: Optional[Dict[str, Any]] = None,
    translation_opts: Optional[Dict[str, Any]] = None,
    script_opts: Optional[Dict[str, Any]] = None,
    tts_opts: Optional[Dict[str, Any]] = None,
    store: Optional[LibraryStore] = None,
) -> Dict[str, Any]:
    """בודק את עדכניות כל התוצרים השמורים עבור הפרק ומפרט מה תקף ומה חסר/התיישן."""
    cfg = settings or load_settings()
    st = store or LibraryStore()
    trans_cfg = cfg.get("translation", {})
    t_opts = translation_opts or {}
    s_opts = script_opts or {}
    a_opts = tts_opts or {}

    engine = (t_opts.get("engine") or trans_cfg.get("engine") or "google_free").strip()
    source_lang = (t_opts.get("source_lang") or trans_cfg.get("source_lang") or "auto").strip()
    target_lang = (t_opts.get("target_lang") or trans_cfg.get("target_lang") or "he").strip()

    mode = (s_opts.get("mode") or "educational").strip()
    custom_instruction = (s_opts.get("custom_instruction") or s_opts.get("instruction") or "").strip()
    use_wikipedia = bool(s_opts.get("use_wikipedia", False))
    wiki_terms = (s_opts.get("wiki_terms") or "").strip()

    output_format = (a_opts.get("output_format") or "mp3").lower().lstrip(".")
    default_voice = (a_opts.get("default_voice") or "edge_avri").strip()
    speed = float(a_opts.get("speed", 1.0))

    src_hash = compute_source_hash(source_text)
    trans_cfg_hash = compute_translation_config_hash(cfg, engine, source_lang, target_lang)
    script_cfg_hash = compute_script_config_hash(cfg, mode, custom_instruction, use_wikipedia, wiki_terms)
    tts_cfg_hash = compute_tts_config_hash(cfg, output_format, default_voice, speed)

    artifacts = st.get_chapter_artifacts(book_id, chapter_index)
    t_rec = artifacts.get("translation")
    s_rec = artifacts.get("script")
    c_rec = artifacts.get("tts_cache")
    e_rec = artifacts.get("last_export")

    t_reason = ""
    t_ready = False
    if not t_rec:
        t_reason = "לא קיים תרגום שמור לפרק זה."
    elif not t_rec.get("is_complete"):
        t_reason = "התרגום השמור חלקי בלבד."
    elif t_rec.get("source_hash") != src_hash:
        t_reason = "טקסט המקור השתנה מאז התרגום האחרון."
    elif t_rec.get("config_hash") != trans_cfg_hash:
        t_reason = "הגדרות התרגום המהותיות (שפות/הוראות) השתנו."
    elif not LibraryStore.is_translation_fresh(t_rec, src_hash, trans_cfg_hash):
        t_reason = "התרגום השמור אינו מכסה את כל המשפטים הדרושים."
    else:
        t_ready = True

    expected_trans_hash = (t_rec or {}).get("translated_hash", "") if t_ready else ""

    s_reason = ""
    s_ready = False
    if not t_ready:
        s_reason = f"ממתין לתרגום עדכני ({t_reason})"
    elif not s_rec:
        s_reason = "לא קיים תסריט שמור לפרק זה."
    elif not s_rec.get("is_complete"):
        s_reason = "התסריט השמור חלקי בלבד."
    elif s_rec.get("source_hash") != src_hash:
        s_reason = "טקסט המקור השתנה מאז יצירת התסריט."
    elif s_rec.get("translation_hash") != expected_trans_hash:
        s_reason = "התרגום עודכן מאז יצירת התסריט האחרון."
    elif s_rec.get("config_hash") != script_cfg_hash:
        s_reason = "הגדרות התסריט או ספק/מודל ה-AI השתנו."
    elif not LibraryStore.is_script_fresh(s_rec, src_hash, expected_trans_hash, script_cfg_hash):
        s_reason = "התסריט אינו מכסה את כל יחידות הפרק."
    else:
        s_ready = True

    tts_ready = bool(
        t_ready
        and s_ready
        and LibraryStore.is_chapter_tts_fresh(
            c_rec,
            expected_source_hash=src_hash,
            expected_translation_hash=expected_trans_hash,
            expected_script_config_hash=script_cfg_hash,
            expected_tts_config_hash=tts_cfg_hash,
        )
    )

    # אבחון יחידות הדרגתי (קריאה בלבד) להצגת יחידות שהושלמו/חסרות/נכשלו ונקודת המשך מדויקת
    src_sentences = split_into_sentences(source_text or "") if (source_text or "").strip() else []
    tp_view = (
        st.reconcile_translation_progress(
            book_id=book_id,
            chapter_index=chapter_index,
            source_sentences=src_sentences,
            config_hash=trans_cfg_hash,
            source_lang=source_lang,
            target_lang=target_lang,
            source_hash=src_hash,
            persist=False,
        )
        if src_sentences
        else {
            "total_units": 0,
            "completed_count": 0,
            "missing_count": 0,
            "failed_count": 0,
            "outdated_count": 0,
            "next_unit_index": 0,
            "next_unit_id": "",
            "status": "pending",
        }
    )

    raw_sp = artifacts.get("script_progress") if isinstance(artifacts.get("script_progress"), dict) else {}
    sp_summary = {
        "total_units": int(raw_sp.get("total_units") or (len(src_sentences) if t_ready else 0)),
        "completed_count": int(s_rec.get("unit_count", len(src_sentences))) if s_ready and s_rec else int(raw_sp.get("completed_count") or 0),
        "missing_count": 0 if s_ready else int(raw_sp.get("missing_count") or (len(src_sentences) if t_ready else 0)),
        "failed_count": 0 if s_ready else int(raw_sp.get("failed_count") or 0),
        "outdated_count": 0 if s_ready else int(raw_sp.get("outdated_count") or 0),
        "next_batch_index": int(raw_sp.get("total_batches", 0)) if s_ready else int(raw_sp.get("next_batch_index") or 0),
        "next_unit_index": int(raw_sp.get("total_units", 0)) if s_ready else int(raw_sp.get("next_unit_index") or 0),
        "next_unit_id": "" if s_ready else str(raw_sp.get("next_unit_id") or ""),
        "status": "completed" if s_ready else str(raw_sp.get("status") or "pending"),
    }

    has_partial_translation = bool(not t_ready and tp_view.get("completed_count", 0) > 0)
    has_partial_script = bool(not s_ready and sp_summary.get("completed_count", 0) > 0)

    return {
        "book_id": book_id,
        "chapter_index": int(chapter_index),
        "source_hash": src_hash,
        "translation_config_hash": trans_cfg_hash,
        "script_config_hash": script_cfg_hash,
        "tts_config_hash": tts_cfg_hash,
        "translation_params": {
            "engine": engine,
            "source_lang": source_lang,
            "target_lang": target_lang,
        },
        "script_params": {
            "mode": mode,
            "custom_instruction": custom_instruction,
            "use_wikipedia": use_wikipedia,
            "wiki_terms": wiki_terms,
        },
        "tts_params": {
            "output_format": output_format,
            "default_voice": default_voice,
            "speed": speed,
        },
        "translation_ready": t_ready,
        "translation_reason": t_reason,
        "translation_record": t_rec if t_ready else None,
        "translation_progress": tp_view,
        "has_partial_translation": has_partial_translation,
        "script_ready": s_ready,
        "script_reason": s_reason,
        "script_record": s_rec if s_ready else None,
        "script_progress": sp_summary,
        "has_partial_script": has_partial_script,
        "tts_ready": tts_ready,
        "tts_record": c_rec if tts_ready else None,
        "last_export": e_rec,
    }


def assess_book_pipeline(
    book: Dict[str, Any],
    settings: Optional[Dict[str, Any]] = None,
    translation_opts: Optional[Dict[str, Any]] = None,
    script_opts: Optional[Dict[str, Any]] = None,
    tts_opts: Optional[Dict[str, Any]] = None,
    store: Optional[LibraryStore] = None,
) -> Dict[str, Any]:
    """מאבחן מוכנות ועדכניות עבור כל פרקי הספר (0..K-1) כולל בדיקת כיסוי PDF והתקדמות ברמת יחידות."""
    cfg = settings or load_settings()
    st = store or LibraryStore()
    bid = (book or {}).get("id", "")
    chapters = (book or {}).get("chapters") or []
    cov_check = validate_book_completeness(book)

    chapter_statuses: List[Dict[str, Any]] = []
    t_ready_cnt = 0
    s_ready_cnt = 0
    tts_ready_cnt = 0
    total_trans_units = 0
    done_trans_units = 0
    missing_trans_units = 0
    failed_trans_units = 0
    outdated_trans_units = 0
    first_trans_resume: Optional[Dict[str, Any]] = None
    first_script_resume: Optional[Dict[str, Any]] = None

    for idx, ch in enumerate(chapters):
        cs = assess_chapter_pipeline(
            book_id=bid,
            chapter_index=idx,
            source_text=ch.get("text", ""),
            settings=cfg,
            translation_opts=translation_opts,
            script_opts=script_opts,
            tts_opts=tts_opts,
            store=st,
        )
        cs["chapter_title"] = ch.get("title", f"פרק {idx + 1}")
        tp = cs.get("translation_progress") or {}
        total_trans_units += int(tp.get("total_units") or 0)
        done_trans_units += int(tp.get("completed_count") or 0)
        missing_trans_units += int(tp.get("missing_count") or 0)
        failed_trans_units += int(tp.get("failed_count") or 0)
        outdated_trans_units += int(tp.get("outdated_count") or 0)

        if cs["translation_ready"]:
            t_ready_cnt += 1
        elif first_trans_resume is None:
            u_idx = int(tp.get("next_unit_index") or 0)
            first_trans_resume = {
                "stage": "translation",
                "chapter_index": idx,
                "chapter_number": idx + 1,
                "unit_index": u_idx,
                "unit_id": tp.get("next_unit_id") or f"u{u_idx + 1}",
            }
        if cs["script_ready"]:
            s_ready_cnt += 1
        elif cs["translation_ready"] and first_script_resume is None:
            sp = cs.get("script_progress") or {}
            su_idx = int(sp.get("next_unit_index") or 0)
            first_script_resume = {
                "stage": "script",
                "chapter_index": idx,
                "chapter_number": idx + 1,
                "unit_index": su_idx,
                "unit_id": sp.get("next_unit_id") or f"u{su_idx + 1}",
            }
        if cs["tts_ready"]:
            tts_ready_cnt += 1
        chapter_statuses.append(cs)

    first_resume_point = first_trans_resume or first_script_resume

    return {
        "book_id": bid,
        "book_title": (book or {}).get("title", ""),
        "book_complete": cov_check["is_complete"],
        "book_completeness_reason": cov_check["reason"],
        "chapter_count": len(chapters),
        "translations_ready": t_ready_cnt,
        "scripts_ready": s_ready_cnt,
        "tts_ready": tts_ready_cnt,
        "all_translations_ready": len(chapters) > 0 and t_ready_cnt == len(chapters),
        "all_scripts_ready": len(chapters) > 0 and s_ready_cnt == len(chapters),
        "all_tts_ready": len(chapters) > 0 and tts_ready_cnt == len(chapters),
        "total_translation_units": total_trans_units,
        "units_completed": done_trans_units,
        "units_missing": missing_trans_units,
        "units_failed": failed_trans_units,
        "units_outdated": outdated_trans_units,
        "resume_point": first_resume_point,
        "has_resumable_progress": bool(done_trans_units > 0 and not (len(chapters) > 0 and s_ready_cnt == len(chapters))),
        "chapters": chapter_statuses,
    }


def _collect_secrets(settings: Dict[str, Any]) -> List[str]:
    secs: List[str] = []
    for p in ((settings or {}).get("providers") or {}).values():
        k = (p.get("api_key") or "").strip()
        if k:
            secs.append(k)
    lk = ((settings or {}).get("translation", {}).get("libretranslate_api_key") or "").strip()
    if lk:
        secs.append(lk)
    return secs


def _extract_failure_details(
    exc: Exception,
    *,
    chapter_index: int,
    chapter_title: str,
    stage: str,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    raw = _sanitize_error_text(str(exc or ""), secrets=_collect_secrets(settings))
    low = raw.lower()
    if "<html" in low or "<!doctype" in low:
        raw = f"הספק חסום בשלב '{stage}' (התקבל דף חסימה/HTML)."
    cat = str(getattr(exc, "category", "") or "").lower() or "error"
    code = getattr(exc, "status_code", None)
    b_filter = str(getattr(exc, "blocked_filter", "") or "")
    prov = str(getattr(exc, "provider_id", "") or "")
    is_blocked = bool(
        getattr(exc, "is_blocked", False)
        or cat == "blocked"
        or code in (418, 451)
        or "netfree" in low
        or "418" in low
        or bool(b_filter)
    )
    if is_blocked and not b_filter and "netfree" in low:
        b_filter = "סינון NetFree (netfree.link)"
    if is_blocked and cat == "error":
        cat = "blocked"
    return {
        "chapter_index": int(chapter_index),
        "chapter_number": int(chapter_index) + 1,
        "chapter_title": chapter_title or f"פרק {int(chapter_index) + 1}",
        "unit_index": getattr(exc, "unit_index", None),
        "unit_id": str(getattr(exc, "unit_id", "") or ""),
        "batch_index": getattr(exc, "batch_index", None),
        "stage": stage,
        "category": cat,
        "is_blocked": is_blocked,
        "status_code": code,
        "blocked_provider": prov,
        "blocked_filter": b_filter,
        "error": raw,
    }


def ensure_translation_for_chapter(
    book_id: str,
    chapter_index: int,
    source_text: str,
    settings: Optional[Dict[str, Any]] = None,
    translation_opts: Optional[Dict[str, Any]] = None,
    store: Optional[LibraryStore] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
    single_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
    fallback_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
    circuit_breaker: Optional[ProviderCircuitBreaker] = None,
    fallback_policy: Optional[Dict[str, Any]] = None,
    request_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    שלב 1 מחייב: מחזיר תרגום עדכני ומלא של כל המשפטים הדרושים, או מתרגם ושומר אטומית.
    שומר אטומית לדיסק אחרי כל משפט שהושלם (כולל בפרק חלקי), ומשתמש ביחידות תקינות קיימות ללא קריאות חוזרות.
    """
    _raise_if_cancelled(cancel_event, "תרגום")
    cfg = settings or load_settings()
    st = store or LibraryStore()
    status = assess_chapter_pipeline(
        book_id=book_id,
        chapter_index=chapter_index,
        source_text=source_text,
        settings=cfg,
        translation_opts=translation_opts,
        store=st,
    )

    if status["translation_ready"] and status["translation_record"] is not None:
        return status["translation_record"]

    tp = status["translation_params"]
    source_sentences = split_into_sentences(source_text)
    if not source_sentences:
        raise MissingArtifactError(f"פרק {chapter_index + 1}: אין משפטים בטקסט המקור לתרגום.")

    tp_view = st.reconcile_translation_progress(
        book_id=book_id,
        chapter_index=chapter_index,
        source_sentences=source_sentences,
        config_hash=status["translation_config_hash"],
        source_lang=tp["source_lang"],
        target_lang=tp["target_lang"],
        source_hash=status["source_hash"],
        persist=False,
    )
    reusable_units: Dict[int, Dict[str, Any]] = {}
    for k, u_rec in (tp_view.get("units") or {}).items():
        if isinstance(u_rec, dict) and u_rec.get("status") == "completed" and (u_rec.get("translated_text") or "").strip():
            reusable_units[int(u_rec["unit_index"])] = u_rec

    if progress_cb:
        if reusable_units:
            progress_cb(
                f"שלב תרגום (פרק {chapter_index + 1}): נמצאו {len(reusable_units)}/{len(source_sentences)} יחידות תקינות, "
                f"ממשיך מהיחידה הבאה ({tp_view.get('next_unit_id') or 'u1'})..."
            )
        else:
            progress_cb(f"שלב תרגום (פרק {chapter_index + 1}): מתרגם את כל {len(source_sentences)} המשפטים...")

    eff_policy = dict(fallback_policy or {})
    t_opts = translation_opts or {}
    for k in ("fallback_to_ai_on_failure", "fallback_secondary_engine"):
        if k in t_opts and t_opts[k] is not None and k not in eff_policy:
            eff_policy[k] = t_opts[k]

    def _on_unit_progress(ev: Dict[str, Any]) -> None:
        ev_type = ev.get("event", "")
        u_idx = int(ev.get("unit_index", 0))
        if ev_type == "unit_done":
            st.save_translation_unit_progress(
                book_id=book_id,
                chapter_index=chapter_index,
                source_sentences=source_sentences,
                source_hash=status["source_hash"],
                config_hash=status["translation_config_hash"],
                source_lang=tp["source_lang"],
                target_lang=tp["target_lang"],
                unit_index=u_idx,
                unit_status="completed",
                translated_text=str(ev.get("translated_text") or ""),
                engine=str(ev.get("engine") or tp["engine"]),
                engine_label=str(ev.get("engine_label") or ""),
                ai_provider=str(ev.get("ai_provider") or ""),
                ai_model=str(ev.get("ai_model") or ""),
                fallback_used=bool(ev.get("fallback_used", False)),
                job_status="in_progress",
            )
        elif ev_type == "unit_failed":
            st.save_translation_unit_progress(
                book_id=book_id,
                chapter_index=chapter_index,
                source_sentences=source_sentences,
                source_hash=status["source_hash"],
                config_hash=status["translation_config_hash"],
                source_lang=tp["source_lang"],
                target_lang=tp["target_lang"],
                unit_index=u_idx,
                unit_status="failed",
                engine=str(ev.get("engine") or tp["engine"]),
                error=str(ev.get("error") or ""),
                job_status="failed",
            )
        elif ev_type == "unit_cancelled":
            st.save_translation_unit_progress(
                book_id=book_id,
                chapter_index=chapter_index,
                source_sentences=source_sentences,
                source_hash=status["source_hash"],
                config_hash=status["translation_config_hash"],
                source_lang=tp["source_lang"],
                target_lang=tp["target_lang"],
                unit_index=u_idx,
                unit_status="pending",
                engine=str(ev.get("engine") or tp["engine"]),
                job_status="cancelled",
            )

    try:
        res = translate_required_sentences(
            sentences=source_sentences,
            engine=tp["engine"],
            source_lang=tp["source_lang"],
            target_lang=tp["target_lang"],
            settings=cfg,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            chat_fn=chat_fn,
            single_translate_fn=single_translate_fn,
            fallback_translate_fn=fallback_translate_fn,
            circuit_breaker=circuit_breaker,
            fallback_policy=eff_policy or None,
            request_fn=request_fn,
            chapter_index=chapter_index,
            existing_units=reusable_units,
            unit_progress_cb=_on_unit_progress,
        )
    except TranslationCancelledError as e:
        st.mark_translation_progress_status(book_id, chapter_index, "cancelled")
        raise PipelineCancelledError(str(e)) from e

    if not res.get("is_complete"):
        raise IncompleteArtifactError(f"פרק {chapter_index + 1}: התרגום הסתיים במצב חלקי ונדחה.")

    translated_text = res["translated_text"]
    record = {
        "source_hash": status["source_hash"],
        "config_hash": status["translation_config_hash"],
        "translated_hash": compute_source_hash(translated_text),
        "engine": res.get("engine", tp["engine"]),
        "primary_engine": res.get("primary_engine", tp["engine"]),
        "fallback_used": bool(res.get("fallback_used", False)),
        "engine_label": res.get("engine_label", tp["engine"]),
        "ai_provider": res.get("ai_provider", ""),
        "ai_model": res.get("ai_model", ""),
        "unit_provenance": res.get("unit_provenance", {}),
        "source_lang": tp["source_lang"],
        "target_lang": tp["target_lang"],
        "source_sentences": res["source_sentences"],
        "translated_sentences": res["translated_sentences"],
        "source_paragraphs": res["source_paragraphs"],
        "target_paragraphs": res["target_paragraphs"],
        "translated_text": translated_text,
        "sentence_count_source": res["sentence_count_source"],
        "sentence_count_translated": res["sentence_count_translated"],
        "is_complete": True,
    }
    st.save_chapter_translation(book_id, chapter_index, record)
    return record


def ensure_script_for_chapter(
    book_id: str,
    chapter_index: int,
    source_text: str,
    book_title: str = "",
    settings: Optional[Dict[str, Any]] = None,
    translation_opts: Optional[Dict[str, Any]] = None,
    script_opts: Optional[Dict[str, Any]] = None,
    store: Optional[LibraryStore] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
    wiki_fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    units_per_batch: int = UNITS_PER_SCRIPT_BATCH,
    max_chars_per_batch: int = MAX_CHARS_PER_SCRIPT_BATCH,
) -> Dict[str, Any]:
    """
    שלב 2 מחייב: מוודא קיום תרגום מלא ועדכני תחילה, ומייצר/שומר תסריט AI מלא ועדכני.
    שומר כל אצוות תסריט מאומתת לדיסק מיד עם השלמתה וממזג אצוות לפי סדר וללא כפילויות.
    """
    _raise_if_cancelled(cancel_event, "יצירת תסריט")
    cfg = settings or load_settings()
    st = store or LibraryStore()
    status = assess_chapter_pipeline(
        book_id=book_id,
        chapter_index=chapter_index,
        source_text=source_text,
        settings=cfg,
        translation_opts=translation_opts,
        script_opts=script_opts,
        store=st,
    )

    if not status["translation_ready"] or not status["translation_record"]:
        reason = status["translation_reason"] or "חסר תרגום עדכני."
        if "השתנ" in reason or "השתנה" in reason:
            raise StaleArtifactError(f"לא ניתן ליצור תסריט לפני חידוש התרגום: {reason}")
        raise MissingArtifactError(f"לא ניתן ליצור תסריט ללא תרגום מלא ועדכני: {reason}")

    if status["script_ready"] and status["script_record"] is not None:
        return status["script_record"]

    trans_rec = status["translation_record"]
    sp = status["script_params"]
    translated_text = trans_rec["translated_text"]

    raw_batches = build_script_batches_spec(
        translated_text,
        units_per_batch=units_per_batch,
        max_chars_per_batch=max_chars_per_batch,
    )
    batches_spec: List[Dict[str, Any]] = []
    for b_idx0, b_units in enumerate(raw_batches):
        batches_spec.append({
            "batch_index": b_idx0,
            "unit_ids": [u["unitId"] for u in b_units],
            "unit_hashes": {u["unitId"]: compute_unit_source_hash(u["text"]) for u in b_units},
        })

    sp_view = st.reconcile_script_progress(
        book_id=book_id,
        chapter_index=chapter_index,
        batches_spec=batches_spec,
        source_hash=status["source_hash"],
        translation_hash=trans_rec["translated_hash"],
        config_hash=status["script_config_hash"],
        mode=sp["mode"],
        persist=False,
    )
    reusable_batches: Dict[int, Dict[str, Any]] = {}
    for bk, brec in (sp_view.get("batches") or {}).items():
        if isinstance(brec, dict) and brec.get("status") == "completed" and brec.get("lines"):
            reusable_batches[int(brec["batch_index"])] = brec

    if progress_cb:
        if reusable_batches:
            progress_cb(
                f"שלב תסריט AI (פרק {chapter_index + 1}): ממשיך מאצווה {int(sp_view.get('next_batch_index', 0)) + 1}/"
                f"{len(batches_spec)} (נשמרו {sp_view.get('completed_count', 0)}/{sp_view.get('total_units', 0)} יחידות)..."
            )
        else:
            progress_cb(f"שלב תסריט AI (פרק {chapter_index + 1}): יוצר תסריט מלא ועדכני...")

    def _on_script_batch(ev: Dict[str, Any]) -> None:
        ev_type = ev.get("event", "")
        b_idx = int(ev.get("batch_index", 0))
        if ev_type == "batch_done":
            st.save_script_batch_progress(
                book_id=book_id,
                chapter_index=chapter_index,
                batches_spec=batches_spec,
                source_hash=status["source_hash"],
                translation_hash=trans_rec["translated_hash"],
                config_hash=status["script_config_hash"],
                mode=sp["mode"],
                batch_index=b_idx,
                batch_status="completed",
                lines=ev.get("lines"),
                title=str(ev.get("title") or ""),
                wiki_articles=ev.get("wiki_articles") or [],
                provider_label=str(ev.get("provider_label") or ""),
                job_status="in_progress",
            )
        elif ev_type == "batch_failed":
            st.save_script_batch_progress(
                book_id=book_id,
                chapter_index=chapter_index,
                batches_spec=batches_spec,
                source_hash=status["source_hash"],
                translation_hash=trans_rec["translated_hash"],
                config_hash=status["script_config_hash"],
                mode=sp["mode"],
                batch_index=b_idx,
                batch_status="failed",
                provider_label=str(ev.get("provider_label") or ""),
                error=str(ev.get("error") or ""),
                job_status="failed",
            )
        elif ev_type == "batch_cancelled":
            st.save_script_batch_progress(
                book_id=book_id,
                chapter_index=chapter_index,
                batches_spec=batches_spec,
                source_hash=status["source_hash"],
                translation_hash=trans_rec["translated_hash"],
                config_hash=status["script_config_hash"],
                mode=sp["mode"],
                batch_index=b_idx,
                batch_status="pending",
                job_status="cancelled",
            )

    try:
        script_data = generate_narration_script(
            text=translated_text,
            mode=sp["mode"],
            custom_instruction=sp["custom_instruction"],
            book_title=book_title,
            settings=cfg,
            use_wikipedia=sp["use_wikipedia"],
            wiki_terms=sp["wiki_terms"],
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            chat_fn=chat_fn,
            wiki_fetcher=wiki_fetcher,
            units_per_batch=units_per_batch,
            max_chars_per_batch=max_chars_per_batch,
            existing_batches=reusable_batches,
            batch_progress_cb=_on_script_batch,
        )
    except ScriptCancelledError as e:
        raise PipelineCancelledError(str(e)) from e

    if not script_data.get("is_complete") or not script_data.get("lines"):
        raise IncompleteArtifactError(f"פרק {chapter_index + 1}: התסריט שנוצר חלקי או ריק ונדחה.")

    record = {
        "source_hash": status["source_hash"],
        "translation_hash": trans_rec["translated_hash"],
        "config_hash": status["script_config_hash"],
        "title": script_data["title"],
        "mode": script_data["mode"],
        "provider_label": script_data["provider_label"],
        "lines": script_data["lines"],
        "wiki_articles": script_data.get("wiki_articles", []),
        "unit_count": script_data["unit_count"],
        "covered_units": script_data["covered_units"],
        "line_count": script_data["line_count"],
        "is_complete": True,
    }
    st.save_chapter_script(book_id, chapter_index, record)
    return record


def _build_voice_overrides_for_script(
    script_record: Dict[str, Any],
    settings: Dict[str, Any],
    default_voice: str,
) -> List[str]:
    narr = (settings or {}).get("narration", {})
    t_voice = narr.get("teacher_voice", default_voice or "edge_avri")
    s_voice = narr.get("student_voice", "edge_hila")
    n_voice = narr.get("narrator_voice", default_voice or "edge_avri")

    overrides: List[str] = []
    for line in script_record.get("lines", []):
        role = (line.get("speakerRole") or "narrator").lower()
        if role == "teacher":
            overrides.append(t_voice)
        elif role == "student":
            overrides.append(s_voice)
        else:
            overrides.append(n_voice)
    return overrides


def run_tts_and_export_strict(
    book_id: str,
    chapter_index: int,
    source_text: str,
    output_path: str,
    settings: Optional[Dict[str, Any]] = None,
    translation_opts: Optional[Dict[str, Any]] = None,
    script_opts: Optional[Dict[str, Any]] = None,
    tts_opts: Optional[Dict[str, Any]] = None,
    store: Optional[LibraryStore] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    mp3_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
    wav_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
    mp3_merge_fn: Optional[Callable[[List[str], str], str]] = None,
) -> Dict[str, Any]:
    """שלב 3–4 לפרק יחיד: אוכף קיום תרגום ותסריט מלאים ועדכניים לפני יצוא שמע."""
    _raise_if_cancelled(cancel_event, "tts")
    cfg = settings or load_settings()
    st = store or LibraryStore()
    status = assess_chapter_pipeline(
        book_id=book_id,
        chapter_index=chapter_index,
        source_text=source_text,
        settings=cfg,
        translation_opts=translation_opts,
        script_opts=script_opts,
        tts_opts=tts_opts,
        store=st,
    )

    if not status["translation_ready"] or not status["translation_record"]:
        reason = status["translation_reason"] or "חסר תרגום."
        if "השתנ" in reason or "השתנה" in reason:
            raise StaleArtifactError(f"עצירת יצוא — התרגום התיישן: {reason}")
        if "חלקי" in reason:
            raise IncompleteArtifactError(f"עצירת יצוא — {reason}")
        raise MissingArtifactError(f"עצירת יצוא — חסר תרגום של כל המשפטים הדרושים: {reason}")

    if not status["script_ready"] or not status["script_record"]:
        reason = status["script_reason"] or "חסר תסריט."
        if "השתנ" in reason or "השתנה" in reason or "עודכן" in reason:
            raise StaleArtifactError(f"עצירת יצוא — התסריט התיישן: {reason}")
        if "חלקי" in reason:
            raise IncompleteArtifactError(f"עצירת יצוא — {reason}")
        raise MissingArtifactError(f"עצירת יצוא — חסר תסריט מלא ועדכני: {reason}")

    script_rec = status["script_record"]
    script_lines = [line["text"] for line in script_rec["lines"]]
    tp_tts = status["tts_params"]
    overrides = _build_voice_overrides_for_script(script_rec, cfg, tp_tts["default_voice"])

    if progress_cb:
        progress_cb(f"שלב TTS (פרק {chapter_index + 1}): מסנתז {len(script_lines)} שורות תסריט...")

    try:
        saved_path = export_audiobook_file(
            sentences=script_lines,
            output_path=output_path,
            default_voice=tp_tts["default_voice"],
            speed=tp_tts["speed"],
            voice_overrides=overrides,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            allow_fallback=False,
            mp3_synth_fn=mp3_synth_fn,
            wav_synth_fn=wav_synth_fn,
            mp3_merge_fn=mp3_merge_fn,
        )
    except ExportCancelledError as e:
        raise PipelineCancelledError(str(e)) from e

    export_record = {
        "success": True,
        "scope": "chapter",
        "output_path": saved_path,
        "source_hash": status["source_hash"],
        "translation_hash": status["translation_record"]["translated_hash"],
        "script_config_hash": status["script_config_hash"],
        "tts_config_hash": status["tts_config_hash"],
        "line_count": len(script_lines),
    }
    st.save_chapter_export(book_id, chapter_index, export_record)
    return export_record


def run_enforced_export_pipeline(
    book_id: str,
    chapter_index: int,
    source_text: str,
    output_path: str,
    book_title: str = "",
    settings: Optional[Dict[str, Any]] = None,
    translation_opts: Optional[Dict[str, Any]] = None,
    script_opts: Optional[Dict[str, Any]] = None,
    tts_opts: Optional[Dict[str, Any]] = None,
    store: Optional[LibraryStore] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
    single_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
    wiki_fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    mp3_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
    wav_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
    mp3_merge_fn: Optional[Callable[[List[str], str], str]] = None,
) -> Dict[str, Any]:
    """צינור יצוא מלא לפרק הנוכחי בלבד (scope='chapter')."""
    cfg = settings or load_settings()
    st = store or LibraryStore()
    current_stage = "translation"

    try:
        trans_rec = ensure_translation_for_chapter(
            book_id=book_id,
            chapter_index=chapter_index,
            source_text=source_text,
            settings=cfg,
            translation_opts=translation_opts,
            store=st,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            chat_fn=chat_fn,
            single_translate_fn=single_translate_fn,
        )

        current_stage = "script"
        script_rec = ensure_script_for_chapter(
            book_id=book_id,
            chapter_index=chapter_index,
            source_text=source_text,
            book_title=book_title,
            settings=cfg,
            translation_opts=translation_opts,
            script_opts=script_opts,
            store=st,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            chat_fn=chat_fn,
            wiki_fetcher=wiki_fetcher,
        )

        current_stage = "tts_export"
        if progress_cb:
            progress_cb("שלב 4/4 — שומר את קובץ השמע של הפרק באופן אטומי...")
        export_rec = run_tts_and_export_strict(
            book_id=book_id,
            chapter_index=chapter_index,
            source_text=source_text,
            output_path=output_path,
            settings=cfg,
            translation_opts=translation_opts,
            script_opts=script_opts,
            tts_opts=tts_opts,
            store=st,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            mp3_synth_fn=mp3_synth_fn,
            wav_synth_fn=wav_synth_fn,
            mp3_merge_fn=mp3_merge_fn,
        )

        return {
            "success": True,
            "status": "completed",
            "scope": "chapter",
            "stage": "completed",
            "output_path": export_rec["output_path"],
            "translation": trans_rec,
            "script": script_rec,
            "export": export_rec,
        }

    except PipelineCancelledError as e:
        return {
            "success": False,
            "scope": "chapter",
            "status": "cancelled",
            "stage": current_stage,
            "error": str(e),
        }
    except Exception as e:
        return {
            "success": False,
            "scope": "chapter",
            "status": "failed",
            "stage": current_stage,
            "error": str(e),
        }


def run_whole_book_script_pipeline(
    book: Dict[str, Any],
    settings: Optional[Dict[str, Any]] = None,
    translation_opts: Optional[Dict[str, Any]] = None,
    script_opts: Optional[Dict[str, Any]] = None,
    store: Optional[LibraryStore] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
    single_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
    wiki_fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    fallback_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
    circuit_breaker: Optional[ProviderCircuitBreaker] = None,
    fallback_policy: Optional[Dict[str, Any]] = None,
    request_fn: Optional[Callable[..., Any]] = None,
    retry_failed_only: bool = False,
    target_chapters: Optional[List[int]] = None,
    continue_on_chapter_failure: bool = False,
) -> Dict[str, Any]:
    """
    מייצר תרגום מאומת ולאחריו תסריט AI מלא עבור כל פרקי הספר (0..K-1 לפי הסדר),
    ללא תלות בפרק הפעיל המוצג בחלון.
    - משמר כל פרק שהושלם אטומית לתמיכה בהמשך/התאוששות מהנקודה שנעצרה
    - משתף Circuit Breaker למניעת חזרה עיוורת על ספק חסום (418 / NetFree) לאורך עשרות פרקים
    - תומך בתיקון ממוקד של פרקים שנכשלו בלבד (`retry_failed_only=True`)
    - תומך ב-'המשך לשאר הפרקים' במצב חלקי מפורש (`status="partial"`) מבלי להכריז על ספר חלקי כהושלם
    """
    cfg = settings or load_settings()
    st = store or LibraryStore()
    book_id = (book or {}).get("id", "")
    book_title = (book or {}).get("title", "")
    chapters = list((book or {}).get("chapters") or [])
    total = len(chapters)
    current_stage = "validation"
    current_chapter_num = 0
    breaker = circuit_breaker or ProviderCircuitBreaker()

    try:
        _raise_if_cancelled(cancel_event, "בדיקת שלמות ספר")
        cov_check = validate_book_completeness(book)
        if not cov_check["is_complete"]:
            raise IncompleteArtifactError(
                f"הספר '{book_title}' אינו שלם לעיבוד מלא: {cov_check['reason']}"
            )

        existing_job = st.get_whole_book_job(book_id) or {}
        initial_assess = assess_book_pipeline(
            book=book,
            settings=cfg,
            translation_opts=translation_opts,
            script_opts=script_opts,
            store=st,
        )
        completed_trans_set = {cs["chapter_index"] for cs in initial_assess["chapters"] if cs["translation_ready"]}
        completed_script_set = {cs["chapter_index"] for cs in initial_assess["chapters"] if cs["script_ready"]}

        failed_chapters: Dict[str, Dict[str, Any]] = {}
        if retry_failed_only or target_chapters is not None:
            for fk, fv in (existing_job.get("failed_chapters") or {}).items():
                if isinstance(fv, dict):
                    failed_chapters[str(fk)] = dict(fv)

        if target_chapters is not None:
            indices_to_run = sorted({int(i) for i in target_chapters if 0 <= int(i) < total})
        elif retry_failed_only:
            failed_idxs = {int(k) for k in failed_chapters.keys() if k.isdigit() and 0 <= int(k) < total}
            incomplete_idxs = {
                cs["chapter_index"]
                for cs in initial_assess["chapters"]
                if not cs["translation_ready"] or not cs["script_ready"]
            }
            indices_to_run = sorted(failed_idxs | incomplete_idxs)
            if not indices_to_run:
                indices_to_run = []
        else:
            indices_to_run = list(range(total))

        st.save_whole_book_job(
            book_id,
            {
                "job_type": "whole_book_script",
                "status": "in_progress",
                "chapter_count": total,
                "stage": "translation",
                "completed_translations": sorted(completed_trans_set),
                "completed_scripts": sorted(completed_script_set),
                "failed_chapters": failed_chapters,
            },
        )

        # שלב 1: תרגום כל הפרקים הנדרשים לפי הסדר
        current_stage = "translation"
        translations_by_idx: Dict[int, Dict[str, Any]] = {}
        for idx in indices_to_run:
            ch = chapters[idx]
            current_chapter_num = idx + 1
            ch_title = ch.get("title", f"פרק {current_chapter_num}")
            _raise_if_cancelled(cancel_event, f"תרגום פרק {current_chapter_num}/{total}")
            if progress_cb:
                progress_cb(
                    f"[ספר שלם — שלב 1/2: תרגום] פרק {current_chapter_num} מתוך {total}: {ch_title}"
                )
            try:
                t_rec = ensure_translation_for_chapter(
                    book_id=book_id,
                    chapter_index=idx,
                    source_text=ch.get("text", ""),
                    settings=cfg,
                    translation_opts=translation_opts,
                    store=st,
                    cancel_event=cancel_event,
                    progress_cb=progress_cb,
                    chat_fn=chat_fn,
                    single_translate_fn=single_translate_fn,
                    fallback_translate_fn=fallback_translate_fn,
                    circuit_breaker=breaker,
                    fallback_policy=fallback_policy,
                    request_fn=request_fn,
                )
                translations_by_idx[idx] = t_rec
                completed_trans_set.add(idx)
                failed_chapters.pop(str(idx), None)
            except PipelineCancelledError:
                raise
            except Exception as exc:
                f_info = _extract_failure_details(
                    exc,
                    chapter_index=idx,
                    chapter_title=ch_title,
                    stage="translation",
                    settings=cfg,
                )
                failed_chapters[str(idx)] = f_info
                if not continue_on_chapter_failure:
                    job_status = "blocked" if f_info["is_blocked"] else "failed"
                    err_assess = assess_book_pipeline(
                        book=book,
                        settings=cfg,
                        translation_opts=translation_opts,
                        script_opts=script_opts,
                        store=st,
                    )
                    st.save_whole_book_job(
                        book_id,
                        {
                            "job_type": "whole_book_script",
                            "status": job_status,
                            "chapter_count": total,
                            "stage": "translation",
                            "stopped_chapter_index": idx,
                            "failed_or_cancelled_chapter": current_chapter_num,
                            "unit_index": f_info.get("unit_index"),
                            "unit_id": f_info.get("unit_id", ""),
                            "is_blocked": f_info["is_blocked"],
                            "blocked_provider": f_info["blocked_provider"],
                            "blocked_filter": f_info["blocked_filter"],
                            "status_code": f_info["status_code"],
                            "completed_translations": sorted(completed_trans_set),
                            "completed_scripts": sorted(completed_script_set),
                            "units_completed": err_assess["units_completed"],
                            "units_missing": err_assess["units_missing"],
                            "units_failed": err_assess["units_failed"],
                            "failed_chapters": failed_chapters,
                            "error": f_info["error"],
                        },
                    )
                    return {
                        "success": False,
                        "scope": "book",
                        "status": job_status,
                        "is_blocked": f_info["is_blocked"],
                        "error_category": f_info["category"],
                        "status_code": f_info["status_code"],
                        "blocked_provider": f_info["blocked_provider"],
                        "blocked_filter": f_info["blocked_filter"],
                        "stage": "translation",
                        "book_id": book_id,
                        "book_title": book_title,
                        "chapter_index": idx,
                        "chapter_number": current_chapter_num,
                        "chapter_title": ch_title,
                        "chapter_count": total,
                        "unit_index": f_info.get("unit_index"),
                        "unit_id": f_info.get("unit_id", ""),
                        "completed_translations": sorted(completed_trans_set),
                        "completed_scripts": sorted(completed_script_set),
                        "units_completed": err_assess["units_completed"],
                        "units_missing": err_assess["units_missing"],
                        "units_failed": err_assess["units_failed"],
                        "failed_chapters": failed_chapters,
                        "error": f"כשל בפרק {current_chapter_num}/{total} (translation): {f_info['error']}",
                    }
                st.save_whole_book_job(
                    book_id,
                    {
                        "job_type": "whole_book_script",
                        "status": "in_progress",
                        "chapter_count": total,
                        "stage": "translation",
                        "stopped_chapter_index": idx,
                        "completed_translations": sorted(completed_trans_set),
                        "completed_scripts": sorted(completed_script_set),
                        "failed_chapters": failed_chapters,
                    },
                )

        # שלב 2: יצירת תסריט מלא לכל הפרקים שתרגומם תקין לפי הסדר
        current_stage = "script"
        scripts_by_idx: Dict[int, Dict[str, Any]] = {}
        for idx in indices_to_run:
            if str(idx) in failed_chapters:
                continue
            ch = chapters[idx]
            current_chapter_num = idx + 1
            ch_title = ch.get("title", f"פרק {current_chapter_num}")
            _raise_if_cancelled(cancel_event, f"תסריט פרק {current_chapter_num}/{total}")
            if progress_cb:
                progress_cb(
                    f"[ספר שלם — שלב 2/2: תסריט AI] פרק {current_chapter_num} מתוך {total}: {ch_title}"
                )
            try:
                s_rec = ensure_script_for_chapter(
                    book_id=book_id,
                    chapter_index=idx,
                    source_text=ch.get("text", ""),
                    book_title=book_title,
                    settings=cfg,
                    translation_opts=translation_opts,
                    script_opts=script_opts,
                    store=st,
                    cancel_event=cancel_event,
                    progress_cb=progress_cb,
                    chat_fn=chat_fn,
                    wiki_fetcher=wiki_fetcher,
                )
                scripts_by_idx[idx] = s_rec
                completed_script_set.add(idx)
                failed_chapters.pop(str(idx), None)
            except PipelineCancelledError:
                raise
            except Exception as exc:
                f_info = _extract_failure_details(
                    exc,
                    chapter_index=idx,
                    chapter_title=ch_title,
                    stage="script",
                    settings=cfg,
                )
                failed_chapters[str(idx)] = f_info
                if not continue_on_chapter_failure:
                    err_assess = assess_book_pipeline(
                        book=book,
                        settings=cfg,
                        translation_opts=translation_opts,
                        script_opts=script_opts,
                        store=st,
                    )
                    st.save_whole_book_job(
                        book_id,
                        {
                            "job_type": "whole_book_script",
                            "status": "blocked" if f_info["is_blocked"] else "failed",
                            "chapter_count": total,
                            "stage": "script",
                            "stopped_chapter_index": idx,
                            "failed_or_cancelled_chapter": current_chapter_num,
                            "unit_index": f_info.get("unit_index"),
                            "unit_id": f_info.get("unit_id", ""),
                            "batch_index": f_info.get("batch_index"),
                            "is_blocked": f_info["is_blocked"],
                            "blocked_provider": f_info["blocked_provider"],
                            "blocked_filter": f_info["blocked_filter"],
                            "status_code": f_info["status_code"],
                            "completed_translations": sorted(completed_trans_set),
                            "completed_scripts": sorted(completed_script_set),
                            "units_completed": err_assess["units_completed"],
                            "units_missing": err_assess["units_missing"],
                            "units_failed": err_assess["units_failed"],
                            "failed_chapters": failed_chapters,
                            "error": f_info["error"],
                        },
                    )
                    return {
                        "success": False,
                        "scope": "book",
                        "status": "blocked" if f_info["is_blocked"] else "failed",
                        "is_blocked": f_info["is_blocked"],
                        "error_category": f_info["category"],
                        "status_code": f_info["status_code"],
                        "blocked_provider": f_info["blocked_provider"],
                        "blocked_filter": f_info["blocked_filter"],
                        "stage": "script",
                        "book_id": book_id,
                        "book_title": book_title,
                        "chapter_index": idx,
                        "chapter_number": current_chapter_num,
                        "chapter_title": ch_title,
                        "chapter_count": total,
                        "unit_index": f_info.get("unit_index"),
                        "unit_id": f_info.get("unit_id", ""),
                        "batch_index": f_info.get("batch_index"),
                        "completed_translations": sorted(completed_trans_set),
                        "completed_scripts": sorted(completed_script_set),
                        "units_completed": err_assess["units_completed"],
                        "units_missing": err_assess["units_missing"],
                        "units_failed": err_assess["units_failed"],
                        "failed_chapters": failed_chapters,
                        "error": f"כשל בפרק {current_chapter_num}/{total} (script): {f_info['error']}",
                    }

        # אימות סופי שכל פרקי הספר מכוסים ומאומתים
        final_assess = assess_book_pipeline(
            book=book,
            settings=cfg,
            translation_opts=translation_opts,
            script_opts=script_opts,
            store=st,
        )
        all_t = [cs["translation_record"] for cs in final_assess["chapters"] if cs["translation_record"]]
        all_s = [cs["script_record"] for cs in final_assess["chapters"] if cs["script_record"]]
        final_completed_t = [cs["chapter_index"] for cs in final_assess["chapters"] if cs["translation_ready"]]
        final_completed_s = [cs["chapter_index"] for cs in final_assess["chapters"] if cs["script_ready"]]

        if failed_chapters or not final_assess["all_translations_ready"] or not final_assess["all_scripts_ready"]:
            needs_attention = sorted(
                {int(k) for k in failed_chapters.keys() if k.isdigit()}
                | {
                    cs["chapter_index"]
                    for cs in final_assess["chapters"]
                    if not cs["translation_ready"] or not cs["script_ready"]
                }
            )
            human_nums = ", ".join(str(i + 1) for i in needs_attention[:25])
            if len(needs_attention) > 25:
                human_nums += f" (+{len(needs_attention) - 25} נוספים)"
            partial_msg = (
                f"העיבוד הסתיים במצב חלקי בלבד (לא הושלם): {len(final_completed_s)}/{total} פרקים מוכנים. "
                f"פרקים טעוני טיפול: [{human_nums}]. לא ניתן לייצא שמע לכל הספר עד להשלמתם."
            )
            st.save_whole_book_job(
                book_id,
                {
                    "job_type": "whole_book_script",
                    "status": "partial",
                    "chapter_count": total,
                    "stage": "partial",
                    "completed_translations": final_completed_t,
                    "completed_scripts": final_completed_s,
                    "failed_chapters": failed_chapters,
                    "needs_attention_chapters": needs_attention,
                    "error": partial_msg,
                },
            )
            return {
                "success": False,
                "scope": "book",
                "status": "partial",
                "is_partial": True,
                "stage": "partial",
                "book_id": book_id,
                "book_title": book_title,
                "chapter_count": total,
                "completed_translations": final_completed_t,
                "completed_scripts": final_completed_s,
                "needs_attention_chapters": needs_attention,
                "failed_chapters": failed_chapters,
                "error": partial_msg,
            }

        st.save_whole_book_job(
            book_id,
            {
                "job_type": "whole_book_script",
                "status": "completed",
                "chapter_count": total,
                "stage": "completed",
                "completed_translations": list(range(total)),
                "completed_scripts": list(range(total)),
                "failed_chapters": {},
                "needs_attention_chapters": [],
            },
        )
        return {
            "success": True,
            "scope": "book",
            "status": "completed",
            "stage": "completed",
            "book_id": book_id,
            "book_title": book_title,
            "chapter_count": total,
            "chapters_processed": list(range(total)),
            "translations": all_t,
            "scripts": all_s,
        }

    except PipelineCancelledError as e:
        cur_assess = assess_book_pipeline(
            book=book,
            settings=cfg,
            translation_opts=translation_opts,
            script_opts=script_opts,
            store=st,
        )
        done_t = [cs["chapter_index"] for cs in cur_assess["chapters"] if cs["translation_ready"]]
        done_s = [cs["chapter_index"] for cs in cur_assess["chapters"] if cs["script_ready"]]
        st.save_whole_book_job(
            book_id,
            {
                "job_type": "whole_book_script",
                "status": "cancelled",
                "chapter_count": total,
                "stage": current_stage,
                "stopped_chapter_index": max(0, current_chapter_num - 1),
                "failed_or_cancelled_chapter": current_chapter_num,
                "completed_translations": done_t,
                "completed_scripts": done_s,
                "error": str(e),
            },
        )
        return {
            "success": False,
            "scope": "book",
            "status": "cancelled",
            "stage": current_stage,
            "book_id": book_id,
            "book_title": book_title,
            "chapter_number": current_chapter_num,
            "chapter_count": total,
            "completed_translations": done_t,
            "completed_scripts": done_s,
            "error": str(e),
        }
    except Exception as e:
        f_info = _extract_failure_details(
            e,
            chapter_index=max(0, current_chapter_num - 1),
            chapter_title="",
            stage=current_stage,
            settings=cfg,
        )
        st.save_whole_book_job(
            book_id,
            {
                "job_type": "whole_book_script",
                "status": "blocked" if f_info["is_blocked"] else "failed",
                "chapter_count": total,
                "stage": current_stage,
                "stopped_chapter_index": max(0, current_chapter_num - 1),
                "failed_or_cancelled_chapter": current_chapter_num,
                "unit_index": f_info.get("unit_index"),
                "unit_id": f_info.get("unit_id", ""),
                "batch_index": f_info.get("batch_index"),
                "is_blocked": f_info["is_blocked"],
                "blocked_provider": f_info["blocked_provider"],
                "blocked_filter": f_info["blocked_filter"],
                "status_code": f_info["status_code"],
                "error": f_info["error"],
            },
        )
        return {
            "success": False,
            "scope": "book",
            "status": "blocked" if f_info["is_blocked"] else "failed",
            "is_blocked": f_info["is_blocked"],
            "error_category": f_info["category"],
            "status_code": f_info["status_code"],
            "blocked_provider": f_info["blocked_provider"],
            "blocked_filter": f_info["blocked_filter"],
            "stage": current_stage,
            "book_id": book_id,
            "book_title": book_title,
            "chapter_number": current_chapter_num,
            "chapter_count": total,
            "unit_index": f_info.get("unit_index"),
            "unit_id": f_info.get("unit_id", ""),
            "batch_index": f_info.get("batch_index"),
            "error": f"כשל בפרק {current_chapter_num}/{total} ({current_stage}): {f_info['error']}",
        }


def run_whole_book_export_pipeline(
    book: Dict[str, Any],
    output_path: str,
    *,
    package_mode: str = "zip",
    settings: Optional[Dict[str, Any]] = None,
    translation_opts: Optional[Dict[str, Any]] = None,
    script_opts: Optional[Dict[str, Any]] = None,
    tts_opts: Optional[Dict[str, Any]] = None,
    store: Optional[LibraryStore] = None,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
    single_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
    wiki_fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    mp3_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
    wav_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
    mp3_merge_fn: Optional[Callable[[List[str], str], str]] = None,
    fallback_translate_fn: Optional[Callable[[str, str, str, str], str]] = None,
    circuit_breaker: Optional[ProviderCircuitBreaker] = None,
    fallback_policy: Optional[Dict[str, Any]] = None,
    request_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    מייצא ספר שלם (כל פרקי הספר 0..K-1 לפי הסדר, ללא תלות בפרק הפעיל):
      1. שלב 1: משלים ומאמת תרגום של כל 0..K-1 הפרקים (עם Circuit Breaker ומדיניות התאוששות)
      2. שלב 2: משלים ומאמת תסריט AI מלא ועדכני של כל 0..K-1 הפרקים
      3. שער קשיח: 0 קריאות TTS אם פרק אחד חסר/ישן/חלקי/טעון-טיפול
      4. שלב 3: מסנתז פרק-פרק למטמון בדיסק (מחדש רק פרקים חסרים/שהתיישנו, זיכרון חסום)
      5. שלב 4: אורז תוצר סופי אטומי ('unified_wav' / 'unified_mp3' / 'zip' / 'folder' + manifest.json)
    """
    cfg = settings or load_settings()
    st = store or LibraryStore()
    book_id = (book or {}).get("id", "")
    book_title = (book or {}).get("title", "")
    chapters = list((book or {}).get("chapters") or [])
    total = len(chapters)
    breaker = circuit_breaker or ProviderCircuitBreaker()

    eff_tts = dict(tts_opts or {})
    if package_mode == "unified_wav":
        eff_tts["output_format"] = "wav"
        if (eff_tts.get("default_voice") or "").startswith("edge"):
            eff_tts["default_voice"] = "sapi5"
    elif package_mode == "unified_mp3":
        eff_tts["output_format"] = "mp3"
    else:
        eff_tts.setdefault("output_format", "mp3" if not str(eff_tts.get("default_voice", "")).startswith("sapi5") else "wav")

    current_stage = "validation"
    current_chapter_num = 0
    failed_chapters: Dict[str, Dict[str, Any]] = {}

    try:
        _raise_if_cancelled(cancel_event, "בדיקת שלמות ספר")
        cov_check = validate_book_completeness(book)
        if not cov_check["is_complete"]:
            raise IncompleteArtifactError(
                f"הספר '{book_title}' אינו שלם ליצוא ספר מלא: {cov_check['reason']}"
            )

        # שלב 1: תרגום כל הפרקים 0..K-1
        current_stage = "translation"
        for idx, ch in enumerate(chapters):
            current_chapter_num = idx + 1
            _raise_if_cancelled(cancel_event, f"תרגום פרק {current_chapter_num}/{total}")
            if progress_cb:
                progress_cb(
                    f"[שלב 1/4: תרגום ספר מלא] פרק {current_chapter_num} מתוך {total}: {ch.get('title', '')}"
                )
            ensure_translation_for_chapter(
                book_id=book_id,
                chapter_index=idx,
                source_text=ch.get("text", ""),
                settings=cfg,
                translation_opts=translation_opts,
                store=st,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                chat_fn=chat_fn,
                single_translate_fn=single_translate_fn,
                fallback_translate_fn=fallback_translate_fn,
                circuit_breaker=breaker,
                fallback_policy=fallback_policy,
                request_fn=request_fn,
            )

        # שלב 2: תסריט כל הפרקים 0..K-1
        current_stage = "script"
        for idx, ch in enumerate(chapters):
            current_chapter_num = idx + 1
            _raise_if_cancelled(cancel_event, f"תסריט פרק {current_chapter_num}/{total}")
            if progress_cb:
                progress_cb(
                    f"[שלב 2/4: תסריט ספר מלא] פרק {current_chapter_num} מתוך {total}: {ch.get('title', '')}"
                )
            ensure_script_for_chapter(
                book_id=book_id,
                chapter_index=idx,
                source_text=ch.get("text", ""),
                book_title=book_title,
                settings=cfg,
                translation_opts=translation_opts,
                script_opts=script_opts,
                store=st,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                chat_fn=chat_fn,
                wiki_fetcher=wiki_fetcher,
            )

        # שער קשיח לפני TTS: חובה שכל 0..K-1 תרגומי ותסריטי הספר יהיו שלמים ומאומתים!
        pre_tts_check = assess_book_pipeline(
            book=book,
            settings=cfg,
            translation_opts=translation_opts,
            script_opts=script_opts,
            tts_opts=eff_tts,
            store=st,
        )
        if not pre_tts_check["all_translations_ready"] or not pre_tts_check["all_scripts_ready"]:
            raise MissingArtifactError("עצירה לפני TTS: לא כל תרגומי ותסריטי הספר מאומתים ועדכניים.")

        # שלב 3: סינתזת TTS פרק-פרק למטמון בדיסק (ללא שמירת כל הספר בזיכרון)
        current_stage = "tts_export"
        chapter_audio_entries: List[Dict[str, Any]] = []
        ch_ext = "wav" if eff_tts["output_format"] == "wav" or eff_tts.get("default_voice") == "sapi5" else "mp3"

        for idx, ch_status in enumerate(pre_tts_check["chapters"]):
            current_chapter_num = idx + 1
            _raise_if_cancelled(cancel_event, f"TTS פרק {current_chapter_num}/{total}")
            ch_title = chapters[idx].get("title", f"פרק {current_chapter_num}")
            script_rec = ch_status["script_record"]
            trans_rec = ch_status["translation_record"]

            if ch_status["tts_ready"] and ch_status["tts_record"]:
                cache_path = ch_status["tts_record"]["audio_path"]
                if progress_cb:
                    progress_cb(
                        f"[שלב 3/4: שמע פרק {current_chapter_num}/{total}] משתמש במטמון קיים של '{ch_title}'..."
                    )
            else:
                if progress_cb:
                    progress_cb(
                        f"[שלב 3/4: TTS ספר מלא] מסנתז פרק {current_chapter_num} מתוך {total}: {ch_title}"
                    )
                cache_path = get_chapter_audio_cache_path(book_id, idx, ch_ext)
                script_lines = [line["text"] for line in script_rec["lines"]]
                overrides = _build_voice_overrides_for_script(
                    script_rec, cfg, eff_tts.get("default_voice", "edge_avri")
                )
                try:
                    cache_path = export_audiobook_file(
                        sentences=script_lines,
                        output_path=cache_path,
                        default_voice=eff_tts.get("default_voice", "edge_avri"),
                        speed=float(eff_tts.get("speed", 1.0)),
                        voice_overrides=overrides,
                        progress_cb=progress_cb,
                        cancel_event=cancel_event,
                        allow_fallback=False,
                        mp3_synth_fn=mp3_synth_fn,
                        wav_synth_fn=wav_synth_fn,
                        mp3_merge_fn=mp3_merge_fn,
                    )
                except ExportCancelledError as e:
                    raise PipelineCancelledError(str(e)) from e

                st.save_chapter_tts_cache(
                    book_id,
                    idx,
                    {
                        "is_complete": True,
                        "audio_path": cache_path,
                        "format": ch_ext,
                        "source_hash": ch_status["source_hash"],
                        "translation_hash": trans_rec["translated_hash"],
                        "script_config_hash": ch_status["script_config_hash"],
                        "tts_config_hash": ch_status["tts_config_hash"],
                        "line_count": len(script_lines),
                    },
                )

            chapter_audio_entries.append({
                "chapter_index": idx,
                "chapter_title": ch_title,
                "audio_path": cache_path,
                "line_count": len(script_rec["lines"]),
            })

        # שלב 4: אריזת תוצר סופי של הספר כולו לפי הסדר
        current_stage = "packaging"
        _raise_if_cancelled(cancel_event, "אריזת שמע הספר כולו")
        if progress_cb:
            progress_cb(f"[שלב 4/4: אריזה סופית] יוצר תוצר שמע לכל {total} הפרקים...")

        try:
            pkg = package_book_audio(
                book_id=book_id,
                book_title=book_title,
                chapter_entries=chapter_audio_entries,
                output_path=output_path,
                package_mode=package_mode,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                mp3_merge_fn=mp3_merge_fn,
            )
        except ExportCancelledError as e:
            raise PipelineCancelledError(str(e)) from e

        st.save_whole_book_job(
            book_id,
            {
                "job_type": "whole_book_export",
                "status": "completed",
                "chapter_count": total,
                "stage": "completed",
                "completed_translations": list(range(total)),
                "completed_scripts": list(range(total)),
                "failed_chapters": {},
                "needs_attention_chapters": [],
                "output_path": pkg["output_path"],
                "package_mode": package_mode,
            },
        )
        return {
            "success": True,
            "scope": "book",
            "status": "completed",
            "stage": "completed",
            "book_id": book_id,
            "book_title": book_title,
            "chapter_count": total,
            "chapters_processed": [e["chapter_index"] for e in chapter_audio_entries],
            "output_path": pkg["output_path"],
            "package_mode": pkg["package_mode"],
            "manifest": pkg.get("manifest"),
            "manifest_path": pkg.get("manifest_path"),
        }

    except PipelineCancelledError as e:
        cur_assess = assess_book_pipeline(
            book=book,
            settings=cfg,
            translation_opts=translation_opts,
            script_opts=script_opts,
            tts_opts=eff_tts,
            store=st,
        )
        done_t = [cs["chapter_index"] for cs in cur_assess["chapters"] if cs["translation_ready"]]
        done_s = [cs["chapter_index"] for cs in cur_assess["chapters"] if cs["script_ready"]]
        st.save_whole_book_job(
            book_id,
            {
                "job_type": "whole_book_export",
                "status": "cancelled",
                "chapter_count": total,
                "stage": current_stage,
                "stopped_chapter_index": max(0, current_chapter_num - 1),
                "failed_or_cancelled_chapter": current_chapter_num,
                "completed_translations": done_t,
                "completed_scripts": done_s,
                "error": str(e),
            },
        )
        return {
            "success": False,
            "scope": "book",
            "status": "cancelled",
            "stage": current_stage,
            "book_id": book_id,
            "book_title": book_title,
            "chapter_number": current_chapter_num,
            "chapter_count": total,
            "completed_translations": done_t,
            "completed_scripts": done_s,
            "error": str(e),
        }
    except Exception as e:
        err_idx = max(0, current_chapter_num - 1)
        ch_t = chapters[err_idx].get("title", "") if 0 <= err_idx < total else ""
        f_info = _extract_failure_details(
            e,
            chapter_index=err_idx,
            chapter_title=ch_t,
            stage=current_stage,
            settings=cfg,
        )
        if 0 <= err_idx < total and current_stage in ("translation", "script"):
            failed_chapters[str(err_idx)] = f_info
        cur_assess = assess_book_pipeline(
            book=book,
            settings=cfg,
            translation_opts=translation_opts,
            script_opts=script_opts,
            tts_opts=eff_tts,
            store=st,
        )
        done_t = [cs["chapter_index"] for cs in cur_assess["chapters"] if cs["translation_ready"]]
        done_s = [cs["chapter_index"] for cs in cur_assess["chapters"] if cs["script_ready"]]
        job_status = "blocked" if f_info["is_blocked"] else "failed"
        st.save_whole_book_job(
            book_id,
            {
                "job_type": "whole_book_export",
                "status": job_status,
                "chapter_count": total,
                "stage": current_stage,
                "stopped_chapter_index": err_idx,
                "failed_or_cancelled_chapter": current_chapter_num,
                "unit_index": f_info.get("unit_index"),
                "unit_id": f_info.get("unit_id", ""),
                "batch_index": f_info.get("batch_index"),
                "is_blocked": f_info["is_blocked"],
                "blocked_provider": f_info["blocked_provider"],
                "blocked_filter": f_info["blocked_filter"],
                "status_code": f_info["status_code"],
                "completed_translations": done_t,
                "completed_scripts": done_s,
                "units_completed": cur_assess["units_completed"],
                "units_missing": cur_assess["units_missing"],
                "units_failed": cur_assess["units_failed"],
                "failed_chapters": failed_chapters,
                "error": f_info["error"],
            },
        )
        return {
            "success": False,
            "scope": "book",
            "status": job_status,
            "is_blocked": f_info["is_blocked"],
            "error_category": f_info["category"],
            "status_code": f_info["status_code"],
            "blocked_provider": f_info["blocked_provider"],
            "blocked_filter": f_info["blocked_filter"],
            "stage": current_stage,
            "book_id": book_id,
            "book_title": book_title,
            "chapter_index": err_idx,
            "chapter_number": current_chapter_num,
            "chapter_title": ch_t,
            "chapter_count": total,
            "unit_index": f_info.get("unit_index"),
            "unit_id": f_info.get("unit_id", ""),
            "batch_index": f_info.get("batch_index"),
            "completed_translations": done_t,
            "completed_scripts": done_s,
            "units_completed": cur_assess["units_completed"],
            "units_missing": cur_assess["units_missing"],
            "units_failed": cur_assess["units_failed"],
            "failed_chapters": failed_chapters,
            "error": f"כשל בפרק {current_chapter_num}/{total} ({current_stage}): {f_info['error']}",
        }
