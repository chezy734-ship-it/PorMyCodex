# -*- coding: utf-8 -*-
"""
חלון הממשק הראשי (MainWindow) של תוכנת PorMyCodex ב-PyQt6.
כולל:
- טעינת קבצי PDF / DOCX / TXT גדולים ברקע (QThread) עם חלוקה אוטומטית לחלקים ורינדור מוגן קריסות
- יצירת תסריט AI (לימודי מורה-תלמיד / דרמטי) והקראה רב-קולית (אברי + הילה)
- תרגום פרקים עם AI או מנועים חינמיים (Google Translate / MyMemory / LibreTranslate)
- בחירת ספקי AI מרובים (Google / OpenAI / Claude / OpenRouter / Groq / שרת מותאם) וגילוי מודלים
"""

from copy import deepcopy
import os
import html
import json
import threading
from typing import List, Dict, Any, Optional, Callable
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QPushButton, QComboBox, QSlider, QProgressBar,
    QListWidget, QListWidgetItem, QTextBrowser, QFileDialog,
    QMessageBox, QToolBar, QStatusBar, QFrame, QInputDialog,
    QProgressDialog, QCheckBox
)
from PyQt6.QtGui import QAction, QFont, QKeySequence, QShortcut

from book_loader import (
    SAMPLE_BOOKS,
    load_file_content,
    resegment_book,
    segment_text_smartly,
    split_into_sentences,
    validate_book_completeness,
)
from audio_engine import AudioEngine
from audio_exporter import find_ffmpeg_executable
from ai_linguist import vocalize_and_expand_hebrew, ask_companion
from settings_manager import load_settings, save_settings, get_active_provider_config
from ai_providers import AICancelledError, get_provider_label
from script_engine import format_script_as_text
from wiki_engine import enrich_with_wikipedia_and_ai, fetch_wikipedia_context_for_terms
from library_store import LibraryStore, sanitize_filename_part
from export_pipeline import (
    PipelineCancelledError,
    assess_book_pipeline,
    assess_chapter_pipeline,
    ensure_translation_for_chapter,
    ensure_script_for_chapter,
    run_enforced_export_pipeline,
    run_whole_book_script_pipeline,
    run_whole_book_export_pipeline,
)
from dialogs import (
    SettingsDialog,
    TranslateDialog,
    ScriptStudioDialog,
    WikiSearchDialog,
    ExportPipelineDialog,
    BookRecoveryDialog,
    SegmentBookDialog,
)


_WINDOW_THREAD_LOCK = threading.Lock()
_RETIRED_WINDOW_THREADS: List[QThread] = []


def _retire_window_thread(th: Optional[QThread], *, cancel: bool = False, wait_ms: int = 200) -> None:
    """
    שומר הפניה חזקה לכל QThread שעדיין רץ עד סיומו האמיתי כדי למנוע קריסת C++
    ('QThread: Destroyed while thread is still running').
    """
    if th is None:
        return
    if cancel and hasattr(th, "cancel"):
        try:
            th.cancel()
        except Exception:
            pass
    try:
        if th.isRunning():
            th.wait(max(0, int(wait_ms)))
    except Exception:
        pass
    try:
        still_running = th.isRunning()
    except Exception:
        still_running = False
    if still_running:
        with _WINDOW_THREAD_LOCK:
            if th not in _RETIRED_WINDOW_THREADS:
                _RETIRED_WINDOW_THREADS.append(th)

        def _drop(t=th):
            with _WINDOW_THREAD_LOCK:
                if t in _RETIRED_WINDOW_THREADS:
                    _RETIRED_WINDOW_THREADS.remove(t)

        try:
            th.finished.connect(_drop)
        except Exception:
            pass


def _safe_close_progress_dialog(dlg: Optional[QProgressDialog]) -> None:
    """סוגר QProgressDialog בלי לגרום ל-signal 'canceled' לירות בזמן סיום תקין."""
    if dlg is None:
        return
    try:
        dlg.canceled.disconnect()
    except Exception:
        pass
    try:
        dlg.close()
    except Exception:
        pass


class PipelineExportThread(QThread):
    progress = pyqtSignal(str)
    finished_result = pyqtSignal(dict)

    def __init__(self, pipeline_kwargs: Dict[str, Any], scope: str = "chapter"):
        super().__init__(None)
        self.scope = scope
        self.cancel_event = threading.Event()
        self.pipeline_kwargs = dict(pipeline_kwargs)
        self.pipeline_kwargs["cancel_event"] = self.cancel_event
        self.pipeline_kwargs["progress_cb"] = lambda msg: self.progress.emit(msg)

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            if self.scope == "book":
                res = run_whole_book_export_pipeline(**self.pipeline_kwargs)
            else:
                res = run_enforced_export_pipeline(**self.pipeline_kwargs)
            self.finished_result.emit(res)
        except Exception as exc:
            self.finished_result.emit({
                "success": False,
                "status": "cancelled" if self.cancel_event.is_set() else "failed",
                "scope": self.scope,
                "stage": "export",
                "error": str(exc),
            })


class WholeBookScriptThread(QThread):
    progress = pyqtSignal(str)
    finished_result = pyqtSignal(dict)

    def __init__(self, pipeline_kwargs: Dict[str, Any]):
        super().__init__(None)
        self.cancel_event = threading.Event()
        self.pipeline_kwargs = dict(pipeline_kwargs)
        self.pipeline_kwargs["cancel_event"] = self.cancel_event
        self.pipeline_kwargs["progress_cb"] = lambda msg: self.progress.emit(msg)

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            res = run_whole_book_script_pipeline(**self.pipeline_kwargs)
            self.finished_result.emit(res)
        except Exception as exc:
            self.finished_result.emit({
                "success": False,
                "status": "cancelled" if self.cancel_event.is_set() else "failed",
                "scope": "book",
                "stage": "script",
                "error": str(exc),
            })


class FileLoadThread(QThread):
    progress = pyqtSignal(str)
    finished_book = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path

    def run(self):
        try:
            book = load_file_content(self.file_path, progress_cb=lambda msg: self.progress.emit(msg))
            self.finished_book.emit(book)
        except Exception as e:
            self.failed.emit(str(e))


class TaskWorkerThread(QThread):
    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    cancelled = pyqtSignal(str)
    failed = pyqtSignal(str)
    failed_details = pyqtSignal(dict)

    def __init__(self, fn: Callable[[threading.Event, Callable[[str], None]], Any]):
        super().__init__()
        self.fn = fn
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            res = self.fn(self.cancel_event, lambda msg: self.progress.emit(msg))
            if self.cancel_event.is_set():
                self.cancelled.emit("הפעולה בוטלה לבקשת המשתמש.")
            else:
                self.finished_ok.emit(res)
        except (AICancelledError, PipelineCancelledError) as e:
            self.cancelled.emit(str(e))
        except Exception as e:
            if self.cancel_event.is_set():
                self.cancelled.emit("הפעולה בוטלה לבקשת המשתמש.")
            else:
                msg = str(e)
                low = msg.lower()
                if "<html" in low or "<!doctype" in low:
                    msg = "התקבלה תשובת HTML/חסימה מהספק במקום נתוני תרגום תקינים."
                details = {
                    "error": msg,
                    "is_blocked": bool(
                        getattr(e, "is_blocked", False)
                        or getattr(e, "category", "") == "blocked"
                        or getattr(e, "status_code", None) in (418, 451)
                        or "netfree" in low
                        or "418" in low
                    ),
                    "blocked_provider": str(getattr(e, "provider_id", "") or ""),
                    "blocked_filter": str(getattr(e, "blocked_filter", "") or ""),
                    "unit_index": getattr(e, "unit_index", None),
                    "unit_id": str(getattr(e, "unit_id", "") or ""),
                    "batch_index": getattr(e, "batch_index", None),
                    "category": str(getattr(e, "category", "") or "error"),
                }
                self.failed_details.emit(details)
                self.failed.emit(msg)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PorMyCodex - קורא ספרים חכם בעברית (AI + תסריטים + תרגום)")
        self.resize(1240, 820)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

        # Persistent Store & State
        self.store = LibraryStore()
        self.settings: Dict[str, Any] = load_settings()
        loaded_state = self.store.load_library(SAMPLE_BOOKS)
        self.books: List[Dict[str, Any]] = loaded_state.get("books") or list(SAMPLE_BOOKS)
        prog = loaded_state.get("reading_progress") or {}
        self._restoring_state = True

        self.current_book_index: int = 0
        self.current_chapter_index: int = 0
        self.sentences: List[str] = []
        self.speaker_roles: List[str] = []       # תפקיד דובר לכל משפט (ריק במצב רגיל)
        self.speaker_labels: List[str] = []      # תווית תצוגה (🎓 מורה / 🙋 תלמיד / 🎙️ מספר)
        self.current_script_data: Optional[Dict[str, Any]] = None
        self.active_sentence_index: int = 0
        self.font_size: int = int(prog.get("font_size", 18) or 18)

        self._file_thread: Optional[FileLoadThread] = None
        self._task_thread: Optional[TaskWorkerThread] = None
        self._task_progress_dlg: Optional[QProgressDialog] = None
        self._whole_book_script_thread: Optional[WholeBookScriptThread] = None
        self._whole_book_script_progress_dlg: Optional[QProgressDialog] = None
        self._export_thread: Optional[PipelineExportThread] = None
        self._export_progress_dlg: Optional[QProgressDialog] = None
        self._last_whole_book_script_params: Optional[Dict[str, Any]] = None
        self._last_whole_book_export_params: Optional[Dict[str, Any]] = None
        self._auto_advancing_chapter: bool = False
        self._selecting_book: bool = False
        self._resume_banner_cache_key: Optional[tuple] = None
        self._progress_dirty: bool = False

        # טיימר Debounce לשמירת התקדמות קריאה (מונע כתיבות דיסק חוסמות בכל לחיצה/גרירת סליידר)
        self._progress_save_timer = QTimer(self)
        self._progress_save_timer.setSingleShot(True)
        self._progress_save_timer.setInterval(450)
        self._progress_save_timer.timeout.connect(self._flush_progress_save)

        # Audio Engine
        self.audio = AudioEngine()
        self.audio.voice_type = prog.get("voice_type", "edge_avri") or "edge_avri"
        self.audio.speed = float(prog.get("speed", 1.0) or 1.0)
        self.audio.sentence_changed.connect(self.on_sentence_changed)
        self.audio.chapter_completed.connect(self._on_chapter_audio_completed)
        self.audio.state_changed.connect(self.on_playback_state_changed)
        self.audio.error_occurred.connect(self.on_audio_error)

        self.init_ui()

        # שחזור הגדרות נגן ותנוחת קריאה אחרונה
        v_idx = self.combo_voice.findData(self.audio.voice_type)
        if v_idx >= 0:
            self.combo_voice.blockSignals(True)
            self.combo_voice.setCurrentIndex(v_idx)
            self.combo_voice.blockSignals(False)
        self.slider_speed.blockSignals(True)
        self.slider_speed.setValue(max(75, min(200, int(round(self.audio.speed * 100)))))
        self.slider_speed.blockSignals(False)
        self.lbl_speed_val.setText(f"{self.audio.speed:.1f}x")

        restore_book_idx = 0
        target_book_id = prog.get("active_book_id", "")
        if target_book_id:
            for idx, b in enumerate(self.books):
                if b.get("id") == target_book_id:
                    restore_book_idx = idx
                    break
        restore_ch_idx = int(prog.get("active_chapter_index", 0) or 0)
        restore_sent_idx = int(prog.get("active_sentence_index", 0) or 0)

        self._restore_initial_position(restore_book_idx, restore_ch_idx, restore_sent_idx)
        self._restoring_state = False
        self._refresh_provider_badge()

        # Keyboard shortcuts
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.toggle_play)
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, self.prev_sentence)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, self.next_sentence)

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Top Toolbar (compact labels so all buttons stay visible without horizontal clipping)
        toolbar = QToolBar("פעולות ספרייה ו-AI")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        open_action = QAction("📂 פתח ספר", self)
        open_action.setToolTip("פתח ספר או מסמך (PDF / Word / TXT)")
        open_action.triggered.connect(self.open_file_dialog)
        toolbar.addAction(open_action)

        paste_action = QAction("📋 הדבק", self)
        paste_action.setToolTip("הדבק טקסט חופשי לקריאה")
        paste_action.triggered.connect(self.paste_custom_text)
        toolbar.addAction(paste_action)

        self.action_resegment_book = QAction("✂️ פיצול מקטעים", self)
        self.action_resegment_book.setToolTip("פצל מחדש את הספר למקטעים נוחים לפי גודל ונושא")
        self.action_resegment_book.triggered.connect(self.open_resegment_dialog)
        toolbar.addAction(self.action_resegment_book)

        settings_action = QAction("⚙️ הגדרות AI ומודלים", self)
        settings_action.setToolTip("בחר חברת AI, הזן מפתח API ושאב מודלים זמינים")
        settings_action.triggered.connect(self.open_settings_dialog)
        toolbar.addAction(settings_action)

        toolbar.addSeparator()

        lbl_scope = QLabel("היקף:")
        lbl_scope.setStyleSheet("padding: 0 4px; color: #94a3b8; font-weight: bold;")
        toolbar.addWidget(lbl_scope)

        self.combo_scope = QComboBox()
        self.combo_scope.setObjectName("scopeCombo")
        self.combo_scope.setToolTip("בחר אם ליצור תסריט/לשמור שמע עבור הפרק הנוכחי בלבד או עבור הספר כולו לפי הסדר")
        self.combo_scope.addItem("📖 הפרק הנוכחי", "chapter")
        self.combo_scope.addItem("📚 הספר כולו", "book")
        toolbar.addWidget(self.combo_scope)

        toolbar.addSeparator()

        vocalize_action = QAction("✨ נקד AI", self)
        vocalize_action.triggered.connect(self.vocalize_current_chapter)
        toolbar.addAction(vocalize_action)

        script_action = QAction("🎬 תסריט AI", self)
        script_action.triggered.connect(lambda _checked=False: self.open_script_studio())
        toolbar.addAction(script_action)

        self.action_script_whole_book = QAction("📚🎬 תסריט לכל הספר", self)
        self.action_script_whole_book.setToolTip("צור תרגום ותסריט AI מלאים לכל פרקי הספר לפי הסדר")
        self.action_script_whole_book.triggered.connect(lambda _checked=False: self.open_whole_book_script_dialog())
        toolbar.addAction(self.action_script_whole_book)

        translate_action = QAction("🌐 תרגום", self)
        translate_action.triggered.connect(self.open_translate_dialog)
        toolbar.addAction(translate_action)

        export_audio_action = QAction("🎧 ייצא שמע", self)
        export_audio_action.setToolTip("ייצא קובץ שמע MP3 / WAV או ספר שלם להעברה")
        export_audio_action.triggered.connect(lambda _checked=False: self.export_audio_dialog())
        toolbar.addAction(export_audio_action)

        self.action_export_whole_book = QAction("📚🎧 שמע לכל הספר", self)
        self.action_export_whole_book.setToolTip("ייצא את כל פרקי הספר לשמע (ZIP / תיקייה / WAV / MP3 מאוחד)")
        self.action_export_whole_book.triggered.connect(lambda _checked=False: self.export_whole_book_audio_dialog())
        toolbar.addAction(self.action_export_whole_book)

        self.action_resume_book = QAction("🔄 המשך מטמון", self)
        self.action_resume_book.setToolTip("המשך עיבוד ספר שנעצר / תקן פרקים שנכשלו בלי ליצור מחדש פרקים תקינים")
        self.action_resume_book.triggered.connect(lambda _checked=False: self.resume_whole_book_job())
        toolbar.addAction(self.action_resume_book)

        toolbar.addSeparator()

        explain_action = QAction("💡 באר", self)
        explain_action.triggered.connect(lambda: self.trigger_companion("explain"))
        toolbar.addAction(explain_action)

        summary_action = QAction("📝 סכם", self)
        summary_action.triggered.connect(lambda: self.trigger_companion("summary"))
        toolbar.addAction(summary_action)

        quiz_action = QAction("❓ שאלות", self)
        quiz_action.triggered.connect(lambda: self.trigger_companion("quiz"))
        toolbar.addAction(quiz_action)

        wiki_action = QAction("📚 ויקיפדיה", self)
        wiki_action.setToolTip("חפש מושגים ספציפיים בוויקיפדיה להרחבת ידע של מודל ה-AI")
        wiki_action.triggered.connect(self.open_wikipedia_dialog)
        toolbar.addAction(wiki_action)

        # Splitter Layout
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter, 1)

        # --- Sidebar (Library & Chapters) ---
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 12, 12, 12)
        sidebar_layout.setSpacing(8)

        lbl_books = QLabel("📚 ספריית ספרים")
        lbl_books.setObjectName("sectionLabel")
        sidebar_layout.addWidget(lbl_books)

        self.book_list = QListWidget()
        self.book_list.currentRowChanged.connect(self.on_book_selected)
        sidebar_layout.addWidget(self.book_list, 2)

        lbl_chapters = QLabel("📑 פרקים / חלקים")
        lbl_chapters.setObjectName("sectionLabel")
        sidebar_layout.addWidget(lbl_chapters)

        self.chapter_list = QListWidget()
        self.chapter_list.currentRowChanged.connect(self.on_chapter_selected)
        sidebar_layout.addWidget(self.chapter_list, 3)

        splitter.addWidget(sidebar)

        # --- Center: Reading Display ---
        center_widget = QWidget()
        center_layout = QVBoxLayout(center_widget)
        center_layout.setContentsMargins(16, 14, 16, 14)
        center_layout.setSpacing(8)

        header_row = QHBoxLayout()
        self.lbl_current_title = QLabel("כותרת ספר")
        self.lbl_current_title.setObjectName("titleLabel")
        header_row.addWidget(self.lbl_current_title)

        header_row.addStretch()

        self.btn_ai_badge = QPushButton("")
        self.btn_ai_badge.setObjectName("settingsBadgeBtn")
        self.btn_ai_badge.setToolTip("לחץ לפתיחת הגדרות AI, הזנת API ובחירת מודל")
        self.btn_ai_badge.clicked.connect(self.open_settings_dialog)
        header_row.addWidget(self.btn_ai_badge)

        self.btn_restore_original = QPushButton("↩ חזור למקור")
        self.btn_restore_original.setVisible(False)
        self.btn_restore_original.clicked.connect(self.restore_original_chapter_text)
        header_row.addWidget(self.btn_restore_original)

        self.btn_export_script = QPushButton("💾 שמור תסריט")
        self.btn_export_script.setVisible(False)
        self.btn_export_script.clicked.connect(self.export_current_script)
        header_row.addWidget(self.btn_export_script)

        btn_font_plus = QPushButton("A+")
        btn_font_plus.setFixedWidth(40)
        btn_font_plus.clicked.connect(lambda: self.change_font_size(2))
        header_row.addWidget(btn_font_plus)

        btn_font_minus = QPushButton("A-")
        btn_font_minus.setFixedWidth(40)
        btn_font_minus.clicked.connect(lambda: self.change_font_size(-2))
        header_row.addWidget(btn_font_minus)

        center_layout.addLayout(header_row)

        # באנר המשך התקדמות שמורה (מוצג בעת פתיחת ספר עם עבודה חלקית / שנעצרה)
        self.resume_banner_frame = QFrame()
        self.resume_banner_frame.setVisible(False)
        self.resume_banner_frame.setStyleSheet(
            "QFrame { background-color: #1e293b; border: 1px solid #f59e0b; border-radius: 8px; padding: 6px 10px; }"
        )
        rb_lay = QHBoxLayout(self.resume_banner_frame)
        rb_lay.setContentsMargins(8, 4, 8, 4)
        self.lbl_resume_banner = QLabel("")
        self.lbl_resume_banner.setWordWrap(True)
        self.lbl_resume_banner.setStyleSheet("color: #fde68a; font-size: 12px; border: none;")
        rb_lay.addWidget(self.lbl_resume_banner, 1)
        self.btn_resume_banner = QPushButton("▶️ המשך מנקודת העצירה")
        self.btn_resume_banner.setObjectName("primaryBtn")
        self.btn_resume_banner.clicked.connect(self.resume_whole_book_job)
        rb_lay.addWidget(self.btn_resume_banner)
        center_layout.addWidget(self.resume_banner_frame)

        self.text_browser = QTextBrowser()
        self.text_browser.setOpenExternalLinks(False)
        self.text_browser.setFont(QFont("FrankRuehl", self.font_size))
        center_layout.addWidget(self.text_browser, 1)

        splitter.addWidget(center_widget)

        # --- Right: AI Companion / Translation / Script Drawer ---
        self.companion_widget = QWidget()
        comp_layout = QVBoxLayout(self.companion_widget)
        comp_layout.setContentsMargins(12, 12, 12, 12)
        comp_layout.setSpacing(8)

        comp_header = QHBoxLayout()
        self.lbl_comp_title = QLabel("🤖 עוזר ספרותי AI")
        self.lbl_comp_title.setObjectName("titleLabel")
        comp_header.addWidget(self.lbl_comp_title)

        btn_close_comp = QPushButton("✕")
        btn_close_comp.setFixedWidth(30)
        btn_close_comp.clicked.connect(lambda: self.companion_widget.setVisible(False))
        comp_header.addWidget(btn_close_comp)
        comp_layout.addLayout(comp_header)

        self.comp_text = QTextBrowser()
        self.comp_text.setOpenExternalLinks(True)
        self.comp_text.setFont(QFont("Arial", 12))
        comp_layout.addWidget(self.comp_text)

        splitter.addWidget(self.companion_widget)
        self.companion_widget.setVisible(False)

        splitter.setSizes([260, 680, 300])

        # --- Bottom Player Bar ---
        player_bar = QFrame()
        player_bar.setObjectName("playerBar")
        player_layout = QHBoxLayout(player_bar)
        player_layout.setContentsMargins(16, 8, 16, 8)
        player_layout.setSpacing(14)

        self.btn_prev = QPushButton("⏮")
        self.btn_prev.setFixedSize(38, 38)
        self.btn_prev.clicked.connect(self.prev_sentence)
        player_layout.addWidget(self.btn_prev)

        self.btn_play = QPushButton("▶")
        self.btn_play.setObjectName("playBtn")
        self.btn_play.setFixedSize(50, 50)
        self.btn_play.clicked.connect(self.toggle_play)
        player_layout.addWidget(self.btn_play)

        self.btn_next = QPushButton("⏭")
        self.btn_next.setFixedSize(38, 38)
        self.btn_next.clicked.connect(self.next_sentence)
        player_layout.addWidget(self.btn_next)

        self.lbl_progress = QLabel("משפט 0 מתוך 0")
        self.lbl_progress.setFixedWidth(185)
        player_layout.addWidget(self.lbl_progress)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setValue(0)
        player_layout.addWidget(self.progress_bar, 1)

        self.chk_continuous_book = QCheckBox("🔁 רציף לכל הספר")
        self.chk_continuous_book.setToolTip("עבור אוטומטית למקטע/פרק הבא בסיום ההקראה כדי להקריא את הספר כולו ברצף")
        self.chk_continuous_book.setChecked(True)
        player_layout.addWidget(self.chk_continuous_book)

        player_layout.addWidget(QLabel("קול:"))
        self.combo_voice = QComboBox()
        self.combo_voice.addItem("קול עברית - אברי (Edge-TTS)", "edge_avri")
        self.combo_voice.addItem("קול עברית - הילה (Edge-TTS)", "edge_hila")
        self.combo_voice.addItem("מנוע מקומי Windows (SAPI5)", "sapi5")
        self.combo_voice.currentIndexChanged.connect(self.on_voice_changed)
        player_layout.addWidget(self.combo_voice)

        player_layout.addWidget(QLabel("מהירות:"))
        self.slider_speed = QSlider(Qt.Orientation.Horizontal)
        self.slider_speed.setRange(75, 200)
        self.slider_speed.setValue(100)
        self.slider_speed.setFixedWidth(90)
        self.slider_speed.valueChanged.connect(self.on_speed_changed)
        player_layout.addWidget(self.slider_speed)

        self.lbl_speed_val = QLabel("1.0x")
        self.lbl_speed_val.setFixedWidth(35)
        player_layout.addWidget(self.lbl_speed_val)

        main_layout.addWidget(player_bar)

        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.statusBar.showMessage("מוכן לקריאה. לחץ רווח כדי להתחיל להקריא.")

    def _refresh_provider_badge(self):
        pconf = get_active_provider_config(self.settings)
        pid = pconf["provider_id"]
        model = pconf.get("model") or "לא נבחר"
        has_key = bool(pconf.get("api_key")) or pid == "custom"
        icon = "🟢" if has_key else "🟡"
        self.btn_ai_badge.setText(f"⚙️ {icon} {get_provider_label(pid)} | {model} (שינוי)")

    def _get_current_book(self) -> Optional[Dict[str, Any]]:
        if not self.books:
            return None
        if not (0 <= self.current_book_index < len(self.books)):
            self.current_book_index = 0
        return self.books[self.current_book_index]

    def _get_current_book_and_chapter(self):
        book = self._get_current_book()
        if not book:
            return None, None
        chapters = book.get("chapters") or []
        if not chapters:
            return book, None
        if not (0 <= self.current_chapter_index < len(chapters)):
            self.current_chapter_index = 0
        return book, chapters[self.current_chapter_index]

    def _refresh_resume_banner(self, force: bool = False):
        if not hasattr(self, "resume_banner_frame"):
            return
        book = self._get_current_book()
        if not book:
            self.resume_banner_frame.setVisible(False)
            self._resume_banner_cache_key = None
            return
        try:
            book_id = book.get("id", "")
            chapters = book.get("chapters") or []
            pconf = get_active_provider_config(self.settings)
            t_cfg = self.settings.get("translation", {})
            cache_key = (
                book_id,
                len(chapters),
                self.store.get_state_signature(),
                pconf.get("provider_id"),
                pconf.get("model"),
                t_cfg.get("source_lang"),
                t_cfg.get("target_lang"),
            )
            if not force and self._resume_banner_cache_key == cache_key:
                return
            self._resume_banner_cache_key = cache_key

            job = self.store.get_whole_book_job(book_id) or {}
            assess = assess_book_pipeline(book=book, settings=self.settings, store=self.store)
            total = assess["chapter_count"]
            t_ready = assess["translations_ready"]
            s_ready = assess["scripts_ready"]
            u_done = assess.get("units_completed", 0)
            u_tot = assess.get("total_translation_units", 0)
            rp = assess.get("resume_point") or {}
            job_status = job.get("status", "")
            has_failed = bool(job.get("failed_chapters"))
            is_stopped_job = job_status in ("failed", "blocked", "cancelled", "partial", "in_progress")
            show = bool(
                (assess.get("has_resumable_progress") or is_stopped_job or has_failed)
                and not (assess.get("all_translations_ready") and assess.get("all_scripts_ready") and not has_failed)
            )
            if not show:
                self.resume_banner_frame.setVisible(False)
                return

            if is_stopped_job and job_status == "blocked":
                ch_n = job.get("failed_or_cancelled_chapter") or rp.get("chapter_number", 1)
                uid = job.get("unit_id") or rp.get("unit_id") or ""
                u_txt = f" ({uid})" if uid else ""
                msg = (
                    f"⛔ <b>נמצאה התקדמות שמורה שנעצרה עקב חסימה</b> בפרק {ch_n}/{total}{u_txt} — "
                    f"נשמרו אטומית <b>{u_done}/{u_tot}</b> יחידות תקינות ({t_ready}/{total} פרקים)."
                )
            else:
                rp_txt = ""
                if rp:
                    rp_txt = f" | נקודת המשך: פרק {rp.get('chapter_number', 1)}/{total} ({rp.get('unit_id', 'u1')})"
                msg = (
                    f"💾 <b>נמצאה התקדמות שמורה לספר:</b> תרגומים {t_ready}/{total} | תסריטים {s_ready}/{total} | "
                    f"יחידות תקינות {u_done}/{u_tot}{rp_txt}."
                )
            self.lbl_resume_banner.setText(msg)
            self.resume_banner_frame.setVisible(True)
        except Exception:
            self.resume_banner_frame.setVisible(False)

    def _restore_initial_position(self, book_idx: int, chapter_idx: int, sentence_idx: int):
        self.load_book_to_ui(book_idx)
        book = self._get_current_book()
        if not book:
            return
        chapters = book.get("chapters") or []
        if 0 <= chapter_idx < len(chapters):
            self.chapter_list.setCurrentRow(chapter_idx)
        if self.sentences:
            self.active_sentence_index = max(0, min(len(self.sentences) - 1, sentence_idx))
            self.render_text()
            self.update_progress_display()
        self._refresh_resume_banner(force=True)

    def _persist_library_and_progress(self):
        if getattr(self, "_restoring_state", False) or not self.books:
            return
        if hasattr(self, "_progress_save_timer"):
            self._progress_save_timer.stop()
        self._progress_dirty = False
        book = self._get_current_book()
        if not book:
            return
        try:
            self.store.save_books_and_progress(
                self.books,
                {
                    "active_book_id": book.get("id", ""),
                    "active_chapter_index": self.current_chapter_index,
                    "active_sentence_index": self.active_sentence_index,
                    "font_size": self.font_size,
                    "voice_type": self.combo_voice.currentData() or self.audio.voice_type,
                    "speed": self.audio.speed,
                },
            )
        except Exception:
            pass

    def _flush_progress_save(self):
        if getattr(self, "_restoring_state", False) or not self.books:
            self._progress_dirty = False
            return
        book = self._get_current_book()
        if not book:
            self._progress_dirty = False
            return
        self._progress_dirty = False
        try:
            self.store.update_reading_progress(
                book_id=book.get("id", ""),
                chapter_index=self.current_chapter_index,
                sentence_index=self.active_sentence_index,
                font_size=self.font_size,
                voice_type=self.combo_voice.currentData() or self.audio.voice_type,
                speed=self.audio.speed,
            )
        except Exception:
            pass

    def _persist_progress_only(self, *, immediate: bool = False):
        if getattr(self, "_restoring_state", False) or not self.books:
            return
        self._progress_dirty = True
        if immediate or not hasattr(self, "_progress_save_timer"):
            if hasattr(self, "_progress_save_timer"):
                self._progress_save_timer.stop()
            self._flush_progress_save()
        else:
            self._progress_save_timer.start()

    def load_book_to_ui(self, book_idx: int):
        self.book_list.clear()
        for b in self.books:
            self.book_list.addItem(QListWidgetItem(f"📖 {b.get('title', 'ספר')}"))
        if 0 <= book_idx < len(self.books):
            self.book_list.setCurrentRow(book_idx)

    def _format_chapter_list_label(self, book_id: str, idx: int, ch: Dict[str, Any], artifacts_map: Optional[Dict[str, Any]] = None) -> str:
        base = f"{idx + 1}. {ch.get('title', f'פרק {idx + 1}')}"
        arts = artifacts_map if artifacts_map is not None else (self.store._read_state_cached().get("chapter_artifacts") or {})
        art = arts.get(f"{book_id}::{idx}") or {}
        if isinstance(art.get("script"), dict) and art["script"].get("is_complete"):
            return f"🎬 {base}"
        if isinstance(art.get("translation"), dict) and art["translation"].get("is_complete"):
            return f"🌐 {base}"
        return base

    def _refresh_chapter_list_badges(self):
        book = self._get_current_book()
        if not book:
            return
        book_id = book.get("id", "")
        chapters = book.get("chapters") or []
        arts = self.store._read_state_cached().get("chapter_artifacts") or {}
        for idx, ch in enumerate(chapters):
            item = self.chapter_list.item(idx)
            if item is not None:
                item.setText(self._format_chapter_list_label(book_id, idx, ch, arts))

    def on_book_selected(self, row: int):
        if row < 0 or row >= len(self.books):
            return
        self.current_book_index = row
        self.current_chapter_index = 0
        book = self.books[row]
        book_id = book.get("id", "")
        chapters = book.get("chapters") or []
        arts = self.store._read_state_cached().get("chapter_artifacts") or {}
        self._selecting_book = True
        try:
            self.chapter_list.clear()
            for idx, ch in enumerate(chapters):
                self.chapter_list.addItem(self._format_chapter_list_label(book_id, idx, ch, arts))
            if chapters:
                self.chapter_list.setCurrentRow(0)
            else:
                self.sentences = []
                self.speaker_roles = []
                self.speaker_labels = []
                self.current_script_data = None
                self.active_sentence_index = 0
                self.lbl_current_title.setText(book.get("title", ""))
                self.render_text()
                self.update_progress_display()
        finally:
            self._selecting_book = False
        self._refresh_resume_banner(force=True)

    def on_chapter_selected(self, row: int):
        if row < 0:
            return
        book = self._get_current_book()
        if not book:
            return
        chapters = book.get("chapters") or []
        if row >= len(chapters):
            return
        self.current_chapter_index = row
        chapter = chapters[row]
        ch_text = chapter.get("text", "") or ""

        self.lbl_current_title.setText(f"{book.get('title', '')} — {chapter.get('title', f'פרק {row + 1}')}")
        self.sentences = split_into_sentences(ch_text)
        self.speaker_roles = []
        self.speaker_labels = []
        self.current_script_data = None
        self.btn_restore_original.setVisible(False)
        self.btn_export_script.setVisible(False)
        self.active_sentence_index = 0

        # טעינת התקדמות קריאה שמורה לפרק בקריאה קלה (ללא deepcopy של כל הספרייה)
        try:
            prog = self.store.get_reading_progress()
            saved_pos = (prog.get("chapter_positions") or {}).get(f"{book.get('id', '')}::{row}")
            if isinstance(saved_pos, int) and 0 <= saved_pos < len(self.sentences):
                self.active_sentence_index = saved_pos
        except Exception:
            pass

        # אם קיימים תוצרים שמורים לפרק זה (תסריט / תרגום), טוענים ומציגים אותם מיד בקורא
        try:
            status = assess_chapter_pipeline(
                book_id=book.get("id", ""),
                chapter_index=row,
                source_text=ch_text,
                settings=self.settings,
                store=self.store,
            )
            s_rec = status["script_record"] if (status["script_ready"] and status["script_record"]) else None
            used_saved_different_cfg = False
            if s_rec is None:
                # גם אם הפרק הבודד נוצר קודם עם הגדרת ויקיפדיה/סגנון/מודל שונה — משחזרים אותו לקריאה
                # כל עוד טקסט המקור לא השתנה!
                raw_art = self.store.get_chapter_artifacts(book.get("id", ""), row)
                cand_s = raw_art.get("script")
                if (
                    isinstance(cand_s, dict)
                    and cand_s.get("is_complete")
                    and cand_s.get("source_hash") == status["source_hash"]
                    and cand_s.get("lines")
                ):
                    s_rec = cand_s
                    used_saved_different_cfg = True

            if s_rec is not None:
                lines = [ln for ln in (s_rec.get("lines") or []) if isinstance(ln, dict)]
                if lines:
                    self.current_script_data = s_rec
                    self.sentences = [line.get("text", "") for line in lines]
                    self.speaker_roles = [line.get("speakerRole", "") for line in lines]
                    self.speaker_labels = [
                        f"{line.get('speakerLabel', '')} [{line['style']}]" if line.get("style") else line.get("speakerLabel", "")
                        for line in lines
                    ]
                    self.active_sentence_index = min(self.active_sentence_index, max(0, len(self.sentences) - 1))
                    self.btn_restore_original.setVisible(True)
                    self.btn_export_script.setVisible(True)
                    if used_saved_different_cfg:
                        self.statusBar.showMessage(
                            f"נטען תסריט שמור לפרק {row + 1} ({s_rec.get('provider_label', '')}).",
                            4500,
                        )
            elif status["translation_ready"] and status["translation_record"]:
                t_rec = status["translation_record"]
                self.sentences = list(t_rec.get("translated_sentences") or split_into_sentences(t_rec.get("translated_text", "")))
                self.active_sentence_index = min(self.active_sentence_index, max(0, len(self.sentences) - 1))
                self.btn_restore_original.setVisible(True)
        except Exception:
            pass

        self.render_text()
        self.update_progress_display()
        if not self._auto_advancing_chapter:
            self.audio.stop()
        self._persist_progress_only()
        if not self._selecting_book:
            self._refresh_resume_banner()

    def restore_original_chapter_text(self):
        book, chapter = self._get_current_book_and_chapter()
        if not book or not chapter:
            return
        self.audio.stop()
        self.sentences = split_into_sentences(chapter.get("text", "") or "")
        self.speaker_roles = []
        self.speaker_labels = []
        self.current_script_data = None
        self.active_sentence_index = 0
        self.btn_restore_original.setVisible(False)
        self.btn_export_script.setVisible(False)
        self.render_text()
        self.update_progress_display()
        self._persist_progress_only()
        self.statusBar.showMessage("הוצג מחדש טקסט המקור של הפרק.", 3500)

    def render_text(self):
        """
        מציג את הטקסט ב-QTextBrowser עם הגנה מוחלטת מתווים שבורים ב-PDF (html.escape)
        וחלון גלילה (Sliding Window) כדי שספרים ענקיים לעולם לא יקפאו או יקרסו.
        """
        total = len(self.sentences)
        if total == 0:
            self.text_browser.setHtml("<div dir='rtl'><p>אין טקסט להצגה בפרק זה.</p></div>")
            return

        # חלון תצוגה של עד 280 משפטים סביב המשפט הפעיל
        window_size = 280
        if total > window_size:
            start_idx = max(0, self.active_sentence_index - 100)
            end_idx = min(total, start_idx + window_size)
            if end_idx - start_idx < window_size:
                start_idx = max(0, end_idx - window_size)
        else:
            start_idx = 0
            end_idx = total

        html_lines: List[str] = []
        if start_idx > 0 or end_idx < total:
            html_lines.append(
                f'<p style="color: #94a3b8; font-size: 12px; background: #1e293b; padding: 6px 12px; border-radius: 6px;">'
                f'מציג משפטים {start_idx + 1}–{end_idx} מתוך {total} (לביצועים מהירים וללא קריסות)</p>'
            )

        for idx in range(start_idx, end_idx):
            sent = html.escape(self.sentences[idx])
            role = self.speaker_roles[idx] if idx < len(self.speaker_roles) else ""
            role_lbl = html.escape(self.speaker_labels[idx]) if idx < len(self.speaker_labels) else ""
            prefix = ""
            if role_lbl:
                badge_color = "#38bdf8" if role == "teacher" else ("#34d399" if role == "student" else "#fbbf24")
                prefix = f'<span style="color: {badge_color}; font-weight: bold;">{role_lbl}: </span>'

            if idx == self.active_sentence_index:
                html_lines.append(
                    f'<a name="active_sent"></a>'
                    f'<p style="background-color: rgba(245, 158, 11, 0.28); color: #fef3c7; '
                    f'padding: 8px 14px; border-radius: 8px; border-right: 4px solid #f59e0b; '
                    f'margin: 6px 0; font-size: {self.font_size}px; line-height: 1.8;">'
                    f'{prefix}{sent}</p>'
                )
            else:
                html_lines.append(
                    f'<p style="color: #cbd5e1; margin: 4px 0; font-size: {self.font_size}px; '
                    f'line-height: 1.8;">{prefix}{sent}</p>'
                )

        full_html = (
            f'<div dir="rtl" style="font-family: \'FrankRuehl\', \'David\', \'Segoe UI\', serif;">'
            f'{"".join(html_lines)}'
            f'</div>'
        )
        self.text_browser.setHtml(full_html)
        self.text_browser.scrollToAnchor("active_sent")

    def on_sentence_changed(self, idx: int):
        if not self.sentences:
            return
        self.active_sentence_index = max(0, min(len(self.sentences) - 1, int(idx)))
        self.render_text()
        self.update_progress_display()
        self._persist_progress_only()

    def update_progress_display(self):
        total = len(self.sentences)
        if total > 0:
            self.active_sentence_index = max(0, min(total - 1, self.active_sentence_index))
        current = self.active_sentence_index + 1 if total > 0 else 0
        book = self._get_current_book()
        ch_total = len((book.get("chapters") or [])) if book else 0
        if ch_total > 1:
            self.lbl_progress.setText(f"מקטע {self.current_chapter_index + 1}/{ch_total} · {current}/{total}")
        else:
            self.lbl_progress.setText(f"משפט {current} מתוך {total}")
        if total > 0:
            self.progress_bar.setValue(int((current / total) * 100))
        else:
            self.progress_bar.setValue(0)

    def _on_chapter_audio_completed(self):
        book = self._get_current_book()
        if not book:
            return
        if not getattr(self, "chk_continuous_book", None) or not self.chk_continuous_book.isChecked():
            return
        chapters = book.get("chapters") or []
        next_idx = self.current_chapter_index + 1
        if next_idx >= len(chapters):
            self.statusBar.showMessage(f"הסתיימה הקראת הספר '{book.get('title', '')}' במלואו!", 7000)
            return
        self._auto_advancing_chapter = True
        try:
            self.chapter_list.setCurrentRow(next_idx)
            self.active_sentence_index = 0
            self.render_text()
            self.update_progress_display()
            self.statusBar.showMessage(
                f"ממשיך אוטומטית למקטע {next_idx + 1}/{len(chapters)}: {chapters[next_idx].get('title', '')}",
                4000,
            )
            if self.sentences:
                self.audio.play(
                    self.sentences,
                    0,
                    voice_overrides=self._get_voice_overrides(),
                )
        finally:
            self._auto_advancing_chapter = False

    def _get_voice_overrides(self) -> Optional[List[str]]:
        if not self.speaker_roles:
            return None
        ncfg = self.settings.get("narration", {})
        t_voice = ncfg.get("teacher_voice", "edge_avri")
        s_voice = ncfg.get("student_voice", "edge_hila")
        n_voice = ncfg.get("narrator_voice", "edge_avri")
        overrides: List[str] = []
        for role in self.speaker_roles:
            if role == "teacher":
                overrides.append(t_voice)
            elif role == "student":
                overrides.append(s_voice)
            else:
                overrides.append(n_voice)
        return overrides

    def toggle_play(self):
        if not self.sentences:
            self.statusBar.showMessage("אין טקסט להקראה בפרק הנוכחי.", 3000)
            return
        if self.audio.is_playing():
            self.audio.pause()
        else:
            if self.audio.worker and self.audio.worker.isRunning() and self.audio.worker._is_paused:
                self.audio.resume()
            else:
                self.audio.play(
                    self.sentences,
                    self.active_sentence_index,
                    voice_overrides=self._get_voice_overrides(),
                )

    def on_playback_state_changed(self, playing: bool):
        self.btn_play.setText("⏸" if playing else "▶")
        self.statusBar.showMessage("מקריא כעת..." if playing else "מושהה")

    def prev_sentence(self):
        if not self.sentences:
            return
        new_idx = max(0, self.active_sentence_index - 1)
        self.active_sentence_index = new_idx
        self.render_text()
        self.update_progress_display()
        self._persist_progress_only()
        if self.audio.is_playing():
            self.audio.play(self.sentences, new_idx, voice_overrides=self._get_voice_overrides())

    def next_sentence(self):
        if not self.sentences:
            return
        new_idx = min(len(self.sentences) - 1, self.active_sentence_index + 1)
        self.active_sentence_index = new_idx
        self.render_text()
        self.update_progress_display()
        self._persist_progress_only()
        if self.audio.is_playing():
            self.audio.play(self.sentences, new_idx, voice_overrides=self._get_voice_overrides())

    def on_voice_changed(self):
        voice_id = self.combo_voice.currentData() or "edge_avri"
        self.audio.voice_type = voice_id
        self._persist_progress_only()
        if self.audio.is_playing() and self.sentences:
            self.audio.play(self.sentences, self.active_sentence_index, voice_overrides=self._get_voice_overrides())

    def on_speed_changed(self, val: int):
        speed = val / 100.0
        self.lbl_speed_val.setText(f"{speed:.1f}x")
        self.audio.speed = speed
        self._persist_progress_only()

    def change_font_size(self, delta: int):
        self.font_size = max(12, min(36, self.font_size + delta))
        self.render_text()
        self._persist_progress_only()

    def on_audio_error(self, err_msg: str):
        self.statusBar.showMessage(f"שגיאת אודיו: {err_msg}", 5000)

    # --- Async File & Text Import (Crash-proof for huge PDFs) ---
    def open_file_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "בחר קובץ ספר או טקסט",
            "",
            "קבצי טקסט ומסמכים (*.txt *.md *.pdf *.docx);;כל הקבצים (*.*)"
        )
        if not file_path:
            return

        if self._file_thread and self._file_thread.isRunning():
            QMessageBox.information(self, "טעינה בתהליך", "קובץ אחר עדיין נטען ברקע, אנא המתן רגע.")
            return

        _retire_window_thread(self._file_thread, wait_ms=100)
        self.audio.stop()
        self.statusBar.showMessage("מתחיל טעינת קובץ גדול ברקע...")
        self._file_thread = FileLoadThread(file_path)
        self._file_thread.progress.connect(lambda msg: self.statusBar.showMessage(msg))
        self._file_thread.finished_book.connect(self._on_file_loaded)
        self._file_thread.failed.connect(self._on_file_load_failed)
        self._file_thread.start()

    def _on_file_loaded(self, new_book: dict):
        # הימנעות מדריסת ID קיים אם נטען קובץ בעל שם זהה
        existing_ids = {b.get("id") for b in self.books}
        base_id = new_book.get("id", "imported_book")
        uniq_id = base_id
        n = 2
        while uniq_id in existing_ids:
            uniq_id = f"{base_id}_{n}"
            n += 1
        new_book["id"] = uniq_id

        self.books.append(new_book)
        self.load_book_to_ui(len(self.books) - 1)
        self._persist_library_and_progress()
        ch_count = len(new_book.get("chapters", []))
        cov = new_book.get("pdf_coverage") or {}
        if cov and not cov.get("is_complete_extraction", True):
            warn = cov.get("warning_message") or "חלק מעמודי ה-PDF לא חולצו ודורשים OCR."
            self.statusBar.showMessage(f"נטען ספר עם חילוץ PDF חלקי ({ch_count} חלקים) — בדוק אזהרת OCR.", 9000)
            QMessageBox.warning(self, "אזהרה: חילוץ PDF חלקי / נדרש OCR", warn)
        else:
            self.statusBar.showMessage(f"הספר '{new_book['title']}' נשמר ונטען בהצלחה ({ch_count} פרקים/חלקים)!", 6000)

    def _on_file_load_failed(self, err: str):
        self.statusBar.showMessage("טעינת הקובץ נכשלה.", 4000)
        QMessageBox.critical(self, "שגיאה בטעינת קובץ", err)

    def open_resegment_dialog(self):
        book = self._get_current_book()
        if not book:
            QMessageBox.information(self, "אין ספר", "טען ספר או הדבק טקסט לפני פיצול מקטעים.")
            return
        dlg = SegmentBookDialog(self, book)
        if not dlg.exec():
            return
        opts = dlg.get_options()
        self.audio.stop()
        updated = resegment_book(book, preset=opts["preset"], max_chars=opts["max_chars"])
        self.books[self.current_book_index] = updated
        self.on_book_selected(self.current_book_index)
        self._persist_library_and_progress()
        cnt = len(updated.get("chapters") or [])
        self.statusBar.showMessage(f"הספר פוצל מחדש ל-{cnt} מקטעים נוחים!", 6000)

    def paste_custom_text(self):
        text, ok = QInputDialog.getMultiLineText(self, "הדבקת טקסט", "הדבק כאן טקסט בעברית או באנגלית לקריאה/תרגום:")
        if ok and text.strip():
            clean = text.strip()
            chapters = (
                segment_text_smartly(clean, base_title="מקטע")
                if len(clean) > 4000
                else [{"title": "פרק א׳", "text": clean}]
            )
            new_book = {
                "id": f"custom_{len(self.books)}_{abs(hash(clean)) % 100000}",
                "title": f"טקסט מותאם אישית ({len(self.books) + 1})",
                "author": "הדבקה אישית",
                "description": f"טקסט שהודבק במערכת ({len(chapters)} מקטעים)",
                "chapters": chapters,
            }
            self.books.append(new_book)
            self.load_book_to_ui(len(self.books) - 1)
            self._persist_library_and_progress()

    # --- Settings Dialog ---
    def open_settings_dialog(self):
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec():
            self.settings = load_settings()
            self._refresh_provider_badge()
            # בדיקת התיישנות תוצרים פעילים בעקבות שינוי הגדרות
            book, chapter = self._get_current_book_and_chapter()
            if book and chapter:
                status = assess_chapter_pipeline(
                    book_id=book.get("id", ""),
                    chapter_index=self.current_chapter_index,
                    source_text=chapter.get("text", "") or "",
                    settings=self.settings,
                    store=self.store,
                )
                if self.current_script_data and not status["script_ready"]:
                    self.current_script_data = None
                    self.speaker_roles = []
                    self.speaker_labels = []
                    self.sentences = split_into_sentences(chapter.get("text", "") or "")
                    self.btn_export_script.setVisible(False)
                    self.btn_restore_original.setVisible(False)
                    self.render_text()
                    self.update_progress_display()
                    self._refresh_resume_banner(force=True)
                    self.statusBar.showMessage("ההגדרות נשמרו; התסריט הקודם התיישן ולא ישמש ליצוא.", 6000)
                    return
            self._refresh_resume_banner(force=True)
            self.statusBar.showMessage("הגדרות ה-AI, המודלים, התרגום והקולות נשמרו!", 4000)

    # --- Background Worker Helper (עם התקדמות וביטול מיידי) ---
    def _close_task_progress(self):
        if self._task_progress_dlg:
            _safe_close_progress_dialog(self._task_progress_dlg)
            self._task_progress_dlg = None

    def _run_background_task(
        self,
        status_msg: str,
        fn: Callable[[threading.Event, Callable[[str], None]], Any],
        on_ok: Callable[[Any], None],
        title: str = "מבצע משימת AI ברקע",
        on_error: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        if self._task_thread and self._task_thread.isRunning():
            QMessageBox.information(self, "פעולה רצה", "משימת AI/תרגום אחרת עדיין רצה ברקע, אנא המתן לסיומה.")
            return

        _retire_window_thread(self._task_thread, wait_ms=100)
        self.statusBar.showMessage(status_msg)
        th = TaskWorkerThread(fn)
        self._task_thread = th

        prog_dlg = QProgressDialog(status_msg, "🛑 בטל פעולה", 0, 0, self)
        prog_dlg.setWindowTitle(title)
        prog_dlg.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        prog_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        prog_dlg.setMinimumWidth(420)
        prog_dlg.canceled.connect(th.cancel)
        self._task_progress_dlg = prog_dlg
        last_err_details: Dict[str, Any] = {}

        def _on_prog(msg: str):
            self.statusBar.showMessage(msg)
            if self._task_progress_dlg:
                self._task_progress_dlg.setLabelText(msg)

        def _on_ok_wrap(res: Any):
            self._close_task_progress()
            on_ok(res)

        def _on_cancelled(msg: str):
            self._close_task_progress()
            self._refresh_resume_banner()
            self.statusBar.showMessage(msg, 5000)
            QMessageBox.information(self, "הפעולה בוטלה", msg)

        def _on_failed_details(d: dict):
            last_err_details.clear()
            last_err_details.update(d or {})

        def _on_failed(err: str):
            self._close_task_progress()
            self._refresh_resume_banner()
            if on_error is not None:
                details = dict(last_err_details)
                details.setdefault("error", err)
                on_error(details)
            else:
                self._on_background_task_error(err)

        th.progress.connect(_on_prog)
        th.finished_ok.connect(_on_ok_wrap)
        th.cancelled.connect(_on_cancelled)
        th.failed_details.connect(_on_failed_details)
        th.failed.connect(_on_failed)
        th.start()
        prog_dlg.show()

    def _on_background_task_error(self, err: str):
        self.statusBar.showMessage("הפעולה הופסקה עם שגיאה — התקדמות תקינה נשמרה.", 6000)
        QMessageBox.warning(
            self,
            "שגיאה בביצוע המשימה",
            f"{err}\n\nניתן לבדוק את ההגדרות / החיבור ולנסות שוב (יחידות תקינות שכבר הושלמו נשמרו).",
        )

    # --- AI Vocalization & Companion ---
    def vocalize_current_chapter(self):
        book, chapter = self._get_current_book_and_chapter()
        if not book or not chapter:
            return
        chapter_text = "\n".join(self.sentences) if self.sentences else (chapter.get("text", "") or "")
        if not chapter_text.strip():
            return
        cfg = self.settings

        self._run_background_task(
            "מפעיל בלשן AI לניקוד ופתיחת קיצורים...",
            lambda cancel_ev, prog_cb: vocalize_and_expand_hebrew(
                chapter_text,
                settings=cfg,
                cancel_event=cancel_ev,
                progress_cb=prog_cb,
            ),
            self._on_vocalize_done,
            title="ניקוד ופתיחת קיצורים (AI)",
        )

    def _on_vocalize_done(self, res: dict):
        self.sentences = res.get("sentences") or self.sentences
        self.speaker_roles = []
        self.speaker_labels = []
        self.active_sentence_index = 0
        self.btn_restore_original.setVisible(True)
        self.render_text()
        self.update_progress_display()
        note = res.get("note")
        if note:
            self.statusBar.showMessage(note, 6000)
        else:
            self.statusBar.showMessage(f"הניקוד הושלם ({res.get('source', '')})!", 5000)

    def trigger_companion(self, mode: str):
        book, chapter = self._get_current_book_and_chapter()
        if not book or not chapter:
            QMessageBox.information(self, "אין ספר", "טען ספר או פרק לפני הפעלת העוזר הספרותי.")
            return
        safe_idx = max(0, min(len(self.sentences) - 1, self.active_sentence_index)) if self.sentences else 0
        target_text = (
            self.sentences[safe_idx]
            if mode == "explain" and self.sentences
            else "\n".join(self.sentences[:120]) or (chapter.get("text", "") or "")
        )

        titles = {
            "explain": "💡 ביאור פסקה",
            "summary": "📝 סיכום פרק",
            "quiz": "❓ שאלות הבנה",
        }
        self.lbl_comp_title.setText(titles.get(mode, "🤖 עוזר ספרותי AI"))
        self.companion_widget.setVisible(True)
        self.comp_text.setHtml("<div dir='rtl'><i>מנתח את הטקסט ברקע באמצעות AI...</i></div>")
        cfg = self.settings

        self._run_background_task(
            "מפעיל עוזר ספרותי AI...",
            lambda cancel_ev, prog_cb: ask_companion(
                target_text,
                mode=mode,
                book_title=book["title"],
                settings=cfg,
                cancel_event=cancel_ev,
                progress_cb=prog_cb,
            ),
            lambda reply: (
                self.comp_text.setMarkdown(str(reply)),
                self.statusBar.showMessage("הניתוח הסתיים.", 3500),
            ),
            title=titles.get(mode, "עוזר ספרותי AI"),
        )

    # --- Wikipedia Search & AI Knowledge Grounding ---
    def open_wikipedia_dialog(self):
        book, chapter = self._get_current_book_and_chapter()
        if not book or not chapter:
            QMessageBox.information(self, "אין ספר", "טען ספר או פרק לפני חיפוש בוויקיפדיה.")
            return
        active_sentence = ""
        if self.sentences and 0 <= self.active_sentence_index < len(self.sentences):
            active_sentence = self.sentences[self.active_sentence_index]
        chapter_context = (
            active_sentence
            or "\n".join(self.sentences[:60])
            or (chapter.get("text", "") or "")[:2500]
        )

        dlg = WikiSearchDialog(self, self.settings)
        if not dlg.exec():
            return
        opts = dlg.get_options()
        cfg = self.settings

        self.lbl_comp_title.setText("📚 מחקר מושגים — ויקיפדיה + AI")
        self.companion_widget.setVisible(True)
        self.comp_text.setHtml("<div dir='rtl'><i>מחפש ערכים בוויקיפדיה ומרחיב את הידע ברקע...</i></div>")

        def _worker(cancel_ev, prog_cb):
            if prog_cb:
                prog_cb("שולף ערכים מוויקיפדיה...")
            if opts["use_ai"]:
                res = enrich_with_wikipedia_and_ai(
                    query_terms=opts["terms"],
                    book_context=chapter_context,
                    book_title=book["title"],
                    settings=cfg,
                    lang=opts["lang"],
                )
                res["mode"] = "ai"
                return res
            else:
                res = fetch_wikipedia_context_for_terms(
                    terms_input=opts["terms"],
                    fallback_text=chapter_context,
                    settings=cfg,
                    lang=opts["lang"],
                )
                if not res.get("articles"):
                    raise RuntimeError(
                        f"לא נמצאו ערכים מתאימים בוויקיפדיה עבור: {', '.join(res.get('terms', [])) or opts['terms']}"
                    )
                res["mode"] = "wiki_only"
                return res

        self._run_background_task(
            "מחפש מושגים בוויקיפדיה ומעשיר ידע...",
            _worker,
            self._on_wikipedia_done,
            title="חיפוש בוויקיפדיה והרחבת AI",
        )

    def _on_wikipedia_done(self, res: dict):
        terms = res.get("terms") or []
        articles = res.get("articles") or []
        ai_expansion = res.get("ai_expansion", "")

        md_parts = [f"### 📚 מושגים שנחקרו: {', '.join(terms) if terms else 'חיפוש חופשי'}\n"]
        if ai_expansion:
            md_parts.append(f"## 🤖 הרחבה וביאור של מודל ה-AI\n\n{ai_expansion}\n\n---")

        md_parts.append("## 🌐 ערכי ויקיפדיה שנשלפו")
        for art in articles:
            title = art.get("title", "")
            lang = art.get("lang", "")
            extract = art.get("extract", "")
            url = art.get("url", "")
            md_parts.append(
                f"### [{title} ({lang})]({url})\n\n{extract}\n\n[פתח ערך מלא בוויקיפדיה ↗]({url})\n"
            )

        self.lbl_comp_title.setText("📚 ויקיפדיה והרחבת ידע")
        self.companion_widget.setVisible(True)
        self.comp_text.setMarkdown("\n\n".join(md_parts))
        self.statusBar.showMessage(f"נמצאו {len(articles)} ערכי ויקיפדיה והתוכן הורחב בהצלחה!", 5000)

    # --- AI Script Studio (הפרק הנוכחי או הספר כולו) ---
    def open_whole_book_script_dialog(self):
        idx = self.combo_scope.findData("book")
        if idx >= 0:
            self.combo_scope.setCurrentIndex(idx)
        self.open_script_studio(forced_scope="book")

    def _persist_script_preferences(self, opts: Dict[str, Any]):
        if not isinstance(opts, dict):
            return
        s_cfg = self.settings.setdefault("script", {})
        new_mode = (opts.get("mode") or s_cfg.get("mode") or "educational").strip()
        new_inst = (opts.get("custom_instruction") if "custom_instruction" in opts else opts.get("instruction", s_cfg.get("custom_instruction", ""))) or ""
        new_inst = str(new_inst).strip()
        new_wiki = bool(opts["use_wikipedia"]) if "use_wikipedia" in opts else bool(s_cfg.get("use_wikipedia", False))
        new_terms = str(opts["wiki_terms"]).strip() if "wiki_terms" in opts else str(s_cfg.get("wiki_terms", "")).strip()
        if (
            s_cfg.get("mode") != new_mode
            or s_cfg.get("custom_instruction") != new_inst
            or bool(s_cfg.get("use_wikipedia", False)) != new_wiki
            or s_cfg.get("wiki_terms") != new_terms
        ):
            s_cfg["mode"] = new_mode
            s_cfg["custom_instruction"] = new_inst
            s_cfg["use_wikipedia"] = new_wiki
            s_cfg["wiki_terms"] = new_terms
            try:
                save_settings(self.settings)
            except Exception:
                pass

    def open_script_studio(self, forced_scope: Optional[str] = None):
        if not isinstance(forced_scope, str):
            forced_scope = None
        book, chapter = self._get_current_book_and_chapter()
        if not book or not chapter:
            QMessageBox.information(self, "אין ספר", "אין פרק פעיל ליצירת תסריט.")
            return
        chapters = book.get("chapters") or []
        default_scope = forced_scope or (self.combo_scope.currentData() or "chapter")

        dlg = ScriptStudioDialog(
            self,
            self.settings,
            default_scope=default_scope,
            book_title=book.get("title", ""),
            chapter_count=len(chapters),
            current_chapter_title=chapter.get("title", ""),
        )
        if not dlg.exec():
            return
        opts = dlg.get_options()
        self._persist_script_preferences(opts)
        scope = opts.get("scope", default_scope)

        if scope == "book":
            t_opts_arg = {"bypass_same_language": True} if opts.get("bypass_same_language") else None
            self._start_whole_book_script_job(book, opts, translation_opts=t_opts_arg)
            return

        source_text = chapter["text"]
        cfg = self.settings
        book_id = book.get("id", "")
        chapter_idx = self.current_chapter_index
        ch_t_opts = {"bypass_same_language": True} if opts.get("bypass_same_language") else None

        def _worker(cancel_ev, prog_cb):
            ensure_translation_for_chapter(
                book_id=book_id,
                chapter_index=chapter_idx,
                source_text=source_text,
                settings=cfg,
                translation_opts=ch_t_opts,
                store=self.store,
                cancel_event=cancel_ev,
                progress_cb=prog_cb,
            )
            return ensure_script_for_chapter(
                book_id=book_id,
                chapter_index=chapter_idx,
                source_text=source_text,
                book_title=book["title"],
                settings=cfg,
                script_opts=opts,
                store=self.store,
                cancel_event=cancel_ev,
                progress_cb=prog_cb,
            )

        status_msg = (
            "מאמת תרגום ומייצר תסריט AI מועשר בוויקיפדיה לפרק..."
            if opts.get("use_wikipedia")
            else "מאמת תרגום ומייצר תסריט AI מלא לפרק..."
        )
        self._run_background_task(
            status_msg,
            _worker,
            self._on_script_generated,
            title="יצירת תסריט AI (פרק נוכחי)",
            on_error=lambda err_d: self._handle_chapter_task_recovery(
                book=book,
                chapter_index=chapter_idx,
                stage="script",
                err_details=err_d,
                retry_fn=lambda: self._run_single_chapter_script_with_opts(book, chapter_idx, opts),
            ),
        )

    def _run_single_chapter_script_with_opts(self, book: Dict[str, Any], chapter_idx: int, opts: Dict[str, Any]):
        source_text = book["chapters"][chapter_idx]["text"]
        cfg = self.settings
        book_id = book.get("id", "")

        def _worker(cancel_ev, prog_cb):
            ensure_translation_for_chapter(
                book_id=book_id,
                chapter_index=chapter_idx,
                source_text=source_text,
                settings=cfg,
                store=self.store,
                cancel_event=cancel_ev,
                progress_cb=prog_cb,
            )
            return ensure_script_for_chapter(
                book_id=book_id,
                chapter_index=chapter_idx,
                source_text=source_text,
                book_title=book.get("title", ""),
                settings=cfg,
                script_opts=opts,
                store=self.store,
                cancel_event=cancel_ev,
                progress_cb=prog_cb,
            )

        self._run_background_task(
            "ממשיך תרגום/תסריט AI לפרק מהנקודה שנשמרה...",
            _worker,
            self._on_script_generated,
            title="יצירת תסריט AI (פרק נוכחי)",
            on_error=lambda err_d: self._handle_chapter_task_recovery(
                book=book,
                chapter_index=chapter_idx,
                stage="script",
                err_details=err_d,
                retry_fn=lambda: self._run_single_chapter_script_with_opts(book, chapter_idx, opts),
            ),
        )

    def _handle_chapter_task_recovery(
        self,
        *,
        book: Dict[str, Any],
        chapter_index: int,
        stage: str,
        err_details: Dict[str, Any],
        retry_fn: Callable[[], None],
    ):
        chapters = book.get("chapters") or []
        src = chapters[chapter_index].get("text", "") if 0 <= chapter_index < len(chapters) else ""
        ch_assess = assess_chapter_pipeline(
            book_id=book.get("id", ""),
            chapter_index=chapter_index,
            source_text=src,
            settings=self.settings,
            store=self.store,
        )
        tp = ch_assess.get("translation_progress") or {}
        sp = ch_assess.get("script_progress") or {}
        use_sp = stage == "script" and ch_assess.get("translation_ready") and not ch_assess.get("script_ready")
        stats = sp if use_sp else tp
        dlg = BookRecoveryDialog(
            self,
            book_title=book.get("title", ""),
            chapter_number=chapter_index + 1,
            chapter_count=len(chapters) or 1,
            unit_index=err_details.get("unit_index"),
            unit_id=err_details.get("unit_id") or stats.get("next_unit_id", ""),
            stage=stage,
            error_summary=err_details.get("error", ""),
            is_blocked=bool(err_details.get("is_blocked")),
            blocked_provider=err_details.get("blocked_provider", ""),
            blocked_filter=err_details.get("blocked_filter", ""),
            settings=self.settings,
            completed_count=0,
            units_completed=int(stats.get("completed_count") or 0),
            units_missing=int(stats.get("missing_count") or 0),
            units_failed=int(stats.get("failed_count") or 0),
            failed_chapters={},
            allow_continue_other_chapters=False,
        )
        if not dlg.exec():
            self.statusBar.showMessage("ההתקדמות נשמרה אטומית בפרק — תוכל להמשיך מאותה נקודה בכל עת.", 6000)
            return
        decision = dlg.get_recovery_decision()
        action = decision["action"]
        self._apply_fallback_policy_preference(
            decision.get("fallback_to_ai_on_failure"),
            new_engine=decision.get("engine") if action == "switch_and_resume" else None,
        )
        retry_fn()

    def _apply_fallback_policy_preference(self, fallback_to_ai: Optional[bool], new_engine: Optional[str] = None):
        changed = False
        trans_cfg = self.settings.setdefault("translation", {})
        if fallback_to_ai is not None and bool(trans_cfg.get("fallback_to_ai_on_failure", False)) != bool(fallback_to_ai):
            trans_cfg["fallback_to_ai_on_failure"] = bool(fallback_to_ai)
            changed = True
        if new_engine and trans_cfg.get("engine") != new_engine:
            trans_cfg["engine"] = new_engine
            changed = True
        if changed:
            save_settings(self.settings)

    def _start_whole_book_script_job(
        self,
        book: Dict[str, Any],
        script_opts: Dict[str, Any],
        *,
        translation_opts: Optional[Dict[str, Any]] = None,
        retry_failed_only: bool = False,
        continue_on_chapter_failure: bool = False,
    ):
        if self._whole_book_script_thread and self._whole_book_script_thread.isRunning():
            QMessageBox.information(self, "תהליך בתהליך", "יצירת תסריט לספר שלם עדיין רצה ברקע.")
            return

        cov_check = validate_book_completeness(book)
        if not cov_check["is_complete"]:
            QMessageBox.warning(
                self,
                "לא ניתן לעבד ספר חלקי כספר שלם",
                f"{cov_check['reason']}\n\nתקן או השלם OCR עבור העמודים החסרים לפני הפעלת עיבוד לכל הספר.",
            )
            return

        if "fallback_to_ai_on_failure" in script_opts:
            self._apply_fallback_policy_preference(bool(script_opts.get("fallback_to_ai_on_failure")))

        book_snapshot = deepcopy(book)
        target_chapters = script_opts.get("target_chapters")
        clean_script_opts = {
            k: v
            for k, v in dict(script_opts).items()
            if k not in ("fallback_to_ai_on_failure", "target_chapters", "bypass_same_language")
        }
        eff_trans_opts = dict(translation_opts or {})
        if script_opts.get("bypass_same_language"):
            eff_trans_opts["bypass_same_language"] = True
        self._last_whole_book_script_params = {
            "book_id": book_snapshot.get("id", ""),
            "script_opts": dict(clean_script_opts),
            "translation_opts": dict(eff_trans_opts),
        }

        total = len(book_snapshot.get("chapters") or [])
        run_cnt = len(target_chapters) if target_chapters is not None else total
        self.statusBar.showMessage(f"מייצר תרגום ותסריט לספר ({run_cnt} מתוך {total} מקטעים לפי הסדר)...")

        _retire_window_thread(self._whole_book_script_thread, wait_ms=100)
        th = WholeBookScriptThread({
            "book": book_snapshot,
            "settings": deepcopy(self.settings),
            "translation_opts": eff_trans_opts or None,
            "script_opts": clean_script_opts,
            "store": self.store,
            "retry_failed_only": bool(retry_failed_only),
            "target_chapters": target_chapters,
            "continue_on_chapter_failure": bool(continue_on_chapter_failure),
        })
        self._whole_book_script_thread = th

        prog_dlg = QProgressDialog(f"מייצר תסריט לכל {total} פרקי הספר...", "🛑 בטל משימה", 0, 0, self)
        prog_dlg.setWindowTitle("📚🎬 תסריט AI לכל הספר")
        prog_dlg.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        prog_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        prog_dlg.setMinimumWidth(440)
        prog_dlg.canceled.connect(th.cancel)
        self._whole_book_script_progress_dlg = prog_dlg

        def _on_prog(msg: str):
            self.statusBar.showMessage(msg)
            if self._whole_book_script_progress_dlg:
                self._whole_book_script_progress_dlg.setLabelText(msg)

        th.progress.connect(_on_prog)
        th.finished_result.connect(self._on_whole_book_script_finished)
        th.start()
        prog_dlg.show()

    def _launch_whole_book_export_thread(self, job_kwargs: Dict[str, Any]):
        if self._export_thread and self._export_thread.isRunning():
            QMessageBox.information(self, "ייצוא בתהליך", "ייצוא שמע קודם עדיין רץ ברקע, אנא המתן לסיומו.")
            return

        total_ch = len((job_kwargs.get("book") or {}).get("chapters") or [])
        self._last_whole_book_export_params = {
            "book": deepcopy(job_kwargs["book"]),
            "output_path": job_kwargs["output_path"],
            "package_mode": job_kwargs["package_mode"],
            "translation_opts": dict(job_kwargs.get("translation_opts") or {}),
            "script_opts": dict(job_kwargs.get("script_opts") or {}),
            "tts_opts": dict(job_kwargs.get("tts_opts") or {}),
        }

        _retire_window_thread(self._export_thread, wait_ms=100)
        self.statusBar.showMessage(f"מפעיל יצוא מלא לכל הספר ({total_ch} פרקים לפי הסדר)...")
        self._export_thread = PipelineExportThread(
            {
                **job_kwargs,
                "settings": deepcopy(self.settings),
                "store": self.store,
            },
            scope="book",
        )
        prog_dlg = QProgressDialog(
            f"מתחיל עיבוד הספר כולו ({total_ch} פרקים לפי הסדר)...",
            "🛑 בטל יצוא",
            0,
            0,
            self,
        )
        prog_dlg.setWindowTitle(f"📚🎧 מייצא את הספר כולו ({total_ch} פרקים)")
        prog_dlg.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        prog_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        prog_dlg.setMinimumWidth(440)
        prog_dlg.canceled.connect(self._export_thread.cancel)
        self._export_progress_dlg = prog_dlg

        def _on_prog(msg: str):
            self.statusBar.showMessage(msg)
            if self._export_progress_dlg:
                self._export_progress_dlg.setLabelText(msg)

        self._export_thread.progress.connect(_on_prog)
        self._export_thread.finished_result.connect(self._on_pipeline_finished)
        self._export_thread.start()
        prog_dlg.show()

    def _find_book_by_id(self, book_id: str) -> Optional[Dict[str, Any]]:
        for b in self.books:
            if b.get("id") == book_id:
                return b
        if self.books:
            return self.books[self.current_book_index]
        return None

    def _handle_whole_book_recovery(self, res: dict, *, is_export: bool = False):
        book_id = res.get("book_id") or (
            self.books[self.current_book_index].get("id", "") if self.books else ""
        )
        book = self._find_book_by_id(book_id)
        if not book:
            QMessageBox.critical(self, "שגיאה", res.get("error", "שגיאה לא ידועה"))
            return

        assess = assess_book_pipeline(book=book, settings=self.settings, store=self.store)
        total = int(res.get("chapter_count") or len(book.get("chapters") or []) or 1)
        ch_num = int(res.get("chapter_number") or 1)
        if res.get("status") == "partial":
            needs = res.get("needs_attention_chapters") or []
            ch_num = (needs[0] + 1) if needs else ch_num
        completed_list = res.get("completed_translations") or []
        completed_cnt = len(completed_list) if completed_list else int(assess.get("translations_ready", 0))
        rp = assess.get("resume_point") or {}
        u_idx = res.get("unit_index") if res.get("unit_index") is not None else rp.get("unit_index")
        u_id = res.get("unit_id") or rp.get("unit_id") or ""
        u_done = res.get("units_completed") if res.get("units_completed") is not None else assess.get("units_completed", 0)
        u_miss = res.get("units_missing") if res.get("units_missing") is not None else assess.get("units_missing", 0)
        u_fail = res.get("units_failed") if res.get("units_failed") is not None else assess.get("units_failed", 0)

        dlg = BookRecoveryDialog(
            self,
            book_title=book.get("title", ""),
            chapter_number=ch_num,
            chapter_count=total,
            unit_index=u_idx,
            unit_id=u_id,
            stage=res.get("stage", "translation"),
            error_summary=res.get("error", "העיבוד נעצר."),
            is_blocked=bool(res.get("is_blocked")),
            blocked_provider=res.get("blocked_provider", ""),
            blocked_filter=res.get("blocked_filter", ""),
            settings=self.settings,
            completed_count=completed_cnt,
            units_completed=int(u_done or 0),
            units_missing=int(u_miss or 0),
            units_failed=int(u_fail or 0),
            failed_chapters=res.get("failed_chapters") or {},
            allow_continue_other_chapters=not is_export,
        )
        if not dlg.exec():
            self.statusBar.showMessage("התקדמות הפרקים והיחידות נשמרה אטומית — ניתן להמשיך בכל עת מ'המשך מטמון'.", 6000)
            self._refresh_resume_banner()
            return

        decision = dlg.get_recovery_decision()
        action = decision["action"]
        self._apply_fallback_policy_preference(
            decision.get("fallback_to_ai_on_failure"),
            new_engine=decision.get("engine") if action == "switch_and_resume" else None,
        )

        if is_export and self._last_whole_book_export_params:
            exp_p = deepcopy(self._last_whole_book_export_params)
            exp_t_opts = dict(exp_p.get("translation_opts") or {})
            if action == "switch_and_resume" and decision.get("engine"):
                exp_t_opts["engine"] = decision["engine"]
            exp_t_opts["fallback_to_ai_on_failure"] = bool(
                self.settings.get("translation", {}).get("fallback_to_ai_on_failure", False)
            )
            exp_p["translation_opts"] = exp_t_opts
            self._launch_whole_book_export_thread(exp_p)
            return

        last_s = self._last_whole_book_script_params or {
            "script_opts": {"mode": "educational", "instruction": "", "use_wikipedia": False, "wiki_terms": ""},
            "translation_opts": {},
        }
        t_opts = dict(last_s.get("translation_opts") or {})
        if action == "switch_and_resume" and decision.get("engine"):
            t_opts["engine"] = decision["engine"]
        t_opts["fallback_to_ai_on_failure"] = bool(
            self.settings.get("translation", {}).get("fallback_to_ai_on_failure", False)
        )

        self._start_whole_book_script_job(
            book,
            dict(last_s.get("script_opts") or {}),
            translation_opts=t_opts,
            retry_failed_only=(action == "resume_failed_only"),
            continue_on_chapter_failure=(action == "continue_other_chapters"),
        )

    def resume_whole_book_job(self):
        if not self.books:
            QMessageBox.information(self, "אין ספר", "אין ספר פעיל להמשך עיבוד.")
            return
        book = self.books[self.current_book_index]
        book_id = book.get("id", "")
        job = self.store.get_whole_book_job(book_id)
        assess = assess_book_pipeline(book=book, settings=self.settings, store=self.store)
        total = assess["chapter_count"]
        t_ready = assess["translations_ready"]
        s_ready = assess["scripts_ready"]
        failed_map = (job or {}).get("failed_chapters") or {}

        if assess["all_translations_ready"] and assess["all_scripts_ready"] and not failed_map:
            QMessageBox.information(
                self,
                "הספר מוכן ומאומת",
                f"כל {total} פרקי הספר '{book.get('title', '')}' כבר מתורגמים ומתוסרטים במטמון.\n"
                "ניתן להפעיל ישירות '📚🎧 שמע לכל הספר'.",
            )
            return

        rp = assess.get("resume_point") or {}
        ch_num = int((job or {}).get("failed_or_cancelled_chapter") or rp.get("chapter_number") or 1)
        if not job and rp:
            ch_num = int(rp.get("chapter_number") or 1)

        pseudo_res = {
            "book_id": book_id,
            "book_title": book.get("title", ""),
            "status": (job or {}).get("status", "failed"),
            "stage": (job or {}).get("stage") or rp.get("stage") or "translation",
            "chapter_number": ch_num,
            "chapter_count": total,
            "unit_index": (job or {}).get("unit_index") if (job or {}).get("unit_index") is not None else rp.get("unit_index"),
            "unit_id": (job or {}).get("unit_id") or rp.get("unit_id") or "",
            "is_blocked": bool((job or {}).get("is_blocked", False)),
            "blocked_provider": (job or {}).get("blocked_provider", ""),
            "blocked_filter": (job or {}).get("blocked_filter", ""),
            "completed_translations": [cs["chapter_index"] for cs in assess["chapters"] if cs["translation_ready"]],
            "units_completed": assess.get("units_completed", 0),
            "units_missing": assess.get("units_missing", 0),
            "units_failed": assess.get("units_failed", 0),
            "failed_chapters": failed_map,
            "error": (job or {}).get("error") or (
                f"מוכנים {t_ready}/{total} תרגומים ו-{s_ready}/{total} תסריטים "
                f"({assess.get('units_completed', 0)}/{assess.get('total_translation_units', 0)} יחידות תקינות)."
            ),
        }
        self._handle_whole_book_recovery(pseudo_res, is_export=False)

    def _on_whole_book_script_finished(self, res: dict):
        if self._whole_book_script_progress_dlg:
            _safe_close_progress_dialog(self._whole_book_script_progress_dlg)
            self._whole_book_script_progress_dlg = None

        self._refresh_resume_banner(force=True)

        if res.get("success") and res.get("status") == "completed":
            scripts = res.get("scripts") or []
            if self.books and self.books[self.current_book_index].get("id") == res.get("book_id"):
                if 0 <= self.current_chapter_index < len(scripts):
                    self._on_script_generated(scripts[self.current_chapter_index])
            ch_cnt = res.get("chapter_count", len(scripts))
            self.statusBar.showMessage(
                f"הושלמו ונשמרו אטומית התרגומים והתסריטים לכל {ch_cnt} פרקי הספר '{res.get('book_title', '')}'!",
                8000,
            )
            QMessageBox.information(
                self,
                "📚🎬 תסריט לכל הספר הושלם",
                f"כל {ch_cnt} פרקי הספר '{res.get('book_title', '')}' תורגמו ונוצר עבורם תסריט AI מאומת לפי הסדר.\n"
                "ניתן כעת להפעיל '📚🎧 שמע לכל הספר' ללא יצירה חוזרת של פרקים תקינים.",
            )
        elif res.get("status") == "cancelled":
            self.statusBar.showMessage("יצירת התסריט לספר כולו בוטלה (פרקים ויחידות שהושלמו נשמרו להמשך).", 6000)
            QMessageBox.information(
                self,
                "יצירת תסריט לספר בוטלה",
                f"התהליך בוטל ({res.get('stage', '')}, פרק {res.get('chapter_number', 0)}/{res.get('chapter_count', 0)}).\n"
                "הפרקים והיחידות שכבר הושלמו נשמרו אטומית — ניתן להמשיך דרך '🔄 המשך מטמון' בלי ליצור מחדש עבודה תקינה.",
            )
        elif res.get("status") == "partial":
            needs = [i + 1 for i in (res.get("needs_attention_chapters") or [])]
            self.statusBar.showMessage(
                f"עיבוד הספר הסתיים במצב חלקי — פרקים טעוני טיפול: {needs[:10]}",
                8000,
            )
            self._handle_whole_book_recovery(res, is_export=False)
        else:
            self.statusBar.showMessage("יצירת התסריט לספר כולו נעצרה (התקדמות נשמרה להתאוששות).", 6000)
            self._handle_whole_book_recovery(res, is_export=False)

    def _on_script_generated(self, script_data: dict):
        self.current_script_data = script_data
        lines = script_data.get("lines", [])
        self.audio.stop()

        self.sentences = [line["text"] for line in lines]
        self.speaker_roles = [line["speakerRole"] for line in lines]
        self.speaker_labels = [
            f"{line['speakerLabel']} [{line['style']}]" if line.get("style") else line["speakerLabel"]
            for line in lines
        ]
        self.active_sentence_index = 0
        self.btn_restore_original.setVisible(True)
        self.btn_export_script.setVisible(True)
        self.render_text()
        self.update_progress_display()
        self._persist_progress_only()

        self.lbl_comp_title.setText("🎬 תצוגת תסריט והערות")
        self.companion_widget.setVisible(True)
        self.comp_text.setPlainText(format_script_as_text(script_data))
        self.statusBar.showMessage(
            f"התסריט המלא נוצר ונשמר ({len(lines)} שורות)! לחץ ▶ להקראה רב-קולית.",
            6000,
        )
        self._refresh_chapter_list_badges()
        self._refresh_resume_banner(force=True)

    def export_current_script(self):
        if not self.current_script_data:
            return
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "שמור תסריט לקובץ",
            "script.txt",
            "קובץ טקסט (*.txt);;קובץ JSON (*.json)",
        )
        if not file_path:
            return
        try:
            if file_path.lower().endswith(".json") or "JSON" in selected_filter:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(self.current_script_data, f, ensure_ascii=False, indent=2)
            else:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(format_script_as_text(self.current_script_data))
            self.statusBar.showMessage(f"התסריט נשמר בהצלחה אל {file_path}", 4000)
        except Exception as e:
            QMessageBox.critical(self, "שגיאה בשמירה", str(e))

    # --- Translation Engine (AI or Free — תרגום כל המשפטים ושמירה אטומית) ---
    def _run_chapter_translation_job(self, book: Dict[str, Any], chapter_idx: int, opts: Dict[str, Any]):
        source_text = book["chapters"][chapter_idx]["text"]
        cfg = self.settings
        book_id = book.get("id", "")
        trans_opts = {
            "source_lang": opts.get("source_lang", "auto"),
            "target_lang": opts.get("target_lang", "he"),
            "fallback_to_ai_on_failure": bool(
                opts.get(
                    "fallback_to_ai_on_failure",
                    self.settings.get("translation", {}).get("fallback_to_ai_on_failure", False),
                )
            ),
        }
        if opts.get("engine"):
            trans_opts["engine"] = opts["engine"]

        def _retry_same_or_switched():
            updated_opts = dict(opts)
            updated_opts["engine"] = self.settings.get("translation", {}).get("engine", "google_free")
            updated_opts["fallback_to_ai_on_failure"] = bool(
                self.settings.get("translation", {}).get("fallback_to_ai_on_failure", False)
            )
            self._run_chapter_translation_job(book, chapter_idx, updated_opts)

        self._run_background_task(
            "מתרגם את כל משפטֵי הפרק ושומר אטומית אחרי כל יחידה...",
            lambda cancel_ev, prog_cb: ensure_translation_for_chapter(
                book_id=book_id,
                chapter_index=chapter_idx,
                source_text=source_text,
                settings=cfg,
                translation_opts=trans_opts,
                store=self.store,
                cancel_event=cancel_ev,
                progress_cb=prog_cb,
            ),
            lambda res: self._on_translation_done(res, replace_reader=opts.get("replace_reader", True)),
            title="תרגום פרק מלא",
            on_error=lambda err_d: self._handle_chapter_task_recovery(
                book=book,
                chapter_index=chapter_idx,
                stage="translation",
                err_details=err_d,
                retry_fn=_retry_same_or_switched,
            ),
        )

    def open_translate_dialog(self):
        book, chapter = self._get_current_book_and_chapter()
        if not book or not chapter:
            QMessageBox.information(self, "אין ספר", "אין פרק פעיל לתרגום.")
            return
        dlg = TranslateDialog(self, self.settings)
        if not dlg.exec():
            return
        opts = dlg.get_options()
        self._apply_fallback_policy_preference(opts.get("fallback_to_ai_on_failure"))
        self._run_chapter_translation_job(book, self.current_chapter_index, opts)

    def _on_translation_done(self, res: dict, replace_reader: bool):
        translated = res.get("translated_text", "")
        engine_label = res.get("engine_label", "")
        if replace_reader:
            self.audio.stop()
            self.sentences = list(res.get("translated_sentences") or split_into_sentences(translated))
            self.speaker_roles = []
            self.speaker_labels = []
            self.current_script_data = None
            self.active_sentence_index = 0
            self.btn_restore_original.setVisible(True)
            self.btn_export_script.setVisible(False)
            self.render_text()
            self.update_progress_display()
            self._persist_progress_only()
            self.statusBar.showMessage(f"הפרק תורגם ונשמר במלואו ({engine_label}) ומוכן ליצירת תסריט/האזנה!", 6000)
        else:
            src_paras = res.get("source_paragraphs", [])
            dst_paras = res.get("target_paragraphs", [])
            md_blocks = [f"### תרגום ({engine_label})\n"]
            max_len = max(len(src_paras), len(dst_paras))
            for i in range(max_len):
                if i < len(src_paras):
                    md_blocks.append(f"**מקור:** {src_paras[i]}")
                if i < len(dst_paras):
                    md_blocks.append(f"**תרגום:** {dst_paras[i]}\n---")
            self.lbl_comp_title.setText(f"🌐 תרגום משולב ({engine_label})")
            self.companion_widget.setVisible(True)
            self.comp_text.setMarkdown("\n\n".join(md_blocks))
            self.statusBar.showMessage(f"התרגום הושלם ונשמר ({engine_label}).", 5000)
        self._refresh_resume_banner(force=True)

    # --- Enforced Audiobook Export Pipeline (פרק נוכחי / הספר כולו) ---
    def export_whole_book_audio_dialog(self):
        idx = self.combo_scope.findData("book")
        if idx >= 0:
            self.combo_scope.setCurrentIndex(idx)
        self.export_audio_dialog(forced_scope="book")

    def export_audio_dialog(self, forced_scope: Optional[str] = None):
        if not isinstance(forced_scope, str):
            forced_scope = None
        book, chapter = self._get_current_book_and_chapter()
        if not book or not chapter:
            QMessageBox.information(self, "אין ספר", "אין ספר פעיל לייצוא שמע.")
            return

        if self._export_thread and self._export_thread.isRunning():
            QMessageBox.information(self, "ייצוא בתהליך", "ייצוא שמע קודם עדיין רץ ברקע, אנא המתן לסיומו.")
            return

        source_text = chapter.get("text", "") or ""
        book_id = book.get("id", "")
        chapter_idx = self.current_chapter_index
        default_scope = forced_scope or (self.combo_scope.currentData() or "chapter")

        pipe_dlg = ExportPipelineDialog(
            self,
            book_id=book_id,
            chapter_index=chapter_idx,
            source_text=source_text,
            settings=self.settings,
            store=self.store,
            default_voice=self.combo_voice.currentData() or "edge_avri",
            default_speed=self.audio.speed,
            book=book,
            default_scope=default_scope,
        )
        if not pipe_dlg.exec():
            return
        opts = pipe_dlg.get_options()
        scope = opts.get("scope", "chapter")
        package_mode = opts.get("package_mode", "single_chapter")
        fmt = opts["tts_opts"]["output_format"]
        safe_title = sanitize_filename_part(book.get("title", ""), "audiobook")

        if scope == "book":
            allow_partial_pdf = bool(opts.get("allow_partial_pdf", False))
            cov_check = validate_book_completeness(book, allow_partial_pdf=allow_partial_pdf)
            if not cov_check["is_complete"]:
                QMessageBox.warning(
                    self,
                    "לא ניתן לייצא ספר חלקי כספר שלם",
                    f"{cov_check['reason']}\n\nלא ניתן לסמן ספר חלקי כהושלם.",
                )
                return

            if package_mode == "unified_mp3" and not find_ffmpeg_executable():
                QMessageBox.warning(
                    self,
                    "נדרש FFmpeg לאיחוד MP3",
                    "איחוד MP3 מאוחד מחייב פענוח וקידוד מחדש עם FFmpeg (לא חיבור בינארי).\n"
                    "כרגע FFmpeg אינו זמין בסביבה — בחר באריזת ZIP/תיקייה של קובצי MP3 או ב-WAV מאוחד.",
                )
                return

            if package_mode == "folder":
                chosen_dir = QFileDialog.getExistingDirectory(
                    self,
                    "בחר תיקייה לשמירת קובצי השמע של הספר כולו",
                )
                if not chosen_dir:
                    return
                target_path = os.path.join(chosen_dir, f"{safe_title}_full_book")
            elif package_mode == "zip":
                target_path, _ = QFileDialog.getSaveFileName(
                    self,
                    "בחר יעד לשמירת ארכיון ZIP של הספר כולו",
                    f"{safe_title}_full_book.zip",
                    "ארכיון ZIP של פרקי השמע + manifest (*.zip)",
                )
                if not target_path:
                    return
                if not target_path.lower().endswith(".zip"):
                    target_path = f"{os.path.splitext(target_path)[0]}.zip"
            elif package_mode == "unified_wav":
                target_path, _ = QFileDialog.getSaveFileName(
                    self,
                    "בחר יעד לשמירת קובץ WAV מאוחד לכל הספר",
                    f"{safe_title}_full_book.wav",
                    "קובץ שמע WAV מאוחד לכל הספר (*.wav)",
                )
                if not target_path:
                    return
                if not target_path.lower().endswith(".wav"):
                    target_path = f"{os.path.splitext(target_path)[0]}.wav"
            else:
                target_path, _ = QFileDialog.getSaveFileName(
                    self,
                    "בחר יעד לשמירת קובץ MP3 מאוחד לכל הספר",
                    f"{safe_title}_full_book.mp3",
                    "קובץ שמע MP3 מאוחד לכל הספר (*.mp3)",
                )
                if not target_path:
                    return
                if not target_path.lower().endswith(".mp3"):
                    target_path = f"{os.path.splitext(target_path)[0]}.mp3"

            if "fallback_to_ai_on_failure" in (opts.get("translation_opts") or {}):
                self._apply_fallback_policy_preference(bool(opts["translation_opts"]["fallback_to_ai_on_failure"]))

            self._launch_whole_book_export_thread({
                "book": deepcopy(book),
                "output_path": target_path,
                "package_mode": package_mode,
                "translation_opts": opts["translation_opts"],
                "script_opts": opts["script_opts"],
                "tts_opts": opts["tts_opts"],
                "target_chapters": opts.get("target_chapters"),
                "allow_partial_pdf": allow_partial_pdf,
            })
            return
        else:
            default_name = f"{safe_title}_chapter_{chapter_idx + 1}.{fmt}"
            filter_str = (
                "קובץ שמע MP3 לפרק (*.mp3)"
                if fmt == "mp3"
                else "קובץ שמע WAV אופליין לפרק (*.wav)"
            )
            target_path, _ = QFileDialog.getSaveFileName(
                self,
                f"בחר יעד לשמירת שמע הפרק הנוכחי בלבד (פרק {chapter_idx + 1})",
                default_name,
                filter_str,
            )
            if not target_path:
                return
            if fmt == "wav" and not target_path.lower().endswith(".wav"):
                target_path = f"{os.path.splitext(target_path)[0]}.wav"
            elif fmt == "mp3" and not target_path.lower().endswith(".mp3"):
                target_path = f"{os.path.splitext(target_path)[0]}.mp3"

            _retire_window_thread(self._export_thread, wait_ms=100)
            self.statusBar.showMessage(f"מפעיל צינור יצוא מלא לפרק {chapter_idx + 1}...")
            self._export_thread = PipelineExportThread(
                {
                    "book_id": book_id,
                    "chapter_index": chapter_idx,
                    "source_text": source_text,
                    "output_path": target_path,
                    "book_title": book["title"],
                    "settings": deepcopy(self.settings),
                    "translation_opts": opts["translation_opts"],
                    "script_opts": opts["script_opts"],
                    "tts_opts": opts["tts_opts"],
                    "store": self.store,
                },
                scope="chapter",
            )
            dlg_title = f"🎧 מייצא פרק {chapter_idx + 1} בלבד"
            initial_lbl = "מתחיל תהליך יצוא הפרק הנוכחי..."

        prog_dlg = QProgressDialog(initial_lbl, "🛑 בטל יצוא", 0, 0, self)
        prog_dlg.setWindowTitle(dlg_title)
        prog_dlg.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        prog_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        prog_dlg.setMinimumWidth(440)
        prog_dlg.canceled.connect(self._export_thread.cancel)
        self._export_progress_dlg = prog_dlg

        def _on_prog(msg: str):
            self.statusBar.showMessage(msg)
            if self._export_progress_dlg:
                self._export_progress_dlg.setLabelText(msg)

        self._export_thread.progress.connect(_on_prog)
        self._export_thread.finished_result.connect(self._on_pipeline_finished)
        self._export_thread.start()
        prog_dlg.show()

    def _on_pipeline_finished(self, res: dict):
        if self._export_progress_dlg:
            _safe_close_progress_dialog(self._export_progress_dlg)
            self._export_progress_dlg = None

        self._refresh_resume_banner(force=True)

        scope = res.get("scope", "chapter")
        if res.get("success") and res.get("status") == "completed":
            saved_path = res.get("output_path", "")
            script_rec = res.get("script")
            if isinstance(script_rec, dict) and script_rec.get("lines"):
                self._on_script_generated(script_rec)

            if scope in ("book", "range"):
                ch_cnt = res.get("selected_count") or res.get("chapter_count", 0)
                scope_txt = "טווח המקטעים שנבחר" if scope == "range" else "הספר כולו"
                title_txt = "📚🎧 שמע מוכן להעברה!"
                body_txt = (
                    f"הושלמו כל השלבים (תרגום ← תסריט ← TTS ← אריזה) עבור {ch_cnt} מקטעים ({scope_txt}) של '{res.get('book_title', '')}':\n\n"
                    f"{saved_path}"
                )
                self.statusBar.showMessage(f"שמע ({ch_cnt} מקטעים) נשמר בהצלחה: {saved_path}", 8000)
            else:
                title_txt = "🎧 קובץ השמע של הפרק מוכן להעברה!"
                body_txt = f"יצוא הפרק הנוכחי הושלם בהצלחה (פרק בודד, לא הספר כולו):\n\n{saved_path}"
                self.statusBar.showMessage(f"שמע הפרק נשמר בהצלחה: {saved_path}", 7000)

            msg = QMessageBox(self)
            msg.setWindowTitle(title_txt)
            msg.setText(body_txt)
            btn_open_folder = msg.addButton("📂 פתח תיקייה", QMessageBox.ButtonRole.ActionRole)
            msg.addButton("אישור", QMessageBox.ButtonRole.AcceptRole)
            msg.exec()
            if msg.clickedButton() == btn_open_folder and saved_path:
                folder = saved_path if os.path.isdir(saved_path) else os.path.dirname(os.path.abspath(saved_path))
                try:
                    if os.name == "nt":
                        os.startfile(folder)
                except Exception:
                    pass
        elif res.get("status") == "cancelled":
            self.statusBar.showMessage("היצוא בוטל — התקדמות תקינה נשמרה אטומית.", 5000)
            QMessageBox.information(
                self,
                "היצוא בוטל",
                f"התהליך בוטל בשלב '{res.get('stage', '')}'. לא נוצר קובץ סופי חלקי (יחידות ופרקים קודמים נשמרו להמשך).",
            )
        elif scope in ("book", "range"):
            self.statusBar.showMessage("ייצוא הספר נעצר — התקדמות תקינה נשמרה להתאוששות.", 6000)
            self._handle_whole_book_recovery(res, is_export=True)
        else:
            stage = res.get("stage", "")
            err = res.get("error", "שגיאה לא ידועה")
            self.statusBar.showMessage(f"היצוא נכשל בשלב {stage}.", 6000)
            QMessageBox.critical(
                self,
                "היצוא נכשל (לא הוכרז כהושלם)",
                f"כשל בשלב '{stage}':\n\n{err}",
            )

    def closeEvent(self, event):
        try:
            if hasattr(self, "_progress_save_timer"):
                self._progress_save_timer.stop()
            self._persist_library_and_progress()
            self.audio.shutdown(wait_ms=1000)
            _safe_close_progress_dialog(self._task_progress_dlg)
            self._task_progress_dlg = None
            _safe_close_progress_dialog(self._whole_book_script_progress_dlg)
            self._whole_book_script_progress_dlg = None
            _safe_close_progress_dialog(self._export_progress_dlg)
            self._export_progress_dlg = None

            _retire_window_thread(self._file_thread, cancel=False, wait_ms=600)
            self._file_thread = None
            _retire_window_thread(self._task_thread, cancel=True, wait_ms=800)
            self._task_thread = None
            _retire_window_thread(self._whole_book_script_thread, cancel=True, wait_ms=900)
            self._whole_book_script_thread = None
            _retire_window_thread(self._export_thread, cancel=True, wait_ms=900)
            self._export_thread = None
        except Exception:
            pass
        super().closeEvent(event)
