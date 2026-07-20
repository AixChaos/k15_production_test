# K15 生产测试上位机

交互式 GUI：每个环境配置 / 生产测试步骤可单独点击执行。  
适配 **Ubuntu 22.04**。本机只显示日志，命令通过 **SSH + docker exec** 在域控执行。

## 依赖

```bash
cd ~/Projects/k15_production_test
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

系统库（若缺 Qt 平台插件）：

```bash
sudo apt update
sudo apt install -y libxcb-cursor0 libxkbcommon-x11-0
```

## 运行

```bash
source .venv/bin/activate
python main.py
```

## 使用流程

1. 填写域控 IP / 用户 / 密码或私钥，点击「连接」
2. 「环境配置」页：Docker、Git、相机、编译等（危险步骤有二次确认）
3. 「生产测试」页：底盘话题、DDS、Talker/Listener、Controller、关节时延等
4. 选中步骤 → 「执行本步骤」→ 右侧看实时日志
5. 需人工核对的步骤（Camera SN、夹爪 Mock、时延）执行后点「人工确认通过」
6. 「导出报告」生成 `reports/k15_test_report_*.json`

## 配置

编辑或在界面「保存配置」写入 `config/default.yaml`：

- `domain_controller.host` / `user` / `host_work_dir` / `container_name`
- `pc.ip`：写入域控 `cyclonedds.xml` 的 Peer（上位机 IP）
- `ros.domain_id`（默认 40）、`network_interface`（文档示例 `eth5`）

## 说明

- `docker_run.sh` 若为交互菜单，步骤会提示在域控终端手动执行
- Talker/Listener 在**域控容器内自环**验证；完整 PC↔域控需在装有 ROS2 的 PC 上另测 listener
- 本机不安装 / 不启动 ROS2
