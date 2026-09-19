from fastapi import APIRouter, HTTPException

from .. import service
from ..schemas import CreateExpeditionRequest

router = APIRouter(prefix="/api/expeditions", tags=["expedition"])


@router.post("")
def create_expedition(body: CreateExpeditionRequest):
    """创建跨章节远征。返回的视口与普通 run 同构（run_id 可直接续局/行动/回放）。"""
    try:
        return service.create_expedition(seed=body.seed, chapters=body.chapters)
    except service.InvalidAction as e:
        raise HTTPException(status_code=400, detail=str(e))
