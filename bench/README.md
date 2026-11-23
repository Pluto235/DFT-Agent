# DFT Agent Bench

这是 DFT-Agent 的公开练习题集，围绕 Quantum ESPRESSO 和 GBRV USPP PBE
赝势构建。当前版本只包含 M1 材料和 T1/T2 两类任务：

- 材料：**58 个 M1 材料**
- T1：58 道 `vc-relax` 结构弛豫题
- T2：58 道 `bands + DOS` 电子结构题
- 总计：**116 道题**
- Schema：v1.3

## 目录

```text
bench/
├── harness/                  # 运行、审计和评分
├── materials/
│   ├── m1.yaml              # M1 材料清单
│   ├── m1_resolved.yaml     # Materials Project 校验后的结构数据
│   └── resolve.py           # 材料解析与校验脚本
├── pseudos/                  # GBRV 清单、下载和校验工具
├── questions/
│   ├── M1_T1/               # 58 道结构弛豫题
│   ├── M1_T2/               # 58 道电子结构题及 DOS 参考数据
│   └── generate.py          # 题目生成脚本
└── schema/                   # 字段规范、示例和 prompt 模板
```

M2 及更高材料层、T3/T4 任务和 web 平台不在本公开版中。

以下命令均从本目录（`bench/`）执行。浏览题目只需 Python 3.10+：

```bash
python3 harness/run.py --list-tasks
```

[用已有 Si 输出试评分](harness/README.md#score-a-saved-output)只需安装 Python 依赖。

## 环境准备

需要 Python 3.10+。实际运行题目还需要 Quantum ESPRESSO；可通过
`QE_GPU_WRAPPER_DIR` 指向包含 `pw.x`、`bands.x` 和 `dos.x` 的目录。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash setup.sh
```

`setup.sh` 会下载 GBRV USPP PBE v1.5 赝势并校验 SHA256。原始 UPF 文件
不提交到仓库。

## 查看题目

```bash
jq . questions/M1_T1/M1_T1_Si_vc_relax.json
jq . questions/M1_T2/M1_T2_Si_bands_dos.json
```

每道题的 JSON 包含材料、任务、ground truth、评分规则和审计策略。T2 的
DOS 曲线通过 `ground_truth.*.value.ref_file` 引用同目录下的参考数据。

## 运行自己的 solver

将路径换成你的 Python 入口，在同一 Python 环境中安装它需要的依赖。

```bash
python harness/run.py \
  --task-id M1_T1_Si_vc_relax \
  --solver /path/to/your/solve.py \
  --runs-dir runs
```

[Harness 文档](harness/README.md#solver-interface)列出了参数、QE 输出、日志和报告字段。
也可以使用参考实现 `../agent/v1/agent/solve.py` 或 `../agent/v4.3/solve.py`；
先按对应 README 配置模型，再通过 `--solver-args` 传入模型参数。
公开版与论文评测范围的对应关系见[仓库首页](../README.md#public-edition-and-paper-scope)。

## 重新生成题目

题目生成需要 Materials Project API key 以及 `pymatgen`、`mp-api`、
`seekpath` 和 `pyyaml`：

```bash
export MP_API_KEY=<your-key>
python questions/generate.py
```

只生成 T1 或单个材料：

```bash
python questions/generate.py --task T1
python questions/generate.py --only-id M1_Si
```

生成逻辑读取 `materials/m1_resolved.yaml`，只会写入 `M1_T1` 和 `M1_T2`。

## 数据来源

- 赝势：K. F. Garrity et al., *Comput. Mater. Sci.* **81**, 446 (2014),
  [DOI:10.1016/j.commatsci.2013.08.053](https://doi.org/10.1016/j.commatsci.2013.08.053)
- 材料结构与电子结构：[Materials Project](https://next-gen.materialsproject.org/)
- 高对称 k-path：Hinuma et al., *Comput. Mater. Sci.* **128**, 140 (2017)
