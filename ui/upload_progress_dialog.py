"""文件上传进度弹窗。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)


def _fmt_bytes(n: int) -> str:
    if n < 0:
        n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            if u in ("B", "KB"):
                return f"{size:.0f} {u}"
            return f"{size:.2f} {u}"
        size /= 1024
    return f"{n} B"


def _fmt_eta(done: int, total: int, speed_bps: float) -> str:
    if speed_bps <= 0 or total <= done:
        return "计算剩余时间…"
    remain = (total - done) / speed_bps
    if remain < 60:
        return f"约剩余 {int(remain)} 秒"
    if remain < 3600:
        m = int(remain // 60)
        s = int(remain % 60)
        return f"约剩余 {m} 分 {s:02d} 秒"
    h = int(remain // 3600)
    m = int((remain % 3600) // 60)
    return f"约剩余 {h} 小时 {m} 分"


def _parse_speed_to_bps(speed: str) -> float:
    """把 rsync 的 '28.66MB/s' 转成字节/秒。"""
    s = (speed or "").strip().upper().replace("/S", "")
    if not s:
        return 0.0
    mult = 1.0
    if s.endswith("GB"):
        mult = 1024**3
        s = s[:-2]
    elif s.endswith("MB"):
        mult = 1024**2
        s = s[:-2]
    elif s.endswith("KB"):
        mult = 1024
        s = s[:-2]
    elif s.endswith("B"):
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


class UploadProgressDialog(QDialog):
    """模态进度框：显示文件名、百分比、速度与预估剩余时间。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("文件上传")
        self.setModal(True)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
        )
        self.setFixedWidth(460)
        self.setStyleSheet(
            """
            QDialog {
                background: #1a1d23;
                color: #e8eaed;
            }
            QLabel#uploadTitle {
                font-size: 16px;
                font-weight: 700;
                color: #f0f3f6;
            }
            QLabel#uploadFile {
                color: #c5c8c6;
                font-size: 13px;
            }
            QLabel#uploadDetail {
                color: #9aa0a6;
                font-size: 12px;
            }
            QProgressBar {
                border: 1px solid #2d333b;
                border-radius: 6px;
                text-align: center;
                background: #0f1115;
                height: 22px;
                color: #e8eaed;
                font-weight: 600;
            }
            QProgressBar::chunk {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #3d5a80, stop:1 #52b788
                );
                border-radius: 5px;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        self.title_label = QLabel("正在上传到域控")
        self.title_label.setObjectName("uploadTitle")
        layout.addWidget(self.title_label)

        self.file_label = QLabel("—")
        self.file_label.setObjectName("uploadFile")
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        layout.addWidget(self.bar)

        self.detail_label = QLabel("准备中…")
        self.detail_label.setObjectName("uploadDetail")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self._total = 0

    def start(self, filename: str, total_bytes: int) -> None:
        self._total = max(0, total_bytes)
        self.file_label.setText(filename)
        self.bar.setValue(0)
        size_txt = _fmt_bytes(self._total) if self._total else "未知大小"
        self.detail_label.setText(f"总大小 {size_txt} · 正在建立传输…")
        self.title_label.setText("正在上传到域控")
        self.show()
        self.raise_()
        self.activateWindow()

    def update_progress(
        self,
        percent: int,
        transferred: int,
        total: int,
        speed: str = "",
    ) -> None:
        pct = max(0, min(100, int(percent)))
        self.bar.setValue(pct)
        tot = total or self._total
        if tot <= 0:
            self.detail_label.setText(f"{pct}% · {speed or '—'}")
            return
        speed_bps = _parse_speed_to_bps(speed)
        eta = _fmt_eta(transferred, tot, speed_bps)
        speed_txt = speed if speed else "—"
        self.detail_label.setText(
            f"{_fmt_bytes(transferred)} / {_fmt_bytes(tot)}  ·  {speed_txt}  ·  {eta}"
        )

    def finish(self, ok: bool = True) -> None:
        if ok:
            self.bar.setValue(100)
            self.title_label.setText("上传完成")
            self.detail_label.setText("文件已传输到域控")
        else:
            self.title_label.setText("上传失败")
        self.accept()
