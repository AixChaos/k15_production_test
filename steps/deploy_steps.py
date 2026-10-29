"""一键部署：串行执行环境配置前 6 步，并汇总结果。"""
from __future__ import annotations

from pathlib import Path

from steps.base import LogFn, StepResult, StepStatus, TestStep
from steps.env_steps import ENV_STEPS
from core.context import AppContext

# 环境配置 1～6（不含末端配置）
ONE_CLICK_ENV_COUNT = 6


class OneClickDeployStep(TestStep):
    """自动执行环境配置 1～6，并打印成功/失败汇总与 camera 配置。"""

    def __init__(self) -> None:
        super().__init__(
            id="deploy_one_click",
            title="自动化一键部署",
            description=(
                "依次执行环境配置 1～6："
                "Docker → 上传部署压缩包 → 相机初始化 → Camera SN → "
                "相机编译 → 本体硬件包编译；"
                "结束后汇总成败，并打印设备 camera.json"
            ),
            category="deploy",
            dangerous=True,
        )

    def run(self, ctx: AppContext, log: LogFn) -> StepResult:
        sub_steps = list(ENV_STEPS[:ONE_CLICK_ENV_COUNT])
        if len(sub_steps) < ONE_CLICK_ENV_COUNT:
            return StepResult(
                False,
                f"环境配置步骤不足 {ONE_CLICK_ENV_COUNT} 个，无法一键部署",
                "",
            )

        results: list[tuple[TestStep, StepResult]] = []
        logs: list[str] = []

        log(f"一键部署开始：将依次执行环境配置 1～{ONE_CLICK_ENV_COUNT} 步")
        for i, step in enumerate(sub_steps, 1):
            log("")
            log(f"======== 一键部署 {i}/{ONE_CLICK_ENV_COUNT}: {step.title} ========")

            # 步骤 2：弹出压缩包选择（与环境配置「上传部署环境压缩包」一致）
            if step.id == "env_package_deploy":
                log("请选择本机环境压缩包…")
                path = ""
                if ctx.request_env_package:
                    path = (ctx.request_env_package() or "").strip()
                if not path or not Path(path).is_file():
                    result = StepResult(
                        False,
                        "已取消或文件无效：未选择有效环境压缩包",
                        "",
                    )
                    step.status = StepStatus.FAIL
                    step.last_message = result.message
                    results.append((step, result))
                    logs.append(f"[{i}] {step.title}: FAIL — {result.message}")
                    log(f"—— 子步骤结束: {step.title} → 失败（{result.message}）——")
                    continue
                ctx.local_package_path = path
                ctx.config.setdefault("env_package", {})["local_path"] = path
                log(f"已选择环境压缩包: {path}")

            step.status = StepStatus.RUNNING
            try:
                result = step.run(ctx, log)
            except Exception as exc:  # noqa: BLE001
                result = StepResult(False, f"异常: {exc}", str(exc))
            step.last_message = result.message
            step.last_log = result.log
            if result.ok:
                step.status = (
                    StepStatus.MANUAL if result.needs_manual_confirm else StepStatus.PASS
                )
            else:
                step.status = StepStatus.FAIL
            results.append((step, result))
            logs.append(f"[{i}] {step.title}: {'OK' if result.ok else 'FAIL'} — {result.message}")
            log(
                f"—— 子步骤结束: {step.title} → "
                f"{'成功' if result.ok else '失败'}（{result.message}）——"
            )

        log("")
        log("======== 一键部署结果汇总 ========")
        ok_n = 0
        fail_n = 0
        for i, (step, result) in enumerate(results, 1):
            if result.ok:
                ok_n += 1
                mark = "成功"
            else:
                fail_n += 1
                mark = "失败"
            log(f"  {i}. [{mark}] {step.title} — {result.message}")
        log(f"合计: 成功 {ok_n} / 失败 {fail_n} / 共 {len(results)}")

        log("")
        log("======== 设备 Camera 配置 ========")
        cam = self._dump_camera_json(ctx, log)
        logs.append(cam)

        all_ok = fail_n == 0
        summary = (
            f"一键部署完成：成功 {ok_n}，失败 {fail_n}"
            if all_ok
            else f"一键部署部分失败：成功 {ok_n}，失败 {fail_n}"
        )
        return StepResult(all_ok, summary, "\n".join(logs))

    @staticmethod
    def _dump_camera_json(ctx: AppContext, log: LogFn) -> str:
        """打印 /etc/anyverse_config/device/<hostname>_camera.json。"""
        cmd = r"""
set +e
hn=$(tr -d '[:space:]' </etc/hostname 2>/dev/null || hostname | tr -d '[:space:]')
base=$(echo "$hn" | sed 's/-docker$//')
echo "hostname_raw=$hn"
echo "device_base=$base"
for cand in "$base" "$hn"; do
  f="/etc/anyverse_config/device/${cand}_camera.json"
  if [ -f "$f" ]; then
    echo "camera_file=$f"
    echo "----- BEGIN camera.json -----"
    if command -v sudo >/dev/null 2>&1; then
      sudo cat "$f" 2>/dev/null || cat "$f" 2>/dev/null
    else
      cat "$f" 2>/dev/null
    fi
    echo
    echo "----- END camera.json -----"
    exit 0
  fi
done
# 回退：任意 kitt15-*_camera.json
f=$(ls /etc/anyverse_config/device/kitt15-*_camera.json 2>/dev/null | head -1)
if [ -n "$f" ] && [ -f "$f" ]; then
  echo "camera_file=$f (fallback)"
  echo "----- BEGIN camera.json -----"
  sudo cat "$f" 2>/dev/null || cat "$f" 2>/dev/null
  echo
  echo "----- END camera.json -----"
  exit 0
fi
echo "CAMERA_JSON_MISSING"
ls -la /etc/anyverse_config/device/ 2>/dev/null || true
exit 1
""".strip()
        res = ctx.ssh.exec_host(cmd, log=log, timeout=60, stream_output=True)
        if "CAMERA_JSON_MISSING" in (res.combined or ""):
            log("未找到 *_camera.json（请确认相机初始化 / Camera SN 是否已生成该文件）")
        return res.combined or ""


DEPLOY_STEPS: list[TestStep] = [
    OneClickDeployStep(),
]
