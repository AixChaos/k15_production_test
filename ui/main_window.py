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
        self._local_file: str = ""

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

        hint = QLabel(
            "页面1：连接配置与文件传输 · 页面2：环境配置与生产测试 · 本机仅显示日志，命令经 SSH + docker exec 执行"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

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
        self.domain_spin = QSpinBox()
        self.domain_spin.setRange(0, 101)
        self.iface_edit = QLineEdit()

        form.addRow(QLabel("<b>目标域控</b>"))
        host_row = QHBoxLayout()
        host_row.addWidget(self.host_edit, 3)
        host_row.addWidget(QLabel("端口"))
        host_row.addWidget(self.port_spin)
        form.addRow("域控 IP", host_row)
        form.addRow("用户名", self.user_edit)
        form.addRow("密码", self.password_edit)
        row_ros = QHBoxLayout()
        row_ros.addWidget(self.domain_spin)
        row_ros.addWidget(QLabel("DDS 网卡"))
        row_ros.addWidget(self.iface_edit)
        form.addRow("DOMAIN_ID", row_ros)

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
        ros = self.config.get("ros", {})

        # 本机：自动探测网线 IP（不影响域控 IP）
        wired = get_wired_ipv4()
        self.pc_ip_edit.setText(wired or str(pc.get("ip", "")))

        # 域控 IP：从配置/历史恢复，启动后不主动清空
        self._load_host_history()
        self.port_spin.setValue(int(dc.get("port", 22)))
        self.user_edit.setText(str(dc.get("user", "nvidia") or "nvidia"))
        self.password_edit.setText(str(dc.get("password", "nvidia") or "nvidia"))
        self.domain_spin.setValue(int(ros.get("domain_id", 40)))
        self.iface_edit.setText(str(ros.get("network_interface", "eth5")))

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
        self.config["ros"]["domain_id"] = self.domain_spin.value()
        self.config["ros"]["network_interface"] = self.iface_edit.text().strip()

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
            self.conn_label.setText(f"已连接 {self._host_text()}")
            self.conn_label.setObjectName("statusConnected")
        else:
            self.conn_label.setText("未连接")
            self.conn_label.setObjectName("statusDisconnected")
        self.conn_label.style().unpolish(self.conn_label)
        self.conn_label.style().polish(self.conn_label)

    def append_log(self, msg: str) -> None:
        for view in (getattr(self, "conn_log_view", None), getattr(self, "log_view", None)):
            if view is None:
                continue
            view.append(msg)
            view.moveCursor(QTextCursor.MoveOperation.End)

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
        try:
            res = self.ssh.upload_local_file(self._local_file, remote, log=self.append_log)
        except Exception as exc:  # noqa: BLE001
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
            )
            client.connect(log=self.append_log)
            # 探测容器并写回配置（界面已不展示容器名）
            try:
                name = client.resolve_container(log=self.append_log)
                self.config.setdefault("domain_controller", {})["container_name"] = name
            except Exception as exc:  # noqa: BLE001
                self.append_log(f"提示: 暂未解析到容器 ({exc})，可先跑「Docker 拉取」步骤")
            if self.ssh:
                self.ssh.close()
            self.ssh = client
            self._set_connection_status(True)
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
                "host": self._host_text(),
                "domain_id": self.domain_spin.value(),
            },
        )
        self.append_log(f"报告已导出: {path}")
        QMessageBox.information(self, "报告", f"已保存:\n{path}")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.ssh:
            self.ssh.close()
        super().closeEvent(event)
