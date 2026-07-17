"""Write shadow rows into mcp_server_history — one call site, keep it there."""

import logging
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import MCPServer, MCPServerHistory

logger = logging.getLogger(__name__)


def _snapshot(server: Optional[MCPServer]) -> Optional[Dict[str, Any]]:
    if server is None:
        return None
    return {
        "application_id": str(server.application_id),
        "name": server.name,
        "transport": server.transport,
        "endpoint_url": server.endpoint_url,
        "auth_type": server.auth_type,
        "auth_ref": server.auth_ref,
        "capabilities_snapshot": server.capabilities_snapshot,
        "metadata": server.server_metadata,
        "status": server.status,
        "owner_email": server.owner_email,
        "updated_by": server.updated_by,
    }


def audit(
    db: Session,
    application_id: UUID,
    op: str,
    before: Optional[MCPServer],
    after: Optional[MCPServer],
    changed_by: Optional[str],
) -> None:
    row = MCPServerHistory(
        application_id=application_id,
        op=op,
        before=_snapshot(before),
        after=_snapshot(after),
        changed_by=changed_by,
    )
    db.add(row)
