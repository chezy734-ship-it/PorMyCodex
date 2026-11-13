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
from typing import List, Dict, Any, Optional, Callable
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QPushButton, QComboBox, QSlider, QProgressBar,
    QListWidget, QListWidgetItem, QTextBrowser, QFileDialog,
    QMessageBox, QToolBar, QStatusBar, QFrame, QInputDialog
)
from PyQt6.QtGui import QAction, QFont, QKeySequence, QShortcut

from book_loader import SAMPLE_BOOKS, load_file_content, split_into_sentences
from audio_engine import AudioEngine
from ai_linguist import vocalize_and_expand_hebrew, ask_companion
from settings_manager import load_settings, get_active_provider_config
from ai_providers import get_provider_label
from translator_engine import translate_text
from script_engine import generate_narration_script, format_script_as_text
from audio_exporter import export_audiobook_file
from dialogs import SettingsDialog, TranslateDialog, ScriptStudioDialog


class AudioExportThread(QThread):
    progress = pyqtSignal(str)
    finished_path = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        sentences: List[str],
        output_path: str,
        default_voice: str,
        speed: float,
        voice_overrides: Optional[List[str]] = None,
    ):
        super().__init__()
        self.sentences = list(sentences)
        self.output_path = output_path
        self.default_voice = default_voice
        self.speed = speed
        self.voice_overrides = list(voice_overrides) if voice_overrides else None

    def run(self):
        try:
            saved = export_audiobook_file(
                sentences=self.sentences,
                output_path=self.output_path,
                default_voice=self.default_voice,
                speed=self.speed,
                voice_overrides=self.voice_overrides,
                progress_cb=lambda msg: self.progress.emit(msg),
            )
            self.finished_path.emit(saved)
        except Exception as e:
            self.failed.emit(str(e))


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

        # State
        self.settings: Dict[str, Any] = load_settings()
        self.books: List[Dict[str, Any]] = list(SAMPLE_BOOKS)
        self.current_book_index: int = 0
        self.current_chapter_index: int = 0
        self.sentences: List[str] = []
        self.speaker_roles: List[str] = []       # תפקיד דובר לכל משפט (ריק במצב רגיל)
        self.speaker_labels: List[str] = []      # תווית תצוגה (🎓 מורה / 🙋 תלמיד / 🎙️ מספר)
        self.current_script_data: Optional[Dict[str, Any]] = None
        self.active_sentence_index: int = 0
        self.font_size: int = 18

        self._file_thread: Optional[FileLoadThread] = None
        self._task_thread: Optional[TaskWorkerThread] = None
        self._export_thread: Optional[AudioExportThread] = None

        # Audio Engine
        self.audio = AudioEngine()
        self.audio.sentence_changed.connect(self.on_sentence_changed)
        self.audio.state_changed.connect(self.on_playback_state_changed)
        self.audio.error_occurred.connect(self.on_audio_error)

        self.init_ui()
        self.load_book_to_ui(0)
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

        # Top Toolbar
        toolbar = QToolBar("פעולות ספרייה ו-AI")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        open_action = QAction("📂 פתח ספר (PDF/Word/TXT)", self)
        open_action.triggered.connect(self.open_file_dialog)
        toolbar.addAction(open_action)

        paste_action = QAction("📋 הדבק טקסט", self)
        paste_action.triggered.connect(self.paste_custom_text)
        toolbar.addAction(paste_action)

        toolbar.addSeparator()

        vocalize_action = QAction("✨ נקד AI", self)
        vocalize_action.triggered.connect(self.vocalize_current_chapter)
        toolbar.addAction(vocalize_action)

        script_action = QAction("🎬 צור תסריט AI", self)
        script_action.triggered.connect(self.open_script_studio)
        toolbar.addAction(script_action)

        translate_action = QAction("🌐 תרגום (AI / חינמי)", self)
        translate_action.triggered.connect(self.open_translate_dialog)
        toolbar.addAction(translate_action)

        export_audio_action = QAction("🎧 ייצא שמע (MP3/WAV)", self)
        export_audio_action.triggered.connect(self.export_audio_dialog)
        toolbar.addAction(export_audio_action)

        toolbar.addSeparator()

        explain_action = QAction("💡 באר פסקה", self)
        explain_action.triggered.connect(lambda: self.trigger_companion("explain"))
        toolbar.addAction(explain_action)

        summary_action = QAction("📝 סכם פרק", self)
        summary_action.triggered.connect(lambda: self.trigger_companion("summary"))
        toolbar.addAction(summary_action)

        quiz_action = QAction("❓ שאלות הבנה", self)
        quiz_action.triggered.connect(lambda: self.trigger_companion("quiz"))
        toolbar.addAction(quiz_action)

        toolbar.addSeparator()

        settings_action = QAction("⚙️ הגדרות AI ומודלים", self)
        settings_action.triggered.connect(self.open_settings_dialog)
        toolbar.addAction(settings_action)

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

        self.lbl_ai_badge = QLabel("")
        self.lbl_ai_badge.setStyleSheet(
            "background-color: #1e293b; color: #38bdf8; border: 1px solid #334155; "
            "border-radius: 6px; padding: 4px 10px; font-size: 11px; font-weight: bold;"
        )
        header_row.addWidget(self.lbl_ai_badge)

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
        self.comp_text.setFont(QFont("Arial", 12))
        comp_layout.addWidget(self.comp_text)

        splitter.addWidget(self.companion_widget)
        self.companion_widget.setVisible(False)

        splitter.setSizes([260, 680, 300])

        # --- Bottom Player Bar ---
        player_bar = QFrame()
        player_bar.setStyleSheet("background-color: #1e293b; border-top: 1px solid #334155; padding: 6px;")
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
        self.lbl_ai_badge.setText(f"{icon} {get_provider_label(pid)} | {model}")

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
        self.render_text()
        self.update_progress_display()
        self.audio.stop()

    def restore_original_chapter_text(self):
        self.on_chapter_selected(self.current_chapter_index)
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
        if self.audio.is_playing():
            self.audio.play(self.sentences, new_idx, voice_overrides=self._get_voice_overrides())

    def next_sentence(self):
        if not self.sentences:
            return
        new_idx = min(len(self.sentences) - 1, self.active_sentence_index + 1)
        self.active_sentence_index = new_idx
        self.render_text()
        self.update_progress_display()
        if self.audio.is_playing():
            self.audio.play(self.sentences, new_idx, voice_overrides=self._get_voice_overrides())

    def on_voice_changed(self):
        voice_id = self.combo_voice.currentData()
        self.audio.voice_type = voice_id
        if self.audio.is_playing():
            self.audio.play(self.sentences, self.active_sentence_index, voice_overrides=self._get_voice_overrides())

    def on_speed_changed(self, val: int):
        speed = val / 100.0
        self.lbl_speed_val.setText(f"{speed:.1f}x")
        self.audio.speed = speed

    def change_font_size(self, delta: int):
        self.font_size = max(12, min(36, self.font_size + delta))
        self.render_text()

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
        self.books.append(new_book)
        self.load_book_to_ui(len(self.books) - 1)
        ch_count = len(new_book.get("chapters", []))
        self.statusBar.showMessage(f"הספר '{new_book['title']}' נטען בהצלחה ({ch_count} פרקים/חלקים)!", 6000)

    def _on_file_load_failed(self, err: str):
        self.statusBar.showMessage("טעינת הקובץ נכשלה.", 4000)
        QMessageBox.critical(self, "שגיאה בטעינת קובץ", err)

    def paste_custom_text(self):
        text, ok = QInputDialog.getMultiLineText(self, "הדבקת טקסט", "הדבק כאן טקסט בעברית או באנגלית לקריאה/תרגום:")
        if ok and text.strip():
            new_book = {
                "id": f"custom_{len(self.books)}",
                "title": "טקסט מותאם אישית",
                "author": "הדבקה אישית",
                "description": "טקסט שהודבק במערכת",
                "chapters": [{"title": "פרק א׳", "text": text.strip()}]
            }
            self.books.append(new_book)
            self.load_book_to_ui(len(self.books) - 1)

    # --- Settings Dialog ---
    def open_settings_dialog(self):
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec():
            self.settings = load_settings()
            self._refresh_provider_badge()
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

    # --- AI Script Studio ---
    def open_script_studio(self):
        dlg = ScriptStudioDialog(self, self.settings)
        if not dlg.exec():
            return
        opts = dlg.get_options()
        book = self.books[self.current_book_index]
        source_text = "\n".join(self.sentences) if self.sentences else book["chapters"][self.current_chapter_index]["text"]
        cfg = self.settings

        self._run_background_task(
            "יוצר תסריט AI אינטראקטיבי ברקע (ייתכן שייקח מספר שניות)...",
            lambda: generate_narration_script(
                source_text,
                mode=opts["mode"],
                custom_instruction=opts["instruction"],
                book_title=book["title"],
                settings=cfg,
            ),
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

        self.lbl_comp_title.setText("🎬 תצוגת תסריט והערות")
        self.companion_widget.setVisible(True)
        self.comp_text.setPlainText(format_script_as_text(script_data))
        self.statusBar.showMessage(
            f"התסריט נוצר בהצלחה ({len(lines)} שורות)! לחץ ▶ להקראה רב-קולית.",
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

    # --- Translation Engine (AI or Free) ---
    def open_translate_dialog(self):
        dlg = TranslateDialog(self, self.settings)
        if not dlg.exec():
            return
        opts = dlg.get_options()
        book = self.books[self.current_book_index]
        source_text = "\n".join(self.sentences) if self.sentences else book["chapters"][self.current_chapter_index]["text"]
        cfg = self.settings

        self._run_background_task(
            "מתרגם את הפרק ברקע...",
            lambda: translate_text(
                source_text,
                engine=opts["engine"],
                source_lang=opts["source_lang"],
                target_lang=opts["target_lang"],
                settings=cfg,
            ),
            lambda res: self._on_translation_done(res, replace_reader=opts["replace_reader"]),
        )

    def _on_translation_done(self, res: dict, replace_reader: bool):
        translated = res.get("translated_text", "")
        engine_label = res.get("engine_label", "")
        if replace_reader:
            self.audio.stop()
            self.sentences = split_into_sentences(translated)
            self.speaker_roles = []
            self.speaker_labels = []
            self.current_script_data = None
            self.active_sentence_index = 0
            self.btn_restore_original.setVisible(True)
            self.btn_export_script.setVisible(False)
            self.render_text()
            self.update_progress_display()
            self.statusBar.showMessage(f"הפרק תורגם ({engine_label}) ומוכן להקראה קולית!", 6000)
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
            self.statusBar.showMessage(f"התרגום הושלם ({engine_label}).", 5000)

    # --- Portable Audiobook Export (MP3 / WAV) ---
    def export_audio_dialog(self):
        if not self.sentences:
            QMessageBox.information(self, "אין טקסט", "אין תוכן בפרק הנוכחי לייצוא שמע.")
            return

        if self._export_thread and self._export_thread.isRunning():
            QMessageBox.information(self, "ייצוא בתהליך", "ייצוא שמע קודם עדיין רץ ברקע, אנא המתן לסיומו.")
            return

        book = self.books[self.current_book_index]
        safe_title = "".join(c for c in book["title"] if c.isalnum() or c in (" ", "-", "_")).strip() or "audiobook"
        default_name = f"{safe_title}_part{self.current_chapter_index + 1}.mp3"

        file_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "ייצא קובץ שמע נייד לשיתוף (MP3 / WAV)",
            default_name,
            "קובץ שמע MP3 באיכות אולפן (*.mp3);;קובץ שמע WAV אופליין (*.wav)",
        )
        if not file_path:
            return

        if "WAV" in selected_filter and not file_path.lower().endswith(".wav"):
            file_path = f"{os.path.splitext(file_path)[0]}.wav"
        elif "MP3" in selected_filter and not file_path.lower().endswith(".mp3"):
            file_path = f"{os.path.splitext(file_path)[0]}.mp3"

        self.statusBar.showMessage("מתחיל ייצוא קובץ שמע נייד ברקע...")
        self._export_thread = AudioExportThread(
            sentences=self.sentences,
            output_path=file_path,
            default_voice=self.combo_voice.currentData() or "edge_avri",
            speed=self.audio.speed,
            voice_overrides=self._get_voice_overrides(),
        )
        self._export_thread.progress.connect(lambda msg: self.statusBar.showMessage(msg))
        self._export_thread.finished_path.connect(self._on_audio_exported)
        self._export_thread.failed.connect(self._on_audio_export_failed)
        self._export_thread.start()

    def _on_audio_exported(self, saved_path: str):
        self.statusBar.showMessage(f"קובץ השמע נשמר בהצלחה: {saved_path}", 7000)
        msg = QMessageBox(self)
        msg.setWindowTitle("🎧 קובץ השמע מוכן להעברה!")
        msg.setText(f"קובץ השמע נוצר בהצלחה ומוכן לשיתוף או העברה:\n\n{saved_path}")
        btn_open_folder = msg.addButton("📂 פתח תיקייה", QMessageBox.ButtonRole.ActionRole)
        msg.addButton("אישור", QMessageBox.ButtonRole.AcceptRole)
        msg.exec()
        if msg.clickedButton() == btn_open_folder:
            folder = os.path.dirname(os.path.abspath(saved_path))
            try:
                if os.name == "nt":
                    os.startfile(folder)
            except Exception:
                pass

    def _on_audio_export_failed(self, err: str):
        self.statusBar.showMessage("ייצוא השמע נכשל.", 5000)
        QMessageBox.critical(self, "שגיאה בייצוא קובץ שמע", err)

    def closeEvent(self, event):
        try:
            self.audio.stop()
            if self._file_thread and self._file_thread.isRunning():
                self._file_thread.wait(800)
            if self._task_thread and self._task_thread.isRunning():
                self._task_thread.wait(800)
            if self._export_thread and self._export_thread.isRunning():
                self._export_thread.wait(1200)
        except Exception:
            pass
        super().closeEvent(event)
