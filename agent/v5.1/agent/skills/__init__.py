"""Material-family skill library.

Human-authored markdown playbooks matched against structural classification
facts at solver boot. See `loader.py` for API.
"""
from agent.skills.loader import (
    SkillMeta,
    load_all,
    match,
    select_and_render,
)

__all__ = ["SkillMeta", "load_all", "match", "select_and_render"]
