# K15 生产测试上位机

交互式 GUI：每个环境配置 / 生产测试步骤可单独点击执行。  
适配 **Ubuntu 22.04+**。本机只显示日志，命令通过 **SSH + docker exec** 在域控执行。

## 推荐：安装 deb 包

```bash
# 构建（默认 1.0.0；指定版本：./packaging/build_deb.sh 1.0.1）
./packaging/build_deb.sh
# 产物：dist/k15-production-test_<版本>_all.deb

# 安装（自动拉系统依赖，并 pip 安装 PySide6/paramiko/PyYAML 到 /opt/.../venv）
sudo apt install -y ./dist/k15-production-test_1.0.0_all.deb
# 若依赖未自动拉齐：
sudo apt -f install -y

# 启动
k15-production-test
```

安装后：
- 程序目录：`/opt/k15-production-test`
- 用户配置：`~/.config/k15-production-test/default.yaml`
- 测试报告：`~/.local/share/k15-production-test/reports/`

## 开发态依赖

```bash
cd ~/Projects/k15_production_test
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

系统库（若缺 Qt 平台插件）：

```bash
sudo apt update
sudo apt install -y libxcb-cursor0 libxkbcommon-x11-0 rsync openssh-client sshpass
```

## 运行（开发）

```bash
source .venv/bin/activate
python main.py
# 或
./run.sh
```

## 使用流程

1. 启动后自动填充本机网线 IP，本机账号默认 `wujie` / `123456`
2. **手动填写域控 IP**，域控账号默认 `nvidia` / `nvidia`，点击「连接域控」
3. **文件下载**：连接后点「浏览域控并下载到本机」；单击选中，双击进文件夹，点「下载」保存文件或整夹到「下载/Downloads」
4. 「环境配置」页步骤「上传部署环境压缩包」：选择本机 `K15_env_con.tar`（或 `.tar.gz`，含密钥、anyverse-dev-pnc / anyverse、docker_arm 镜像、99-EtherCAT.rules），自动上传并部署
5. 其他「环境配置」/「生产测试」步骤：选中 →「执行本步骤」→ 右侧看日志
6. 需人工核对的步骤执行后点「人工确认通过」
7. 「导出报告」生成报告 JSON

## 配置

界面「保存配置」写入用户配置文件（打包安装）或仓库 `config/default.yaml`（开发态）：

- `domain_controller.host`（需手动填）/ `user`（默认 nvidia）/ `host_work_dir` / `container_name`
- `pc.ip`：启动时自动探测网线 IP，用于 DDS Peer
- `ros.domain_id`、`network_interface`（文档示例 `eth5`）

## 说明

- 环境压缩包部署使用 `docker load` 导入 `docker_arm_*.tar.gz` 镜像
- Talker/Listener 在**域控容器内自环**验证；完整 PC↔域控需在装有 ROS2 的 PC 上另测 listener
- 本机不安装 / 不启动 ROS2
