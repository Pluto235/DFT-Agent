"""WorkdirStager — v4.3 (post-fix): per-step subdir setup + post-execution verify.

After the executor cwd fix (it now runs from MAIN workdir, not the step
subdir), Stager's responsibilities shrink:

1. **stage(step, plan)** — ensure `workdir/<step_id>/` exists. Collect a
   summary of what upstream-step artifacts are available (their planner-
   declared paths, all relative to main workdir) so the executor's prompt
   can list them. No symlinks are created — the executor sees deps at
   their natural workdir-relative paths because everything shares cwd.
2. **promote(step)** — verifier passed; verify every expected_output exists
   at its workdir-relative path. Returns `{key: workdir_relative_path}`,
   the orchestrator picks the keys that match required_keys for the final
   result.json. No file movement: the planner-declared paths are already
   what bench audit will see.

The executor is told (in its prompt) to keep ALL files for step <id> under
`<id>/` subdir prefix. If it stays disciplined, expected_outputs are at
exactly the declared paths. If not, the verifier will fail and replan.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from agent.ledger import PlanStep


@dataclass
class StagedStep:
    """stage() return: subdir + summary of upstream artifacts for prompt rendering."""
    step_id: str
    sub_workdir: Path                       # absolute path: workdir/<step_id>
    upstream_paths: dict[str, list[str]]    # dep_id → [workdir-relative paths]


class WorkdirStager:
    """Per-step subdir setup; post-execution verify. Shared cwd model."""

    def __init__(self, workdir: Path):
        self.workdir = workdir.resolve()
        if not self.workdir.is_dir():
            raise ValueError(f"workdir does not exist: {self.workdir}")

    # ─────────────────────── stage ──────────────────────────────────────────

    def stage(self, step: PlanStep, all_steps: dict[str, PlanStep]) -> StagedStep:
        """Create `workdir/<step_id>/` and collect upstream-artifact summary.

        Raises if an upstream's expected_output doesn't yet exist on disk
        (meaning the dep step never completed) — orchestrator should not
        be calling stage() for a step whose deps aren't done.
        """
        sub = self.workdir / step.id
        sub.mkdir(parents=True, exist_ok=True)

        upstream: dict[str, list[str]] = {}
        for dep_id in step.deps:
            dep = all_steps.get(dep_id)
            if dep is None:
                raise RuntimeError(
                    f"stage({step.id}): dep {dep_id!r} not in plan"
                )
            paths_for_dep: list[str] = []
            for _key, rel_path in dep.expected_outputs.items():
                src = (self.workdir / rel_path).resolve()
                if not src.exists():
                    raise RuntimeError(
                        f"stage({step.id}): upstream {dep_id} artifact missing: "
                        f"{rel_path} (resolved {src})"
                    )
                paths_for_dep.append(rel_path)
            upstream[dep_id] = paths_for_dep

        return StagedStep(
            step_id=step.id,
            sub_workdir=sub,
            upstream_paths=upstream,
        )

    # ─────────────────────── verify (formerly promote) ──────────────────────

    def promote(self, step: PlanStep) -> dict[str, str]:
        """Verify expected_outputs exist at their declared workdir-relative
        paths. No file movement — the executor wrote them in place.

        Returns {key: workdir-relative path} for the orchestrator to use
        in the final result.json. Raises RuntimeError on missing file.
        """
        verified: dict[str, str] = {}
        for key, rel_path in step.expected_outputs.items():
            full = (self.workdir / rel_path).resolve()
            try:
                full.relative_to(self.workdir)
            except ValueError as e:
                raise RuntimeError(
                    f"promote({step.id}): expected_outputs[{key!r}]={rel_path!r} "
                    f"escapes workdir"
                ) from e
            if not full.exists():
                raise RuntimeError(
                    f"promote({step.id}): expected_outputs[{key!r}] not produced "
                    f"by executor at workdir-relative path {rel_path!r} "
                    f"(absolute: {full})"
                )
            verified[key] = rel_path
        return verified


# ─────────────────────── helpers ──────────────────────────────────────────

def list_stale_steps(workdir: Path, current_step_ids: Iterable[str]) -> list[str]:
    """Return existing step subdirs that are NOT in the current plan.

    Used by orchestrator on replan: stale subdirs can be left in place for
    postmortem. This function only enumerates.
    """
    wanted = set(current_step_ids)
    return [
        p.name for p in workdir.iterdir()
        if p.is_dir() and p.name not in wanted and not p.name.startswith(".")
        and p.name not in {"out", "pseudo", "pseudos", "tmp"}
    ]
