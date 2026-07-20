"""主窗口：连接域控、分步点击执行、日志与报告。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
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
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.config import ROOT, load_config, save_config
from core.context import AppContext, write_report
from core.ssh_client import SshClient
from steps import all_steps, steps_by_category
from steps.base import StepResult, StepStatus, TestStep
from ui.styles import APP_QSS, STATUS_COLORS


class StepWorker(QThread):
    log_line = Signal(str)
    finished_step = Signal(str, object)  # step_id, StepResult

    def __init__(self, step: TestStep, ctx: AppContext) -> None:
        super().__init__()
        self.step = step
        self.ctx = ctx

    def run(self) -> None:
        def _log(msg: str) -> None:
            self.log_line.emit(msg)

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

        # Header
        header = QHBoxLayout()
        title = QLabel("K15 域控生产测试上位机")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch()
        self.conn_label = QLabel("未连接")
        self.conn_label.setObjectName("statusDisconnected")
        header.addWidget(self.conn_label)
        layout.addLayout(header)

        hint = QLabel(
            "适配 Ubuntu 22.04 · 本机仅显示日志 · 所有命令经 SSH + docker exec 在域控执行 · 每步可单独点击执行"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Connection bar
        conn_box = QGroupBox("域控连接")
        form = QFormLayout(conn_box)
        self.host_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.user_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("可选：私钥路径，留空用 ssh-agent/默认密钥")
        self.container_edit = QLineEdit()
        self.container_edit.setPlaceholderText("可选：容器名，留空自动匹配")
        self.work_edit = QLineEdit()
        self.pc_ip_edit = QLineEdit()
        self.domain_spin = QSpinBox()
        self.domain_spin.setRange(0, 101)
        self.iface_edit = QLineEdit()

        row1 = QHBoxLayout()
        row1.addWidget(self.host_edit, 3)
        row1.addWidget(QLabel("端口"))
        row1.addWidget(self.port_spin)
        form.addRow("主机", row1)
        form.addRow("用户", self.user_edit)
        form.addRow("密码", self.password_edit)
        form.addRow("私钥", self.key_edit)
        form.addRow("容器名", self.container_edit)
        form.addRow("宿主机仓库", self.work_edit)
        form.addRow("上位机 IP", self.pc_ip_edit)
        row_ros = QHBoxLayout()
        row_ros.addWidget(self.domain_spin)
        row_ros.addWidget(QLabel("DDS 网卡"))
        row_ros.addWidget(self.iface_edit)
        form.addRow("DOMAIN_ID", row_ros)

        btn_row = QHBoxLayout()
        self.btn_connect = QPushButton("连接")
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
        layout.addWidget(conn_box)

        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        self.btn_save_cfg.clicked.connect(self.on_save_config)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: tabs with step lists
        left = QWidget()
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.env_list = QListWidget()
        self.test_list = QListWidget()
        self.tabs.addTab(self.env_list, "环境配置")
        self.tabs.addTab(self.test_list, "生产测试")
        left_l.addWidget(self.tabs)

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

        self.btn_run.clicked.connect(self.on_run_step)
        self.btn_manual.clicked.connect(self.on_manual_pass)
        self.btn_skip.clicked.connect(self.on_skip)
        self.btn_reset.clicked.connect(self.on_reset)
        self.btn_export.clicked.connect(self.on_export)

        splitter.addWidget(left)

        # Right: detail + log
        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        self.detail_title = QLabel("选择步骤")
        self.detail_title.setObjectName("title")
        self.detail_desc = QLabel("")
        self.detail_desc.setObjectName("hint")
        self.detail_desc.setWordWrap(True)
        right_l.addWidget(self.detail_title)
        right_l.addWidget(self.detail_desc)
        right_l.addWidget(QLabel("实时日志"))
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        right_l.addWidget(self.log_view, 1)
        log_btns = QHBoxLayout()
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_clear_log.setObjectName("ghost")
        self.btn_clear_log.clicked.connect(self.log_view.clear)
        log_btns.addStretch()
        log_btns.addWidget(self.btn_clear_log)
        right_l.addLayout(log_btns)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        self.env_list.currentItemChanged.connect(self.on_selection_changed)
        self.test_list.currentItemChanged.connect(self.on_selection_changed)
        self.tabs.currentChanged.connect(lambda _: self.on_selection_changed())

    def _load_fields_from_config(self) -> None:
        dc = self.config.get("domain_controller", {})
        pc = self.config.get("pc", {})
        ros = self.config.get("ros", {})
        self.host_edit.setText(str(dc.get("host", "")))
        self.port_spin.setValue(int(dc.get("port", 22)))
        self.user_edit.setText(str(dc.get("user", "anyverse")))
        self.password_edit.setText(str(dc.get("password", "")))
        self.key_edit.setText(str(dc.get("key_filename", "")))
        self.container_edit.setText(str(dc.get("container_name", "")))
        self.work_edit.setText(str(dc.get("host_work_dir", "")))
        self.pc_ip_edit.setText(str(pc.get("ip", "")))
        self.domain_spin.setValue(int(ros.get("domain_id", 40)))
        self.iface_edit.setText(str(ros.get("network_interface", "eth5")))

    def _apply_fields_to_config(self) -> None:
        self.config.setdefault("domain_controller", {})
        self.config.setdefault("pc", {})
        self.config.setdefault("ros", {})
        self.config["domain_controller"].update(
            {
                "host": self.host_edit.text().strip(),
                "port": self.port_spin.value(),
                "user": self.user_edit.text().strip(),
                "password": self.password_edit.text(),
                "key_filename": self.key_edit.text().strip(),
                "container_name": self.container_edit.text().strip(),
                "host_work_dir": self.work_edit.text().strip(),
            }
        )
        self.config["pc"]["ip"] = self.pc_ip_edit.text().strip()
        self.config["ros"]["domain_id"] = self.domain_spin.value()
        self.config["ros"]["network_interface"] = self.iface_edit.text().strip()

    def _current_list(self) -> QListWidget:
        return self.env_list if self.tabs.currentIndex() == 0 else self.test_list

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
            if step.dangerous:
                text += "  [慎]"
            if step.needs_manual:
                text += "  [人工]"
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
            self.conn_label.setText(f"已连接 {self.host_edit.text().strip()}")
            self.conn_label.setObjectName("statusConnected")
        else:
            self.conn_label.setText("未连接")
            self.conn_label.setObjectName("statusDisconnected")
        self.conn_label.style().unpolish(self.conn_label)
        self.conn_label.style().polish(self.conn_label)

    def append_log(self, msg: str) -> None:
        self.log_view.append(msg)
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def make_ctx(self) -> AppContext:
        if not self.ssh or not self.ssh.connected:
            raise RuntimeError("请先连接域控")
        self._apply_fields_to_config()
        # 同步 UI 字段到 ssh 对象
        self.ssh.container_name = self.container_edit.text().strip()
        self.ssh.host_work_dir = self.work_edit.text().strip()
        return AppContext(config=self.config, ssh=self.ssh, log=self.append_log)

    @Slot()
    def on_selection_changed(self, *_args) -> None:
        step = self._current_step()
        if not step:
            return
        self.detail_title.setText(step.title)
        extra = []
        if step.dangerous:
            extra.append("危险操作：请确认域控环境后再执行。")
        if step.needs_manual:
            extra.append("需要人工确认：执行后请检查结果，再点「人工确认通过」。")
        if step.last_message:
            extra.append(f"上次结果: {step.last_message}")
        self.detail_desc.setText(step.description + ("\n" + "\n".join(extra) if extra else ""))

    @Slot()
    def on_connect(self) -> None:
        self._apply_fields_to_config()
        dc = self.config["domain_controller"]
        try:
            client = SshClient(
                host=dc["host"],
                port=int(dc.get("port", 22)),
                user=dc.get("user", "anyverse"),
                password=dc.get("password", "") or "",
                key_filename=dc.get("key_filename", "") or "",
                container_name=dc.get("container_name", "") or "",
                container_work_dir=dc.get("container_work_dir", "/anyverse"),
                host_work_dir=dc.get("host_work_dir", "/home/anyverse/work/anyverse"),
            )
            client.connect(log=self.append_log)
            # 探测容器
            try:
                name = client.resolve_container(log=self.append_log)
                self.container_edit.setText(name)
            except Exception as exc:  # noqa: BLE001
                self.append_log(f"提示: 暂未解析到容器 ({exc})，可先跑「Docker 拉取」步骤")
            if self.ssh:
                self.ssh.close()
            self.ssh = client
            self._set_connection_status(True)
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
        self._apply_fields_to_config()
        save_config(self.config)
        self.append_log(f"配置已保存: {ROOT / 'config' / 'default.yaml'}")
        QMessageBox.information(self, "保存", "配置已写入 config/default.yaml")

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
        try:
            ctx = self.make_ctx()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "未连接", str(exc))
            return

        self._busy = True
        step.status = StepStatus.RUNNING
        self._refresh_lists()
        self.append_log(f"\n======== 开始: {step.title} ========")
        self.btn_run.setEnabled(False)

        self.worker = StepWorker(step, ctx)
        self.worker.log_line.connect(self.append_log)
        self.worker.finished_step.connect(self.on_step_finished)
        self.worker.start()

    @Slot(str, object)
    def on_step_finished(self, step_id: str, result: object) -> None:
        self._busy = False
        self.btn_run.setEnabled(True)
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
                "host": self.host_edit.text().strip(),
                "domain_id": self.domain_spin.value(),
            },
        )
        self.append_log(f"报告已导出: {path}")
        QMessageBox.information(self, "报告", f"已保存:\n{path}")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.ssh:
            self.ssh.close()
        super().closeEvent(event)
