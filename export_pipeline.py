# -*- coding: utf-8 -*-
"""
שירות יצוא נאכף (Enforced Export Pipeline Service) עבור PorMyCodex.

אוכף סדר שלבים קשיח ללא אפשרות עקיפה (Bypass) וללא שימוש בתוצרים ישנים (Stale Artifacts):
  1. תרגום כל המשפטים הדרושים (translate_required_sentences)
  2. תסריט AI מלא ועדכני המבוסס על התרגום העדכני והגדרות הסקריפט הנוכחיות
  3. סינתזת TTS של כל שורה בתסריט ללא דילוג על מקטעים
  4. כתיבה אטומית לקובץ היעד

כל שינוי בטקסט המקור, בשפת המקור/יעד, במנוע התרגום, בספק/מודל ה-AI, במצב/הנחיית התסריט,
בוויקיפדיה או בהגדרות הקול/מהירות פוסל אוטומטית תוצרים שהתיישנו.
כשל חלקי או ביטול לעולם אינם מדווחים כהצלחה ואינם משאירים קובץ פגום בנתיב היעד.
"""

from typing import Any, Callable, Dict, List, Optional

from audio_exporter import ExportCancelledError, export_audiobook_file
from book_loader import split_into_sentences
from library_store import (
    LibraryStore,
    compute_script_config_hash,
    compute_source_hash,
    compute_translation_config_hash,
    compute_tts_config_hash,
)
from script_engine import ScriptCancelledError, generate_narration_script
from settings_manager import load_settings
from translator_engine import TranslationCancelledError, translate_required_sentences


class PipelineError(RuntimeError):
    """שגיאת בסיס בשירות היצוא."""


class MissingArtifactError(PipelineError):
    """נזרק בעת ניסיון עקיפה כאשר תוצר נדרש (תרגום או תסריט) חסר."""


class StaleArtifactError(PipelineError):
    """נזרק בעת ניסיון שימוש בתוצר ישן לאחר שינוי מקור או הגדרות."""


class IncompleteArtifactError(PipelineError):
    """נזרק כאשר תרגום או תסריט הם חלקיים בלבד."""


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
    """
    בודק את עדכניות כל התוצרים השמורים עבור הפרק ומפרט בדיוק מה תקף ומה חסר/התיישן.
    """
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
    e_rec = artifacts.get("last_export")

    # אבחון שלב 1: תרגום
    t_reason = ""
    t_ready = False
    if not t_rec:
        t_reason = "לא קיים תרגום שמור לפרק זה."
    elif not t_rec.get("is_complete"):
        t_reason = "התרגום השמור חלקי בלבד."
    elif t_rec.get("source_hash") != src_hash:
        t_reason = "טקסט המקור השתנה מאז התרגום האחרון."
    elif t_rec.get("config_hash") != trans_cfg_hash:
        t_reason = "הגדרות התרגום (מנוע/שפות/מודל) השתנו."
    elif not LibraryStore.is_translation_fresh(t_rec, src_hash, trans_cfg_hash):
        t_reason = "התרגום השמור אינו מכסה את כל המשפטים הדרושים."
    else:
        t_ready = True

    expected_trans_hash = (t_rec or {}).get("translated_hash", "") if t_ready else ""

    # אבחון שלב 2: תסריט
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
        "script_ready": s_ready,
        "script_reason": s_reason,
        "script_record": s_rec if s_ready else None,
        "last_export": e_rec,
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
) -> Dict[str, Any]:
    """
    שלב 1 מחייב: מחזיר תרגום עדכני ומלא של כל המשפטים הדרושים, או מתרגם מחדש ושומר אטומית.
    """
    _raise_if_cancelled(cancel_event, "translation")
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
        raise MissingArtifactError("אין משפטים בטקסט המקור לתרגום.")

    if progress_cb:
        progress_cb(f"שלב 1/4 — מתרגם את כל {len(source_sentences)} המשפטים הדרושים...")

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
        )
    except TranslationCancelledError as e:
        raise PipelineCancelledError(str(e)) from e

    if not res.get("is_complete"):
        raise IncompleteArtifactError("התרגום הסתיים במצב חלקי ונדחה.")

    translated_text = res["translated_text"]
    record = {
        "source_hash": status["source_hash"],
        "config_hash": status["translation_config_hash"],
        "translated_hash": compute_source_hash(translated_text),
        "engine": tp["engine"],
        "engine_label": res.get("engine_label", tp["engine"]),
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
) -> Dict[str, Any]:
    """
    שלב 2 מחייב: מוודא קיום תרגום מלא ועדכני תחילה (אין עקיפה!),
    ולאחר מכן מחזיר או מייצר תסריט AI מלא ועדכני התואם בדיוק לתרגום ולהגדרות.
    """
    _raise_if_cancelled(cancel_event, "script")
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

    if progress_cb:
        progress_cb("שלב 2/4 — מייצר תסריט AI מלא ועדכני מהטקסט המתורגם...")

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
        )
    except ScriptCancelledError as e:
        raise PipelineCancelledError(str(e)) from e

    if not script_data.get("is_complete") or not script_data.get("lines"):
        raise IncompleteArtifactError("התסריט שנוצר חלקי או ריק ונדחה.")

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
) -> Dict[str, Any]:
    """
    שלב 3 ו-4 מחייבים: אוכף שלא ניתן לייצא אודיו אם התרגום או התסריט חסרים, חלקיים או ישנים!
    מונע כל עקיפה ישירה לשלב ה-TTS/יצוא.
    """
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
        progress_cb(f"שלב 3/4 — מסנתז קול (TTS) עבור {len(script_lines)} שורות תסריט...")

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
        )
    except ExportCancelledError as e:
        raise PipelineCancelledError(str(e)) from e

    export_record = {
        "success": True,
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
) -> Dict[str, Any]:
    """
    מריץ את הצינור המלא לפי הסדר המחייב:
      תרגום כל המשפטים הדרושים -> תסריט מלא ועדכני -> TTS -> יצוא אטומי.
    מחזיר מילון תוצאה מאומת שלעולם אינו מסמן ביטול או כשל חלקי כהצלחה.
    """
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
            progress_cb("שלב 4/4 — מאחד מקטעים ושומר את קובץ השמע באופן אטומי...")
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
        )

        return {
            "success": True,
            "status": "completed",
            "stage": "completed",
            "output_path": export_rec["output_path"],
            "translation": trans_rec,
            "script": script_rec,
            "export": export_rec,
        }

    except PipelineCancelledError as e:
        return {
            "success": False,
            "status": "cancelled",
            "stage": current_stage,
            "error": str(e),
        }
    except Exception as e:
        return {
            "success": False,
            "status": "failed",
            "stage": current_stage,
            "error": str(e),
        }
