"""多路日志并行采集（域控 / 本机 docker 日志文件）。"""
from __future__ import annotations

import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from core.ssh_client import SshClient

LogFn = Callable[[str], None]


@dataclass
class LogStream:
    """一路日志源。"""

    tag: str  # 显示前缀，如 [Controller]
    # remote: 经 SSH docker exec tail；local: 本机 docker exec tail
    kind: str  # "remote" | "local"
    log_path: str
    container: str = ""
    user: str = "admin"


def _format_line(tag: str, line: str) -> str:
    line = line.rstrip("\n\r")
    if not line:
        return ""
    # 固定宽度标签，便于对齐
    return f"{tag:<12} {line}"


def _remote_tail_cmd(container: str, user: str, path: str) -> str:
    return (
        f"docker exec -u {user} {container} "
        f"bash -lc 'touch {path}; tail -n +1 -F {path}'"
    )


def _local_tail_cmd(container: str, user: str, path: str) -> list[str]:
    return [
        "docker",
        "exec",
        "-u",
        user,
        container,
        "bash",
        "-lc",
        f"touch {path}; tail -n +1 -F {path}",
    ]


class ParallelLogHub:
    """并行拉取多路 tail -F，汇总到同一 log 回调。"""

    def __init__(self, ssh: SshClient | None = None) -> None:
        self.ssh = ssh
        self._q: queue.Queue[tuple[str, str]] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._procs: list[subprocess.Popen] = []
        self._channels: list = []

    def start(self, streams: list[LogStream]) -> None:
        self._stop.clear()
        for s in streams:
            if s.kind == "remote":
                t = threading.Thread(
                    target=self._remote_reader, args=(s,), daemon=True
                )
            else:
                t = threading.Thread(
                    target=self._local_reader, args=(s,), daemon=True
                )
            self._threads.append(t)
            t.start()

    def _remote_reader(self, s: LogStream) -> None:
        if not self.ssh or not self.ssh.connected:
            self._q.put((s.tag, "(SSH 未连接，无法拉取该路日志)"))
            return
        try:
            client = self.ssh.ensure()
            cmd = _remote_tail_cmd(s.container, s.user, s.log_path)
            _stdin, stdout, _stderr = client.exec_command(cmd, timeout=None)
            chan = stdout.channel
            self._channels.append(chan)
            buf = ""
            while not self._stop.is_set():
                if chan.recv_ready():
                    data = chan.recv(4096).decode("utf-8", errors="replace")
                    if not data:
                        break
                    buf += data
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        if line.strip():
                            self._q.put((s.tag, line))
                elif chan.exit_status_ready():
                    break
                else:
                    time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            self._q.put((s.tag, f"(日志流中断: {exc})"))

    def _local_reader(self, s: LogStream) -> None:
        try:
            proc = subprocess.Popen(
                _local_tail_cmd(s.container, s.user, s.log_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
            )
            self._procs.append(proc)
            assert proc.stdout is not None
            while not self._stop.is_set():
                line = proc.stdout.readline()
                if line:
                    if line.strip():
                        self._q.put((s.tag, line.rstrip("\n")))
                elif proc.poll() is not None:
                    break
                else:
                    time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            self._q.put((s.tag, f"(日志流中断: {exc})"))

    def drain(
        self,
        log: LogFn,
        sink: Optional[list[str]] = None,
        channel_log: Optional[Callable[[str, str], None]] = None,
        tag_to_channel: Optional[dict[str, str]] = None,
    ) -> int:
        """取出当前队列中所有行并输出，返回行数。

        channel_log: 若提供，则按 tag_to_channel 分发到独立面板，不再交错写入主 log。
        """
        mapping = tag_to_channel or {
            "[Controller]": "controller",
            "[MoveIt]": "moveit",
            "[Latency]": "latency",
        }
        n = 0
        while True:
            try:
                tag, line = self._q.get_nowait()
            except queue.Empty:
                break
            text = _format_line(tag, line)
            if not text:
                continue
            if channel_log is not None:
                ch = mapping.get(tag.strip())
                if ch is None:
                    continue
                # 面板内只显示纯内容，去掉重复标签
                body = line.rstrip("\n\r")
                if body:
                    channel_log(ch, body)
                if sink is not None:
                    sink.append(text)
            else:
                log(text)
                if sink is not None:
                    sink.append(text)
            n += 1
        return n

    def stop(self) -> None:
        self._stop.set()
        for chan in self._channels:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass
        for proc in self._procs:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
        self._channels.clear()
        self._procs.clear()
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads.clear()


def banner_parallel_start(log: LogFn) -> None:
    log("关节时延 · 三路分栏：左 Controller / 中 MoveIt / 右 Latency")
