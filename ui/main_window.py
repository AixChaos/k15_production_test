"""主窗口：连接域控、分步点击执行、日志与报告。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.config import ROOT, load_config, save_config
from core.context import AppContext, write_report
from core.netinfo import get_wired_ipv4
from core.ssh_client import SshClient
from steps import all_steps, steps_by_category
from steps.base import StepResult, StepStatus, TestStep
from ui.remote_path_dialog import RemotePathDialog
from ui.end_effector_dialog import EndEffectorChoiceDialog
from ui.styles import APP_QSS, STATUS_COLORS
from ui.upload_progress_dialog import UploadProgressDialog


class StepWorker(QThread):
    log_line = Signal(str)
    log_channel = Signal(str, str)  # channel, message
    log_layout = Signal(str)  # "single" | "triple"
    # 字节数可能超过 2GB，不能用 Qt int（有符号 32 位），改用 object 传 Python int
    upload_begin = Signal(str, object)  # filename, total_bytes
    upload_progress = Signal(int, object, object, str)  # pct, done, total, speed
    upload_end = Signal(bool)
    finished_step = Signal(str, object)  # step_id, StepResult

    def __init__(self, step: TestStep, ctx: AppContext) -> None:
        super().__init__()
        self.step = step
        self.ctx = ctx

    def run(self) -> None:
        def _log(msg: str) -> None:
            self.log_line.emit(msg)

        def _log_channel(channel: str, msg: str) -> None:
            self.log_channel.emit(channel, msg)

        def _set_layout(mode: str) -> None:
            self.log_layout.emit(mode)

        self.ctx.log = _log
        self.ctx.log_channel = _log_channel
        self.ctx.set_log_layout = _set_layout
        self.ctx.on_upload_begin = lambda name, total: self.upload_begin.emit(name, int(total))
        self.ctx.on_upload_progress = (
            lambda pct, done, total, speed: self.upload_progress.emit(
                int(pct), int(done), int(total), speed or ""
            )
        )
        self.ctx.on_upload_end = lambda ok: self.upload_end.emit(bool(ok))

        try:
            result = self.step.run(self.ctx, _log)
        except Exception as exc:  # noqa: BLE001
            result = StepResult(False, f"异常: {exc}", str(exc))
        self.finished_step.emit(self.step.id, result)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("K15 生产测试上位机")
        self.resize(1280, 820)
        self.setStyleSheet(APP_QSS)

        self.config = load_config()
        self.steps: list[TestStep] = all_steps()
        self.step_map = {s.id: s for s in self.steps}
        self.ssh: Optional[SshClient] = None
        self.worker: Optional[StepWorker] = None
        self._busy = False
        self._local_file: str = ""
        self._upload_dlg: Optional[UploadProgressDialog] = None

        self._build_ui()
        self._load_fields_from_config()
        self._refresh_lists()
        self._set_connection_status(False)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # Header（两页共用）
        header = QHBoxLayout()
        title = QLabel("K15 域控生产测试上位机")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch()
        self.conn_label = QLabel("未连接")
        self.conn_label.setObjectName("statusDisconnected")
        header.addWidget(self.conn_label)
        layout.addLayout(header)

        # 顶层两个页面
        self.main_tabs = QTabWidget()
        self.main_tabs.setObjectName("mainTabs")
        self.main_tabs.addTab(self._build_page_connection(), "1. 连接配置")
        self.main_tabs.addTab(self._build_page_tests(), "2. 环境与测试")
        layout.addWidget(self.main_tabs, 1)

        self.btn_refresh_ip.clicked.connect(self.on_refresh_pc_ip)
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        self.btn_save_cfg.clicked.connect(self.on_save_config)
        self.btn_pick_local.clicked.connect(self.on_pick_local_file)
        self.btn_xfer.clicked.connect(self.on_transfer_file)
        self.host_edit.lineEdit().editingFinished.connect(self.on_host_editing_finished)
        self.btn_run.clicked.connect(self.on_run_step)
        self.btn_manual.clicked.connect(self.on_manual_pass)
        self.btn_skip.clicked.connect(self.on_skip)
        self.btn_reset.clicked.connect(self.on_reset)
        self.btn_export.clicked.connect(self.on_export)
        self.env_list.currentItemChanged.connect(self.on_selection_changed)
        self.test_list.currentItemChanged.connect(self.on_selection_changed)
        self.step_tabs.currentChanged.connect(lambda _: self.on_selection_changed())

    def _build_page_connection(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 12, 8, 8)

        splitter = QSplitter(Qt.Orientation.Vertical)

        form_host = QWidget()
        form_l = QVBoxLayout(form_host)
        form_l.setContentsMargins(0, 0, 0, 0)

        conn_box = QGroupBox("连接配置")
        form = QFormLayout(conn_box)

        # —— 本机 ——
        self.pc_ip_edit = QLineEdit()
        self.pc_ip_edit.setReadOnly(True)
        self.btn_refresh_ip = QPushButton("刷新网线 IP")
        self.btn_refresh_ip.setObjectName("ghost")
        pc_ip_row = QHBoxLayout()
        pc_ip_row.addWidget(self.pc_ip_edit, 1)
        pc_ip_row.addWidget(self.btn_refresh_ip)
        form.addRow(QLabel("<b>本机</b>"))
        form.addRow("网线 IP", pc_ip_row)

        # —— 域控 ——
        self.host_edit = QComboBox()
        self.host_edit.setEditable(True)
        self.host_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.host_edit.setMaxCount(30)
        self.host_edit.lineEdit().setPlaceholderText("请手动填写目标域控 IP（填写后自动记住）")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(22)
        self.user_edit = QLineEdit("nvidia")
        self.password_edit = QLineEdit("nvidia")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)

        form.addRow(QLabel("<b>目标域控</b>"))
        host_row = QHBoxLayout()
        host_row.addWidget(self.host_edit, 3)
        host_row.addWidget(QLabel("端口"))
        host_row.addWidget(self.port_spin)
        form.addRow("域控 IP", host_row)
        form.addRow("用户名", self.user_edit)
        form.addRow("密码", self.password_edit)

        btn_row = QHBoxLayout()
        self.btn_connect = QPushButton("连接域控")
        self.btn_connect.setObjectName("success")
        self.btn_disconnect = QPushButton("断开")
        self.btn_disconnect.setObjectName("ghost")
        self.btn_save_cfg = QPushButton("保存配置")
        self.btn_save_cfg.setObjectName("ghost")
        btn_row.addWidget(self.btn_connect)
        btn_row.addWidget(self.btn_disconnect)
        btn_row.addWidget(self.btn_save_cfg)
        btn_row.addStretch()
        form.addRow(btn_row)

        # —— 文件传输 ——
        form.addRow(QLabel("<b>文件传输</b>（本机 → 域控）"))
        self.local_file_edit = QLineEdit()
        self.local_file_edit.setReadOnly(True)
        self.local_file_edit.setPlaceholderText("尚未选择本地文件")
        xfer_row1 = QHBoxLayout()
        self.btn_pick_local = QPushButton("1. 选择本地文件")
        xfer_row1.addWidget(self.local_file_edit, 1)
        xfer_row1.addWidget(self.btn_pick_local)
        form.addRow("本地文件", xfer_row1)

        self.remote_path_edit = QLineEdit()
        self.remote_path_edit.setPlaceholderText("域控目标路径（按钮 2 可浏览选择并自动传输）")
        xfer_row2 = QHBoxLayout()
        self.btn_xfer = QPushButton("2. 选择域控路径并传输")
        self.btn_xfer.setObjectName("success")
        xfer_row2.addWidget(self.remote_path_edit, 1)
        xfer_row2.addWidget(self.btn_xfer)
        form.addRow("域控路径", xfer_row2)

        form_l.addWidget(conn_box)
        form_l.addStretch()
        splitter.addWidget(form_host)

        # 页面1 日志
        log_host = QWidget()
        log_l = QVBoxLayout(log_host)
        log_l.setContentsMargins(0, 0, 0, 0)
        log_l.addWidget(QLabel("连接 / 传输日志"))
        self.conn_log_view = QTextEdit()
        self.conn_log_view.setReadOnly(True)
        log_l.addWidget(self.conn_log_view, 1)
        btn_clear_conn = QPushButton("清空日志")
        btn_clear_conn.setObjectName("ghost")
        btn_clear_conn.clicked.connect(self.conn_log_view.clear)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(btn_clear_conn)
        log_l.addLayout(row)
        splitter.addWidget(log_host)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout.addWidget(splitter)
        return page

    def _build_page_tests(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 12, 8, 8)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 0, 0)
        self.step_tabs = QTabWidget()
        self.env_list = QListWidget()
        self.test_list = QListWidget()
        self.step_tabs.addTab(self.env_list, "环境配置")
        self.step_tabs.addTab(self.test_list, "生产测试")
        left_l.addWidget(self.step_tabs)

        self.progress_label = QLabel("进度: 0 / 0 通过")
        left_l.addWidget(self.progress_label)

        act = QHBoxLayout()
        self.btn_run = QPushButton("执行本步骤")
        self.btn_manual = QPushButton("人工确认通过")
        self.btn_manual.setObjectName("success")
        self.btn_skip = QPushButton("跳过")
        self.btn_skip.setObjectName("ghost")
        self.btn_reset = QPushButton("重置状态")
        self.btn_reset.setObjectName("ghost")
        act.addWidget(self.btn_run)
        act.addWidget(self.btn_manual)
        left_l.addLayout(act)
        act2 = QHBoxLayout()
        act2.addWidget(self.btn_skip)
        act2.addWidget(self.btn_reset)
        self.btn_export = QPushButton("导出报告")
        act2.addWidget(self.btn_export)
        left_l.addLayout(act2)
        splitter.addWidget(left)

        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        self.log_title = QLabel("实时日志")
        right_l.addWidget(self.log_title)

        self.log_stack = QStackedWidget()

        # 单栏（默认）
        single_page = QWidget()
        single_l = QVBoxLayout(single_page)
        single_l.setContentsMargins(0, 0, 0, 0)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        single_l.addWidget(self.log_view, 1)
        self.log_stack.addWidget(single_page)

        # 三栏（关节时延：Controller / MoveIt / Latency）
        triple_page = QWidget()
        triple_l = QVBoxLayout(triple_page)
        triple_l.setContentsMargins(0, 0, 0, 0)
        self.triple_status = QLabel("三栏就绪后将只显示各进程日志")
        self.triple_status.setObjectName("hint")
        self.triple_status.setWordWrap(True)
        triple_l.addWidget(self.triple_status)
        self.log_channel_views: dict[str, QTextEdit] = {}
        self.log_triple_splitter = QSplitter()
        for key, title, color in (
            ("controller", "Controller（域控）", "#4fc3f7"),
            ("moveit", "MoveIt（本机）", "#81c784"),
            ("latency", "Latency（时延）", "#ffb74d"),
        ):
            col = QWidget()
            col_l = QVBoxLayout(col)
            col_l.setContentsMargins(0, 0, 0, 0)
            col_l.setSpacing(4)
            hdr = QLabel(title)
            hdr.setObjectName("logPaneHeader")
            hdr.setStyleSheet(f"color: {color}; font-weight: 600;")
            view = QTextEdit()
            view.setReadOnly(True)
            col_l.addWidget(hdr)
            col_l.addWidget(view, 1)
            self.log_channel_views[key] = view
            self.log_triple_splitter.addWidget(col)
        self.log_triple_splitter.setStretchFactor(0, 1)
        self.log_triple_splitter.setStretchFactor(1, 1)
        self.log_triple_splitter.setStretchFactor(2, 1)
        triple_l.addWidget(self.log_triple_splitter, 1)
        self.log_stack.addWidget(triple_page)
        self.log_stack.setCurrentIndex(0)
        self._log_layout_mode = "single"

        right_l.addWidget(self.log_stack, 1)
        log_btns = QHBoxLayout()
        self.btn_finish_test = QPushButton("测试完成（停止三路）")
        self.btn_finish_test.setObjectName("success")
        self.btn_finish_test.setEnabled(False)
        self.btn_finish_test.setToolTip("关节时延测试运行中可点：停止 Controller / MoveIt / Latency")
        self.btn_finish_test.clicked.connect(self.on_finish_joint_test)
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_clear_log.setObjectName("ghost")
        self.btn_clear_log.clicked.connect(self.clear_logs)
        log_btns.addWidget(self.btn_finish_test)
        log_btns.addStretch()
        log_btns.addWidget(self.btn_clear_log)
        right_l.addLayout(log_btns)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        layout.addWidget(splitter)
        return page

    def _host_text(self) -> str:
        return self.host_edit.currentText().strip()

    def _set_host_text(self, host: str) -> None:
        """设置当前 IP，不主动清空已有内容。"""
        host = (host or "").strip()
        if not host:
            return
        # 阻断信号，避免填充历史时触发多余保存
        self.host_edit.blockSignals(True)
        idx = self.host_edit.findText(host)
        if idx < 0:
            self.host_edit.insertItem(0, host)
            idx = 0
        self.host_edit.setCurrentIndex(idx)
        self.host_edit.setEditText(host)
        self.host_edit.blockSignals(False)

    def _load_host_history(self) -> None:
        dc = self.config.get("domain_controller", {})
        history = dc.get("host_history") or []
        if not isinstance(history, list):
            history = []
        last = str(dc.get("host", "") or "").strip()
        # 合并：last 优先，去重保序
        merged: list[str] = []
        for item in [last, *[str(x).strip() for x in history]]:
            if item and item not in merged:
                merged.append(item)
        self.host_edit.blockSignals(True)
        self.host_edit.clear()
        for ip in merged:
            self.host_edit.addItem(ip)
        if merged:
            self.host_edit.setCurrentIndex(0)
            self.host_edit.setEditText(merged[0])
        else:
            # 无历史时保持空，等待用户填写；不强制清空已有编辑内容
            self.host_edit.setEditText("")
        self.host_edit.blockSignals(False)

    def _remember_host(self, persist: bool = True) -> str:
        """记录当前域控 IP 到历史，并写回配置（不会用空值覆盖已有 host）。"""
        host = self._host_text()
        dc = self.config.setdefault("domain_controller", {})
        history = [str(x).strip() for x in (dc.get("host_history") or []) if str(x).strip()]
        if host:
            history = [host, *[h for h in history if h != host]]
            dc["host"] = host
            dc["host_history"] = history[:20]
            # 更新下拉，当前项置顶
            self.host_edit.blockSignals(True)
            existing = [self.host_edit.itemText(i) for i in range(self.host_edit.count())]
            if host not in existing:
                self.host_edit.insertItem(0, host)
            else:
                idx = self.host_edit.findText(host)
                if idx > 0:
                    self.host_edit.removeItem(idx)
                    self.host_edit.insertItem(0, host)
            self.host_edit.setCurrentIndex(0)
            self.host_edit.setEditText(host)
            self.host_edit.blockSignals(False)
            if persist:
                save_config(self.config)
        # 空输入：不写回空 host，保留配置里上次的值
        return host

    def _load_fields_from_config(self) -> None:
        dc = self.config.get("domain_controller", {})
        pc = self.config.get("pc", {})

        # 本机：自动探测网线 IP（不影响域控 IP）
        wired = get_wired_ipv4()
        self.pc_ip_edit.setText(wired or str(pc.get("ip", "")))

        # 域控 IP：从配置/历史恢复，启动后不主动清空
        self._load_host_history()
        self.port_spin.setValue(int(dc.get("port", 22)))
        self.user_edit.setText(str(dc.get("user", "nvidia") or "nvidia"))
        self.password_edit.setText(str(dc.get("password", "nvidia") or "nvidia"))

    def _apply_fields_to_config(self) -> None:
        self.config.setdefault("domain_controller", {})
        self.config.setdefault("pc", {})
        self.config.setdefault("ros", {})
        dc = self.config["domain_controller"]
        host = self._host_text()
        # 仅在非空时更新 host，避免空值覆盖历史
        if host:
            dc["host"] = host
        dc.update(
            {
                "port": self.port_spin.value(),
                "user": self.user_edit.text().strip() or "nvidia",
                "password": self.password_edit.text() or "nvidia",
            }
        )
        # 私钥 / 容器名 / 仓库路径：界面已隐藏，保留配置文件中的值
        dc.setdefault("key_filename", "")
        dc.setdefault("container_name", "")
        dc.setdefault("host_work_dir", "/home/nvidia/work/anyverse")
        dc.setdefault("host_history", [])
        self.config["pc"]["ip"] = self.pc_ip_edit.text().strip()
        self.config["pc"].setdefault("user", "wujie")
        self.config["pc"].setdefault("password", "123456")
        # DOMAIN_ID / DDS 网卡：界面已隐藏，保留配置文件默认值
        self.config["ros"].setdefault("domain_id", 40)
        self.config["ros"].setdefault("network_interface", "eth5")

    def _current_list(self) -> QListWidget:
        return self.env_list if self.step_tabs.currentIndex() == 0 else self.test_list

    def _current_step(self) -> Optional[TestStep]:
        item = self._current_list().currentItem()
        if not item:
            return None
        sid = item.data(Qt.ItemDataRole.UserRole)
        return self.step_map.get(sid)

    def _refresh_lists(self) -> None:
        self._fill_list(self.env_list, steps_by_category("env"))
        self._fill_list(self.test_list, steps_by_category("test"))
        self._update_progress()

    def _fill_list(self, widget: QListWidget, steps: list[TestStep]) -> None:
        current_id = None
        cur = widget.currentItem()
        if cur:
            current_id = cur.data(Qt.ItemDataRole.UserRole)
        widget.clear()
        for i, step in enumerate(steps, 1):
            mark = {
                StepStatus.PENDING: "○",
                StepStatus.RUNNING: "◎",
                StepStatus.PASS: "●",
                StepStatus.FAIL: "✕",
                StepStatus.SKIP: "–",
                StepStatus.MANUAL: "!",
            }.get(step.status, "○")
            text = f"{mark} {i}. {step.title}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, step.id)
            color = STATUS_COLORS.get(step.status.value, "#9aa0a6")
            item.setForeground(QColor(color))
            widget.addItem(item)
            if step.id == current_id:
                widget.setCurrentItem(item)

    def _update_progress(self) -> None:
        total = len(self.steps)
        passed = sum(1 for s in self.steps if s.status in (StepStatus.PASS, StepStatus.MANUAL))
        failed = sum(1 for s in self.steps if s.status == StepStatus.FAIL)
        self.progress_label.setText(f"进度: {passed} / {total} 通过 · 失败 {failed}")

    def _set_connection_status(self, ok: bool) -> None:
        if ok:
            self.conn_label.setText(f"已连接 {self._host_text()}")
            self.conn_label.setObjectName("statusConnected")
        else:
            self.conn_label.setText("未连接")
            self.conn_label.setObjectName("statusDisconnected")
        self.conn_label.style().unpolish(self.conn_label)
        self.conn_label.style().polish(self.conn_label)

    def clear_logs(self) -> None:
        self.log_view.clear()
        for view in getattr(self, "log_channel_views", {}).values():
            view.clear()

    def set_log_layout(self, mode: str) -> None:
        mode = (mode or "single").strip().lower()
        if mode not in ("single", "triple"):
            mode = "single"
        self._log_layout_mode = mode
        if mode == "triple":
            self.log_title.setText("实时日志 · 三路分栏（各栏仅本进程日志）")
            for view in self.log_channel_views.values():
                view.clear()
            if hasattr(self, "triple_status"):
                self.triple_status.setText("等待三路启动…")
            self.log_stack.setCurrentIndex(1)
            # 关节时延运行中才允许点「测试完成」
            if getattr(self, "_busy", False) and getattr(self, "worker", None):
                step = getattr(self.worker, "step", None)
                if step is not None and getattr(step, "id", "") == "test_joint_latency":
                    self.btn_finish_test.setEnabled(True)
        else:
            self.log_title.setText("实时日志")
            self.log_stack.setCurrentIndex(0)
            self.btn_finish_test.setEnabled(False)

    def append_channel_log(self, channel: str, msg: str) -> None:
        """channel: controller|moveit|latency → 对应栏；status → 状态条。"""
        if channel == "status":
            if hasattr(self, "triple_status"):
                self.triple_status.setText(msg)
            return
        view = self.log_channel_views.get(channel)
        if view is None:
            # 未知通道：不写三栏，落到主日志（若在单栏）或忽略
            if self._log_layout_mode != "triple":
                self.log_view.append(msg)
                self.log_view.moveCursor(QTextCursor.MoveOperation.End)
            return
        view.append(msg)
        view.moveCursor(QTextCursor.MoveOperation.End)

    def _append_to_text_views(self, msg: str, *views: object) -> None:
        import html

        color = None
        if msg.startswith("[Controller]"):
            color = "#4fc3f7"
        elif msg.startswith("[MoveIt]"):
            color = "#81c784"
        elif msg.startswith("[Latency]"):
            color = "#ffb74d"
        elif msg.startswith("[系统]"):
            color = "#ce93d8"
        elif msg.startswith("═") or msg.startswith("─"):
            color = "#90a4ae"

        for view in views:
            if view is None:
                continue
            if color:
                view.append(
                    f'<span style="color:{color};font-family:Consolas,monospace;">'
                    f"{html.escape(msg)}</span>"
                )
            else:
                view.append(msg)
            view.moveCursor(QTextCursor.MoveOperation.End)

    def append_log(self, msg: str) -> None:
        # 三栏模式：进程日志进对应栏；系统日志进状态条，并始终镜像到连接页日志
        if getattr(self, "_log_layout_mode", "single") == "triple":
            if msg.startswith("[Controller]"):
                body = msg.split(" ", 1)[1] if " " in msg else msg
                self.append_channel_log("controller", body.strip())
                return
            if msg.startswith("[MoveIt]"):
                body = msg.split(" ", 1)[1] if " " in msg else msg
                self.append_channel_log("moveit", body.strip())
                return
            if msg.startswith("[Latency]"):
                body = msg.split(" ", 1)[1] if " " in msg else msg
                self.append_channel_log("latency", body.strip())
                return
            self.append_channel_log("status", msg.strip() or msg)
            self._append_to_text_views(msg, getattr(self, "conn_log_view", None))
            return

        self._append_to_text_views(
            msg,
            getattr(self, "conn_log_view", None),
            getattr(self, "log_view", None),
        )

    def make_ctx(self) -> AppContext:
        if not self.ssh or not self.ssh.connected:
            raise RuntimeError("请先连接域控")
        self._apply_fields_to_config()
        # 同步配置到 ssh 对象（容器名 / 仓库路径来自配置文件）
        dc = self.config.get("domain_controller", {})
        self.ssh.container_name = str(dc.get("container_name", "") or "")
        self.ssh.host_work_dir = str(
            dc.get("host_work_dir", "/home/nvidia/work/anyverse") or "/home/nvidia/work/anyverse"
        )
        self.ssh.container_user = str(dc.get("container_user", "admin") or "admin")
        return AppContext(config=self.config, ssh=self.ssh, log=self.append_log)

    @Slot()
    def on_finish_joint_test(self) -> None:
        """关节时延运行中：通知步骤侧停止三路并结束等待。"""
        w = self.worker
        if not w or not getattr(self, "_busy", False):
            return
        step = getattr(w, "step", None)
        if step is None or getattr(step, "id", "") != "test_joint_latency":
            return
        ctx = getattr(w, "ctx", None)
        if ctx is None:
            return
        ctx.finish_event.set()
        self.btn_finish_test.setEnabled(False)
        self.append_channel_log("status", "已请求测试完成，正在停止三路…")

    @Slot()
    def on_selection_changed(self, *_args) -> None:
        # 右侧仅保留实时日志，不再展示步骤说明
        return

    @Slot()
    def on_refresh_pc_ip(self) -> None:
        ip = get_wired_ipv4()
        self.pc_ip_edit.setText(ip)
        if ip:
            self.append_log(f"本机网线 IP: {ip}")
        else:
            self.append_log("未检测到有线网卡 IP，请检查网线是否已连接")
            QMessageBox.warning(self, "本机 IP", "未检测到有线网卡 IP")

    @Slot()
    def on_pick_local_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择本地文件", str(Path.home()), "所有文件 (*)")
        if not path:
            return
        self._local_file = path
        self.local_file_edit.setText(path)
        self.append_log(f"已选择本地文件: {path}")

    @Slot(str, object)
    def on_upload_begin(self, filename: str, total: object) -> None:
        if self._upload_dlg is None:
            self._upload_dlg = UploadProgressDialog(self)
        self._upload_dlg.start(filename, int(total or 0))

    @Slot(int, object, object, str)
    def on_upload_progress(self, pct: int, done: object, total: object, speed: str) -> None:
        if self._upload_dlg is not None:
            self._upload_dlg.update_progress(int(pct), int(done or 0), int(total or 0), speed or "")

    @Slot(bool)
    def on_upload_end(self, ok: bool) -> None:
        if self._upload_dlg is not None:
            self._upload_dlg.finish(ok)
            self._upload_dlg = None

    @Slot()
    def on_transfer_file(self) -> None:
        if not self._local_file or not Path(self._local_file).is_file():
            QMessageBox.information(self, "提示", "请先点击「1. 选择本地文件」")
            return
        if not self.ssh or not self.ssh.connected:
            QMessageBox.warning(self, "未连接", "请先连接域控后再传输文件")
            return

        start = self.remote_path_edit.text().strip() or f"/home/{self.user_edit.text().strip() or 'nvidia'}"
        dlg = RemotePathDialog(self.ssh, start_path=start, parent=self)
        # 预填本地文件名
        dlg.file_name_edit.setText(Path(self._local_file).name)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return

        remote = dlg.selected_path()
        self.remote_path_edit.setText(remote)
        self.append_log(f"\n======== 文件传输 ========\n本地: {self._local_file}\n域控: {remote}")

        progress_dlg = UploadProgressDialog(self)
        from PySide6.QtWidgets import QApplication

        def _begin(name: str, total: int) -> None:
            progress_dlg.start(name, total)
            QApplication.processEvents()

        def _prog(pct: int, done: int, total: int, speed: str) -> None:
            progress_dlg.update_progress(pct, done, total, speed)
            QApplication.processEvents()

        def _end(ok: bool) -> None:
            progress_dlg.finish(ok)
            QApplication.processEvents()

        try:
            res = self.ssh.upload_local_file(
                self._local_file,
                remote,
                log=self.append_log,
                progress=_prog,
                on_begin=_begin,
                on_end=_end,
            )
        except Exception as exc:  # noqa: BLE001
            if progress_dlg.isVisible():
                progress_dlg.finish(False)
            QMessageBox.critical(self, "传输失败", str(exc))
            self.append_log(f"[FAIL] 传输异常: {exc}")
            return
        if res.ok:
            self.append_log(f"[PASS] 已传输到 {remote}")
            QMessageBox.information(self, "传输成功", f"已上传到域控:\n{remote}")
        else:
            self.append_log(f"[FAIL] 传输失败: {res.combined}")
            QMessageBox.critical(self, "传输失败", res.combined or "未知错误")

    @Slot()
    def on_host_editing_finished(self) -> None:
        """离开输入框时记住本次填写（空值不覆盖历史）。"""
        host = self._remember_host(persist=True)
        if host:
            self.append_log(f"已记住域控 IP: {host}")

    @Slot()
    def on_connect(self) -> None:
        host = self._host_text()
        if not host:
            # 尝试用配置里上次的 host
            last = str(self.config.get("domain_controller", {}).get("host", "") or "").strip()
            if last:
                self._set_host_text(last)
                host = last
        if not host:
            QMessageBox.warning(self, "域控 IP", "请手动填写目标域控 IP")
            self.host_edit.setFocus()
            return
        self._remember_host(persist=True)
        self._apply_fields_to_config()
        dc = self.config["domain_controller"]
        try:
            client = SshClient(
                host=dc["host"],
                port=int(dc.get("port", 22)),
                user=dc.get("user", "nvidia") or "nvidia",
                password=dc.get("password", "") or "",
                key_filename=dc.get("key_filename", "") or "",
                container_name=dc.get("container_name", "") or "",
                container_work_dir=dc.get("container_work_dir", "/anyverse"),
                host_work_dir=dc.get("host_work_dir", "/home/nvidia/work/anyverse"),
                container_user=dc.get("container_user", "admin") or "admin",
            )
            client.connect(log=self.append_log)
            # 探测容器并写回配置（界面已不展示容器名）
            try:
                name = client.resolve_container(log=self.append_log)
                self.config.setdefault("domain_controller", {})["container_name"] = name
                # 域控重启后容器常因 jtop.sock 未就绪而未起来，连接时一并拉起
                client.ensure_container_running(log=self.append_log)
            except Exception as exc:  # noqa: BLE001
                self.append_log(f"提示: 容器未就绪 ({exc})，SSH 已连通，可先完成环境部署后再测")
            if self.ssh:
                self.ssh.close()
            self.ssh = client
            self._set_connection_status(True)
            self.append_log(f"连接成功: {self.user_edit.text().strip() or 'nvidia'}@{host}")
            save_config(self.config)
        except Exception as exc:  # noqa: BLE001
            self._set_connection_status(False)
            QMessageBox.critical(self, "连接失败", str(exc))

    @Slot()
    def on_disconnect(self) -> None:
        if self.ssh:
            self.ssh.close()
            self.ssh = None
        self._set_connection_status(False)
        self.append_log("已断开 SSH")

    @Slot()
    def on_save_config(self) -> None:
        self._remember_host(persist=False)
        self._apply_fields_to_config()
        save_config(self.config)
        self.append_log(f"配置已保存: {ROOT / 'config' / 'default.yaml'}")
        QMessageBox.information(self, "保存", "配置已写入 config/default.yaml")

    def _pick_env_package(self) -> str:
        """选择本机环境压缩包；默认打开 ~/下载。"""
        downloads = Path.home() / "下载"
        if not downloads.is_dir():
            downloads = Path.home() / "Downloads"
        start = str(downloads if downloads.is_dir() else Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择环境压缩包 (如 K15_env_con.tar.gz)",
            start,
            "压缩包 (*.tar.gz *.tgz);;所有文件 (*)",
        )
        return path or ""

    @Slot()
    def on_run_step(self) -> None:
        if self._busy:
            QMessageBox.warning(self, "忙碌", "已有步骤在执行")
            return
        step = self._current_step()
        if not step:
            QMessageBox.information(self, "提示", "请先选择步骤")
            return
        if step.dangerous:
            ret = QMessageBox.question(
                self,
                "确认危险操作",
                f"步骤「{step.title}」可能修改系统配置或重建容器，确认执行？",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return

        package_path = ""
        if step.id == "env_package_deploy":
            package_path = self._pick_env_package()
            if not package_path:
                self.append_log("已取消：未选择环境压缩包")
                return
            if not Path(package_path).is_file():
                QMessageBox.warning(self, "文件无效", f"所选文件不存在:\n{package_path}")
                return
            self.append_log(f"已选择环境压缩包: {package_path}")

        end_effector_mode = ""
        if step.id == "env_end_effector":
            choice_dlg = EndEffectorChoiceDialog(self)
            if choice_dlg.exec() != choice_dlg.DialogCode.Accepted:
                self.append_log("已取消：未选择末端配置方式")
                return
            end_effector_mode = choice_dlg.selected_mode() or ""
            if not end_effector_mode:
                self.append_log("已取消：未选择末端配置方式")
                return
            mode_cn = "已装末端设备" if end_effector_mode == "installed" else "未装末端设备"
            self.append_log(f"已选择: {mode_cn}")

        try:
            ctx = self.make_ctx()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "未连接", str(exc))
            return

        if package_path:
            ctx.local_package_path = package_path
            ctx.config.setdefault("env_package", {})["local_path"] = package_path
        if end_effector_mode:
            ctx.end_effector_mode = end_effector_mode

        self._busy = True
        step.status = StepStatus.RUNNING
        self._refresh_lists()
        # 非关节时延步骤恢复单栏；三栏由该步骤自行切换并保留到下次执行
        if step.id != "test_joint_latency":
            self.set_log_layout("single")
            self.btn_finish_test.setEnabled(False)
        else:
            self.btn_finish_test.setEnabled(True)
        self.append_log(f"\n======== 开始: {step.title} ========")
        self.btn_run.setEnabled(False)

        self.worker = StepWorker(step, ctx)
        self.worker.log_line.connect(self.append_log)
        self.worker.log_channel.connect(self.append_channel_log)
        self.worker.log_layout.connect(self.set_log_layout)
        self.worker.upload_begin.connect(self.on_upload_begin)
        self.worker.upload_progress.connect(self.on_upload_progress)
        self.worker.upload_end.connect(self.on_upload_end)
        self.worker.finished_step.connect(self.on_step_finished)
        self.worker.start()

    @Slot(str, object)
    def on_step_finished(self, step_id: str, result: object) -> None:
        self._busy = False
        self.btn_run.setEnabled(True)
        self.btn_finish_test.setEnabled(False)
        step = self.step_map[step_id]
        assert isinstance(result, StepResult)
        step.last_message = result.message
        step.last_log = result.log
        if result.ok:
            if result.needs_manual_confirm or step.needs_manual:
                step.status = StepStatus.MANUAL
                self.append_log(f"[待人工确认] {result.message}")
            else:
                step.status = StepStatus.PASS
                self.append_log(f"[PASS] {result.message}")
        else:
            step.status = StepStatus.FAIL
            self.append_log(f"[FAIL] {result.message}")
        self._refresh_lists()
        self.on_selection_changed()

    @Slot()
    def on_manual_pass(self) -> None:
        step = self._current_step()
        if not step:
            return
        step.status = StepStatus.PASS
        step.last_message = "人工确认通过"
        self.append_log(f"[PASS][人工] {step.title}")
        self._refresh_lists()

    @Slot()
    def on_skip(self) -> None:
        step = self._current_step()
        if not step:
            return
        step.status = StepStatus.SKIP
        step.last_message = "已跳过"
        self.append_log(f"[SKIP] {step.title}")
        self._refresh_lists()

    @Slot()
    def on_reset(self) -> None:
        for s in self.steps:
            s.status = StepStatus.PENDING
            s.last_message = ""
            s.last_log = ""
        self._refresh_lists()
        self.append_log("已重置全部步骤状态")

    @Slot()
    def on_export(self) -> None:
        results = [
            {
                "id": s.id,
                "title": s.title,
                "category": s.category,
                "status": s.status.value,
                "message": s.last_message,
            }
            for s in self.steps
        ]
        out_dir = ROOT / self.config.get("report", {}).get("output_dir", "reports")
        path = write_report(
            results,
            out_dir,
            meta={
                "host": self._host_text(),
                "domain_id": self.config.get("ros", {}).get("domain_id", 40),
            },
        )
        self.append_log(f"报告已导出: {path}")
        QMessageBox.information(self, "报告", f"已保存:\n{path}")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.ssh:
            self.ssh.close()
        super().closeEvent(event)
