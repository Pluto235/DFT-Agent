# GBRV USPP PBE v1.5

This library supplies scalar-relativistic PBE ultrasoft pseudopotentials for Quantum ESPRESSO (QE) tasks with `material.pseudopotential.family = "GBRV_USPP_PBE_v1.5"`.

## Source and citation

The benchmark uses GBRV v1.5, downloaded on 2026-04-23. The [manifest](MANIFEST.json) records the original filenames, per-file SHA256 checksums and the upstream license (“GNU Public License”). The release page is dated 2019-08-26; the archive contents are dated 2017-06-06. [GBRV project](https://www.physics.rutgers.edu/gbrv/) · [Source archive](https://www.physics.rutgers.edu/gbrv/all_pbe_UPF_v1.5.tar.gz)

Archive SHA256: `4ebfeaa4c9d916c1ac2da6e89ec888fdffce096714f1956db01fea5ec9c9e4f0`

Cite K. F. Garrity, J. W. Bennett, K. M. Rabe and D. Vanderbilt, *Computational Materials Science* **81**, 446 (2014). [DOI](https://doi.org/10.1016/j.commatsci.2013.08.053)

## Install and verify

From the repository root:

```bash
bash bench/setup.sh
```

Setup downloads the archive, verifies its checksum, extracts the 65 UPF files into `raw/`, and builds 65 canonical symlinks. Git tracks the manifest and build script; setup creates the installed files locally. If `raw/` is already populated, you can rebuild the symlinks and recalculate the manifest:

```bash
cd bench/pseudos/GBRV_USPP_PBE_v1.5
python3 build_manifest.py
```

## Element coverage and filenames

The 65 files cover 64 elements. The extra file is an Hf variant for the +4 oxidation state. The library covers La, but omits the other lanthanides, noble gases, actinides and elements above Bi. See `covered_elements` and `not_covered` in the [manifest](MANIFEST.json) for the element lists.

The task schema maps element symbols to canonical filenames, for example `{"Si": "Si.upf", "Al": "Al.upf", "Cu": "Cu.upf"}`. The symlinks retain the upstream file and version for each element:

| Canonical filename | Installed target |
| --- | --- |
| `Ag.upf` | `raw/ag_pbe_v1.4.uspp.F.UPF` |
| `Cu.upf` | `raw/cu_pbe_v1.2.uspp.F.UPF` |
| `Hf.upf` (standard) | `raw/hf_pbe_v1.uspp.F.UPF` |
| `Hf_plus4.upf` | `raw/hf_pbe_plus4_v1.uspp.F.UPF` |

## Use in QE

Set `pseudo_dir` to the installed family directory and use the canonical names in `ATOMIC_SPECIES`. The harness also makes the task's requested files available under `./pseudos/` in its work directory.

```fortran
&CONTROL
  pseudo_dir = '<repo-root>/bench/pseudos/GBRV_USPP_PBE_v1.5'
/

ATOMIC_SPECIES
  Si  28.0855  Si.upf
  O   15.999   O.upf
```

The library guidance uses a wavefunction cutoff of at least 40 Ry and a charge-density cutoff of at least eight times the wavefunction cutoff. Tighter calculations may require 50 Ry or more for the wavefunctions. Check convergence for the material and observable.
