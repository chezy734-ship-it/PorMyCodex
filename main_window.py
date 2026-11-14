# -*- coding: utf-8 -*-
"""
חלון הממשק הראשי (MainWindow) של תוכנת PorMyCodex ב-PyQt6.
כולל:
- טעינת קבצי PDF / DOCX / TXT גדולים ברקע (QThread) עם חלוקה אוטומטית לחלקים ורינדור מוגן קריסות
- יצירת תסריט AI (לימודי מורה-תלמיד / דרמטי) והקראה רב-קולית (אברי + הילה)
- תרגום פרקים עם AI או מנועים חינמיים (Google Translate / MyMemory / LibreTranslate)
- בחירת ספקי AI מרובים (Google / OpenAI / Claude / OpenRouter / Groq / שרת מותאם) וגילוי מודלים
"""

import os
import html
import json
import threading
from typing import List, Dict, Any, Optional, Callable
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QPushButton, QComboBox, QSlider, QProgressBar,
    QListWidget, QListWidgetItem, QTextBrowser, QFileDialog,
    QMessageBox, QToolBar, QStatusBar, QFrame, QInputDialog,
    QProgressDialog
)
from PyQt6.QtGui import QAction, QFont, QKeySequence, QShortcut

from book_loader import SAMPLE_BOOKS, load_file_content, split_into_sentences
from audio_engine import AudioEngine
from ai_linguist import vocalize_and_expand_hebrew, ask_companion
from settings_manager import load_settings, get_active_provider_config
from ai_providers import get_provider_label
from script_engine import format_script_as_text
from wiki_engine import enrich_with_wikipedia_and_ai, fetch_wikipedia_context_for_terms
from library_store import LibraryStore
from export_pipeline import (
    assess_chapter_pipeline,
    ensure_translation_for_chapter,
    ensure_script_for_chapter,
    run_enforced_export_pipeline,
)
from dialogs import (
    SettingsDialog,
    TranslateDialog,
    ScriptStudioDialog,
    WikiSearchDialog,
    ExportPipelineDialog,
)


class PipelineExportThread(QThread):
    progress = pyqtSignal(str)
    finished_result = pyqtSignal(dict)

    def __init__(self, pipeline_kwargs: Dict[str, Any]):
        super().__init__()
        self.cancel_event = threading.Event()
        self.pipeline_kwargs = dict(pipeline_kwargs)
        self.pipeline_kwargs["cancel_event"] = self.cancel_event
        self.pipeline_kwargs["progress_cb"] = lambda msg: self.progress.emit(msg)

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        res = run_enforced_export_pipeline(**self.pipeline_kwargs)
        self.finished_result.emit(res)


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
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            res = self.fn()
            self.finished_ok.emit(res)
        except Exception as e:
            self.failed.emit(str(e))


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
        self._export_thread: Optional[PipelineExportThread] = None
        self._export_progress_dlg: Optional[QProgressDialog] = None

        # Audio Engine
        self.audio = AudioEngine()
        self.audio.voice_type = prog.get("voice_type", "edge_avri") or "edge_avri"
        self.audio.speed = float(prog.get("speed", 1.0) or 1.0)
        self.audio.sentence_changed.connect(self.on_sentence_changed)
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

        settings_action = QAction("⚙️ הגדרות AI ומודלים", self)
        settings_action.setToolTip("בחר חברת AI, הזן מפתח API ושאב מודלים זמינים")
        settings_action.triggered.connect(self.open_settings_dialog)
        toolbar.addAction(settings_action)

        toolbar.addSeparator()

        vocalize_action = QAction("✨ נקד AI", self)
        vocalize_action.triggered.connect(self.vocalize_current_chapter)
        toolbar.addAction(vocalize_action)

        script_action = QAction("🎬 תסריט AI", self)
        script_action.triggered.connect(self.open_script_studio)
        toolbar.addAction(script_action)

        translate_action = QAction("🌐 תרגום", self)
        translate_action.triggered.connect(self.open_translate_dialog)
        toolbar.addAction(translate_action)

        export_audio_action = QAction("🎧 ייצא MP3", self)
        export_audio_action.setToolTip("ייצא קובץ שמע MP3 / WAV נייד להעברה")
        export_audio_action.triggered.connect(self.export_audio_dialog)
        toolbar.addAction(export_audio_action)

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
        self.lbl_progress.setFixedWidth(145)
        player_layout.addWidget(self.lbl_progress)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setValue(0)
        player_layout.addWidget(self.progress_bar, 1)

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

    def _restore_initial_position(self, book_idx: int, chapter_idx: int, sentence_idx: int):
        self.load_book_to_ui(book_idx)
        if not self.books:
            return
        book = self.books[self.current_book_index]
        chapters = book.get("chapters") or []
        if 0 <= chapter_idx < len(chapters):
            self.chapter_list.setCurrentRow(chapter_idx)
        if self.sentences:
            self.active_sentence_index = max(0, min(len(self.sentences) - 1, sentence_idx))
            self.render_text()
            self.update_progress_display()

    def _persist_library_and_progress(self):
        if getattr(self, "_restoring_state", False) or not self.books:
            return
        book = self.books[self.current_book_index]
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

    def _persist_progress_only(self):
        if getattr(self, "_restoring_state", False) or not self.books:
            return
        book = self.books[self.current_book_index]
        self.store.update_reading_progress(
            book_id=book.get("id", ""),
            chapter_index=self.current_chapter_index,
            sentence_index=self.active_sentence_index,
            font_size=self.font_size,
            voice_type=self.combo_voice.currentData() or self.audio.voice_type,
            speed=self.audio.speed,
        )

    def load_book_to_ui(self, book_idx: int):
        self.book_list.clear()
        for b in self.books:
            self.book_list.addItem(QListWidgetItem(f"📖 {b['title']}"))
        if 0 <= book_idx < len(self.books):
            self.book_list.setCurrentRow(book_idx)

    def on_book_selected(self, row: int):
        if row < 0 or row >= len(self.books):
            return
        self.current_book_index = row
        book = self.books[row]
        self.chapter_list.clear()
        for idx, ch in enumerate(book["chapters"]):
            self.chapter_list.addItem(f"{idx+1}. {ch['title']}")
        if book["chapters"]:
            self.chapter_list.setCurrentRow(0)

    def on_chapter_selected(self, row: int):
        if row < 0:
            return
        book = self.books[self.current_book_index]
        if row >= len(book["chapters"]):
            return
        self.current_chapter_index = row
        chapter = book["chapters"][row]

        self.lbl_current_title.setText(f"{book['title']} — {chapter['title']}")
        self.sentences = split_into_sentences(chapter["text"])
        self.speaker_roles = []
        self.speaker_labels = []
        self.current_script_data = None
        self.btn_restore_original.setVisible(False)
        self.btn_export_script.setVisible(False)
        self.active_sentence_index = 0

        # טעינת התקדמות קריאה שמורה לפרק (אם איננה במצב שחזור ראשוני שמגדיר אינדקס ספציפי)
        raw_state = self.store.load_raw_state()
        saved_pos = (raw_state.get("reading_progress", {}).get("chapter_positions", {}) or {}).get(
            f"{book.get('id', '')}::{row}"
        )
        if isinstance(saved_pos, int) and 0 <= saved_pos < len(self.sentences):
            self.active_sentence_index = saved_pos

        # אם קיימים תוצרים עדכניים ושאינם ישנים לפרק זה (תסריט / תרגום), מסמנים זאת למשתמש
        status = assess_chapter_pipeline(
            book_id=book.get("id", ""),
            chapter_index=row,
            source_text=chapter["text"],
            settings=self.settings,
            store=self.store,
        )
        if status["script_ready"] and status["script_record"]:
            s_rec = status["script_record"]
            lines = s_rec.get("lines", [])
            self.current_script_data = s_rec
            self.sentences = [line["text"] for line in lines]
            self.speaker_roles = [line["speakerRole"] for line in lines]
            self.speaker_labels = [
                f"{line['speakerLabel']} [{line['style']}]" if line.get("style") else line["speakerLabel"]
                for line in lines
            ]
            self.active_sentence_index = min(self.active_sentence_index, max(0, len(self.sentences) - 1))
            self.btn_restore_original.setVisible(True)
            self.btn_export_script.setVisible(True)
        elif status["translation_ready"] and status["translation_record"]:
            t_rec = status["translation_record"]
            self.sentences = list(t_rec.get("translated_sentences") or split_into_sentences(t_rec.get("translated_text", "")))
            self.active_sentence_index = min(self.active_sentence_index, max(0, len(self.sentences) - 1))
            self.btn_restore_original.setVisible(True)

        self.render_text()
        self.update_progress_display()
        self.audio.stop()
        self._persist_progress_only()

    def restore_original_chapter_text(self):
        if not self.books:
            return
        book = self.books[self.current_book_index]
        chapter = book["chapters"][self.current_chapter_index]
        self.audio.stop()
        self.sentences = split_into_sentences(chapter["text"])
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
        self.active_sentence_index = idx
        self.render_text()
        self.update_progress_display()
        self._persist_progress_only()

    def update_progress_display(self):
        total = len(self.sentences)
        current = self.active_sentence_index + 1 if total > 0 else 0
        self.lbl_progress.setText(f"משפט {current} מתוך {total}")
        if total > 0:
            self.progress_bar.setValue(int((current / total) * 100))
        else:
            self.progress_bar.setValue(0)

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
        if self.audio.is_playing():
            self.audio.pause()
        else:
            if self.audio.worker and self.audio.worker._is_paused:
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
        voice_id = self.combo_voice.currentData()
        self.audio.voice_type = voice_id
        self._persist_progress_only()
        if self.audio.is_playing():
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
        self.statusBar.showMessage(f"הספר '{new_book['title']}' נשמר ונטען בהצלחה ({ch_count} פרקים/חלקים)!", 6000)

    def _on_file_load_failed(self, err: str):
        self.statusBar.showMessage("טעינת הקובץ נכשלה.", 4000)
        QMessageBox.critical(self, "שגיאה בטעינת קובץ", err)

    def paste_custom_text(self):
        text, ok = QInputDialog.getMultiLineText(self, "הדבקת טקסט", "הדבק כאן טקסט בעברית או באנגלית לקריאה/תרגום:")
        if ok and text.strip():
            new_book = {
                "id": f"custom_{len(self.books)}_{abs(hash(text.strip())) % 100000}",
                "title": f"טקסט מותאם אישית ({len(self.books) + 1})",
                "author": "הדבקה אישית",
                "description": "טקסט שהודבק במערכת",
                "chapters": [{"title": "פרק א׳", "text": text.strip()}]
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
            if self.books:
                book = self.books[self.current_book_index]
                chapter = book["chapters"][self.current_chapter_index]
                status = assess_chapter_pipeline(
                    book_id=book.get("id", ""),
                    chapter_index=self.current_chapter_index,
                    source_text=chapter["text"],
                    settings=self.settings,
                    store=self.store,
                )
                if self.current_script_data and not status["script_ready"]:
                    self.current_script_data = None
                    self.speaker_roles = []
                    self.speaker_labels = []
                    self.sentences = split_into_sentences(chapter["text"])
                    self.btn_export_script.setVisible(False)
                    self.btn_restore_original.setVisible(False)
                    self.render_text()
                    self.update_progress_display()
                    self.statusBar.showMessage("ההגדרות נשמרו; התסריט הקודם התיישן ולא ישמש ליצוא.", 6000)
                    return
            self.statusBar.showMessage("הגדרות ה-AI, המודלים, התרגום והקולות נשמרו!", 4000)

    # --- Background Worker Helper ---
    def _run_background_task(self, status_msg: str, fn: Callable[[], Any], on_ok: Callable[[Any], None]):
        if self._task_thread and self._task_thread.isRunning():
            QMessageBox.information(self, "פעולה רצה", "משימת AI/תרגום אחרת עדיין רצה ברקע, אנא המתן לסיומה.")
            return

        self.statusBar.showMessage(status_msg)
        self._task_thread = TaskWorkerThread(fn)
        self._task_thread.finished_ok.connect(on_ok)
        self._task_thread.failed.connect(self._on_background_task_error)
        self._task_thread.start()

    def _on_background_task_error(self, err: str):
        self.statusBar.showMessage("הפעולה הופסקה עם שגיאה.", 5000)
        QMessageBox.warning(self, "שגיאה", err)

    # --- AI Vocalization & Companion ---
    def vocalize_current_chapter(self):
        if not self.books:
            return
        chapter_text = "\n".join(self.sentences) if self.sentences else self.books[self.current_book_index]["chapters"][self.current_chapter_index]["text"]
        cfg = self.settings

        self._run_background_task(
            "מפעיל בלשן AI לניקוד ופתיחת קיצורים...",
            lambda: vocalize_and_expand_hebrew(chapter_text, settings=cfg),
            self._on_vocalize_done,
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
        book = self.books[self.current_book_index]
        chapter = book["chapters"][self.current_chapter_index]
        target_text = self.sentences[self.active_sentence_index] if mode == "explain" and self.sentences else "\n".join(self.sentences[:120]) or chapter["text"]

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
            lambda: ask_companion(target_text, mode=mode, book_title=book["title"], settings=cfg),
            lambda reply: (
                self.comp_text.setMarkdown(str(reply)),
                self.statusBar.showMessage("הניתוח הסתיים.", 3500),
            ),
        )

    # --- Wikipedia Search & AI Knowledge Grounding ---
    def open_wikipedia_dialog(self):
        if not self.books:
            return
        book = self.books[self.current_book_index]
        active_sentence = ""
        if self.sentences and 0 <= self.active_sentence_index < len(self.sentences):
            active_sentence = self.sentences[self.active_sentence_index]
        chapter_context = active_sentence or "\n".join(self.sentences[:60]) or book["chapters"][self.current_chapter_index]["text"][:2500]

        dlg = WikiSearchDialog(self, self.settings)
        if not dlg.exec():
            return
        opts = dlg.get_options()
        cfg = self.settings

        self.lbl_comp_title.setText("📚 מחקר מושגים — ויקיפדיה + AI")
        self.companion_widget.setVisible(True)
        self.comp_text.setHtml("<div dir='rtl'><i>מחפש ערכים בוויקיפדיה ומרחיב את הידע ברקע...</i></div>")

        def _worker():
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

    # --- AI Script Studio (מבטיח תרגום עדכני תחילה ושומר תוצר אטומית) ---
    def open_script_studio(self):
        dlg = ScriptStudioDialog(self, self.settings)
        if not dlg.exec():
            return
        opts = dlg.get_options()
        book = self.books[self.current_book_index]
        chapter = book["chapters"][self.current_chapter_index]
        source_text = chapter["text"]
        cfg = self.settings
        book_id = book.get("id", "")
        chapter_idx = self.current_chapter_index

        def _worker():
            ensure_translation_for_chapter(
                book_id=book_id,
                chapter_index=chapter_idx,
                source_text=source_text,
                settings=cfg,
                store=self.store,
            )
            return ensure_script_for_chapter(
                book_id=book_id,
                chapter_index=chapter_idx,
                source_text=source_text,
                book_title=book["title"],
                settings=cfg,
                script_opts=opts,
                store=self.store,
            )

        status_msg = (
            "מאמת תרגום ומייצר תסריט AI מועשר בוויקיפדיה..."
            if opts.get("use_wikipedia")
            else "מאמת תרגום ומייצר תסריט AI מלא ברקע..."
        )
        self._run_background_task(
            status_msg,
            _worker,
            self._on_script_generated,
        )

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
    def open_translate_dialog(self):
        dlg = TranslateDialog(self, self.settings)
        if not dlg.exec():
            return
        opts = dlg.get_options()
        book = self.books[self.current_book_index]
        chapter = book["chapters"][self.current_chapter_index]
        source_text = chapter["text"]
        cfg = self.settings
        book_id = book.get("id", "")
        chapter_idx = self.current_chapter_index

        self._run_background_task(
            "מתרגם את כל משפטֵי הפרק ושומר אטומית...",
            lambda: ensure_translation_for_chapter(
                book_id=book_id,
                chapter_index=chapter_idx,
                source_text=source_text,
                settings=cfg,
                translation_opts=opts,
                store=self.store,
            ),
            lambda res: self._on_translation_done(res, replace_reader=opts["replace_reader"]),
        )

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

    # --- Enforced Audiobook Export Pipeline (תרגום -> תסריט -> TTS -> יצוא) ---
    def export_audio_dialog(self):
        if not self.books:
            QMessageBox.information(self, "אין ספר", "אין ספר פעיל לייצוא שמע.")
            return

        if self._export_thread and self._export_thread.isRunning():
            QMessageBox.information(self, "ייצוא בתהליך", "ייצוא שמע קודם עדיין רץ ברקע, אנא המתן לסיומו.")
            return

        book = self.books[self.current_book_index]
        chapter = book["chapters"][self.current_chapter_index]
        source_text = chapter["text"]
        book_id = book.get("id", "")
        chapter_idx = self.current_chapter_index

        pipe_dlg = ExportPipelineDialog(
            self,
            book_id=book_id,
            chapter_index=chapter_idx,
            source_text=source_text,
            settings=self.settings,
            store=self.store,
            default_voice=self.combo_voice.currentData() or "edge_avri",
            default_speed=self.audio.speed,
        )
        if not pipe_dlg.exec():
            return
        opts = pipe_dlg.get_options()
        fmt = opts["tts_opts"]["output_format"]

        safe_title = "".join(c for c in book["title"] if c.isalnum() or c in (" ", "-", "_")).strip() or "audiobook"
        default_name = f"{safe_title}_part{chapter_idx + 1}.{fmt}"
        filter_str = (
            "קובץ שמע MP3 באיכות אולפן (*.mp3)"
            if fmt == "mp3"
            else "קובץ שמע WAV אופליין (*.wav)"
        )

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "בחר יעד לשמירת קובץ השמע המלא",
            default_name,
            filter_str,
        )
        if not file_path:
            return

        if fmt == "wav" and not file_path.lower().endswith(".wav"):
            file_path = f"{os.path.splitext(file_path)[0]}.wav"
        elif fmt == "mp3" and not file_path.lower().endswith(".mp3"):
            file_path = f"{os.path.splitext(file_path)[0]}.mp3"

        self.statusBar.showMessage("מפעיל צינור יצוא מלא (תרגום ← תסריט ← TTS ← שמירה)...")
        self._export_thread = PipelineExportThread({
            "book_id": book_id,
            "chapter_index": chapter_idx,
            "source_text": source_text,
            "output_path": file_path,
            "book_title": book["title"],
            "settings": self.settings,
            "translation_opts": opts["translation_opts"],
            "script_opts": opts["script_opts"],
            "tts_opts": opts["tts_opts"],
            "store": self.store,
        })

        prog_dlg = QProgressDialog("מתחיל תהליך יצוא מלא...", "🛑 בטל יצוא", 0, 0, self)
        prog_dlg.setWindowTitle("מייצא ספר קולי מלא")
        prog_dlg.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        prog_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        prog_dlg.setMinimumWidth(420)
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
            self._export_progress_dlg.close()
            self._export_progress_dlg = None

        if res.get("success") and res.get("status") == "completed":
            saved_path = res.get("output_path", "")
            # סנכרון תצוגת התסריט העדכני שנוצר בצינור
            script_rec = res.get("script")
            if isinstance(script_rec, dict) and script_rec.get("lines"):
                self._on_script_generated(script_rec)

            self.statusBar.showMessage(f"קובץ השמע המלא נשמר בהצלחה: {saved_path}", 7000)
            msg = QMessageBox(self)
            msg.setWindowTitle("🎧 קובץ השמע המלא מוכן להעברה!")
            msg.setText(f"כל השלבים (תרגום ← תסריט ← TTS ← יצוא) הושלמו בהצלחה:\n\n{saved_path}")
            btn_open_folder = msg.addButton("📂 פתח תיקייה", QMessageBox.ButtonRole.ActionRole)
            msg.addButton("אישור", QMessageBox.ButtonRole.AcceptRole)
            msg.exec()
            if msg.clickedButton() == btn_open_folder and saved_path:
                folder = os.path.dirname(os.path.abspath(saved_path))
                try:
                    if os.name == "nt":
                        os.startfile(folder)
                except Exception:
                    pass
        elif res.get("status") == "cancelled":
            self.statusBar.showMessage("היצוא בוטל — לא נשמר קובץ חלקי.", 5000)
            QMessageBox.information(
                self,
                "היצוא בוטל",
                f"התהליך בוטל בשלב '{res.get('stage', '')}'. לא נוצר קובץ חלקי ביעד.",
            )
        else:
            stage = res.get("stage", "")
            err = res.get("error", "שגיאה לא ידועה")
            self.statusBar.showMessage(f"היצוא נכשל בשלב {stage}.", 6000)
            QMessageBox.critical(
                self,
                "היצוא נכשל (לא נוצר קובץ)",
                f"כשל בשלב '{stage}':\n\n{err}",
            )

    def closeEvent(self, event):
        try:
            self._persist_library_and_progress()
            self.audio.stop()
            if self._file_thread and self._file_thread.isRunning():
                self._file_thread.wait(800)
            if self._task_thread and self._task_thread.isRunning():
                self._task_thread.wait(800)
            if self._export_thread and self._export_thread.isRunning():
                self._export_thread.cancel()
                self._export_thread.wait(1200)
        except Exception:
            pass
        super().closeEvent(event)
