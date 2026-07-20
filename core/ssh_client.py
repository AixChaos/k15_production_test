"""SSH + docker exec 远程执行。"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import paramiko

LogFn = Callable[[str], None]


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def combined(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts)


@dataclass
class SshClient:
    host: str
    port: int = 22
    user: str = "anyverse"
    password: str = ""
    key_filename: str = ""
    container_name: str = ""
    container_work_dir: str = "/anyverse"
    host_work_dir: str = "/home/anyverse/work/anyverse"
    _client: Optional[paramiko.SSHClient] = field(default=None, repr=False)

    def connect(self, log: LogFn | None = None) -> None:
        def _log(msg: str) -> None:
            if log:
                log(msg)

        self.close()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
            "timeout": 20,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if self.key_filename:
            kwargs["key_filename"] = self.key_filename
        if self.password:
            kwargs["password"] = self.password
            kwargs["look_for_keys"] = False
            kwargs["allow_agent"] = False
        _log(f"连接域控 {self.user}@{self.host}:{self.port} ...")
        client.connect(**kwargs)
        self._client = client
        _log("SSH 连接成功")

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @property
    def connected(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return bool(transport and transport.is_active())

    def ensure(self) -> paramiko.SSHClient:
        if not self.connected:
            raise RuntimeError("未连接域控，请先点击「连接」")
        assert self._client is not None
        return self._client

    def exec_host(
        self,
        command: str,
        log: LogFn | None = None,
        timeout: int | None = 600,
        get_pty: bool = False,
    ) -> ExecResult:
        client = self.ensure()
        if log:
            log(f"$ (host) {command}")
        stdin, stdout, stderr = client.exec_command(
            command, timeout=timeout, get_pty=get_pty
        )
        out_chunks: list[str] = []
        err_chunks: list[str] = []

        def _drain(channel_file, chunks: list[str], is_err: bool = False) -> None:
            while True:
                line = channel_file.readline()
                if not line:
                    break
                text = line.rstrip("\n")
                chunks.append(text)
                if log:
                    prefix = "[stderr] " if is_err else ""
                    log(prefix + text)

        # 交错读会导致阻塞，先读 stdout 再 stderr；大输出用 channel 轮询更稳
        chan = stdout.channel
        start = time.time()
        while True:
            if chan.recv_ready():
                data = chan.recv(4096).decode("utf-8", errors="replace")
                for line in data.splitlines():
                    out_chunks.append(line)
                    if log:
                        log(line)
            if chan.recv_stderr_ready():
                data = chan.recv_stderr(4096).decode("utf-8", errors="replace")
                for line in data.splitlines():
                    err_chunks.append(line)
                    if log:
                        log("[stderr] " + line)
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if timeout and (time.time() - start) > timeout:
                chan.close()
                raise TimeoutError(f"命令超时 ({timeout}s): {command}")
            time.sleep(0.05)

        code = chan.recv_exit_status()
        return ExecResult(code, "\n".join(out_chunks), "\n".join(err_chunks))

    def resolve_container(self, log: LogFn | None = None) -> str:
        if self.container_name:
            return self.container_name
        result = self.exec_host(
            "docker ps --format '{{.Names}}\\t{{.Image}}'",
            log=log,
            timeout=60,
        )
        if not result.ok:
            raise RuntimeError(f"无法列出 docker 容器: {result.combined}")
        names = []
        for line in result.stdout.splitlines():
            name = line.split("\t")[0].strip().strip("'")
            if not name:
                continue
            lower = name.lower()
            if "anyverse" in lower or "kitt" in lower or "k15" in lower:
                names.append(name)
        if not names:
            # 取第一个运行中的容器
            for line in result.stdout.splitlines():
                name = line.split("\t")[0].strip().strip("'")
                if name:
                    names.append(name)
                    break
        if not names:
            raise RuntimeError("未找到运行中的 Docker 容器，请先执行 docker_run")
        if log:
            log(f"使用容器: {names[0]}")
        self.container_name = names[0]
        return names[0]

    def exec_docker(
        self,
        command: str,
        log: LogFn | None = None,
        timeout: int | None = 1800,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        name = self.resolve_container(log=log)
        wd = workdir or self.container_work_dir
        env_prefix = ""
        if env:
            parts = [f'{k}="{v}"' for k, v in env.items()]
            env_prefix = " ".join(parts) + " "
        # 用 bash -lc 以便 source
        inner = f"cd {wd} && {env_prefix}{command}"
        quoted = inner.replace("'", "'\"'\"'")
        host_cmd = f"docker exec {name} bash -lc '{quoted}'"
        return self.exec_host(host_cmd, log=log, timeout=timeout)

    def write_remote_file(
        self,
        remote_path: str,
        content: str,
        sudo: bool = False,
        log: LogFn | None = None,
    ) -> ExecResult:
        # 通过 SFTP 写临时文件再移动，避免复杂转义
        client = self.ensure()
        sftp = client.open_sftp()
        tmp = f"/tmp/k15_upload_{int(time.time() * 1000)}"
        try:
            with sftp.file(tmp, "w") as f:
                f.write(content)
            if sudo:
                cmd = f"sudo mv {tmp} {remote_path} && sudo chmod 644 {remote_path}"
            else:
                cmd = f"mv {tmp} {remote_path}"
            return self.exec_host(cmd, log=log, timeout=60)
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    def read_remote_file(self, remote_path: str) -> str:
        client = self.ensure()
        sftp = client.open_sftp()
        try:
            with sftp.file(remote_path, "r") as f:
                return f.read().decode("utf-8", errors="replace")
        finally:
            sftp.close()

    @staticmethod
    def local_ip_guess() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
