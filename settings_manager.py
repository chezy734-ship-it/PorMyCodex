# -*- coding: utf-8 -*-
"""
מנהל הגדרות מקומי עבור PorMyCodex.
שומר באופן מאובטח ומקומי במחשב המשתמש (AppData / .config) את:
- ספק ה-AI הפעיל (Google Gemini, OpenAI, Anthropic, OpenRouter, Groq, שרת מותאם)
- מפתחות API לכל חברה בנפרד
- מודל נבחר לכל חברה + רשימת מודלים שהתגלו מהשרת
- מנוע תרגום מועדף (AI / Google Translate חינמי / MyMemory / LibreTranslate)
- קולות דוברים לתסריט רב-קולי (מורה / תלמיד / מספר)
"""

import os
import json
from copy import deepcopy
from typing import Dict, Any


def get_settings_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "PorMyCodex")
    os.makedirs(path, exist_ok=True)
    return path


def get_settings_path() -> str:
    return os.path.join(get_settings_dir(), "settings.json")


DEFAULT_SETTINGS: Dict[str, Any] = {
    "active_provider": "gemini",
    "providers": {
        "gemini": {
            "api_key": "",
            "model": "gemini-2.5-flash",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "discovered_models": [],
        },
        "openai": {
            "api_key": "",
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "discovered_models": [],
        },
        "anthropic": {
            "api_key": "",
            "model": "claude-3-7-sonnet-latest",
            "base_url": "https://api.anthropic.com/v1",
            "discovered_models": [],
        },
        "openrouter": {
            "api_key": "",
            "model": "google/gemini-2.5-flash",
            "base_url": "https://openrouter.ai/api/v1",
            "discovered_models": [],
        },
        "groq": {
            "api_key": "",
            "model": "llama-3.3-70b-versatile",
            "base_url": "https://api.groq.com/openai/v1",
            "discovered_models": [],
        },
        "custom": {
            "api_key": "",
            "model": "llama3.1",
            "base_url": "http://localhost:11434/v1",
            "discovered_models": [],
        },
    },
    "translation": {
        "engine": "google_free",
        "source_lang": "auto",
        "target_lang": "he",
        "libretranslate_url": "https://libretranslate.com/translate",
        "libretranslate_api_key": "",
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
    path = get_settings_path()
    loaded: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            loaded = {}
    merged = _deep_merge(DEFAULT_SETTINGS, loaded)

    # מילוי מפתחות ממשתני סביבה רק אם לא הוזן מפתח בהגדרות
    for pid, env_name in ENV_KEY_MAP.items():
        pconf = merged["providers"].get(pid, {})
        if not pconf.get("api_key") and os.environ.get(env_name):
            pconf["api_key"] = os.environ.get(env_name, "").strip()

    return merged


def save_settings(settings: Dict[str, Any]) -> None:
    path = get_settings_path()
    clean = _deep_merge(DEFAULT_SETTINGS, settings)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)


def get_active_provider_config(settings: Dict[str, Any] = None) -> Dict[str, Any]:
    cfg = settings or load_settings()
    pid = cfg.get("active_provider", "gemini")
    providers = cfg.get("providers", {})
    pconf = dict(providers.get(pid, providers.get("gemini", {})))
    pconf["provider_id"] = pid
    return pconf
