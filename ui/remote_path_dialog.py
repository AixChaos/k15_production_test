"""域控远程路径选择对话框。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core.ssh_client import SshClient


class RemotePathDialog(QDialog):
    def __init__(self, ssh: SshClient, start_path: str = "/home/nvidia", parent=None) -> None:
        super().__init__(parent)
        self.ssh = ssh
        self.setWindowTitle("选择域控目标路径")
        self.resize(520, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("当前目录（双击进入子目录；也可直接编辑路径）"))

        nav = QHBoxLayout()
        self.path_edit = QLineEdit(start_path)
        self.btn_go = QPushButton("进入")
        self.btn_up = QPushButton("上级")
        self.btn_up.setObjectName("ghost")
        nav.addWidget(self.path_edit, 1)
        nav.addWidget(self.btn_go)
        nav.addWidget(self.btn_up)
        layout.addLayout(nav)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)

        self.file_name_edit = QLineEdit()
        self.file_name_edit.setPlaceholderText("可选：目标文件名（默认用本地文件名）")
        layout.addWidget(QLabel("目标文件名（可空）"))
        layout.addWidget(self.file_name_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.btn_go.clicked.connect(self.refresh)
        self.btn_up.clicked.connect(self.go_up)
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.refresh()

    def current_dir(self) -> str:
        p = self.path_edit.text().strip() or "/"
        return p if p.startswith("/") else "/" + p

    def selected_path(self) -> str:
        base = self.current_dir().rstrip("/") or ""
        name = self.file_name_edit.text().strip()
        if name:
            return f"{base}/{name}" if base else f"/{name}"
        return base + "/" if base else "/"

    def refresh(self) -> None:
        path = self.current_dir()
        self.list_widget.clear()
        try:
            entries = self.ssh.list_remote_dir(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法列出目录", f"{path}\n{exc}")
            return
        for name, is_dir in entries:
            item = QListWidgetItem(("📁 " if is_dir else "📄 ") + name)
            item.setData(Qt.ItemDataRole.UserRole, (name, is_dir))
            self.list_widget.addItem(item)

    def go_up(self) -> None:
        path = self.current_dir().rstrip("/")
        if not path or path == "/":
            self.path_edit.setText("/")
        else:
            parent = "/".join(path.split("/")[:-1]) or "/"
            self.path_edit.setText(parent)
        self.refresh()

    def on_item_double_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        name, is_dir = data
        if is_dir:
            base = self.current_dir().rstrip("/")
            self.path_edit.setText(f"{base}/{name}" if base != "/" else f"/{name}")
            self.refresh()
        else:
            self.file_name_edit.setText(name)
