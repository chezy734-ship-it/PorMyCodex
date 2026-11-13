# -*- coding: utf-8 -*-
"""
חלונות דיאלוג עבור PorMyCodex:
1. SettingsDialog - הגדרת ספקי AI (Google/OpenAI/Claude/OpenRouter/Groq/מקומי), מפתחות API, גילוי מודלים, תרגום וקולות.
2. TranslateDialog - בחירת מנוע תרגום (AI או חינמי) ושפות יעד/מקור.
3. ScriptStudioDialog - יצירת תסריט AI (לימודי מורה-תלמיד / דרמטי) והנחיות מותאמות.
"""

from copy import deepcopy
from typing import Dict, Any, Optional
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QPushButton, QComboBox, QTabWidget,
    QWidget, QRadioButton, QTextEdit, QMessageBox
)

from settings_manager import load_settings, save_settings
from ai_providers import PROVIDER_CATALOG, discover_models, get_preset_models, get_provider_label
from translator_engine import TRANSLATION_ENGINES, LANGUAGE_OPTIONS


class ModelDiscoveryThread(QThread):
    finished_ok = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, provider_id: str, api_key: str, base_url: str):
        super().__init__()
        self.provider_id = provider_id
        self.api_key = api_key
        self.base_url = base_url

    def run(self):
        try:
            models = discover_models(self.provider_id, self.api_key, self.base_url)
            self.finished_ok.emit(models)
        except Exception as e:
            self.failed.emit(str(e))


class SettingsDialog(QDialog):
    def __init__(self, parent=None, current_settings: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ הגדרות AI, מודלים, תרגום וקולות")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(620, 540)
        self.settings = deepcopy(current_settings or load_settings())
        self._current_provider = self.settings.get("active_provider", "gemini")
        self._discovery_thread: Optional[ModelDiscoveryThread] = None

        self._build_ui()
        self._load_provider_fields(self._current_provider)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        # === TAB 1: AI Providers & Models ===
        ai_tab = QWidget()
        ai_layout = QVBoxLayout(ai_tab)
        ai_layout.setSpacing(10)

        grp_provider = QGroupBox("בחירת חברת AI ומודל")
        form_p = QFormLayout(grp_provider)
        form_p.setSpacing(10)

        self.combo_provider = QComboBox()
        for pid, meta in PROVIDER_CATALOG.items():
            self.combo_provider.addItem(meta["label"], pid)
        idx = self.combo_provider.findData(self._current_provider)
        if idx >= 0:
            self.combo_provider.setCurrentIndex(idx)
        self.combo_provider.currentIndexChanged.connect(self._on_provider_changed)
        form_p.addRow("חברה / ספק AI:", self.combo_provider)

        key_row = QHBoxLayout()
        self.txt_api_key = QLineEdit()
        self.txt_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_api_key.setPlaceholderText("הדבק כאן מפתח API של הספק שנבחר...")
        self.txt_api_key.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        key_row.addWidget(self.txt_api_key, 1)

        self.btn_toggle_key = QPushButton("👁 הצג")
        self.btn_toggle_key.setCheckable(True)
        self.btn_toggle_key.toggled.connect(
            lambda checked: self.txt_api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        key_row.addWidget(self.btn_toggle_key)
        form_p.addRow("מפתח API:", key_row)

        self.txt_base_url = QLineEdit()
        self.txt_base_url.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        form_p.addRow("כתובת שרת (Base URL):", self.txt_base_url)

        model_row = QHBoxLayout()
        self.combo_model = QComboBox()
        self.combo_model.setEditable(True)
        self.combo_model.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        model_row.addWidget(self.combo_model, 1)

        self.btn_discover = QPushButton("🔍 בדוק חיבור וגלה מודלים")
        self.btn_discover.setObjectName("accentBtn")
        self.btn_discover.clicked.connect(self._run_model_discovery)
        model_row.addWidget(self.btn_discover)
        form_p.addRow("מודל פעיל:", model_row)

        self.lbl_discovery_status = QLabel("ניתן לבחור מודל מהרשימה, להקליד שם מודל ידנית, או ללחוץ 'גלה מודלים' לשליפה חיה מהספק.")
        self.lbl_discovery_status.setWordWrap(True)
        self.lbl_discovery_status.setStyleSheet("color: #94a3b8; font-size: 12px;")
        form_p.addRow("", self.lbl_discovery_status)

        ai_layout.addWidget(grp_provider)

        grp_info = QGroupBox("הערת אבטחה ופרטיות")
        info_lay = QVBoxLayout(grp_info)
        lbl_sec = QLabel(
            "• כל מפתחות ה-API נשמרים מקומית אצלך במחשב בלבד (קובץ settings.json בתיקיית AppData).\n"
            "• ניתן להחליף בכל רגע בין Google Gemini, ChatGPT/OpenAI, Claude, OpenRouter, Groq או Ollama מקומי."
        )
        lbl_sec.setWordWrap(True)
        lbl_sec.setStyleSheet("color: #cbd5e1; font-size: 12px;")
        info_lay.addWidget(lbl_sec)
        ai_layout.addWidget(grp_info)
        ai_layout.addStretch()

        tabs.addTab(ai_tab, "🤖 ספקי AI ומודלים")

        # === TAB 2: Translation & Multi-Speaker ===
        trans_tab = QWidget()
        trans_layout = QVBoxLayout(trans_tab)
        trans_layout.setSpacing(10)

        grp_trans = QGroupBox("הגדרות תרגום ברירת מחדל")
        form_t = QFormLayout(grp_trans)
        form_t.setSpacing(10)

        self.combo_trans_engine = QComboBox()
        for eid, elabel in TRANSLATION_ENGINES:
            self.combo_trans_engine.addItem(elabel, eid)
        t_cfg = self.settings.get("translation", {})
        t_idx = self.combo_trans_engine.findData(t_cfg.get("engine", "google_free"))
        if t_idx >= 0:
            self.combo_trans_engine.setCurrentIndex(t_idx)
        form_t.addRow("מנוע תרגום:", self.combo_trans_engine)

        self.txt_libre_url = QLineEdit(t_cfg.get("libretranslate_url", "https://libretranslate.com/translate"))
        self.txt_libre_url.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        form_t.addRow("כתובת LibreTranslate:", self.txt_libre_url)

        self.txt_libre_key = QLineEdit(t_cfg.get("libretranslate_api_key", ""))
        self.txt_libre_key.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.txt_libre_key.setPlaceholderText("אופציונלי")
        form_t.addRow("מפתח LibreTranslate:", self.txt_libre_key)

        trans_layout.addWidget(grp_trans)

        grp_voices = QGroupBox("קולות להקראת תסריט רב-קולי (מורה ותלמיד)")
        form_v = QFormLayout(grp_voices)
        form_v.setSpacing(10)

        n_cfg = self.settings.get("narration", {})
        self.combo_teacher_voice = self._make_voice_combo(n_cfg.get("teacher_voice", "edge_avri"))
        self.combo_student_voice = self._make_voice_combo(n_cfg.get("student_voice", "edge_hila"))
        self.combo_narrator_voice = self._make_voice_combo(n_cfg.get("narrator_voice", "edge_avri"))

        form_v.addRow("🎓 קול המורה:", self.combo_teacher_voice)
        form_v.addRow("🙋 קול התלמיד:", self.combo_student_voice)
        form_v.addRow("🎙️ קול המספר:", self.combo_narrator_voice)

        trans_layout.addWidget(grp_voices)
        trans_layout.addStretch()

        tabs.addTab(trans_tab, "🌐 תרגום ודוברים")

        # Bottom Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        btn_cancel = QPushButton("ביטול")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        btn_save = QPushButton("💾 שמור הגדרות")
        btn_save.setObjectName("primaryBtn")
        btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(btn_save)

        layout.addLayout(btn_row)

    def _make_voice_combo(self, selected: str) -> QComboBox:
        cb = QComboBox()
        cb.addItem("אברי - עברית (Edge-TTS)", "edge_avri")
        cb.addItem("הילה - עברית (Edge-TTS)", "edge_hila")
        cb.addItem("Windows SAPI5 מקומי", "sapi5")
        idx = cb.findData(selected)
        if idx >= 0:
            cb.setCurrentIndex(idx)
        return cb

    def _save_current_provider_to_dict(self):
        pdict = self.settings["providers"].setdefault(self._current_provider, {})
        pdict["api_key"] = self.txt_api_key.text().strip()
        pdict["base_url"] = self.txt_base_url.text().strip()
        pdict["model"] = self.combo_model.currentText().strip()

    def _load_provider_fields(self, pid: str):
        pdict = self.settings["providers"].get(pid, {})
        meta = PROVIDER_CATALOG.get(pid, {})
        self.txt_api_key.setText(pdict.get("api_key", ""))
        self.txt_base_url.setText(pdict.get("base_url") or meta.get("default_base_url", ""))

        self.combo_model.blockSignals(True)
        self.combo_model.clear()
        discovered = pdict.get("discovered_models") or []
        presets = get_preset_models(pid)
        combined = []
        for m in list(discovered) + list(presets):
            if m and m not in combined:
                combined.append(m)
        for m in combined:
            self.combo_model.addItem(m)

        saved_model = pdict.get("model") or (combined[0] if combined else "")
        self.combo_model.setEditText(saved_model)
        self.combo_model.blockSignals(False)

        self.lbl_discovery_status.setStyleSheet("color: #94a3b8; font-size: 12px;")
        if discovered:
            self.lbl_discovery_status.setText(f"נטענו {len(discovered)} מודלים מאומתים עבור {meta.get('label', pid)}.")
        else:
            self.lbl_discovery_status.setText("ניתן לבחור מודל מהרשימה, להקליד שם מודל ידנית, או ללחוץ 'בדוק חיבור וגלה מודלים'.")

    def _on_provider_changed(self):
        self._save_current_provider_to_dict()
        new_pid = self.combo_provider.currentData()
        self._current_provider = new_pid
        self.settings["active_provider"] = new_pid
        self._load_provider_fields(new_pid)

    def _run_model_discovery(self):
        self._save_current_provider_to_dict()
        pid = self._current_provider
        key = self.txt_api_key.text().strip()
        base_url = self.txt_base_url.text().strip()

        self.btn_discover.setEnabled(False)
        self.lbl_discovery_status.setStyleSheet("color: #fbbf24; font-size: 12px;")
        self.lbl_discovery_status.setText(f"בודק חיבור מול {get_provider_label(pid)} ושולף מודלים...")

        self._discovery_thread = ModelDiscoveryThread(pid, key, base_url)
        self._discovery_thread.finished_ok.connect(self._on_discovery_ok)
        self._discovery_thread.failed.connect(self._on_discovery_failed)
        self._discovery_thread.start()

    def _on_discovery_ok(self, models: list):
        self.btn_discover.setEnabled(True)
        pid = self._current_provider
        self.settings["providers"][pid]["discovered_models"] = models
        current_text = self.combo_model.currentText().strip()
        self.combo_model.clear()
        for m in models:
            self.combo_model.addItem(m)
        if current_text and current_text in models:
            self.combo_model.setCurrentText(current_text)
        elif models:
            self.combo_model.setCurrentIndex(0)

        self.lbl_discovery_status.setStyleSheet("color: #34d399; font-size: 12px; font-weight: bold;")
        self.lbl_discovery_status.setText(f"✅ החיבור הצליח! התגלו {len(models)} מודלים זמינים.")

    def _on_discovery_failed(self, err: str):
        self.btn_discover.setEnabled(True)
        self.lbl_discovery_status.setStyleSheet("color: #f87171; font-size: 12px;")
        self.lbl_discovery_status.setText(f"❌ {err}")

    def _on_save(self):
        self._save_current_provider_to_dict()
        self.settings["active_provider"] = self.combo_provider.currentData()
        self.settings["translation"]["engine"] = self.combo_trans_engine.currentData()
        self.settings["translation"]["libretranslate_url"] = self.txt_libre_url.text().strip()
        self.settings["translation"]["libretranslate_api_key"] = self.txt_libre_key.text().strip()
        self.settings["narration"]["teacher_voice"] = self.combo_teacher_voice.currentData()
        self.settings["narration"]["student_voice"] = self.combo_student_voice.currentData()
        self.settings["narration"]["narrator_voice"] = self.combo_narrator_voice.currentData()

        save_settings(self.settings)
        self.accept()


class TranslateDialog(QDialog):
    def __init__(self, parent=None, settings: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("🌐 תרגום פרק (AI או מנוע חינמי)")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(480, 320)
        self.settings = settings or load_settings()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self.combo_engine = QComboBox()
        for eid, elabel in TRANSLATION_ENGINES:
            self.combo_engine.addItem(elabel, eid)
        default_engine = self.settings.get("translation", {}).get("engine", "google_free")
        idx = self.combo_engine.findData(default_engine)
        if idx >= 0:
            self.combo_engine.setCurrentIndex(idx)
        form.addRow("מנוע תרגום:", self.combo_engine)

        self.combo_src = QComboBox()
        self.combo_tgt = QComboBox()
        for code, label in LANGUAGE_OPTIONS:
            self.combo_src.addItem(label, code)
            if code != "auto":
                self.combo_tgt.addItem(label, code)
        self.combo_src.setCurrentIndex(self.combo_src.findData("auto"))
        self.combo_tgt.setCurrentIndex(self.combo_tgt.findData("he"))
        form.addRow("שפת מקור:", self.combo_src)
        form.addRow("שפת יעד:", self.combo_tgt)

        layout.addLayout(form)

        grp_mode = QGroupBox("אופן הצגת התרגום")
        mode_lay = QVBoxLayout(grp_mode)
        self.radio_replace = QRadioButton("הצג טקסט מתורגם לקריאה והקראה קולית (מומלץ להאזנה)")
        self.radio_replace.setChecked(True)
        self.radio_bilingual = QRadioButton("תצוגה משולבת (שורה מקור + שורה תרגום בחלונית הצד)")
        mode_lay.addWidget(self.radio_replace)
        mode_lay.addWidget(self.radio_bilingual)
        layout.addWidget(grp_mode)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("ביטול")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        btn_go = QPushButton("🌐 תרגם עכשיו")
        btn_go.setObjectName("primaryBtn")
        btn_go.clicked.connect(self.accept)
        btn_row.addWidget(btn_go)
        layout.addLayout(btn_row)

    def get_options(self) -> Dict[str, Any]:
        return {
            "engine": self.combo_engine.currentData(),
            "source_lang": self.combo_src.currentData(),
            "target_lang": self.combo_tgt.currentData(),
            "replace_reader": self.radio_replace.isChecked(),
        }


class ScriptStudioDialog(QDialog):
    def __init__(self, parent=None, settings: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("🎬 יצירת תסריט AI (קורא ספרים חכם)")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(520, 360)
        self.settings = settings or load_settings()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        pid = self.settings.get("active_provider", "gemini")
        model = self.settings.get("providers", {}).get(pid, {}).get("model", "")
        lbl_active = QLabel(f"ספק ומודל פעילים ליצירת התסריט: <b>{get_provider_label(pid)}</b> (<code>{model}</code>)")
        lbl_active.setStyleSheet("background-color: #1e293b; padding: 8px 12px; border-radius: 8px; color: #fbbf24;")
        layout.addWidget(lbl_active)

        grp_mode = QGroupBox("סגנון התסריט")
        mode_lay = QVBoxLayout(grp_mode)
        self.radio_edu = QRadioButton("🎓 תסריט לימודי — דיאלוג מורה ותלמיד (שאלות סקרנות, ביאור מדויק, הקראה בשני קולות)")
        self.radio_edu.setChecked(True)
        self.radio_dramatic = QRadioButton("🎭 תסריט דרמטי — קריינות ספרותית אקספרסיבית עם סגנון הגשה והדגשות")
        mode_lay.addWidget(self.radio_edu)
        mode_lay.addWidget(self.radio_dramatic)
        layout.addWidget(grp_mode)

        lbl_extra = QLabel("הנחיה נוספת (אופציונלי):")
        layout.addWidget(lbl_extra)
        self.txt_instruction = QTextEdit()
        self.txt_instruction.setMaximumHeight(90)
        self.txt_instruction.setPlaceholderText("לדוגמה: הרחב על המושגים ההלכתיים, או הוסף סיכום קצר בסוף הדיאלוג...")
        layout.addWidget(self.txt_instruction)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("ביטול")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        btn_gen = QPushButton("🎬 צור תסריט עכשיו")
        btn_gen.setObjectName("primaryBtn")
        btn_gen.clicked.connect(self.accept)
        btn_row.addWidget(btn_gen)
        layout.addLayout(btn_row)

    def get_options(self) -> Dict[str, str]:
        return {
            "mode": "educational" if self.radio_edu.isChecked() else "dramatic",
            "instruction": self.txt_instruction.toPlainText().strip(),
        }
