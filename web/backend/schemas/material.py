"""Material / Compose schemas — 上传解析、prompt 增强、custom mode material spec。

⚠️ source enum 用 prototype compose.jsx 行 148 的取值 `upload | describe | pick`（不是老 v2 contract 的 "text"）；
P0 audit round 1 已修订（宪法 2：后端适配前端）。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .common import MaterialSourceEnum, StructFormatEnum


class MaterialSpec(BaseModel):
    """custom mode material spec — 三种 source 至少填一种对应字段。"""
    source: MaterialSourceEnum
    formula_text: Optional[str] = None              # source=describe
    bench_task_id: Optional[str] = None             # source=pick
    parsed_summary: Optional[dict] = None           # source=upload；MaterialParseResponse 摘要
    struct_data: Optional[str] = None               # source=upload；CIF/POSCAR 原文
    struct_format: Optional[StructFormatEnum] = None


class EnhanceRequest(BaseModel):
    user_description: str = Field(min_length=20)
    task_type: str                                  # 必须 ∈ ConfigResponse.task_types[].id
    material_hint: Optional[str] = None


class EnhanceResponse(BaseModel):
    enhanced_prompt: str
    diff_summary: str
    model: str                                      # echo enhance_model
    usage: Optional[dict] = None                    # {input_tokens, output_tokens}


class MaterialParseResponse(BaseModel):
    """upload 解析返回。space_group=int 1-230（与 prototype compose.jsx setUploadParse 对齐；
    宪法 2 — 后端适配前端，不暴露 symbol 字符串）。"""
    formula: str
    lattice_system: str                             # cubic / hexagonal / ...
    space_group: int = Field(ge=1, le=230)
    atoms_count: int                                # primitive cell 原子数
    raw_struct_hash: str                            # sha256 前 16 字
    struct_data: str                                # 原文回传供 inline-pass
    struct_format: StructFormatEnum
