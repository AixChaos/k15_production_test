"""测试步骤基类。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from core.context import AppContext

LogFn = Callable[[str], None]


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    MANUAL = "manual"


@dataclass
class StepResult:
    ok: bool
    message: str
    log: str = ""
    needs_manual_confirm: bool = False


@dataclass
class TestStep:
    id: str
    title: str
    description: str
    category: str  # env | test
    dangerous: bool = False
    needs_manual: bool = False
    status: StepStatus = StepStatus.PENDING
    last_message: str = ""
    last_log: str = ""

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        raise NotImplementedError


def shell_ok(ctx: AppContext, cmd: str, log: LogFn, docker: bool = False, timeout: int = 1800) -> StepResult:
    if docker:
        res = ctx.ssh.exec_docker(cmd, log=log, timeout=timeout)
    else:
        res = ctx.ssh.exec_host(cmd, log=log, timeout=timeout)
    return StepResult(
        ok=res.ok,
        message="成功" if res.ok else f"失败 (exit={res.exit_code})",
        log=res.combined,
    )
