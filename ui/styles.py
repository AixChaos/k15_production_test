"""界面样式。"""

from ui.fonts import APP_FONT_FAMILY

# 全局 / 日志统一使用冬青黑体
_FONT = APP_FONT_FAMILY

APP_QSS = f"""
QMainWindow, QWidget {{
    background: #1a1d23;
    color: #e8eaed;
    font-family: "{_FONT}";
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid #2d333b;
    border-radius: 6px;
    top: -1px;
}}
QTabBar::tab {{
    background: #252a33;
    color: #9aa0a6;
    padding: 10px 22px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-family: "{_FONT}";
    font-weight: 600;
}}
QTabBar::tab:selected {{
    background: #2d333b;
    color: #e8eaed;
}}
QTabWidget#mainTabs::pane {{
    border: 1px solid #2d333b;
    border-radius: 6px;
    top: -1px;
    padding: 4px;
}}
QLineEdit, QSpinBox, QComboBox, QComboBox QAbstractItemView {{
    background: #0f1115;
    border: 1px solid #3d4450;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #3d5a80;
    font-family: "{_FONT}";
}}
QPushButton {{
    background: #3d5a80;
    color: white;
    border: none;
    border-radius: 5px;
    padding: 8px 14px;
    font-family: "{_FONT}";
    font-weight: 600;
}}
QPushButton:hover {{ background: #4a6d99; }}
QPushButton:pressed {{ background: #2f4766; }}
QPushButton:disabled {{ background: #3a3f48; color: #777; }}
QPushButton#danger {{
    background: #8b3a3a;
}}
QPushButton#danger:hover {{ background: #a44848; }}
QPushButton#success {{
    background: #2d6a4f;
}}
QPushButton#success:hover {{ background: #40916c; }}
QPushButton#ghost {{
    background: #2d333b;
    border: 1px solid #3d4450;
}}
QListWidget {{
    background: #0f1115;
    border: 1px solid #2d333b;
    border-radius: 6px;
    outline: none;
    font-family: "{_FONT}";
}}
QListWidget::item {{
    padding: 10px 12px;
    border-bottom: 1px solid #252a33;
}}
QListWidget::item:selected {{
    background: #2d3a4d;
    color: #fff;
}}
QTextEdit {{
    background: #0b0d10;
    border: 1px solid #2d333b;
    border-radius: 6px;
    font-family: "{_FONT}";
    font-size: 12px;
    color: #c5c8c6;
}}
QLabel {{
    font-family: "{_FONT}";
}}
QLabel#title {{
    font-size: 18px;
    font-weight: 700;
    color: #f0f3f6;
}}
QLabel#hint {{
    color: #9aa0a6;
}}
QLabel#statusConnected {{ color: #52b788; font-weight: 600; }}
QLabel#statusDisconnected {{ color: #e76f51; font-weight: 600; }}
QGroupBox {{
    border: 1px solid #2d333b;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 12px;
    font-family: "{_FONT}";
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #9aa0a6;
}}
QProgressBar {{
    border: 1px solid #2d333b;
    border-radius: 4px;
    text-align: center;
    background: #0f1115;
    font-family: "{_FONT}";
}}
QProgressBar::chunk {{
    background: #3d5a80;
    border-radius: 3px;
}}
QMessageBox, QDialog {{
    font-family: "{_FONT}";
}}
"""

STATUS_COLORS = {
    "pending": "#9aa0a6",
    "running": "#f4a261",
    "pass": "#52b788",
    "fail": "#e76f51",
    "skip": "#6c757d",
    "manual": "#e9c46a",
}
