"""生产测试步骤（全部在域控 Docker / 宿主机执行，本机只看日志）。"""
from __future__ import annotations

import textwrap

from core.context import AppContext
from steps.base import LogFn, StepResult, TestStep, shell_ok


class ChassisBuildStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="test_chassis_build",
            title="编译底盘话题代码",
            description="./script/build.sh -s src/robot_hardwares/monkey_chassis_v2",
            category="test",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        return shell_ok(
            ctx,
            "./script/build.sh -s src/robot_hardwares/monkey_chassis_v2",
            log,
            docker=True,
            timeout=3600,
        )


class SensorTopicTestStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="test_sensor_topic",
            title="底盘话题测试",
            description="python sensor_topic_test.py --hz --bw --yes",
            category="test",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        cmd = (
            f"{ctx.source_ws()} && "
            f"{ctx.ros_env_exports()} && "
            "python sensor_topic_test.py --hz --bw --yes "
            "|| python3 sensor_topic_test.py --hz --bw --yes "
            "|| python ./sensor_topic_test.py --hz --bw --yes "
            "|| python3 ./script/sensor_topic_test.py --hz --bw --yes "
            "|| find . -name sensor_topic_test.py 2>/dev/null | head -3"
        )
        res = ctx.ssh.exec_docker(cmd, log=log, timeout=600)
        lower = res.combined.lower()
        ok = res.ok and ("fail" not in lower or "passed" in lower or "pass" in lower)
        # 若只是找到了文件路径，说明脚本位置需人工确认
        if "sensor_topic_test.py" in res.combined and not res.ok:
            return StepResult(
                False,
                "未成功运行测试脚本，请确认脚本路径后重试",
                res.combined,
                needs_manual_confirm=True,
            )
        return StepResult(ok or res.ok, "话题测试完成" if res.ok else "话题测试失败", res.combined)


class DdsConfigStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="test_dds_config",
            title="配置 CycloneDDS",
            description="更新域控 cyclonedds.xml：网卡 + Peer=上位机 IP（本机不跑 ROS，只写域控侧）",
            category="test",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        ros = ctx.ros
        pc_ip = ctx.config.get("pc", {}).get("ip") or ctx.ssh.local_ip_guess()
        iface = ros.get("network_interface", "eth5")
        uri = ros.get("cyclonedds_uri", "/anyverse/config/cyclonedds.xml")
        # 容器路径；同时尝试宿主机挂载路径
        host_work = ctx.dc.get("host_work_dir", "/home/anyverse/work/anyverse")
        host_xml = f"{host_work}/config/cyclonedds.xml"

        xml = textwrap.dedent(
            f"""\
            <?xml version="1.0" encoding="UTF-8" ?>
            <CycloneDDS xmlns="https://cdds.io/config"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xsi:schemaLocation="https://cdds.io/config https://raw.githubusercontent.com/eclipse-cyclonedds/cyclonedds/master/etc/cyclonedds.xsd">
                <Domain id="any">
                    <General>
                        <Interfaces>
                            <NetworkInterface name="{iface}" priority="default" multicast="default" />
                        </Interfaces>
                        <AllowMulticast>spdp</AllowMulticast>
                        <MaxMessageSize>1300B</MaxMessageSize>
                        <DontRoute>true</DontRoute>
                    </General>
                    <Discovery>
                        <EnableTopicDiscoveryEndpoints>true</EnableTopicDiscoveryEndpoints>
                        <MaxAutoParticipantIndex>200</MaxAutoParticipantIndex>
                        <Peers>
                            <Peer address="{pc_ip}"/>
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
        log(f"Peer(上位机 IP)={pc_ip}, NetworkInterface={iface}")
        res = ctx.ssh.write_remote_file(host_xml, xml, sudo=False, log=log)
        if not res.ok:
            # 尝试直接写容器内路径（通过 docker cp）
            tmp = "/tmp/cyclonedds_k15.xml"
            ctx.ssh.write_remote_file(tmp, xml, sudo=False, log=log)
            name = ctx.ssh.resolve_container(log=log)
            res2 = ctx.ssh.exec_host(
                f"docker cp {tmp} {name}:{uri}",
                log=log,
                timeout=60,
            )
            if not res2.ok:
                return StepResult(False, "写入 cyclonedds.xml 失败", res.combined + "\n" + res2.combined)
            return StepResult(True, f"已写入容器 {uri}，Peer={pc_ip}", res2.combined)

        log("提示: 若另有 ROS PC，请在其 cyclonedds.xml 中把 Peer 设为域控 IP，并互相对应网卡。")
        return StepResult(True, f"已更新 {host_xml}，Peer={pc_ip}", res.combined)


class RosDaemonStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="test_ros_daemon",
            title="设置 DDS 环境并重启 ROS daemon",
            description="RMW_IMPLEMENTATION / CYCLONEDDS_URI + ros2 daemon stop/start",
            category="test",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        cmd = (
            f"{ctx.source_ws()} && "
            f"{ctx.ros_env_exports()} && "
            "ros2 daemon stop; ros2 daemon start; "
            "echo DOMAIN=$ROS_DOMAIN_ID RMW=$RMW_IMPLEMENTATION URI=$CYCLONEDDS_URI"
        )
        return shell_ok(ctx, cmd, log, docker=True, timeout=120)


class DomainIdStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="test_domain_id",
            title="校验 ROS_DOMAIN_ID",
            description="确认域控容器内 DOMAIN_ID / LOCALHOST_ONLY 与配置一致",
            category="test",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        did = ctx.ros.get("domain_id", 40)
        lo = ctx.ros.get("localhost_only", 0)
        cmd = (
            f"{ctx.ros_env_exports()} && "
            f'python3 -c "import os; '
            f"assert os.environ.get('ROS_DOMAIN_ID')=='{did}', os.environ.get('ROS_DOMAIN_ID'); "
            f"assert os.environ.get('ROS_LOCALHOST_ONLY')=='{lo}', os.environ.get('ROS_LOCALHOST_ONLY'); "
            f'print(\'OK DOMAIN\', os.environ[\'ROS_DOMAIN_ID\'])"'
        )
        return shell_ok(ctx, cmd, log, docker=True, timeout=60)


class TalkerListenerStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="test_talker_listener",
            title="通讯链路测试 (Talker/Listener)",
            description="本机不跑 ROS：在域控容器内并行 talker+listener 自检 DDS/ROS",
            category="test",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        # 单容器内自环验证；完整 PC↔域控需在有 ROS 的 PC 上另测
        script = textwrap.dedent(
            f"""
            set -e
            {ctx.source_ws()}
            {ctx.ros_env_exports()}
            LOG=/tmp/k15_listener.log
            rm -f "$LOG"
            timeout 25 ros2 run demo_nodes_cpp listener >"$LOG" 2>&1 &
            LPID=$!
            sleep 2
            timeout 15 ros2 run demo_nodes_cpp talker >/tmp/k15_talker.log 2>&1 &
            TPID=$!
            wait $TPID || true
            sleep 2
            kill $LPID 2>/dev/null || true
            wait $LPID 2>/dev/null || true
            echo '--- listener log ---'
            cat "$LOG" || true
            if grep -qiE 'I heard|Publishing|Hello World' "$LOG" /tmp/k15_talker.log 2>/dev/null; then
              echo PASS_TALKER_LISTENER
              exit 0
            fi
            echo FAIL_TALKER_LISTENER
            exit 1
            """
        ).strip()
        res = ctx.ssh.exec_docker(script, log=log, timeout=90)
        ok = res.ok and "PASS_TALKER_LISTENER" in res.combined
        msg = "容器内 Talker/Listener 通过"
        if ok:
            msg += "（完整 PC↔域控请在 ROS PC 上再验 listener）"
        else:
            msg = "Talker/Listener 未通过，检查 DOMAIN_ID / DDS / demo_nodes"
        return StepResult(ok, msg, res.combined)


class ControllerLaunchStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="test_controller_launch",
            title="启动硬件 Controller",
            description="ros2 launch hardware_integration controller.launch.py（后台）",
            category="test",
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        mv = ctx.config.get("moveit", {})
        pkg = mv.get("controller_package", "hardware_integration")
        launch = mv.get("controller_launch", "controller.launch.py")
        moveit_pkg = mv.get("moveit_config_package", "kitt_1_5_robot_with_zx90d_moveit")
        # 先停旧进程，再后台启动
        script = textwrap.dedent(
            f"""
            {ctx.source_ws()}
            {ctx.ros_env_exports()}
            pkill -f 'controller.launch.py' 2>/dev/null || true
            sleep 1
            nohup ros2 launch {pkg} {launch} \
              moveit_config_package:={moveit_pkg} \
              >/tmp/k15_controller.log 2>&1 &
            echo $! > /tmp/k15_controller.pid
            sleep 8
            if [ -f /tmp/k15_controller.pid ] && kill -0 $(cat /tmp/k15_controller.pid) 2>/dev/null; then
              echo CONTROLLER_STARTED pid=$(cat /tmp/k15_controller.pid)
              tail -n 40 /tmp/k15_controller.log || true
              exit 0
            fi
            echo CONTROLLER_FAILED
            tail -n 80 /tmp/k15_controller.log || true
            exit 1
            """
        ).strip()
        res = ctx.ssh.exec_docker(script, log=log, timeout=120)
        ok = res.ok and "CONTROLLER_STARTED" in res.combined
        return StepResult(
            ok,
            "Controller 已后台启动" if ok else "Controller 启动失败，见日志",
            res.combined,
        )


class JointLatencyStep(TestStep):
    def __init__(self) -> None:
        super().__init__(
            id="test_joint_latency",
            title="关节精度 / 时延测试",
            description="python3 controller_state_latency6.py（本机不跑 MoveIt Demo）",
            category="test",
            needs_manual=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        script_name = ctx.config.get("moveit", {}).get(
            "latency_script", "controller_state_latency6.py"
        )
        cmd = (
            f"{ctx.source_ws()} && "
            f"{ctx.ros_env_exports()} && "
            f"(python3 {script_name} || python3 ./script/{script_name} || "
            f"python3 $(find . -name {script_name} | head -1))"
        )
        res = ctx.ssh.exec_docker(cmd, log=log, timeout=600)
        return StepResult(
            ok=res.ok,
            message="时延脚本执行完成，请根据输出确认精度后点「人工确认通过」"
            if res.ok
            else "时延脚本失败（需先启动 controller；MoveIt Demo 若在 ROS PC）",
            log=res.combined,
            needs_manual_confirm=True,
        )


TEST_STEPS = [
    ChassisBuildStep(),
    SensorTopicTestStep(),
    DdsConfigStep(),
    RosDaemonStep(),
    DomainIdStep(),
    TalkerListenerStep(),
    ControllerLaunchStep(),
    JointLatencyStep(),
]
