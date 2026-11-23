#!/usr/bin/env python3
"""Build MANIFEST.json and canonical symlinks for GBRV USPP PBE v1.5.

规则：
- 每个元素在 raw/ 下的原始 UPF 文件保持不动
- 规范化命名通过符号链接暴露：`<Element>.upf` -> `raw/<element>_pbe_v*.uspp.F.UPF`
- 默认符号链接指向 GBRV 官方推荐版本（单版本元素直接取该版本）
- Hf 是唯一双版本元素：`Hf.upf` 指向标准 `hf_pbe_v1`；plus4 版本单独暴露为 `Hf_plus4.upf`
- MANIFEST.json 记录：每文件的元素 / 原始名 / 规范名 / minor_version / SHA256 / 大小

运行方式（从仓库根目录的 pseudos/GBRV_USPP_PBE_v1.5/ 目录）：
    python build_manifest.py
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"

# 元素符号周期表顺序 — 只列 GBRV 覆盖的 + La 以方便排序
ELEMENT_ORDER = [
    "H",                                                                                  "He",
    "Li","Be",                                                  "B","C","N","O","F","Ne",
    "Na","Mg",                                                  "Al","Si","P","S","Cl","Ar",
    "K","Ca","Sc","Ti","V","Cr","Mn","Fe","Co","Ni","Cu","Zn", "Ga","Ge","As","Se","Br","Kr",
    "Rb","Sr","Y","Zr","Nb","Mo","Tc","Ru","Rh","Pd","Ag","Cd","In","Sn","Sb","Te","I","Xe",
    "Cs","Ba","La",
    "Hf","Ta","W","Re","Os","Ir","Pt","Au","Hg","Tl","Pb","Bi",
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_upf_name(name: str) -> tuple[str, str, bool]:
    """Parse GBRV UPF filename.

    Returns (element_symbol_cap, minor_version, is_plus4).
    Example:
        'si_pbe_v1.uspp.F.UPF'           -> ('Si', '1', False)
        'cu_pbe_v1.2.uspp.F.UPF'         -> ('Cu', '1.2', False)
        'hf_pbe_plus4_v1.uspp.F.UPF'     -> ('Hf', '1', True)
    """
    m = re.match(r"^(?P<el>[a-z]+)_pbe(?:_(?P<flag>plus\d+))?_v(?P<ver>[\d.]+)\.uspp\.F\.UPF$", name)
    if not m:
        raise ValueError(f"unparseable UPF filename: {name}")
    el_low = m.group("el")
    minor = m.group("ver")
    is_plus4 = m.group("flag") == "plus4"
    element = el_low[0].upper() + el_low[1:]
    return element, minor, is_plus4


def canonical_name(element: str, is_plus4: bool) -> str:
    if is_plus4:
        return f"{element}_plus4.upf"
    return f"{element}.upf"


def make_symlink(canonical: str, target_rel: str) -> None:
    link_path = HERE / canonical
    if link_path.is_symlink() or link_path.exists():
        link_path.unlink()
    link_path.symlink_to(target_rel)


def main() -> None:
    assert RAW.is_dir(), f"raw/ directory missing at {RAW}"

    upf_files = sorted(RAW.glob("*.UPF"))
    records: list[dict] = []

    for upf in upf_files:
        element, minor, is_plus4 = parse_upf_name(upf.name)
        canonical = canonical_name(element, is_plus4)
        records.append({
            "element": element,
            "is_plus4": is_plus4,
            "original_name": upf.name,
            "canonical_name": canonical,
            "minor_version": minor,
            "size_bytes": upf.stat().st_size,
            "sha256": sha256_of(upf),
        })
        make_symlink(canonical, f"raw/{upf.name}")

    # 按周期表顺序排序（plus4 紧跟标准版后）
    order_index = {e: i for i, e in enumerate(ELEMENT_ORDER)}
    records.sort(key=lambda r: (order_index.get(r["element"], 999), r["is_plus4"]))

    covered_elements = sorted({r["element"] for r in records}, key=lambda e: order_index.get(e, 999))

    manifest = {
        "library": "GBRV Ultrasoft Pseudopotential Library",
        "version": "v1.5",
        "functional": "PBE",
        "relativistic": "scalar",
        "source_url": "https://www.physics.rutgers.edu/gbrv/all_pbe_UPF_v1.5.tar.gz",
        "source_website": "https://www.physics.rutgers.edu/gbrv/",
        "tarball_sha256": "4ebfeaa4c9d916c1ac2da6e89ec888fdffce096714f1956db01fea5ec9c9e4f0",
        "citation": (
            "K.F. Garrity, J.W. Bennett, K.M. Rabe, D. Vanderbilt, "
            "Comput. Mater. Sci. 81, 446 (2014). "
            "DOI: 10.1016/j.commatsci.2013.08.053"
        ),
        "license": "GNU Public License",
        "downloaded_at": "2026-04-23",
        "downloaded_by": "weilin",
        "total_files": len(records),
        "unique_elements": len(covered_elements),
        "covered_elements": covered_elements,
        "not_covered": [
            "He", "Ne", "Ar", "Kr", "Xe",                                     # 稀有气体
            "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er",  # 镧系（不含 La）
            "Tm", "Yb", "Lu",
            "Po", "At", "Rn",                                                 # 放射性
            "Fr", "Ra", "Ac",                                                 # 锕系
            # 锕系全部不含，此处只列前三作代表
        ],
        "schema_conventions": {
            "canonical_filename_pattern": "<ElementSymbol>.upf",
            "hf_dual_version": (
                "Hf has two versions in GBRV. Default symlink Hf.upf points to "
                "hf_pbe_v1.uspp.F.UPF (standard). The plus4 variant is exposed as "
                "Hf_plus4.upf -> hf_pbe_plus4_v1.uspp.F.UPF, for +4 oxidation "
                "states where the semi-core state treatment differs."
            ),
            "schema_json_usage": (
                'In bench question JSON, use '
                '"material.pseudopotential.files": {"Hf": "Hf.upf"} or '
                '{"Hf": "Hf_plus4.upf"} depending on the material.'
            ),
        },
        "known_gaps_for_first_batch": {
            "affected_questions": [
                "M2 Filled Skutterudite CeFe4Sb12 (Ce not covered)",
                "M2 Filled Skutterudite YbCo4Sb12 (Yb not covered)",
            ],
            "resolution_options": [
                "skip these two questions in first batch",
                "substitute with Co-only or Fe-only skutterudites",
                "use a different pseudopotential library (e.g. PseudoDojo FR) for lanthanides, breaking schema v1.2 family=GBRV convention",
            ],
        },
        "files": records,
    }

    manifest_path = HERE / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {manifest_path} — {len(records)} files, {len(covered_elements)} elements")
    print(f"symlinks created in {HERE}")


if __name__ == "__main__":
    main()
