"""双臂末端配置选择对话框。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class EndEffectorChoiceDialog(QDialog):
    """返回 installed / not_installed；取消则返回 None。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("双臂末端配置")
        self.setModal(True)
        self.setFixedWidth(420)
        self._choice: Optional[str] = None
        self.setStyleSheet(
            """
            QDialog { background: #1a1d23; color: #e8eaed; }
            QLabel#title { font-size: 16px; font-weight: 700; color: #f0f3f6; }
            QLabel#hint { color: #9aa0a6; }
            QPushButton {
                background: #3d5a80; color: white; border: none;
                border-radius: 5px; padding: 12px 16px; font-weight: 600;
            }
            QPushButton:hover { background: #4a6d99; }
            QPushButton#ghost {
                background: #2d333b; border: 1px solid #3d4450;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("请选择末端设备状态")
        title.setObjectName("title")
        layout.addWidget(title)

        hint = QLabel(
            "将据此检查并更新 hw_params.yaml 中左右夹爪的 plugin 配置。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.btn_installed = QPushButton("已装末端设备")
        self.btn_not_installed = QPushButton("未装末端设备")
        self.btn_installed.clicked.connect(self._on_installed)
        self.btn_not_installed.clicked.connect(self._on_not_installed)
        layout.addWidget(self.btn_installed)
        layout.addWidget(self.btn_not_installed)

        row = QHBoxLayout()
        row.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("ghost")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        layout.addLayout(row)

    def _on_installed(self) -> None:
        self._choice = "installed"
        self.accept()

    def _on_not_installed(self) -> None:
        self._choice = "not_installed"
        self.accept()

    def selected_mode(self) -> Optional[str]:
        return self._choice
