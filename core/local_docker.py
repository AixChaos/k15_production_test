"""本机 Docker 执行（MoveIt Demo 等跑在上位机容器内）。"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from core.ssh_client import ExecResult

LogFn = Callable[[str], None]


@dataclass
class LocalDocker:
    container_name: str = "agent_dev_wujie"
    container_user: str = "admin"
    work_dir: str = "/anyverse"

    def exec(
        self,
        command: str,
        log: LogFn | None = None,
        timeout: int | None = 1800,
        stream_output: bool = True,
        workdir: str | None = None,
    ) -> ExecResult:
        wd = workdir or self.work_dir
        user = (self.container_user or "admin").strip() or "admin"
        inner = f"cd {wd} && {command}"
        # 与域控一致：bash -lc 以便 source
        cmd = [
            "docker",
            "exec",
            "-u",
            user,
            self.container_name,
            "bash",
            "-lc",
            inner,
        ]
        if log:
            one = " ".join(command.strip().splitlines())
            if len(one) > 120:
                one = one[:117] + "..."
            log(f"$ [local docker] {one}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except FileNotFoundError:
            return ExecResult(127, "", "本机未找到 docker 命令")
        except Exception as exc:  # noqa: BLE001
            return ExecResult(1, "", str(exc))

        out_lines: list[str] = []
        start = time.time()
        assert proc.stdout is not None
        while True:
            if timeout and (time.time() - start) > timeout:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                return ExecResult(124, "\n".join(out_lines), f"本机 docker 命令超时 ({timeout}s)")
            line = proc.stdout.readline()
            if line:
                text = line.rstrip("\n")
                out_lines.append(text)
                if log and stream_output:
                    log(text)
            elif proc.poll() is not None:
                # drain remainder
                rest = proc.stdout.read() or ""
                for ln in rest.splitlines():
                    out_lines.append(ln)
                    if log and stream_output:
                        log(ln)
                break
            else:
                time.sleep(0.05)

        code = proc.returncode if proc.returncode is not None else 1
        return ExecResult(code, "\n".join(out_lines), "")

    def write_file(self, container_path: str, content: str, log: LogFn | None = None) -> ExecResult:
        """写入容器内文件。

        使用 ``docker exec … cat > path`` 原地覆盖，兼容单文件 bind-mount
        （如 cyclonedds_x86.xml → /anyverse/config/cyclonedds.xml）；
        ``docker cp`` 会 unlink 目标，报 device or resource busy。
        """
        import shlex

        user = (self.container_user or "admin").strip() or "admin"
        quoted = shlex.quote(container_path)
        try:
            cp = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    "-u",
                    user,
                    self.container_name,
                    "bash",
                    "-lc",
                    f"cat > {quoted}",
                ],
                input=content,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if cp.returncode != 0:
                err = (cp.stderr or cp.stdout or "").strip()
                if log:
                    log(f"写入容器文件失败: {err}")
                return ExecResult(cp.returncode, cp.stdout or "", cp.stderr or "")
            return ExecResult(0, "OK", "")
        except Exception as exc:  # noqa: BLE001
            return ExecResult(1, "", str(exc))
