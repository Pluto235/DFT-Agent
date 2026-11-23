# DFT-Agent Bench

Evaluate your own solver on Quantum ESPRESSO (QE) workflows. This public edition
includes **245 stable M1/M2 tasks** and **30 exploratory M3 tasks**. Another
41 task definitions are retained for diagnostics. The harness renders each prompt,
scores properties extracted from the solver's QE outputs, and checks execution
evidence before assigning the final score.

## Benchmark design: four material tiers × four workflows

Each task pairs a material with a QE workflow. The **M axis** groups materials by
the physics and crystal structure a solver must handle. The **T axis** specifies
the calculation and the observable to recover. For example, moving from M1/T1
to M1/T2 keeps the material pool fixed and changes the target from a relaxed
structure to electronic properties. The two labels do not imply a single
numerical difficulty score.

| Material tier | T1 · Variable-cell relaxation | T2 · Bands + DOS | T3 · Equation of state | T4 · Elastic tensor |
| --- | ---: | ---: | ---: | ---: |
| **M1** · Simple elemental and binary crystals | [52 / 58](bench/questions/M1_T1/) | [43 / 58](bench/questions/M1_T2/) | [52 / 52](bench/questions/M1_T3/) | [45 / 52](bench/questions/M1_T4/) |
| **M2** · Larger or lower-symmetry cells | [25 / 28](bench/questions/M2_T1/) | [22 / 28](bench/questions/M2_T2/) | [4 / 5](bench/questions/M2_T3/) | [2 / 5](bench/questions/M2_T4/) |
| **M3** · Magnetic transition-metal systems | [15 exploratory](bench/questions/M3_T1/) | [15 exploratory](bench/questions/M3_T2/) | — | — |
| **M4** · Strongly correlated systems | — | — | — | — |

M1/M2 entries show **stable tasks / task files**. A dash means no task files
in this release. The [task manifest](bench/task_manifest.tsv) marks every ID
as stable, exploratory or held out and records its frozen-slice membership.

T1 relaxes the cell and atomic positions. T2
computes the band structure and density of states. T3 fits an energy–volume
curve to obtain equilibrium volume and bulk modulus. T4 uses finite strains and
their stresses to recover the elastic stiffness tensor.

## Start with the benchmark

From the repository root, list the standard evaluation set with Python 3.10+:

```bash
python3 bench/harness/run.py --list-tasks stable
```

- **Explore a task.** Inspect the [silicon relaxation task](bench/questions/M1_T1/M1_T1_Si_vc_relax.json) for its input structure, reference values and scoring tolerances. During evaluation, pass only the rendered prompt and prepared working directory to the solver.
- **Try scoring offline.** Use a [bundled silicon output fixture](bench/harness/README.md#score-a-saved-output) to inspect field scores without running QE or calling a model.
- **Evaluate your solver.** Follow the [setup and single-task command](bench/README.md#运行自己的-solver). The [solver interface](bench/harness/README.md#solver-interface) specifies the arguments, output files and execution log. Any Python adapter implementing it can participate.

## Public edition and paper scope

The original [116-task practice release](https://github.com/Pluto235/DFT-Agent/tree/5763997) covered M1/T1 and M1/T2. This release adds 200 task definitions from the [collaborators’ benchmark snapshot](https://github.com/wanweilin/dft-agent-bench/tree/821429331ddfbfbbc43c829797047d7b0c8965d2/questions). Of the 316 IDs, 245 form the paper's stable M1/M2 set, 30 are exploratory M3 tasks, and 41 have `metadata.is_stable=false`. Select a set explicitly when reporting aggregate scores. Include the repository commit, task IDs, solver, model, budget, repeats and treatment of failures.

## Reference agents and paper results

The two [reference solvers](agent/README.md) are the paper's **v1 near-bare ReAct** baseline and **v5.1 engineered scaffold**. Both come from the paper's [frozen review artifact](https://anonymous.4open.science/r/231e31re91287h/README.md).

| Fixed diagnostic slice | Paired tasks | v1 mean | v5.1 mean | Paired difference (95% CI) |
| --- | ---: | ---: | ---: | ---: |
| `deepseek-v4-pro`, retrieval disabled | 100 | 0.706 | 0.876 | +0.170 [0.108, 0.234] |

Scores are provenance-gated; failed runs count as zero. The 100 IDs span 58 stable, 30 exploratory and 12 held-out tasks in this checkout. The table reproduces the archived scores on that diagnostic slice. See the [per-task scores](results/frozen_matched_scores.csv), [reproduction command and model-specific results](results/README.md), and [paper](https://arxiv.org/abs/2611.10629).

### Results across workflows and material tiers

The following figures reproduce the broader model-matched evaluation in the [paper](https://arxiv.org/abs/2611.10629). The table above reports the separate 100-task frozen comparison.

![Baseline and engineered agent scores by workflow, shown separately for DeepSeek and Opus](results/figures/fig_compare.png)

**Figure 1.** On common M1/M2 tasks, both models gain on bands + DOS. Elastic-tensor scores rise for DeepSeek and change little for Opus. Error bars show 95% task-clustered bootstrap intervals.

![Heatmaps of the change in gated score from v1 to v5.1 by material tier, workflow and model](results/figures/fig_tierheat.png)

**Figure 2.** Each cell shows the change in execution-gated score from v1 to v5.1. On M3 relaxation, both models gain 0.17. Hatched cells contain fewer than ten paired tasks.

## License

Project code is licensed under the [MIT License](LICENSE). Third-party datasets,
pseudopotentials and referenced materials retain their respective terms.
