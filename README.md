# DFT-Agent Bench

Evaluate your own solver on Quantum ESPRESSO (QE) workflows. This public edition
contains **116 tasks for 58 M1 materials**. The harness renders each prompt,
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
| **M1** · Simple elemental and binary crystals (e.g., Si, GaAs) | [58](bench/questions/M1_T1/) | [58](bench/questions/M1_T2/) | — | — |
| **M2** · Larger or lower-symmetry cells (e.g., perovskites and skutterudites) | — | — | — | — |
| **M3** · Magnetic transition-metal systems requiring spin polarization | — | — | — | — |
| **M4** · Strongly correlated systems | — | — | — | — |

Counts refer to task files in **this public edition**. A dash means the checkout
contains no tasks in that cell. T1 relaxes the cell and atomic positions. T2
computes the band structure and density of states. T3 fits an energy–volume
curve to obtain equilibrium volume and bulk modulus. T4 uses finite strains and
their stresses to recover the elastic stiffness tensor.

## Start with the benchmark

From the repository root, list the task IDs with Python 3.10+:

```bash
python3 bench/harness/run.py --list-tasks
```

- **Explore a task.** Inspect the [silicon relaxation task](bench/questions/M1_T1/M1_T1_Si_vc_relax.json) for its input structure, reference values and scoring tolerances. During evaluation, pass only the rendered prompt and prepared working directory to the solver.
- **Try scoring offline.** Use a [bundled silicon output fixture](bench/harness/README.md#score-a-saved-output) to inspect field scores without running QE or calling a model.
- **Evaluate your solver.** Follow the [setup and single-task command](bench/README.md#运行自己的-solver). The [solver interface](bench/harness/README.md#solver-interface) specifies the arguments, output files and execution log. Any Python adapter implementing it can participate.

## Public edition and paper scope

| Scope | Tasks and materials | Solver coverage |
| --- | --- | --- |
| This checkout | 58 M1 materials, each with one variable-cell relaxation (T1) and one bands + DOS workflow (T2). | Bring your own solver or use the included v1 or v4.3 reference implementation. |
| Paper benchmark | 200+ tasks across multiple material tiers, also covering equations of state and elastic tensors. | The paper compares near-bare ReAct v1 with the bundled v5.1 scaffold on two models. |
| Paper's frozen analysis | A fixed 100-task diagnostic slice. The paper links a review artifact containing the slice, prompts, scorer and solver revisions. | Use the linked artifact to identify the revisions for that analysis. |

Report results for this checkout as **public M1 T1/T2**, with the Git commit,
evaluated task IDs, solver/model, budget and repeat count. The paper's aggregate
scores use different evaluation scopes. Cite [DFT-Agent Bench: Scaffold Effects
and Execution Provenance in Autonomous DFT](https://arxiv.org/abs/2611.10629).

## Reference agents

[v1](agent/v1/) is the baseline solver. [v4.3](agent/v4.3/) adds planner,
executor and verifier roles. Their READMEs cover dependencies and provider
configuration. The v5.1 scaffold used in the paper is outside this edition.

## License

Project code is licensed under the [MIT License](LICENSE). Third-party datasets,
pseudopotentials and referenced materials retain their respective terms.
