"""运行上下文与报告。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.ssh_client import SshClient

LogFn = Callable[[str], None]


@dataclass
class AppContext:
    config: dict[str, Any]
    ssh: SshClient
    log: LogFn = field(default=lambda _m: None)
    cancelled: bool = False

    @property
    def dc(self) -> dict[str, Any]:
        return self.config.get("domain_controller", {})

    @property
    def ros(self) -> dict[str, Any]:
        return self.config.get("ros", {})

    def ros_env_exports(self) -> str:
        r = self.ros
        return (
            f"export RMW_IMPLEMENTATION={r.get('rmw', 'rmw_cyclonedds_cpp')}; "
            f"export CYCLONEDDS_URI={r.get('cyclonedds_uri', '/anyverse/config/cyclonedds.xml')}; "
            f"export ROS_DOMAIN_ID={r.get('domain_id', 40)}; "
            f"export ROS_LOCALHOST_ONLY={r.get('localhost_only', 0)}"
        )

    def source_ws(self) -> str:
        return "source /opt/ros/humble/setup.bash 2>/dev/null || true; source .ws/devel/setup.bash"


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
