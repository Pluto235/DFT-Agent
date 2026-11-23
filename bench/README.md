# DFT-Agent Bench

Each file in `questions/M*_T*/` defines one task. Where a task uses a DOS, equation-of-state or elastic reference curve, its data lives under `gt_data/<task_id>/` in the same task group. The [task manifest](task_manifest.tsv) lists all 316 IDs, their release sets and reference quality. The [coverage figure](../README.md#benchmark-design-four-material-tiers--four-workflows) shows the counts by material tier and workflow.

| Selection | Tasks | Use |
| --- | ---: | --- |
| `stable` | 245 | Standard M1/M2 comparisons. The upstream schema treats a missing `metadata.is_stable` flag as true. |
| `exploratory` | 30 | M3 magnetic systems. Report these separately. The paper discusses DFT+U diagnostics for CoO and Cr₂O₃. |
| `held_out` | 41 | Diagnostics. These tasks have `metadata.is_stable=false`, and some reference values are missing. |
| `frozen` | 100 | The paper's fixed diagnostic slice. Its IDs span the three sets above. |

Run the commands below from `bench/`. Listing tasks requires only Python 3.10 or later.

```bash
python3 harness/run.py --list-tasks stable
```

To try scoring before installing Quantum ESPRESSO (QE), use the [saved silicon output](harness/README.md#score-a-saved-output).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
bash setup.sh
```

The setup script downloads the GBRV USPP PBE v1.5 pseudopotentials and verifies their SHA256 checksum. For live calculations, install QE and put its executables on `PATH`, or set `QE_GPU_WRAPPER_DIR` to the directory containing binaries such as `pw.x`, `bands.x` and `dos.x`.

## Inspect a task

```bash
jq . questions/M1_T1/M1_T1_Si_vc_relax.json
jq . questions/M1_T2/M1_T2_Si_bands_dos.json
```

The task JSON describes the material, workflow, ground truth, scoring rules and execution policy. A T2 DOS reference uses `ground_truth.*.value.ref_file` to identify its data file.

## Run your own solver

Install your solver's dependencies in the same Python environment and replace the example path with your adapter.

```bash
python harness/run.py \
  --task-id M1_T1_Si_vc_relax \
  --solver /path/to/your/solve.py \
  --runs-dir runs
```

The [solver interface](harness/README.md#solver-interface) covers arguments, required QE outputs, logs and report fields. To run either reference implementation, configure its provider and pass model options through `--solver-args`. See the [reference agent commands](../agent/README.md) and the [scope of the paper comparison](../README.md#public-edition-and-paper-scope).

## Task provenance

The task JSONs, scoring curves and four workflow templates come from the collaborators' snapshot `8214293`. The `materials/` directory contains the M1/M2 and M3 material records. M4 candidates do not contribute to the release counts.

Use the frozen files in `questions/` to reproduce the benchmark. The earlier seed generator, `questions/generate.py`, covers M1/M2 T1/T2 and predates the reviewed reference values. Running the benchmark requires no Materials Project API key.

## Data sources

- Pseudopotentials: K. F. Garrity et al., *Computational Materials Science* **81**, 446 (2014). [DOI](https://doi.org/10.1016/j.commatsci.2013.08.053)
- Materials and electronic structure: [Materials Project](https://next-gen.materialsproject.org/)
- High-symmetry k-point paths: Hinuma et al., *Computational Materials Science* **128**, 140 (2017).
