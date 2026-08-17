"""配置加载。

开发态：读写仓库内 config/default.yaml。
安装态（/opt/...）：默认模板在包内，读写用户目录
~/.config/k15-production-test/default.yaml。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGED_CONFIG = ROOT / "config" / "default.yaml"
USER_CONFIG_DIR = Path.home() / ".config" / "k15-production-test"
USER_CONFIG = USER_CONFIG_DIR / "default.yaml"
USER_DATA_DIR = Path.home() / ".local" / "share" / "k15-production-test"

# 兼容旧导入名
DEFAULT_CONFIG = PACKAGED_CONFIG


def is_packaged_install() -> bool:
    """安装到系统目录时视为打包安装。"""
    parts = {p.lower() for p in ROOT.parts}
    return "opt" in parts or ROOT.as_posix().startswith("/usr/")


def config_path() -> Path:
    """优先用户配置；没有则用包内默认模板。"""
    if USER_CONFIG.is_file():
        return USER_CONFIG
    return PACKAGED_CONFIG


def report_dir(configured: str | Path | None = None) -> Path:
    """报告输出目录：打包安装写到用户数据目录。"""
    name = str(configured or "reports").strip() or "reports"
    p = Path(name)
    if p.is_absolute():
        return p
    if is_packaged_install():
        return USER_DATA_DIR / name
    return ROOT / name


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or config_path()
    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def save_config(data: dict[str, Any], path: Path | None = None) -> None:
    # 打包安装始终写用户目录，避免 /opt 无写权限
    if path is None:
        cfg_path = USER_CONFIG if is_packaged_install() else PACKAGED_CONFIG
    else:
        cfg_path = path
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
