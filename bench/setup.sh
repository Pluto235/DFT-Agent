#!/usr/bin/env bash
# Fresh clone 后一键准备：拉赝势 + 校验 symlinks + 检测 QE wrapper。
#
# 用法：
#   bash setup.sh
#
# 依赖：bash, curl, tar, python3, sha256sum, jq（可选，无则 fallback python）
#
# 环境变量（可选）：
#   QE_GPU_WRAPPER_DIR   包含 pw.x 的 QE 可执行文件目录。
#
# 退出码：
#   0  全部就绪（wrapper 找不到只 warning，不退出）
#   1  pseudos fetch / 校验失败
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[setup] (1/3) Fetching pseudopotentials ..."
bash pseudos/fetch.sh

echo "[setup] (2/3) Verifying pseudos manifest ..."
N=$(find pseudos/GBRV_USPP_PBE_v1.5 -maxdepth 1 -name '*.upf' 2>/dev/null | wc -l)
if [ "$N" -lt 60 ]; then
  echo "[setup] FAIL: only $N .upf symlinks under pseudos/GBRV_USPP_PBE_v1.5/ (expected ~65)." >&2
  echo "[setup]   pseudos/fetch.sh probably failed; rerun and inspect output." >&2
  exit 1
fi
echo "[setup]   OK: $N .upf symlinks"

echo "[setup] (3/3) Checking Quantum ESPRESSO ..."
if [ -n "${QE_GPU_WRAPPER_DIR:-}" ] && [ -x "$QE_GPU_WRAPPER_DIR/pw.x" ]; then
  echo "[setup]   OK: pw.x found under QE_GPU_WRAPPER_DIR"
elif command -v pw.x >/dev/null 2>&1; then
  echo "[setup]   OK: pw.x found on PATH"
else
  echo "[setup] WARNING: pw.x not found." >&2
  echo "[setup]   Install Quantum ESPRESSO or set QE_GPU_WRAPPER_DIR to its bin directory." >&2
fi

echo "[setup] DONE. Try: python harness/run.py --help"
