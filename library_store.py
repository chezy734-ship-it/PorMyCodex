# -*- coding: utf-8 -*-
"""
מנהל התמדה אטומי וקבוע בתיקיית נתוני המשתמש (User Data Persistence Store) עבור PorMyCodex.

מאפיינים עיקריים:
- שמירה קבועה בתיקיית נתוני המשתמש (AppData / LocalAppData ב-Windows, או XDG_DATA_HOME / ~/.local/share בלינוקס/מק),
  תמיד עצמאית מתיקיית העבודה הנוכחית (cwd) ומתיקיית המשאבים של PyInstaller (_MEIPASS).
- כתיבה אטומית מלאה לכל קובץ (קובץ זמני באותה תיקייה -> flush -> fsync -> os.replace)
  כך שהפסקת חשמל, ביטול או קריסה לעולם לא משחיתים קובץ שמור קיים.
- שמירה ושחזור של: ספרים (כולל ספרים שיובאו/הודבקו), טקסט, תרגומים מאומתים, תסריטים מאומתים והתקדמות קריאה.
- בקרת תוקף (Freshness / Staleness) באמצעות חתימות SHA-256 של המקור וההגדרות כדי למנוע שימוש בתוצרים ישנים.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional

from book_loader import split_into_sentences

SCHEMA_VERSION = 2


def get_user_data_dir() -> str:
    """
    מחזיר נתיב קבוע ומוחלט לתיקיית נתוני המשתמש של PorMyCodex.
    לעולם אינו תלוי ב-os.getcwd() או ב-sys._MEIPASS בעת הרצה מתוך EXE.
    """
    override = (os.environ.get("PORMYCODEX_DATA_DIR") or "").strip()
    if override:
        path = os.path.abspath(override)
        os.makedirs(path, exist_ok=True)
        return path

    if os.name == "nt":
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or os.path.expanduser("~")
        )
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )

    path = os.path.abspath(os.path.join(base, "PorMyCodex"))
    os.makedirs(path, exist_ok=True)
    return path


def get_library_state_path() -> str:
    return os.path.join(get_user_data_dir(), "library_state.json")


def get_audio_cache_dir() -> str:
    path = os.path.join(get_user_data_dir(), "audio_cache")
    os.makedirs(path, exist_ok=True)
    return path


def sanitize_filename_part(name: str, fallback: str = "item") -> str:
    safe = "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in (name or "").strip())
    safe = "_".join(part for part in safe.split("_") if part)
    return (safe or fallback)[:48]


def get_chapter_audio_cache_path(book_id: str, chapter_index: int, ext: str = "wav") -> str:
    clean_ext = (ext or "wav").lower().lstrip(".")
    safe_bid = sanitize_filename_part(book_id, "book")
    return os.path.join(get_audio_cache_dir(), f"{safe_bid}__ch{int(chapter_index):04d}.{clean_ext}")


def atomic_write_bytes(target_path: str, data: bytes) -> str:
    """כותב בייטים בצורה אטומית (Temp file באותה תיקייה + fsync + os.replace)."""
    abs_target = os.path.abspath(target_path)
    parent = os.path.dirname(abs_target)
    os.makedirs(parent, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=parent,
        prefix=f".tmp_{os.path.basename(abs_target)}_",
        suffix=".part",
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, abs_target)
        return abs_target
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


def atomic_write_json(target_path: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return atomic_write_bytes(target_path, raw)


def _sha256_obj(obj: Any) -> str:
    encoded = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_source_hash(text: str) -> str:
    norm = (text or "").replace("\r\n", "\n").strip()
    sents = split_into_sentences(norm) if norm else []
    return _sha256_obj({"text": norm, "sentences": sents})


def compute_unit_source_hash(text: str) -> str:
    norm = (text or "").replace("\r\n", "\n").strip()
    return _sha256_obj({"unit_text": norm})


def compute_translation_config_hash(
    settings: Dict[str, Any],
    engine: str,
    source_lang: str,
    target_lang: str,
    translation_instruction: str = "",
) -> str:
    """
    מחשב חתימת תוקף להגדרות התרגום המהותיות בלבד (שפת מקור, שפת יעד, והוראות תרגום מהותיות).
    שם ספק התרגום/מנוע/מודל נשמר כתיעוד מקור (provenance) על כל תוצר ואינו פוסל תרגומים תקינים
    שכבר הושלמו כאשר מחליפים ספק לצורך התאוששות (למשל עקב חסימת שרת).
    """
    payload: Dict[str, Any] = {
        "source_lang": (source_lang or "auto").strip(),
        "target_lang": (target_lang or "he").strip(),
    }
    clean_inst = (translation_instruction or "").strip()
    if clean_inst:
        payload["translation_instruction"] = clean_inst
    return _sha256_obj(payload)


def compute_script_config_hash(
    settings: Dict[str, Any],
    mode: str,
    custom_instruction: str = "",
    use_wikipedia: bool = False,
    wiki_terms: str = "",
) -> str:
    pid = (settings or {}).get("active_provider", "gemini")
    pconf = ((settings or {}).get("providers") or {}).get(pid, {})
    payload = {
        "mode": (mode or "educational").strip(),
        "custom_instruction": (custom_instruction or "").strip(),
        "use_wikipedia": bool(use_wikipedia),
        "wiki_terms": (wiki_terms or "").strip() if use_wikipedia else "",
        "ai_provider": pid,
        "ai_model": (pconf.get("model") or "").strip(),
        "ai_base_url": (pconf.get("base_url") or "").strip(),
    }
    return _sha256_obj(payload)


def compute_tts_config_hash(
    settings: Dict[str, Any],
    output_format: str,
    default_voice: str,
    speed: float,
) -> str:
    narr = (settings or {}).get("narration", {})
    payload = {
        "output_format": (output_format or "mp3").lower().lstrip("."),
        "default_voice": (default_voice or "edge_avri").strip(),
        "speed": round(float(speed), 2),
        "teacher_voice": (narr.get("teacher_voice") or "edge_avri").strip(),
        "student_voice": (narr.get("student_voice") or "edge_hila").strip(),
        "narrator_voice": (narr.get("narrator_voice") or "edge_avri").strip(),
    }
    return _sha256_obj(payload)


def chapter_key(book_id: str, chapter_index: int) -> str:
    return f"{book_id}::{int(chapter_index)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_PROGRESS: Dict[str, Any] = {
    "active_book_id": "",
    "active_chapter_index": 0,
    "active_sentence_index": 0,
    "font_size": 18,
    "voice_type": "edge_avri",
    "speed": 1.0,
    "chapter_positions": {},
}


class LibraryStore:
    """
    מנהל התמדה מלא לספרים, טקסטים, תרגומים, תסריטים, מטמון שמע לפרקים והתקדמות קריאה.
    """

    def __init__(self, state_path: Optional[str] = None):
        self._explicit_path = os.path.abspath(state_path) if state_path else None

    @property
    def state_path(self) -> str:
        return self._explicit_path or get_library_state_path()

    def _empty_state(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "books": [],
            "reading_progress": deepcopy(DEFAULT_PROGRESS),
            "chapter_artifacts": {},
            "whole_book_jobs": {},
        }

    def load_raw_state(self) -> Dict[str, Any]:
        path = self.state_path
        if not os.path.exists(path):
            return self._empty_state()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return self._empty_state()
        except Exception:
            return self._empty_state()

        state = self._empty_state()
        if isinstance(data.get("books"), list):
            state["books"] = data["books"]
        if isinstance(data.get("reading_progress"), dict):
            merged_prog = deepcopy(DEFAULT_PROGRESS)
            merged_prog.update(data["reading_progress"])
            if not isinstance(merged_prog.get("chapter_positions"), dict):
                merged_prog["chapter_positions"] = {}
            state["reading_progress"] = merged_prog
        if isinstance(data.get("chapter_artifacts"), dict):
            state["chapter_artifacts"] = data["chapter_artifacts"]
        if isinstance(data.get("whole_book_jobs"), dict):
            state["whole_book_jobs"] = data["whole_book_jobs"]
        return state

    def _save_raw_state(self, state: Dict[str, Any]) -> None:
        state["schema_version"] = SCHEMA_VERSION
        state["updated_at"] = _now_iso()
        atomic_write_json(self.state_path, state)

    def load_library(self, default_books: List[Dict[str, Any]]) -> Dict[str, Any]:
        state = self.load_raw_state()
        saved_books = state.get("books") or []

        by_id: Dict[str, Dict[str, Any]] = {}
        ordered_books: List[Dict[str, Any]] = []

        # אם קיימים ספרים שמורים — משמרים את הסדר ואת התוכן המלא שלהם
        for b in saved_books:
            if isinstance(b, dict) and b.get("id") and isinstance(b.get("chapters"), list):
                by_id[b["id"]] = deepcopy(b)
                ordered_books.append(by_id[b["id"]])

        # משלימים ספרי ברירת מחדל רק אם אינם קיימים עדיין
        for db in default_books:
            bid = db.get("id")
            if bid and bid not in by_id:
                copy_b = deepcopy(db)
                by_id[bid] = copy_b
                ordered_books.append(copy_b)

        state["books"] = ordered_books
        return state

    def save_books_and_progress(
        self,
        books: List[Dict[str, Any]],
        progress: Dict[str, Any],
    ) -> None:
        state = self.load_raw_state()
        state["books"] = deepcopy(books)
        merged_prog = deepcopy(DEFAULT_PROGRESS)
        merged_prog.update(state.get("reading_progress") or {})
        merged_prog.update(progress or {})
        state["reading_progress"] = merged_prog
        self._save_raw_state(state)

    def update_reading_progress(
        self,
        book_id: str,
        chapter_index: int,
        sentence_index: int,
        font_size: Optional[int] = None,
        voice_type: Optional[str] = None,
        speed: Optional[float] = None,
    ) -> None:
        state = self.load_raw_state()
        prog = deepcopy(DEFAULT_PROGRESS)
        prog.update(state.get("reading_progress") or {})
        prog["active_book_id"] = book_id
        prog["active_chapter_index"] = int(chapter_index)
        prog["active_sentence_index"] = max(0, int(sentence_index))
        if font_size is not None:
            prog["font_size"] = int(font_size)
        if voice_type is not None:
            prog["voice_type"] = str(voice_type)
        if speed is not None:
            prog["speed"] = float(speed)
        positions = prog.setdefault("chapter_positions", {})
        positions[chapter_key(book_id, chapter_index)] = max(0, int(sentence_index))
        state["reading_progress"] = prog
        self._save_raw_state(state)

    def get_chapter_artifacts(self, book_id: str, chapter_index: int) -> Dict[str, Any]:
        state = self.load_raw_state()
        return deepcopy(state.get("chapter_artifacts", {}).get(chapter_key(book_id, chapter_index), {}))

    def save_chapter_translation(
        self,
        book_id: str,
        chapter_index: int,
        translation_record: Dict[str, Any],
    ) -> None:
        if not translation_record.get("is_complete"):
            raise ValueError("אסור לשמור תרגום חלקי כרשומה שלמה.")
        src_count = int(translation_record.get("sentence_count_source", 0))
        dst_count = int(translation_record.get("sentence_count_translated", 0))
        if src_count <= 0 or dst_count < src_count:
            raise ValueError("כמות המשפטים המתורגמים אינה מכסה את כל משפטֵי המקור.")

        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.setdefault("chapter_artifacts", {}).setdefault(ck, {})
        prev_t = art.get("translation")
        if isinstance(prev_t, dict) and prev_t.get("is_complete"):
            if (
                prev_t.get("source_hash") != translation_record.get("source_hash")
                or prev_t.get("config_hash") != translation_record.get("config_hash")
                or prev_t.get("translated_hash") != translation_record.get("translated_hash")
            ):
                hist = art.setdefault("translation_history", [])
                hist.append(deepcopy(prev_t))
                if len(hist) > 10:
                    del hist[:-10]

        rec = deepcopy(translation_record)
        rec["saved_at"] = _now_iso()
        art["translation"] = rec

        # סנכרון מלא של translation_progress כך שיחידות שלא השתנו בהמשך יוכלו להישמר ממוקדת
        src_sents = rec.get("source_sentences") or []
        dst_sents = rec.get("translated_sentences") or []
        u_prov = rec.get("unit_provenance") or {}
        if src_sents and len(src_sents) == len(dst_sents):
            existing_tp = art.get("translation_progress") if isinstance(art.get("translation_progress"), dict) else {}
            prev_units = existing_tp.get("units") if isinstance(existing_tp.get("units"), dict) else {}
            units_map: Dict[str, Any] = {}
            for i, (ss, ds) in enumerate(zip(src_sents, dst_sents)):
                key = str(i)
                prior_u = prev_units.get(key) if isinstance(prev_units.get(key), dict) else {}
                hist_u = list(prior_u.get("previous_versions") or [])
                if (
                    prior_u.get("status") == "completed"
                    and (prior_u.get("translated_text") or "").strip()
                    and (
                        prior_u.get("unit_source_hash") != compute_unit_source_hash(ss)
                        or prior_u.get("config_hash") != rec.get("config_hash")
                    )
                ):
                    snap = {k: v for k, v in prior_u.items() if k != "previous_versions"}
                    hist_u.append(snap)
                    if len(hist_u) > 5:
                        hist_u = hist_u[-5:]
                p_info = u_prov.get(key) if isinstance(u_prov.get(key), dict) else {}
                units_map[key] = {
                    "unit_id": f"u{i + 1}",
                    "unit_index": i,
                    "source_text": ss,
                    "unit_source_hash": compute_unit_source_hash(ss),
                    "config_hash": rec.get("config_hash", ""),
                    "translated_text": ds,
                    "status": "completed",
                    "engine": p_info.get("engine") or rec.get("engine", ""),
                    "engine_label": p_info.get("engine_label") or rec.get("engine_label", ""),
                    "ai_provider": p_info.get("ai_provider") if "ai_provider" in p_info else rec.get("ai_provider", ""),
                    "ai_model": p_info.get("ai_model") if "ai_model" in p_info else rec.get("ai_model", ""),
                    "fallback_used": bool(p_info.get("fallback_used", rec.get("fallback_used", False))),
                    "error": "",
                    "previous_versions": hist_u,
                    "updated_at": rec["saved_at"],
                }
            art["translation_progress"] = {
                "book_id": book_id,
                "chapter_index": int(chapter_index),
                "source_hash": rec.get("source_hash", ""),
                "config_hash": rec.get("config_hash", ""),
                "source_lang": rec.get("source_lang", "auto"),
                "target_lang": rec.get("target_lang", "he"),
                "total_units": len(src_sents),
                "status": "completed",
                "completed_count": len(src_sents),
                "missing_count": 0,
                "failed_count": 0,
                "outdated_count": 0,
                "next_unit_index": len(src_sents),
                "next_unit_id": "",
                "units": units_map,
                "updated_at": rec["saved_at"],
            }

        # תרגום חדש פוסל רק את הסטטוס השלם של התסריט הישן אם התרגום הכולל השתנה (תוך שמירה בארכיון, ללא מחיקה עיוורת)
        existing_script = art.get("script")
        if existing_script and existing_script.get("translation_hash") != rec.get("translated_hash"):
            shist = art.setdefault("script_history", [])
            shist.append(deepcopy(existing_script))
            if len(shist) > 10:
                del shist[:-10]
            art.pop("script", None)
            art.pop("last_export", None)

        # ניקוי רשומת כשל ישנה אם הפרק שוחזר/תורגם בהצלחה
        job = (state.get("whole_book_jobs") or {}).get(book_id)
        if isinstance(job, dict) and isinstance(job.get("failed_chapters"), dict):
            job["failed_chapters"].pop(str(int(chapter_index)), None)
            if isinstance(job.get("completed_translations"), list) and int(chapter_index) not in job["completed_translations"]:
                job["completed_translations"].append(int(chapter_index))
                job["completed_translations"].sort()
        self._save_raw_state(state)

    def _compute_translation_progress_view(
        self,
        art: Dict[str, Any],
        book_id: str,
        chapter_index: int,
        source_sentences: List[str],
        config_hash: str,
        source_lang: str,
        target_lang: str,
        source_hash: str,
    ) -> Dict[str, Any]:
        """מחזיר תמונת מצב עקבית של יחידות התרגום (ללא כתיבה לדיסק) תוך פסילה ממוקדת בלבד."""
        total = len(source_sentences)
        raw_tp = art.get("translation_progress") if isinstance(art.get("translation_progress"), dict) else {}
        existing_units = raw_tp.get("units") if isinstance(raw_tp.get("units"), dict) else {}

        # אם לא קיים translation_progress אך קיים תרגום שלם קודם (למשל מגרסה קודמת לפני עדכון ה-schema),
        # טוענים אותו כבסיס להשוואת יחידות כדי ששינוי מקור נקודתי ישמר יחידות זהות.
        if not existing_units:
            prev_complete = art.get("translation") if isinstance(art.get("translation"), dict) else {}
            p_src = prev_complete.get("source_sentences") or []
            p_dst = prev_complete.get("translated_sentences") or []
            p_cfg = prev_complete.get("config_hash", "")
            if p_src and len(p_src) == len(p_dst):
                seeded: Dict[str, Any] = {}
                for i, (ss, ds) in enumerate(zip(p_src, p_dst)):
                    if (ds or "").strip():
                        seeded[str(i)] = {
                            "unit_id": f"u{i + 1}",
                            "unit_index": i,
                            "source_text": ss,
                            "unit_source_hash": compute_unit_source_hash(ss),
                            "config_hash": p_cfg,
                            "translated_text": ds,
                            "status": "completed",
                            "engine": prev_complete.get("engine", ""),
                            "engine_label": prev_complete.get("engine_label", ""),
                            "ai_provider": prev_complete.get("ai_provider", ""),
                            "ai_model": prev_complete.get("ai_model", ""),
                            "fallback_used": bool(prev_complete.get("fallback_used", False)),
                            "error": "",
                        }
                existing_units = seeded

        units_out: Dict[str, Any] = {}
        completed_cnt = 0
        failed_cnt = 0
        outdated_cnt = 0
        next_idx: Optional[int] = None

        for i, sent in enumerate(source_sentences):
            key = str(i)
            u_hash = compute_unit_source_hash(sent)
            prev = existing_units.get(key) if isinstance(existing_units.get(key), dict) else None
            hist = list((prev or {}).get("previous_versions") or [])
            if prev:
                prev_status = prev.get("status", "pending")
                same_unit = (prev.get("unit_source_hash") == u_hash and prev.get("config_hash") == config_hash)
                if same_unit and prev_status == "completed" and (prev.get("translated_text") or "").strip():
                    u_entry = deepcopy(prev)
                    u_entry["unit_id"] = f"u{i + 1}"
                    u_entry["unit_index"] = i
                    units_out[key] = u_entry
                    completed_cnt += 1
                    continue
                if prev_status == "completed" and (prev.get("translated_text") or "").strip():
                    snap = {k: v for k, v in prev.items() if k != "previous_versions"}
                    if not hist or hist[-1].get("unit_source_hash") != snap.get("unit_source_hash") or hist[-1].get("config_hash") != snap.get("config_hash"):
                        hist.append(snap)
                        if len(hist) > 5:
                            hist = hist[-5:]
                    units_out[key] = {
                        "unit_id": f"u{i + 1}",
                        "unit_index": i,
                        "source_text": sent,
                        "unit_source_hash": u_hash,
                        "config_hash": config_hash,
                        "translated_text": "",
                        "status": "outdated",
                        "engine": "",
                        "engine_label": "",
                        "ai_provider": "",
                        "ai_model": "",
                        "fallback_used": False,
                        "error": "היחידה התיישנה בעקבות שינוי טקסט המקור או הגדרות שפה/הוראות.",
                        "previous_versions": hist,
                        "updated_at": prev.get("updated_at", ""),
                    }
                    outdated_cnt += 1
                    if next_idx is None:
                        next_idx = i
                    continue
                if same_unit and prev_status == "failed":
                    u_entry = deepcopy(prev)
                    u_entry["unit_id"] = f"u{i + 1}"
                    u_entry["unit_index"] = i
                    u_entry["previous_versions"] = hist
                    units_out[key] = u_entry
                    failed_cnt += 1
                    if next_idx is None:
                        next_idx = i
                    continue

            units_out[key] = {
                "unit_id": f"u{i + 1}",
                "unit_index": i,
                "source_text": sent,
                "unit_source_hash": u_hash,
                "config_hash": config_hash,
                "translated_text": "",
                "status": "pending",
                "engine": "",
                "engine_label": "",
                "ai_provider": "",
                "ai_model": "",
                "fallback_used": False,
                "error": "",
                "previous_versions": hist,
            }
            if next_idx is None:
                next_idx = i

        if next_idx is None:
            next_idx = total
        missing_cnt = max(0, total - completed_cnt)
        status_val = raw_tp.get("status", "pending")
        if total > 0 and completed_cnt == total:
            status_val = "completed"
        elif failed_cnt > 0 and status_val not in ("blocked", "cancelled"):
            status_val = "failed"
        elif outdated_cnt > 0 and status_val == "completed":
            status_val = "in_progress"

        return {
            "book_id": book_id,
            "chapter_index": int(chapter_index),
            "source_hash": source_hash,
            "config_hash": config_hash,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "total_units": total,
            "status": status_val,
            "completed_count": completed_cnt,
            "missing_count": missing_cnt,
            "failed_count": failed_cnt,
            "outdated_count": outdated_cnt,
            "next_unit_index": next_idx,
            "next_unit_id": f"u{next_idx + 1}" if next_idx < total else "",
            "units": units_out,
            "updated_at": raw_tp.get("updated_at", ""),
        }

    def reconcile_translation_progress(
        self,
        book_id: str,
        chapter_index: int,
        source_sentences: List[str],
        config_hash: str,
        source_lang: str,
        target_lang: str,
        source_hash: str,
        *,
        persist: bool = False,
    ) -> Dict[str, Any]:
        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.get("chapter_artifacts", {}).get(ck, {})
        view = self._compute_translation_progress_view(
            art=art,
            book_id=book_id,
            chapter_index=chapter_index,
            source_sentences=source_sentences,
            config_hash=config_hash,
            source_lang=source_lang,
            target_lang=target_lang,
            source_hash=source_hash,
        )
        if persist and art is not None:
            art_mut = state.setdefault("chapter_artifacts", {}).setdefault(ck, {})
            view["updated_at"] = _now_iso()
            art_mut["translation_progress"] = view
            self._save_raw_state(state)
        return view

    def save_translation_unit_progress(
        self,
        book_id: str,
        chapter_index: int,
        source_sentences: List[str],
        source_hash: str,
        config_hash: str,
        source_lang: str,
        target_lang: str,
        unit_index: int,
        unit_status: str,
        *,
        translated_text: str = "",
        engine: str = "",
        engine_label: str = "",
        ai_provider: str = "",
        ai_model: str = "",
        fallback_used: bool = False,
        error: str = "",
        job_status: str = "in_progress",
    ) -> Dict[str, Any]:
        """שומר אטומית התקדמות ברמת משפט בודד מבלי לסמן פרק חלקי כמוכן ליצוא."""
        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.setdefault("chapter_artifacts", {}).setdefault(ck, {})
        view = self._compute_translation_progress_view(
            art=art,
            book_id=book_id,
            chapter_index=chapter_index,
            source_sentences=source_sentences,
            config_hash=config_hash,
            source_lang=source_lang,
            target_lang=target_lang,
            source_hash=source_hash,
        )
        idx = int(unit_index)
        total = len(source_sentences)
        if 0 <= idx < total:
            key = str(idx)
            sent = source_sentences[idx]
            existing_u = view["units"].get(key) or {}
            hist = list(existing_u.get("previous_versions") or [])
            now_ts = _now_iso()
            if unit_status == "completed" and (translated_text or "").strip():
                view["units"][key] = {
                    "unit_id": f"u{idx + 1}",
                    "unit_index": idx,
                    "source_text": sent,
                    "unit_source_hash": compute_unit_source_hash(sent),
                    "config_hash": config_hash,
                    "translated_text": translated_text.strip(),
                    "status": "completed",
                    "engine": engine,
                    "engine_label": engine_label,
                    "ai_provider": ai_provider,
                    "ai_model": ai_model,
                    "fallback_used": bool(fallback_used),
                    "error": "",
                    "previous_versions": hist,
                    "updated_at": now_ts,
                }
            else:
                view["units"][key] = {
                    "unit_id": f"u{idx + 1}",
                    "unit_index": idx,
                    "source_text": sent,
                    "unit_source_hash": compute_unit_source_hash(sent),
                    "config_hash": config_hash,
                    "translated_text": existing_u.get("translated_text", "") if existing_u.get("status") == "completed" else "",
                    "status": unit_status if existing_u.get("status") != "completed" else "completed",
                    "engine": engine or existing_u.get("engine", ""),
                    "engine_label": engine_label or existing_u.get("engine_label", ""),
                    "ai_provider": ai_provider,
                    "ai_model": ai_model,
                    "fallback_used": bool(fallback_used),
                    "error": error,
                    "previous_versions": hist,
                    "updated_at": now_ts,
                }

        completed_cnt = 0
        failed_cnt = 0
        outdated_cnt = 0
        next_idx: Optional[int] = None
        for i in range(total):
            u = view["units"].get(str(i), {})
            st_u = u.get("status", "pending")
            if st_u == "completed":
                completed_cnt += 1
            else:
                if next_idx is None:
                    next_idx = i
                if st_u == "failed":
                    failed_cnt += 1
                elif st_u == "outdated":
                    outdated_cnt += 1

        if next_idx is None:
            next_idx = total
        view["completed_count"] = completed_cnt
        view["missing_count"] = max(0, total - completed_cnt)
        view["failed_count"] = failed_cnt
        view["outdated_count"] = outdated_cnt
        view["next_unit_index"] = next_idx
        view["next_unit_id"] = f"u{next_idx + 1}" if next_idx < total else ""
        view["status"] = job_status
        view["updated_at"] = _now_iso()
        art["translation_progress"] = view
        self._save_raw_state(state)
        return view

    def mark_translation_progress_status(
        self,
        book_id: str,
        chapter_index: int,
        job_status: str,
    ) -> None:
        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.get("chapter_artifacts", {}).get(ck)
        if isinstance(art, dict) and isinstance(art.get("translation_progress"), dict):
            art["translation_progress"]["status"] = job_status
            art["translation_progress"]["updated_at"] = _now_iso()
            self._save_raw_state(state)

    def _compute_script_progress_view(
        self,
        art: Dict[str, Any],
        book_id: str,
        chapter_index: int,
        batches_spec: List[Dict[str, Any]],
        source_hash: str,
        translation_hash: str,
        config_hash: str,
        mode: str,
    ) -> Dict[str, Any]:
        total_batches = len(batches_spec)
        total_units = sum(len(b["unit_ids"]) for b in batches_spec)
        raw_sp = art.get("script_progress") if isinstance(art.get("script_progress"), dict) else {}
        existing_batches = raw_sp.get("batches") if isinstance(raw_sp.get("batches"), dict) else {}

        # אם לא קיים script_progress אך קיים תסריט שלם קודם, אפשר לשחזר אצוות תקינות שיחידות התרגום שלהן לא השתנו
        if not existing_batches:
            prev_complete = art.get("script") if isinstance(art.get("script"), dict) else {}
            prev_lines = prev_complete.get("lines") or []
            prev_cfg = prev_complete.get("config_hash", "")
            prev_th = prev_complete.get("translation_hash", "")
            if prev_lines and prev_cfg == config_hash and prev_th == translation_hash:
                by_uid: Dict[str, List[Dict[str, Any]]] = {}
                for ln in prev_lines:
                    if isinstance(ln, dict) and ln.get("unitId"):
                        by_uid.setdefault(str(ln["unitId"]), []).append(ln)
                seeded_b: Dict[str, Any] = {}
                for b in batches_spec:
                    b_idx = b["batch_index"]
                    uids = b["unit_ids"]
                    if all(uid in by_uid for uid in uids):
                        b_lines = []
                        for uid in uids:
                            b_lines.extend(by_uid[uid])
                        seeded_b[str(b_idx)] = {
                            "batch_index": b_idx,
                            "unit_ids": uids,
                            "unit_hashes": b["unit_hashes"],
                            "config_hash": prev_cfg,
                            "status": "completed",
                            "title": prev_complete.get("title", ""),
                            "lines": b_lines,
                            "wiki_articles": prev_complete.get("wiki_articles", []),
                            "provider_label": prev_complete.get("provider_label", ""),
                            "error": "",
                        }
                existing_batches = seeded_b

        batches_out: Dict[str, Any] = {}
        units_out: Dict[str, Any] = {}
        completed_units_set = set()
        failed_units_cnt = 0
        outdated_units_cnt = 0
        next_batch_idx: Optional[int] = None
        next_unit_id = ""
        next_unit_idx: Optional[int] = None

        global_u_cursor = 0
        for b in batches_spec:
            b_idx = int(b["batch_index"])
            b_key = str(b_idx)
            uids = list(b["unit_ids"])
            u_hashes = dict(b["unit_hashes"])
            prev_b = existing_batches.get(b_key) if isinstance(existing_batches.get(b_key), dict) else None
            hist_b = list((prev_b or {}).get("previous_versions") or [])

            matched_ok = False
            b_status = "pending"
            if prev_b:
                p_status = prev_b.get("status", "pending")
                same_cfg = prev_b.get("config_hash") == config_hash
                same_ids = prev_b.get("unit_ids") == uids
                p_hashes = prev_b.get("unit_hashes") or {}
                same_unit_hashes = all(p_hashes.get(uid) == u_hashes.get(uid) for uid in uids)
                has_lines = isinstance(prev_b.get("lines"), list) and len(prev_b["lines"]) > 0
                if p_status == "completed" and same_cfg and same_ids and same_unit_hashes and has_lines:
                    matched_ok = True
                    b_status = "completed"
                elif p_status == "completed" and has_lines:
                    snap = {k: v for k, v in prev_b.items() if k != "previous_versions"}
                    hist_b.append(snap)
                    if len(hist_b) > 5:
                        hist_b = hist_b[-5:]
                    b_status = "outdated"
                elif same_cfg and same_ids and same_unit_hashes and p_status == "failed":
                    b_status = "failed"

            if matched_ok and prev_b is not None:
                b_entry = deepcopy(prev_b)
                b_entry["previous_versions"] = hist_b
                batches_out[b_key] = b_entry
                for local_offset, uid in enumerate(uids):
                    completed_units_set.add(uid)
                    units_out[uid] = {
                        "unit_id": uid,
                        "unit_index": global_u_cursor + local_offset,
                        "batch_index": b_idx,
                        "unit_translation_hash": u_hashes.get(uid, ""),
                        "config_hash": config_hash,
                        "status": "completed",
                        "provider_label": b_entry.get("provider_label", ""),
                        "error": "",
                    }
            else:
                if next_batch_idx is None:
                    next_batch_idx = b_idx
                    next_unit_idx = global_u_cursor
                    next_unit_id = uids[0] if uids else ""
                err_txt = (prev_b or {}).get("error", "") if b_status == "failed" else (
                    "האצווה התיישנה בעקבות שינוי תרגום או הגדרות תסריט." if b_status == "outdated" else ""
                )
                batches_out[b_key] = {
                    "batch_index": b_idx,
                    "unit_ids": uids,
                    "unit_hashes": u_hashes,
                    "config_hash": config_hash,
                    "status": b_status,
                    "title": "",
                    "lines": [],
                    "wiki_articles": [],
                    "provider_label": (prev_b or {}).get("provider_label", ""),
                    "error": err_txt,
                    "previous_versions": hist_b,
                    "updated_at": (prev_b or {}).get("updated_at", ""),
                }
                for local_offset, uid in enumerate(uids):
                    if b_status == "failed":
                        failed_units_cnt += 1
                    elif b_status == "outdated":
                        outdated_units_cnt += 1
                    units_out[uid] = {
                        "unit_id": uid,
                        "unit_index": global_u_cursor + local_offset,
                        "batch_index": b_idx,
                        "unit_translation_hash": u_hashes.get(uid, ""),
                        "config_hash": config_hash,
                        "status": b_status,
                        "error": err_txt,
                    }
            global_u_cursor += len(uids)

        completed_cnt = len(completed_units_set)
        missing_cnt = max(0, total_units - completed_cnt)
        if next_batch_idx is None:
            next_batch_idx = total_batches
            next_unit_idx = total_units
            next_unit_id = ""

        sp_status = raw_sp.get("status", "pending")
        if total_units > 0 and completed_cnt == total_units:
            sp_status = "completed"
        elif failed_units_cnt > 0 and sp_status not in ("blocked", "cancelled"):
            sp_status = "failed"
        elif outdated_units_cnt > 0 and sp_status == "completed":
            sp_status = "in_progress"

        return {
            "book_id": book_id,
            "chapter_index": int(chapter_index),
            "source_hash": source_hash,
            "translation_hash": translation_hash,
            "config_hash": config_hash,
            "mode": mode,
            "total_units": total_units,
            "total_batches": total_batches,
            "status": sp_status,
            "completed_units": [uid for b in batches_spec for uid in b["unit_ids"] if uid in completed_units_set],
            "completed_count": completed_cnt,
            "missing_count": missing_cnt,
            "failed_count": failed_units_cnt,
            "outdated_count": outdated_units_cnt,
            "next_batch_index": next_batch_idx,
            "next_unit_index": next_unit_idx,
            "next_unit_id": next_unit_id,
            "batches": batches_out,
            "units": units_out,
            "updated_at": raw_sp.get("updated_at", ""),
        }

    def reconcile_script_progress(
        self,
        book_id: str,
        chapter_index: int,
        batches_spec: List[Dict[str, Any]],
        source_hash: str,
        translation_hash: str,
        config_hash: str,
        mode: str,
        *,
        persist: bool = False,
    ) -> Dict[str, Any]:
        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.get("chapter_artifacts", {}).get(ck, {})
        view = self._compute_script_progress_view(
            art=art,
            book_id=book_id,
            chapter_index=chapter_index,
            batches_spec=batches_spec,
            source_hash=source_hash,
            translation_hash=translation_hash,
            config_hash=config_hash,
            mode=mode,
        )
        if persist and art is not None:
            art_mut = state.setdefault("chapter_artifacts", {}).setdefault(ck, {})
            view["updated_at"] = _now_iso()
            art_mut["script_progress"] = view
            self._save_raw_state(state)
        return view

    def save_script_batch_progress(
        self,
        book_id: str,
        chapter_index: int,
        batches_spec: List[Dict[str, Any]],
        source_hash: str,
        translation_hash: str,
        config_hash: str,
        mode: str,
        batch_index: int,
        batch_status: str,
        *,
        lines: Optional[List[Dict[str, Any]]] = None,
        title: str = "",
        wiki_articles: Optional[List[Dict[str, Any]]] = None,
        provider_label: str = "",
        error: str = "",
        job_status: str = "in_progress",
    ) -> Dict[str, Any]:
        """שומר אטומית אצוות תסריט מאומתת לדיסק מבלי לסמן פרק חלקי כמוכן ליצוא."""
        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.setdefault("chapter_artifacts", {}).setdefault(ck, {})
        view = self._compute_script_progress_view(
            art=art,
            book_id=book_id,
            chapter_index=chapter_index,
            batches_spec=batches_spec,
            source_hash=source_hash,
            translation_hash=translation_hash,
            config_hash=config_hash,
            mode=mode,
        )
        b_key = str(int(batch_index))
        spec = next((b for b in batches_spec if int(b["batch_index"]) == int(batch_index)), None)
        if spec is not None:
            existing_b = view["batches"].get(b_key) or {}
            hist_b = list(existing_b.get("previous_versions") or [])
            now_ts = _now_iso()
            if batch_status == "completed" and lines:
                view["batches"][b_key] = {
                    "batch_index": int(batch_index),
                    "unit_ids": list(spec["unit_ids"]),
                    "unit_hashes": dict(spec["unit_hashes"]),
                    "config_hash": config_hash,
                    "status": "completed",
                    "title": title,
                    "lines": deepcopy(lines),
                    "wiki_articles": deepcopy(wiki_articles or []),
                    "provider_label": provider_label,
                    "error": "",
                    "previous_versions": hist_b,
                    "updated_at": now_ts,
                }
            else:
                keep_ok = existing_b.get("status") == "completed" and bool(existing_b.get("lines"))
                if not keep_ok:
                    view["batches"][b_key] = {
                        "batch_index": int(batch_index),
                        "unit_ids": list(spec["unit_ids"]),
                        "unit_hashes": dict(spec["unit_hashes"]),
                        "config_hash": config_hash,
                        "status": batch_status,
                        "title": title,
                        "lines": [],
                        "wiki_articles": [],
                        "provider_label": provider_label,
                        "error": error,
                        "previous_versions": hist_b,
                        "updated_at": now_ts,
                    }

        # חישוב מחדש של המונים ונקודת ההמשך הבאה
        completed_units_set = set()
        failed_u = 0
        outdated_u = 0
        next_b_idx: Optional[int] = None
        next_u_idx: Optional[int] = None
        next_u_id = ""
        cursor = 0
        for b in batches_spec:
            bk = str(int(b["batch_index"]))
            b_rec = view["batches"].get(bk, {})
            st_b = b_rec.get("status", "pending")
            for offset, uid in enumerate(b["unit_ids"]):
                if st_b == "completed":
                    completed_units_set.add(uid)
                    view["units"][uid] = {
                        "unit_id": uid,
                        "unit_index": cursor + offset,
                        "batch_index": int(b["batch_index"]),
                        "unit_translation_hash": b["unit_hashes"].get(uid, ""),
                        "config_hash": config_hash,
                        "status": "completed",
                        "provider_label": b_rec.get("provider_label", ""),
                        "error": "",
                    }
                else:
                    if next_b_idx is None:
                        next_b_idx = int(b["batch_index"])
                        next_u_idx = cursor + offset
                        next_u_id = uid
                    if st_b == "failed":
                        failed_u += 1
                    elif st_b == "outdated":
                        outdated_u += 1
                    view["units"][uid] = {
                        "unit_id": uid,
                        "unit_index": cursor + offset,
                        "batch_index": int(b["batch_index"]),
                        "unit_translation_hash": b["unit_hashes"].get(uid, ""),
                        "config_hash": config_hash,
                        "status": st_b,
                        "error": b_rec.get("error", ""),
                    }
            cursor += len(b["unit_ids"])

        if next_b_idx is None:
            next_b_idx = len(batches_spec)
            next_u_idx = view["total_units"]
            next_u_id = ""

        view["completed_units"] = [uid for b in batches_spec for uid in b["unit_ids"] if uid in completed_units_set]
        view["completed_count"] = len(completed_units_set)
        view["missing_count"] = max(0, view["total_units"] - len(completed_units_set))
        view["failed_count"] = failed_u
        view["outdated_count"] = outdated_u
        view["next_batch_index"] = next_b_idx
        view["next_unit_index"] = next_u_idx
        view["next_unit_id"] = next_u_id
        view["status"] = job_status
        view["updated_at"] = _now_iso()
        art["script_progress"] = view
        self._save_raw_state(state)
        return view

    def save_chapter_script(
        self,
        book_id: str,
        chapter_index: int,
        script_record: Dict[str, Any],
    ) -> None:
        if not script_record.get("is_complete"):
            raise ValueError("אסור לשמור תסריט חלקי כרשומה שלמה.")
        lines = script_record.get("lines") or []
        if not lines:
            raise ValueError("אסור לשמור תסריט ריק.")

        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.setdefault("chapter_artifacts", {}).setdefault(ck, {})
        prev_s = art.get("script")
        if isinstance(prev_s, dict) and prev_s.get("is_complete"):
            if (
                prev_s.get("source_hash") != script_record.get("source_hash")
                or prev_s.get("translation_hash") != script_record.get("translation_hash")
                or prev_s.get("config_hash") != script_record.get("config_hash")
            ):
                shist = art.setdefault("script_history", [])
                shist.append(deepcopy(prev_s))
                if len(shist) > 10:
                    del shist[:-10]

        rec = deepcopy(script_record)
        rec["saved_at"] = _now_iso()
        art["script"] = rec
        if isinstance(art.get("script_progress"), dict):
            art["script_progress"]["status"] = "completed"
            art["script_progress"]["updated_at"] = rec["saved_at"]
        art.pop("last_export", None)
        self._save_raw_state(state)

    def save_chapter_export(
        self,
        book_id: str,
        chapter_index: int,
        export_record: Dict[str, Any],
    ) -> None:
        if not export_record.get("success"):
            raise ValueError("אסור לשמור רשומת יצוא שנכשלה או חלקית כהצלחה.")
        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.setdefault("chapter_artifacts", {}).setdefault(ck, {})
        rec = deepcopy(export_record)
        rec["saved_at"] = _now_iso()
        art["last_export"] = rec
        self._save_raw_state(state)

    def invalidate_chapter_artifacts(
        self,
        book_id: str,
        chapter_index: int,
        keep_translation: bool = False,
    ) -> None:
        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.get("chapter_artifacts", {}).get(ck)
        if not art:
            return
        if not keep_translation:
            prev_t = art.pop("translation", None)
            if isinstance(prev_t, dict) and prev_t.get("is_complete"):
                art.setdefault("translation_history", []).append(deepcopy(prev_t))
        prev_s = art.pop("script", None)
        if isinstance(prev_s, dict) and prev_s.get("is_complete"):
            art.setdefault("script_history", []).append(deepcopy(prev_s))
        art.pop("last_export", None)
        self._save_raw_state(state)

    def restore_previous_translation_version(self, book_id: str, chapter_index: int) -> Optional[Dict[str, Any]]:
        """משחזר את גרסת התרגום השלמה הקודמת שנשמרה בארכיון."""
        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.get("chapter_artifacts", {}).get(ck)
        if not isinstance(art, dict):
            return None
        hist = art.get("translation_history") or []
        if not hist:
            return None
        prev_rec = hist.pop()
        if isinstance(art.get("translation"), dict):
            hist.append(art["translation"])
        art["translation"] = deepcopy(prev_rec)
        art["translation_history"] = hist[-10:]
        self._save_raw_state(state)
        return deepcopy(prev_rec)

    @staticmethod
    def is_translation_fresh(
        translation_record: Optional[Dict[str, Any]],
        current_source_hash: str,
        current_translation_config_hash: str,
    ) -> bool:
        if not translation_record or not isinstance(translation_record, dict):
            return False
        if not translation_record.get("is_complete"):
            return False
        if translation_record.get("source_hash") != current_source_hash:
            return False
        if translation_record.get("config_hash") != current_translation_config_hash:
            return False
        src_n = int(translation_record.get("sentence_count_source", 0))
        dst_s = translation_record.get("translated_sentences") or []
        if src_n <= 0 or len(dst_s) != src_n:
            return False
        if any(not (s or "").strip() for s in dst_s):
            return False
        if not (translation_record.get("translated_text") or "").strip():
            return False
        return True

    @staticmethod
    def is_script_fresh(
        script_record: Optional[Dict[str, Any]],
        current_source_hash: str,
        expected_translation_hash: str,
        current_script_config_hash: str,
    ) -> bool:
        if not script_record or not isinstance(script_record, dict):
            return False
        if not script_record.get("is_complete"):
            return False
        if script_record.get("source_hash") != current_source_hash:
            return False
        if script_record.get("translation_hash") != expected_translation_hash:
            return False
        if script_record.get("config_hash") != current_script_config_hash:
            return False
        lines = script_record.get("lines") or []
        if not lines or any(not (l.get("text") or "").strip() for l in lines if isinstance(l, dict)):
            return False
        expected_units = int(script_record.get("unit_count", 0))
        covered_units = script_record.get("covered_units") or []
        if expected_units > 0 and len(set(covered_units)) < expected_units:
            return False
        return True

    def save_chapter_tts_cache(
        self,
        book_id: str,
        chapter_index: int,
        tts_record: Dict[str, Any],
    ) -> None:
        if not tts_record.get("is_complete") or not tts_record.get("audio_path"):
            raise ValueError("אסור לשמור מטמון TTS חלקי או ללא נתיב קובץ.")
        state = self.load_raw_state()
        ck = chapter_key(book_id, chapter_index)
        art = state.setdefault("chapter_artifacts", {}).setdefault(ck, {})
        rec = deepcopy(tts_record)
        rec["saved_at"] = _now_iso()
        art["tts_cache"] = rec
        self._save_raw_state(state)

    @staticmethod
    def is_chapter_tts_fresh(
        tts_record: Optional[Dict[str, Any]],
        expected_source_hash: str,
        expected_translation_hash: str,
        expected_script_config_hash: str,
        expected_tts_config_hash: str,
    ) -> bool:
        if not tts_record or not isinstance(tts_record, dict):
            return False
        if not tts_record.get("is_complete"):
            return False
        if tts_record.get("source_hash") != expected_source_hash:
            return False
        if tts_record.get("translation_hash") != expected_translation_hash:
            return False
        if tts_record.get("script_config_hash") != expected_script_config_hash:
            return False
        if tts_record.get("tts_config_hash") != expected_tts_config_hash:
            return False
        ap = tts_record.get("audio_path") or ""
        if not ap or not os.path.exists(ap) or os.path.getsize(ap) <= 0:
            return False
        return True

    def save_whole_book_job(self, book_id: str, job_data: Dict[str, Any]) -> None:
        state = self.load_raw_state()
        jobs = state.setdefault("whole_book_jobs", {})
        rec = deepcopy(job_data)
        rec["updated_at"] = _now_iso()
        jobs[book_id] = rec
        self._save_raw_state(state)

    def get_whole_book_job(self, book_id: str) -> Optional[Dict[str, Any]]:
        state = self.load_raw_state()
        rec = (state.get("whole_book_jobs") or {}).get(book_id)
        return deepcopy(rec) if isinstance(rec, dict) else None
