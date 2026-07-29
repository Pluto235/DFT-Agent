# Ground-Truth Self-Run Tools

These utilities reproduce and inspect Quantum ESPRESSO ground truth for the
retained M1 benchmark. The current dataset contains 58 T1 questions and 58 T2
questions.

Canonical outputs are stored in:

```text
canonical_gt/      # M1 T1 ground truth
canonical_gt_t2/   # M1 T2 ground truth and DOS data
```

The top-level T1 flow is split into parameter generation (`params.py`), QE
execution (`run.py`), analysis (`analyze.py`), and report generation
(`report.py`). T2-specific execution code is under `t2/`.

These scripts are benchmark-maintenance tools, not part of the solver API.
Normal practice runs should use `bench/harness/run.py`.
