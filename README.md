# DFT-Agent Bench

Evaluate your own solver on Quantum ESPRESSO (QE) workflows. This public edition
contains **116 tasks for 58 M1 materials**. The harness renders each prompt,
scores properties extracted from the solver's QE outputs, and checks execution
evidence before assigning the final score.

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
