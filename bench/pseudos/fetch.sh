#!/usr/bin/env bash
# 一键从 Rutgers GBRV 官方站下载 USPP PBE v1.5 赝势库并验证。
#
# 流程：
#   1. 读 pseudos/GBRV_USPP_PBE_v1.5/MANIFEST.json 拿 tarball URL + SHA256
#   2. curl 下载到临时文件
#   3. 校验 SHA256，不匹配则退出
#   4. 解压到 pseudos/GBRV_USPP_PBE_v1.5/raw/
#   5. cd 到该目录，跑 build_manifest.py 重建 <Element>.upf symlinks
#   6. 对比重建后的 MANIFEST.json 与仓库内的，确认逐字节一致
#
# 运行：
#   从 repo 根目录：bash pseudos/fetch.sh
#   或直接：      cd pseudos && bash fetch.sh
#
# 依赖：bash, curl, tar, python3, sha256sum, jq（可选；没有则退化用 python 解析 JSON）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAMILY_DIR="$SCRIPT_DIR/GBRV_USPP_PBE_v1.5"
MANIFEST="$FAMILY_DIR/MANIFEST.json"
RAW_DIR="$FAMILY_DIR/raw"

if [[ ! -f "$MANIFEST" ]]; then
    echo "ERROR: $MANIFEST 不存在" >&2
    exit 1
fi

# 从 MANIFEST.json 读 URL 和 SHA256（优先 jq，fallback Python）
read_field() {
    local key="$1"
    if command -v jq &>/dev/null; then
        jq -r ".$key" "$MANIFEST"
    else
        python3 -c "import json; print(json.load(open('$MANIFEST'))['$key'])"
    fi
}

URL="$(read_field source_url)"
EXPECTED_SHA256="$(read_field tarball_sha256)"

echo "[fetch] 目标：$URL"
echo "[fetch] 期望 SHA256：$EXPECTED_SHA256"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

TARBALL="$TMPDIR/all_pbe_UPF_v1.5.tar.gz"
echo "[fetch] 下载到 $TARBALL …"
curl -fSL -o "$TARBALL" "$URL"

ACTUAL_SHA256="$(sha256sum "$TARBALL" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
    echo "ERROR: SHA256 不匹配" >&2
    echo "  期望: $EXPECTED_SHA256" >&2
    echo "  实际: $ACTUAL_SHA256" >&2
    exit 2
fi
echo "[fetch] SHA256 校验通过 ✓"

mkdir -p "$RAW_DIR"
# tarball 内部直接放了 65 个 UPF 文件（非子目录），extract 到 raw/
echo "[fetch] 解压到 $RAW_DIR …"
tar -xzf "$TARBALL" -C "$RAW_DIR"
UPF_COUNT="$(find "$RAW_DIR" -maxdepth 1 -name '*.UPF' | wc -l)"
echo "[fetch] 解压到 $UPF_COUNT 个 UPF 文件"

# 备份当前 MANIFEST 做 diff
BEFORE_MANIFEST="$TMPDIR/manifest_before.json"
cp "$MANIFEST" "$BEFORE_MANIFEST"

echo "[fetch] 重建 symlinks + MANIFEST.json …"
( cd "$FAMILY_DIR" && python3 build_manifest.py )

if ! diff -q "$BEFORE_MANIFEST" "$MANIFEST" &>/dev/null; then
    echo "WARN: 重建后的 MANIFEST.json 与仓库内版本不完全一致 ——" >&2
    echo "      可能 GBRV 官方更新了 tarball 内容。diff:" >&2
    diff "$BEFORE_MANIFEST" "$MANIFEST" | head -40 >&2
    exit 3
fi

echo "[fetch] MANIFEST 与仓库内版本逐字节一致 ✓"
echo "[fetch] 完成。赝势库可用于 bench 的 QE 运行。"
