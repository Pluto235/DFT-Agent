# Bench Question Schema

The public practice set uses schema version `1.3`. It contains two task
families over the M1 material tier:

- `M1_T1_<material>_vc_relax`
- `M1_T2_<material>_bands_dos`

## Layout

```text
bench/
├── questions/
│   ├── M1_T1/
│   └── M1_T2/
├── schema/
│   ├── examples/
│   ├── fields/
│   └── prompts/
└── pseudos/
```

Each question is one JSON file, and its filename matches its `id`. Numeric
ground-truth values and scoring rules are embedded in the JSON. Larger
reference data, such as a DOS curve, is stored beside the question and
referenced through `value.ref_file`.

## Top-Level Fields

| Field | Required | Purpose |
|---|---:|---|
| `schema_version` | yes | Schema compatibility marker, currently `1.3` |
| `id` | yes | Globally unique task identifier |
| `difficulty` | yes | Material and task tier labels |
| `material` | yes | Structure, source, and pseudopotential mapping |
| `task` | yes | Task type, prompt override, and runtime policy |
| `evaluator` | yes | Output files and fields to extract |
| `ground_truth` | yes | Reference values, units, tolerances, and rules |
| `scoring` | yes | Field weights and aggregation policy |
| `convergence_evidence` | conditional | Evidence supporting the chosen GT parameters |
| `metadata` | no | Provenance and quality annotations |

Detailed definitions are in `fields/`.

## Identifiers

Identifiers use only letters, digits, and underscores:

```text
M1_T1_Si_vc_relax
M1_T2_GaAs_bands_dos
```

`difficulty.material_tier` is `M1`; `difficulty.task_tier` is `T1` or `T2`.

## Task Types

### T1: `vc_relax`

The agent performs a Quantum ESPRESSO variable-cell relaxation. Typical
scored fields are lattice constants, lattice angles, and space-group number.
The evaluator reads the submitted relaxation output and applies relative,
absolute, or exact matching as declared by each ground-truth field.

### T2: `bands_dos`

The agent performs SCF/NSCF, band-structure, and density-of-states work. The
submission includes QE log files plus machine-readable bands and DOS data.
Typical scored fields include band gap, gap type, VBM/CBM labels, Fermi-level
DOS, and DOS-curve similarity.

## Units

| Quantity | Unit |
|---|---|
| Length | angstrom |
| Angle | degree |
| Energy and band gap | eV |
| Stress | kbar |
| Bulk modulus | GPa |
| Charge | elementary charge (`e`) |

Every numeric ground-truth field declares its unit when applicable.

## Runtime Policy

`task.policy.network_allowed` is `false` for the retained questions. The
harness renders this policy into the task prompt and audits submitted command
logs for forbidden network access.

## Examples

The current examples cover both retained task types:

| Example | Focus |
|---|---|
| `example_M1_T1_Si_vc_relax.json` | Structure relaxation and mixed scoring rules |
| `example_M1_T2_Si_bands_dos.json` | Combined bands/DOS evaluation |

Each JSON example has a matching `*.review.md` human-readable walkthrough.
Prompt templates and rendering rules are documented in `prompts/README.md`.
