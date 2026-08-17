"""浏览域控目录并下载文件/文件夹到本机 Downloads。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
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
from ui.upload_progress_dialog import UploadProgressDialog


def local_downloads_dir() -> Path:
    """优先中文「下载」，其次 Downloads / Download。"""
    home = Path.home()
    for name in ("下载", "Downloads", "Download"):
        p = home / name
        if p.is_dir():
            return p
    p = home / "Downloads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def unique_local_path(directory: Path, filename: str) -> Path:
    """若同名已存在则追加 _1、_2…"""
    base = directory / filename
    if not base.exists():
        return base
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    n = 1
    while True:
        cand = directory / f"{stem}_{n}{suffix}"
        if not cand.exists():
            return cand
        n += 1


class RemoteDownloadDialog(QDialog):
    def __init__(self, ssh: SshClient, start_path: str = "/home/nvidia", parent=None) -> None:
        super().__init__(parent)
        self.ssh = ssh
        self._dest_dir = local_downloads_dir()
        self.setWindowTitle("从域控下载文件")
        self.resize(580, 480)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "浏览域控目录：单击选中，双击文件夹进入；"
                "选中文件或文件夹后点「下载」保存到本机下载目录"
            )
        )

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

        action_row = QHBoxLayout()
        self.btn_enter = QPushButton("进入选中文件夹")
        self.btn_enter.setObjectName("ghost")
        self.btn_download = QPushButton("下载")
        self.btn_download.setObjectName("success")
        self.btn_download.setToolTip("下载当前选中的文件，或递归下载选中文件夹及全部内容")
        action_row.addWidget(self.btn_enter)
        action_row.addWidget(self.btn_download)
        action_row.addStretch()
        layout.addLayout(action_row)

        self.dest_label = QLabel(f"保存到: {self._dest_dir}")
        self.dest_label.setObjectName("hint")
        self.dest_label.setWordWrap(True)
        layout.addWidget(self.dest_label)

        self.status_label = QLabel("")
        self.status_label.setObjectName("hint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.setText("关闭")
        layout.addWidget(buttons)

        self.btn_go.clicked.connect(self.refresh)
        self.btn_up.clicked.connect(self.go_up)
        self.btn_enter.clicked.connect(self.on_enter_selected)
        self.btn_download.clicked.connect(self.on_download_selected)
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.refresh()

    def current_dir(self) -> str:
        p = self.path_edit.text().strip() or "/"
        return p if p.startswith("/") else "/" + p

    def _selected_entry(self) -> tuple[str, bool] | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return None
        name, is_dir = data
        return str(name), bool(is_dir)

    def _remote_path_for(self, name: str) -> str:
        base = self.current_dir().rstrip("/")
        return f"{base}/{name}" if base != "/" else f"/{name}"

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
        self.status_label.setText(f"当前: {path}  ·  共 {len(entries)} 项")

    def go_up(self) -> None:
        path = self.current_dir().rstrip("/")
        if not path or path == "/":
            self.path_edit.setText("/")
        else:
            parent = "/".join(path.split("/")[:-1]) or "/"
            self.path_edit.setText(parent)
        self.refresh()

    def _enter_dir(self, remote: str) -> None:
        self.path_edit.setText(remote)
        self.refresh()

    def on_item_double_clicked(self, item: QListWidgetItem) -> None:
        """双击文件夹：进入；双击文件：无操作（需点下载）。"""
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        name, is_dir = data
        if is_dir:
            self._enter_dir(self._remote_path_for(name))

    def on_enter_selected(self) -> None:
        sel = self._selected_entry()
        if not sel:
            QMessageBox.information(self, "提示", "请先选中一个文件夹")
            return
        name, is_dir = sel
        if not is_dir:
            QMessageBox.information(self, "提示", "选中项是文件，请点「下载」")
            return
        self._enter_dir(self._remote_path_for(name))

    def on_download_selected(self) -> None:
        sel = self._selected_entry()
        if not sel:
            QMessageBox.information(self, "提示", "请先选中要下载的文件或文件夹")
            return
        name, is_dir = sel
        remote = self._remote_path_for(name)
        if is_dir:
            self._download_dir(remote, name)
        else:
            self._download_file(remote, name)

    def _run_with_progress(self, title_name: str, runner):
        progress_dlg = UploadProgressDialog(self)

        def _begin(name: str, total: int) -> None:
            progress_dlg.start(name or title_name, total, mode="download")
            QApplication.processEvents()

        def _prog(pct: int, done: int, total: int, speed: str) -> None:
            progress_dlg.update_progress(pct, done, total, speed)
            QApplication.processEvents()

        def _end(ok: bool) -> None:
            progress_dlg.finish(ok)
            QApplication.processEvents()

        try:
            return runner(_prog, _begin, _end)
        except Exception as exc:  # noqa: BLE001
            if progress_dlg.isVisible():
                progress_dlg.finish(False)
            QMessageBox.critical(self, "下载失败", str(exc))
            self.status_label.setText(f"下载失败: {exc}")
            return None

    def _download_file(self, remote_path: str, filename: str) -> None:
        local = unique_local_path(self._dest_dir, filename)
        self.status_label.setText(f"正在下载文件: {remote_path}")

        def runner(prog, begin, end):
            return self.ssh.download_remote_file(
                remote_path,
                str(local),
                log=None,
                progress=prog,
                on_begin=begin,
                on_end=end,
            )

        res = self._run_with_progress(filename, runner)
        if res is None:
            return
        if res.ok:
            self.status_label.setText(f"已保存: {local}")
            QMessageBox.information(self, "下载成功", f"已保存到:\n{local}")
        else:
            QMessageBox.critical(self, "下载失败", res.combined or "未知错误")
            self.status_label.setText(f"下载失败: {res.combined or '未知错误'}")

    def _download_dir(self, remote_path: str, dirname: str) -> None:
        local = unique_local_path(self._dest_dir, dirname)
        self.status_label.setText(f"正在下载文件夹: {remote_path}")

        def runner(prog, begin, end):
            return self.ssh.download_remote_dir(
                remote_path,
                str(local),
                log=None,
                progress=prog,
                on_begin=begin,
                on_end=end,
            )

        res = self._run_with_progress(dirname + "/", runner)
        if res is None:
            return
        if res.ok:
            self.status_label.setText(f"已保存文件夹: {local}")
            QMessageBox.information(
                self,
                "下载成功",
                f"文件夹已保存到:\n{local}\n\n（含该目录下全部内容）",
            )
        else:
            QMessageBox.critical(self, "下载失败", res.combined or "未知错误")
            self.status_label.setText(f"下载失败: {res.combined or '未知错误'}")
