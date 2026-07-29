"""SSH + docker exec 远程执行。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import paramiko

LogFn = Callable[[str], None]
# percent, transferred_bytes, total_bytes, speed_text (e.g. "28.6MB/s")
ProgressFn = Callable[[int, int, int, str], None]


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
        echo_cmd: bool = False,
        stream_output: bool = True,
    ) -> ExecResult:
        """在域控宿主机执行命令。

        echo_cmd: 是否把原始 shell 打到日志（默认关闭，避免刷屏）
        stream_output: 是否把远端 stdout/stderr 逐行打到日志
        """
        client = self.ensure()
        if log and echo_cmd:
            # 多行脚本只显示首行摘要，避免把整段脚本甩给用户
            one_line = " ".join(command.strip().splitlines())
            if len(one_line) > 120:
                one_line = one_line[:117] + "..."
            log(f"$ {one_line}")
        stdin, stdout, stderr = client.exec_command(
            command, timeout=timeout, get_pty=get_pty
        )
        out_chunks: list[str] = []
        err_chunks: list[str] = []

        # 交错读会导致阻塞，先读 stdout 再 stderr；大输出用 channel 轮询更稳
        chan = stdout.channel
        start = time.time()
        while True:
            if chan.recv_ready():
                data = chan.recv(4096).decode("utf-8", errors="replace")
                for line in data.splitlines():
                    out_chunks.append(line)
                    if log and stream_output:
                        log(line)
            if chan.recv_stderr_ready():
                data = chan.recv_stderr(4096).decode("utf-8", errors="replace")
                for line in data.splitlines():
                    err_chunks.append(line)
                    if log and stream_output:
                        log("[stderr] " + line)
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if timeout and (time.time() - start) > timeout:
                chan.close()
                raise TimeoutError(f"命令超时 ({timeout}s)")
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
        stream_output: bool = True,
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
        return self.exec_host(
            host_cmd, log=log, timeout=timeout, stream_output=stream_output
        )

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
                # 必须整段进 sudo，否则 && 后的 chmod 仍是普通用户
                cmd = self._sudo_cmd(
                    f"sh -c 'mv {tmp} {remote_path} && chmod 644 {remote_path}'"
                )
            else:
                cmd = f"mv {tmp} {remote_path}"
            return self.exec_host(cmd, log=log, timeout=60)
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    def _sudo_cmd(self, command: str) -> str:
        """用当前密码非交互提权；无密码则直接 sudo。"""
        if self.password:
            # -S 从 stdin 读密码；-p '' 避免提示干扰
            escaped = self.password.replace("'", "'\"'\"'")
            return f"printf '%s\\n' '{escaped}' | sudo -S -p '' {command}"
        return f"sudo -n {command}"

    def list_remote_dir(self, remote_dir: str) -> list[tuple[str, bool]]:
        """返回 [(name, is_dir), ...]。"""
        client = self.ensure()
        sftp = client.open_sftp()
        try:
            entries: list[tuple[str, bool]] = []
            for attr in sftp.listdir_attr(remote_dir):
                name = attr.filename
                if name in (".", ".."):
                    continue
                is_dir = False
                try:
                    import stat as statmod

                    is_dir = statmod.S_ISDIR(attr.st_mode or 0)
                except Exception:
                    is_dir = False
                entries.append((name, is_dir))
            entries.sort(key=lambda x: (not x[1], x[0].lower()))
            return entries
        finally:
            sftp.close()

    def _ssh_cli_opts(self, *, for_scp: bool = False) -> list[str]:
        """外部 ssh/scp/rsync 共用参数：关压缩、优先快密码套件。"""
        port_flag = "-P" if for_scp else "-p"
        opts = [
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "Compression=no",
            "-o",
            "ServerAliveInterval=20",
            "-o",
            "ServerAliveCountMax=6",
            "-o",
            "IPQoS=throughput",
            "-c",
            "aes128-gcm@openssh.com,chacha20-poly1305@openssh.com,aes128-ctr",
            port_flag,
            str(self.port),
        ]
        if self.key_filename:
            opts.extend(["-i", self.key_filename])
        return opts

    def _run_cli_transfer(
        self,
        argv: list[str],
        *,
        use_password: bool,
        log: LogFn | None,
        total_size: int,
        timeout: int | None = None,
        progress: ProgressFn | None = None,
    ) -> ExecResult:
        """运行 rsync/scp；进度走 progress 回调，不写日志刷屏。"""
        import os
        import re
        import subprocess
        import time as time_mod

        env = os.environ.copy()
        cmd = list(argv)
        if use_password and self.password:
            env["SSHPASS"] = self.password
            cmd = ["sshpass", "-e", *cmd]

        last_pct = [-1]
        out_lines: list[str] = []
        pct_re = re.compile(r"(\d[\d,]*)\s+(\d+)%\s+([\d.]+[KMGT]?B/s)", re.I)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=env,
            )
        except FileNotFoundError as exc:
            return ExecResult(127, "", str(exc))

        assert proc.stdout is not None
        buf = b""
        start = time_mod.time()
        try:
            while True:
                if timeout and (time_mod.time() - start) > timeout:
                    proc.kill()
                    proc.wait(timeout=10)
                    return ExecResult(124, "\n".join(out_lines), "transfer timeout")
                chunk = proc.stdout.read(4096)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    time_mod.sleep(0.05)
                    continue
                buf += chunk
                while True:
                    m_sep = re.search(rb"[\r\n]", buf)
                    if not m_sep:
                        break
                    part, buf = buf[: m_sep.start()], buf[m_sep.end() :]
                    if not part:
                        continue
                    line = part.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    out_lines.append(line)
                    m = pct_re.search(line)
                    if m and total_size > 0:
                        pct = int(m.group(2))
                        speed = m.group(3)
                        done = int(m.group(1).replace(",", ""))
                        # 弹窗需要较密刷新；每 1% 回调一次
                        if pct != last_pct[0] and (pct == 100 or pct >= last_pct[0] + 1):
                            last_pct[0] = pct
                            if progress:
                                progress(pct, done, total_size, speed)
            if buf.strip():
                line = buf.decode("utf-8", errors="replace").strip()
                out_lines.append(line)
            code = proc.wait(timeout=30)
        except Exception as exc:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:
                pass
            return ExecResult(1, "\n".join(out_lines), str(exc))

        if code == 0 and progress and total_size > 0:
            progress(100, total_size, total_size, "")
        text = "\n".join(out_lines[-20:])
        return ExecResult(code, text if code == 0 else "", text if code != 0 else "")

    def upload_local_file(
        self,
        local_path: str,
        remote_path: str,
        log: LogFn | None = None,
        progress: ProgressFn | None = None,
        on_begin: Callable[[str, int], None] | None = None,
        on_end: Callable[[bool], None] | None = None,
    ) -> ExecResult:
        """上传本地文件到域控。

        优先 rsync，其次 scp（远快于 Paramiko SFTP）；都不可用时回退 SFTP。
        进度通过 progress 回调上报（供 UI 弹窗），不写入日志栏。
        """
        import os
        import shutil
        import time
        from pathlib import Path

        local = Path(local_path)
        if not local.is_file():
            raise FileNotFoundError(f"本地文件不存在: {local_path}")

        remote_path = remote_path.strip()
        if remote_path.endswith("/"):
            remote_path = remote_path + local.name
        probe = self.exec_host(
            f'test -d "{remote_path}" && echo ISDIR || true',
            log=None,
            timeout=30,
        )
        if "ISDIR" in probe.combined:
            remote_path = remote_path.rstrip("/") + "/" + local.name

        parent = os.path.dirname(remote_path) or "/"
        self.exec_host(f'mkdir -p "{parent}" 2>/dev/null || true', log=None, timeout=30)

        total = local.stat().st_size
        tmp = f"/tmp/k15_xfer_{int(time.time() * 1000)}_{local.name}"
        dest_candidates = [remote_path, tmp]
        use_password = bool(self.password) and not self.key_filename
        remote_target_base = f"{self.user}@{self.host}"

        if on_begin:
            on_begin(local.name, total)

        def _place_from_tmp() -> ExecResult:
            res = self.exec_host(
                f'mkdir -p "{parent}" 2>/dev/null; mv "{tmp}" "{remote_path}" && chmod 644 "{remote_path}"',
                log=None,
                timeout=600,
            )
            if res.ok:
                return res
            if log:
                log("普通权限不足，改用提权放置…")
            self.exec_host(f'rm -f "{remote_path}" 2>/dev/null || true', log=None, timeout=30)
            return self.exec_host(
                self._sudo_cmd(
                    f'mkdir -p "{parent}" && mv "{tmp}" "{remote_path}" && chmod 644 "{remote_path}"'
                ),
                log=None,
                timeout=600,
            )

        ok = False
        try:
            # —— 1) rsync ——
            if shutil.which("rsync") and (shutil.which("sshpass") or not use_password):
                ssh_e = "ssh " + " ".join(self._ssh_cli_opts(for_scp=False))
                for dest in dest_candidates:
                    remote_spec = f"{remote_target_base}:{dest}"
                    argv = [
                        "rsync",
                        "-a",
                        "--inplace",
                        "--partial",
                        "--info=progress2",
                        "-e",
                        ssh_e,
                        str(local),
                        remote_spec,
                    ]
                    res = self._run_cli_transfer(
                        argv,
                        use_password=use_password,
                        log=log,
                        total_size=total,
                        timeout=6 * 3600,
                        progress=progress,
                    )
                    if res.ok:
                        if dest == tmp:
                            placed = _place_from_tmp()
                            if not placed.ok:
                                return placed
                        if log:
                            log("上传完成")
                        ok = True
                        return ExecResult(0, f"uploaded via rsync -> {remote_path}", "")
                    if log and dest == remote_path:
                        log("直达目标失败，改传到临时目录后再放置…")

            # —— 2) scp ——
            if shutil.which("scp") and (shutil.which("sshpass") or not use_password):
                for dest in dest_candidates:
                    argv = [
                        "scp",
                        "-q",
                        *self._ssh_cli_opts(for_scp=True),
                        str(local),
                        f"{remote_target_base}:{dest}",
                    ]
                    if log:
                        log("正在通过 scp 上传…")
                    res = self._run_cli_transfer(
                        argv,
                        use_password=use_password,
                        log=None,
                        total_size=total,
                        timeout=6 * 3600,
                        progress=progress,
                    )
                    if res.ok:
                        if progress:
                            progress(100, total, total, "")
                        if dest == tmp:
                            placed = _place_from_tmp()
                            if not placed.ok:
                                return placed
                        if log:
                            log("上传完成")
                        ok = True
                        return ExecResult(0, f"uploaded via scp -> {remote_path}", "")
                    if log and dest == remote_path:
                        log("scp 直达失败，改临时目录…")

            # —— 3) Paramiko SFTP 回退 ——
            if log:
                log("rsync/scp 不可用，回退 SFTP（较慢）…")
            client = self.ensure()
            last_pct = [-1]

            def _progress(transferred: int, _total: int) -> None:
                if total <= 0:
                    return
                pct = int(transferred * 100 / total)
                if pct != last_pct[0] and (pct == 100 or pct >= last_pct[0] + 1):
                    last_pct[0] = pct
                    if progress:
                        progress(pct, transferred, total, "")

            try:
                import paramiko.common as pcommon

                transport = client.get_transport()
                if transport is not None:
                    transport.default_window_size = pcommon.MAX_WINDOW_SIZE
                    transport.packetizer.REKEY_BYTES = pow(2, 40)  # type: ignore[attr-defined]
                    transport.packetizer.REKEY_PACKETS = pow(2, 40)  # type: ignore[attr-defined]
            except Exception:
                pass

            def _sftp_put(dest_tmp: str) -> None:
                sftp = client.open_sftp()
                try:
                    try:
                        sftp.get_channel().settimeout(None)  # type: ignore[union-attr]
                    except Exception:
                        pass
                    sftp.put(str(local), dest_tmp, callback=_progress)
                finally:
                    try:
                        sftp.close()
                    except Exception:
                        pass

            _sftp_put(tmp)
            placed = _place_from_tmp()
            if placed.ok:
                ok = True
                if log:
                    log("上传完成")
            return placed
        finally:
            if on_end:
                on_end(ok)
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
        from core.netinfo import get_wired_ipv4

        return get_wired_ipv4() or "127.0.0.1"
