"""环境配置相关步骤（SSH 在域控宿主机 / Docker 内执行）。"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

from core.context import AppContext
from steps.base import LogFn, StepResult, TestStep, shell_ok


class DockerSetupStep(TestStep):
    """加入 docker 组 → 刷新会话组权限 → 写 daemon.json → 重启 docker → 授权 sock。"""

    def __init__(self) -> None:
        super().__init__(
            id="env_docker_setup",
            title="配置 Docker（用户组 / 镜像源 / 重启）",
            description=(
                "1) usermod -aG docker + 重连 SSH"
                "  2) 写入 daemon.json（registry-mirrors / nvidia）"
                "  3) systemctl restart docker"
                "  4) 给当前用户 ACL 授权 docker.sock（已有终端无需 newgrp 即可用 docker）"
            ),
            category="env",
            dangerous=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        logs: list[str] = []
        user = ctx.dc.get("user", "nvidia") or "nvidia"

        log("—— 1/5 加入 docker 用户组 ——")
        # usermod 只改帐号配置；当前 SSH 会话的补充组在连接时已固定，不会自动更新。
        # 交互终端里的 newgrp docker 会开新 shell；非交互 SSH 里 newgrp 无法刷新后续 exec，
        # 因此这里用「重连 SSH」达到与 newgrp / 重新登录相同的效果。
        res1 = ctx.ssh.exec_host(
            f"{ctx.ssh._sudo_cmd(f'usermod -aG docker {user}')} && getent group docker && id {user}",
            log=log,
            timeout=60,
            stream_output=False,
        )
        logs.append(res1.combined)
        if not res1.ok:
            return StepResult(False, f"加入 docker 组失败 (exit={res1.exit_code})", "\n".join(logs))
        # getent group docker → docker:x:979:nvidia
        getent_lines = [ln for ln in res1.combined.splitlines() if ln.startswith("docker:")]
        if not getent_lines or user not in getent_lines[-1].split(":")[-1].split(","):
            # 兼容 members 字段含空格等情况：只要行里同时有 docker 与用户名
            if not any(ln.startswith("docker:") and user in ln for ln in res1.combined.splitlines()):
                return StepResult(False, f"usermod 后未在 docker 组中看到 {user}", "\n".join(logs))
        log(f"已将用户 {user} 加入 docker 组")

        log("—— 2/5 重连 SSH，使上位机会话立即带上 docker 组 ——")
        try:
            ctx.ssh.connect(log=log)
        except Exception as exc:  # noqa: BLE001
            return StepResult(False, f"重连 SSH 失败: {exc}", "\n".join(logs))
        # 校验「当前会话」组（id/groups），不要用 groups <user>（那只看帐号配置）
        res_id = ctx.ssh.exec_host("id; groups", log=log, timeout=30, stream_output=False)
        logs.append(res_id.combined)
        if "docker" not in res_id.combined:
            return StepResult(
                False,
                "重连后当前会话仍无 docker 组，请检查 /etc/group 与 SSH 登录组刷新",
                "\n".join(logs),
            )
        log("上位机 SSH 会话已包含 docker 组")

        log("—— 3/5 配置 Docker 仓库镜像 ——")
        d = ctx.config.get("docker", {})
        payload = {
            "registry-mirrors": d.get("registry_mirrors", []),
            "insecure-registries": d.get("insecure_registries", []),
            "runtimes": {
                "nvidia": {
                    "path": "nvidia-container-runtime",
                    "runtimeArgs": [],
                }
            },
        }
        content = json.dumps(payload, indent=4, ensure_ascii=False) + "\n"
        path = d.get("daemon_json_path", "/etc/docker/daemon.json")
        res2 = ctx.ssh.write_remote_file(path, content, sudo=True, log=None)
        logs.append(res2.combined)
        if not res2.ok:
            return StepResult(False, f"写入 daemon.json 失败: {res2.combined}", "\n".join(logs))
        log("daemon.json 已更新")

        log("—— 4/5 写入 docker.sock ACL 持久化（重启 Docker 后仍生效）——")
        # 远程已打开的终端不会自动获得新 group；仅靠 usermod+newgrp 对「当前已登录 shell」无效。
        # 给该用户 ACL 写 docker.sock，无需 newgrp / 重新登录即可 docker_run.sh。
        dropin_dir = "/etc/systemd/system/docker.service.d"
        dropin_path = f"{dropin_dir}/k15-sock-acl.conf"
        dropin_body = textwrap.dedent(
            f"""\
            [Service]
            # K15 上位机：Docker 重启后为 {user} 恢复 sock 访问（无需 newgrp）
            ExecStartPost=-/bin/sh -c '/usr/bin/setfacl -m u:{user}:rw /var/run/docker.sock 2>/dev/null || chmod 666 /var/run/docker.sock'
            """
        )
        res_drop = ctx.ssh.write_remote_file(dropin_path, dropin_body, sudo=True, log=None)
        if not res_drop.ok:
            # 目录可能不存在，先创建再写
            ctx.ssh.exec_host(
                ctx.ssh._sudo_cmd(f"mkdir -p {dropin_dir}"),
                log=None,
                timeout=30,
                stream_output=False,
            )
            res_drop = ctx.ssh.write_remote_file(dropin_path, dropin_body, sudo=True, log=None)
        logs.append(res_drop.combined)
        if not res_drop.ok:
            return StepResult(False, f"写入 docker ACL drop-in 失败: {res_drop.combined}", "\n".join(logs))
        log("已配置 docker.service ExecStartPost ACL")

        log("—— 5/5 重启 Docker、授权 sock 并验证 ——")
        res3 = ctx.ssh.exec_host(
            f"{ctx.ssh._sudo_cmd('systemctl daemon-reload')} && "
            f"{ctx.ssh._sudo_cmd('systemctl restart docker')} && "
            f"{ctx.ssh._sudo_cmd('systemctl is-active docker')}",
            log=log,
            timeout=120,
            stream_output=False,
        )
        logs.append(res3.combined)
        if not res3.ok:
            return StepResult(False, f"重启 Docker 失败 (exit={res3.exit_code})", "\n".join(logs))
        log("Docker 服务已重启")

        # 立即再授一次权（防止 ExecStartPost 时机早于 sock 创建）
        acl_cmd = (
            ctx.ssh._sudo_cmd(
                f"sh -c 'if command -v setfacl >/dev/null; then "
                f"setfacl -m u:{user}:rw /var/run/docker.sock; "
                f"else chmod 666 /var/run/docker.sock; fi; "
                f"ls -l /var/run/docker.sock; getfacl -p /var/run/docker.sock 2>/dev/null | head -20 || true'"
            )
        )
        res_acl = ctx.ssh.exec_host(acl_cmd, log=log, timeout=30, stream_output=False)
        logs.append(res_acl.combined)
        if not res_acl.ok:
            return StepResult(False, f"授权 docker.sock 失败: {res_acl.combined}", "\n".join(logs))
        log(f"已为用户 {user} 授权 /var/run/docker.sock")

        # 模拟「尚未 newgrp 的旧会话」：用 sg 去掉 docker 组后仍应能访问（依赖 ACL）
        # 同时验证当前会话 docker ps
        res4 = ctx.ssh.exec_host(
            "docker ps >/dev/null && echo 'DOCKER_OK_WITHOUT_SUDO'",
            log=log,
            timeout=60,
            stream_output=False,
        )
        logs.append(res4.combined)
        if not res4.ok or "DOCKER_OK_WITHOUT_SUDO" not in res4.combined:
            return StepResult(
                False,
                "docker 仍无法免 sudo 使用，请检查 /var/run/docker.sock 权限与 ACL",
                "\n".join(logs),
            )
        log("已验证：无需 sudo 即可使用 docker（远程已有终端也无需再 newgrp）")

        return StepResult(
            True,
            "Docker 已配置：用户组 + 镜像源 + sock ACL（远程终端可直接 docker_run.sh）",
            "\n".join(logs),
        )


class EnvPackageDeployStep(TestStep):
    """选择本机环境压缩包，上传到域控并完成密钥/代码/镜像/udev 部署。"""

    TOTAL_STEPS = 7

    def __init__(self) -> None:
        super().__init__(
            id="env_package_deploy",
            title="上传部署环境压缩包",
            description=(
                "手动选择本机 K15_env_con.tar.gz（含：密钥、anyverse-dev-pnc.tar.gz、"
                "docker_arm_*.tar.gz、99-EtherCAT.rules）→ 上传域控 → 解压 → "
                "安装 SSH 密钥与 GitLab known_hosts、代码到 ~/work、docker load 镜像、"
                "安装 udev 规则并 reload/trigger"
            ),
            category="env",
            dangerous=True,
        )

    def _stage(self, log: LogFn, index: int, title: str) -> None:
        log(f"—— {index}/{self.TOTAL_STEPS} {title} ——")

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        logs: list[str] = []
        local_path = (ctx.local_package_path or "").strip()
        if not local_path:
            cfg = ctx.config.get("env_package") or {}
            local_path = str(cfg.get("local_path") or "").strip()
        local = Path(local_path)
        if not local.is_file():
            return StepResult(False, "未选择有效的本机压缩包，请先在文件对话框中选择", "")

        user = ctx.dc.get("user", "nvidia") or "nvidia"
        home = f"/home/{user}"
        work_dir = f"{home}/work"
        staging_root = f"{home}/k15_env_staging"
        remote_archive = f"{staging_root}/{local.name}"
        extract_dir = f"{staging_root}/extract"

        # —— 1 上传 ——
        self._stage(log, 1, "上传环境压缩包到域控")
        size_gb = local.stat().st_size / (1024**3)
        log(f"文件: {local.name}（约 {size_gb:.1f} GB），上传中请耐心等待")
        try:
            up = ctx.ssh.upload_local_file(
                str(local),
                remote_archive,
                log=log,
                progress=ctx.on_upload_progress,
                on_begin=ctx.on_upload_begin,
                on_end=ctx.on_upload_end,
            )
        except Exception as exc:  # noqa: BLE001
            return StepResult(False, f"上传失败: {exc}", str(exc))
        logs.append(up.combined)
        if not up.ok:
            return StepResult(False, f"上传失败 (exit={up.exit_code})", "\n".join(logs))

        # —— 2 解压外层包 ——
        self._stage(log, 2, "在域控解压环境压缩包")
        log("正在解压（大文件可能需要数分钟）…")
        extract_cmd = textwrap.dedent(
            f"""
            set -e
            rm -rf "{extract_dir}"
            mkdir -p "{extract_dir}"
            tar -xzf "{remote_archive}" -C "{extract_dir}"
            echo OK
            """
        ).strip()
        res_ex = ctx.ssh.exec_host(extract_cmd, log=log, timeout=7200, stream_output=False)
        logs.append(res_ex.combined)
        if not res_ex.ok:
            return StepResult(False, f"解压失败 (exit={res_ex.exit_code})", "\n".join(logs))
        log("解压完成")

        # —— 3 安装 SSH 密钥 ——
        self._stage(log, 3, "安装 SSH 密钥")
        git_repo = str((ctx.config.get("git") or {}).get("repo") or "")
        git_host = "gitlab.anyverse.work"
        if git_repo.startswith("git@"):
            # git@host:group/repo.git
            git_host = git_repo.split("@", 1)[1].split(":", 1)[0] or git_host
        elif "://" in git_repo:
            # ssh://git@host/… 或 https://host/…
            rest = git_repo.split("://", 1)[1]
            rest = rest.split("@")[-1]
            git_host = rest.split("/")[0].split(":")[0] or git_host
        key_cmd = textwrap.dedent(
            f"""
            set -e
            KEY_DIR=$(find "{extract_dir}" -maxdepth 2 -type d \\( -name '密钥' -o -iname 'ssh*' -o -iname '*key*' \\) | head -1)
            if [ -z "$KEY_DIR" ]; then
              if ls "{extract_dir}"/id_* >/dev/null 2>&1; then
                KEY_DIR="{extract_dir}"
              else
                echo "ERROR: 未找到密钥目录"
                exit 1
              fi
            fi
            mkdir -p "{home}/.ssh"
            cp -a "$KEY_DIR"/. "{home}/.ssh/"
            chmod 700 "{home}/.ssh"
            find "{home}/.ssh" -type f -name 'id_*' ! -name '*.pub' -exec chmod 600 {{}} +
            find "{home}/.ssh" -type f -name '*.pub' -exec chmod 644 {{}} +
            # 预写入 GitLab host key，避免编译时并行 git fetch 卡在 yes/no 提示
            touch "{home}/.ssh/known_hosts"
            chmod 644 "{home}/.ssh/known_hosts"
            if ! grep -qF "{git_host}" "{home}/.ssh/known_hosts" 2>/dev/null; then
              ssh-keyscan -H "{git_host}" >> "{home}/.ssh/known_hosts" 2>/dev/null || true
            fi
            if ! grep -qF "{git_host}" "{home}/.ssh/known_hosts" 2>/dev/null; then
              echo "WARN: ssh-keyscan {git_host} 未写入 known_hosts（网络或 DNS 可能不可达）"
            else
              echo "KNOWN_HOSTS_OK {git_host}"
            fi
            chown -R "{user}:{user}" "{home}/.ssh" 2>/dev/null || true
            echo OK
            """
        ).strip()
        res_key = ctx.ssh.exec_host(key_cmd, log=log, timeout=120, stream_output=False)
        logs.append(res_key.combined)
        if not res_key.ok:
            err = res_key.combined.strip() or f"exit={res_key.exit_code}"
            return StepResult(False, f"安装密钥失败: {err}", "\n".join(logs))
        if "KNOWN_HOSTS_OK" in res_key.combined:
            log(f"SSH 密钥已安装，known_hosts 已加入 {git_host}")
        else:
            log(f"SSH 密钥已安装；警告: 未能写入 {git_host} 到 known_hosts，编译时可能出现 SSH 确认提示")

        # —— 4 解压代码包 ——
        self._stage(log, 4, "解压代码包到 work 目录")
        log("正在解压 anyverse-dev-pnc…")
        code_cmd = textwrap.dedent(
            f"""
            set -e
            CODE_TGZ=$(find "{extract_dir}" -maxdepth 2 -type f -name 'anyverse-dev-pnc*.tar.gz' | head -1)
            if [ -z "$CODE_TGZ" ]; then
              CODE_TGZ=$(find "{extract_dir}" -maxdepth 2 -type f -name 'anyverse*.tar.gz' ! -name 'docker*' | head -1)
            fi
            if [ -z "$CODE_TGZ" ]; then
              echo "ERROR: 未找到 anyverse-dev-pnc.tar.gz"
              exit 1
            fi
            mkdir -p "{work_dir}"
            tar -xzf "$CODE_TGZ" -C "{work_dir}"
            echo OK
            """
        ).strip()
        res_code = ctx.ssh.exec_host(code_cmd, log=log, timeout=7200, stream_output=False)
        logs.append(res_code.combined)
        if not res_code.ok:
            err = res_code.combined.strip() or f"exit={res_code.exit_code}"
            return StepResult(False, f"解压代码包失败: {err}", "\n".join(logs))
        log("代码包解压完成")

        # —— 5 加载 Docker 镜像 ——
        self._stage(log, 5, "加载 Docker 镜像")
        log("正在 docker load（大镜像耗时较长）…")
        if ctx.ssh.password:
            esc = ctx.ssh.password.replace("'", "'\"'\"'")
            sudo_fn = (
                f"sudo_run() {{ printf '%s\\n' '{esc}' | sudo -S -p '' \"$@\"; }}"
            )
        else:
            sudo_fn = 'sudo_run() { sudo -n "$@"; }'
        docker_cmd = textwrap.dedent(
            f"""
            set -e
            {sudo_fn}
            IMG=$(find "{extract_dir}" -maxdepth 2 -type f \\( -name 'docker_arm*.tar.gz' -o -name 'docker_arm*.tar' -o -name 'docker*.tar.gz' \\) | head -1)
            if [ -z "$IMG" ]; then
              IMG_DIR=$(find "{extract_dir}" -maxdepth 2 -type d -name 'docker_arm*' | head -1)
              if [ -n "$IMG_DIR" ]; then
                IMG=$(find "$IMG_DIR" -maxdepth 2 -type f \\( -name '*.tar.gz' -o -name '*.tar' \\) | head -1)
              fi
            fi
            if [ -z "$IMG" ]; then
              echo "ERROR: 未找到 docker_arm 镜像压缩包"
              exit 1
            fi
            do_load() {{
              local img="$1"
              if docker load -i "$img" >/tmp/k15_docker_load.out 2>/tmp/k15_docker_load.err; then
                return 0
              fi
              if [[ "$img" == *.tar.gz ]] || [[ "$img" == *.tgz ]]; then
                if sudo_run docker load -i "$img" >/tmp/k15_docker_load.out 2>/tmp/k15_docker_load.err; then
                  return 0
                fi
                gunzip -c "$img" | sudo_run docker load >/tmp/k15_docker_load.out 2>/tmp/k15_docker_load.err
              else
                sudo_run docker load -i "$img" >/tmp/k15_docker_load.out 2>/tmp/k15_docker_load.err
              fi
            }}
            if [[ "$IMG" == *.tar.gz ]] || [[ "$IMG" == *.tgz ]]; then
              if ! do_load "$IMG"; then
                gunzip -c "$IMG" | docker load >/tmp/k15_docker_load.out 2>/tmp/k15_docker_load.err \\
                  || gunzip -c "$IMG" | sudo_run docker load >/tmp/k15_docker_load.out 2>/tmp/k15_docker_load.err
              fi
            else
              do_load "$IMG"
            fi
            echo OK
            """
        ).strip()
        res_docker = ctx.ssh.exec_host(docker_cmd, log=log, timeout=14400, stream_output=False)
        logs.append(res_docker.combined)
        if not res_docker.ok:
            return StepResult(
                False,
                f"Docker 镜像部署失败 (exit={res_docker.exit_code})",
                "\n".join(logs),
            )
        log("Docker 镜像加载完成")

        # —— 6 安装 udev 规则 ——
        self._stage(log, 6, "安装 EtherCAT udev 规则")
        sudo_install = ctx.ssh._sudo_cmd(
            "sh -c 'cp \"$1\" /etc/udev/rules.d/99-EtherCAT.rules && chmod 644 /etc/udev/rules.d/99-EtherCAT.rules' _ \"$RULE\""
        )
        udev_install = textwrap.dedent(
            f"""
            set -e
            RULE=$(find "{extract_dir}" -maxdepth 2 -type f -name '99-EtherCAT.rules' | head -1)
            if [ -z "$RULE" ]; then
              RULE=$(find "{extract_dir}" -maxdepth 2 -type f -name '*EtherCAT*.rules' | head -1)
            fi
            if [ -z "$RULE" ]; then
              echo "ERROR: 未找到 99-EtherCAT.rules"
              exit 1
            fi
            {sudo_install}
            echo OK
            """
        ).strip()
        res_udev = ctx.ssh.exec_host(udev_install, log=log, timeout=120, stream_output=False)
        logs.append(res_udev.combined)
        if not res_udev.ok:
            err = res_udev.combined.strip() or f"exit={res_udev.exit_code}"
            return StepResult(False, f"安装 udev 规则失败: {err}", "\n".join(logs))
        log("udev 规则已安装")

        # —— 7 生效 udev ——
        self._stage(log, 7, "生效 udev 规则")
        reload_cmd = (
            ctx.ssh._sudo_cmd("udevadm control --reload-rules")
            + " && "
            + ctx.ssh._sudo_cmd("udevadm trigger")
            + " && echo OK"
        )
        res_reload = ctx.ssh.exec_host(reload_cmd, log=log, timeout=120, stream_output=False)
        logs.append(res_reload.combined)
        if not res_reload.ok:
            return StepResult(False, f"udev 生效失败 (exit={res_reload.exit_code})", "\n".join(logs))
        log("udev 规则已生效")

        log("全部子步骤完成")
        return StepResult(
            True,
            "环境压缩包已部署：密钥 / 代码 / Docker 镜像 / EtherCAT udev 规则",
            "\n".join(logs),
        )


class CameraInitStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="env_camera_init",
            title="相机初始化配置",
            description=(
                "若 agent_dev_nvidia 在运行则先 docker stop → "
                "anyverse_config_init.sh → docker_run.sh 选 2 重建并启动容器"
            ),
            category="env",
            dangerous=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        work = ctx.dc.get("host_work_dir", "/home/nvidia/work/anyverse")
        cname = "agent_dev_nvidia"
        logs: list[str] = []

        log(f"检查容器是否在运行: {cname}")
        check = ctx.ssh.exec_host(
            f'docker ps --format "{{{{.Names}}}}" | grep -Fx "{cname}" '
            f'&& echo RUNNING || echo NOT_RUNNING',
            log=log,
            timeout=60,
            stream_output=False,
        )
        logs.append(check.combined)

        if "RUNNING" in check.combined:
            log(f"容器 {cname} 正在运行，先停止…")
            stop = ctx.ssh.exec_host(
                f'docker stop "{cname}"',
                log=log,
                timeout=180,
                stream_output=True,
            )
            logs.append(stop.combined)
            if not stop.ok:
                return StepResult(
                    False,
                    f"停止容器 {cname} 失败 (exit={stop.exit_code})",
                    "\n".join(logs),
                )
            log(f"容器 {cname} 已停止")
        else:
            log(f"容器 {cname} 未在运行，直接执行初始化脚本")

        log("执行 anyverse_config_init.sh …")
        cmd = f'cd "{work}" && sudo ./script/anyverse_config_init.sh'
        res = ctx.ssh.exec_host(cmd, log=log, timeout=600, stream_output=True)
        logs.append(res.combined)
        if not res.ok:
            return StepResult(
                False,
                f"相机初始化失败 (exit={res.exit_code})",
                "\n".join(logs),
            )

        # docker_run.sh 为交互菜单：选 2 = 重建容器并启动
        log("执行 docker_run.sh（菜单选项 2：重建并启动容器）…")
        run_cmd = f'cd "{work}" && printf "2\\n" | bash ./docker/docker_run.sh'
        run = ctx.ssh.exec_host(
            run_cmd,
            log=log,
            timeout=900,
            stream_output=True,
            get_pty=True,
        )
        logs.append(run.combined)
        if not run.ok:
            return StepResult(
                False,
                f"docker_run.sh 失败 (exit={run.exit_code})",
                "\n".join(logs),
            )

        verify = ctx.ssh.exec_host(
            f'docker ps --format "{{{{.Names}}}}" | grep -Fx "{cname}" '
            f'&& echo UP || echo DOWN',
            log=log,
            timeout=60,
            stream_output=False,
        )
        logs.append(verify.combined)
        if "UP" not in verify.combined:
            return StepResult(
                False,
                f"docker_run 后容器 {cname} 未在运行",
                "\n".join(logs),
            )

        ctx.dc["container_name"] = cname
        ctx.ssh.container_name = cname
        return StepResult(True, f"相机初始化完成，容器 {cname} 已重建并启动", "\n".join(logs))


class CameraSnStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="env_camera_sn",
            title="获取 Camera SN",
            description="容器内 extract_camera_sn.sh；左右手 D405 需人工改 JSON 后确认",
            category="env",
            needs_manual=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        cmd = "sudo ./script/dev/realsense/extract_camera_sn.sh"
        res = ctx.ssh.exec_docker(cmd, log=log, timeout=300)
        follow = ctx.ssh.exec_docker(
            'echo "hostname=$(hostname)"; '
            'f="/etc/anyverse_config/device/$(hostname)_camera.json"; '
            'echo "file=$f"; '
            'if [ -f "$f" ]; then sudo cat "$f"; else ls -la /etc/anyverse_config/device/ 2>/dev/null || true; fi',
            log=log,
            timeout=60,
        )
        combined = res.combined + "\n" + follow.combined
        return StepResult(
            ok=res.ok,
            message="已生成 SN 文件，请人工区分左右手 D405 后点「人工确认通过」"
            if res.ok
            else "提取 SN 失败",
            log=combined,
            needs_manual_confirm=True,
        )


class CameraBuildStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="env_camera_build",
            title="相机相关代码编译",
            description=(
                "依次编译 realsense2_camera_msgs → ros2_ws_4xx → ros2_ws_img_trans"
            ),
            category="env",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        cmds = [
            "./script/build.sh -p realsense2_camera_msgs",
            "./script/build.sh -s sub_modules/sal/ros2_ws_4xx/src/ -c",
            "./script/build.sh -s sub_modules/sal/ros2_ws_img_trans/src/ -c",
        ]
        logs: list[str] = []
        for c in cmds:
            res = ctx.ssh.exec_docker(c, log=log, timeout=3600)
            logs.append(res.combined)
            if not res.ok:
                return StepResult(False, f"编译失败: {c}", "\n".join(logs))
        return StepResult(True, "相机包编译完成", "\n".join(logs))


class HardwareBuildStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="env_hw_build",
            title="本体硬件包编译",
            description=(
                "编译 hardware_integration / marvin_dual_arm / monkey_chassis / moveit；"
                "完成后设置 EtherCAT 的 C_INCLUDE_PATH / CPLUS_INCLUDE_PATH / LIBRARY_PATH / LD_LIBRARY_PATH"
            ),
            category="env",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        pkgs = [
            "hardware_integration",
            "marvin_dual_arm_hardware",
            "monkey_chassis",
            "kitt_1_5_robot_with_zx90d_moveit",
        ]
        logs = []
        for p in pkgs:
            cmd = f"./script/build.sh -s src -p {p}"
            res = ctx.ssh.exec_docker(cmd, log=log, timeout=3600)
            logs.append(res.combined)
            if not res.ok:
                return StepResult(False, f"编译失败: {p}", "\n".join(logs))

        log("设置 EtherCAT 环境变量…")
        ethercat_env = textwrap.dedent(
            r"""
            set -e
            export C_INCLUDE_PATH=/anyverse/sub_modules/sal/ethercat/include:$C_INCLUDE_PATH
            export CPLUS_INCLUDE_PATH=/anyverse/sub_modules/sal/ethercat/include:$CPLUS_INCLUDE_PATH
            export LIBRARY_PATH=/anyverse/sub_modules/sal/ethercat/lib/.libs:$LIBRARY_PATH
            export LD_LIBRARY_PATH=/anyverse/sub_modules/sal/ethercat/lib/.libs:$LD_LIBRARY_PATH
            ENV_FILE=/anyverse/.k15_ethercat_env.sh
            printf '%s\n' \
              'export C_INCLUDE_PATH=/anyverse/sub_modules/sal/ethercat/include:$C_INCLUDE_PATH' \
              'export CPLUS_INCLUDE_PATH=/anyverse/sub_modules/sal/ethercat/include:$CPLUS_INCLUDE_PATH' \
              'export LIBRARY_PATH=/anyverse/sub_modules/sal/ethercat/lib/.libs:$LIBRARY_PATH' \
              'export LD_LIBRARY_PATH=/anyverse/sub_modules/sal/ethercat/lib/.libs:$LD_LIBRARY_PATH' \
              > "$ENV_FILE"
            if [ -f "$HOME/.bashrc" ] && ! grep -q 'k15_ethercat_env' "$HOME/.bashrc" 2>/dev/null; then
              echo 'source /anyverse/.k15_ethercat_env.sh 2>/dev/null || true' >> "$HOME/.bashrc"
            fi
            echo "C_INCLUDE_PATH=$C_INCLUDE_PATH"
            echo "CPLUS_INCLUDE_PATH=$CPLUS_INCLUDE_PATH"
            echo "LIBRARY_PATH=$LIBRARY_PATH"
            echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
            echo "ETHERCAT_ENV_OK"
            """
        ).strip()
        res_env = ctx.ssh.exec_docker(ethercat_env, log=log, timeout=60)
        logs.append(res_env.combined)
        if not res_env.ok or "ETHERCAT_ENV_OK" not in res_env.combined:
            return StepResult(
                False,
                f"硬件包已编译，但 EtherCAT 环境变量设置失败 (exit={res_env.exit_code})",
                "\n".join(logs),
            )
        log("EtherCAT 环境变量已设置（并写入 /anyverse/.k15_ethercat_env.sh）")
        return StepResult(True, "硬件相关包编译完成，EtherCAT 环境变量已设置", "\n".join(logs))


class EndEffectorConfigStep(TestStep):
    """双臂末端配置：按是否安装末端设备，切换 gripper plugin。"""

    REAL_PLUGIN = "zx_90d_gripper/Zx90dGripperHardware"
    MOCK_PLUGIN = "mock_components/GenericSystem"
    CONTAINER_PATH = (
        "/anyverse/src/pnc/config/moveit_config/"
        "kitt_1_5_robot_with_zx90d_moveit/config/"
        "kitt_1_5_robot_with_zx90d_hw_params.yaml"
    )

    def __init__(self) -> None:
        super().__init__(
            id="env_end_effector",
            title="双臂末端配置",
            description=(
                "选择已装/未装末端设备后，检查并更新 hw_params.yaml 中 "
                "left_gripper / right_gripper 的 plugin"
            ),
            category="env",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        mode = (ctx.end_effector_mode or "").strip()
        if mode not in ("installed", "not_installed"):
            return StepResult(False, "未选择末端配置方式（已装 / 未装）", "")

        target = self.REAL_PLUGIN if mode == "installed" else self.MOCK_PLUGIN
        mode_cn = "已装末端设备" if mode == "installed" else "未装末端设备"
        log(f"模式: {mode_cn}")
        log(f"目标 plugin: {target}")

        # 注意：外层不要用 f-string，保留脚本内 {var} 给容器内 Python 使用
        py = textwrap.dedent(
            r"""
            import pathlib, re, sys
            path = pathlib.Path(sys.argv[1])
            target = sys.argv[2]
            if not path.exists():
                print("MISSING")
                sys.exit(2)

            raw = path.read_text(encoding="utf-8")
            lines = raw.splitlines(keepends=True)
            blocks = {"left_gripper", "right_gripper"}
            current = None
            current_plugins = {"left_gripper": None, "right_gripper": None}
            plugin_idx = {"left_gripper": None, "right_gripper": None}

            for i, line in enumerate(lines):
                m_key = re.match(r"^([A-Za-z0-9_]+):\s*$", line)
                if m_key:
                    name = m_key.group(1)
                    current = name if name in blocks else None
                    continue
                if current is None:
                    continue
                m_plug = re.match(r"^([ \t]*)plugin:\s*(.+?)\s*$", line)
                if m_plug:
                    current_plugins[current] = m_plug.group(2).strip()
                    plugin_idx[current] = i

            left = current_plugins["left_gripper"]
            right = current_plugins["right_gripper"]
            if left is None or right is None:
                print("ERROR_NO_PLUGIN")
                sys.exit(3)
            if left == target and right == target:
                print("ALREADY_OK")
                sys.exit(0)

            for name in ("left_gripper", "right_gripper"):
                idx = plugin_idx[name]
                line = lines[idx]
                indent = re.match(r"^([ \t]*)", line).group(1)
                nl = "\n" if line.endswith("\n") else ""
                lines[idx] = f"{indent}plugin: {target}{nl}"

            new_text = "".join(lines)
            try:
                path.write_text(new_text, encoding="utf-8")
            except PermissionError:
                import tempfile, os, subprocess
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
                    tf.write(new_text)
                    tmp = tf.name
                subprocess.check_call(["sudo", "cp", tmp, str(path)])
                os.unlink(tmp)

            text2 = path.read_text(encoding="utf-8")

            def read_plugin(src, block):
                cur = None
                for ln in src.splitlines():
                    if re.match(rf"^{block}:\s*$", ln):
                        cur = block
                        continue
                    if cur and re.match(r"^[A-Za-z0-9_]+:\s*$", ln):
                        cur = None
                        continue
                    if cur:
                        m = re.match(r"^[ \t]*plugin:\s*(.+?)\s*$", ln)
                        if m:
                            return m.group(1).strip()
                return None

            nl = read_plugin(text2, "left_gripper")
            nr = read_plugin(text2, "right_gripper")
            if nl != target or nr != target:
                print("ERROR_VERIFY")
                sys.exit(4)
            print("UPDATED")
            """
        ).strip()

        res_w = ctx.ssh.write_remote_file(
            "/tmp/k15_end_effector.py", py + "\n", sudo=False, log=None
        )
        if not res_w.ok:
            return StepResult(False, "上传配置脚本失败", res_w.combined)

        name = ctx.ssh.resolve_container(log=None)
        prep = ctx.ssh.exec_host(
            f"docker cp /tmp/k15_end_effector.py {name}:/tmp/k15_end_effector.py",
            log=None,
            timeout=30,
            stream_output=False,
        )
        if not prep.ok:
            return StepResult(False, "拷贝脚本到容器失败", prep.combined)

        res = ctx.ssh.exec_docker(
            f"python3 /tmp/k15_end_effector.py '{self.CONTAINER_PATH}' '{target}'",
            log=None,
            timeout=60,
            stream_output=False,
        )
        text = res.combined
        if "MISSING" in text:
            return StepResult(False, "配置文件不存在", text)
        if "ERROR_NO_PLUGIN" in text:
            return StepResult(False, "未找到左右夹爪 plugin 配置", text)
        if "ALREADY_OK" in text:
            log("配置已是目标状态，无需修改")
            return StepResult(True, f"{mode_cn}：配置已正确，无需修改", text)
        if "UPDATED" in text:
            log("配置已更新并保存")
            return StepResult(True, f"{mode_cn}：已更新左右夹爪 plugin 为 {target}", text)
        if "ERROR_VERIFY" in text:
            return StepResult(False, f"{mode_cn}：写入后复核失败", text)
        return StepResult(False, f"{mode_cn}：修改失败", text)


ENV_STEPS = [
    DockerSetupStep(),
    EnvPackageDeployStep(),
    CameraInitStep(),
    CameraSnStep(),
    CameraBuildStep(),
    HardwareBuildStep(),
    EndEffectorConfigStep(),
]
