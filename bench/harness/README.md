# Evaluation harness

Run the commands below from `bench/`. List tasks with
`python3 harness/run.py --list-tasks`. For a live run, follow the
[benchmark setup and solver command](../README.md#运行自己的-solver).

## Score a saved output

With the Python dependencies from `bench/requirements.txt` installed, try
field extraction and scoring on the bundled Si relaxation test fixture:

```bash
run="$(mktemp -d)/demo_M1_T1_Si_vc_relax"
mkdir -p "$run/workdir"
cp tools/gt_selfrun/tests/fixtures/sample_relax_converged.out "$run/workdir/relax.out"
echo '{"task_outputs":{"vc_relax_out":"relax.out"}}' > "$run/workdir/result.json"
python harness/rerun_eval.py --run-dir "$run"
python -m json.tool "$run/report_rerun.json"
```

Inspect `extracted_fields`, `per_field_scores` and `total_score`. This replay
scores an abbreviated test fixture and skips the execution audit. The expected
raw score is 0.0: the extracted space group is 166 while the Si reference is
227, and the lattice parameters also fall outside tolerance. For a measured
benchmark result, use the live harness and report its gated score.

## Solver interface

The harness starts your Python script with the same interpreter as the harness,
in the prepared work directory. It passes:

| Argument | Purpose |
| --- | --- |
| `--prompt` | Read the rendered task prompt, including required outputs and task policy. |
| `--workdir` | Run QE here. Use the supplied `pseudos/` directory and save your calculation outputs here. |
| `--out` | Write a JSON object here with a `task_outputs` map of keys to workdir-relative file paths. |

For T1, return `vc_relax_out` (the final relaxation log). For T2, return
`scf_out`, `bands_out`, `bands_data`, `dos_dat` and `nscf_dos_out`. The prompt
explains each file. For T3, return `eos_points_csv`; for T4, return
`stress_strain_csv`. Keep the individual QE `.out` logs in the work directory
so the harness can audit the volume or strain calculations. Log the shell commands actually executed to `workdir/bash.log`
using `$ command` lines. The audit reads this log for QE invocation and
network commands, and checks completion and timing in the submitted outputs.

## Minimal Python adapter

Save this as `solve.py` alongside your own code. Replace the example import and call with your agent's API. Here `run_agent` receives the prompt text and working directory and returns a dictionary of output paths on completion, or `None` if the calculation could not finish.

```python
import argparse
import json
from pathlib import Path

from my_agent import run_agent  # Replace with your agent's entry point.

parser = argparse.ArgumentParser()
parser.add_argument("--prompt", type=Path, required=True)
parser.add_argument("--workdir", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()

outputs = run_agent(args.prompt.read_text(), args.workdir)
if outputs is None:
    result = {"incomplete": True, "reason": "agent did not finish the calculation"}
else:
    result = {"task_outputs": outputs}
args.out.write_text(json.dumps(result))
```

For example, a successful T1 call that wrote `relax.out` directly under `--workdir` should return `{"vc_relax_out": "relax.out"}`. Instrument the agent's command runner to append each actual invocation to `bash.log` in that directory as it runs. Install the agent's dependencies and configure any provider credentials before launching the harness. If your adapter accepts extra flags such as `--model`, forward them with `--solver-args="--model YOUR_MODEL"`.

## Read a live result

Open `<runs-dir>/<UTC>_<task_id>/report.json`. With the documented command,
the timestamped run directories are under `bench/runs/`.

- `total_score_raw` and `per_field_scores` describe agreement with the rubric.
- `audit_flags` records six checks. Four gate the score: `qe_invoked`,
  `job_done`, `qe_wallclock_ok` and `no_network_access`. The other two,
  `paths_in_workdir` and `files_nonempty`, are diagnostic.
- `total_score` equals the raw score when `audit_passed` is true and zero when
  the gate fails. For example, a missing QE invocation in the command log can
  invalidate a numerically accurate result.
- A missing or incomplete solver result or failed extraction produces a failure
  report with `failure_reason`. Keep these failures visible when aggregating runs.

The harness saves `solver_stdout.log` and `solver_stderr.log` beside the report. Start with stderr if the adapter exited before writing its result. For an execution-audit failure, inspect `workdir/bash.log` and the original QE output logs. A completed harness process can still produce a gated score of zero.
