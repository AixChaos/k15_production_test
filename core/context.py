"""运行上下文与报告。"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.ssh_client import SshClient

LogFn = Callable[[str], None]
LogChannelFn = Callable[[str, str], None]  # channel, message
LogLayoutFn = Callable[[str], None]  # "single" | "triple"
UploadBeginFn = Callable[[str, int], None]
UploadProgressFn = Callable[[int, int, int, str], None]
UploadEndFn = Callable[[bool], None]


@dataclass
class AppContext:
    config: dict[str, Any]
    ssh: SshClient
    log: LogFn = field(default=lambda _m: None)
    # 分通道日志（关节时延三栏）：controller / moveit / latency；status 仅状态条
    log_channel: LogChannelFn | None = None
    set_log_layout: LogLayoutFn | None = None
    cancelled: bool = False
    # 关节时延：UI「测试完成」置位后，步骤侧停止三路并结束等待
    finish_event: threading.Event = field(default_factory=threading.Event)
    # 本机选择的环境压缩包路径（由 UI 在执行步骤前填入）
    local_package_path: str = ""
    # 上传进度（由 UI 注入，供弹窗显示）
    on_upload_begin: UploadBeginFn | None = None
    on_upload_progress: UploadProgressFn | None = None
    on_upload_end: UploadEndFn | None = None
    # 双臂末端：installed=已装 / not_installed=未装
    end_effector_mode: str = ""

    @property
    def dc(self) -> dict[str, Any]:
        return self.config.get("domain_controller", {})

    @property
    def ros(self) -> dict[str, Any]:
        return self.config.get("ros", {})

    def ros_env_exports(self, domain_id: int | None = None) -> str:
        r = self.ros
        did = r.get("domain_id", 40) if domain_id is None else domain_id
        uri = str(r.get("cyclonedds_uri", "/anyverse/config/cyclonedds.xml") or "")
        if uri.startswith("/") and not uri.startswith("file:"):
            uri = f"file://{uri}"
        return (
            f"export RMW_IMPLEMENTATION={r.get('rmw', 'rmw_cyclonedds_cpp')}; "
            f"export CYCLONEDDS_URI={uri}; "
            f"export ROS_DOMAIN_ID={did}; "
            f"export ROS_LOCALHOST_ONLY={r.get('localhost_only', 0)}"
        )

    def source_ws(self) -> str:
        # 域控镜像多为 jazzy；humble 仅作回退，避免错误 distro 干扰
        return (
            "source /opt/ros/jazzy/setup.bash 2>/dev/null || "
            "source /opt/ros/humble/setup.bash 2>/dev/null || true; "
            "source .ws/devel/setup.bash"
        )


def write_report(
    results: list[dict[str, Any]],
    output_dir: Path,
    meta: dict[str, Any] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"k15_test_report_{ts}.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": meta or {},
        "results": results,
        "summary": {
            "total": len(results),
            "pass": sum(1 for r in results if r.get("status") == "pass"),
            "fail": sum(1 for r in results if r.get("status") == "fail"),
            "skip": sum(1 for r in results if r.get("status") == "skip"),
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
