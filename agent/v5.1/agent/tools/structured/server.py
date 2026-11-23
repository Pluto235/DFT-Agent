"""build_qe_mcp_server — assemble the 5 structured tools into an
in-process SDK MCP server.

Each tool delegates to its `do_*` async function in this package, which
takes a single `args: dict` plus an injected `ctx: ToolContext`. The
@tool decorator only knows about `args`, so ctx is bound via closure.

The returned server config can be passed to ClaudeAgentOptions:

    options = ClaudeAgentOptions(
        mcp_servers={"dft_qe": build_qe_mcp_server(ctx)},
        allowed_tools=["mcp__dft_qe__write_qe_input",
                       "mcp__dft_qe__run_pw",
                       "mcp__dft_qe__read_qe_output",
                       "mcp__dft_qe__parse_structure",
                       "mcp__dft_qe__verify_run",
                       "Bash"],
    )
"""

from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent.tools.structured.bash_tool import do_bash
from agent.tools.structured.context import ToolContext
from agent.tools.structured.parse_structure import do_parse_structure
from agent.tools.structured.read_qe_output import do_read_qe_output
from agent.tools.structured.run_pw import do_run_pw
from agent.tools.structured.submit import do_submit
from agent.tools.structured.verify_run import do_verify_run
from agent.tools.structured.write_qe_input import do_write_qe_input


# Tool names — kept here as a single source of truth for allowed_tools wiring.
TOOL_NAMES: list[str] = [
    "write_qe_input", "run_pw", "read_qe_output",
    "parse_structure", "verify_run", "bash", "submit",
]


def build_qe_mcp_server(ctx: ToolContext):
    """Build the in-process MCP server. All 5 tools share `ctx`."""

    @tool(
        "write_qe_input",
        "Generate a Quantum ESPRESSO input file (.in) for one of "
        "{scf, nscf, bands, relax, vc-relax}. Returns calc_id + path. "
        "vc-relax thresholds are auto-tightened to bench defaults "
        "(press_conv_thr=0.05, forc_conv_thr=1e-4, etot_conv_thr=1e-5) "
        "to avoid losing the spacegroup audit (0.25 of T1 score).",
        {
            "calc": str, "prefix": str, "outdir": str,
            "atoms": dict,         # {symbols, positions_frac, cell_angstrom}
            "lattice_params": dict,  # {a,b,c,alpha,beta,gamma} — cell built via pymatgen (alt to atoms.cell_angstrom)
            "pseudo_map": dict,    # {symbol: upf_path}
            "ecutwfc": float, "ecutrho": float,
            "kpts": list,          # [kx,ky,kz] MP grid  (use kpts OR kpath)
            "kpath": list,         # [[kx,ky,kz,npts],...] crystal_b band path for nscf/bands
            "occupations": str, "smearing": str, "degauss": float,
            "nspin": int, "starting_magnetization": dict,
            "extra_control": dict, "extra_system": dict, "extra_electrons": dict,
            "filename": str,
        },
    )
    async def _write_qe_input(args):
        return await do_write_qe_input(args, ctx=ctx)

    @tool(
        "run_pw",
        "Execute pw.x on a registered calc_id. Returns exit_code, wall_s, "
        "and a short tail summary (~80 lines). DO NOT cat the full output — "
        "use read_qe_output for windowed views.",
        {"calc_id": str, "timeout_s": int, "np": int},
    )
    async def _run_pw(args):
        return await do_run_pw(args, ctx=ctx)

    @tool(
        "read_qe_output",
        "Windowed view onto a calc_id's pw.x output. window ∈ "
        "{tail, errors, forces, stress, final_cell, scf_steps, bands_table, "
        "summary, grep:PATTERN}. Default returns last 200 lines.",
        {"calc_id": str, "window": str, "max_lines": int},
    )
    async def _read_qe_output(args):
        return await do_read_qe_output(args, ctx=ctx)

    @tool(
        "parse_structure",
        "Extract relaxed structure + symmetry from a vc-relax/relax calc. "
        "Returns lattice constants, angles, space group number, point group, "
        "crystal system. THIS is what the bench audit uses to score T1's "
        "space_group_number field — run this before submit.",
        {"calc_id": str, "symprec": float},
    )
    async def _parse_structure(args):
        return await do_parse_structure(args, ctx=ctx)

    @tool(
        "verify_run",
        "Program-level physical sanity check for a calc_id. "
        "expected_type ∈ {vc_relax, bands_dos_full}. Returns "
        "verdict ∈ {pass, soft_fail, hard_fail} with itemized checks "
        "(SCF converged, forces/stress under threshold, JOB DONE, "
        "spacegroup preserved, lattice drift, etc.). solve.py runs this "
        "as a mandatory gate before final submit.",
        {"calc_id": str, "expected_type": str,
         "initial": dict, "paths": dict, "symprec": float},
    )
    async def _verify_run(args):
        return await do_verify_run(args, ctx=ctx)

    @tool(
        "bash",
        "Escape-hatch shell. Use for bands.x / dos.x / pp.x post-processing "
        "and ad-hoc tasks not covered by the structured tools. Every command "
        "is logged to <workdir>/bash.log (bench audit requirement). "
        "Backgrounded commands (&, nohup) are rejected. stdout/stderr "
        "truncated to 8K/4K chars.",
        {"command": str, "timeout_s": int},
    )
    async def _bash(args):
        return await do_bash(args, ctx=ctx)

    @tool(
        "submit",
        "Finalize the task. Pass task_outputs={key: workdir-relative path} "
        "matching the keys listed in '=== Submission Instructions ==='. "
        "Set incomplete=True to give up gracefully. Driver will run the "
        "program-level verifier on every path before writing result.json.",
        {"task_outputs": dict, "notes": str, "incomplete": bool},
    )
    async def _submit(args):
        return await do_submit(args, ctx=ctx)

    return create_sdk_mcp_server(
        name="dft_qe",
        version="0.5.0",
        tools=[_write_qe_input, _run_pw, _read_qe_output,
               _parse_structure, _verify_run, _bash, _submit],
    )


def allowed_tools_for_server(server_name: str = "dft_qe") -> list[str]:
    """Return the allowed_tools list for ClaudeAgentOptions."""
    return [f"mcp__{server_name}__{n}" for n in TOOL_NAMES]
