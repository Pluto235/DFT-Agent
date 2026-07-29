# Harness — dft-agent-bench v1.3 跑题基础设施

本目录是 bench v1.3 的跑题 harness，**只读** bench 的 `questions/` / `schema/` / `materials/` / `pseudos/`，产物是一份可跑的 `python harness/run.py` CLI。

## 硬性约束

- harness 只**读**，不**写** bench 的任何题目或评分内容
- 任何需要改题、改 evaluator、改 ground truth、改 scoring 的想法，必须走 bench schema 层讨论，**不得**在 harness 内 workaround
- 删除整个 `harness/` 子目录不影响 bench 本身（`questions/*.json` 和 `generate.py` 继续可用）

## 用法

```bash
python harness/run.py \
  --task-id M1_T1_Si_vc_relax \
  --solver  <path_to_solver/agent/solve.py> \
  --solver-args '--model claude-sonnet-4-6' \
  --runs-dir runs/ \
  [--timeout 1800]
```

## 依赖

需要 Python 3.10+；harness 自身的 Python 依赖在 `requirements.txt`。

## 契约

- **输入**：bench repo 的 `questions/<Mi_Tj>/<id>.json`
- **Solver 契约**：subprocess CLI，见设计 spec 5.2
- **产出**：`runs/<UTC>_<task_id>/report.json` + 归档的 `workdir/`

## Anti-fraud 审计

每次 run 检查 6 项（QE 是否被调、JOB DONE、路径都在 workdir 内、文件非空、pw.x wall clock 合理、无网络访问），结果附到 `report.json.audit_flags`。
