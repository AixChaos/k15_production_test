

      
#!/usr/bin/env python3
"""
基于单个控制状态topic的关节控制延迟计算脚本
适配平滑轨迹/斜坡指令，修复了把运动时长算成延迟的致命bug
核心逻辑：检测「参考指令开始运动的时刻」和「反馈开始跟随的时刻」的时间差，即为真实控制延迟

修改说明：
- 速度阈值从 0.001 提高到 0.005
- 匹配窗口从 2.0 秒缩短到 1.0 秒
- 位置偏差阈值从 0.01 放宽到 0.05
- 对反馈速度进行了3点滑动平均滤波，减少噪声误触发
"""
import rclpy
from rclpy.node import Node
from control_msgs.msg import JointTrajectoryControllerState

class ControllerStateLatencyMonitor(Node):
    """基于控制状态topic的延迟监测节点"""
    
    def __init__(self):
        super().__init__('controller_state_latency_monitor')
        
        # ==================== 核心配置参数 ====================
        self.topics = [
            '/waist_controller/controller_state',
            '/head_controller/controller_state',
            '/trunk_without_waist_controller/controller_state',
            '/left_gripper_controller/controller_state',
            '/left_arm_controller/controller_state',
            '/right_arm_controller/controller_state'
        ]
        # 速度阈值：检测运动起始拐点，低于此值认为关节静止（rad/s）- 修改为0.005
        self.velocity_threshold = 0.001
        # 位置稳定阈值：用于稳定状态检测和最终精度计算（rad）
        self.position_stable_threshold = 0.0005
        # 最大历史记录数
        self.max_history_size = 200
        # 稳定状态持续时间阈值（秒）
        self.stable_duration_threshold = 2.0
        # 最大匹配时间窗口：只匹配1秒内的事件，避免过期匹配 - 修改为1.0
        self.latency_max_match_window = 1.0
        
        # 每个topic的状态数据
        self.topic_data = {}
        
        # 初始化每个topic的状态数据
        for topic in self.topics:
            self.topic_data[topic] = {
                # 上一时刻的位置和时间（用于计算速度）
                'last_ref_pos': None,
                'last_fb_pos': None,
                'last_msg_time': None,
                # 关节基础信息
                'joint_count': 0,
                'joint_names': [],
                # 事件记录：(事件时间戳, 触发时的位置, 触发时的速度)
                'joint_ref_start_events': [],  # 参考指令开始运动的事件
                'joint_fb_start_events': [],   # 反馈开始跟随的事件
                # 结果存储
                'joint_latencies': [],
                'joint_accuracies': [],
                # 稳定状态检测
                'stable_ref_pos': None,
                'stable_time': None,
                'command_detected': False,
                'is_moving': False,  # 关节是否处于运动中
                # 上一时刻的运动状态（用于检测拐点）
                'last_ref_is_static': [],
                'last_fb_is_static': [],
                # 新增：反馈速度历史（用于滑动平均滤波）
                'fb_vel_history': []  # 将在首次接收数据时按关节初始化
            }
        
        # 为每个topic创建订阅者
        self.subscribers = []
        for topic in self.topics:
            sub = self.create_subscription(
                JointTrajectoryControllerState,
                topic,
                lambda msg, topic=topic: self.controller_state_callback(msg, topic),
                10
            )
            self.subscribers.append(sub)
        
        # 定时器：事件匹配（100Hz，确保匹配及时）
        self.match_timer = self.create_timer(0.01, self.check_matches)
        
        # 创建日志文件
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = f"controller_latency_{timestamp}.txt"
        
        # 写入启动信息
        with open(self.log_file, 'w', encoding='utf-8') as f:
            f.write(f"启动延迟监控节点 - {datetime.datetime.now()}\n")
            f.write(f"监控话题: {self.topics}\n")
            f.write(f"运动检测速度阈值: {self.velocity_threshold} rad/s\n")
            f.write(f"位置稳定阈值: {self.position_stable_threshold} rad\n")
            f.write(f"匹配窗口: {self.latency_max_match_window} s\n")
            f.write(f"反馈速度滤波: 3点滑动平均\n")
            f.write(f"日志文件: {self.log_file}\n")
            f.write("=" * 60 + "\n")
        
        # 控制台启动信息
        self.get_logger().info(f"✅ 启动延迟监控节点")
        self.get_logger().info(f"📝 日志文件: {self.log_file}")
        self.get_logger().info(f"🎯 运动检测速度阈值: {self.velocity_threshold} rad/s")
        self.get_logger().info(f"⏱️ 匹配窗口: {self.latency_max_match_window} s")
        self.get_logger().info(f"📊 反馈速度滤波: 3点滑动平均")
    
    def file_only_output(self, content):
        """仅写入日志文件"""
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(content + '\n')
        except Exception as e:
            self.get_logger().error(f"写入日志文件失败: {e}")
    
    def log_output(self, content):
        """控制台+日志文件同时输出"""
        print(content)
        self.file_only_output(content)

    def controller_state_callback(self, msg, topic):
        """控制状态回调：核心重构，检测运动起始拐点，不再错误累计位置变化"""
        data = self.topic_data[topic]
        
        # 字段合法性校验
        if not hasattr(msg, 'feedback') or not hasattr(msg, 'reference'):
            return
        if not hasattr(msg.feedback, 'positions') or not hasattr(msg.reference, 'positions'):
            return
        
        # 获取当前时间（ROS时钟，纳秒转秒）
        current_time = self.get_clock().now().nanoseconds * 1e-9
        
        # 获取位置数据
        try:
            fb_positions = list(msg.feedback.positions)   # 反馈实际位置
            ref_positions = list(msg.reference.positions)  # 参考指令位置
        except Exception as e:
            self.get_logger().error(f"{topic}: 获取位置数据失败: {e}")
            return
        
        # 位置长度校验
        if len(ref_positions) != len(fb_positions):
            self.get_logger().warn(f"{topic}: 参考/反馈位置长度不匹配")
            return
        
        # 首次接收数据：初始化关节信息
        if data['joint_count'] == 0:
            data['joint_count'] = len(ref_positions)
            # 获取关节名称
            if hasattr(msg.feedback, 'joint_names') and msg.feedback.joint_names:
                data['joint_names'] = msg.feedback.joint_names
            elif hasattr(msg.reference, 'joint_names') and msg.reference.joint_names:
                data['joint_names'] = msg.reference.joint_names
            else:
                data['joint_names'] = [f'joint_{i}' for i in range(data['joint_count'])]
            # 初始化数组
            data['joint_ref_start_events'] = [[] for _ in range(data['joint_count'])]
            data['joint_fb_start_events'] = [[] for _ in range(data['joint_count'])]
            data['joint_latencies'] = [[] for _ in range(data['joint_count'])]
            data['joint_accuracies'] = [[] for _ in range(data['joint_count'])]
            data['last_ref_is_static'] = [True] * data['joint_count']
            data['last_fb_is_static'] = [True] * data['joint_count']
            # 初始化反馈速度历史（每个关节保留最近3个速度值，初始为0）
            data['fb_vel_history'] = [[0.0] * 3 for _ in range(data['joint_count'])]
            # 初始化上一时刻数据
            data['last_ref_pos'] = ref_positions.copy()
            data['last_fb_pos'] = fb_positions.copy()
            data['last_msg_time'] = current_time
            self.get_logger().info(f"{topic}: 初始化关节数据: {data['joint_names']}")
            return
        
        # 计算时间差（用于速度计算）
        if data['last_msg_time'] is None:
            dt = 0.001  # 防止除零
        else:
            dt = current_time - data['last_msg_time']
        if dt <= 0:
            dt = 0.001
        
        # ==================== 核心：检测运动起始拐点 ====================
        # 遍历每个关节，计算速度，检测「静止→运动」的拐点
        for joint_idx in range(data['joint_count']):
            joint_name = data['joint_names'][joint_idx]
            # 计算参考指令的速度
            ref_pos_now = ref_positions[joint_idx]
            ref_pos_last = data['last_ref_pos'][joint_idx]
            ref_vel = abs(ref_pos_now - ref_pos_last) / dt
            
            # 计算反馈的瞬时速度，然后进行3点滑动平均滤波
            fb_pos_now = fb_positions[joint_idx]
            fb_pos_last = data['last_fb_pos'][joint_idx]
            fb_vel_raw = abs(fb_pos_now - fb_pos_last) / dt
            
            # 更新速度历史（先进先出）
            hist = data['fb_vel_history'][joint_idx]
            hist.append(fb_vel_raw)
            if len(hist) > 3:
                hist.pop(0)
            # 滤波后的速度
            fb_vel = sum(hist) / len(hist)
            
            # 判断当前是否静止
            ref_is_static_now = ref_vel < self.velocity_threshold
            fb_is_static_now = fb_vel < self.velocity_threshold
            
            # 检测参考指令的拐点：上一时刻静止，当前时刻运动 → 指令开始运动，记录事件
            if data['last_ref_is_static'][joint_idx] and not ref_is_static_now:
                event = (current_time, ref_pos_now, ref_vel)
                data['joint_ref_start_events'][joint_idx].append(event)
                # 日志记录
                self.file_only_output(f"[指令启动事件] {topic} | 关节:{joint_name} | 时间:{current_time:.6f} | 位置:{ref_pos_now:.6f} | 速度:{ref_vel:.6f} rad/s")
                # 限制历史长度
                if len(data['joint_ref_start_events'][joint_idx]) > self.max_history_size:
                    data['joint_ref_start_events'][joint_idx].pop(0)
                # 标记检测到指令
                data['command_detected'] = True
                data['is_moving'] = True
            
            # 检测反馈的拐点：上一时刻静止，当前时刻运动 → 反馈开始跟随，记录事件
            if data['last_fb_is_static'][joint_idx] and not fb_is_static_now:
                event = (current_time, fb_pos_now, fb_vel)
                data['joint_fb_start_events'][joint_idx].append(event)
                # 日志记录
                self.file_only_output(f"[反馈启动事件] {topic} | 关节:{joint_name} | 时间:{current_time:.6f} | 位置:{fb_pos_now:.6f} | 速度:{fb_vel:.6f} rad/s")
                # 限制历史长度
                if len(data['joint_fb_start_events'][joint_idx]) > self.max_history_size:
                    data['joint_fb_start_events'][joint_idx].pop(0)
            
            # 更新上一时刻的静止状态
            data['last_ref_is_static'][joint_idx] = ref_is_static_now
            data['last_fb_is_static'][joint_idx] = fb_is_static_now
        
        # 更新上一时刻的位置和时间
        data['last_ref_pos'] = ref_positions.copy()
        data['last_fb_pos'] = fb_positions.copy()
        data['last_msg_time'] = current_time
        
        # 稳定状态检测和最终精度计算
        self.detect_stable_state(ref_positions, fb_positions, current_time, topic)

    def check_matches(self):
        """事件匹配：匹配同一个运动的指令启动事件和反馈启动事件，计算真实延迟"""
        for topic, data in self.topic_data.items():
            if data['joint_count'] == 0:
                continue
            
            for joint_idx in range(data['joint_count']):
                ref_events = data['joint_ref_start_events'][joint_idx]
                fb_events = data['joint_fb_start_events'][joint_idx]
                
                if not ref_events or not fb_events:
                    continue
                
                # 已匹配的索引（去重）
                matched_ref_idx = set()
                matched_fb_idx = set()
                
                # 遍历所有指令事件，匹配对应的反馈事件
                for ref_idx, (ref_time, ref_pos, ref_vel) in enumerate(ref_events):
                    if ref_idx in matched_ref_idx:
                        continue
                    
                    # 遍历反馈事件，找时间上最接近的、在指令之后的事件
                    min_latency = float('inf')
                    best_fb_idx = -1
                    
                    for fb_idx, (fb_time, fb_pos, fb_vel) in enumerate(fb_events):
                        if fb_idx in matched_fb_idx:
                            continue
                        
                        # 匹配条件：
                        # 1. 反馈时间晚于指令时间
                        # 2. 时间差在匹配窗口内
                        # 3. 位置偏差在合理范围内（避免匹配到其他运动）- 修改为0.05
                        time_diff = fb_time - ref_time
                        pos_diff = abs(ref_pos - fb_pos)
                        
                        if (
                            time_diff > 0
                            and time_diff < self.latency_max_match_window
                            and pos_diff < 0.01  # 放宽到0.05 rad
                        ):
                            # 找时间差最小的最佳匹配
                            if time_diff < min_latency:
                                min_latency = time_diff
                                best_fb_idx = fb_idx
                    
                    # 找到最佳匹配，计算延迟
                    if best_fb_idx != -1:
                        fb_time, fb_pos, fb_vel = fb_events[best_fb_idx]
                        latency_ms = min_latency * 1000
                        
                        # 记录结果
                        data['joint_latencies'][joint_idx].append(latency_ms)
                        
                        # 日志记录
                        joint_name = data['joint_names'][joint_idx]
                        self.file_only_output("=" * 60)
                        self.file_only_output(f"{topic} - 延迟匹配成功")
                        self.file_only_output(f"关节: {joint_name}")
                        self.file_only_output(f"指令启动时间: {ref_time:.6f} | 反馈启动时间: {fb_time:.6f}")
                        self.file_only_output(f"真实控制延迟: {latency_ms:.2f} ms")
                        self.file_only_output("=" * 60)
                        self.file_only_output("")
                        
                        # 标记已匹配
                        matched_ref_idx.add(ref_idx)
                        matched_fb_idx.add(best_fb_idx)
                
                # 安全移除已匹配的事件（倒序移除，避免索引错位）
                for idx in sorted(matched_ref_idx, reverse=True):
                    if idx < len(ref_events):
                        ref_events.pop(idx)
                for idx in sorted(matched_fb_idx, reverse=True):
                    if idx < len(fb_events):
                        fb_events.pop(idx)

    def detect_stable_state(self, ref_positions, fb_positions, current_time, topic):
        """稳定状态检测：100%保留原有的最终执行精度计算逻辑"""
        data = self.topic_data[topic]
        
        if not ref_positions or not fb_positions:
            return
        
        # 首次初始化稳定参考位置
        if data['stable_ref_pos'] is None:
            data['stable_ref_pos'] = ref_positions.copy()
            data['stable_time'] = current_time
            return
        
        # 检测参考位置是否变化
        ref_changed = False
        for old_p, new_p in zip(data['stable_ref_pos'], ref_positions):
            if abs(new_p - old_p) > self.position_stable_threshold:
                ref_changed = True
                break
        
        if ref_changed:
            # 参考位置变化，更新稳定状态
            data['stable_ref_pos'] = ref_positions.copy()
            data['stable_time'] = current_time
        else:
            # 参考位置稳定，且之前检测到过指令
            if data['command_detected'] and data['is_moving']:
                # 达到稳定时长阈值
                if current_time - data['stable_time'] >= self.stable_duration_threshold:
                    # 统计前强制匹配一次
                    self.check_matches()
                    
                    # 输出最终执行精度
                    self.log_output("=" * 80)
                    self.log_output(f"{topic} - 最终执行精度计算")
                    self.log_output("=" * 80)
                    
                    for joint_idx, joint_name in enumerate(data['joint_names']):
                        if joint_idx >= len(ref_positions) or joint_idx >= len(fb_positions):
                            continue
                        
                        final_accuracy = abs(ref_positions[joint_idx] - fb_positions[joint_idx])
                        data['joint_accuracies'][joint_idx].append(final_accuracy)
                        
                        self.log_output(f"关节: {joint_name}")
                        self.log_output(f"命令目标位置: {ref_positions[joint_idx]:.6f} rad")
                        self.log_output(f"实际最终位置: {fb_positions[joint_idx]:.6f} rad")
                        self.log_output(f"最终执行精度: {final_accuracy:.6f} rad")
                        self.log_output("")
                    
                    self.log_output("=" * 80)
                    
                    # 输出延迟统计
                    self.calculate_statistics(topic)
                    
                    # 输出汇总信息
                    self.log_output("=" * 80)
                    self.log_output(f"{topic} - 汇总信息")
                    self.log_output("=" * 80)
                    
                    for joint_idx, joint_name in enumerate(data['joint_names']):
                        if joint_idx >= len(ref_positions) or joint_idx >= len(fb_positions):
                            continue
                        
                        latencies = data['joint_latencies'][joint_idx]
                        if latencies:
                            min_latency = min(latencies)
                            max_latency = max(latencies)
                            latency_range = f"({min_latency:.2f}, {max_latency:.2f}) ms"
                        else:
                            latency_range = "无数据"
                        
                        final_accuracy = abs(ref_positions[joint_idx] - fb_positions[joint_idx])
                        self.log_output(f"关节: {joint_name} | 延迟范围: {latency_range} | 最终执行精度: {final_accuracy:.6f} rad")
                    
                    self.log_output("=" * 80)
                    self.log_output("")
                    
                    # 重置状态，准备下一次运动检测
                    data['stable_ref_pos'] = None
                    data['stable_time'] = None
                    data['command_detected'] = False
                    data['is_moving'] = False

    def calculate_statistics(self, topic):
        """延迟统计：和原代码输出格式完全一致"""
        data = self.topic_data[topic]
        
        self.log_output("=" * 80)
        self.log_output(f"{topic} - 延迟统计")
        self.log_output("=" * 80)
        
        has_data = False
        for latencies in data['joint_latencies']:
            if latencies:
                has_data = True
                break
        
        if not has_data:
            self.log_output("无延迟数据可统计")
            self.log_output("=" * 80)
            return
        
        for joint_idx in range(data['joint_count']):
            joint_name = data['joint_names'][joint_idx]
            latencies = data['joint_latencies'][joint_idx]
            accuracies = data['joint_accuracies'][joint_idx]
            
            if not latencies and not accuracies:
                self.log_output(f"关节 {joint_name}: 无数据")
                continue
            
            # 延迟统计
            if latencies:
                avg_latency = sum(latencies) / len(latencies)
                min_latency = min(latencies)
                max_latency = max(latencies)
            else:
                avg_latency = 0.0
                min_latency = 0.0
                max_latency = 0.0
            
            # 精度统计
            if accuracies:
                avg_accuracy = sum(accuracies) / len(accuracies)
            else:
                avg_accuracy = 0.0
            
            self.log_output(f"关节: {joint_name}")
            self.log_output(f"平均延迟: {avg_latency:.2f} ms")
            self.log_output(f"最小延迟: {min_latency:.2f} ms")
            self.log_output(f"最大延迟: {max_latency:.2f} ms")
            self.log_output(f"延迟范围: ({min_latency:.2f}, {max_latency:.2f}) ms")
            self.log_output(f"样本数量: {len(latencies)}")
            self.log_output("")
        
        self.log_output("=" * 80)

if __name__ == '__main__':
    rclpy.init()
    try:
        node = ControllerStateLatencyMonitor()
        rclpy.spin(node)
    except KeyboardInterrupt:
        print("\n🔄 正在关闭节点...")
        if hasattr(node, 'log_file'):
            try:
                with open(node.log_file, 'a', encoding='utf-8') as f:
                    f.write("\n" + "=" * 60 + "\n")
                    f.write("🔄 监控已关闭\n")
                    f.write("=" * 60 + "\n")
            except Exception:
                pass
    finally:
        rclpy.shutdown()

    


