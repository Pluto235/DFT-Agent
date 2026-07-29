"""准备 solver 的 workdir：渲染 prompt + 软链赝势。"""
from pathlib import Path
from typing import Any


def prepare_workdir(
    *,
    run_dir: Path,
    bench_root: Path,
    task: dict[str, Any],
    rendered_prompt: str,
) -> Path:
    """在 run_dir 下创建 workdir 子目录，布置:
      - task_prompt.txt (rendered)
      - pseudos/ 软链到 bench_root/pseudos/<family>/*.upf
    返回 workdir 路径。"""
    run_dir = Path(run_dir)
    workdir = run_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. task_prompt.txt
    (workdir / "task_prompt.txt").write_text(rendered_prompt, encoding="utf-8")

    # 2. pseudos/ symlink
    pp = task["material"]["pseudopotential"]
    family = pp["family"]
    src_pseudos_dir = bench_root / "pseudos" / family
    if not src_pseudos_dir.exists():
        raise FileNotFoundError(
            f"pseudo family dir not found: {src_pseudos_dir} — "
            f"run `bash pseudos/fetch.sh` in bench repo first"
        )
    dst_pseudos = workdir / "pseudos"
    if dst_pseudos.is_symlink() or dst_pseudos.exists():
        dst_pseudos.unlink()
    dst_pseudos.symlink_to(src_pseudos_dir.resolve(), target_is_directory=True)

    return workdir
