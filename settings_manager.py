# -*- coding: utf-8 -*-
"""
מנהל הגדרות מקומי עבור PorMyCodex.
שומר באופן מאובטח ומקומי במחשב המשתמש (AppData / .config) את:
- ספק ה-AI הפעיל (Google Gemini, OpenAI, Anthropic, OpenRouter, Groq, שרת מותאם)
- מפתחות API לכל חברה בנפרד (מקומית בלבד; אינם נכללים בהודעות או בלוגים)
- מודל נבחר לכל חברה + רשימת מודלים שהתגלו מהשרת (בליווי טביעת אצבע למניעת תוצרים ישנים)
- מנוע תרגום מועדף (AI / Google Translate חינמי / MyMemory / LibreTranslate)
- קולות דוברים לתסריט רב-קולי (מורה / תלמיד / מספר)
"""

from copy import deepcopy
import hashlib
import json
import os
import tempfile
import threading
import time
from typing import Dict, Any

_SETTINGS_LOCK = threading.RLock()


def get_settings_dir() -> str:
    override = (os.environ.get("PORMYCODEX_DATA_DIR") or "").strip()
    if override:
        path = os.path.abspath(override)
        os.makedirs(path, exist_ok=True)
        return path
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.abspath(os.path.join(base, "PorMyCodex"))
    os.makedirs(path, exist_ok=True)
    return path


def get_settings_path() -> str:
    return os.path.join(get_settings_dir(), "settings.json")


def compute_provider_fingerprint(provider_id: str, api_key: str, base_url: str) -> str:
    """
    מחשב חתימה חד-כיוונית (SHA-256) עבור שילוב ספק/מפתח/כתובת כדי לזהות תוצרי גילוי ישנים
    מבלי לשמור או לחשוף את המפתח עצמו.
    """
    payload = f"{(provider_id or '').strip()}|{(base_url or '').strip().rstrip('/')}|{(api_key or '').strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


API_PACING_PRESETS: Dict[str, Dict[str, Any]] = {
    "safe_free": {
        "label": "🐢 איטי ובטוח במיוחד — 6.5 שנ' (מומלץ למפתח חינמי / מודלי Pro)",
        "min_interval_sec": 6.5,
        "max_retries_429": 5,
    },
    "gentle": {
        "label": "🚶 רגוע — 4.0 שנ' (ברירת מחדל מומלצת לספרים ארוכים)",
        "min_interval_sec": 4.0,
        "max_retries_429": 4,
    },
    "balanced": {
        "label": "⚖️ מאוזן — 2.0 שנ' (מפתח API בתשלום / Tier 1)",
        "min_interval_sec": 2.0,
        "max_retries_429": 4,
    },
    "fast": {
        "label": "⚡ מהיר — 0.5 שנ' (מכסה גבוהה בלבד)",
        "min_interval_sec": 0.5,
        "max_retries_429": 3,
    },
    "custom": {
        "label": "🎛️ מותאם אישית",
        "min_interval_sec": 4.0,
        "max_retries_429": 4,
    },
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "active_provider": "gemini",
    "providers": {
        "gemini": {
            "api_key": "",
            "model": "gemini-2.5-flash",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "discovered_models": [],
            "discovered_fingerprint": "",
        },
        "openai": {
            "api_key": "",
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "discovered_models": [],
            "discovered_fingerprint": "",
        },
        "anthropic": {
            "api_key": "",
            "model": "claude-3-7-sonnet-latest",
            "base_url": "https://api.anthropic.com/v1",
            "discovered_models": [],
            "discovered_fingerprint": "",
        },
        "openrouter": {
            "api_key": "",
            "model": "google/gemini-2.5-flash",
            "base_url": "https://openrouter.ai/api/v1",
            "discovered_models": [],
            "discovered_fingerprint": "",
        },
        "groq": {
            "api_key": "",
            "model": "llama-3.3-70b-versatile",
            "base_url": "https://api.groq.com/openai/v1",
            "discovered_models": [],
            "discovered_fingerprint": "",
        },
        "custom": {
            "api_key": "",
            "model": "",
            "base_url": "http://localhost:11434/v1",
            "discovered_models": [],
            "discovered_fingerprint": "",
        },
    },
    "api_pacing": {
        "preset": "gentle",
        "min_interval_sec": 4.0,
        "adaptive_slowdown": True,
        "max_retries_429": 4,
    },
    "translation": {
        "engine": "google_free",
        "source_lang": "auto",
        "target_lang": "he",
        "libretranslate_url": "https://libretranslate.com/translate",
        "libretranslate_api_key": "",
        "fallback_to_ai_on_failure": False,
        "fallback_secondary_engine": "",
    },
    "narration": {
        "teacher_voice": "edge_avri",
        "student_voice": "edge_hila",
        "narrator_voice": "edge_avri",
    },
}

ENV_KEY_MAP = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "custom": "CUSTOM_AI_API_KEY",
}


def _deep_merge(defaults: Dict[str, Any], loaded: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(defaults)
    for k, v in (loaded or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_settings() -> Dict[str, Any]:
    with _SETTINGS_LOCK:
        path = get_settings_path()
        loaded: Dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception:
                loaded = {}
        merged = _deep_merge(DEFAULT_SETTINGS, loaded)

        # מיגרציה חד-פעמית משמות מודלים לא קיימים מגרסה ישנה (לעולם לא דורס מודל תקף כמו gemini-2.5-flash)
        gem_conf = merged["providers"].get("gemini", {})
        if gem_conf.get("model") in ("gemini-3.8-flash", "gemini-3.8-pro"):
            gem_conf["model"] = "gemini-2.5-flash" if gem_conf.get("model") == "gemini-3.8-flash" else "gemini-2.5-pro"

        # הסרת שמות מודלים מומצאים מרשימות גילוי שמורות אם נשארו מגרסה ישנה
        disc = gem_conf.get("discovered_models") or []
        if disc:
            gem_conf["discovered_models"] = [m for m in disc if m not in ("gemini-3.8-flash", "gemini-3.8-pro")]

        # מילוי מפתחות ממשתני סביבה רק אם לא הוזן מפתח בהגדרות
        for pid, env_name in ENV_KEY_MAP.items():
            pconf = merged["providers"].get(pid, {})
            if not pconf.get("api_key") and os.environ.get(env_name):
                pconf["api_key"] = os.environ.get(env_name, "").strip()

        return merged


def save_settings(settings: Dict[str, Any]) -> None:
    with _SETTINGS_LOCK:
        path = get_settings_path()
        clean = _deep_merge(DEFAULT_SETTINGS, settings)
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        raw = json.dumps(clean, ensure_ascii=False, indent=2).encode("utf-8")
        fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".tmp_settings_", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            backoff = (0.025, 0.05, 0.08, 0.12, 0.18, 0.25)
            for attempt in range(len(backoff) + 1):
                try:
                    os.replace(tmp_path, path)
                    return
                except OSError as exc:
                    winerr = getattr(exc, "winerror", None)
                    retryable = isinstance(exc, PermissionError) or winerr in (5, 32, 33) or getattr(exc, "errno", None) in (13, 16)
                    if attempt < len(backoff) and retryable:
                        time.sleep(backoff[attempt])
                        continue
                    raise
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise


def get_active_provider_config(settings: Dict[str, Any] = None) -> Dict[str, Any]:
    cfg = settings or load_settings()
    pid = cfg.get("active_provider", "gemini")
    providers = cfg.get("providers", {})
    pconf = dict(providers.get(pid, providers.get("gemini", {})))
    pconf["provider_id"] = pid
    return pconf


def get_api_pacing_config(settings: Dict[str, Any] = None) -> Dict[str, Any]:
    """מחזיר תצורת קצב בקשות API תקינה ומוגבלת לטווחים בטוחים."""
    cfg = settings if settings is not None else load_settings()
    raw = dict(DEFAULT_SETTINGS["api_pacing"])
    if isinstance(cfg, dict) and isinstance(cfg.get("api_pacing"), dict):
        raw.update(cfg["api_pacing"])

    preset = str(raw.get("preset") or "gentle").strip().lower()
    if preset not in API_PACING_PRESETS:
        preset = "gentle"

    try:
        min_interval = float(raw.get("min_interval_sec", API_PACING_PRESETS[preset]["min_interval_sec"]))
    except (TypeError, ValueError):
        min_interval = float(API_PACING_PRESETS[preset]["min_interval_sec"])
    min_interval = max(0.0, min(60.0, min_interval))

    try:
        max_429 = int(raw.get("max_retries_429", API_PACING_PRESETS[preset]["max_retries_429"]))
    except (TypeError, ValueError):
        max_429 = int(API_PACING_PRESETS[preset]["max_retries_429"])
    max_429 = max(0, min(8, max_429))

    return {
        "preset": preset,
        "min_interval_sec": round(min_interval, 2),
        "adaptive_slowdown": bool(raw.get("adaptive_slowdown", True)),
        "max_retries_429": max_429,
    }

