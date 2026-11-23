"""multipart UploadFile → pymatgen Structure → MaterialParseResponse。

space_group 取 int 编号 1-230（与 prototype + 宪法 2 对齐；audit round 4 P0-3）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import HTTPException, UploadFile

from backend.schemas.material import MaterialParseResponse


_MAX_BYTES = 1 * 1024 * 1024
_ALLOWED_SUFFIX = {".cif", ".poscar", ".vasp"}


async def parse_upload(file: UploadFile) -> MaterialParseResponse:
    raw = await file.read()
    if len(raw) > _MAX_BYTES:
        raise HTTPException(413, f"file > 1 MB (got {len(raw)} bytes)")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_SUFFIX:
        raise HTTPException(415, f"unsupported format: {suffix or '<missing>'}")
    text = raw.decode("utf-8", errors="replace")
    fmt = "cif" if suffix == ".cif" else "poscar"
    try:
        from pymatgen.core import Structure
        struct = Structure.from_str(text, fmt="cif" if fmt == "cif" else "poscar")
    except Exception as e:
        raise HTTPException(422, f"parse failed: {e}")
    sg_symbol, sg_number = struct.get_space_group_info()
    from pymatgen.symmetry.groups import SpaceGroup
    crystal_system = SpaceGroup.from_int_number(sg_number).crystal_system
    return MaterialParseResponse(
        formula=str(struct.composition.reduced_formula),
        lattice_system=crystal_system,
        space_group=int(sg_number),
        atoms_count=len(struct),
        raw_struct_hash=hashlib.sha256(raw).hexdigest()[:16],
        struct_data=text,
        struct_format=fmt,
    )
