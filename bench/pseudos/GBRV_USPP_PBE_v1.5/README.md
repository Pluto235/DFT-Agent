# GBRV USPP PBE v1.5

Ultrasoft 赝势库，Quantum ESPRESSO 用。为 bench schema v1.2 `material.pseudopotential.family = "GBRV_USPP_PBE_v1.5"` 提供物理文件。

## 来源

- 官方站：<https://www.physics.rutgers.edu/gbrv/>
- Tarball：<http://www.physics.rutgers.edu/gbrv/all_pbe_UPF_v1.5.tar.gz>
- 版本：v1.5（页面标注 2019-08-26，tarball 内容时戳 2017-06-06）
- SHA256（tarball）：`4ebfeaa4c9d916c1ac2da6e89ec888fdffce096714f1956db01fea5ec9c9e4f0`
- 许可：GNU Public License
- 下载时间：2026-04-23
- 引用：K.F. Garrity, J.W. Bennett, K.M. Rabe, D. Vanderbilt, *Comput. Mater. Sci.* **81**, 446 (2014). [DOI:10.1016/j.commatsci.2013.08.053](https://doi.org/10.1016/j.commatsci.2013.08.053)

## 目录结构

```
GBRV_USPP_PBE_v1.5/
├── raw/                        # tarball 原始解压产物，65 个 UPF 文件
│   ├── ag_pbe_v1.4.uspp.F.UPF
│   ├── cu_pbe_v1.2.uspp.F.UPF
│   ├── hf_pbe_v1.uspp.F.UPF
│   ├── hf_pbe_plus4_v1.uspp.F.UPF
│   └── ... 共 65 个文件
├── Ag.upf -> raw/ag_pbe_v1.4.uspp.F.UPF   # 规范化 symlink
├── Cu.upf -> raw/cu_pbe_v1.2.uspp.F.UPF
├── Hf.upf -> raw/hf_pbe_v1.uspp.F.UPF      # 默认 Hf 用标准版
├── Hf_plus4.upf -> raw/hf_pbe_plus4_v1.uspp.F.UPF  # +4 氧化态专用
├── Si.upf -> raw/si_pbe_v1.uspp.F.UPF
├── ... 共 65 个 symlink
├── MANIFEST.json               # 完整元数据 + 每文件 SHA256
├── build_manifest.py           # 从 raw/ 重建 symlink + MANIFEST 的脚本
└── README.md                   # 本文件
```

## 元素覆盖

**包含 64 个独立元素**：H、Li、Be、B、C、N、O、F、Na–Cl、K–Br、Rb–I、Cs、Ba、La、Hf–Bi。

**不包含**：
- 稀有气体 (He, Ne, Ar, Kr, Xe)
- **镧系除 La 外全缺** (Ce, Pr, Nd, …, Lu)（0423 与杨博确认后，第一期 M2 已删除 CeFe₄Sb₁₂ 与 YbCo₄Sb₁₂ 两道题；M4 强关联时再议）
- 放射性重元素 (Po, At, Rn)
- 锕系全缺 (Ac, Th, U, ...)

详见 `MANIFEST.json` 的 `covered_elements` 与 `not_covered` 字段。

## 文件命名约定

Schema 约定（`material.pseudopotential.files`）：

```json
{ "Si": "Si.upf", "Al": "Al.upf", "Cu": "Cu.upf" }
```

**双层结构**：

| 层 | 文件名 | 用途 |
|---|---|---|
| 面向 schema | `Si.upf` / `Cu.upf` / `Hf.upf` / `Hf_plus4.upf` | 简洁、对齐 QE ATOMIC_SPECIES 元素符号首字母大写约定 |
| 面向审计 | `raw/si_pbe_v1.uspp.F.UPF`（保留原名）| 保留 GBRV 每元素 minor_version 溯源（1 / 1.2 / 1.4 / 1.5 混合）|

**Hf 特例**：默认 `Hf.upf` 指向标准版；`Hf_plus4.upf` 额外暴露给 +4 氧化态材料（强关联情景未来 M4 可能用）。

## 使用方式

QE 输入文件的 `pseudo_dir` 指向本目录：

```fortran
&CONTROL
  pseudo_dir = '<repo-root>/pseudos/GBRV_USPP_PBE_v1.5'
/

ATOMIC_SPECIES
  Si  28.0855  Si.upf
  O   15.999   O.upf
```

QE 跟随 symlink 到 `raw/` 下真实文件，无需额外配置。

## 如何重建（下载新版本或修复 symlink）

```bash
cd pseudos/GBRV_USPP_PBE_v1.5/
# 需要时重新解压到 raw/（tarball 不归档到 git）
# tar -xzf /tmp/all_pbe_UPF_v1.5.tar.gz -C raw/
python build_manifest.py
```

`build_manifest.py` 是幂等的：会清除并重建所有 `<Element>.upf` symlink，重新计算 SHA256，覆盖写 `MANIFEST.json`。

## 推荐 ecutwfc / ecutrho

GBRV v1.5 推荐值（来自官方站说明）：

- `ecutwfc ≥ 40 Ry`（严格到 `50 Ry` 以上更稳）
- `ecutrho ≥ 8 × ecutwfc`（USPP 的惯例）

比 PseudoDojo NC（推荐 `ecutwfc ≥ 80 Ry`，`ecutrho = 4 × ecutwfc`）便宜约 **一半计算量**——这是 0423 会议决定切换到 GBRV 的核心动机之一。

## Git 归档策略

- 归档：`MANIFEST.json`、`build_manifest.py`、`README.md`、**所有 symlink**（symlink 本身是文本，很小）
- 不归档：`raw/` 下的 UPF 文件（38 MB 二进制）—— 加到 `.gitignore`

重新 clone 后，下载 tarball + 运行 `build_manifest.py` 即可还原完整状态。
