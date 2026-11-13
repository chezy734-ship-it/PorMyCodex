# -*- coding: utf-8 -*-
"""
נקודת הכניסה הראשית לתוכנת PorMyCodex (מקריא ספרים חכם בעברית).
הפעלה ישירה: python main.py
או הידור ל-EXE עצמאי באמצעות build_exe.bat
"""

import sys
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from main_window import MainWindow
from style import get_stylesheet


def main():
    # הגדרות תמיכה במסכי High DPI
    if hasattr(Qt.ApplicationAttribute, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    if hasattr(Qt.ApplicationAttribute, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("PorMyCodex")
    app.setOrganizationName("ShipIt")

    # קביעת כיווניות עברית מלאה (RTL) ברמת האפליקציה כולה
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

    # טעינת ערכת נושא כהה ומעוצבת
    app.setStyleSheet(get_stylesheet())

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
