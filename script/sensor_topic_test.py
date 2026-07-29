#!/usr/bin/env python3
"""
全量传感器 topic 自动化测试脚本（数据存在性 + 帧率检查 + 带宽记录）。
增加测试条件确认：某些 topic 需特定操作（如运动）才发包，可交互确认或自动跳过。
在 Thor Docker 内执行，需 source ROS2 环境。
输出：终端 + 日志文件 (sensor_test_YYYYmmdd_HHMMSS.log)
用法：
  python sensor_topic_test.py
  python sensor_topic_test.py --hz
  python sensor_topic_test.py --hz --bw
  python sensor_topic_test.py --hz --bw --skip TC03,TC06,TC14,TC17
  python sensor_topic_test.py --hz --bw --yes          # 自动确认所有条件

注意：本脚本已针对域控与底盘 ROS 版本不一致问题，在所有 echo 命令中添加
      --qos-profile sensor_data 参数，并移除不支持的 --timeout 选项。
"""

import subprocess
import sys
import time
import argparse
import logging
import re
import select
from datetime import datetime
from typing import List, Dict, Tuple, Optional

# ============ 数据存在性测试项（13项） ============
DATA_TESTS = [
    {"id": "TC01", "desc": "后激光雷达点云",          "topic": "/driver/lidar/lidar_back/point_cloud/Data",  "required": True,  "condition": None},
    {"id": "TC02", "desc": "前激光雷达点云",          "topic": "/driver/lidar/lidar_front/point_cloud/Data", "required": True,  "condition": None},
    {"id": "TC03", "desc": "融合激光雷达点云",        "topic": "/driver/lidar/point_cloud/Data",             "required": True, "condition": None},
    {"id": "TC04", "desc": "主IMU(base_link)",       "topic": "/driver/imu/Data",                           "required": True,  "condition": None},
    {"id": "TC05", "desc": "IMU原始数据",             "topic": "/driver/imu/RawData",                       "required": True,  "condition": None},
    {"id": "TC06", "desc": "扩展IMU Data2(可选)",     "topic": "/driver/imu/Data2",                         "required": False, "condition": None},
    {"id": "TC07", "desc": "后雷达内置IMU",           "topic": "/driver/lidar/lidar_back/imu/Data",         "required": True,  "condition": None},
    {"id": "TC08", "desc": "前雷达内置IMU",           "topic": "/driver/lidar/lidar_front/imu/Data",        "required": True,  "condition": None},
    {"id": "TC09", "desc": "相机融合点云",            "topic": "/driver/camera/point_cloud/Data",           "required": True,  "condition": None},
    {"id": "TC10", "desc": "超声波雷达",              "topic": "/driver/radar/Data",                        "required": True,  "condition": None},
    {"id": "TC11", "desc": "轮速计(电机)",            "topic": "/driver/motor/Data",                        "required": True,  "condition": "需要车辆运动或电机转动"},
    {"id": "TC12", "desc": "上传运动状态(可选)",            "topic": "/move/State",                               "required": False,  "condition": "需要车辆运动或电机转动"},
    {"id": "TC13", "desc": "自动运动控制(可选)",            "topic": "/move/AutoMoveCmd",                         "required": False,  "condition": "需要车辆运动或电机转动"},
]

# ============ 帧率测试项（13项） ============
HZ_TESTS = [
    {"id": "TC14", "desc": "后激光雷达点云帧率",           "topic": "/driver/lidar/lidar_back/point_cloud/Data",  "required": True,  "expected_hz": (9.5, 10.5),   "condition": None},
    {"id": "TC15", "desc": "前激光雷达点云帧率",           "topic": "/driver/lidar/lidar_front/point_cloud/Data", "required": True,  "expected_hz": (9.5, 10.5),   "condition": None},
    {"id": "TC16", "desc": "融合激光雷达点云帧率",         "topic": "/driver/lidar/point_cloud/Data",             "required": True, "expected_hz": (9.5, 10.5),   "condition": None},
    {"id": "TC17", "desc": "主IMU帧率",                   "topic": "/driver/imu/Data",                           "required": True,  "expected_hz": (180, 220), "condition": None},
    {"id": "TC18", "desc": "IMU原始数据帧率",             "topic": "/driver/imu/RawData",                        "required": True,  "expected_hz": (180, 220), "condition": None},
    {"id": "TC19", "desc": "扩展IMU Data2帧率(可选)",     "topic": "/driver/imu/Data2",                          "required": False, "expected_hz": None,      "condition": None},
    {"id": "TC20", "desc": "后雷达内置IMU帧率",           "topic": "/driver/lidar/lidar_back/imu/Data",          "required": True,  "expected_hz": (180, 220), "condition": None},
    {"id": "TC21", "desc": "前雷达内置IMU帧率",           "topic": "/driver/lidar/lidar_front/imu/Data",         "required": True,  "expected_hz": (180, 220), "condition": None},
    {"id": "TC22", "desc": "相机融合点云帧率",            "topic": "/driver/camera/point_cloud/Data",            "required": True,  "expected_hz": (28, 32),   "condition": None},
    {"id": "TC23", "desc": "超声波雷达帧率",              "topic": "/driver/radar/Data",                         "required": True,  "expected_hz": (9.5, 10.5),  "condition": None},
    {"id": "TC24", "desc": "轮速计(电机)帧率",            "topic": "/driver/motor/Data",                         "required": True,  "expected_hz": (180, 220),  "condition": "需要车辆运动或电机转动"},
    {"id": "TC25", "desc": "上传运动状态帧率(可选)",                "topic": "/move/State",                                "required": False,  "expected_hz": (180, 220),  "condition": "需要车辆运动或电机转动"},
    {"id": "TC26", "desc": "自动运动控制帧率(可选)",          "topic": "/move/AutoMoveCmd",                          "required": False,  "expected_hz": (180, 220),  "condition": "需要车辆运动或电机转动"},
]

# ============ 带宽测试项（基于 DATA_TESTS 的 topic，13项） ============
BANDWIDTH_TESTS = [
    {"id": "TC27", "desc": "后激光雷达点云带宽",          "topic": "/driver/lidar/lidar_back/point_cloud/Data",  "required": True,  "condition": None},
    {"id": "TC28", "desc": "前激光雷达点云带宽",          "topic": "/driver/lidar/lidar_front/point_cloud/Data", "required": True,  "condition": None},
    {"id": "TC29", "desc": "融合激光雷达点云带宽",        "topic": "/driver/lidar/point_cloud/Data",             "required": True,  "condition": None},
    {"id": "TC30", "desc": "主IMU(base_link)带宽",       "topic": "/driver/imu/Data",                           "required": True,  "condition": None},
    {"id": "TC31", "desc": "IMU原始数据带宽",             "topic": "/driver/imu/RawData",                       "required": True,  "condition": None},
    {"id": "TC32", "desc": "扩展IMU Data2带宽(可选)",     "topic": "/driver/imu/Data2",                         "required": False, "condition": None},
    {"id": "TC33", "desc": "后雷达内置IMU带宽",           "topic": "/driver/lidar/lidar_back/imu/Data",         "required": True,  "condition": None},
    {"id": "TC34", "desc": "前雷达内置IMU带宽",           "topic": "/driver/lidar/lidar_front/imu/Data",        "required": True,  "condition": None},
    {"id": "TC35", "desc": "相机融合点云带宽",            "topic": "/driver/camera/point_cloud/Data",           "required": True,  "condition": None},
    {"id": "TC36", "desc": "超声波雷达带宽",              "topic": "/driver/radar/Data",                        "required": True,  "condition": None},
    {"id": "TC37", "desc": "轮速计(电机)带宽",            "topic": "/driver/motor/Data",                        "required": True,  "condition": "需要车辆运动或电机转动"},
    {"id": "TC38", "desc": "上传运动状态带宽(可选)",            "topic": "/move/State",                               "required": False,  "condition": "需要车辆运动或电机转动"},
    {"id": "TC39", "desc": "自动运动控制带宽(可选)",            "topic": "/move/AutoMoveCmd",                         "required": False,  "condition": "需要车辆运动或电机转动"},
]

class SensorTester:
    def __init__(self, timeout: int = 5, skip_ids: Optional[List[str]] = None,
                 check_hz: bool = False, hz_window: int = 10, auto_yes: bool = False,
                 check_bw: bool = False):
        self.timeout = timeout
        self.skip_ids = set(skip_ids) if skip_ids else set()
        self.check_hz = check_hz
        self.hz_window = hz_window
        self.auto_yes = auto_yes
        self.check_bw = check_bw
        self.logger = self._setup_logging()
        self.hz_samples = {}          # 存储每个测试项的 Hz 样本列表
        self.bw_samples = {}          # 存储每个测试项的带宽样本列表 (KB/s)

    def _setup_logging(self) -> logging.Logger:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"sensor_test_{timestamp}.log"
        logger = logging.getLogger("SensorTest")
        logger.setLevel(logging.INFO)

        # 文件输出
        fh = logging.FileHandler(log_filename, encoding="utf-8")
        fh.setLevel(logging.INFO)
        file_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        fh.setFormatter(file_fmt)

        # 控制台输出
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        console_fmt = logging.Formatter('%(message)s')
        ch.setFormatter(console_fmt)

        logger.addHandler(fh)
        logger.addHandler(ch)
        logger.propagate = False
        return logger

    def _confirm_condition(self, test_case: Dict) -> str:
        """
        确认测试前提条件。
        返回 'yes'（继续）、'skip'（跳过该测试）或 'quit'（退出全部）。
        """
        condition = test_case.get("condition")
        if not condition:
            return "yes"   # 无条件，直接继续

        if self.auto_yes:
            self.logger.info(f"[{test_case['id']}] {test_case['desc']} - 条件自动确认: {condition}")
            return "yes"

        self.logger.info(f"[{test_case['id']}] {test_case['desc']}")
        self.logger.info(f"    前提条件: {condition}")
        while True:
            ans = input("    条件是否满足？(y=继续, n=跳过此测试, q=退出): ").strip().lower()
            if ans in ('y', 'yes'):
                return "yes"
            elif ans in ('n', 'no', 'skip'):
                return "skip"
            elif ans in ('q', 'quit'):
                return "quit"
            else:
                self.logger.info("    请输入 y/n/q")

    def _filter_tests_by_condition(self, test_list: List[Dict]) -> Tuple[List[Dict], set]:
        """
        根据用户确认的测试条件，过滤掉跳过的测试项。
        返回 (过滤后的测试列表, 跳过的ID集合)
        """
        filtered = []
        skipped_ids = set()
        for test in test_list:
            tid = test["id"]
            if tid in self.skip_ids:
                skipped_ids.add(tid)
                continue
            action = self._confirm_condition(test)
            if action == "yes":
                filtered.append(test)
            elif action == "skip":
                self.logger.warning(f"    → 跳过测试 {tid}")
                skipped_ids.add(tid)
            elif action == "quit":
                self.logger.info("用户请求退出测试。")
                raise SystemExit(0)
        return filtered, skipped_ids

    def run_command(self, cmd: List[str], extra_timeout: int = 2) -> Tuple[int, str, str]:
        """执行子进程，返回 (returncode, stdout, stderr)"""
        total_timeout = self.timeout + extra_timeout
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=total_timeout)
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"超时 ({total_timeout}s)"
        except FileNotFoundError:
            return -2, "", "ros2 命令未找到，请先 source ROS2 环境"
        except Exception as e:
            return -3, "", str(e)

    def test_data_presence(self, test_case: Dict) -> bool:
        """数据存在性测试（ros2 topic echo --once，已适配 sensor_data QoS，移除不支持的 --timeout）"""
        tid = test_case["id"]
        desc = test_case["desc"]
        topic = test_case["topic"]
        self.logger.info(f"[{tid}] 数据测试: {desc} ({topic})")
        # 只保留 --qos-profile 和 --once，不使用 --timeout
        cmd = [
            "ros2", "topic", "echo", topic,
            "--qos-profile", "sensor_data",
            "--once"
        ]
        start = time.time()
        ret, stdout, stderr = self.run_command(cmd)
        elapsed = time.time() - start

        fail_reason = ""
        if ret == -2:
            fail_reason = "ros2 命令不可用"
        elif ret == -1 or "timed out" in stderr.lower():
            fail_reason = f"超时 ({self.timeout}s) 无消息"
        elif ret != 0:
            fail_reason = f"返回码 {ret}, {stderr.strip()}"
        elif not stdout.strip():
            fail_reason = "空消息"

        if not fail_reason:
            self.logger.info(f"    ✅ 通过 (耗时 {elapsed:.1f}s)")
            return True
        else:
            self.logger.error(f"    ❌ 失败: {fail_reason}")
            if stderr.strip():
                self.logger.error(f"    详情: {stderr.strip()[-200:]}")
            return False

    def test_frequency(self, test_case: Dict) -> bool:
        """
        帧率测试（ros2 topic hz），收集最多10个 Hz 样本。
        有预期范围时：任一样本超出范围即判定失败。
        无预期范围时：最终频率 > 0 即通过。
        使用 select 进行非阻塞读取，防止无数据时卡死。
        """
        tid = test_case["id"]
        desc = test_case["desc"]
        topic = test_case["topic"]
        expected = test_case.get("expected_hz", None)
        self.logger.info(f"[{tid}] 帧率测试: {desc} ({topic})")

        cmd = ["ros2", "topic", "hz", topic, "--window", str(self.hz_window)]
        freq_timeout = self.hz_window + 15   # 充足时间收集10条输出

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1
            )
        except FileNotFoundError:
            self.logger.error(f"    ❌ 失败: ros2 命令未找到")
            return False
        except Exception as e:
            self.logger.error(f"    ❌ 失败: 无法启动子进程 - {e}")
            return False

        samples = []          # 收集到的 Hz 值
        final_freq = None
        start = time.time()

        try:
            while len(samples) < 10:
                # 整体超时检查
                elapsed = time.time() - start
                if elapsed > freq_timeout:
                    self.logger.warning(f"    整体超时 ({freq_timeout}s)，停止收集样本")
                    break

                # 计算本次 select 最多等待的时间
                remaining = freq_timeout - elapsed
                if remaining <= 0:
                    break

                # 使用 select 等待 stdout 可读，设置超时防止无限阻塞
                ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
                if ready:
                    line = proc.stdout.readline()
                    if not line:
                        # 如果 stdout 已关闭且进程已结束，跳出
                        if proc.poll() is not None:
                            break
                        continue

                    # 提取 "average rate: xxx"
                    match = re.search(r'average rate:\s+([\d.]+)', line)
                    if match:
                        freq_val = float(match.group(1))
                        samples.append(freq_val)
                        final_freq = freq_val
                # 如果 select 超时（无数据），循环会回到开头检查总超时
        finally:
            # 确保子进程被终止
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        # 保存样本供外部查看
        self.hz_samples[tid] = samples

        if final_freq is None:
            self.logger.error(f"    ❌ 失败: 未在 {freq_timeout}s 内检测到平均频率")
            if samples:
                self.logger.info(f"    已收集到 {len(samples)} 个样本: {[f'{s:.2f}' for s in samples]}")
            else:
                self.logger.info("    未收集到任何样本")
            return False

        # 输出样本列表
        sample_str = ", ".join(f"{s:.2f}" for s in samples)
        self.logger.info(f"    Hz 样本 ({len(samples)}): [{sample_str}]")

        # 判断频率是否符合预期
        if expected is None:
            if final_freq > 0:
                self.logger.info(f"    ✅ 通过: {final_freq:.2f} Hz (>0)")
                return True
            else:
                self.logger.error(f"    ❌ 失败: 频率为 0 Hz")
                return False
        else:
            min_hz, max_hz = expected
            out_of_range = [s for s in samples if not (min_hz <= s <= max_hz)]
            if out_of_range:
                self.logger.error(
                    f"    ❌ 失败: 存在样本超出范围 {min_hz}-{max_hz} Hz: "
                    f"{[f'{v:.2f}' for v in out_of_range]}"
                )
                return False
            else:
                self.logger.info(
                    f"    ✅ 通过: 所有 {len(samples)} 个样本均在 {min_hz}-{max_hz} Hz 内"
                )
                return True

    def test_bandwidth(self, test_case: Dict) -> bool:
        """
        带宽测试（ros2 topic bw），收集5组带宽样本。
        不判断通过/失败，仅记录数据。
        返回 True 表示记录完成，False 表示未能收集到任何数据（会在日志中体现）。
        """
        tid = test_case["id"]
        desc = test_case["desc"]
        topic = test_case["topic"]
        self.logger.info(f"[{tid}] 带宽测试: {desc} ({topic})")

        cmd = ["ros2", "topic", "bw", topic]
        bw_timeout = 30  # 给予充足时间收集5组数据

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1
            )
        except FileNotFoundError:
            self.logger.error(f"    ❌ 失败: ros2 命令未找到")
            return False
        except Exception as e:
            self.logger.error(f"    ❌ 失败: 无法启动子进程 - {e}")
            return False

        samples = []  # 存储带宽值 (KB/s)
        start = time.time()

        try:
            while len(samples) < 5:
                elapsed = time.time() - start
                if elapsed > bw_timeout:
                    self.logger.warning(f"    整体超时 ({bw_timeout}s)，停止收集样本")
                    break

                remaining = bw_timeout - elapsed
                if remaining <= 0:
                    break

                ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
                if ready:
                    line = proc.stdout.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        continue

                    # 解析带宽行，例如: "65.33 KB/s from 100 messages"
                    match = re.search(r'([\d.]+)\s*(KB/s|MB/s|B/s)', line)
                    if match:
                        value = float(match.group(1))
                        unit = match.group(2)
                        # 统一转换为 KB/s
                        if unit == "MB/s":
                            value *= 1024
                        elif unit == "B/s":
                            value /= 1024
                        samples.append(value)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        # 保存样本
        self.bw_samples[tid] = samples

        if not samples:
            self.logger.error(f"    ❌ 未收集到带宽数据 (topic 可能无消息)")
            return False

        # 输出样本
        sample_str = ", ".join(f"{s:.2f}" for s in samples)
        self.logger.info(f"    带宽样本 (KB/s): [{sample_str}]")
        # 记录完成，返回 True 表示已记录
        return True

    def run_all(self) -> int:
        self.logger.info("=" * 60)
        self.logger.info("全量传感器 Topic 自动化测试")
        self.logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"超时: {self.timeout}s | 帧率测试: {'开' if self.check_hz else '关'} | 窗口: {self.hz_window}")
        self.logger.info(f"带宽测试: {'开' if self.check_bw else '关'}")
        self.logger.info(f"跳过: {self.skip_ids if self.skip_ids else '无'}")
        self.logger.info(f"条件确认: {'自动' if self.auto_yes else '交互'}")
        self.logger.info("=" * 60)

        # 条件确认并过滤测试项
        data_tests = [t for t in DATA_TESTS if t["id"] not in self.skip_ids]
        hz_tests = [t for t in HZ_TESTS if t["id"] not in self.skip_ids]
        bw_tests = [t for t in BANDWIDTH_TESTS if t["id"] not in self.skip_ids] if self.check_bw else []

        all_skipped_ids = set(self.skip_ids)  # 记录所有跳过的ID（包括条件未满足）

        # 数据测试条件确认
        data_tests, skip1 = self._filter_tests_by_condition(data_tests)
        all_skipped_ids.update(skip1)

        # 帧率测试条件确认
        if self.check_hz:
            hz_tests, skip2 = self._filter_tests_by_condition(hz_tests)
            all_skipped_ids.update(skip2)
        else:
            hz_tests = []

        # 带宽测试条件确认
        if self.check_bw:
            bw_tests, skip3 = self._filter_tests_by_condition(bw_tests)
            all_skipped_ids.update(skip3)
        else:
            bw_tests = []

        # 运行数据测试
        results = {}   # key: test_id, value: "PASS"/"FAIL"/"SKIP"
        if data_tests:
            self.logger.info("\n>>> 数据存在性测试 <<<")
            for test in data_tests:
                success = self.test_data_presence(test)
                results[test["id"]] = "PASS" if success else "FAIL"
                self.logger.info("")

        # 运行帧率测试
        if hz_tests:
            self.logger.info(">>> 帧率测试 <<<")
            for test in hz_tests:
                success = self.test_frequency(test)
                results[test["id"]] = "PASS" if success else "FAIL"
                self.logger.info("")

        # 运行带宽测试
        if bw_tests:
            self.logger.info(">>> 带宽测试 <<<")
            for test in bw_tests:
                recorded = self.test_bandwidth(test)
                # 带宽测试只记录，不影响必须项判定，统一标记为 PASS（表示已执行）
                results[test["id"]] = "PASS" if recorded else "FAIL"
                self.logger.info("")

        # 标记跳过的测试
        for tid in all_skipped_ids:
            if tid not in results:
                results[tid] = "SKIP"

        # 汇总
        self.logger.info("=" * 60)
        self.logger.info("测 试 结 果 汇 总")
        self.logger.info("-" * 60)
        all_defs = {t["id"]: t for t in DATA_TESTS + HZ_TESTS + BANDWIDTH_TESTS}
        total = len(results)
        passed = sum(1 for v in results.values() if v == "PASS")
        failed = sum(1 for v in results.values() if v == "FAIL")
        skipped = sum(1 for v in results.values() if v == "SKIP")

        for tid, status in results.items():
            def_info = all_defs[tid]
            desc = def_info["desc"]
            topic = def_info["topic"]
            prefix = f"{tid} ({desc} - {topic})"

            # 特殊处理带宽测试项：显示样本数据
            if self.check_bw and tid in [t["id"] for t in BANDWIDTH_TESTS]:
                bw_vals = self.bw_samples.get(tid, [])
                if bw_vals:
                    bw_str = ", ".join(f"{v:.2f} KB/s" for v in bw_vals)
                    line = f"{prefix}: ✅ 记录: [{bw_str}]"
                else:
                    line = f"{prefix}: ❌ 无数据"
            elif status == "PASS":
                line = f"{prefix}: ✅ PASS"
            elif status == "FAIL":
                if not def_info.get("required", True):
                    line = f"{prefix}: ❌ FAIL (可选)"
                else:
                    line = f"{prefix}: ❌ FAIL"
            else:  # SKIP
                if not def_info.get("required", True):
                    line = f"{prefix}: ⏭️  SKIP (可选)"
                else:
                    line = f"{prefix}: ⏭️  SKIP"
            self.logger.info(f"  {line}")

        self.logger.info("-" * 60)
        self.logger.info(f"通过: {passed} | 失败: {failed} | 跳过: {skipped} | 总计: {total}")

        # 必须项失败仅针对数据和帧率测试，带宽测试不影响必须项判定
        mandatory_fails = [
            tid for tid, v in results.items()
            if v == "FAIL" and all_defs[tid].get("required", True) and tid not in [t["id"] for t in BANDWIDTH_TESTS]
        ]
        optional_fails = [
            tid for tid, v in results.items()
            if v == "FAIL" and not all_defs[tid].get("required", True)
        ]

        if mandatory_fails:
            self.logger.error(f"❌ 必须项失败: {', '.join(mandatory_fails)}")
        if optional_fails:
            self.logger.info(f"⚠️  可选项失败(不影响): {', '.join(optional_fails)}")
        if skipped:
            self.logger.info(f"⏭️  跳过项: {', '.join([k for k,v in results.items() if v=='SKIP'])}")

        if mandatory_fails:
            self.logger.error("存在必须项失败，请检查传感器驱动/硬件。")
            self.logger.info("=" * 60)
            return 1
        else:
            if failed or skipped:
                self.logger.info("所有必须项通过（含可选项失败或跳过）。")
            else:
                self.logger.info("🎉 全部测试通过！")
            self.logger.info("=" * 60)
            return 0

def main():
    parser = argparse.ArgumentParser(description="ROS2 传感器 topic 全量自动测试")
    parser.add_argument("--timeout", type=int, default=5, help="Python 层超时秒数，用于 echo 等待 (默认5)")
    parser.add_argument("--skip", type=str, default="", help="跳过的测试ID，逗号分隔，如 TC03,TC06,TC14,TC17")
    parser.add_argument("--hz", action="store_true", help="启用帧率测试")
    parser.add_argument("--hz-window", type=int, default=10, help="帧率统计窗口大小 (默认10)")
    parser.add_argument("--bw", action="store_true", help="启用带宽测试")
    parser.add_argument("--yes", action="store_true", dest="auto_yes", help="自动确认所有测试条件（无交互）")
    args = parser.parse_args()

    skip_list = [x.strip() for x in args.skip.split(",") if x.strip()] if args.skip else []
    tester = SensorTester(timeout=args.timeout, skip_ids=skip_list,
                          check_hz=args.hz, hz_window=args.hz_window,
                          auto_yes=args.auto_yes, check_bw=args.bw)
    sys.exit(tester.run_all())

if __name__ == "__main__":
    main()
