"""Admin endpoints — cache invalidation + audit-trail read."""

from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.auth import Principal, require_admin
from app.db import session_scope
from app.models import MCPServerHistory
from app.services import cache

router = APIRouter()


@router.post("/invalidate/{application_id}")
def invalidate(
    application_id: UUID,
    _p: Principal = Depends(require_admin),
) -> dict:
    """Force cache-bust for one application. Next lookup rehydrates from PG."""
    cache.invalidate(application_id)
    return {"ok": True, "application_id": str(application_id)}


@router.get("/history/{application_id}", response_model=List[dict])
def history(
    application_id: UUID,
    _p: Principal = Depends(require_admin),
) -> List[dict]:
    """Audit trail (INSERT/UPDATE/DELETE) for one application, newest first."""
    with session_scope() as db:
        stmt = (
            select(MCPServerHistory)
            .where(MCPServerHistory.application_id == application_id)
            .order_by(MCPServerHistory.changed_at.desc())
            .limit(200)
        )
        return [
            {
                "id": row.id,
                "op": row.op,
                "before": row.before,
                "after": row.after,
                "changed_by": row.changed_by,
                "changed_at": row.changed_at.isoformat(),
            }
            for row in db.scalars(stmt).all()
        ]
