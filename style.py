# -*- coding: utf-8 -*-
"""
עיצוב ערכת נושא כהה ומודרנית (QSS) עבור PorMyCodex.
מותאם לכיווניות עברית (RTL) עם גווני Slate ו-Amber, כולל חלונות הגדרות, תרגום ותסריט.
"""

def get_stylesheet() -> str:
    return """
    /* Global Window & Font */
    QWidget {
        background-color: #0f172a;
        color: #f1f5f9;
        font-family: 'Segoe UI', 'Arial', sans-serif;
        font-size: 13px;
        selection-background-color: #f59e0b;
        selection-color: #0f172a;
    }

    QMainWindow, QDialog {
        background-color: #0b1120;
    }

    /* ToolBar */
    QToolBar {
        background-color: #1e293b;
        border-bottom: 1px solid #334155;
        padding: 6px;
        spacing: 6px;
    }

    QToolButton {
        background-color: #334155;
        color: #f8fafc;
        border: 1px solid #475569;
        border-radius: 8px;
        padding: 6px 11px;
        font-weight: 600;
        font-size: 12px;
    }

    QToolButton:hover {
        background-color: #f59e0b;
        color: #0f172a;
        border-color: #d97706;
    }

    QToolButton:pressed {
        background-color: #b45309;
        color: #ffffff;
    }

    /* Buttons */
    QPushButton {
        background-color: #1e293b;
        color: #f1f5f9;
        border: 1px solid #334155;
        border-radius: 8px;
        padding: 7px 14px;
        font-size: 13px;
        font-weight: 600;
    }

    QPushButton:hover {
        background-color: #334155;
        border-color: #475569;
    }

    QPushButton:pressed {
        background-color: #0f172a;
    }

    QPushButton#primaryBtn {
        background-color: #f59e0b;
        color: #0f172a;
        border: 1px solid #d97706;
        font-weight: bold;
    }

    QPushButton#primaryBtn:hover {
        background-color: #fbbf24;
        border-color: #f59e0b;
    }

    QPushButton#accentBtn {
        background-color: #4f46e5;
        color: #ffffff;
        border: 1px solid #6366f1;
        font-weight: bold;
    }

    QPushButton#accentBtn:hover {
        background-color: #6366f1;
    }

    QPushButton#settingsBadgeBtn {
        background-color: #1e293b;
        color: #38bdf8;
        border: 1px solid #38bdf8;
        border-radius: 8px;
        padding: 5px 12px;
        font-size: 12px;
        font-weight: bold;
    }

    QPushButton#settingsBadgeBtn:hover {
        background-color: #0284c7;
        color: #ffffff;
    }

    QFrame#playerBar {
        background-color: #1e293b;
        border-top: 1px solid #334155;
    }

    QPushButton#playBtn {
        background-color: #f59e0b;
        color: #0f172a;
        border: 2px solid #fbbf24;
        border-radius: 25px;
        padding: 0px;
        font-size: 20px;
        font-weight: bold;
        min-width: 50px;
        min-height: 50px;
        max-width: 50px;
        max-height: 50px;
    }

    QPushButton#playBtn:hover {
        background-color: #fbbf24;
    }

    /* Inputs & GroupBoxes */
    QLineEdit, QPlainTextEdit {
        background-color: #020617;
        color: #f1f5f9;
        border: 1px solid #334155;
        border-radius: 8px;
        padding: 7px 10px;
    }

    QLineEdit:focus, QPlainTextEdit:focus {
        border-color: #f59e0b;
    }

    QGroupBox {
        border: 1px solid #334155;
        border-radius: 10px;
        margin-top: 12px;
        padding: 14px 10px 10px 10px;
        font-weight: bold;
        color: #fbbf24;
    }

    QGroupBox::title {
        subcontrol-origin: margin;
        subcontrol-position: top right;
        padding: 0 8px;
        background-color: #0b1120;
    }

    QTabWidget::pane {
        border: 1px solid #334155;
        border-radius: 8px;
        background-color: #0f172a;
    }

    QTabBar::tab {
        background-color: #1e293b;
        color: #94a3b8;
        padding: 8px 16px;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        margin-left: 4px;
        font-weight: 600;
    }

    QTabBar::tab:selected {
        background-color: #f59e0b;
        color: #0f172a;
    }

    /* Lists and Trees */
    QListWidget {
        background-color: #1e293b;
        border: 1px solid #334155;
        border-radius: 10px;
        padding: 6px;
        outline: none;
    }

    QListWidget::item {
        padding: 8px 10px;
        border-radius: 6px;
        margin-bottom: 2px;
    }

    QListWidget::item:hover {
        background-color: #334155;
    }

    QListWidget::item:selected {
        background-color: rgba(245, 158, 11, 0.25);
        border: 1px solid #f59e0b;
        color: #fef3c7;
        font-weight: bold;
    }

    /* Text & Reading Area */
    QTextEdit, QTextBrowser {
        background-color: #020617;
        color: #e2e8f0;
        border: 1px solid #1e293b;
        border-radius: 12px;
        padding: 18px;
        line-height: 1.8;
    }

    /* Combo Box */
    QComboBox {
        background-color: #1e293b;
        border: 1px solid #334155;
        border-radius: 6px;
        padding: 6px 10px;
        color: #f1f5f9;
        font-weight: 500;
        min-width: 140px;
    }

    QComboBox::drop-down {
        border: none;
        width: 24px;
    }

    QComboBox QAbstractItemView {
        background-color: #1e293b;
        border: 1px solid #475569;
        selection-background-color: #f59e0b;
        selection-color: #0f172a;
        padding: 4px;
    }

    /* Sliders */
    QSlider::groove:horizontal {
        height: 6px;
        background: #334155;
        border-radius: 3px;
    }

    QSlider::sub-page:horizontal {
        background: #f59e0b;
        border-radius: 3px;
    }

    QSlider::handle:horizontal {
        background: #ffffff;
        border: 2px solid #f59e0b;
        width: 16px;
        margin-top: -5px;
        margin-bottom: -5px;
        border-radius: 8px;
    }

    QSlider::handle:horizontal:hover {
        background: #fef3c7;
    }

    /* Progress Bar */
    QProgressBar {
        background-color: #1e293b;
        border: 1px solid #334155;
        border-radius: 5px;
        text-align: center;
        color: #cbd5e1;
        font-size: 11px;
    }

    QProgressBar::chunk {
        background-color: #f59e0b;
        border-radius: 4px;
    }

    /* Labels & Headings */
    QLabel {
        color: #cbd5e1;
    }

    QLabel#titleLabel {
        font-size: 16px;
        font-weight: bold;
        color: #f8fafc;
    }

    QLabel#sectionLabel {
        font-size: 12px;
        font-weight: 700;
        color: #94a3b8;
        text-transform: uppercase;
        margin-top: 6px;
        margin-bottom: 2px;
    }

    /* Status Bar */
    QStatusBar {
        background-color: #0b1120;
        color: #94a3b8;
        border-top: 1px solid #1e293b;
        font-size: 11px;
        padding: 4px;
    }

    /* Scrollbars */
    QScrollBar:vertical {
        background: #0f172a;
        width: 10px;
        margin: 0px;
        border-radius: 5px;
    }

    QScrollBar::handle:vertical {
        background: #334155;
        min-height: 25px;
        border-radius: 5px;
    }

    QScrollBar::handle:vertical:hover {
        background: #475569;
    }

    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0px;
    }
    """
