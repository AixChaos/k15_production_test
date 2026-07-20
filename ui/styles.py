"""界面样式。"""

APP_QSS = """
QMainWindow, QWidget {
    background: #1a1d23;
    color: #e8eaed;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #2d333b;
    border-radius: 6px;
    top: -1px;
}
QTabBar::tab {
    background: #252a33;
    color: #9aa0a6;
    padding: 8px 18px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background: #2d333b;
    color: #e8eaed;
}
QLineEdit, QSpinBox, QComboBox {
    background: #0f1115;
    border: 1px solid #3d4450;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #3d5a80;
}
QPushButton {
    background: #3d5a80;
    color: white;
    border: none;
    border-radius: 5px;
    padding: 8px 14px;
    font-weight: 600;
}
QPushButton:hover { background: #4a6d99; }
QPushButton:pressed { background: #2f4766; }
QPushButton:disabled { background: #3a3f48; color: #777; }
QPushButton#danger {
    background: #8b3a3a;
}
QPushButton#danger:hover { background: #a44848; }
QPushButton#success {
    background: #2d6a4f;
}
QPushButton#success:hover { background: #40916c; }
QPushButton#ghost {
    background: #2d333b;
    border: 1px solid #3d4450;
}
QListWidget {
    background: #0f1115;
    border: 1px solid #2d333b;
    border-radius: 6px;
    outline: none;
}
QListWidget::item {
    padding: 10px 12px;
    border-bottom: 1px solid #252a33;
}
QListWidget::item:selected {
    background: #2d3a4d;
    color: #fff;
}
QTextEdit {
    background: #0b0d10;
    border: 1px solid #2d333b;
    border-radius: 6px;
    font-family: "JetBrains Mono", "Fira Code", "Ubuntu Mono", monospace;
    font-size: 12px;
    color: #c5c8c6;
}
QLabel#title {
    font-size: 18px;
    font-weight: 700;
    color: #f0f3f6;
}
QLabel#hint {
    color: #9aa0a6;
}
QLabel#statusConnected { color: #52b788; font-weight: 600; }
QLabel#statusDisconnected { color: #e76f51; font-weight: 600; }
QGroupBox {
    border: 1px solid #2d333b;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 12px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #9aa0a6;
}
QProgressBar {
    border: 1px solid #2d333b;
    border-radius: 4px;
    text-align: center;
    background: #0f1115;
}
QProgressBar::chunk {
    background: #3d5a80;
    border-radius: 3px;
}
"""

STATUS_COLORS = {
    "pending": "#9aa0a6",
    "running": "#f4a261",
    "pass": "#52b788",
    "fail": "#e76f51",
    "skip": "#6c757d",
    "manual": "#e9c46a",
}
