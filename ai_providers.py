# -*- coding: utf-8 -*-
"""
שכבת ספקי בינה מלאכותית אוניברסלית (Multi-Provider AI Client) עבור PorMyCodex.

מאפיינים מרכזיים:
- הפרדה מלאה בין Connect Timeout ל-Read Timeout + תקציב זמן כולל (Deadline)
- ניסיונות חוזרים (Retries) מוגבלים עם Exponential Backoff לכשלים זמניים בלבד (429/5xx/Timeout/Network)
- ביטול מיידי גם בזמן המתנה/Backoff (cancel_event.wait)
- אי-החלפה שקטה של מודלים ואי-שימוש בשמות מודלים מומצאים
- גילוי מודלים חי מה-API הרשמי של כל ספק עם Pagination מלא וסינון לפי יכולת נדרשת (text)
- סינון והסתרה מוחלטת של מפתחות API מכל שגיאה או לוג
"""

import json
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None  # type: ignore


class AICallError(RuntimeError):
    """שגיאה מובחנת ומתועדת בפעולת רשת / AI עם שלב, קטגוריה והנחיית פעולה."""

    def __init__(
        self,
        message: str,
        *,
        stage: str = "קריאת AI",
        category: str = "error",
        retryable: bool = False,
        status_code: Optional[int] = None,
        provider_id: str = "",
        blocked_filter: str = "",
    ):
        super().__init__(message)
        self.stage = stage
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.provider_id = provider_id
        self.blocked_filter = blocked_filter

    @property
    def is_blocked(self) -> bool:
        return (
            self.category == "blocked"
            or self.status_code in (418, 451)
            or bool(self.blocked_filter)
        )


class AICancelledError(AICallError):
    """נזרק מיד בעת ביטול בקשה פעילה או המתנת Backoff."""

    def __init__(self, stage: str = "קריאת AI"):
        super().__init__(
            f"הפעולה בוטלה על ידי המשתמש בשלב '{stage}'.",
            stage=stage,
            category="cancelled",
            retryable=False,
        )


def _require_requests():
    if requests is None:
        raise AICallError(
            "חבילת התקשורת 'requests' אינה מותקנת בסביבה זו.",
            stage="חיבור רשת",
            category="network",
            retryable=False,
        )
    return requests


def _sanitize_error_text(text: str, secrets: Optional[List[str]] = None) -> str:
    cleaned = str(text or "")
    for sec in secrets or []:
        s = (sec or "").strip()
        if s and len(s) >= 4:
            cleaned = cleaned.replace(s, "[REDACTED_KEY]")
    cleaned = re.sub(r"(AIza[0-9A-Za-z\-_]{10,})", "[REDACTED_KEY]", cleaned)
    cleaned = re.sub(r"(sk-[0-9A-Za-z\-_]{8,})", "[REDACTED_KEY]", cleaned)
    cleaned = re.sub(r"(ghp_[0-9A-Za-z]{8,})", "[REDACTED_KEY]", cleaned)
    cleaned = re.sub(r"([?&](?:key|api_key)=)[^&\s]+", r"\1[REDACTED_KEY]", cleaned, flags=re.IGNORECASE)
    return cleaned


def detect_blocked_response(
    resp: Any,
    body_text: str = "",
    secrets: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    מזהה תגובת חסימה / סינון תוכן / דף HTML במקום JSON או טקסט שירות תקין
    (כגון HTTP 418, דף חסימה של NetFree, או תגובת HTML משער/פרוקסי).
    לעולם אינו מחזיר את גוף ה-HTML הגולמי למשתמש.
    """
    code = int(getattr(resp, "status_code", 0) or 0) if resp is not None else 0
    headers_raw = getattr(resp, "headers", None) or {}
    content_type = ""
    if isinstance(headers_raw, dict):
        for hk, hv in headers_raw.items():
            if str(hk).lower() == "content-type":
                content_type = str(hv or "").lower()
                break

    raw_text = body_text or (getattr(resp, "text", "") if resp is not None else "") or ""
    sample = _sanitize_error_text(str(raw_text)[:4096], secrets=secrets)
    low = sample.lower()

    is_html_ct = "text/html" in content_type or "application/xhtml" in content_type
    is_html_body = any(
        tok in low
        for tok in (
            "<!doctype html",
            "<html",
            "<head>",
            "<head ",
            "<body>",
            "<body ",
            "<title>",
        )
    )
    is_html = is_html_ct or is_html_body

    filter_name = ""
    if any(tok in low for tok in ("netfree.link", "netfree", "נטפרי")):
        filter_name = "סינון NetFree (netfree.link)"
    elif any(
        tok in low
        for tok in ("אתר זה חסום", "האתר חסום", "סינון אינטרנט", "blocked by", "content filter", "web filter")
    ):
        filter_name = "מסנן תוכן"
    elif is_html:
        filter_name = "דף HTML במקום JSON"

    if code in (418, 451):
        return {
            "is_blocked": True,
            "status_code": code,
            "is_html": is_html,
            "filter_name": filter_name or f"חסימת שרת/סינון (HTTP {code})",
        }

    if is_html or filter_name:
        if code in (0, 200, 403, 406, 407, 429, 451, 502, 503) or code >= 400:
            return {
                "is_blocked": True,
                "status_code": code,
                "is_html": is_html,
                "filter_name": filter_name or "תגובת HTML/חסימה",
            }

    return None


class ProviderCircuitBreaker:
    """
    מפסק זרם (Circuit Breaker) לפי ספק/מנוע למניעת לולאת ניסיונות אינסופית כאשר ספק נחסם
    (למשל HTTP 418 / NetFree) או סובל מכשלים רצופים לאורך עשרות פרקים.
    """

    def __init__(self, transient_threshold: int = 4):
        self._lock = threading.RLock()
        self._transient_threshold = max(2, int(transient_threshold))
        self._states: Dict[str, Dict[str, Any]] = {}

    def is_open(self, provider_id: str) -> bool:
        with self._lock:
            st = self._states.get(provider_id)
            return bool(st and st.get("open"))

    def get_state(self, provider_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            st = self._states.get(provider_id)
            return dict(st) if st else None

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._states.items()}

    def reset(self, provider_id: Optional[str] = None) -> None:
        with self._lock:
            if provider_id is None:
                self._states.clear()
            else:
                self._states.pop(provider_id, None)

    def check_or_raise(self, provider_id: str, stage: str = "תרגום") -> None:
        with self._lock:
            st = self._states.get(provider_id)
            if not st or not st.get("open"):
                return
            reason = st.get("reason") or f"הספק '{provider_id}' נחסם זמנית בצינור זה."
            raise AICallError(
                reason,
                stage=stage or st.get("stage") or "תרגום",
                category=st.get("category") or "blocked",
                retryable=False,
                status_code=st.get("status_code"),
                provider_id=provider_id,
                blocked_filter=st.get("blocked_filter", ""),
            )

    def record_success(self, provider_id: str) -> None:
        with self._lock:
            st = self._states.get(provider_id)
            if st and not st.get("open"):
                st["consecutive_transient"] = 0

    def record_failure(self, provider_id: str, err: Exception) -> bool:
        cat = str(getattr(err, "category", "") or "").lower()
        code = getattr(err, "status_code", None)
        b_filter = str(getattr(err, "blocked_filter", "") or "")
        stage = str(getattr(err, "stage", "") or "תרגום")
        err_msg = str(err or "")

        is_hard_block = (
            cat in ("blocked", "auth", "model_not_found")
            or code in (418, 451)
            or bool(b_filter)
            or "netfree" in err_msg.lower()
            or "418" in err_msg
        )

        with self._lock:
            st = self._states.setdefault(
                provider_id,
                {
                    "open": False,
                    "category": "",
                    "status_code": None,
                    "blocked_filter": "",
                    "reason": "",
                    "stage": stage,
                    "tripped_at": 0.0,
                    "consecutive_transient": 0,
                },
            )
            if is_hard_block:
                st["open"] = True
                st["category"] = "blocked" if (cat == "blocked" or code in (418, 451) or b_filter) else cat
                st["status_code"] = code
                st["blocked_filter"] = b_filter
                st["stage"] = stage
                st["reason"] = err_msg
                st["tripped_at"] = time.monotonic()
                return True

            st["consecutive_transient"] = int(st.get("consecutive_transient", 0)) + 1
            if st["consecutive_transient"] >= self._transient_threshold:
                st["open"] = True
                st["category"] = cat or "network"
                st["status_code"] = code
                st["blocked_filter"] = b_filter
                st["stage"] = stage
                st["reason"] = (
                    f"הופסקו ניסיונות חוזרים מול '{provider_id}' עקב {st['consecutive_transient']} כשלים רצופים: {err_msg}"
                )
                st["tripped_at"] = time.monotonic()
                return True
            return False


def _check_cancelled(cancel_event: Any, stage: str) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise AICancelledError(stage=stage)


def _sleep_cancellable(seconds: float, cancel_event: Any, stage: str) -> None:
    if seconds <= 0:
        _check_cancelled(cancel_event, stage)
        return
    if cancel_event is not None:
        triggered = cancel_event.wait(seconds)
        if triggered or cancel_event.is_set():
            raise AICancelledError(stage=stage)
    else:
        time.sleep(seconds)


PROVIDER_CATALOG: Dict[str, Dict[str, Any]] = {
    "gemini": {
        "label": "Google Gemini",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "requires_key": True,
        "supports_discovery": True,
        "help_url": "https://aistudio.google.com/apikey",
    },
    "openai": {
        "label": "OpenAI (ChatGPT)",
        "default_base_url": "https://api.openai.com/v1",
        "requires_key": True,
        "supports_discovery": True,
        "help_url": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "default_base_url": "https://api.anthropic.com/v1",
        "requires_key": True,
        "supports_discovery": True,
        "help_url": "https://console.anthropic.com/settings/keys",
    },
    "openrouter": {
        "label": "OpenRouter (מגוון חברות ומודלים)",
        "default_base_url": "https://openrouter.ai/api/v1",
        "requires_key": True,
        "supports_discovery": True,
        "help_url": "https://openrouter.ai/keys",
    },
    "groq": {
        "label": "Groq (Llama / Mixtral במהירות גבוהה)",
        "default_base_url": "https://api.groq.com/openai/v1",
        "requires_key": True,
        "supports_discovery": True,
        "help_url": "https://console.groq.com/keys",
    },
    "custom": {
        "label": "שרת מותאם / מקומי (Ollama / LM Studio / OpenAI-API)",
        "default_base_url": "http://localhost:11434/v1",
        "requires_key": False,
        "supports_discovery": True,
        "help_url": "https://ollama.com",
    },
}


def get_provider_label(provider_id: str) -> str:
    return PROVIDER_CATALOG.get(provider_id, {}).get("label", provider_id)


def _rank_gemini_model(mid: str) -> tuple:
    low = mid.lower()
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", low)
    if m:
        maj = int(m.group(1))
        min_v = int(m.group(2) or 0)
        tier = 0 if "flash" in low and "lite" not in low else (1 if "pro" in low else (2 if "lite" in low else 3))
        experimental = 1 if any(t in low for t in ("exp", "preview", "001", "latest")) else 0
        return (0, -maj, -min_v, tier, experimental, low)
    return (1, 0, 0, 9, 0, low)


def _classify_timeout_kind(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "connecttimeout" in name or "connect timed out" in msg:
        return "connect"
    if "readtimeout" in name or "read timed out" in msg:
        return "read"
    return "timeout"


def _parse_api_error_detail(resp: Any, secrets: Optional[List[str]] = None) -> str:
    if detect_blocked_response(resp, secrets=secrets) is not None:
        return ""
    detail = ""
    try:
        err_json = resp.json()
        if isinstance(err_json, dict):
            err_val = err_json.get("error")
            if isinstance(err_val, dict):
                detail = str(err_val.get("message") or err_val.get("status") or "")
            else:
                detail = str(err_val or err_json.get("message") or "")
    except Exception:
        raw = (getattr(resp, "text", "") or "")[:240]
        if "<html" in raw.lower() or "<!doctype" in raw.lower():
            return ""
        detail = raw
    return _sanitize_error_text(detail.strip(), secrets=secrets)


def _build_http_error(
    provider_name: str,
    resp: Any,
    stage: str,
    selected_model: str = "",
    secrets: Optional[List[str]] = None,
) -> AICallError:
    code = int(getattr(resp, "status_code", 0) or 0)
    blk = detect_blocked_response(resp, secrets=secrets)
    if blk is not None:
        f_name = blk.get("filter_name") or ""
        f_note = f" — זוהה: {f_name}" if f_name else ""
        code_txt = f"HTTP {code}" if code else "תגובת חסימה"
        msg = (
            f"השירות '{provider_name}' חסום בשלב '{stage}' ({code_txt}{f_note}): "
            "התקבל דף חסימה/HTML במקום תשובת שירות תקינה.\n"
            "אפשרויות התאוששות: בדוק את מדיניות הסינון, הפעל מעבר מורשה לספק ה-AI שנבחר, או החלף ספק והמשך מהשמירה."
        )
        return AICallError(
            msg,
            stage=stage,
            category="blocked",
            retryable=False,
            status_code=code,
            provider_id=provider_name,
            blocked_filter=f_name,
        )

    detail = _parse_api_error_detail(resp, secrets=secrets)
    detail_suffix = f" ({detail})" if detail else ""

    if code in (401, 403):
        msg = (
            f"שגיאת מפתח/הרשאה בשלב '{stage}' מול {provider_name} (HTTP {code}){detail_suffix}.\n"
            "בדוק שמפתח ה-API תקין ובעל הרשאות מתאימות, ולאחר מכן לחץ 'נסה שוב'."
        )
        return AICallError(msg, stage=stage, category="auth", retryable=False, status_code=code)

    if code == 429:
        msg = (
            f"חריגה ממכסת הבקשות/קצב בשלב '{stage}' מול {provider_name} (HTTP 429){detail_suffix}.\n"
            "המתן מספר שניות ונסה שוב, או הקטן עומס בקשות בחשבון."
        )
        return AICallError(msg, stage=stage, category="quota", retryable=True, status_code=code)

    if code == 404:
        model_note = f" המודל '{selected_model}' אינו זמין או שאינו נתמך בנתיב זה." if selected_model else " המשאב המבוקש לא נמצא."
        msg = (
            f"שגיאת זמינות מודל/נתיב בשלב '{stage}' מול {provider_name} (HTTP 404):{model_note}{detail_suffix}\n"
            "פתח 'הגדרות AI ומודלים', לחץ 'רענן מודלים' ובחר מודל עדכני מהרשימה (המודל לא הוחלף אוטומטית)."
        )
        return AICallError(msg, stage=stage, category="model_not_found", retryable=False, status_code=code)

    if code == 400:
        msg = (
            f"בקשה לא תקינה בשלב '{stage}' מול {provider_name} (HTTP 400){detail_suffix}.\n"
            "בדוק את הגדרות המודל או גודל הטקסט ונסה שוב."
        )
        return AICallError(msg, stage=stage, category="validation", retryable=False, status_code=code)

    retryable = code in (408, 500, 502, 503, 504)
    cat = "server" if code >= 500 or code == 408 else "error"
    msg = (
        f"שגיאת שרת זמנית בשלב '{stage}' מול {provider_name} (HTTP {code}){detail_suffix}.\n"
        "זוהי תקלת שרת/עומס (לא בהכרח בעיית מפתח) — ניתן לנסות שוב."
        if retryable
        else f"שגיאה בשלב '{stage}' מול {provider_name} (HTTP {code}){detail_suffix}."
    )
    return AICallError(msg, stage=stage, category=cat, retryable=retryable, status_code=code)


def execute_http_with_retry(
    method: str,
    url: str,
    *,
    provider_name: str,
    stage: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    connect_timeout: float = 10.0,
    read_timeout: float = 35.0,
    total_deadline: float = 105.0,
    max_retries: int = 2,
    backoff_base: float = 1.5,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    selected_model: str = "",
    secrets: Optional[List[str]] = None,
    request_fn: Optional[Callable[..., Any]] = None,
    clock_fn: Optional[Callable[[], float]] = None,
    expect_json: bool = False,
) -> Any:
    """
    מבצע בקשת HTTP עם הפרדת (connect_timeout, read_timeout), תקציב זמן כולל (total_deadline),
    מספר מוגבל של ניסיונות חוזרים לכשלים זמניים בלבד, וביטול מיידי גם באמצע Retry/Backoff.
    מזהה תגובות חסימה (418 / HTML / NetFree) ומונע ניסיונות חוזרים עיוורים.
    """
    req = _require_requests() if request_fn is None else None
    caller = request_fn if request_fn is not None else req.request
    now_fn = clock_fn or time.monotonic

    connect_t = max(1.0, float(connect_timeout))
    read_t = max(1.0, float(read_timeout))
    deadline_ts = now_fn() + max(2.0, float(total_deadline))
    retries_allowed = max(0, int(max_retries))
    transient_http_codes = {408, 429, 500, 502, 503, 504}

    last_error: Optional[AICallError] = None

    for attempt in range(retries_allowed + 1):
        _check_cancelled(cancel_event, stage)
        remaining = deadline_ts - now_fn()
        if remaining <= 0.25:
            break

        eff_connect = min(connect_t, max(0.5, remaining * 0.5))
        eff_read = min(read_t, max(0.5, remaining))

        try:
            kwargs: Dict[str, Any] = {
                "headers": headers or {},
                "params": params,
                "timeout": (eff_connect, eff_read),
            }
            if json_body is not None:
                kwargs["json"] = json_body

            resp = caller(method.upper(), url, **kwargs)
            _check_cancelled(cancel_event, stage)

            if getattr(resp, "ok", False):
                if expect_json:
                    blk_ok = detect_blocked_response(resp, secrets=secrets)
                    if blk_ok is not None:
                        raise _build_http_error(
                            provider_name,
                            resp,
                            stage=stage,
                            selected_model=selected_model,
                            secrets=secrets,
                        )
                return resp

            code = int(getattr(resp, "status_code", 0) or 0)
            err = _build_http_error(
                provider_name,
                resp,
                stage=stage,
                selected_model=selected_model,
                secrets=secrets,
            )
            if err.category == "blocked" or code not in transient_http_codes or attempt >= retries_allowed:
                raise err

            last_error = err
            retry_after_val = 0.0
            try:
                ra_hdr = getattr(resp, "headers", {}).get("Retry-After")
                if ra_hdr:
                    retry_after_val = max(0.0, float(ra_hdr))
            except Exception:
                retry_after_val = 0.0

            delay = max(backoff_base * (2 ** attempt), retry_after_val)
            delay = min(delay, 12.0)

        except AICancelledError:
            raise
        except AICallError:
            raise
        except Exception as exc:
            _check_cancelled(cancel_event, stage)
            req_exc = requests.exceptions if requests is not None else None
            is_timeout = (
                (req_exc is not None and isinstance(exc, req_exc.Timeout))
                or "timeout" in type(exc).__name__.lower()
                or "timed out" in str(exc).lower()
            )
            is_conn = (
                (req_exc is not None and isinstance(exc, req_exc.ConnectionError))
                or "connection" in type(exc).__name__.lower()
            )
            safe_exc = _sanitize_error_text(str(exc), secrets=secrets)

            if is_timeout:
                kind = _classify_timeout_kind(exc)
                if kind == "connect":
                    msg = (
                        f"פג זמן ההתחברות לשרת {provider_name} בשלב '{stage}' (Connect Timeout={eff_connect:.0f}s).\n"
                        "בדוק את חיבור האינטרנט/פרוקסי ונסה שוב (זוהי תקלת תקשורת, לא בעיית מפתח API)."
                    )
                elif kind == "read":
                    msg = (
                        f"השרת {provider_name} לא החזיר תשובה בזמן בשלב '{stage}' (Read Timeout={eff_read:.0f}s).\n"
                        "תקלה זו נובעת מעומס שרת או מקטע ארוך מדי — לא בהכרח מפתח שגוי. "
                        "האצווה חולקה או ניתן לנסות שוב."
                    )
                else:
                    msg = (
                        f"חריגת זמן המתנה מול {provider_name} בשלב '{stage}' ({safe_exc}).\n"
                        "זוהי שגיאת זמן רשת/שרת — לחץ 'נסה שוב'."
                    )
                last_error = AICallError(msg, stage=stage, category="timeout", retryable=True)
            elif is_conn:
                last_error = AICallError(
                    f"שגיאת חיבור רשת אל {provider_name} בשלב '{stage}': {safe_exc}.\nבדוק חיבור אינטרנט ונסה שוב.",
                    stage=stage,
                    category="network",
                    retryable=True,
                )
            else:
                raise AICallError(
                    f"שגיאת תקשורת מול {provider_name} בשלב '{stage}': {safe_exc}",
                    stage=stage,
                    category="network",
                    retryable=False,
                ) from exc

            if attempt >= retries_allowed:
                raise last_error
            delay = min(backoff_base * (2 ** attempt), 10.0)

        # בדיקת תקציב הזמן לפני המתנת Backoff לניסיון הבא
        remaining_after = deadline_ts - now_fn()
        if remaining_after <= 1.0:
            break
        wait_sec = min(delay, max(0.0, remaining_after - 1.0))
        if progress_cb:
            progress_cb(
                f"שלב '{stage}': השרת התעכב, מנסה שוב ({attempt + 2}/{retries_allowed + 1}) בעוד {wait_sec:.1f} שנ'..."
            )
        _sleep_cancellable(wait_sec, cancel_event, stage)

    if last_error is not None:
        raise AICallError(
            f"{last_error}\n(הסתיים תקציב הזמן הכולל: {total_deadline:.0f} שנ' בשלב '{stage}').",
            stage=stage,
            category=last_error.category,
            retryable=True,
            status_code=last_error.status_code,
        )
    raise AICallError(
        f"חריגה מתקציב הזמן הכולל ({total_deadline:.0f} שנ') בשלב '{stage}' מול {provider_name}. נסה שוב.",
        stage=stage,
        category="timeout",
        retryable=True,
    )


def _filter_models_for_capability(
    provider_id: str,
    raw_models: List[Dict[str, Any]],
    capability: str = "text",
) -> Tuple[List[str], int]:
    """
    מסנן מודלים שהוחזרו מה-API הרשמי של הספק לפי היכולת הנדרשת (ברירת מחדל: text/chat).
    לעולם אינו ממציא מודלים שאינם בתשובת הספק.
    """
    accepted: List[str] = []
    filtered_out = 0

    for item in raw_models:
        if not isinstance(item, dict):
            continue

        if provider_id == "gemini":
            methods = item.get("supportedGenerationMethods") or []
            mid = str(item.get("name") or "").strip()
            if mid.startswith("models/"):
                mid = mid[len("models/"):]
            if not mid:
                continue
            low = mid.lower()
            if capability == "text":
                bad_tokens = (
                    "embedding",
                    "text-embedding",
                    "aqa",
                    "imagen",
                    "veo",
                    "tts",
                    "-audio",
                    "native-audio",
                    "robotics",
                    "computer-use",
                    "bisheng",
                    "medlm",
                )
                if "generateContent" not in methods or any(tok in low for tok in bad_tokens):
                    filtered_out += 1
                    continue
            accepted.append(mid)

        elif provider_id == "anthropic":
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            if capability == "text" and any(tok in mid.lower() for tok in ("embedding", "audio", "tts", "whisper")):
                filtered_out += 1
                continue
            accepted.append(mid)

        elif provider_id == "openai":
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            low = mid.lower()
            if capability == "text":
                is_chat = any(pref in low for pref in ("gpt-", "o1", "o3", "o4", "chatgpt"))
                is_non_text = any(
                    bad in low
                    for bad in (
                        "embedding",
                        "audio",
                        "realtime",
                        "transcribe",
                        "tts",
                        "image",
                        "dall-e",
                        "whisper",
                        "moderation",
                        "sora",
                        "search-api",
                        "instruct-0914",
                        "codex-mini",
                    )
                )
                if not is_chat or is_non_text:
                    filtered_out += 1
                    continue
            accepted.append(mid)

        elif provider_id == "openrouter":
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            low = mid.lower()
            arch = item.get("architecture") or {}
            out_mods = arch.get("output_modalities") if isinstance(arch, dict) else None
            if capability == "text":
                if isinstance(out_mods, list) and out_mods and "text" not in out_mods:
                    filtered_out += 1
                    continue
                if any(bad in low for bad in ("embedding", "whisper", "tts", "dall-e", "flux", "stable-diffusion")):
                    filtered_out += 1
                    continue
            accepted.append(mid)

        elif provider_id == "groq":
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            low = mid.lower()
            if item.get("active") is False:
                filtered_out += 1
                continue
            if capability == "text" and any(
                bad in low for bad in ("whisper", "playai-tts", "orpheus", "prompt-guard", "safeguard", "embedding")
            ):
                filtered_out += 1
                continue
            accepted.append(mid)

        else:
            # custom / Ollama / LM Studio / OpenAI-compatible
            mid = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
            if not mid:
                continue
            low = mid.lower()
            if capability == "text" and any(bad in low for bad in ("embed", "nomic-embed", "whisper", "tts")):
                filtered_out += 1
                continue
            accepted.append(mid)

    unique = list(dict.fromkeys(accepted))
    if provider_id == "gemini":
        unique = sorted(unique, key=_rank_gemini_model)
    elif provider_id == "anthropic":
        unique = sorted(unique, reverse=True)
    else:
        unique = sorted(unique)
    return unique, filtered_out


def discover_models_detailed(
    provider_id: str,
    api_key: str,
    base_url: str = "",
    *,
    capability: str = "text",
    connect_timeout: float = 8.0,
    read_timeout: float = 20.0,
    total_deadline: float = 40.0,
    max_retries: int = 1,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    request_fn: Optional[Callable[..., Any]] = None,
    clock_fn: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """
    מבצע גילוי מודלים חי מול ה-endpoint הרשמי של הספק כולל Pagination וסינון לפי יכולת.
    לעולם אינו מחזיר רשימת מודלים מומצאת כשיש כשל או תשובה ריקה.
    """
    stage = "גילוי מודלים"
    meta = PROVIDER_CATALOG.get(provider_id)
    if not meta:
        raise AICallError(f"ספק לא מוכר: {provider_id}", stage=stage, category="validation")

    key = (api_key or "").strip()
    if meta.get("requires_key", True) and not key:
        raise AICallError(
            "יש להזין מפתח API תקין לפני שליפת המודלים הזמינים מהחברה.",
            stage=stage,
            category="auth",
        )

    url_base = (base_url or meta["default_base_url"]).strip().rstrip("/")
    secrets = [key] if key else []
    raw_items: List[Dict[str, Any]] = []
    pages_fetched = 0
    max_pages = 20

    if provider_id == "gemini":
        page_token: Optional[str] = None
        seen_tokens = set()
        while pages_fetched < max_pages:
            _check_cancelled(cancel_event, stage)
            params: Dict[str, Any] = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            resp = execute_http_with_retry(
                "GET",
                f"{url_base}/models",
                provider_name=meta["label"],
                stage=stage,
                headers={"x-goog-api-key": key, "Accept": "application/json"},
                params=params,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                total_deadline=total_deadline,
                max_retries=max_retries,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                secrets=secrets,
                request_fn=request_fn,
                clock_fn=clock_fn,
                expect_json=True,
            )
            data = resp.json() if hasattr(resp, "json") else {}
            pages_fetched += 1
            batch = data.get("models") or []
            if isinstance(batch, list):
                raw_items.extend([m for m in batch if isinstance(m, dict)])
            next_tok = (data.get("nextPageToken") or "").strip()
            if not next_tok or next_tok in seen_tokens:
                break
            seen_tokens.add(next_tok)
            page_token = next_tok

    elif provider_id == "anthropic":
        after_id: Optional[str] = None
        seen_cursors = set()
        while pages_fetched < max_pages:
            _check_cancelled(cancel_event, stage)
            params = {"limit": 100}
            if after_id:
                params["after_id"] = after_id
            resp = execute_http_with_retry(
                "GET",
                f"{url_base}/models",
                provider_name=meta["label"],
                stage=stage,
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "Accept": "application/json",
                },
                params=params,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                total_deadline=total_deadline,
                max_retries=max_retries,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                secrets=secrets,
                request_fn=request_fn,
                clock_fn=clock_fn,
                expect_json=True,
            )
            data = resp.json() if hasattr(resp, "json") else {}
            pages_fetched += 1
            batch = data.get("data") or []
            if isinstance(batch, list):
                raw_items.extend([m for m in batch if isinstance(m, dict)])
            has_more = bool(data.get("has_more"))
            last_id = (data.get("last_id") or "").strip()
            if not has_more or not last_id or last_id in seen_cursors:
                break
            seen_cursors.add(last_id)
            after_id = last_id

    else:
        # OpenAI / OpenRouter / Groq / Custom OpenAI-compatible (+ Ollama /api/tags fallback)
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        after_cursor: Optional[str] = None
        seen_cursors = set()
        ollama_checked = False

        while pages_fetched < max_pages:
            _check_cancelled(cancel_event, stage)
            params = {"after": after_cursor} if after_cursor else None
            try:
                resp = execute_http_with_retry(
                    "GET",
                    f"{url_base}/models",
                    provider_name=meta["label"],
                    stage=stage,
                    headers=headers,
                    params=params,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    total_deadline=total_deadline,
                    max_retries=max_retries,
                    cancel_event=cancel_event,
                    progress_cb=progress_cb,
                    secrets=secrets,
                    request_fn=request_fn,
                    clock_fn=clock_fn,
                    expect_json=True,
                )
            except AICallError as err:
                if provider_id == "custom" and err.status_code == 404 and not ollama_checked and pages_fetched == 0:
                    ollama_checked = True
                    root_url = url_base[:-3] if url_base.endswith("/v1") else url_base
                    ollama_resp = execute_http_with_retry(
                        "GET",
                        f"{root_url}/api/tags",
                        provider_name=meta["label"],
                        stage=stage,
                        headers=headers,
                        connect_timeout=connect_timeout,
                        read_timeout=read_timeout,
                        total_deadline=total_deadline,
                        max_retries=max_retries,
                        cancel_event=cancel_event,
                        progress_cb=progress_cb,
                        secrets=secrets,
                        request_fn=request_fn,
                        clock_fn=clock_fn,
                        expect_json=True,
                    )
                    odata = ollama_resp.json() if hasattr(ollama_resp, "json") else {}
                    pages_fetched += 1
                    obatch = odata.get("models") or []
                    if isinstance(obatch, list):
                        raw_items.extend([m for m in obatch if isinstance(m, dict)])
                    break
                raise

            data = resp.json() if hasattr(resp, "json") else {}
            pages_fetched += 1
            batch = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            if isinstance(batch, list):
                raw_items.extend([m for m in batch if isinstance(m, dict)])

            has_more = isinstance(data, dict) and bool(data.get("has_more"))
            last_id = (data.get("last_id") or "").strip() if isinstance(data, dict) else ""
            if not last_id and isinstance(data, dict) and has_more and batch:
                last_id = str((batch[-1] or {}).get("id") or "").strip()
            if not has_more or not last_id or last_id in seen_cursors:
                break
            seen_cursors.add(last_id)
            after_cursor = last_id

    models, filtered_out = _filter_models_for_capability(provider_id, raw_items, capability=capability)
    if not models:
        raise AICallError(
            f"הספק {meta['label']} לא החזיר מודלים תואמי טקסט/שיחה זמינים עבור מפתח זה "
            f"(סוננו {filtered_out} מודלים לא-טקסטואליים).",
            stage=stage,
            category="empty",
            retryable=True,
        )

    return {
        "provider_id": provider_id,
        "models": models,
        "pages_fetched": pages_fetched,
        "filtered_out": filtered_out,
        "capability": capability,
    }


def discover_models(
    provider_id: str,
    api_key: str,
    base_url: str = "",
    timeout: Any = (8.0, 20.0),
    capability: str = "text",
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    request_fn: Optional[Callable[..., Any]] = None,
    clock_fn: Optional[Callable[[], float]] = None,
) -> List[str]:
    if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
        conn_t, read_t = float(timeout[0]), float(timeout[1])
    else:
        conn_t = min(8.0, float(timeout or 20.0))
        read_t = float(timeout or 20.0)

    res = discover_models_detailed(
        provider_id=provider_id,
        api_key=api_key,
        base_url=base_url,
        capability=capability,
        connect_timeout=conn_t,
        read_timeout=read_t,
        total_deadline=max(20.0, read_t * 2.0),
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        request_fn=request_fn,
        clock_fn=clock_fn,
    )
    return res["models"]


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
    timeout: Any = None,
    connect_timeout: float = 10.0,
    read_timeout: float = 40.0,
    total_deadline: float = 120.0,
    max_retries: int = 2,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    stage: str = "קריאת AI",
    request_fn: Optional[Callable[..., Any]] = None,
    clock_fn: Optional[Callable[[], float]] = None,
) -> str:
    """
    קריאה אחידה ומבוקרת לספק AI עם:
    - connect/read timeout מופרדים + deadline כולל + retry/backoff רק לכשלים זמניים
    - ביטול מיידי גם בזמן המתנה
    - ללא החלפת מודל שקטה!
    """
    _check_cancelled(cancel_event, stage)
    meta = PROVIDER_CATALOG.get(provider_id)
    if not meta:
        raise AICallError(f"ספק AI לא מוכר: {provider_id}", stage=stage, category="validation")

    key = (api_key or "").strip()
    if meta.get("requires_key", True) and not key:
        raise AICallError(
            f"לא הוגדר מפתח API עבור {meta['label']} בשלב '{stage}'. פתח 'הגדרות AI ומודלים' והזן מפתח.",
            stage=stage,
            category="auth",
        )

    selected_model = (model or "").strip()
    if not selected_model:
        raise AICallError(
            f"לא נבחר מודל AI פעיל עבור {meta['label']} בשלב '{stage}'. פתח 'הגדרות AI ומודלים' ובחר מודל מהרשימה.",
            stage=stage,
            category="validation",
        )

    if timeout is not None:
        if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
            connect_timeout = float(timeout[0])
            read_timeout = float(timeout[1])
        else:
            read_timeout = float(timeout)
            connect_timeout = min(connect_timeout, read_timeout)

    url_base = (base_url or meta["default_base_url"]).strip().rstrip("/")
    secrets = [key] if key else []

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

        resp = execute_http_with_retry(
            "POST",
            f"{url_base}/models/{selected_model}:generateContent",
            provider_name="Google Gemini",
            stage=stage,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json_body=payload,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            total_deadline=total_deadline,
            max_retries=max_retries,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            selected_model=selected_model,
            secrets=secrets,
            request_fn=request_fn,
            clock_fn=clock_fn,
            expect_json=True,
        )
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise AICallError(
                f"Gemini ({selected_model}) לא החזיר תוכן בשלב '{stage}' (ייתכן שנחסם ע\"י מסנן בטיחות). נסה שוב או בדוק את הטקסט.",
                stage=stage,
                category="invalid_response",
            )
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
        resp = execute_http_with_retry(
            "POST",
            f"{url_base}/messages",
            provider_name="Anthropic Claude",
            stage=stage,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json_body=payload,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            total_deadline=total_deadline,
            max_retries=max_retries,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            selected_model=selected_model,
            secrets=secrets,
            request_fn=request_fn,
            clock_fn=clock_fn,
            expect_json=True,
        )
        data = resp.json()
        blocks = data.get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    else:
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
        if json_mode and provider_id in ("openai", "groq"):
            payload["response_format"] = {"type": "json_object"}

        resp = execute_http_with_retry(
            "POST",
            f"{url_base}/chat/completions",
            provider_name=meta["label"],
            stage=stage,
            headers=headers,
            json_body=payload,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            total_deadline=total_deadline,
            max_retries=max_retries,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            selected_model=selected_model,
            secrets=secrets,
            request_fn=request_fn,
            clock_fn=clock_fn,
            expect_json=True,
        )
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise AICallError(
                f"{meta['label']} ({selected_model}) לא החזיר תשובה בשלב '{stage}'. נסה שוב.",
                stage=stage,
                category="invalid_response",
            )
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
