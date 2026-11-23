# DFT Agent Bench

单题文件在 `questions/M*_T*/`，对应目录内的 `gt_data/<task_id>/` 保存 DOS、EOS 或弹性参考数据。
[逐题清单](task_manifest.tsv)给出全部 316 个 ID 的分组和 GT 质量。[4 × 4 覆盖表](../README.md#benchmark-design-four-material-tiers--four-workflows)列出了每格数量。

- `stable`：245 道 M1/M2 正式评测题。按上游 schema，`metadata.is_stable` 缺省取 true。
- `exploratory`：30 道 M3 磁性体系题，单列报告。论文讨论了 CoO 与 Cr₂O₃ 的 DFT+U 诊断。
- `held_out`：41 道显式标为 `is_stable=false` 的题，部分参考值为空。仅作诊断。
- `frozen`：论文 100 题固定切片，跨越以上三组，可凭 ID 与结果 CSV 对接。

以下命令均从本目录（`bench/`）执行。浏览题目只需 Python 3.10+：

```bash
python3 harness/run.py --list-tasks stable
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
也可以使用参考实现 `../agent/v1/agent/solve.py` 或 `../agent/v5.1/solve.py`；
先按[参考实现指南](../agent/README.md)配置模型，再通过 `--solver-args` 传入模型参数。
公开版与论文评测范围的对应关系见[仓库首页](../README.md#public-edition-and-paper-scope)。

## 题目来源

任务 JSON、评分用参考曲线和四类工作流模板来自合作组源码提交 `8214293`。
`materials/` 包含 M1/M2 和 M3 材料记录，M4 候选清单不计入题目数。
`questions/generate.py` 是早期 M1/M2 T1/T2 种子生成器；复用已发布 benchmark 请直接使用冻结的题目文件。
生成种子不能复原审阅后的参考值。运行时无需 Materials Project API key。

## 数据来源

- 赝势：K. F. Garrity et al., *Comput. Mater. Sci.* **81**, 446 (2014),
  [DOI:10.1016/j.commatsci.2013.08.053](https://doi.org/10.1016/j.commatsci.2013.08.053)
- 材料结构与电子结构：[Materials Project](https://next-gen.materialsproject.org/)
- 高对称 k-path：Hinuma et al., *Comput. Mater. Sci.* **128**, 140 (2017)
