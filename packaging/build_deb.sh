#!/usr/bin/env bash
# 构建 k15-production-test_<version>_all.deb
# 用法:
#   ./packaging/build_deb.sh           # 默认 1.0.0
#   ./packaging/build_deb.sh 1.0.1     # 指定版本
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG_NAME="k15-production-test"
VERSION="${1:-1.0.0}"
ARCH="all"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.+~]+)?$ ]]; then
  echo "无效版本号: $VERSION" >&2
  echo "示例: ./packaging/build_deb.sh 1.0.1" >&2
  exit 1
fi

DIST="$ROOT/dist"
STAGE="$DIST/${PKG_NAME}_${VERSION}_${ARCH}"
DEB="$DIST/${PKG_NAME}_${VERSION}_${ARCH}.deb"
APP_ROOT="/opt/${PKG_NAME}"

echo "Building ${PKG_NAME} ${VERSION} (${ARCH})…"

rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN"
mkdir -p "$STAGE${APP_ROOT}"
mkdir -p "$STAGE/usr/bin"
mkdir -p "$STAGE/usr/share/applications"
mkdir -p "$STAGE/usr/share/doc/${PKG_NAME}"

# —— 应用文件 ——
rsync -a \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'dist/' \
  --exclude 'packaging/' \
  --exclude 'reports/' \
  --exclude '.gitignore' \
  "$ROOT/" "$STAGE${APP_ROOT}/"

# 包内使用干净默认配置（不含本机 IP/路径）
install -m 0644 "$ROOT/packaging/default.yaml" "$STAGE${APP_ROOT}/config/default.yaml"

# 启动器
cat > "$STAGE/usr/bin/k15-production-test" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="/opt/k15-production-test"
VENV="$APP_ROOT/venv"
export PYTHONPATH="$APP_ROOT${PYTHONPATH:+:$PYTHONPATH}"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "未找到运行环境，请重新安装本软件包（postinst 会创建 venv）。" >&2
  exit 1
fi
exec "$VENV/bin/python" "$APP_ROOT/main.py" "$@"
EOF
chmod 0755 "$STAGE/usr/bin/k15-production-test"

# Desktop
cat > "$STAGE/usr/share/applications/k15-production-test.desktop" << EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=K15 生产测试上位机
Name[zh_CN]=K15 生产测试上位机
Comment=Domain controller production test HMI
Comment[zh_CN]=域控生产测试上位机（SSH + Docker）
Exec=k15-production-test
Icon=utilities-terminal
Terminal=false
Categories=Development;Utility;
StartupNotify=true
EOF

# 文档
cp "$ROOT/README.md" "$STAGE/usr/share/doc/${PKG_NAME}/README.md"
cat > "$STAGE/usr/share/doc/${PKG_NAME}/copyright" << EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: k15-production-test
Copyright: Anyverse
License: Proprietary
EOF

# —— control ——
INSTALLED_SIZE="$(du -sk "$STAGE${APP_ROOT}" | awk '{print $1}')"
cat > "$STAGE/DEBIAN/control" << EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Installed-Size: ${INSTALLED_SIZE}
Maintainer: Anyverse <dev@anyverse.work>
Depends: python3 (>= 3.10), python3-venv, python3-pip, python3-setuptools, python3-wheel, rsync, openssh-client, sshpass, libxcb-cursor0, libxkbcommon-x11-0, libxcb-xinerama0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, libxcb-render-util0, libxcb-shape0, libegl1, libgl1, libglib2.0-0, libdbus-1-3, libnss3, libxcomposite1, libxdamage1, libxrandr2, libxtst6, libasound2 | libasound2t64
Recommends: docker.io | docker-ce
Description: K15 domain-controller production test HMI
 GUI tool to configure and test K15 domain controllers over SSH/Docker.
 Installs Python dependencies (PySide6, paramiko, PyYAML) into
 /opt/k15-production-test/venv during package configuration.
EOF

# —— postinst：自动创建 venv 并安装 Python 依赖 ——
cat > "$STAGE/DEBIAN/postinst" << 'EOF'
#!/bin/bash
set -e
APP_ROOT="/opt/k15-production-test"
VENV="$APP_ROOT/venv"
REQ="$APP_ROOT/requirements.txt"

echo "k15-production-test: 正在创建 Python 虚拟环境并安装依赖…"
# 可重复安装：先清旧 venv，避免半残状态
rm -rf "$VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip setuptools wheel
"$VENV/bin/pip" install -r "$REQ"

# 桌面数据库刷新（忽略失败）
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi

echo "k15-production-test: 安装完成。运行: k15-production-test"
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/prerm" << 'EOF'
#!/bin/bash
set -e
# 卸载前保留用户配置；仅删除运行环境
rm -rf /opt/k15-production-test/venv
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/prerm"

cat > "$STAGE/DEBIAN/postrm" << 'EOF'
#!/bin/bash
set -e
if [ "$1" = "purge" ]; then
  rm -rf /opt/k15-production-test
fi
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postrm"

# 权限
find "$STAGE${APP_ROOT}" -type d -exec chmod 0755 {} +
find "$STAGE${APP_ROOT}" -type f -exec chmod 0644 {} +
chmod 0755 "$STAGE${APP_ROOT}/main.py"
chmod 0755 "$STAGE${APP_ROOT}/run.sh" 2>/dev/null || true

# 构建
mkdir -p "$DIST"
dpkg-deb --root-owner-group --build "$STAGE" "$DEB"
echo "Built: $DEB"
dpkg-deb -I "$DEB"
ls -lh "$DEB"
