# -*- coding: utf-8 -*-
"""
שכבת ספקי בינה מלאכותית אוניברסלית (Multi-Provider AI Client) עבור PorMyCodex.
תומכת ב:
- Google Gemini (כולל גילוי מודלים חי מה-API)
- OpenAI (כולל גילוי מודלים חי)
- Anthropic Claude
- OpenRouter (מאות מודלים מכל החברות, כולל חינמיים)
- Groq (מהירות שיא)
- שרת מותאם / מקומי (Ollama, LM Studio, OpenAI-Compatible)
"""

from typing import List, Dict, Any, Optional
import json
import requests

PROVIDER_CATALOG: Dict[str, Dict[str, Any]] = {
    "gemini": {
        "label": "Google Gemini",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "requires_key": True,
        "preset_models": [
            "gemini-3.8-flash",
            "gemini-3.8-pro",
            "gemini-2.5-pro",
        ],
        "help_url": "https://aistudio.google.com/apikey",
    },
    "openai": {
        "label": "OpenAI (ChatGPT)",
        "default_base_url": "https://api.openai.com/v1",
        "requires_key": True,
        "preset_models": [
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-4.1-mini",
            "gpt-4.1",
            "o3-mini",
        ],
        "help_url": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "default_base_url": "https://api.anthropic.com/v1",
        "requires_key": True,
        "preset_models": [
            "claude-3-7-sonnet-latest",
            "claude-sonnet-4-20250514",
            "claude-3-5-haiku-latest",
            "claude-3-5-sonnet-latest",
        ],
        "help_url": "https://console.anthropic.com/settings/keys",
    },
    "openrouter": {
        "label": "OpenRouter (מגוון חברות ומודלים)",
        "default_base_url": "https://openrouter.ai/api/v1",
        "requires_key": True,
        "preset_models": [
            "google/gemini-2.5-flash",
            "anthropic/claude-3.7-sonnet",
            "openai/gpt-4o-mini",
            "deepseek/deepseek-chat-v3-0324:free",
            "meta-llama/llama-3.3-70b-instruct:free",
        ],
        "help_url": "https://openrouter.ai/keys",
    },
    "groq": {
        "label": "Groq (Llama / Mixtral במהירות גבוהה)",
        "default_base_url": "https://api.groq.com/openai/v1",
        "requires_key": True,
        "preset_models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ],
        "help_url": "https://console.groq.com/keys",
    },
    "custom": {
        "label": "שרת מותאם / מקומי (Ollama / LM Studio / OpenAI-API)",
        "default_base_url": "http://localhost:11434/v1",
        "requires_key": False,
        "preset_models": [
            "llama3.1",
            "qwen2.5:14b",
            "gemma2",
            "mistral",
        ],
        "help_url": "https://ollama.com",
    },
}


def get_provider_label(provider_id: str) -> str:
    return PROVIDER_CATALOG.get(provider_id, {}).get("label", provider_id)


def get_preset_models(provider_id: str) -> List[str]:
    return list(PROVIDER_CATALOG.get(provider_id, {}).get("preset_models", []))


def _rank_gemini_model(mid: str) -> tuple:
    low = mid.lower()
    if low == "gemini-3.8-flash":
        return (0, low)
    if "gemini-3.8" in low:
        return (1, low)
    if "gemini-3" in low:
        return (2, low)
    if "gemini-2.5-pro" in low:
        return (3, low)
    if "gemini-2.5" in low:
        return (4, low)
    if "gemini-2" in low:
        return (5, low)
    if "gemini-" in low:
        return (6, low)
    return (7, low)


def discover_models(provider_id: str, api_key: str, base_url: str = "", timeout: int = 20) -> List[str]:
    """
    מבצע בדיקת חיבור אמיתית מול הספק ומחזיר את רשימת המודלים הזמינים בחשבון.
    """
    meta = PROVIDER_CATALOG.get(provider_id)
    if not meta:
        raise ValueError(f"ספק לא מוכר: {provider_id}")

    key = (api_key or "").strip()
    if meta.get("requires_key", True) and not key:
        raise ValueError("יש להזין מפתח API לפני בדיקת החיבור וגילוי המודלים.")

    url_base = (base_url or meta["default_base_url"]).strip().rstrip("/")

    try:
        if provider_id == "gemini":
            resp = requests.get(
                f"{url_base}/models?pageSize=1000",
                headers={"x-goog-api-key": key, "Accept": "application/json"},
                timeout=timeout,
            )
            _raise_for_api_error("Google Gemini", resp)
            data = resp.json()
            models = []
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods:
                    mid = m.get("name", "")
                    if mid.startswith("models/"):
                        mid = mid[len("models/"):]
                    low = mid.lower()
                    if mid and not any(skip in low for skip in ("embedding", "aqa", "imagen", "veo", "tts", "audio", "robotics", "computer-use")):
                        models.append(mid)
            unique = sorted(set(models), key=_rank_gemini_model)
            return unique or get_preset_models("gemini")

        elif provider_id == "anthropic":
            resp = requests.get(
                f"{url_base}/models",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "Accept": "application/json",
                },
                timeout=timeout,
            )
            _raise_for_api_error("Anthropic", resp)
            data = resp.json()
            models = [m.get("id") for m in data.get("data", []) if m.get("id")]
            return sorted(set(models), reverse=True) or get_preset_models("anthropic")

        else:
            # OpenAI / OpenRouter / Groq / Custom OpenAI-compatible
            headers = {"Accept": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            resp = requests.get(f"{url_base}/models", headers=headers, timeout=timeout)
            _raise_for_api_error(meta["label"], resp)
            data = resp.json()
            raw_list = data.get("data", []) if isinstance(data, dict) else []
            models = [m.get("id") for m in raw_list if isinstance(m, dict) and m.get("id")]
            if provider_id == "openai":
                filtered = [
                    m for m in models
                    if any(pref in m for pref in ("gpt-", "o1", "o3", "o4", "chatgpt"))
                    and not any(bad in m for bad in ("audio", "realtime", "transcribe", "tts", "image", "dall-e", "whisper"))
                ]
                models = filtered or models
            return sorted(set(models)) or get_preset_models(provider_id)

    except requests.exceptions.Timeout:
        raise RuntimeError(f"הבקשה אל {meta['label']} חרגה מזמן ההמתנה.")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"לא ניתן להתחבר אל {meta['label']}: {e}")


def chat_completion(
    provider_id: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    base_url: str = "",
    json_mode: bool = False,
    temperature: float = 0.25,
    max_tokens: int = 8192,
    timeout: int = 90,
) -> str:
    """
    קריאה אחידה להפקת טקסט או JSON בכל אחד מספקי ה-AI הנתמכים.
    """
    meta = PROVIDER_CATALOG.get(provider_id)
    if not meta:
        raise ValueError(f"ספק AI לא מוכר: {provider_id}")

    key = (api_key or "").strip()
    if meta.get("requires_key", True) and not key:
        raise ValueError(f"לא הוגדר מפתח API עבור {meta['label']}. פתח 'הגדרות AI' והזן מפתח.")

    selected_model = (model or "").strip() or get_preset_models(provider_id)[0]
    url_base = (base_url or meta["default_base_url"]).strip().rstrip("/")

    if provider_id == "gemini":
        gen_cfg: Dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": gen_cfg,
        }
        resp = requests.post(
            f"{url_base}/models/{selected_model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        # אם מודל ספציפי הוצא משימוש (404), מנסים אוטומטית את המודל המומלץ או gemini-3.8-flash
        if resp.status_code == 404:
            import re
            body_txt = resp.text or ""
            m_rec = re.search(r"use\s+models/([a-zA-Z0-9._-]+)", body_txt, flags=re.IGNORECASE)
            fallback_model = m_rec.group(1) if m_rec else "gemini-3.8-flash"
            if fallback_model and fallback_model != selected_model:
                resp = requests.post(
                    f"{url_base}/models/{fallback_model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                )
        _raise_for_api_error("Google Gemini", resp)
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini לא החזיר תשובה (אולי נחסם ע\"י מסנן בטיחות).")
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()

    elif provider_id == "anthropic":
        payload = {
            "model": selected_model,
            "max_tokens": min(max_tokens, 8192),
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        resp = requests.post(
            f"{url_base}/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        _raise_for_api_error("Anthropic Claude", resp)
        data = resp.json()
        blocks = data.get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    else:
        # OpenAI / OpenRouter / Groq / Custom OpenAI-compatible
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if provider_id == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/chezy734-ship-it/PorMyCodex"
            headers["X-Title"] = "PorMyCodex"

        payload = {
            "model": selected_model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        # לחלק מהמודלים המקומיים/מותאמים response_format עלול להיתקע, אז מפעילים בבטחה איפה שנתמך
        if json_mode and provider_id in ("openai", "groq"):
            payload["response_format"] = {"type": "json_object"}

        resp = requests.post(
            f"{url_base}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        _raise_for_api_error(meta["label"], resp)
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"{meta['label']} לא החזיר תשובה.")
        return (choices[0].get("message", {}).get("content") or "").strip()


def extract_json_object(raw_text: str) -> Dict[str, Any]:
    """מחלץ JSON נקי גם אם המודל עטף אותו ב-```json ... ```."""
    txt = (raw_text or "").strip()
    if txt.startswith("```"):
        lines = txt.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        txt = "\n".join(lines).strip()

    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        start = txt.find("{")
        end = txt.rfind("}")
        if start != -1 and end > start:
            return json.loads(txt[start:end + 1])
        raise


def _raise_for_api_error(provider_name: str, resp: requests.Response) -> None:
    if resp.ok:
        return
    code = resp.status_code
    detail = ""
    try:
        err_json = resp.json()
        if isinstance(err_json, dict):
            detail = (
                err_json.get("error", {}).get("message")
                if isinstance(err_json.get("error"), dict)
                else str(err_json.get("error") or err_json.get("message") or "")
            )
    except Exception:
        detail = (resp.text or "")[:250]

    if code == 401:
        raise RuntimeError(f"{provider_name} דחה את המפתח (401). בדוק שמפתח ה-API תקין.")
    if code == 403:
        raise RuntimeError(f"הגישה אל {provider_name} נדחתה (403). {detail}")
    if code == 429:
        raise RuntimeError(f"חריגה ממכסת הבקשות של {provider_name} (429). {detail}")
    raise RuntimeError(f"שגיאת {provider_name} (HTTP {code}): {detail}")
