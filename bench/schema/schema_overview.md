# Bench Question Schema

Task IDs pair a material tier with a workflow. The [manifest](../task_manifest.tsv)
lists the files and evaluation sets; [the coverage grid](../../README.md#benchmark-design-four-material-tiers--four-workflows)
gives the counts. Task JSONs are under `questions/<tier>_<workflow>/`.

Each question is one JSON file, and its filename matches its `id`. Numeric
ground-truth values and scoring rules are embedded in the JSON. Larger
reference data, such as a DOS curve, is stored beside the question and
referenced through `value.ref_file`.

## Top-Level Fields

| Field | Required | Purpose |
|---|---:|---|
| `schema_version` | yes | Schema compatibility marker, `1.2` or `1.3` in this snapshot |
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

In v1.3, the ID prefix matches `difficulty.material_tier` and `difficulty.task_tier`. The 30 M3 files use v1.2 and store `difficulty` as the string `M3`; read their workflow tier from the ID or manifest.

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

### T3 and T4

EOS tasks fit an energy–volume curve. Elastic tasks fit stiffnesses from finite strains and stresses. See the [prompt templates](prompts/) for the required outputs.

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

Two examples from the M1 tier:

| Example | Focus |
|---|---|
| `example_M1_T1_Si_vc_relax.json` | Structure relaxation and mixed scoring rules |
| `example_M1_T2_Si_bands_dos.json` | Combined bands/DOS evaluation |

Each JSON example has a matching `*.review.md` human-readable walkthrough.
Prompt templates and rendering rules are documented in `prompts/README.md`.
