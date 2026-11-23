from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from backend.auth.capabilities import VIEW_DEV_AGENTS
from backend.auth.tokens import User
from backend.schemas.config import ConfigResponse, ConfigUpdateRequest
from backend.services.config_service import get_config, update_config


router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("", response_model=ConfigResponse)
async def read_config(request: Request) -> ConfigResponse:
    user: User = request.state.user
    return await get_config(
        request.app.state.settings, request.app.state.db,
        can_see_dev_agents=user.has_capability(VIEW_DEV_AGENTS),
    )


@router.put("", response_model=ConfigResponse)
async def write_config(req: ConfigUpdateRequest, request: Request) -> ConfigResponse:
    user: User = request.state.user
    try:
        return await update_config(
            request.app.state.settings, request.app.state.db,
            max_concurrent_runs=req.max_concurrent_runs,
            cleanup_intermediates_on_finish=req.cleanup_intermediates_on_finish,
            is_dev_role=user.is_dev_role,
            can_see_dev_agents=user.has_capability(VIEW_DEV_AGENTS),
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
