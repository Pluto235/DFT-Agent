"""Read bench task JSON + render prompt (模板 + slot + submit 指引)."""
import json
from pathlib import Path
from typing import Any


# T1/T2/T3/T4 所需 submit key，列在渲染 prompt 的尾部告诉 agent。
# 所有 value 必须是文件路径 (str)，audit 入口会过滤掉非 str value；标量结果
# 不进 task_outputs，harness 从这些 CSV/log 自行解析重算 (T3 BM3 重 fit / T4
# lstsq 重 fit)，agent 自报标量仅供 transcript / notes, 不参与打分。
SUBMIT_INSTRUCTIONS: dict[str, dict[str, str]] = {
    "vc_relax": {
        "vc_relax_out": "path to your pw.x vc-relax output (with final CELL_PARAMETERS and ATOMIC_POSITIONS)",
    },
    "bands_dos": {
        "scf_out":    "path to the scf pw.x output",
        "bands_out":  "path to the nscf-bands pw.x output",
        "bands_data": "path to the bands.dat produced by bands.x (filband)",
        "dos_dat":    "path to the dos.dat produced by dos.x",
        "nscf_dos_out": "path to the DOS-NSCF pw.x output (.out containing 'the Fermi energy is X.XXXX ev')",
    },
    "eos": {
        "eos_points_csv": "path to a CSV (header `ratio,volume_ang3,energy_ry,pressure_kbar`, 7 rows) summarizing all 7 volume points; do NOT put V0/B0/B0' scalars in task_outputs (paths only)",
    },
    "elastic_stiffness": {
        "stress_strain_csv": "path to a CSV (header `mode,amplitude,sigma_1_gpa..sigma_6_gpa`, 24 rows; `mode` ∈ {e1..e6}, `amplitude` is signed strain like -0.01) summarizing all 24 strain points; do NOT put Cij/K_VRH/G_VRH scalars in task_outputs (paths only)",
    },
}


def get_network_allowed(task: dict[str, Any]) -> bool:
    """读 task.policy.network_allowed，兜底 false。

    v1.3 schema 把"禁网"从 harness 硬编码升级为 bench JSON 字段。本 helper
    支持两种 schema：
      - v1.3+：从 task["task"]["policy"]["network_allowed"] 读
      - v1.2 及更老：字段缺失，兜底返回 False（与 MVP 硬编码语义一致）
    """
    return bool(task.get("task", {}).get("policy", {}).get("network_allowed", False))


def load_task(bench_root: Path, task_id: str) -> dict[str, Any]:
    """按 task_id 定位 questions/<Mi_Tj>/<id>.json，返回 parsed dict。"""
    # task_id 形如 M1_T1_Si_vc_relax → 子目录 M1_T1
    parts = task_id.split("_", 2)
    if len(parts) < 3 or not parts[0].startswith("M") or not parts[1].startswith("T"):
        raise ValueError(f"invalid task_id format: {task_id!r}")
    subdir = f"{parts[0]}_{parts[1]}"
    path = bench_root / "questions" / subdir / f"{task_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"task JSON not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def render_prompt(bench_root: Path, task: dict[str, Any]) -> str:
    """渲染 task_prompt.txt 内容。

    包含：模板正文 → slot 替换 → 末尾追加 submit 指引。
    submit 指引是 harness 的"运行时注入"，不改 bench schema 里的模板文件。
    """
    task_type = task["task"]["type"]
    template_path = bench_root / "schema" / "prompts" / f"{task_type}.txt"
    template = template_path.read_text(encoding="utf-8")

    material = task["material"]
    slots: dict[str, str] = {
        "{formula}": material["formula"],
        "{structure_type}": material.get("structure", ""),
        "{space_group}": material.get("space_group", ""),
        "{atoms_per_primitive_cell}": str(material.get("atoms_per_primitive_cell", "")),
        "{initial_structure_block}": _render_initial_structure(material["initial_structure"]),
        "{pseudopotential_block}": _render_pseudopotential_block(material["pseudopotential"]),
        "{advisory_notes_block}": _render_advisory_notes(material.get("advisory_notes")),
    }

    rendered = template
    for slot, value in slots.items():
        rendered = rendered.replace(slot, value)

    submit_section = _render_submit_instructions(task_type)
    sections = [rendered.rstrip(), submit_section]
    if not get_network_allowed(task):
        sections.append(_render_network_policy_section())
    return "\n\n".join(sections) + "\n"


def derive_initial_files(bench_root: Path, task: dict[str, Any]) -> list[dict[str, Any]]:
    """派生 agent 起手 ls workdir/ 能看到的文件清单。

    精确反映 prepare_workdir 的产物（harness/lib/workdir.py）：
      - task_prompt.txt（render_prompt 派生）
      - pseudos/<filename>（每个 material.pseudopotential.files 项）

    返回 list[dict] 每项含 name / size / description；size 为字节数（pseudo 文件
    not exist 时为 None，rendered prompt 始终能算 size）。
    """
    rendered = render_prompt(bench_root, task)
    files: list[dict[str, Any]] = [
        {
            "name": "task_prompt.txt",
            "size": len(rendered.encode("utf-8")),
            "description": "Rendered task prompt for the agent",
        }
    ]
    pp = task["material"]["pseudopotential"]
    pp_dir = bench_root / "pseudos" / pp["family"]
    for elem, fname in pp["files"].items():
        fpath = pp_dir / fname
        try:
            size = fpath.stat().st_size
        except OSError:
            size = None
        files.append(
            {
                "name": f"pseudos/{fname}",
                "size": size,
                "description": f"{elem} pseudopotential ({pp['family']})",
            }
        )
    return files


def _render_initial_structure(initial: dict[str, Any]) -> str:
    """把 lattice_constants + lattice_angles + atomic_positions_crystal 渲染成文本块。
    给 agent 的是 human-readable 描述，由 agent 翻译成实际 QE 输入。

    lattice_constants_angstrom / lattice_angles_degree 兼容两种 shape：
      - dict {a,b,c} / {alpha,beta,gamma}（M1/M2 原始格式）
      - list [a,b,c] / [alpha,beta,gamma]（M3 自跑 GT 输出格式，worker-0 6ed34b2）
    """
    lc = initial["lattice_constants_angstrom"]
    la = initial["lattice_angles_degree"]
    if isinstance(lc, list):
        a, b, c = lc[0], lc[1], lc[2]
    else:
        a, b, c = lc["a"], lc["b"], lc["c"]
    if isinstance(la, list):
        alpha, beta, gamma = la[0], la[1], la[2]
    else:
        alpha, beta, gamma = la["alpha"], la["beta"], la["gamma"]
    lines = [
        "Initial structure (primitive cell, lattice constants in angstrom, angles in degree):",
        f"  a = {a}, b = {b}, c = {c}",
        f"  alpha = {alpha}, beta = {beta}, gamma = {gamma}",
        "",
        "ATOMIC_POSITIONS (crystal, i.e. fractional coordinates of the primitive cell):",
    ]
    for site in initial["atomic_positions_crystal"]:
        el = site["element"]
        x, y, z = site["position"]
        lines.append(f"  {el}  {x:.6f}  {y:.6f}  {z:.6f}")
    if initial.get("note"):
        lines.append("")
        lines.append(f"Note: {initial['note']}")
    return "\n".join(lines)


def _render_pseudopotential_block(pp: dict[str, Any]) -> str:
    lines = [
        f"Pseudopotentials: {pp['family']} ({pp.get('relativistic', 'scalar')}-relativistic).",
        "UPF files available in the symlinked ./pseudos/ directory:",
    ]
    for el, fname in pp["files"].items():
        lines.append(f"  {el}: {fname}")
    return "\n".join(lines)


def _render_advisory_notes(notes: Any) -> str:
    if not notes:
        return ""
    if isinstance(notes, list):
        return "\n".join([f"Note: {n}" for n in notes])
    return f"Note: {notes}"


def _render_network_policy_section() -> str:
    """注入禁网说明（仅当 task.policy.network_allowed=false 时调用）。

    harness 内置文本，不改 bench prompts 模板——保持模板与 policy 解耦。
    """
    return (
        "=== Network Policy ===\n"
        "This task forbids network access. Do NOT use any network commands "
        "(curl/wget/ssh/scp/rsync, pip install, conda install, apt install, "
        "git clone, or any HTTP(S) downloads). Solve using only pre-installed "
        "tools and the data already provided in the workdir."
    )


def _render_submit_instructions(task_type: str) -> str:
    keys = SUBMIT_INSTRUCTIONS.get(task_type)
    if keys is None:
        return ""
    lines = [
        "=== Submission Instructions ===",
        "When you are done, call submit with task_outputs containing these keys",
        "(values are workdir-relative paths to YOUR output files):",
    ]
    for k, desc in keys.items():
        lines.append(f'  "{k}": {desc}')
    lines.append("")
    lines.append("Example:")
    example = {k: f"runs/<your_path>/{k}.out" for k in keys}
    example_json = json.dumps({"task_outputs": example, "notes": "..."}, indent=2)
    lines.append(example_json)
    return "\n".join(lines)
