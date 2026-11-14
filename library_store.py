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


def compute_translation_config_hash(
    settings: Dict[str, Any],
    engine: str,
    source_lang: str,
    target_lang: str,
) -> str:
    trans_cfg = (settings or {}).get("translation", {})
    payload: Dict[str, Any] = {
        "engine": (engine or "google_free").strip(),
        "source_lang": (source_lang or "auto").strip(),
        "target_lang": (target_lang or "he").strip(),
    }
    if payload["engine"] == "libretranslate":
        payload["libretranslate_url"] = (trans_cfg.get("libretranslate_url") or "").strip()
    elif payload["engine"] == "ai":
        pid = (settings or {}).get("active_provider", "gemini")
        pconf = ((settings or {}).get("providers") or {}).get(pid, {})
        payload["ai_provider"] = pid
        payload["ai_model"] = (pconf.get("model") or "").strip()
        payload["ai_base_url"] = (pconf.get("base_url") or "").strip()
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
    מנהל התמדה מלא לספרים, טקסטים, תרגומים, תסריטים והתקדמות קריאה.
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
        rec = deepcopy(translation_record)
        rec["saved_at"] = _now_iso()
        art["translation"] = rec
        # תרגום חדש פוסל כל תסריט ישן שאינו תואם לתרגום החדש
        existing_script = art.get("script")
        if existing_script and existing_script.get("translation_hash") != rec.get("translated_hash"):
            art.pop("script", None)
            art.pop("last_export", None)
        self._save_raw_state(state)

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
        rec = deepcopy(script_record)
        rec["saved_at"] = _now_iso()
        art["script"] = rec
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
            art.pop("translation", None)
        art.pop("script", None)
        art.pop("last_export", None)
        self._save_raw_state(state)

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
