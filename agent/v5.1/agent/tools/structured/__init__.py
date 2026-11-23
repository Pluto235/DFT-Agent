"""v5 structured tools — in-process SDK MCP server.

server.py          — create_sdk_mcp_server registering 5 tools below
calc_registry.py   — VASPilot-style calc_id store (input_path, output_path, status...)
write_qe_input.py  — wraps qe_tools.write_pw_input + qe_input_lint
run_pw.py          — wraps BashTool to invoke pw.x; preserves bash.log
read_qe_output.py  — windowed view (tail/forces/stress/final_cell/bands_table)
parse_structure.py — pymatgen SpacegroupAnalyzer + lattice extraction
verify_run.py      — program-level physical sanity check (T1/T2)
"""
