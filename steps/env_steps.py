"""环境配置相关步骤（SSH 在域控宿主机 / Docker 内执行）。"""
from __future__ import annotations

import json
import textwrap

from core.context import AppContext
from steps.base import LogFn, StepResult, TestStep, shell_ok


class DockerSetupStep(TestStep):
    """原文档：加入 docker 组 → 写 daemon.json → 重启 docker，合并为一步。"""

    def __init__(self) -> None:
        super().__init__(
            id="env_docker_setup",
            title="配置 Docker（用户组 / 镜像源 / 重启）",
            description=(
                "1) usermod -aG docker  2) 写入 daemon.json（registry-mirrors / nvidia）"
                "  3) systemctl daemon-reload && restart docker"
            ),
            category="env",
            dangerous=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        logs: list[str] = []
        user = ctx.dc.get("user", "anyverse")

        log("—— 1/3 加入 docker 用户组 ——")
        res1 = ctx.ssh.exec_host(
            f"sudo usermod -aG docker {user} && groups {user}",
            log=log,
            timeout=60,
        )
        logs.append(res1.combined)
        if not res1.ok:
            return StepResult(False, f"加入 docker 组失败 (exit={res1.exit_code})", "\n".join(logs))

        log("—— 2/3 配置 Docker 仓库镜像 ——")
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
        res2 = ctx.ssh.write_remote_file(path, content, sudo=True, log=log)
        logs.append(res2.combined)
        if not res2.ok:
            return StepResult(False, f"写入 daemon.json 失败: {res2.combined}", "\n".join(logs))

        log("—— 3/3 重启 Docker ——")
        res3 = ctx.ssh.exec_host(
            "sudo systemctl daemon-reload && sudo systemctl restart docker && sudo systemctl is-active docker",
            log=log,
            timeout=120,
        )
        logs.append(res3.combined)
        if not res3.ok:
            return StepResult(False, f"重启 Docker 失败 (exit={res3.exit_code})", "\n".join(logs))

        return StepResult(
            True,
            "Docker 用户组、镜像源已配置并已重启（用户组需重新登录/newgrp 后完全生效）",
            "\n".join(logs),
        )


class GitlabSshStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="env_gitlab_ssh",
            title="验证 GitLab SSH 密钥",
            description="ssh -T git@gitlab.anyverse.work，期望 Welcome to GitLab",
            category="env",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        # GitLab 成功时 exit code 往往非 0，靠欢迎语判断
        res = ctx.ssh.exec_host(
            "ssh -o StrictHostKeyChecking=accept-new -T git@gitlab.anyverse.work 2>&1 || true",
            log=log,
            timeout=60,
        )
        text = res.combined
        ok = "Welcome to GitLab" in text or "authenticated" in text.lower()
        return StepResult(
            ok=ok,
            message="密钥有效" if ok else "未检测到 Welcome 提示，请检查密钥",
            log=text,
        )


class CloneCodeStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="env_clone",
            title="首次拉取代码",
            description="git clone -b branch --recurse-submodules（已存在则跳过）",
            category="env",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        git = ctx.config.get("git", {})
        parent = git.get("host_clone_parent", "/home/anyverse/work")
        repo = git.get("repo", "git@gitlab.anyverse.work:dev/anyverse.git")
        branch = git.get("branch", "dev/pnc")
        work = ctx.dc.get("host_work_dir", f"{parent}/anyverse")
        cmd = textwrap.dedent(
            f"""
            set -e
            if [ -d "{work}/.git" ]; then
              echo "代码已存在: {work}"
              cd "{work}" && git rev-parse --abbrev-ref HEAD && git status -sb | head -5
            else
              mkdir -p "{parent}"
              cd "{parent}"
              git clone -b "{branch}" --recurse-submodules "{repo}"
            fi
            """
        ).strip()
        return shell_ok(ctx, cmd, log, timeout=3600)


class CameraInitStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="env_camera_init",
            title="相机初始化配置",
            description="宿主机执行 anyverse_config_init.sh（仅一次，必须成功）",
            category="env",
            dangerous=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        work = ctx.dc.get("host_work_dir", "/home/anyverse/work/anyverse")
        cmd = f"cd {work} && sudo ./script/anyverse_config_init.sh"
        return shell_ok(ctx, cmd, log, timeout=600)


class DockerRunStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="env_docker_run",
            title="Docker 拉取 / 启动容器",
            description="checkout 分支后执行 docker/docker_run.sh（非交互：优先已有容器）",
            category="env",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        work = ctx.dc.get("host_work_dir", "/home/anyverse/work/anyverse")
        branch = ctx.config.get("git", {}).get("branch", "dev/pnc")
        # 交互脚本难以自动化：若已有运行容器则直接通过；否则尝试常见非交互参数
        check = ctx.ssh.exec_host(
            "docker ps --format '{{.Names}}' | head -5",
            log=log,
            timeout=30,
        )
        running = [x.strip().strip("'") for x in check.stdout.splitlines() if x.strip()]
        if running:
            ctx.ssh.container_name = ctx.ssh.container_name or running[0]
            log(f"已有运行中容器: {', '.join(running)}")
            return StepResult(True, f"容器已运行 ({running[0]})", check.combined)

        cmd = textwrap.dedent(
            f"""
            set -e
            cd "{work}"
            git checkout "{branch}" || true
            # 尝试非交互；若脚本仅支持菜单，请人工在域控执行 bash docker/docker_run.sh 选 2
            if grep -qE 'non-interactive|NONINTERACTIVE|--yes|-y' docker/docker_run.sh 2>/dev/null; then
              NONINTERACTIVE=1 bash docker/docker_run.sh || bash docker/docker_run.sh --yes || true
            else
              echo "WARN: docker_run.sh 可能为交互式菜单。请在域控终端手动执行:"
              echo "  cd {work} && bash docker/docker_run.sh"
              echo "选择 2 重建容器后，在本上位机点「连接」再继续。"
              exit 2
            fi
            docker ps --format '{{{{.Names}}}}\\t{{{{.Status}}}}'
            """
        ).strip()
        res = ctx.ssh.exec_host(cmd, log=log, timeout=3600)
        if res.exit_code == 2:
            return StepResult(
                False,
                "需人工在域控执行 docker_run.sh（交互菜单）",
                res.combined,
                needs_manual_confirm=True,
            )
        return StepResult(res.ok, "成功" if res.ok else "失败", res.combined)


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
            description="编译 ros2_ws_4xx / ros2_ws_img_trans",
            category="env",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        cmds = [
            "./script/build.sh -s sub_modules/sal/ros2_ws_4xx/src/ -c",
            "./script/build.sh -s sub_modules/sal/ros2_ws_img_trans/src/ -c",
        ]
        logs = []
        for c in cmds:
            res = ctx.ssh.exec_docker(c, log=log, timeout=3600)
            logs.append(res.combined)
            if not res.ok:
                log("首包失败时可尝试: ./script/build.sh -p realsense2_camera_msgs")
                return StepResult(False, f"编译失败: {c}", "\n".join(logs))
        return StepResult(True, "相机包编译完成", "\n".join(logs))


class HardwareBuildStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="env_hw_build",
            title="本体硬件包编译",
            description="hardware_integration / marvin_dual_arm / monkey_chassis / moveit 包",
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
        # EtherCAT 环境变量提示
        log("提示: EtherCAT 相关编译可 export C_INCLUDE_PATH / LD_LIBRARY_PATH（见文档）")
        return StepResult(True, "硬件相关包编译完成", "\n".join(logs))


class MockGripperStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="env_mock_gripper",
            title="末端未装：夹爪改为 Mock",
            description="hw_params.yaml 中 left/right gripper plugin → mock_components/GenericSystem",
            category="env",
            needs_manual=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        rel = ctx.config.get("moveit", {}).get(
            "hw_params_relpath",
            "src/pnc/config/moveit_config/kitt_1_5_robot_with_zx90d_moveit/config/kitt_1_5_robot_with_zx90d_hw_params.yaml",
        )
        work = ctx.dc.get("host_work_dir", "/home/anyverse/work/anyverse")
        path = f"{work}/{rel}"
        # 在宿主机改文件（通常挂载进容器）
        py = textwrap.dedent(
            r"""
            import pathlib, re, sys
            p = pathlib.Path(sys.argv[1])
            if not p.exists():
                print(f"MISSING:{p}")
                sys.exit(1)
            text = p.read_text(encoding="utf-8")
            def repl_block(name, text):
                # 将对应 gripper 块内 plugin 改为 mock
                pattern = rf"({name}:\n(?:.*\n)*?\s*)plugin:\s*.*"
                new, n = re.subn(
                    pattern,
                    rf"\1plugin: mock_components/GenericSystem",
                    text,
                    count=1,
                )
                return new if n else text
            text2 = repl_block("left_gripper", text)
            text2 = repl_block("right_gripper", text2)
            p.write_text(text2, encoding="utf-8")
            print("UPDATED")
            print(p.read_text(encoding="utf-8")[-800:])
            """
        ).strip()
        # 上传临时脚本执行
        res_w = ctx.ssh.write_remote_file("/tmp/k15_mock_gripper.py", py + "\n", sudo=False, log=log)
        if not res_w.ok:
            return StepResult(False, "上传脚本失败", res_w.combined)
        res = ctx.ssh.exec_host(f"python3 /tmp/k15_mock_gripper.py '{path}'", log=log, timeout=60)
        ok = res.ok and "UPDATED" in res.combined
        return StepResult(
            ok=ok,
            message="已改为 Mock，请确认 YAML 后点「人工确认通过」" if ok else "修改失败或文件不存在",
            log=res.combined,
            needs_manual_confirm=True,
        )


ENV_STEPS = [
    DockerSetupStep(),
    GitlabSshStep(),
    CloneCodeStep(),
    CameraInitStep(),
    DockerRunStep(),
    CameraSnStep(),
    CameraBuildStep(),
    HardwareBuildStep(),
    MockGripperStep(),
]
