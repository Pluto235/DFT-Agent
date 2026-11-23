"""write_qe_input — structured QE input writer with lint + calc_id registration.

Replaces v4's heredoc-based `python3 -c "from qe_tools import ...; write_pw_input(...)"`
shell trick. The LLM no longer has to remember function signatures or escape Fortran
namelist syntax — it passes typed dicts.

Wraps:
  - qe_tools.write_pw_input — actual file generation (ASE-backed)
  - agent.tools.qe_input_lint.lint_pw_input — auto-tighten vc-relax thresholds

Returns: {calc_id, path, lint_diff, params_used}
Errors are returned as {"is_error": True, "content": [{"type": "text", "text": ...}]}
so the LLM can recover instead of the loop crashing.
"""

from __future__ import annotations

from pathlib import Path

import qe_tools
from agent.tools.qe_input_lint import lint_pw_input, read_is_metal
from agent.tools.structured.context import ToolContext


# Acceptable values for the `calc` arg. We accept QE's own names verbatim.
_VALID_CALC = {"scf", "nscf", "bands", "relax", "vc-relax"}


def _cell_from_lattice_params(params: dict) -> list[list[float]]:
    """Build a 3x3 cell (Å) from (a, b, c, alpha, beta, gamma) via pymatgen.

    Removes the hand-computation footgun (e.g. mixing the rhombohedral c_z with
    the hcp ratio doubles the cell volume). pymatgen handles every Bravais
    lattice correctly.
    """
    required = {"a", "b", "c", "alpha", "beta", "gamma"}
    missing = required - set(params)
    if missing:
        raise ValueError(f"lattice_params missing keys: {sorted(missing)}")
    from pymatgen.core import Lattice
    lat = Lattice.from_parameters(
        float(params["a"]), float(params["b"]), float(params["c"]),
        float(params["alpha"]), float(params["beta"]), float(params["gamma"]),
    )
    return [[float(x) for x in row] for row in lat.matrix]


def _build_ase_atoms(atoms_arg: dict):
    """Translate the LLM's atoms dict into an ASE Atoms object.

    Required keys: symbols (list[str]), positions_frac (list[[x,y,z]] in 0..1),
                   cell_angstrom (3x3 list, lattice vectors in Å).
    """
    from ase import Atoms

    required = {"symbols", "positions_frac", "cell_angstrom"}
    missing = required - set(atoms_arg)
    if missing:
        raise ValueError(f"atoms dict missing keys: {sorted(missing)}")

    symbols = list(atoms_arg["symbols"])
    cell = atoms_arg["cell_angstrom"]
    if len(cell) != 3 or any(len(row) != 3 for row in cell):
        raise ValueError(f"cell_angstrom must be 3x3; got shape "
                         f"({len(cell)}, {len(cell[0]) if cell else 0})")

    positions = atoms_arg["positions_frac"]
    if len(positions) != len(symbols):
        raise ValueError(f"positions_frac ({len(positions)}) must match "
                         f"symbols ({len(symbols)})")

    return Atoms(symbols=symbols, scaled_positions=positions, cell=cell, pbc=True)


def _validate_pseudos(pseudo_map: dict, workdir: Path, pseudo_dir: Path | None) -> dict:
    """Resolve each pseudo to an absolute path; verify file existence."""
    if not isinstance(pseudo_map, dict) or not pseudo_map:
        raise ValueError("pseudo_map must be a non-empty dict {symbol: upf_path}")
    resolved: dict[str, str] = {}
    for sym, upf in pseudo_map.items():
        p = Path(upf)
        candidates: list[Path] = []
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append((workdir / p).resolve())
            if pseudo_dir is not None:
                candidates.append((pseudo_dir / p.name).resolve())
        found = next((c for c in candidates if c.exists()), None)
        if found is None:
            raise FileNotFoundError(
                f"pseudo for {sym!r} not found: tried {[str(c) for c in candidates]}"
            )
        resolved[sym] = str(found)
    return resolved


async def do_write_qe_input(args: dict, *, ctx: ToolContext) -> dict:
    """Generate a QE input file and register a calc_id.

    Args (typed):
        calc          str in {scf,nscf,bands,relax,vc-relax}
        prefix        QE prefix (e.g. "Si")
        outdir        QE scratch dir (workdir-relative, e.g. "out")
        atoms         {symbols, positions_frac, cell_angstrom}
        pseudo_map    {symbol: upf_path}
        ecutwfc       float (Ry)
        ecutrho       float (Ry, typically 8-12× ecutwfc for USPP)
        kpts          [kx, ky, kz] Monkhorst-Pack grid
        occupations   "smearing" | "fixed" | "tetrahedra"  (optional)
        smearing      "mv" | "gauss" | "fd"               (optional)
        degauss       float in Ry                          (optional)
        nspin         1 | 2                                (optional)
        starting_magnetization  {symbol: float}            (optional, nspin=2)
        extra_control dict for &CONTROL                    (optional)
        extra_system  dict for &SYSTEM                     (optional)
        extra_electrons dict for &ELECTRONS                (optional;
                      e.g. electron_maxstep / mixing_beta / mixing_mode /
                      diagonalization / diago_thr_init)
        filename      output .in path (workdir-relative; default "{prefix}.in")
    """
    try:
        calc = args["calc"]
        if calc not in _VALID_CALC:
            raise ValueError(f"calc must be one of {_VALID_CALC}; got {calc!r}")
        prefix = args["prefix"]
        outdir_rel = args.get("outdir", "out")
        ecutwfc = float(args["ecutwfc"])
        ecutrho = float(args["ecutrho"])

        # k-points: either a Monkhorst-Pack grid (kpts) or a high-symmetry
        # band path (kpath, → K_POINTS crystal_b). Exactly one is needed.
        kpath = args.get("kpath")
        kpts = args.get("kpts")
        if kpath is None and kpts is None:
            raise ValueError("provide either `kpts` (MP grid) or `kpath` (crystal_b path)")
        if kpts is not None:
            kpts = tuple(kpts)
            if len(kpts) != 3:
                raise ValueError(f"kpts must be a 3-element grid; got {kpts}")
        filename_rel = args.get("filename") or f"{prefix}.in"

        # Build the cell from (a,b,c,α,β,γ) if the caller passed lattice_params
        # instead of an explicit cell_angstrom (GAP 3 — no hand-computation).
        atoms_arg = dict(args["atoms"])
        if "cell_angstrom" not in atoms_arg and args.get("lattice_params"):
            atoms_arg["cell_angstrom"] = _cell_from_lattice_params(args["lattice_params"])
        atoms_obj = _build_ase_atoms(atoms_arg)
        pseudos = _validate_pseudos(args["pseudo_map"], ctx.workdir, ctx.pseudo_dir)

        # Resolve workdir-relative output paths
        outdir_abs = str(ctx.rel(outdir_rel))
        filename_abs = str(ctx.rel(filename_rel))

        # Optional knobs. A2: when the LLM omits `occupations`, default from the
        # config-advice is_metal flag — an insulator must use 'fixed' (a silent
        # 'smearing' default zeroes its band gap); metal/unknown keep mv-smearing.
        # An explicit args["occupations"] is always honored.
        default_occ = "smearing"
        if "occupations" not in args and read_is_metal(ctx.workdir) is False:
            default_occ = "fixed"
        kw: dict = {
            "occupations": args.get("occupations", default_occ),
            "smearing":    args.get("smearing",   "mv"),
            "degauss":     float(args.get("degauss", 0.02)),
            "nspin":       int(args.get("nspin",   1)),
        }
        if "starting_magnetization" in args:
            kw["starting_magnetization"] = args["starting_magnetization"]
        if "extra_control" in args:
            kw["extra_control"] = args["extra_control"]
        if "extra_system" in args:
            kw["extra_system"] = args["extra_system"]
        if "extra_electrons" in args:
            kw["extra_electrons"] = args["extra_electrons"]
        if kpath is not None:
            kw["kpath"] = kpath

        # ── Actual write ──────────────────────────────────────────────
        qe_tools.write_pw_input(
            atoms_obj,
            prefix=prefix,
            outdir=outdir_abs,
            pseudo_map=pseudos,
            calculation=calc,
            ecutwfc=ecutwfc,
            ecutrho=ecutrho,
            kpts=(kpts if kpts is not None else (8, 8, 8)),
            filename=filename_abs,
            **kw,
        )

        # ── qe_input_lint auto-tightens vc-relax thresholds ──────────
        lint = lint_pw_input(filename_abs, workdir=ctx.workdir)
        lint_diff_parts = [
            lint.diff_text if lint.patched else "",
            lint.advisory_text,
        ]
        lint_diff = "\n\n".join(p for p in lint_diff_parts if p) or None

        # ── Register calc_id ─────────────────────────────────────────
        calc_id = ctx.calc_registry.register(
            calc_type=calc,
            prefix=prefix,
            input_path=str(Path(filename_abs).relative_to(ctx.workdir)),
            params={
                "ecutwfc": ecutwfc, "ecutrho": ecutrho,
                "kpts": (list(kpts) if kpts is not None else None),
                "kpath": kpath,
                "occupations": kw["occupations"], "smearing": kw["smearing"],
                "degauss": kw["degauss"], "nspin": kw["nspin"],
            },
            pseudos={k: str(Path(v).relative_to(ctx.workdir)) if str(v).startswith(str(ctx.workdir)) else v
                     for k, v in pseudos.items()},
            lint_diff=lint_diff,
        )

        return {
            "content": [{"type": "text", "text":
                f"calc_id={calc_id} path={Path(filename_abs).relative_to(ctx.workdir)}"
                + (f"\nlint patches:\n{lint_diff}" if lint_diff else "")
            }],
            "is_error": False,
            "calc_id": calc_id,
            "path": str(Path(filename_abs).relative_to(ctx.workdir)),
            "lint_diff": lint_diff,
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"write_qe_input failed: {type(e).__name__}: {e}"}],
            "is_error": True,
        }
