"""生产测试步骤（域控 Docker + 本机 Docker；本机 UI 只看日志）。"""
from __future__ import annotations

import textwrap
import time
from pathlib import Path

from core.config import ROOT
from core.context import AppContext
from core.local_docker import LocalDocker
from core.netinfo import get_wired_ipv4
from steps.base import LogFn, StepResult, TestStep


def _safe_pkill_snippet(pattern: str) -> str:
    """生成不会误杀当前 bash -lc 的进程清理片段。

    pattern 必须用「正则字符类技巧」，使脚本命令行里的 pattern 字面量
    不会匹配到自身，例如::

        /opt/ros/.*/bin/ros2 [l]aunch .*controller[.]launch[.]py
        [p]ython3 /tmp/k15_controller_state_latency6.py

    切勿使用裸 'controller.launch.py'：docker exec 的 bash -lc 命令行
    含该字符串，pgrep 会命中脚本自身并误杀同组进程。
    """
    return textwrap.dedent(
        f"""\
        for _pid in $(pgrep -f '{pattern}' || true); do
          [ "$_pid" = "$$" ] && continue
          [ "$_pid" = "$PPID" ] && continue
          kill "$_pid" 2>/dev/null || true
        done
        """
    ).strip()


def _run_remote_script(ctx: AppContext, script_body: str, script_name: str):
    """把脚本落到容器内再执行，避免 bash -lc 命令行含路径导致 pkill 误杀。"""
    from core.ssh_client import ExecResult

    remote_host = f"/tmp/{script_name}"
    remote_ctr = f"/tmp/{script_name}"
    w = ctx.ssh.write_remote_file(remote_host, script_body + "\n", sudo=False, log=None)
    if not w.ok:
        return ExecResult(1, "", f"写启动脚本失败: {w.combined}")
    name = ctx.ssh.resolve_container(log=None)
    user = (ctx.ssh.container_user or "admin").strip() or "admin"
    cp = ctx.ssh.exec_host(
        f"docker cp {remote_host} {name}:{remote_ctr}",
        log=None,
        timeout=60,
        stream_output=False,
    )
    if not cp.ok:
        return ExecResult(1, "", f"拷贝启动脚本失败: {cp.combined}")
    return ctx.ssh.exec_host(
        f"docker exec -u {user} {name} bash {remote_ctr}",
        log=None,
        timeout=120,
        stream_output=False,
    )


def _run_local_script(local: LocalDocker, script_body: str, script_name: str):
    """本机容器：写临时脚本再 docker exec bash 文件。"""
    import subprocess
    import tempfile
    from pathlib import Path

    from core.ssh_client import ExecResult

    user = (local.container_user or "admin").strip() or "admin"
    ctr_path = f"/tmp/{script_name}"
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".sh") as f:
            f.write(script_body + "\n")
            tmp = f.name
        cp = subprocess.run(
            ["docker", "cp", tmp, f"{local.container_name}:{ctr_path}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        Path(tmp).unlink(missing_ok=True)
        if cp.returncode != 0:
            return ExecResult(cp.returncode, cp.stdout or "", cp.stderr or "")
        run = subprocess.run(
            ["docker", "exec", "-u", user, local.container_name, "bash", ctr_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return ExecResult(run.returncode, run.stdout or "", run.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return ExecResult(1, "", str(exc))


def _stop_joint_latency_procs(ctx: AppContext, local: LocalDocker | None = None) -> str:
    """停止关节时延三路：域控 Controller + Latency，本机 MoveIt。

    注意：
    - 时延进程 cmdline 为 ``python3 -u /tmp/k15_...``，匹配时必须带上 ``-u``；
    - ``pkill -x ros2_control_node`` 在 Linux 上无效（comm 截断 15 字符），改用 ``pgrep -f``。
    """
    local = local or _local_docker(ctx)
    stop_dc = textwrap.dedent(
        """
        #!/bin/bash
        set +e
        _kill_tree() {
          local pid="$1"
          [ -z "$pid" ] && return 0
          # 先杀进程组，再杀 pid
          kill -TERM -"$pid" 2>/dev/null || true
          kill -TERM "$pid" 2>/dev/null || true
          sleep 0.3
          kill -KILL -"$pid" 2>/dev/null || true
          kill -KILL "$pid" 2>/dev/null || true
        }
        _kill_pat() {
          local pat="$1"
          local pid
          for pid in $(pgrep -f "$pat" || true); do
            [ "$pid" = "$$" ] && continue
            [ "$pid" = "$PPID" ] && continue
            _kill_tree "$pid"
          done
        }
        if [ -f /tmp/k15_controller.pid ]; then
          _kill_tree "$(cat /tmp/k15_controller.pid | tr -d '[:space:]')"
          rm -f /tmp/k15_controller.pid
        fi
        if [ -f /tmp/k15_latency.pid ]; then
          _kill_tree "$(cat /tmp/k15_latency.pid | tr -d '[:space:]')"
          rm -f /tmp/k15_latency.pid
        fi
        # launch 父进程
        _kill_pat '/opt/ros/.*/bin/ros2 [l]aunch .*controller[.]launch[.]py'
        # launch 子进程（不可用 pkill -x，名字超过 15 字符）
        _kill_pat 'controller_manager/ros2_control_node'
        _kill_pat '/lib/robot_state_publisher/robot_state_publisher'
        _kill_pat '/controller_manager/spawner'
        # 时延：启动命令含 -u
        _kill_pat '[p]ython3 -u /tmp/k15_controller_state_latency6'
        _kill_pat '[p]ython3 /tmp/k15_controller_state_latency6'
        sleep 0.5
        echo "=== DC remain ==="
        pgrep -af 'controller_manager/ros2_control_node|[p]ython3 .*k15_controller_state_latency6|bin/ros2 [l]aunch .*controller' || echo NONE
        echo STOP_DC_OK
        """
    ).strip()
    dc_out = ""
    try:
        r = _run_remote_script(ctx, stop_dc, "k15_stop_joint_dc.sh")
        dc_out = r.combined or ""
    except Exception as exc:  # noqa: BLE001
        dc_out = f"stop_dc_exc={exc}"

    stop_pc = textwrap.dedent(
        """
        #!/bin/bash
        set +e
        _kill_tree() {
          local pid="$1"
          [ -z "$pid" ] && return 0
          kill -TERM -"$pid" 2>/dev/null || true
          kill -TERM "$pid" 2>/dev/null || true
          sleep 0.3
          kill -KILL -"$pid" 2>/dev/null || true
          kill -KILL "$pid" 2>/dev/null || true
        }
        _kill_pat() {
          local pat="$1"
          local pid
          for pid in $(pgrep -f "$pat" || true); do
            [ "$pid" = "$$" ] && continue
            [ "$pid" = "$PPID" ] && continue
            _kill_tree "$pid"
          done
        }
        if [ -f /tmp/k15_moveit_demo.pid ]; then
          _kill_tree "$(cat /tmp/k15_moveit_demo.pid | tr -d '[:space:]')"
          rm -f /tmp/k15_moveit_demo.pid
        fi
        _kill_pat '/opt/ros/.*/bin/ros2 [l]aunch .*moveit_demo[.]launch[.]py'
        _kill_pat 'moveit_ros_move_group/move_group'
        _kill_pat '/rviz2'
        _kill_pat 'robot_state_publisher/robot_state_publisher'
        sleep 0.5
        echo "=== PC remain ==="
        pgrep -af 'bin/ros2 [l]aunch .*moveit_demo|move_group|rviz2' || echo NONE
        echo STOP_PC_OK
        """
    ).strip()
    pc_out = ""
    try:
        r2 = _run_local_script(local, stop_pc, "k15_stop_joint_pc.sh")
        pc_out = r2.combined or ""
    except Exception as exc:  # noqa: BLE001
        pc_out = f"stop_pc_exc={exc}"
    return f"{dc_out}\n{pc_out}"


def _cyclonedds_xml(own_ip: str, peer_ip: str) -> str:
    """按另一对话结论：用 address 绑定，避免 eth6 多 IP 宣告不可达地址。"""
    return textwrap.dedent(
        f"""\
        <CycloneDDS xmlns="https://cdds.io/config"
            xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
            xsi:schemaLocation="https://cdds.io/config https://raw.githubusercontent.com/eclipse-cyclonedds/cyclonedds/master/etc/cyclonedds.xsd">
            <Domain Id="any">
                <General>
                    <Interfaces>
                        <NetworkInterface address="{own_ip}" priority="1000" multicast="default" />
                    </Interfaces>
                    <AllowMulticast>true</AllowMulticast>
                    <MaxMessageSize>1300B</MaxMessageSize>
                    <DontRoute>true</DontRoute>
                </General>
                <Discovery>
                    <EnableTopicDiscoveryEndpoints>true</EnableTopicDiscoveryEndpoints>
                    <MaxAutoParticipantIndex>200</MaxAutoParticipantIndex>
                    <Peers>
                        <Peer address="{peer_ip}"/>
                    </Peers>
                </Discovery>
                <Internal>
                    <Watermarks>
                        <WhcHigh>500kB</WhcHigh>
                    </Watermarks>
                </Internal>
                <Tracing>
                    <Verbosity>config</Verbosity>
                    <OutputFile>/tmp/cdds.log.${{CYCLONEDDS_PID}}</OutputFile>
                </Tracing>
            </Domain>
        </CycloneDDS>
        """
    )


def _bridge_domain(ctx: AppContext) -> int:
    """跨机联调 domain：优先 ros.bridge_domain_id，否则 domain_id，默认 0。"""
    r = ctx.ros if isinstance(ctx.ros, dict) else {}
    for key in ("bridge_domain_id", "domain_id"):
        if key in r and r[key] is not None and str(r[key]).strip() != "":
            try:
                return int(r[key])
            except (TypeError, ValueError):
                pass
    return 0


def _pc_ip(ctx: AppContext) -> str:
    pc = ctx.config.get("pc") or {}
    ip = str(pc.get("ip") or "").strip()
    if ip:
        return ip
    return (get_wired_ipv4() or "").strip()


def _dc_ip(ctx: AppContext) -> str:
    return str(ctx.dc.get("host") or ctx.ssh.host or "").strip()


def _local_docker(ctx: AppContext) -> LocalDocker:
    pc = ctx.config.get("pc") or {}
    return LocalDocker(
        container_name=str(pc.get("container_name") or "agent_dev_wujie"),
        container_user=str(pc.get("container_user") or "admin"),
        work_dir=str(pc.get("container_work_dir") or "/anyverse"),
    )


def _local_work(ctx: AppContext) -> Path:
    pc = ctx.config.get("pc") or {}
    return Path(str(pc.get("work_dir") or "/home/wujie/work/anyverse"))


def _bridge_env(ctx: AppContext) -> str:
    """跨机联调环境：强制 file:// CYCLONEDDS_URI + bridge domain。"""
    uri = str(ctx.ros.get("cyclonedds_uri") or "/anyverse/config/cyclonedds.xml")
    if uri.startswith("/") and not uri.startswith("file:"):
        uri = f"file://{uri}"
    did = _bridge_domain(ctx)
    return (
        f"export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; "
        f"export CYCLONEDDS_URI={uri}; "
        f"export ROS_DOMAIN_ID={did}; "
        f"export ROS_LOCALHOST_ONLY=0"
    )


class ChassisTopicTestStep(TestStep):
    """编译底盘包后，在容器内运行 sensor_topic_test.py。"""

    LOCAL_SCRIPT = ROOT / "script" / "sensor_topic_test.py"
    REMOTE_SCRIPT = "/tmp/k15_sensor_topic_test.py"
    CHASSIS_DOMAIN_ID = 0
    READY_TOPICS = (
        "/driver/imu/RawData",
        "/driver/lidar/lidar_front/point_cloud/Data",
    )
    READY_WAIT_SEC = 120

    def __init__(self) -> None:
        super().__init__(
            id="test_chassis_topic",
            title="底盘话题测试",
            description=(
                "1) 编译 monkey_chassis_v2  2) 等待驱动 topic（ROS_DOMAIN_ID=0）"
                "  3) 执行 sensor_topic_test.py（日志实时输出）"
            ),
            category="test",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        if not self.LOCAL_SCRIPT.is_file():
            return StepResult(False, f"本机缺少测试脚本: {self.LOCAL_SCRIPT}", "")

        logs: list[str] = []
        domain = int(
            (ctx.ros.get("chassis_domain_id", self.CHASSIS_DOMAIN_ID)
             if isinstance(ctx.ros, dict)
             else self.CHASSIS_DOMAIN_ID)
            or self.CHASSIS_DOMAIN_ID
        )
        env = ctx.ros_env_exports(domain_id=domain)
        source = ctx.source_ws()

        log("—— 1/3 编译底盘话题代码 ——")
        build = ctx.ssh.exec_docker(
            "./script/build.sh -s src/robot_hardwares/monkey_chassis_v2",
            log=log,
            timeout=3600,
            stream_output=True,
        )
        logs.append(build.combined)
        if not build.ok:
            return StepResult(False, f"底盘编译失败 (exit={build.exit_code})", "\n".join(logs))
        log("编译完成")

        log(f"—— 2/3 等待驱动 topic 就绪（ROS_DOMAIN_ID={domain}，最长 {self.READY_WAIT_SEC}s）——")
        topics_check = " ".join(self.READY_TOPICS)
        wait_cmd = textwrap.dedent(
            f"""
            set +e
            {source}
            {env}
            deadline=$(( $(date +%s) + {self.READY_WAIT_SEC} ))
            list=""
            while [ "$(date +%s)" -lt "$deadline" ]; do
              list=$(ros2 topic list 2>/dev/null || true)
              ok=1
              for t in {topics_check}; do
                echo "$list" | grep -Fx "$t" >/dev/null 2>&1 || ok=0
              done
              if [ "$ok" = 1 ]; then
                echo TOPICS_READY
                echo "$list" | grep -E '^/driver/' | head -30
                exit 0
              fi
              sleep 2
            done
            echo TOPICS_TIMEOUT
            echo "--- current topic list (domain={domain}) ---"
            echo "$list"
            exit 1
            """
        ).strip()
        wait = ctx.ssh.exec_docker(
            wait_cmd,
            log=log,
            timeout=self.READY_WAIT_SEC + 60,
            stream_output=True,
        )
        logs.append(wait.combined)
        if "TOPICS_READY" not in wait.combined:
            return StepResult(
                False,
                f"驱动 topic 未就绪（ROS_DOMAIN_ID={domain}）",
                "\n".join(logs),
            )
        log("驱动 topic 已就绪")

        log("—— 3/3 执行底盘话题测试脚本 ——")
        try:
            content = self.LOCAL_SCRIPT.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return StepResult(False, f"读取本机脚本失败: {exc}", "\n".join(logs))

        up = ctx.ssh.write_remote_file(self.REMOTE_SCRIPT, content, sudo=False, log=None)
        if not up.ok:
            return StepResult(False, "上传测试脚本到域控失败", up.combined)

        name = ctx.ssh.resolve_container(log=None)
        cp = ctx.ssh.exec_host(
            f"docker cp {self.REMOTE_SCRIPT} {name}:{self.REMOTE_SCRIPT}",
            log=None,
            timeout=60,
            stream_output=False,
        )
        if not cp.ok:
            return StepResult(False, "拷贝测试脚本到容器失败", cp.combined)

        run_cmd = f"{source} && {env} && python3 {self.REMOTE_SCRIPT} --hz --bw --yes"
        log(f"开始运行 sensor_topic_test.py（ROS_DOMAIN_ID={domain}）…")
        res = ctx.ssh.exec_docker(run_cmd, log=log, timeout=1800, stream_output=True)
        logs.append(res.combined)
        if not res.ok:
            return StepResult(
                False,
                f"底盘话题测试失败 (exit={res.exit_code})",
                "\n".join(logs),
            )
        return StepResult(True, "底盘话题测试完成", "\n".join(logs))


class DdsBridgeStep(TestStep):
    """配置域控↔本机 CycloneDDS，实现 ROS/DDS 互通（测试结束后由恢复步骤还原）。"""

    BAK_DIR_NAME = ".k15_dds_bak"

    def __init__(self) -> None:
        super().__init__(
            id="test_dds_bridge",
            title="域控↔本机 DDS/ROS 互通",
            description=(
                "备份后改写两边 cyclonedds：按 IP address 绑定 + 互为 Peer；"
                "ROS_DOMAIN_ID 对齐；可选 talker/listener 冒烟"
            ),
            category="test",
            dangerous=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        logs: list[str] = []
        pc_ip = _pc_ip(ctx)
        dc_ip = _dc_ip(ctx)
        if not pc_ip or not dc_ip:
            return StepResult(False, "缺少本机 IP 或域控 IP，请先连接并确认 pc.ip", "")

        domain = _bridge_domain(ctx)
        host_work = Path(str(ctx.dc.get("host_work_dir") or "/home/nvidia/work/anyverse"))
        local_work = _local_work(ctx)
        local = _local_docker(ctx)

        remote_thor = f"{host_work}/config/cyclonedds_thor.xml"
        remote_live = "/anyverse/config/cyclonedds.xml"
        local_x86 = local_work / "config" / "cyclonedds_x86.xml"
        local_live = "/anyverse/config/cyclonedds.xml"
        bak_marker = f"{host_work}/config/{self.BAK_DIR_NAME}/READY"

        log(f"本机 IP={pc_ip}，域控 IP={dc_ip}，ROS_DOMAIN_ID={domain}")

        # —— 1 备份（仅首次：域控 thor 与容器 live 是同一 bind 文件，勿用已改写后的 live 覆盖备份）——
        log("—— 备份原 DDS 配置 ——")
        bak_remote = textwrap.dedent(
            f"""
            set -e
            BAK="{host_work}/config/{self.BAK_DIR_NAME}"
            mkdir -p "$BAK"
            if [ ! -f "$BAK/cyclonedds_thor.xml" ]; then
              cp -a "{remote_thor}" "$BAK/cyclonedds_thor.xml"
            fi
            # live 与 thor 为同一挂载时内容相同；只在首次备份，避免二次跑互通时把测试配置写进 bak
            if [ ! -f "$BAK/cyclonedds_live.xml" ]; then
              docker exec {ctx.ssh.resolve_container(log=None)} \
                cat {remote_live} > "$BAK/cyclonedds_live.xml" 2>/dev/null \
                || cp -a "$BAK/cyclonedds_thor.xml" "$BAK/cyclonedds_live.xml"
            fi
            echo BAK_OK
            """
        ).strip()
        br = ctx.ssh.exec_host(bak_remote, log=log, timeout=60, stream_output=False)
        logs.append(br.combined)
        if "BAK_OK" not in br.combined:
            return StepResult(False, "备份域控 DDS 配置失败", "\n".join(logs))

        local_bak = local_work / "config" / self.BAK_DIR_NAME
        try:
            local_bak.mkdir(parents=True, exist_ok=True)
            if local_x86.is_file() and not (local_bak / "cyclonedds_x86.xml").is_file():
                (local_bak / "cyclonedds_x86.xml").write_text(
                    local_x86.read_text(encoding="utf-8"), encoding="utf-8"
                )
            # 本机 live 与 x86 同为 bind；仅首次备份，防止二次互通污染
            if not (local_bak / "cyclonedds_live.xml").is_file():
                live_get = local.exec(
                    f"cat {local_live}",
                    log=None,
                    timeout=30,
                    stream_output=False,
                )
                if live_get.ok and live_get.stdout.strip():
                    (local_bak / "cyclonedds_live.xml").write_text(
                        live_get.stdout, encoding="utf-8"
                    )
                elif (local_bak / "cyclonedds_x86.xml").is_file():
                    (local_bak / "cyclonedds_live.xml").write_text(
                        (local_bak / "cyclonedds_x86.xml").read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
        except Exception as exc:  # noqa: BLE001
            return StepResult(False, f"备份本机 DDS 配置失败: {exc}", "\n".join(logs))
        log("备份完成")

        # —— 2 写入互通配置 ——
        # 注意：域控/本机 cyclonedds.xml 均为「单文件 bind-mount」
        #   host:config/cyclonedds_thor.xml -> container:/anyverse/config/cyclonedds.xml
        #   host:config/cyclonedds_x86.xml  -> container:/anyverse/config/cyclonedds.xml
        # 必须原地覆盖内容，禁止 mv / docker cp（会 device or resource busy，或 inode 脱节）
        log("—— 写入互通 DDS 配置（address 绑定 + Peer）——")
        dc_xml = _cyclonedds_xml(dc_ip, pc_ip)
        pc_xml = _cyclonedds_xml(pc_ip, dc_ip)

        name = ctx.ssh.resolve_container(log=None)
        w1 = ctx.ssh.write_remote_file(
            remote_thor, dc_xml, sudo=False, log=None, inplace=True
        )
        logs.append(w1.combined)
        if not w1.ok:
            return StepResult(False, "写入域控 cyclonedds_thor.xml 失败", "\n".join(logs))

        # 再经 docker exec 写入当前挂载 inode（兼容此前误用 mv 导致的宿主/容器脱节）
        tmp = "/tmp/k15_cyclonedds_dc.xml"
        ctx.ssh.write_remote_file(tmp, dc_xml, sudo=False, log=None)
        w2 = ctx.ssh.exec_host(
            f"docker exec -i {name} bash -lc 'cat > {remote_live}' < {tmp}",
            log=log,
            timeout=60,
            stream_output=False,
        )
        logs.append(w2.combined)
        if not w2.ok:
            return StepResult(False, "写入域控 cyclonedds.xml 失败", "\n".join(logs))

        # 本机：宿主 x86（原地 truncate）+ 容器 live（exec cat）
        try:
            local_x86.parent.mkdir(parents=True, exist_ok=True)
            local_x86.write_text(pc_xml, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return StepResult(False, f"写入本机 cyclonedds_x86.xml 失败: {exc}", "\n".join(logs))
        w3 = local.write_file(local_live, pc_xml, log=log)
        logs.append(w3.combined)
        if not w3.ok:
            return StepResult(False, "写入本机容器 cyclonedds.xml 失败", "\n".join(logs))

        # 标记已备份，供恢复步骤使用
        ctx.ssh.exec_host(f'echo READY > "{bak_marker}"', log=None, timeout=30, stream_output=False)
        (local_bak / "READY").write_text("READY\n", encoding="utf-8")

        # —— 3 冒烟：域控 talker + 本机 listener ——
        log("—— 冒烟：域控 talker / 本机 listener（约 20s）——")
        env = _bridge_env(ctx)
        source = ctx.source_ws()
        # 注意：不可 pkill -f 'demo_nodes_cpp/talker'——该字符串在 bash -lc 命令行里，
        # 会把当前脚本进程一并杀掉，表现为「talker 启动失败」。
        start_talker = textwrap.dedent(
            f"""
            set +e
            {source}
            {env}
            [ -f /usr/local/mw/setup.bash ] && source /usr/local/mw/setup.bash
            pkill -x talker 2>/dev/null || true
            sleep 1
            rm -f /tmp/k15_bridge_talker.log /tmp/k15_bridge_talker.pid
            setsid ros2 run demo_nodes_cpp talker >/tmp/k15_bridge_talker.log 2>&1 </dev/null &
            sleep 2
            if pgrep -x talker >/dev/null; then
              pgrep -x talker | head -1 > /tmp/k15_bridge_talker.pid
              echo TALKER_STARTED
              exit 0
            fi
            echo TALKER_FAILED
            cat /tmp/k15_bridge_talker.log 2>/dev/null || true
            exit 1
            """
        ).strip()
        t_res = ctx.ssh.exec_docker(start_talker, log=log, timeout=90, stream_output=True)
        logs.append(t_res.combined)
        if "TALKER_STARTED" not in t_res.combined:
            return StepResult(False, "域控 talker 启动失败", "\n".join(logs))

        listen = textwrap.dedent(
            f"""
            set +e
            {source}
            {env}
            [ -f /usr/local/mw/setup.bash ] && source /usr/local/mw/setup.bash
            timeout 15 ros2 topic echo /chatter --once 2>&1 | tee /tmp/k15_bridge_listener.log
            if grep -qiE 'Hello World|data:' /tmp/k15_bridge_listener.log; then
              echo BRIDGE_PASS
              exit 0
            fi
            echo BRIDGE_FAIL
            exit 1
            """
        ).strip()
        l_res = local.exec(listen, log=log, timeout=60, stream_output=True)
        logs.append(l_res.combined)

        ctx.ssh.exec_docker(
            "pkill -x talker 2>/dev/null || true; rm -f /tmp/k15_bridge_talker.pid",
            log=None,
            timeout=30,
            stream_output=False,
        )

        if "BRIDGE_PASS" not in l_res.combined:
            return StepResult(
                False,
                (
                    "DDS 配置已写入，但 talker/listener 冒烟未通过。"
                    "请确认两边容器在跑、网线互通，且 domain 一致"
                ),
                "\n".join(logs),
            )

        return StepResult(
            True,
            f"DDS/ROS 互通已配置并验证（PC={pc_ip} ↔ DC={dc_ip}, domain={domain}）",
            "\n".join(logs),
        )


class JointLatencyTestStep(TestStep):
    """合并：域控 Controller + 本机 MoveIt Demo + 域控时延脚本，三路日志并行。"""

    LOCAL_SCRIPT = ROOT / "script" / "controller_state_latency6.py"
    REMOTE_SCRIPT = "/tmp/k15_controller_state_latency6.py"
    CTRL_LOG = "/tmp/k15_controller.log"
    DEMO_LOG = "/tmp/k15_moveit_demo.log"
    LAT_LOG = "/tmp/k15_latency.log"
    LAT_PID = "/tmp/k15_latency.pid"
    LAT_EXIT = "/tmp/k15_latency.exit"

    def __init__(self) -> None:
        super().__init__(
            id="test_joint_latency",
            title="关节时延测试",
            description=(
                "并行启动域控 Controller、本机 MoveIt、时延脚本；"
                "三栏各显一路日志；点「测试完成」停止三路"
            ),
            category="test",
            dangerous=True,
            needs_manual=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        from core.parallel_logs import LogStream, ParallelLogHub, banner_parallel_start

        logs: list[str] = []
        mv = ctx.config.get("moveit") or {}
        pkg = mv.get("controller_package", "hardware_integration")
        launch = mv.get("controller_launch", "controller.launch.py")
        moveit_pkg = mv.get("moveit_config_package", "kitt_1_5_robot_with_zx90d_moveit")
        demo_launch = mv.get("moveit_demo_launch", "moveit_demo.launch.py")
        env = _bridge_env(ctx)
        source = ctx.source_ws()
        local = _local_docker(ctx)
        dc_name = ctx.ssh.resolve_container(log=None)
        dc_user = (ctx.ssh.container_user or "admin").strip() or "admin"
        pc_name = local.container_name
        pc_user = local.container_user

        if ctx.set_log_layout:
            ctx.set_log_layout("triple")
        ctx.finish_event.clear()

        def clog(channel: str, msg: str) -> None:
            """只写入对应栏；禁止往其它栏串日志。"""
            if ctx.log_channel:
                ctx.log_channel(channel, msg)
            else:
                log(f"[{channel}] {msg}")

        def status(msg: str) -> None:
            """启动进度等只进状态条，不进三栏。"""
            if ctx.log_channel:
                ctx.log_channel("status", msg)
            else:
                log(msg)

        banner_parallel_start(status)

        # —— 快速启动 Controller（不等长日志）——
        # 经脚本文件启动，避免 docker exec bash -lc 命令行触发 pkill 误杀
        status("启动域控 Controller…")
        ctrl_pat = r"/opt/ros/.*/bin/ros2 [l]aunch .*controller[.]launch[.]py"
        ctrl_script = textwrap.dedent(
            f"""
            #!/bin/bash
            set +e
            cd /anyverse
            {source}
            {env}
            [ -f /usr/local/mw/setup.bash ] && source /usr/local/mw/setup.bash
            {_safe_pkill_snippet(ctrl_pat)}
            if [ -f /tmp/k15_controller.pid ]; then
              kill $(cat /tmp/k15_controller.pid) 2>/dev/null || true
            fi
            sleep 1
            : > {self.CTRL_LOG}
            setsid ros2 launch {pkg} {launch} \\
              moveit_config_package:={moveit_pkg} \\
              >{self.CTRL_LOG} 2>&1 </dev/null &
            echo $! > /tmp/k15_controller.pid
            sleep 3
            if [ -f /tmp/k15_controller.pid ] && kill -0 $(cat /tmp/k15_controller.pid) 2>/dev/null; then
              echo CONTROLLER_STARTED
              exit 0
            fi
            if pgrep -f '{ctrl_pat}' >/dev/null; then
              pgrep -f '{ctrl_pat}' | head -1 > /tmp/k15_controller.pid
              echo CONTROLLER_STARTED
              exit 0
            fi
            echo CONTROLLER_FAILED
            tail -n 40 {self.CTRL_LOG} || true
            exit 1
            """
        ).strip()
        ctrl = _run_remote_script(ctx, ctrl_script, "k15_start_controller.sh")
        logs.append(ctrl.combined)
        if "CONTROLLER_STARTED" not in ctrl.combined:
            status("Controller 启动失败")
            for ln in (ctrl.combined or "").splitlines()[-30:]:
                clog("controller", ln)
            return StepResult(False, "域控 Controller 启动失败，见日志", "\n".join(logs))
        status("Controller 已后台运行")

        # —— 快速启动 MoveIt Demo ——
        status("启动本机 MoveIt Demo…")
        demo_pat = r"/opt/ros/.*/bin/ros2 [l]aunch .*moveit_demo[.]launch[.]py"
        demo_script = textwrap.dedent(
            f"""
            #!/bin/bash
            set +e
            cd /anyverse
            {source}
            {env}
            [ -f /usr/local/mw/setup.bash ] && source /usr/local/mw/setup.bash
            {_safe_pkill_snippet(demo_pat)}
            if [ -f /tmp/k15_moveit_demo.pid ]; then
              kill $(cat /tmp/k15_moveit_demo.pid) 2>/dev/null || true
            fi
            sleep 1
            : > {self.DEMO_LOG}
            setsid ros2 launch {moveit_pkg} {demo_launch} \\
              >{self.DEMO_LOG} 2>&1 </dev/null &
            echo $! > /tmp/k15_moveit_demo.pid
            sleep 3
            if [ -f /tmp/k15_moveit_demo.pid ] && kill -0 $(cat /tmp/k15_moveit_demo.pid) 2>/dev/null; then
              echo MOVEIT_STARTED
              exit 0
            fi
            if pgrep -f '{demo_pat}' >/dev/null; then
              pgrep -f '{demo_pat}' | head -1 > /tmp/k15_moveit_demo.pid
              echo MOVEIT_STARTED
              exit 0
            fi
            echo MOVEIT_FAILED
            tail -n 40 {self.DEMO_LOG} || true
            exit 1
            """
        ).strip()
        demo = _run_local_script(local, demo_script, "k15_start_moveit.sh")
        logs.append(demo.combined)
        if "MOVEIT_STARTED" not in demo.combined:
            status("MoveIt Demo 启动失败")
            for ln in (demo.combined or "").splitlines()[-30:]:
                clog("moveit", ln)
            return StepResult(False, "本机 MoveIt Demo 启动失败，见日志", "\n".join(logs))
        status("MoveIt Demo 已后台运行")

        # —— 上传并后台启动时延脚本 ——
        status("启动时延监测脚本…")
        if not self.LOCAL_SCRIPT.is_file():
            return StepResult(False, f"本机缺少脚本: {self.LOCAL_SCRIPT}", "\n".join(logs))
        try:
            content = self.LOCAL_SCRIPT.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return StepResult(False, f"读取脚本失败: {exc}", "\n".join(logs))

        up = ctx.ssh.write_remote_file(self.REMOTE_SCRIPT, content, sudo=False, log=None)
        if not up.ok:
            return StepResult(False, "上传时延脚本失败", "\n".join(logs) + "\n" + up.combined)
        cp = ctx.ssh.exec_host(
            f"docker cp {self.REMOTE_SCRIPT} {dc_name}:{self.REMOTE_SCRIPT}",
            log=None,
            timeout=60,
            stream_output=False,
        )
        if not cp.ok:
            return StepResult(False, "拷贝时延脚本到容器失败", "\n".join(logs) + "\n" + cp.combined)

        # 只按旧 pid 结束，避免在 bash -lc 里 pgrep python 路径；经脚本文件启动
        lat_start = textwrap.dedent(
            f"""
            #!/bin/bash
            set +e
            cd /anyverse
            {source}
            {env}
            [ -f /usr/local/mw/setup.bash ] && source /usr/local/mw/setup.bash
            if [ -f {self.LAT_PID} ]; then
              kill $(cat {self.LAT_PID}) 2>/dev/null || true
              sleep 1
            fi
            # 额外按特征清理（脚本文件 cmdline 不含此字面量时更安全）
            {_safe_pkill_snippet(r"[p]ython3 /tmp/k15_controller_state_latency6[.]py")}
            rm -f {self.LAT_EXIT}
            : > {self.LAT_LOG}
            # 独立进程组 + 退出码落盘（供后续轮询）
            setsid bash -c 'python3 -u {self.REMOTE_SCRIPT} >{self.LAT_LOG} 2>&1; echo $? >{self.LAT_EXIT}' </dev/null &
            echo $! > {self.LAT_PID}
            sleep 2
            if [ -f {self.LAT_PID} ] && kill -0 $(cat {self.LAT_PID}) 2>/dev/null; then
              echo LATENCY_STARTED
              exit 0
            fi
            # 若已退出：看退出码，非 0 则失败
            if [ -f {self.LAT_EXIT} ]; then
              code=$(cat {self.LAT_EXIT} | tr -d '[:space:]')
              echo "latency exited early code=$code"
              cat {self.LAT_LOG} || true
              if [ "$code" = "0" ]; then
                echo LATENCY_STARTED
                exit 0
              fi
              echo LATENCY_FAILED
              exit 1
            fi
            echo LATENCY_FAILED
            cat {self.LAT_LOG} || true
            exit 1
            """
        ).strip()
        lat = _run_remote_script(ctx, lat_start, "k15_start_latency.sh")
        logs.append(lat.combined)
        if "LATENCY_STARTED" not in lat.combined:
            for ln in (lat.combined or "").splitlines()[-30:]:
                clog("latency", ln)
            return StepResult(False, "时延脚本启动失败", "\n".join(logs))
        status("三路已启动 · 各栏仅显示对应进程日志 · 完成后点「测试完成」停止")

        # 分栏流式输出前清空三栏，避免启动阶段串入；随后只推送各文件增量
        if ctx.set_log_layout:
            ctx.set_log_layout("triple")

        hub = ParallelLogHub(ssh=ctx.ssh)
        hub.start(
            [
                LogStream(
                    tag="[Controller]",
                    kind="remote",
                    log_path=self.CTRL_LOG,
                    container=dc_name,
                    user=dc_user,
                ),
                LogStream(
                    tag="[MoveIt]",
                    kind="local",
                    log_path=self.DEMO_LOG,
                    container=pc_name,
                    user=pc_user,
                ),
                LogStream(
                    tag="[Latency]",
                    kind="remote",
                    log_path=self.LAT_LOG,
                    container=dc_name,
                    user=dc_user,
                ),
            ]
        )

        def drain() -> int:
            return hub.drain(
                log,
                logs,
                channel_log=ctx.log_channel,
            )

        deadline = time.time() + 1800
        lat_exit: int | None = None
        user_finished = False
        try:
            while time.time() < deadline:
                drain()
                if ctx.finish_event.is_set() or ctx.cancelled:
                    user_finished = True
                    status("收到「测试完成」，正在停止三路进程…")
                    break
                # 检查时延进程是否结束
                chk = ctx.ssh.exec_docker(
                    f"if [ -f {self.LAT_EXIT} ]; then echo DONE:$(cat {self.LAT_EXIT}); "
                    f"elif [ -f {self.LAT_PID} ] && kill -0 $(cat {self.LAT_PID}) 2>/dev/null; then echo RUN; "
                    f"else echo DONE:1; fi",
                    log=None,
                    timeout=30,
                    stream_output=False,
                )
                text = chk.combined.strip()
                if text.startswith("DONE:"):
                    try:
                        lat_exit = int(text.split(":", 1)[1].strip().split()[0])
                    except Exception:  # noqa: BLE001
                        lat_exit = 1
                    # 再排空一点尾日志
                    for _ in range(10):
                        if drain() == 0:
                            time.sleep(0.15)
                        else:
                            time.sleep(0.05)
                    break
                time.sleep(0.25)
            else:
                drain()
                _stop_joint_latency_procs(ctx, local)
                return StepResult(False, "时延脚本超时（30 分钟）", "\n".join(logs))
        finally:
            hub.stop()
            drain()
            # 测试完成或自然结束：确保三路都停
            stop_log = _stop_joint_latency_procs(ctx, local)
            logs.append(stop_log)
            dc_remain = ""
            if "=== DC remain ===" in stop_log:
                dc_remain = stop_log.split("=== DC remain ===", 1)[-1].split("STOP_DC_OK", 1)[0]
            if "NONE" in dc_remain:
                status("三路停止完成")
            elif "STOP_DC_OK" in stop_log:
                status("已执行停止；域控仍可能有残留，请再点「测试完成」")
            else:
                status("停止命令未正常返回，请检查连接")

        if user_finished:
            status("三路已停止")
            return StepResult(
                True,
                "测试完成：已停止 Controller / MoveIt / Latency，请确认效果后点「人工确认通过」",
                "\n".join(logs),
                needs_manual_confirm=True,
            )
        if lat_exit == 0:
            status("时延脚本自行退出，退出码 0")
            return StepResult(
                True,
                "关节时延测试完成，请根据三栏日志确认执行效果后点「人工确认通过」",
                "\n".join(logs),
                needs_manual_confirm=True,
            )
        status(f"时延脚本失败 (exit={lat_exit})")
        return StepResult(
            False,
            f"时延脚本失败 (exit={lat_exit})",
            "\n".join(logs),
        )


class RestoreTestConfigsStep(TestStep):
    """恢复 DDS 等测试改动，停掉联调进程，保证仓库/配置纯洁。"""

    BAK_DIR_NAME = DdsBridgeStep.BAK_DIR_NAME

    def __init__(self) -> None:
        super().__init__(
            id="test_restore_configs",
            title="恢复测试改动",
            description="还原两边 cyclonedds 备份（进程请在关节时延步骤点「测试完成」停止）",
            category="test",
            dangerous=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        logs: list[str] = []
        host_work = Path(str(ctx.dc.get("host_work_dir") or "/home/nvidia/work/anyverse"))
        local_work = _local_work(ctx)
        local = _local_docker(ctx)
        name = ""
        try:
            name = ctx.ssh.resolve_container(log=None)
        except Exception:  # noqa: BLE001
            name = str(ctx.dc.get("container_name") or "agent_dev_nvidia")

        # —— 恢复域控 ——
        # bind-mount: cyclonedds_thor.xml == 容器 /anyverse/config/cyclonedds.xml
        # 只恢复首次备份的 thor（原厂 eth1 等）；勿用可能被二次互通污染的 live bak
        log("—— 恢复域控 DDS 配置 ——")
        restore_dc = textwrap.dedent(
            f"""
            set -e
            BAK="{host_work}/config/{self.BAK_DIR_NAME}"
            THOR="{host_work}/config/cyclonedds_thor.xml"
            if [ ! -f "$BAK/cyclonedds_thor.xml" ]; then
              echo "NO_BAK_DC"
              exit 1
            fi
            cat "$BAK/cyclonedds_thor.xml" > "$THOR"
            # 同一 inode 时写 thor 已生效；再写一遍容器路径保证一致
            docker exec -i {name} bash -lc 'cat > /anyverse/config/cyclonedds.xml' < "$BAK/cyclonedds_thor.xml"
            echo RESTORE_DC_OK
            echo "--- restored Interfaces ---"
            grep -A3 Interfaces "$THOR" | head -8
            """
        ).strip()
        r3 = ctx.ssh.exec_host(restore_dc, log=log, timeout=60, stream_output=True)
        logs.append(r3.combined)
        if "RESTORE_DC_OK" not in r3.combined:
            return StepResult(
                False,
                "恢复域控 DDS 失败（可能尚未执行互通步骤或备份缺失）",
                "\n".join(logs),
            )

        # —— 恢复本机 ——
        log("—— 恢复本机 DDS 配置 ——")
        bak = local_work / "config" / self.BAK_DIR_NAME
        x86_bak = bak / "cyclonedds_x86.xml"
        x86_path = local_work / "config" / "cyclonedds_x86.xml"
        try:
            if not x86_bak.is_file():
                return StepResult(
                    False,
                    "本机备份缺失，无法恢复（请确认已跑过 DDS 互通步骤）",
                    "\n".join(logs),
                )
            content = x86_bak.read_text(encoding="utf-8")
            x86_path.write_text(content, encoding="utf-8")
            w = local.write_file("/anyverse/config/cyclonedds.xml", content, log=log)
            logs.append(w.combined)
            if not w.ok:
                return StepResult(False, "恢复本机容器 cyclonedds.xml 失败", "\n".join(logs))
        except Exception as exc:  # noqa: BLE001
            return StepResult(False, f"恢复本机 DDS 失败: {exc}", "\n".join(logs))

        log("DDS 配置已恢复（未执行停进程）")
        return StepResult(True, "已恢复两边 DDS 配置", "\n".join(logs))


TEST_STEPS = [
    ChassisTopicTestStep(),
    DdsBridgeStep(),
    JointLatencyTestStep(),
    RestoreTestConfigsStep(),
]
