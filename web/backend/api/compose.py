from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from backend.schemas.material import (
    EnhanceRequest,
    EnhanceResponse,
    MaterialParseResponse,
)
from backend.services.enhance_service import enhance_prompt as do_enhance
from backend.services.upload_parser import parse_upload


router = APIRouter(prefix="/api", tags=["compose"])


@router.post("/compose:enhance", response_model=EnhanceResponse)
async def enhance(req: EnhanceRequest, request: Request) -> EnhanceResponse:
    s = request.app.state.settings
    api_key = s.enhance_api_key or s.anthropic_api_key
    if not api_key:
        raise HTTPException(status_code=502, detail="enhance API key not configured")
    try:
        return await do_enhance(req, model=s.enhance_model, api_key=api_key, timeout_s=30.0)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"enhance failed: {e}")


@router.post("/compose:upload", response_model=MaterialParseResponse)
async def upload_material(file: UploadFile = File(...)) -> MaterialParseResponse:
    return await parse_upload(file)
