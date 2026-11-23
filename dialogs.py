# -*- coding: utf-8 -*-
"""
חלונות דיאלוג עבור PorMyCodex:
1. SettingsDialog - הגדרת ספקי AI, הזנת מפתח מוסתרת, גילוי מודלים חי ברקע עם Debounce/Pagination/סינון ומניעת דריסת תוצאה מאוחרת.
2. TranslateDialog - בחירת מנוע תרגום (AI או חינמי) ושפות יעד/מקור.
3. ScriptStudioDialog - יצירת תסריט AI לפרק הנוכחי או לספר כולו.
4. WikiSearchDialog - חיפוש מושגים בוויקיפדיה והרחבה עם AI.
5. ExportPipelineDialog - יצוא נאכף (תרגום -> תסריט -> TTS -> יצוא) לפרק יחיד או לספר כולו.
"""

from copy import deepcopy
import threading
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ai_providers import (
    AICallError,
    PROVIDER_CATALOG,
    _sanitize_error_text,
    discover_models_detailed,
    get_provider_label,
)
from settings_manager import (
    API_PACING_PRESETS,
    compute_provider_fingerprint,
    get_api_pacing_config,
    load_settings,
    save_settings,
)
from translator_engine import LANGUAGE_OPTIONS, TRANSLATION_ENGINES


class ModelDiscoveryThread(QThread):
    finished_ok = pyqtSignal(int, str, list, dict)
    failed = pyqtSignal(int, str, str, str)

    def __init__(
        self,
        request_id: int,
        provider_id: str,
        api_key: str,
        base_url: str,
        discovery_fn: Optional[Callable[..., Any]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.request_id = int(request_id)
        self.provider_id = provider_id
        self.api_key = api_key
        self.base_url = base_url
        self.discovery_fn = discovery_fn
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            if self.discovery_fn is not None:
                try:
                    raw = self.discovery_fn(
                        self.provider_id,
                        self.api_key,
                        self.base_url,
                        capability="text",
                        cancel_event=self.cancel_event,
                    )
                except TypeError:
                    raw = self.discovery_fn(self.provider_id, self.api_key, self.base_url)

                if isinstance(raw, dict):
                    models = list(raw.get("models") or [])
                    meta = raw
                else:
                    models = list(raw or [])
                    meta = {"pages_fetched": 1, "filtered_out": 0}
            else:
                meta = discover_models_detailed(
                    self.provider_id,
                    self.api_key,
                    self.base_url,
                    capability="text",
                    cancel_event=self.cancel_event,
                )
                models = list(meta.get("models") or [])

            if self.cancel_event.is_set():
                return
            if not models:
                raise AICallError(
                    "הספק החזיר רשימת מודלים ריקה עבור מפתח זה.",
                    stage="גילוי מודלים",
                    category="empty",
                )
            self.finished_ok.emit(self.request_id, self.provider_id, models, meta)
        except Exception as e:
            if self.cancel_event.is_set():
                return
            cat = getattr(e, "category", "error")
            safe_msg = _sanitize_error_text(str(e), [self.api_key])
            self.failed.emit(self.request_id, self.provider_id, str(cat), safe_msg)


class SettingsDialog(QDialog):
    def __init__(
        self,
        parent=None,
        current_settings: Optional[Dict[str, Any]] = None,
        discovery_fn: Optional[Callable[..., Any]] = None,
        debounce_ms: int = 450,
    ):
        super().__init__(parent)
        self.setWindowTitle("⚙️ הגדרות AI, מודלים, תרגום וקולות")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(680, 590)
        self.settings = deepcopy(current_settings or load_settings())
        self._discovery_fn = discovery_fn
        self._current_provider = self.settings.get("active_provider", "gemini")
        self._discovery_thread: Optional[ModelDiscoveryThread] = None
        self._retired_threads: List[ModelDiscoveryThread] = []
        self._request_seq = 0
        self._active_request_id = 0
        self._active_request_fp = ""
        self._loading_fields = False

        # טיימר Debounce לגילוי מודלים ברקע אחרי הזנת/הדבקת API Key או כתובת שרת
        self._auto_discover_timer = QTimer(self)
        self._auto_discover_timer.setSingleShot(True)
        self._auto_discover_timer.setInterval(max(20, int(debounce_ms)))
        self._auto_discover_timer.timeout.connect(lambda: self._run_model_discovery(silent_if_empty=True))

        self._build_ui()
        self._load_provider_fields(self._current_provider, auto_fetch=True)

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
        self.txt_api_key.setPlaceholderText("הדבק כאן מפתח API — המודלים הזמינים ייטענו אוטומטית לרשימה...")
        self.txt_api_key.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.txt_api_key.textChanged.connect(self._on_api_key_edited)
        self.txt_api_key.editingFinished.connect(self._on_api_key_editing_finished)
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
        self.txt_base_url.textChanged.connect(self._on_base_url_edited)
        form_p.addRow("כתובת שרת (Base URL):", self.txt_base_url)

        model_row = QHBoxLayout()
        self.combo_model = QComboBox()
        self.combo_model.setEditable(False)
        self.combo_model.setMaxVisibleItems(20)
        self.combo_model.setMinimumWidth(280)
        self.combo_model.setVisible(True)
        self.combo_model.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.combo_model.setToolTip("רשימת המודלים הזמינים שנטענו משרת הספק")
        model_row.addWidget(self.combo_model, 1)

        self.btn_discover = QPushButton("🔄 רענן / נסה שוב")
        self.btn_discover.setObjectName("accentBtn")
        self.btn_discover.clicked.connect(lambda: self._run_model_discovery(silent_if_empty=False))
        model_row.addWidget(self.btn_discover)
        form_p.addRow("מודל פעיל:", model_row)

        self.chk_manual_model = QCheckBox("הזן שם מודל ידנית (אופציונלי רק לשרת מקומי/מותאם)")
        self.chk_manual_model.setVisible(False)
        self.chk_manual_model.toggled.connect(self._on_manual_override_toggled)
        form_p.addRow("", self.chk_manual_model)

        self.lbl_discovery_status = QLabel("הזן מפתח API כדי לשלוף אוטומטית את רשימת המודלים הזמינים מהחברה.")
        self.lbl_discovery_status.setWordWrap(True)
        self.lbl_discovery_status.setStyleSheet("color: #94a3b8; font-size: 12px;")
        form_p.addRow("", self.lbl_discovery_status)

        ai_layout.addWidget(grp_provider)

        grp_pacing = QGroupBox("⏱️ קצב בקשות ל-API (מניעת עומס וחסימות HTTP 429)")
        form_pace = QFormLayout(grp_pacing)
        form_pace.setSpacing(8)

        pace_cfg = get_api_pacing_config(self.settings)
        self._syncing_pacing = False

        self.combo_pacing_preset = QComboBox()
        for pk, pmeta in API_PACING_PRESETS.items():
            self.combo_pacing_preset.addItem(pmeta["label"], pk)
        p_idx = self.combo_pacing_preset.findData(pace_cfg["preset"])
        if p_idx >= 0:
            self.combo_pacing_preset.setCurrentIndex(p_idx)
        self.combo_pacing_preset.currentIndexChanged.connect(self._on_pacing_preset_changed)
        form_pace.addRow("פרופיל קצב:", self.combo_pacing_preset)

        self.spin_min_interval = QDoubleSpinBox()
        self.spin_min_interval.setRange(0.0, 60.0)
        self.spin_min_interval.setSingleStep(0.5)
        self.spin_min_interval.setDecimals(1)
        self.spin_min_interval.setSuffix(" שניות")
        self.spin_min_interval.setValue(float(pace_cfg["min_interval_sec"]))
        self.spin_min_interval.valueChanged.connect(self._on_min_interval_changed)
        form_pace.addRow("השהיה מינימלית בין בקשות:", self.spin_min_interval)

        self.chk_adaptive_slowdown = QCheckBox(
            "האטה אוטומטית בעת שגיאת עומס/מכסה (HTTP 429) והמתנה לפי הנחיית השרת"
        )
        self.chk_adaptive_slowdown.setChecked(bool(pace_cfg["adaptive_slowdown"]))
        form_pace.addRow("הגנת עומס:", self.chk_adaptive_slowdown)

        ai_layout.addWidget(grp_pacing)

        grp_info = QGroupBox("אבטחה, בחירת מודל והמלצת קצב")
        info_lay = QVBoxLayout(grp_info)
        lbl_sec = QLabel(
            "• הזנת המפתח מוסתרת ונשמרת מקומית בלבד (לעולם אינה נכללת בלוגים או בהודעות שגיאה).\n"
            "• לתרגום ותסריטים ארוכים מומלץ מודל Flash (למשל gemini-2.5-flash / gemini-4-hydrogen) בקצב 'רגוע' (4 שנ').\n"
            "• למודלי Pro או מפתח Free Tier (לתשומת לב: מנוי Google AI Pro נפרד מחיוב Gemini API) מומלץ קצב 'איטי ובטוח' (6.5 שנ')."
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
        self.txt_libre_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_libre_key.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.txt_libre_key.setPlaceholderText("אופציונלי")
        form_t.addRow("מפתח LibreTranslate:", self.txt_libre_key)

        self.combo_fallback_secondary = QComboBox()
        self.combo_fallback_secondary.addItem("ללא שירות גיבוי משני", "")
        self.combo_fallback_secondary.addItem("LibreTranslate (מוגדר למעלה)", "libretranslate")
        self.combo_fallback_secondary.addItem("ספק ה-AI שנבחר (אם אושר למטה)", "ai")
        fb_sec = str(t_cfg.get("fallback_secondary_engine", "") or "")
        fb_idx = self.combo_fallback_secondary.findData(fb_sec)
        if fb_idx >= 0:
            self.combo_fallback_secondary.setCurrentIndex(fb_idx)
        form_t.addRow("שירות תרגום משני:", self.combo_fallback_secondary)

        self.chk_fallback_ai = QCheckBox(
            "אם התרגום החינמי נכשל/נחסם, נסה את ספק ה-AI שנבחר (עשוי לצרוך מכסה/תשלום)"
        )
        self.chk_fallback_ai.setChecked(bool(t_cfg.get("fallback_to_ai_on_failure", False)))
        form_t.addRow("מדיניות התאוששות:", self.chk_fallback_ai)

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

    def _on_manual_override_toggled(self, checked: bool):
        self.combo_model.setEditable(bool(checked))
        if checked:
            self.combo_model.setEnabled(True)

    def _cancel_inflight_discovery(self):
        self._active_request_id += 1
        self._request_seq = max(self._request_seq, self._active_request_id)
        if self._discovery_thread is not None:
            th = self._discovery_thread
            self._discovery_thread = None
            th.cancel()
            if th.isRunning():
                self._retired_threads.append(th)
                th.finished.connect(lambda t=th: self._cleanup_retired_thread(t))

    def _cleanup_retired_thread(self, th: ModelDiscoveryThread):
        if th in self._retired_threads:
            self._retired_threads.remove(th)

    def _save_current_provider_to_dict(self):
        pdict = self.settings["providers"].setdefault(self._current_provider, {})
        pdict["api_key"] = self.txt_api_key.text().strip()
        pdict["base_url"] = self.txt_base_url.text().strip()
        pdict["model"] = self.combo_model.currentText().strip()

    def _set_combo_models(self, models: List[str], preferred_model: str = ""):
        self.combo_model.blockSignals(True)
        try:
            self.combo_model.clear()
            for m in models:
                self.combo_model.addItem(m, m)

            if models:
                self.combo_model.setEnabled(True)
                self.combo_model.setVisible(True)
                if preferred_model and preferred_model in models:
                    self.combo_model.setCurrentText(preferred_model)
                else:
                    self.combo_model.setCurrentIndex(0)
            else:
                self.combo_model.setEnabled(self.chk_manual_model.isChecked())
        finally:
            self.combo_model.blockSignals(False)

    def _on_api_key_edited(self, text: str):
        if self._loading_fields:
            return
        self._auto_discover_timer.stop()
        self._cancel_inflight_discovery()

        pid = self._current_provider
        meta = PROVIDER_CATALOG.get(pid, {})
        clean = (text or "").strip()

        # נקה רשימה ישנה כדי שתוצאה של מפתח קודם לא תישאר מוצגת
        self._set_combo_models([], preferred_model="")
        pdict = self.settings["providers"].setdefault(pid, {})
        pdict["discovered_models"] = []
        pdict["discovered_fingerprint"] = ""

        if meta.get("requires_key", True) and not clean:
            self.lbl_discovery_status.setStyleSheet("color: #94a3b8; font-size: 12px;")
            self.lbl_discovery_status.setText("הזן מפתח API כדי לשלוף אוטומטית את רשימת המודלים הזמינים מהחברה.")
            return

        if len(clean) >= 6 or not meta.get("requires_key", True):
            self.lbl_discovery_status.setStyleSheet("color: #fbbf24; font-size: 12px;")
            self.lbl_discovery_status.setText("⏳ מזהה מפתח — טוען מודלים זמינים מהשרת...")
            self._auto_discover_timer.start()
        else:
            self.lbl_discovery_status.setStyleSheet("color: #94a3b8; font-size: 12px;")
            self.lbl_discovery_status.setText("המשך להזין או הדבק מפתח API מלא לשאיבת המודלים.")

    def _on_api_key_editing_finished(self):
        if self._loading_fields:
            return
        if self._auto_discover_timer.isActive():
            self._auto_discover_timer.stop()
            self._run_model_discovery(silent_if_empty=True)

    def _on_base_url_edited(self, _text: str):
        if self._loading_fields:
            return
        self._auto_discover_timer.stop()
        self._cancel_inflight_discovery()
        pid = self._current_provider
        meta = PROVIDER_CATALOG.get(pid, {})
        key = self.txt_api_key.text().strip()
        if key or not meta.get("requires_key", True):
            self.lbl_discovery_status.setStyleSheet("color: #fbbf24; font-size: 12px;")
            self.lbl_discovery_status.setText("⏳ כתובת השרת עודכנה — בודק מודלים זמינים...")
            self._auto_discover_timer.start()

    def _load_provider_fields(self, pid: str, auto_fetch: bool = False):
        self._loading_fields = True
        try:
            pdict = self.settings["providers"].setdefault(pid, {})
            meta = PROVIDER_CATALOG.get(pid, {})
            key = (pdict.get("api_key") or "").strip()
            base_url = (pdict.get("base_url") or meta.get("default_base_url", "")).strip()

            self.txt_api_key.setText(key)
            self.txt_base_url.setText(base_url)

            is_custom = pid == "custom"
            self.chk_manual_model.setVisible(is_custom)
            if not is_custom:
                self.chk_manual_model.setChecked(False)
                self.combo_model.setEditable(False)

            cur_fp = compute_provider_fingerprint(pid, key, base_url)
            saved_fp = pdict.get("discovered_fingerprint") or ""
            discovered = list(pdict.get("discovered_models") or [])
            saved_model = (pdict.get("model") or "").strip()

            # הצג מודלים רק אם התגלו עבור אותו מפתח/כתובת (ללא רשימות מומצאות!)
            valid_discovered = discovered if (discovered and (saved_fp == cur_fp or not saved_fp)) else []
            self._set_combo_models(valid_discovered, preferred_model=saved_model)

            if meta.get("requires_key", True) and not key:
                self.lbl_discovery_status.setStyleSheet("color: #94a3b8; font-size: 12px;")
                self.lbl_discovery_status.setText("הזן מפתח API כדי לשלוף אוטומטית את רשימת המודלים הזמינים מהחברה.")
            elif valid_discovered:
                self.lbl_discovery_status.setStyleSheet("color: #34d399; font-size: 12px;")
                self.lbl_discovery_status.setText(f"✅ נטענו {len(valid_discovered)} מודלים עבור {meta.get('label', pid)}.")
            else:
                self.lbl_discovery_status.setStyleSheet("color: #fbbf24; font-size: 12px;")
                self.lbl_discovery_status.setText(f"⏳ מתחבר אל {meta.get('label', pid)} לשליפת המודלים...")
        finally:
            self._loading_fields = False

        if auto_fetch and (key or not meta.get("requires_key", True)):
            self._run_model_discovery(silent_if_empty=True)

    def _on_provider_changed(self):
        self._auto_discover_timer.stop()
        self._cancel_inflight_discovery()
        self._save_current_provider_to_dict()
        new_pid = self.combo_provider.currentData()
        self._current_provider = new_pid
        self.settings["active_provider"] = new_pid
        self._load_provider_fields(new_pid, auto_fetch=True)

    def _run_model_discovery(self, silent_if_empty: bool = False):
        self._auto_discover_timer.stop()
        self._save_current_provider_to_dict()
        pid = self._current_provider
        key = self.txt_api_key.text().strip()
        base_url = self.txt_base_url.text().strip()
        meta = PROVIDER_CATALOG.get(pid, {})

        if meta.get("requires_key", True) and not key:
            self._set_combo_models([], preferred_model="")
            if not silent_if_empty:
                self.lbl_discovery_status.setStyleSheet("color: #f87171; font-size: 12px;")
                self.lbl_discovery_status.setText("❌ הזן מפתח API לפני בדיקת החיבור ושאיבת המודלים.")
            return

        # מבטל כל בקשה ישנה קודמת כדי שלא תוכל לחסום או לדרוס את הבקשה החדשה!
        self._cancel_inflight_discovery()
        self._request_seq += 1
        req_id = self._request_seq
        self._active_request_id = req_id
        self._active_request_fp = compute_provider_fingerprint(pid, key, base_url)

        self.btn_discover.setEnabled(False)
        self.btn_discover.setText("⏳ טוען...")
        self.lbl_discovery_status.setStyleSheet("color: #fbbf24; font-size: 12px;")
        self.lbl_discovery_status.setText(f"⏳ שואב מודלים זמינים מול {get_provider_label(pid)}...")

        th = ModelDiscoveryThread(
            req_id,
            pid,
            key,
            base_url,
            discovery_fn=self._discovery_fn,
            parent=self,
        )
        self._discovery_thread = th
        th.finished_ok.connect(self._on_discovery_ok)
        th.failed.connect(self._on_discovery_failed)
        th.start()

    def _on_discovery_ok(self, request_id: int, pid: str, models: list, meta_info: dict):
        if request_id != self._active_request_id or pid != self._current_provider:
            return

        self.btn_discover.setEnabled(True)
        self.btn_discover.setText("🔄 רענן / נסה שוב")

        key_now = self.txt_api_key.text().strip()
        url_now = self.txt_base_url.text().strip()
        fp_now = compute_provider_fingerprint(pid, key_now, url_now)
        if fp_now != self._active_request_fp:
            return

        pdict = self.settings["providers"].setdefault(pid, {})
        pdict["discovered_models"] = list(models)
        pdict["discovered_fingerprint"] = fp_now

        current_text = self.combo_model.currentText().strip() or (pdict.get("model") or "").strip()
        preferred = current_text if current_text in models else (models[0] if models else "")
        self._set_combo_models(models, preferred_model=preferred)

        pdict["model"] = self.combo_model.currentText().strip()
        pages_n = int((meta_info or {}).get("pages_fetched", 1) or 1)
        pages_note = f" ({pages_n} עמודים)" if pages_n > 1 else ""
        self.lbl_discovery_status.setStyleSheet("color: #34d399; font-size: 12px; font-weight: bold;")
        self.lbl_discovery_status.setText(
            f"✅ נשאבו {len(models)} מודלים זמינים{pages_note}! בחר מהרשימה למעלה ולחץ 'שמור הגדרות'."
        )

    def _on_discovery_failed(self, request_id: int, pid: str, category: str, err: str):
        if request_id != self._active_request_id or pid != self._current_provider:
            return

        self.btn_discover.setEnabled(True)
        self.btn_discover.setText("🔄 רענן / נסה שוב")
        self._set_combo_models([], preferred_model="")

        cat_titles = {
            "auth": "🔑 שגיאת מפתח/הרשאה (בדוק את מפתח ה-API):",
            "quota": "⏳ חריגה ממכסת בקשות (429):",
            "timeout": "⏱️ פג זמן ההתחברות/קריאה (זוהי תקלת רשת/עומס, לא בהכרח מפתח שגוי):",
            "network": "🌐 שגיאת תקשורת לשרת הספק:",
            "empty": "📭 הספק לא החזיר מודלים תואמי טקסט:",
        }
        prefix = cat_titles.get(category, "❌ שגיאה בשאיבת מודלים:")
        self.lbl_discovery_status.setStyleSheet("color: #f87171; font-size: 12px;")
        self.lbl_discovery_status.setText(f"{prefix}\n{err}")

    def _cleanup_threads(self):
        self._auto_discover_timer.stop()
        self._cancel_inflight_discovery()
        for th in list(self._retired_threads):
            th.cancel()
            if th.isRunning():
                th.wait(1200)
        self._retired_threads.clear()

    def done(self, r: int):
        self._cleanup_threads()
        super().done(r)

    def closeEvent(self, event):
        self._cleanup_threads()
        super().closeEvent(event)

    def _on_pacing_preset_changed(self, _index: int):
        if self._syncing_pacing:
            return
        preset = self.combo_pacing_preset.currentData() or "gentle"
        if preset != "custom" and preset in API_PACING_PRESETS:
            self._syncing_pacing = True
            try:
                self.spin_min_interval.setValue(float(API_PACING_PRESETS[preset]["min_interval_sec"]))
            finally:
                self._syncing_pacing = False

    def _on_min_interval_changed(self, val: float):
        if self._syncing_pacing:
            return
        cur_preset = self.combo_pacing_preset.currentData() or "gentle"
        expected = API_PACING_PRESETS.get(cur_preset, {}).get("min_interval_sec")
        if cur_preset != "custom" and expected is not None and abs(float(val) - float(expected)) > 0.05:
            self._syncing_pacing = True
            try:
                c_idx = self.combo_pacing_preset.findData("custom")
                if c_idx >= 0:
                    self.combo_pacing_preset.setCurrentIndex(c_idx)
            finally:
                self._syncing_pacing = False

    def _on_save(self):
        self._save_current_provider_to_dict()
        self.settings["active_provider"] = self.combo_provider.currentData()
        preset = self.combo_pacing_preset.currentData() or "gentle"
        max_429 = int(API_PACING_PRESETS.get(preset, API_PACING_PRESETS["gentle"]).get("max_retries_429", 4))
        self.settings["api_pacing"] = {
            "preset": preset,
            "min_interval_sec": round(float(self.spin_min_interval.value()), 2),
            "adaptive_slowdown": self.chk_adaptive_slowdown.isChecked(),
            "max_retries_429": max_429,
        }
        self.settings["translation"]["engine"] = self.combo_trans_engine.currentData()
        self.settings["translation"]["libretranslate_url"] = self.txt_libre_url.text().strip()
        self.settings["translation"]["libretranslate_api_key"] = self.txt_libre_key.text().strip()
        self.settings["translation"]["fallback_secondary_engine"] = self.combo_fallback_secondary.currentData() or ""
        self.settings["translation"]["fallback_to_ai_on_failure"] = self.chk_fallback_ai.isChecked()
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
        self.resize(520, 360)
        self.settings = settings or load_settings()
        t_cfg = self.settings.get("translation", {})

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self.combo_engine = QComboBox()
        for eid, elabel in TRANSLATION_ENGINES:
            self.combo_engine.addItem(elabel, eid)
        default_engine = t_cfg.get("engine", "google_free")
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
        self.combo_src.setCurrentIndex(max(0, self.combo_src.findData(t_cfg.get("source_lang", "auto"))))
        self.combo_tgt.setCurrentIndex(max(0, self.combo_tgt.findData(t_cfg.get("target_lang", "he"))))
        form.addRow("שפת מקור:", self.combo_src)
        form.addRow("שפת יעד:", self.combo_tgt)

        self.chk_fallback_ai = QCheckBox(
            "אם התרגום החינמי נכשל/נחסם, נסה את ספק ה-AI שנבחר (עשוי לצרוך מכסה/תשלום)"
        )
        self.chk_fallback_ai.setChecked(bool(t_cfg.get("fallback_to_ai_on_failure", False)))
        form.addRow("גיבוי התאוששות:", self.chk_fallback_ai)

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
            "fallback_to_ai_on_failure": self.chk_fallback_ai.isChecked(),
        }


class ScriptStudioDialog(QDialog):
    def __init__(
        self,
        parent=None,
        settings: Optional[Dict[str, Any]] = None,
        default_scope: str = "chapter",
        book_title: str = "",
        chapter_count: int = 1,
        current_chapter_title: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle("🎬 יצירת תסריט AI (פרק נוכחי / הספר כולו)")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(580, 560)
        self.settings = settings or load_settings()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        pid = self.settings.get("active_provider", "gemini")
        model = self.settings.get("providers", {}).get(pid, {}).get("model", "") or "לא נבחר"
        lbl_active = QLabel(f"ספק ומודל פעילים ליצירת התסריט: <b>{get_provider_label(pid)}</b> (<code>{model}</code>)")
        lbl_active.setStyleSheet("background-color: #1e293b; padding: 8px 12px; border-radius: 8px; color: #fbbf24;")
        layout.addWidget(lbl_active)

        grp_scope = QGroupBox("היקף היצירה (Scope)")
        scope_lay = QVBoxLayout(grp_scope)
        self.radio_scope_chapter = QRadioButton(
            f"📖 הפרק הנוכחי בלבד ({current_chapter_title or 'פרק פעיל'})"
        )
        self.radio_scope_book = QRadioButton(
            f"📚 הספר כולו ({book_title or 'הספר הנבחר'} — כל {chapter_count} הפרקים לפי הסדר)"
        )
        if default_scope == "book":
            self.radio_scope_book.setChecked(True)
        else:
            self.radio_scope_chapter.setChecked(True)
        scope_lay.addWidget(self.radio_scope_chapter)
        scope_lay.addWidget(self.radio_scope_book)

        t_cfg = self.settings.get("translation", {})
        self.chk_fallback_ai = QCheckBox(
            "אם התרגום החינמי נכשל/נחסם, נסה את ספק ה-AI שנבחר (עשוי לצרוך מכסה/תשלום)"
        )
        self.chk_fallback_ai.setChecked(bool(t_cfg.get("fallback_to_ai_on_failure", False)))
        scope_lay.addWidget(self.chk_fallback_ai)

        # בחירת טווח מקטעים לעיבוד מבוקר בספרים גדולים
        self.chk_custom_range = QCheckBox("עבד טווח מקטעים מסוים בלבד (מומלץ בספרים ענקיים)")
        self.chk_custom_range.setEnabled(default_scope == "book" and chapter_count > 1)
        scope_lay.addWidget(self.chk_custom_range)

        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("ממקטע:"))
        self.spin_from = QSpinBox()
        self.spin_from.setRange(1, max(1, chapter_count))
        self.spin_from.setValue(1)
        range_row.addWidget(self.spin_from)
        range_row.addWidget(QLabel("עד מקטע:"))
        self.spin_to = QSpinBox()
        self.spin_to.setRange(1, max(1, chapter_count))
        self.spin_to.setValue(max(1, chapter_count))
        range_row.addWidget(self.spin_to)
        range_row.addStretch()
        scope_lay.addLayout(range_row)

        def _sync_range_enabled():
            is_bk = self.radio_scope_book.isChecked() and chapter_count > 1
            self.chk_custom_range.setEnabled(is_bk)
            use_r = is_bk and self.chk_custom_range.isChecked()
            self.spin_from.setEnabled(use_r)
            self.spin_to.setEnabled(use_r)

        self.radio_scope_book.toggled.connect(_sync_range_enabled)
        self.chk_custom_range.toggled.connect(_sync_range_enabled)
        _sync_range_enabled()

        self.chk_skip_same_lang_trans = QCheckBox(
            "הטקסט כבר בעברית — דלג על תרגום חיצוני ועבור ישירות לתסריט/הקראה"
        )
        scope_lay.addWidget(self.chk_skip_same_lang_trans)

        layout.addWidget(grp_scope)

        grp_mode = QGroupBox("סגנון התסריט")
        mode_lay = QVBoxLayout(grp_mode)
        self.radio_edu = QRadioButton("🎓 תסריט לימודי — דיאלוג מורה ותלמיד (שאלות סקרנות, ביאור מדויק, הקראה בשני קולות)")
        self.radio_edu.setChecked(True)
        self.radio_dramatic = QRadioButton("🎭 תסריט דרמטי — קריינות ספרותית אקספרסיבית עם סגנון הגשה והדגשות")
        mode_lay.addWidget(self.radio_edu)
        mode_lay.addWidget(self.radio_dramatic)
        layout.addWidget(grp_mode)

        grp_wiki = QGroupBox("📚 הרחבת ידע מוויקיפדיה (Knowledge Grounding)")
        wiki_lay = QVBoxLayout(grp_wiki)
        self.chk_wikipedia = QCheckBox("🌐 חפש מושגים בוויקיפדיה והעשר את התסריט של המודל במידע אנציקלופדי")
        wiki_lay.addWidget(self.chk_wikipedia)

        self.txt_wiki_terms = QLineEdit()
        self.txt_wiki_terms.setPlaceholderText("מושגים ספציפיים לחיפוש מופרדים בפסיק (השאר ריק לזיהוי אוטומטי)")
        self.txt_wiki_terms.setEnabled(False)
        self.chk_wikipedia.toggled.connect(self.txt_wiki_terms.setEnabled)
        wiki_lay.addWidget(self.txt_wiki_terms)
        layout.addWidget(grp_wiki)

        lbl_extra = QLabel("הנחיה נוספת (אופציונלי):")
        layout.addWidget(lbl_extra)
        self.txt_instruction = QTextEdit()
        self.txt_instruction.setMaximumHeight(75)
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

    def get_options(self) -> Dict[str, Any]:
        scope = "book" if self.radio_scope_book.isChecked() else "chapter"
        target_chapters = None
        if scope == "book" and self.chk_custom_range.isChecked():
            a = min(self.spin_from.value(), self.spin_to.value())
            b = max(self.spin_from.value(), self.spin_to.value())
            target_chapters = list(range(a - 1, b))
        return {
            "scope": scope,
            "target_chapters": target_chapters,
            "bypass_same_language": self.chk_skip_same_lang_trans.isChecked(),
            "mode": "educational" if self.radio_edu.isChecked() else "dramatic",
            "instruction": self.txt_instruction.toPlainText().strip(),
            "use_wikipedia": self.chk_wikipedia.isChecked(),
            "wiki_terms": self.txt_wiki_terms.text().strip(),
            "fallback_to_ai_on_failure": self.chk_fallback_ai.isChecked(),
        }


class WikiSearchDialog(QDialog):
    def __init__(self, parent=None, settings: Optional[Dict[str, Any]] = None, initial_hint: str = ""):
        super().__init__(parent)
        self.setWindowTitle("📚 חיפוש בוויקיפדיה והרחבת ידע עם AI")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(540, 360)
        self.settings = settings or load_settings()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        pid = self.settings.get("active_provider", "gemini")
        model = self.settings.get("providers", {}).get(pid, {}).get("model", "") or "לא נבחר"
        lbl_active = QLabel(
            f"מחקר מושגים מבוסס ויקיפדיה + <b>{get_provider_label(pid)}</b> (<code>{model}</code>)"
        )
        lbl_active.setStyleSheet("background-color: #1e293b; padding: 8px 12px; border-radius: 8px; color: #38bdf8;")
        layout.addWidget(lbl_active)

        form = QFormLayout()
        form.setSpacing(10)

        self.txt_terms = QLineEdit(initial_hint)
        self.txt_terms.setPlaceholderText("הקלד מושגים (למשל: רמב\"ם, פילוסופיה יוונית) — או השאר ריק לזיהוי אוטומטי מהפרק")
        form.addRow("מושגים לחיפוש:", self.txt_terms)

        self.combo_lang = QComboBox()
        self.combo_lang.addItem("אוטומטי (עברית + אנגלית לפי המושג)", "auto")
        self.combo_lang.addItem("עברית בלבד (he.wikipedia.org)", "he")
        self.combo_lang.addItem("אנגלית בלבד (en.wikipedia.org)", "en")
        form.addRow("שפת ויקיפדיה:", self.combo_lang)

        layout.addLayout(form)

        grp_mode = QGroupBox("אופן הצגת המידע")
        mode_lay = QVBoxLayout(grp_mode)
        self.radio_ai_enrich = QRadioButton("🤖 חפש בוויקיפדיה + הרחב ובאר עם מודל ה-AI הפעיל (מומלץ)")
        self.radio_ai_enrich.setChecked(True)
        self.radio_wiki_only = QRadioButton("📖 הצג תקצירים וקישורים ישירים מוויקיפדיה בלבד (ללא קריאת AI)")
        mode_lay.addWidget(self.radio_ai_enrich)
        mode_lay.addWidget(self.radio_wiki_only)
        layout.addWidget(grp_mode)

        lbl_tip = QLabel("טיפ: תוכל להשאיר את שדה המושגים ריק והתוכנה תחלץ אוטומטית את 3 המושגים המרכזיים מהמשפט/פרק הנוכחי.")
        lbl_tip.setWordWrap(True)
        lbl_tip.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(lbl_tip)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("ביטול")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        btn_search = QPushButton("🔍 חפש והרחב ידע")
        btn_search.setObjectName("primaryBtn")
        btn_search.clicked.connect(self.accept)
        btn_row.addWidget(btn_search)
        layout.addLayout(btn_row)

    def get_options(self) -> Dict[str, Any]:
        return {
            "terms": self.txt_terms.text().strip(),
            "lang": self.combo_lang.currentData(),
            "use_ai": self.radio_ai_enrich.isChecked(),
        }


class ExportPipelineDialog(QDialog):
    """
    דיאלוג יצוא נאכף (תרגום -> תסריט -> TTS -> יצוא) התומך במפורש בהיקף:
      - 'chapter': הפרק הנוכחי בלבד
      - 'book': הספר כולו (כל פרקי הספר לפי הסדר, עם אריזת ZIP/תיקייה/WAV/MP3 מאוחד)
    """

    def __init__(
        self,
        parent=None,
        book_id: str = "",
        chapter_index: int = 0,
        source_text: str = "",
        settings: Optional[Dict[str, Any]] = None,
        store: Any = None,
        default_voice: str = "edge_avri",
        default_speed: float = 1.0,
        book: Optional[Dict[str, Any]] = None,
        default_scope: str = "chapter",
    ):
        super().__init__(parent)
        self.setWindowTitle("🎧 יצוא ספר קולי (תרגום ← תסריט ← TTS ← יצוא)")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(650, 640)
        self.book_id = book_id
        self.chapter_index = chapter_index
        self.source_text = source_text
        self.book = book or {
            "id": book_id,
            "title": "ספר",
            "chapters": [{"title": f"פרק {chapter_index + 1}", "text": source_text}],
        }
        self.settings = settings or load_settings()
        self.store = store
        self.default_speed = float(default_speed)
        self._default_voice = default_voice

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        lbl_info = QLabel(
            "שירות היצוא אוכף את מלוא הצינור בסדר קשיח ללא עקיפה:\n"
            "<b>1. תרגום כל המשפטים הדרושים ← 2. תסריט AI מלא ועדכני ← 3. סינתזת TTS ← 4. שמירה/אריזה אטומית</b>"
        )
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet("background-color: #1e293b; padding: 10px 12px; border-radius: 8px; color: #fbbf24;")
        layout.addWidget(lbl_info)

        # בחירת היקף מפורשת
        grp_scope = QGroupBox("היקף היצוא (Scope)")
        sc_lay = QVBoxLayout(grp_scope)
        ch_count = len(self.book.get("chapters") or [])
        ch_title = (
            self.book["chapters"][self.chapter_index].get("title", f"פרק {self.chapter_index + 1}")
            if 0 <= self.chapter_index < ch_count
            else f"פרק {self.chapter_index + 1}"
        )
        self.radio_scope_chapter = QRadioButton(f"📖 הפרק הנוכחי בלבד ({ch_title})")
        self.radio_scope_book = QRadioButton(
            f"📚 הספר כולו ({self.book.get('title', '')} — כל {ch_count} הפרקים לפי הסדר)"
        )
        if default_scope == "book":
            self.radio_scope_book.setChecked(True)
        else:
            self.radio_scope_chapter.setChecked(True)
        self.radio_scope_chapter.toggled.connect(self._on_scope_changed)
        sc_lay.addWidget(self.radio_scope_chapter)
        sc_lay.addWidget(self.radio_scope_book)

        # בחירת טווח מקטעים בספר שלם + אישור המשך ל-PDF עם עמודים ריקים/סרוקים
        self.chk_custom_range = QCheckBox("עבד וייצא טווח מקטעים מסוים בלבד (מתוך כל מקטעי הספר)")
        self.chk_custom_range.toggled.connect(self._on_range_toggled)
        sc_lay.addWidget(self.chk_custom_range)

        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("ממקטע:"))
        self.spin_from = QSpinBox()
        self.spin_from.setRange(1, max(1, ch_count))
        self.spin_from.setValue(1)
        self.spin_from.valueChanged.connect(self._refresh_readiness)
        range_row.addWidget(self.spin_from)
        range_row.addWidget(QLabel("עד מקטע:"))
        self.spin_to = QSpinBox()
        self.spin_to.setRange(1, max(1, ch_count))
        self.spin_to.setValue(max(1, ch_count))
        self.spin_to.valueChanged.connect(self._refresh_readiness)
        range_row.addWidget(self.spin_to)
        range_row.addStretch()
        sc_lay.addLayout(range_row)

        cov = (self.book or {}).get("pdf_coverage") or {}
        has_partial_pdf = isinstance(cov, dict) and cov.get("source_type") == "pdf" and not cov.get("is_complete_extraction", True)
        self.chk_allow_partial_pdf = QCheckBox("כלול עמודי טקסט זמינים גם אם קיימים עמודי תמונה/ריקים ב-PDF")
        self.chk_allow_partial_pdf.setVisible(bool(has_partial_pdf))
        self.chk_allow_partial_pdf.setChecked(False)
        self.chk_allow_partial_pdf.toggled.connect(self._refresh_readiness)
        sc_lay.addWidget(self.chk_allow_partial_pdf)

        layout.addWidget(grp_scope)

        # שלב 1: הגדרות תרגום
        grp_trans = QGroupBox("שלב 1 — תרגום כל המשפטים הדרושים")
        form_t = QFormLayout(grp_trans)
        t_cfg = self.settings.get("translation", {})

        self.combo_engine = QComboBox()
        for eid, elabel in TRANSLATION_ENGINES:
            self.combo_engine.addItem(elabel, eid)
        idx = self.combo_engine.findData(t_cfg.get("engine", "google_free"))
        if idx >= 0:
            self.combo_engine.setCurrentIndex(idx)
        self.combo_engine.currentIndexChanged.connect(self._refresh_readiness)
        form_t.addRow("מנוע תרגום:", self.combo_engine)

        lang_row = QHBoxLayout()
        self.combo_src = QComboBox()
        self.combo_tgt = QComboBox()
        for code, label in LANGUAGE_OPTIONS:
            self.combo_src.addItem(label, code)
            if code != "auto":
                self.combo_tgt.addItem(label, code)
        self.combo_src.setCurrentIndex(max(0, self.combo_src.findData(t_cfg.get("source_lang", "auto"))))
        self.combo_tgt.setCurrentIndex(max(0, self.combo_tgt.findData(t_cfg.get("target_lang", "he"))))
        self.combo_src.currentIndexChanged.connect(self._refresh_readiness)
        self.combo_tgt.currentIndexChanged.connect(self._refresh_readiness)
        lang_row.addWidget(QLabel("מקור:"))
        lang_row.addWidget(self.combo_src, 1)
        lang_row.addWidget(QLabel("יעד:"))
        lang_row.addWidget(self.combo_tgt, 1)
        form_t.addRow("שפות:", lang_row)

        self.chk_fallback_ai = QCheckBox(
            "אם התרגום החינמי נכשל/נחסם, נסה את ספק ה-AI שנבחר (עשוי לצרוך מכסה/תשלום)"
        )
        self.chk_fallback_ai.setChecked(bool(t_cfg.get("fallback_to_ai_on_failure", False)))
        form_t.addRow("גיבוי התאוששות:", self.chk_fallback_ai)

        layout.addWidget(grp_trans)

        # שלב 2: הגדרות תסריט
        grp_script = QGroupBox("שלב 2 — תסריט AI מלא ועדכני")
        form_s = QVBoxLayout(grp_script)
        mode_row = QHBoxLayout()
        self.radio_edu = QRadioButton("🎓 לימודי (מורה ותלמיד)")
        self.radio_edu.setChecked(True)
        self.radio_dramatic = QRadioButton("🎭 דרמטי (קריינות)")
        self.radio_edu.toggled.connect(self._refresh_readiness)
        mode_row.addWidget(self.radio_edu)
        mode_row.addWidget(self.radio_dramatic)
        form_s.addLayout(mode_row)

        self.txt_instruction = QLineEdit()
        self.txt_instruction.setPlaceholderText("הנחיה נוספת לתסריט (אופציונלי)")
        self.txt_instruction.textChanged.connect(self._refresh_readiness)
        form_s.addWidget(self.txt_instruction)

        wiki_row = QHBoxLayout()
        self.chk_wiki = QCheckBox("🌐 העשר מוויקיפדיה")
        self.chk_wiki.toggled.connect(self._refresh_readiness)
        self.txt_wiki_terms = QLineEdit()
        self.txt_wiki_terms.setPlaceholderText("מושגים מופרדים בפסיק (ריק = אוטומטי)")
        self.txt_wiki_terms.setEnabled(False)
        self.chk_wiki.toggled.connect(self.txt_wiki_terms.setEnabled)
        self.txt_wiki_terms.textChanged.connect(self._refresh_readiness)
        wiki_row.addWidget(self.chk_wiki)
        wiki_row.addWidget(self.txt_wiki_terms, 1)
        form_s.addLayout(wiki_row)
        layout.addWidget(grp_script)

        # שלב 3 ו-4: TTS ואריזה
        grp_tts = QGroupBox("שלבים 3–4 — סינתזת TTS ואריזת תוצר")
        form_a = QFormLayout(grp_tts)

        self.combo_format = QComboBox()
        self.combo_format.currentIndexChanged.connect(self._refresh_readiness)
        form_a.addRow("תצורת יצוא:", self.combo_format)

        self.combo_voice = QComboBox()
        self.combo_voice.addItem("קול עברית - אברי (Edge-TTS)", "edge_avri")
        self.combo_voice.addItem("קול עברית - הילה (Edge-TTS)", "edge_hila")
        self.combo_voice.addItem("מנוע מקומי Windows (SAPI5)", "sapi5")
        v_idx = self.combo_voice.findData(default_voice)
        if v_idx >= 0:
            self.combo_voice.setCurrentIndex(v_idx)
        self.combo_voice.currentIndexChanged.connect(self._refresh_readiness)
        form_a.addRow("קול ברירת מחדל:", self.combo_voice)
        layout.addWidget(grp_tts)

        grp_status = QGroupBox("סטטוס מוכנות ועדכניות")
        st_lay = QVBoxLayout(grp_status)
        self.lbl_stage1_status = QLabel("")
        self.lbl_stage2_status = QLabel("")
        self.lbl_stage1_status.setWordWrap(True)
        self.lbl_stage2_status.setWordWrap(True)
        st_lay.addWidget(self.lbl_stage1_status)
        st_lay.addWidget(self.lbl_stage2_status)
        layout.addWidget(grp_status)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("ביטול")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        self.btn_go = QPushButton("🚀 הפעל יצוא מלא")
        self.btn_go.setObjectName("primaryBtn")
        self.btn_go.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_go)
        layout.addLayout(btn_row)

        self._populate_format_options()
        self._sync_range_controls()
        self._refresh_readiness()

    def _sync_range_controls(self):
        ch_count = len((self.book or {}).get("chapters") or [])
        is_book = self.radio_scope_book.isChecked() and ch_count > 1
        self.chk_custom_range.setEnabled(is_book)
        use_r = is_book and self.chk_custom_range.isChecked()
        self.spin_from.setEnabled(use_r)
        self.spin_to.setEnabled(use_r)

    def _on_range_toggled(self):
        self._sync_range_controls()
        self._refresh_readiness()

    def _populate_format_options(self):
        is_book = self.radio_scope_book.isChecked()
        self.combo_format.blockSignals(True)
        self.combo_format.clear()
        if is_book:
            self.combo_format.addItem("📦 ארכיון ZIP — פרקי MP3 ממוספרים + manifest.json", "zip_mp3")
            self.combo_format.addItem("📦 ארכיון ZIP — פרקי WAV ממוספרים + manifest.json", "zip_wav")
            self.combo_format.addItem("📁 תיקייה מסודרת — פרקי MP3 ממוספרים + manifest.json", "folder_mp3")
            self.combo_format.addItem("📁 תיקייה מסודרת — פרקי WAV ממוספרים + manifest.json", "folder_wav")
            self.combo_format.addItem("🔊 קובץ WAV מאוחד לכל הספר (הזרמה תקנית)", "unified_wav")
            self.combo_format.addItem("🎵 קובץ MP3 מאוחד לכל הספר (פענוח/קידוד FFmpeg)", "unified_mp3")
        else:
            self.combo_format.addItem("🎵 קובץ MP3 לפרק הנוכחי (Edge-TTS רב-קולי)", "mp3")
            self.combo_format.addItem("🔊 קובץ WAV אופליין לפרק הנוכחי (Windows SAPI5)", "wav")
        self.combo_format.blockSignals(False)

    def _on_scope_changed(self):
        self._populate_format_options()
        self._sync_range_controls()
        self._refresh_readiness()

    def _refresh_readiness(self):
        from export_pipeline import assess_book_pipeline, assess_chapter_pipeline

        opts = self.get_options()
        if opts["scope"] == "book":
            b_status = assess_book_pipeline(
                book=self.book,
                settings=self.settings,
                translation_opts=opts["translation_opts"],
                script_opts=opts["script_opts"],
                tts_opts=opts["tts_opts"],
                store=self.store,
                allow_partial_pdf=opts.get("allow_partial_pdf", False),
            )
            total = b_status["chapter_count"]
            if not b_status["book_complete"]:
                self.lbl_stage1_status.setStyleSheet("color: #f87171; font-size: 12px; font-weight: bold;")
                self.lbl_stage1_status.setText(f"❌ הספר אינו שלם לעיבוד מלא: {b_status['book_completeness_reason']}")
                self.lbl_stage2_status.setStyleSheet("color: #f87171; font-size: 12px;")
                self.lbl_stage2_status.setText("לא ניתן להכריז על ספר חלקי כספר שהושלם — תקן/בצע OCR לעמודים החסרים.")
                return

            self.lbl_stage1_status.setStyleSheet("color: #34d399; font-size: 12px;" if b_status["all_translations_ready"] else "color: #fbbf24; font-size: 12px;")
            t_msg = (
                f"שלב 1 (תרגום ספר מלא): מוכנים {b_status['translations_ready']} מתוך {total} פרקים "
                f"(יחידות תקינות שמורות: {b_status.get('units_completed', 0)}/{b_status.get('total_translation_units', 0)})."
            )
            rp = b_status.get("resume_point")
            if rp and not b_status["all_translations_ready"]:
                t_msg += f" נקודת המשך: פרק {rp.get('chapter_number', 1)} יחידה {rp.get('unit_id', 'u1')}."
            self.lbl_stage1_status.setText(t_msg)
            self.lbl_stage2_status.setStyleSheet("color: #34d399; font-size: 12px;" if b_status["all_scripts_ready"] else "color: #fbbf24; font-size: 12px;")
            self.lbl_stage2_status.setText(
                f"שלב 2–3 (תסריט ו-TTS): תסריטים מוכנים {b_status['scripts_ready']}/{total} | שמע במטמון {b_status['tts_ready']}/{total}."
            )
            return

        status = assess_chapter_pipeline(
            book_id=self.book_id,
            chapter_index=self.chapter_index,
            source_text=self.source_text,
            settings=self.settings,
            translation_opts=opts["translation_opts"],
            script_opts=opts["script_opts"],
            tts_opts=opts["tts_opts"],
            store=self.store,
        )
        if status["translation_ready"]:
            self.lbl_stage1_status.setStyleSheet("color: #34d399; font-size: 12px;")
            self.lbl_stage1_status.setText("✅ שלב 1 (תרגום כל המשפטים): עדכני ושלם בתיקיית המשתמש.")
        elif status.get("has_partial_translation"):
            tp = status.get("translation_progress") or {}
            self.lbl_stage1_status.setStyleSheet("color: #fbbf24; font-size: 12px;")
            self.lbl_stage1_status.setText(
                f"⚠️ שלב 1 (תרגום חלקי שמור): נשמרו {tp.get('completed_count', 0)}/{tp.get('total_units', 0)} יחידות תקינות "
                f"— המשך מיחידה {tp.get('next_unit_id', 'u1')} לפני יצירת תסריט."
            )
        else:
            self.lbl_stage1_status.setStyleSheet("color: #fbbf24; font-size: 12px;")
            self.lbl_stage1_status.setText(f"⚠️ שלב 1 (תרגום): {status['translation_reason']} — יבוצע לפני יצירת התסריט.")

        if status["script_ready"]:
            self.lbl_stage2_status.setStyleSheet("color: #34d399; font-size: 12px;")
            self.lbl_stage2_status.setText("✅ שלב 2 (תסריט AI מלא): עדכני ותואם לתרגום ולהגדרות.")
        elif status.get("has_partial_script"):
            sp = status.get("script_progress") or {}
            self.lbl_stage2_status.setStyleSheet("color: #fbbf24; font-size: 12px;")
            self.lbl_stage2_status.setText(
                f"⚠️ שלב 2 (תסריט חלקי באצוות): נשמרו {sp.get('completed_count', 0)}/{sp.get('total_units', 0)} יחידות תסריט "
                f"— יושלם מהאצווה הבאה לפני ה-TTS."
            )
        else:
            self.lbl_stage2_status.setStyleSheet("color: #fbbf24; font-size: 12px;")
            self.lbl_stage2_status.setText(f"⚠️ שלב 2 (תסריט): {status['script_reason']} — ייווצר מחדש לפני ה-TTS.")

    def get_options(self) -> Dict[str, Any]:
        scope = "book" if self.radio_scope_book.isChecked() else "chapter"
        raw_fmt = self.combo_format.currentData() or ("zip_mp3" if scope == "book" else "mp3")

        if scope == "book":
            if raw_fmt == "zip_mp3":
                package_mode, audio_fmt = "zip", "mp3"
            elif raw_fmt == "zip_wav":
                package_mode, audio_fmt = "zip", "wav"
            elif raw_fmt == "folder_mp3":
                package_mode, audio_fmt = "folder", "mp3"
            elif raw_fmt == "folder_wav":
                package_mode, audio_fmt = "folder", "wav"
            elif raw_fmt == "unified_wav":
                package_mode, audio_fmt = "unified_wav", "wav"
            else:
                package_mode, audio_fmt = "unified_mp3", "mp3"
        else:
            package_mode = "single_chapter"
            audio_fmt = raw_fmt

        voice = self.combo_voice.currentData()
        if audio_fmt == "wav" and str(voice).startswith("edge"):
            voice = "sapi5"

        target_chapters = None
        if scope == "book" and self.chk_custom_range.isChecked():
            a = min(self.spin_from.value(), self.spin_to.value())
            b = max(self.spin_from.value(), self.spin_to.value())
            target_chapters = list(range(a - 1, b))

        return {
            "scope": scope,
            "target_chapters": target_chapters,
            "allow_partial_pdf": bool(self.chk_allow_partial_pdf.isChecked()),
            "package_mode": package_mode,
            "translation_opts": {
                "engine": self.combo_engine.currentData(),
                "source_lang": self.combo_src.currentData(),
                "target_lang": self.combo_tgt.currentData(),
                "fallback_to_ai_on_failure": self.chk_fallback_ai.isChecked(),
            },
            "script_opts": {
                "mode": "educational" if self.radio_edu.isChecked() else "dramatic",
                "instruction": self.txt_instruction.text().strip(),
                "use_wikipedia": self.chk_wiki.isChecked(),
                "wiki_terms": self.txt_wiki_terms.text().strip(),
            },
            "tts_opts": {
                "output_format": audio_fmt,
                "default_voice": voice,
                "speed": self.default_speed,
            },
        }


class BookRecoveryDialog(QDialog):
    """
    דיאלוג התאוששות אינטראקטיבי בעת חסימת ספק תרגום (כגון NetFree / HTTP 418) או עצירת משימת ספר שלם.
    מציג מצב שמור מדויק ומציע:
      - 'נסה שוב' (retry)
      - 'החלף ספק והמשך' (switch_and_resume) בלי לבטל פרקים תקינים שכבר הושלמו
      - 'המשך מטמון / פרקים שנכשלו בלבד' (resume_failed_only)
      - 'המשך להכנת שאר הפרקים (סמן פרק כטעון טיפול)' (continue_other_chapters) -> מצב partial ללא יצוא שמע
    """

    def __init__(
        self,
        parent=None,
        *,
        book_title: str = "",
        chapter_number: int = 1,
        chapter_count: int = 1,
        unit_index: Optional[int] = None,
        unit_id: str = "",
        stage: str = "translation",
        error_summary: str = "",
        is_blocked: bool = False,
        blocked_provider: str = "",
        blocked_filter: str = "",
        settings: Optional[Dict[str, Any]] = None,
        completed_count: int = 0,
        units_completed: int = 0,
        units_missing: int = 0,
        units_failed: int = 0,
        failed_chapters: Optional[Dict[str, Any]] = None,
        allow_continue_other_chapters: bool = True,
    ):
        super().__init__(parent)
        self.setWindowTitle("🛠️ התאוששות והמשך עיבוד ספר")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(640, 500)
        self.settings = settings or load_settings()
        self.action_choice = "cancel"

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        unit_label = ""
        if unit_id:
            unit_label = f" | יחידה/משפט: <b>{unit_id}</b>"
        elif unit_index is not None:
            unit_label = f" | משפט/יחידה: <b>{int(unit_index) + 1}</b>"

        if is_blocked:
            filter_info = f" ({blocked_filter})" if blocked_filter else ""
            header_txt = (
                f"⛔ <b>הספק החינמי '{blocked_provider or 'המנוע הנבחר'}' חסום{filter_info}</b><br>"
                f"המשימה נעצרה בפרק <b>{chapter_number} מתוך {chapter_count}</b> בשלב <b>{stage}</b>{unit_label}.<br>"
                "<i>גוף HTML גולמי של דף החסימה אינו נשמר ואינו מוצג כאן.</i>"
            )
            bg = "#450a0a"
            fg = "#fecaca"
        else:
            header_txt = (
                f"⚠️ <b>עיבוד הספר '{book_title}' נעצר בפרק {chapter_number} מתוך {chapter_count}</b> "
                f"(שלב: <b>{stage}</b>{unit_label})."
            )
            bg = "#451a03"
            fg = "#fde68a"

        self.lbl_diagnosis = QLabel(header_txt)
        self.lbl_diagnosis.setWordWrap(True)
        self.lbl_diagnosis.setStyleSheet(f"background-color: {bg}; color: {fg}; padding: 12px; border-radius: 8px;")
        layout.addWidget(self.lbl_diagnosis)

        clean_err = error_summary or ""
        if "<html" in clean_err.lower() or "<!doctype" in clean_err.lower():
            clean_err = "התקבל דף חסימה HTML במקום תרגום תקין (ספק חסום — ראה אפשרויות המשך למטה)."
        self.lbl_error_detail = QLabel(clean_err)
        self.lbl_error_detail.setWordWrap(True)
        self.lbl_error_detail.setStyleSheet("color: #e2e8f0; font-size: 12px;")
        layout.addWidget(self.lbl_error_detail)

        failed_cnt = len(failed_chapters or {})
        preserved_txt = (
            f"💾 <b>נשמרו אטומית {completed_count} פרקים שלמים</b> — פרקים ויחידות שכבר הושלמו לא ייווצרו מחדש "
            "גם לאחר החלפת ספק התרגום או הפעלה מחדש של התוכנה."
        )
        if units_completed or units_missing or units_failed:
            preserved_txt += (
                f"<br>📊 פירוט יחידות: הושלמו ותקינות <b>{units_completed}</b> | "
                f"חסרות/ממתינות <b>{units_missing}</b> | נכשלו <b>{units_failed}</b>."
            )
        if failed_cnt:
            nums = ", ".join(str(int(k) + 1) for k in sorted((failed_chapters or {}).keys(), key=lambda x: int(x) if str(x).isdigit() else 9999)[:12])
            preserved_txt += f"<br>📌 פרקים טעוני טיפול כעת: <b>{nums}</b>"
        self.lbl_preserved = QLabel(preserved_txt)
        self.lbl_preserved.setWordWrap(True)
        self.lbl_preserved.setStyleSheet("background-color: #0f172a; color: #34d399; padding: 10px; border-radius: 8px;")
        layout.addWidget(self.lbl_preserved)

        grp_switch = QGroupBox("החלפת ספק / אישור מדיניות Fallback להמשך")
        sw_lay = QFormLayout(grp_switch)
        t_cfg = self.settings.get("translation", {})

        self.combo_recovery_engine = QComboBox()
        for eid, elabel in TRANSLATION_ENGINES:
            self.combo_recovery_engine.addItem(elabel, eid)
        # אם המנוע הנוכחי נחסם — הצע כברירת מחדל בדיאלוג ההחלפה מנוע אחר (למשל AI אם מוגדר)
        cur_eng = t_cfg.get("engine", "google_free")
        default_pick = "ai" if (is_blocked and cur_eng == "google_free") else cur_eng
        idx = self.combo_recovery_engine.findData(default_pick)
        if idx >= 0:
            self.combo_recovery_engine.setCurrentIndex(idx)
        sw_lay.addRow("ספק תרגום להמשך:", self.combo_recovery_engine)

        self.chk_recovery_ai_fallback = QCheckBox(
            "אשר מעבר לספק ה-AI שנבחר אם החינמי נכשל/נחסם (עשוי לצרוך מכסה/תשלום)"
        )
        self.chk_recovery_ai_fallback.setChecked(bool(t_cfg.get("fallback_to_ai_on_failure", False)))
        sw_lay.addRow("מדיניות AI:", self.chk_recovery_ai_fallback)
        layout.addWidget(grp_switch)

        btn_col = QVBoxLayout()
        row1 = QHBoxLayout()

        self.btn_switch_provider = QPushButton("🔀 החלף ספק והמשך מנקודת העצירה")
        self.btn_switch_provider.setObjectName("primaryBtn")
        self.btn_switch_provider.clicked.connect(lambda: self._pick("switch_and_resume"))
        row1.addWidget(self.btn_switch_provider)

        self.btn_resume_cache = QPushButton("▶️ המשך מנקודת העצירה (יחידות/פרקים חסרים)")
        self.btn_resume_cache.clicked.connect(lambda: self._pick("resume_failed_only"))
        row1.addWidget(self.btn_resume_cache)

        self.btn_retry = QPushButton("🔄 נסה שוב")
        self.btn_retry.clicked.connect(lambda: self._pick("retry"))
        row1.addWidget(self.btn_retry)
        btn_col.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_continue_partial = QPushButton("⏭️ המשך להכנת שאר הפרקים (סמן פרק כטעון טיפול)")
        self.btn_continue_partial.setVisible(bool(allow_continue_other_chapters))
        self.btn_continue_partial.clicked.connect(lambda: self._pick("continue_other_chapters"))
        row2.addWidget(self.btn_continue_partial)

        row2.addStretch()
        self.btn_close = QPushButton("⏸️ השהה (המשך מאוחר יותר)")
        self.btn_close.clicked.connect(self.reject)
        row2.addWidget(self.btn_close)
        btn_col.addLayout(row2)

        layout.addLayout(btn_col)

    def _pick(self, action: str):
        self.action_choice = action
        self.accept()

    def get_recovery_decision(self) -> Dict[str, Any]:
        return {
            "action": self.action_choice,
            "engine": self.combo_recovery_engine.currentData(),
            "fallback_to_ai_on_failure": self.chk_recovery_ai_fallback.isChecked(),
        }


class SegmentBookDialog(QDialog):
    """
    דיאלוג פיצול מקטעים חכם לפי הצורך והעניין:
    מאפשר חלוקה מחדש של ספר ארוך למקטעים נוחיים לפי גודל יעד, כותרות וגבולות פסקאות.
    """

    def __init__(self, parent=None, book: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        from book_loader import SEGMENT_PRESETS

        self.setWindowTitle("✂️ פיצול ספר למקטעים לפי הצורך והעניין")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(520, 380)
        self.book = book or {}
        chapters = list(self.book.get("chapters") or [])
        total_chars = sum(len(ch.get("text") or "") for ch in chapters)
        cur_preset = self.book.get("segment_preset", "balanced")

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        lbl_info = QLabel(
            f"ספר: <b>{self.book.get('title', 'ללא שם')}</b><br>"
            f"מצב נוכחי: <b>{len(chapters)}</b> מקטעים/פרקים | סה\"כ <b>{total_chars:,}</b> תווים."
        )
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet("background-color: #1e293b; padding: 10px 12px; border-radius: 8px; color: #fbbf24;")
        layout.addWidget(lbl_info)

        grp_mode = QGroupBox("בחר אופן פיצול מקטעים")
        m_lay = QVBoxLayout(grp_mode)

        self.radio_compact = QRadioButton(SEGMENT_PRESETS["compact"]["label"])
        self.radio_balanced = QRadioButton(SEGMENT_PRESETS["balanced"]["label"])
        self.radio_chapter = QRadioButton(SEGMENT_PRESETS["chapter"]["label"])
        self.radio_custom = QRadioButton("מותאם אישית (מספר תווים מרבי למקטע):")

        if cur_preset == "compact":
            self.radio_compact.setChecked(True)
        elif cur_preset == "chapter":
            self.radio_chapter.setChecked(True)
        elif cur_preset == "custom":
            self.radio_custom.setChecked(True)
        else:
            self.radio_balanced.setChecked(True)

        m_lay.addWidget(self.radio_compact)
        m_lay.addWidget(self.radio_balanced)
        m_lay.addWidget(self.radio_chapter)
        m_lay.addWidget(self.radio_custom)

        custom_row = QHBoxLayout()
        custom_row.addWidget(QLabel("גודל מקטע (תווים):"))
        self.spin_chars = QSpinBox()
        self.spin_chars.setRange(800, 25000)
        self.spin_chars.setSingleStep(500)
        self.spin_chars.setValue(int(self.book.get("segment_max_chars") or 5000))
        self.spin_chars.setEnabled(self.radio_custom.isChecked())
        self.radio_custom.toggled.connect(self.spin_chars.setEnabled)
        custom_row.addWidget(self.spin_chars)
        custom_row.addStretch()
        m_lay.addLayout(custom_row)

        layout.addWidget(grp_mode)

        self.lbl_preview = QLabel("")
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setStyleSheet("color: #34d399; font-size: 12px;")
        layout.addWidget(self.lbl_preview)

        for rb in (self.radio_compact, self.radio_balanced, self.radio_chapter, self.radio_custom):
            rb.toggled.connect(self._update_preview)
        self.spin_chars.valueChanged.connect(self._update_preview)
        self._update_preview()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("ביטול")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        btn_apply = QPushButton("✂️ פצל מקטעים עכשיו")
        btn_apply.setObjectName("primaryBtn")
        btn_apply.clicked.connect(self.accept)
        btn_row.addWidget(btn_apply)
        layout.addLayout(btn_row)

    def _update_preview(self):
        from book_loader import SEGMENT_PRESETS

        opts = self.get_options()
        max_ch = opts["max_chars"] or SEGMENT_PRESETS[opts["preset"]]["max_chars"]
        total_chars = sum(len(ch.get("text") or "") for ch in (self.book.get("chapters") or []))
        est = max(1, (total_chars + max_ch - 1) // max_ch)
        self.lbl_preview.setText(
            f"הפיצול יזהה כותרות וגבולות פסקאות (הערכה: כ־{est} מקטעים נוחים לעיבוד, תרגום והקראה רציפה)."
        )

    def get_options(self) -> Dict[str, Any]:
        if self.radio_compact.isChecked():
            return {"preset": "compact", "max_chars": None}
        if self.radio_chapter.isChecked():
            return {"preset": "chapter", "max_chars": None}
        if self.radio_custom.isChecked():
            return {"preset": "custom", "max_chars": int(self.spin_chars.value())}
        return {"preset": "balanced", "max_chars": None}

