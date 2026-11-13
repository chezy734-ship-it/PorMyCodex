# -*- coding: utf-8 -*-
"""
חלון הממשק הראשי (MainWindow) של תוכנת PorMyCodex ב-PyQt6.
עיצוב שולחני מלא, כיווניות עברית (RTL), נגן קולי מתקדם ועוזר AI ספרותי.
"""

import os
from typing import List, Dict, Any
from PyQt6.QtCore import Qt, QTimer
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PorMyCodex - מקריא ספרים חכם בעברית מבוסס AI")
        self.resize(1180, 780)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

        # State
        self.books: List[Dict[str, Any]] = list(SAMPLE_BOOKS)
        self.current_book_index: int = 0
        self.current_chapter_index: int = 0
        self.sentences: List[str] = []
        self.active_sentence_index: int = 0
        self.is_vocalized: bool = False
        self.font_size: int = 18

        # Audio Engine
        self.audio = AudioEngine()
        self.audio.sentence_changed.connect(self.on_sentence_changed)
        self.audio.state_changed.connect(self.on_playback_state_changed)
        self.audio.error_occurred.connect(self.on_audio_error)

        self.init_ui()
        self.load_book_to_ui(0)

        # Keyboard shortcuts
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.toggle_play)
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, self.prev_sentence)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, self.next_sentence)

    def init_ui(self):
        # Central Splitter
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Top Toolbar
        toolbar = QToolBar("פעולות ספרייה ו-AI")
        self.addToolBar(toolbar)

        open_action = QAction("📂 פתח קובץ (PDF / Word / TXT)", self)
        open_action.triggered.connect(self.open_file_dialog)
        toolbar.addAction(open_action)

        paste_action = QAction("📋 הדבק טקסט חופשי", self)
        paste_action.triggered.connect(self.paste_custom_text)
        toolbar.addAction(paste_action)

        toolbar.addSeparator()

        vocalize_action = QAction("✨ נקד והרחב קיצורים עם AI", self)
        vocalize_action.triggered.connect(self.vocalize_current_chapter)
        toolbar.addAction(vocalize_action)

        explain_action = QAction("💡 באר פסקה זו", self)
        explain_action.triggered.connect(lambda: self.trigger_companion("explain"))
        toolbar.addAction(explain_action)

        summary_action = QAction("📝 סכם פרק", self)
        summary_action.triggered.connect(lambda: self.trigger_companion("summary"))
        toolbar.addAction(summary_action)

        quiz_action = QAction("❓ שאלות הבנה", self)
        quiz_action.triggered.connect(lambda: self.trigger_companion("quiz"))
        toolbar.addAction(quiz_action)

        # Splitter Layout (Left: Library & Chapters, Center: Text Reader, Right: AI Companion)
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

        lbl_chapters = QLabel("📑 פרקים בתוכן")
        lbl_chapters.setObjectName("sectionLabel")
        sidebar_layout.addWidget(lbl_chapters)

        self.chapter_list = QListWidget()
        self.chapter_list.currentRowChanged.connect(self.on_chapter_selected)
        sidebar_layout.addWidget(self.chapter_list, 3)

        splitter.addWidget(sidebar)

        # --- Center: Reading Display ---
        center_widget = QWidget()
        center_layout = QVBoxLayout(center_widget)
        center_layout.setContentsMargins(16, 16, 16, 16)
        center_layout.setSpacing(10)

        # Book Title Bar
        header_row = QHBoxLayout()
        self.lbl_current_title = QLabel("כותרת ספר")
        self.lbl_current_title.setObjectName("titleLabel")
        header_row.addWidget(self.lbl_current_title)

        header_row.addStretch()

        btn_font_plus = QPushButton("A+")
        btn_font_plus.setFixedWidth(40)
        btn_font_plus.clicked.connect(lambda: self.change_font_size(2))
        header_row.addWidget(btn_font_plus)

        btn_font_minus = QPushButton("A-")
        btn_font_minus.setFixedWidth(40)
        btn_font_minus.clicked.connect(lambda: self.change_font_size(-2))
        header_row.addWidget(btn_font_minus)

        center_layout.addLayout(header_row)

        # Reading Text Browser
        self.text_browser = QTextBrowser()
        self.text_browser.setOpenExternalLinks(False)
        self.text_browser.setFont(QFont("FrankRuehl", self.font_size))
        center_layout.addWidget(self.text_browser, 1)

        splitter.addWidget(center_widget)

        # --- Right: AI Companion Drawer ---
        self.companion_widget = QWidget()
        comp_layout = QVBoxLayout(self.companion_widget)
        comp_layout.setContentsMargins(12, 12, 12, 12)
        comp_layout.setSpacing(8)

        comp_header = QHBoxLayout()
        lbl_comp = QLabel("🤖 עוזר ספרותי AI")
        lbl_comp.setObjectName("titleLabel")
        comp_header.addWidget(lbl_comp)

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

        splitter.setSizes([260, 640, 280])

        # --- Bottom Player Bar ---
        player_bar = QFrame()
        player_bar.setStyleSheet("background-color: #1e293b; border-top: 1px solid #334155; padding: 6px;")
        player_layout = QHBoxLayout(player_bar)
        player_layout.setContentsMargins(16, 8, 16, 8)
        player_layout.setSpacing(14)

        # Playback Controls
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

        # Progress
        self.lbl_progress = QLabel("משפט 0 מתוך 0")
        self.lbl_progress.setFixedWidth(130)
        player_layout.addWidget(self.lbl_progress)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setValue(0)
        player_layout.addWidget(self.progress_bar, 1)

        # Voice Selector
        player_layout.addWidget(QLabel("קול:"))
        self.combo_voice = QComboBox()
        self.combo_voice.addItem("קול עברית - אברי (Edge-TTS)", "edge_avri")
        self.combo_voice.addItem("קול עברית - הילה (Edge-TTS)", "edge_hila")
        self.combo_voice.addItem("מנוע מקומי Windows (SAPI5)", "sapi5")
        self.combo_voice.currentIndexChanged.connect(self.on_voice_changed)
        player_layout.addWidget(self.combo_voice)

        # Speed Slider
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

        # Status Bar
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.statusBar.showMessage("מוכן לקריאה. לחץ רווח כדי להתחיל להקריא.")

    def load_book_to_ui(self, book_idx: int):
        self.book_list.clear()
        for b in self.books:
            item = QListWidgetItem(f"📖 {b['title']}")
            self.book_list.addItem(item)
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
        self.chapter_list.setCurrentRow(0)

    def on_chapter_selected(self, row: int):
        if row < 0:
            return
        self.current_chapter_index = row
        book = self.books[self.current_book_index]
        chapter = book["chapters"][row]

        self.lbl_current_title.setText(f"{book['title']} — {chapter['title']}")
        self.sentences = split_into_sentences(chapter["text"])
        self.active_sentence_index = 0
        self.render_text()
        self.update_progress_display()
        self.audio.stop()

    def render_text(self):
        """מציג את הטקסט ב-QTextBrowser עם הדגשה זוהרת של המשפט הפעיל."""
        html_lines = []
        for idx, sent in enumerate(self.sentences):
            if idx == self.active_sentence_index:
                # משפט מוקרא פעיל - רקע ענברי זוהר
                html_lines.append(
                    f'<p style="background-color: rgba(245, 158, 11, 0.28); color: #fef3c7; '
                    f'padding: 8px 14px; border-radius: 8px; border-right: 4px solid #f59e0b; '
                    f'margin: 6px 0; font-size: {self.font_size}px; line-height: 1.8;">'
                    f'{sent}</p>'
                )
            else:
                html_lines.append(
                    f'<p style="color: #cbd5e1; margin: 4px 0; font-size: {self.font_size}px; '
                    f'line-height: 1.8;">{sent}</p>'
                )

        full_html = (
            f'<div dir="rtl" style="font-family: \'FrankRuehl\', \'David\', serif;">'
            f'{"".join(html_lines)}'
            f'</div>'
        )
        self.text_browser.setHtml(full_html)

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

    def toggle_play(self):
        if self.audio.is_playing():
            self.audio.pause()
        else:
            if self.audio.worker and self.audio.worker._is_paused:
                self.audio.resume()
            else:
                self.audio.play(self.sentences, self.active_sentence_index)

    def on_playback_state_changed(self, playing: bool):
        self.btn_play.setText("⏸" if playing else "▶")
        self.statusBar.showMessage("מקריא כעת..." if playing else "מושהה")

    def prev_sentence(self):
        new_idx = max(0, self.active_sentence_index - 1)
        self.active_sentence_index = new_idx
        self.render_text()
        self.update_progress_display()
        if self.audio.is_playing():
            self.audio.play(self.sentences, new_idx)

    def next_sentence(self):
        new_idx = min(len(self.sentences) - 1, self.active_sentence_index + 1)
        self.active_sentence_index = new_idx
        self.render_text()
        self.update_progress_display()
        if self.audio.is_playing():
            self.audio.play(self.sentences, new_idx)

    def on_voice_changed(self):
        voice_id = self.combo_voice.currentData()
        self.audio.voice_type = voice_id
        if self.audio.is_playing():
            self.audio.play(self.sentences, self.active_sentence_index)

    def on_speed_changed(self, val: int):
        speed = val / 100.0
        self.lbl_speed_val.setText(f"{speed:.1f}x")
        self.audio.speed = speed

    def change_font_size(self, delta: int):
        self.font_size = max(12, min(36, self.font_size + delta))
        self.render_text()

    def on_audio_error(self, err_msg: str):
        self.statusBar.showMessage(f"שגיאת אודיו: {err_msg}", 5000)

    # --- File & Text Import ---
    def open_file_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "בחר קובץ ספר או טקסט",
            "",
            "קבצי טקסט ומסמכים (*.txt *.md *.pdf *.docx);;כל הקבצים (*.*)"
        )
        if file_path:
            try:
                new_book = load_file_content(file_path)
                self.books.append(new_book)
                self.load_book_to_ui(len(self.books) - 1)
                self.statusBar.showMessage(f"הספר '{new_book['title']}' נטען בהצלחה!", 4000)
            except Exception as e:
                QMessageBox.critical(self, "שגיאה בטעינת קובץ", str(e))

    def paste_custom_text(self):
        text, ok = QInputDialog.getMultiLineText(self, "הדבקת טקסט", "הדבק כאן טקסט בעברית לקריאה:")
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

    # --- AI Features ---
    def vocalize_current_chapter(self):
        self.statusBar.showMessage("מפעיל בלשן AI לניקוד ופתיחת קיצורים...", 10000)
        book = self.books[self.current_book_index]
        chapter = book["chapters"][self.current_chapter_index]
        res = vocalize_and_expand_hebrew(chapter["text"])
        self.sentences = res["sentences"]
        self.active_sentence_index = 0
        self.render_text()
        self.update_progress_display()
        self.statusBar.showMessage("הניקוד והרחבת הקיצורים הושלמו בהצלחה!", 4000)

    def trigger_companion(self, mode: str):
        self.companion_widget.setVisible(True)
        self.comp_text.setHtml("<i>מנתח את הטקסט באמצעות AI...</i>")
        book = self.books[self.current_book_index]
        chapter = book["chapters"][self.current_chapter_index]
        target_text = self.sentences[self.active_sentence_index] if mode == "explain" and self.sentences else chapter["text"]
        result = ask_companion(target_text, mode=mode, book_title=book["title"])
        self.comp_text.setMarkdown(result)
