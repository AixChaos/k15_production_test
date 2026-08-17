"""应用字体：冬青黑体简体中文 W6（OTF）。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

from core.config import ROOT

# 与 fc-query / QFontDatabase 解析到的 family 一致
APP_FONT_FAMILY = "冬青黑體簡體中文"
APP_FONT_FILE = ROOT / "assets" / "fonts" / "HiraginoSansGB-W6.otf"


def load_app_font() -> str:
    """加载内置 OTF，返回实际 family 名；失败则回退系统默认。"""
    if not APP_FONT_FILE.is_file():
        return APP_FONT_FAMILY
    font_id = QFontDatabase.addApplicationFont(str(APP_FONT_FILE))
    if font_id < 0:
        return APP_FONT_FAMILY
    families = QFontDatabase.applicationFontFamilies(font_id)
    if families:
        return families[0]
    return APP_FONT_FAMILY


def apply_app_font(app, family: str | None = None, point_size: int = 11) -> QFont:
    """设为 QApplication 全局字体。"""
    fam = family or APP_FONT_FAMILY
    font = QFont(fam)
    font.setPointSize(point_size)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)
    return font
