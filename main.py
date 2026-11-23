# -*- coding: utf-8 -*-
"""
נקודת הכניסה הראשית לתוכנת PorMyCodex (מקריא ספרים חכם בעברית).
הפעלה ישירה: python main.py
או הידור ל-EXE עצמאי באמצעות build_exe.bat

כולל הגנת קריסות גלובלית (sys.excepthook + threading.excepthook) המונעת
סגירה פתאומית של PyQt6 (qFatal) בעת שגיאה לא צפויה ב-slot ומתעדת ב-crash.log
ללא חשיפת מפתחות API.
"""

from datetime import datetime, timezone
import os
import re
import sys
import threading
import time
import traceback
from typing import Optional

from library_store import get_user_data_dir

_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z\-_]{12,}"),
    re.compile(r"sk-[0-9A-Za-z\-_]{12,}"),
    re.compile(r"gsk_[0-9A-Za-z\-_]{12,}"),
    re.compile(r"xai-[0-9A-Za-z\-_]{12,}"),
    re.compile(r"(Bearer\s+)[0-9A-Za-z\.\-_]{12,}", re.IGNORECASE),
)
_LAST_DIALOG_TS = 0.0
_HOOK_LOCK = threading.Lock()


def sanitize_crash_text(text: str) -> str:
    """מסיר מפתחות API ואסימונים רגישים לפני כתיבה ללוג קריסות או הצגה למשתמש."""
    out = str(text or "")
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            out = pat.sub(r"\1[REDACTED]", out)
        else:
            out = pat.sub("[REDACTED]", out)
    return out


def get_crash_log_path() -> str:
    return os.path.join(get_user_data_dir(), "crash.log")


def log_unhandled_exception(
    exc_type,
    exc_value,
    exc_tb,
    *,
    context: str = "UI",
) -> str:
    """כותב לוג שגיאה נקי ל-crash.log ומחזיר הודעה מסוננת ובטוחה."""
    raw_tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    clean_tb = sanitize_crash_text(raw_tb)
    clean_msg = sanitize_crash_text(f"{getattr(exc_type, '__name__', 'Error')}: {exc_value}")
    try:
        log_path = get_crash_log_path()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== [{stamp}] Unhandled {context} Exception ===\n{clean_tb}\n")
    except Exception:
        pass
    return clean_msg


def _show_safe_error_dialog(app, clean_msg: str) -> None:
    global _LAST_DIALOG_TS
    if app is None:
        return
    with _HOOK_LOCK:
        now = time.monotonic()
        if now - _LAST_DIALOG_TS < 2.0:
            return
        _LAST_DIALOG_TS = now
    try:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QMessageBox

        def _popup():
            try:
                log_path = get_crash_log_path()
                QMessageBox.warning(
                    None,
                    "שגיאה פנימית נלכדה — התוכנה ממשיכה לפעול",
                    "אירעה שגיאה לא צפויה באחת הפעולות, אך התוכנה נבלמה מקריסה והתקדמותך נשמרה:\n\n"
                    f"{clean_msg[:400]}\n\n"
                    f"פירוט טכני נשמר בקובץ:\n{log_path}",
                )
            except Exception:
                pass

        QTimer.singleShot(0, _popup)
    except Exception:
        pass


def install_global_exception_hooks(app=None, *, show_dialog: bool = True) -> None:
    """מתקין לכידת חריגות גלובלית ל-PyQt6 ולחוטי רקע כדי למנוע קריסת qFatal שקטה."""
    def _sys_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        clean_msg = log_unhandled_exception(exc_type, exc_value, exc_tb, context="UI/Main")
        if show_dialog and app is not None:
            _show_safe_error_dialog(app, clean_msg)

    def _thread_hook(args):
        if args.exc_type and issubclass(args.exc_type, SystemExit):
            return
        log_unhandled_exception(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            context=f"Thread:{getattr(args.thread, 'name', 'worker')}",
        )

    sys.excepthook = _sys_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook


def main():
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from main_window import MainWindow
    from style import get_stylesheet

    # הגדרות תמיכה במסכי High DPI
    if hasattr(Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("PorMyCodex")
    app.setOrganizationName("ShipIt")

    # התקנת הגנת קריסות גלובלית לפני בניית החלון וה-slots
    install_global_exception_hooks(app, show_dialog=True)

    # קביעת כיווניות עברית מלאה (RTL) ברמת האפליקציה כולה
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

    # טעינת ערכת נושא כהה ומעוצבת
    app.setStyleSheet(get_stylesheet())

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
