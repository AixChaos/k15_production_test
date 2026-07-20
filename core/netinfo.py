"""本机网卡 / IP 探测。"""
from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path


WIRED_PREFIXES = ("eth", "enp", "eno", "ens")
SKIP_PREFIXES = (
    "lo",
    "wlan",
    "wlp",
    "wwan",
    "docker",
    "br-",
    "veth",
    "virbr",
    "vmnet",
    "tailscale",
    "tun",
    "tap",
    "cni",
    "flannel",
)


def _is_wired_name(name: str) -> bool:
    lower = name.lower()
    if any(lower.startswith(p) for p in SKIP_PREFIXES):
        return False
    return any(lower.startswith(p) for p in WIRED_PREFIXES)


def _has_carrier(iface: str) -> bool:
    carrier = Path(f"/sys/class/net/{iface}/carrier")
    oper = Path(f"/sys/class/net/{iface}/operstate")
    try:
        if carrier.exists() and carrier.read_text(encoding="utf-8").strip() == "1":
            return True
    except OSError:
        pass
    try:
        if oper.exists() and oper.read_text(encoding="utf-8").strip() == "up":
            return True
    except OSError:
        pass
    return False


def _ipv4_of(iface: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "dev", iface],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            idx = parts.index("inet")
            if idx + 1 < len(parts):
                return parts[idx + 1].split("/")[0]
    return None


def get_wired_ipv4() -> str:
    """优先取已插网线且 UP 的有线网卡 IPv4；失败则回退到路由出口地址。"""
    net_dir = Path("/sys/class/net")
    if net_dir.is_dir():
        candidates: list[tuple[int, str, str]] = []
        for entry in sorted(net_dir.iterdir()):
            name = entry.name
            if not _is_wired_name(name):
                continue
            ip = _ipv4_of(name)
            if not ip:
                continue
            score = 2 if _has_carrier(name) else 1
            candidates.append((score, name, ip))
        if candidates:
            candidates.sort(key=lambda x: (-x[0], x[1]))
            return candidates[0][2]

    # 回退：默认路由出口
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return ""


def default_local_user() -> str:
    return os.environ.get("USER") or "wujie"
