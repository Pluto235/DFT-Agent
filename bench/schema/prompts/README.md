# Benchmark prompt templates

The harness selects a plain-text template using `task.type`, fills its named slots from the task JSON, and appends the submission instructions. If the task forbids network access, it appends the network policy last. The [task loader](../../harness/lib/task_loader.py) implements this sequence.

| Tier | Workflow | Template |
| --- | --- | --- |
| T1 | Variable-cell relaxation | [vc_relax.txt](vc_relax.txt) |
| T2 | Band structure and density of states | [bands_dos.txt](bands_dos.txt) |
| T3 | Equation of state | [eos.txt](eos.txt) |
| T4 | Elastic stiffness | [elastic_stiffness.txt](elastic_stiffness.txt) |

## Template slots

The renderer replaces each literal `{slot_name}` with its value. Templates have no front matter.

| Slot | Task JSON field | Rendered value |
| --- | --- | --- |
| `formula` | `material.formula` | Chemical formula, e.g. Si |
| `structure_type` | `material.structure` | Prototype name, e.g. diamond or zinc blende |
| `space_group` | `material.space_group` | Space-group symbol, e.g. Fd-3m |
| `atoms_per_primitive_cell` | `material.atoms_per_primitive_cell` | Atom count |
| `initial_structure_block` | `material.initial_structure` | Primitive-cell lengths and angles, fractional atomic coordinates and any structure note |
| `pseudopotential_block` | `material.pseudopotential` | Family, relativistic treatment and element-to-UPF mapping |
| `advisory_notes_block` | `material.advisory_notes` | Optional task-specific notes |

Use a plain prototype name such as `cubic perovskite`. The template combines the formula and prototype itself. The review label `material.name` is separate.

### Initial structure

The prompt gives `a`, `b` and `c` in angstrom and the three angles in degrees. It renders the `atomic_positions_crystal` entries as `ATOMIC_POSITIONS` in fractional coordinates with six decimal places. The solver converts these inputs to the QE cell representation it chooses. The loader accepts both the keyed lattice parameters used by M1/M2 and the ordered lists used by some M3 tasks.

For a solver writing an explicit QE `CELL_PARAMETERS angstrom` block, one Cartesian convention is to place the first vector along the x-axis and the second in the xy-plane. With $\alpha$, $\beta$ and $\gamma$ denoting the interaxial angles, the three rows are:

$$
(a,0,0),\quad
(b\cos\gamma,b\sin\gamma,0),\quad
(c\cos\beta,cq,c\sqrt{1-\cos^2\beta-q^2}),
\qquad q=\frac{\cos\alpha-\cos\beta\cos\gamma}{\sin\gamma}.
$$

An equivalent basis, such as the symmetric primitive vectors for diamond silicon, describes the same lattice when the positions are expressed in that basis.

### Pseudopotentials and advisory notes

Each species maps to a filename under `./pseudos/` in the work directory, for example `Si: Si.upf`. The advisory slot is empty when the task has no advisory notes. A string produces one `Note:` line, and a list produces one per entry. A note supplied with the initial structure is rendered in the structure block.

## Inspect a rendered prompt

Run this from `bench/` to see the exact text passed to the solver for silicon relaxation, including the required output filename key and the network policy. It requires neither a model API key nor a QE run.

```bash
python3 - <<'PY'
from pathlib import Path
from harness.lib.task_loader import load_task, render_prompt

bench = Path.cwd()
task = load_task(bench, "M1_T1_Si_vc_relax")
print(render_prompt(bench, task), end="")
PY
```

## Field visibility

Keep reference answers, scoring rules and evaluator metadata on the harness side. When changing a template or the fields it consumes, check the [agent-visible field specification](../fields/agent_visible.md) and the [task loader tests](../../harness/tests/test_task_loader.py). The field specification and [earlier prompt design notes](../prompt_generation_strategy.md) are in Chinese.
