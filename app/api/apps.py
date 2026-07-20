"""CRUD for MCP server registrations.

The router is a pure service-discovery / KV registry — it stores the
(application_id → {endpoint_url, auth_type, auth_ref, capabilities}) map
and nothing more. Consumers GET a registration, resolve the target's
auth_ref from their OWN KV secret mount, and make the MCP tool call
themselves via the mcp SDK. The router does NOT proxy tool calls; there
is no runtime coupling between it and target MCP servers.

That simplification is deliberate — see docs/contracts.md §1 for the
consumer contract and the tradeoff (every consumer needs the mcp SDK
and each target-server bearer mounted, but the router itself is a
2-endpoint CRUD service).
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.auth import Principal, get_current_principal, require_admin
from app.db import session_scope
from app.models import MCPServer
from app.schemas.mcp_server import (
    MCPServerCreate,
    MCPServerResponse,
    MCPServerUpdate,
)
from app.services import cache
from app.services.audit import audit

router = APIRouter()
logger = logging.getLogger(__name__)


def _to_response(server: MCPServer) -> MCPServerResponse:
    return MCPServerResponse(
        application_id=server.application_id,
        name=server.name,
        transport=server.transport,
        endpoint_url=server.endpoint_url,
        auth_type=server.auth_type,
        auth_ref=server.auth_ref,
        capabilities=server.capabilities_snapshot,
        metadata=server.server_metadata,
        status=server.status,
        owner_email=server.owner_email,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


def _to_cache(server: MCPServer) -> dict:
    return _to_response(server).model_dump(by_alias=True, mode="json")


@router.get("", response_model=List[MCPServerResponse])
def list_apps(
    principal: Principal = Depends(get_current_principal),
    status_filter: Optional[str] = Query(default=None, alias="status"),
) -> List[MCPServerResponse]:
    """List all registrations. `status=active` most common."""
    with session_scope() as db:
        stmt = select(MCPServer).order_by(MCPServer.name)
        if status_filter:
            stmt = stmt.where(MCPServer.status == status_filter)
        elif not principal.is_admin:
            # Non-admin default: hide non-active. ADMIN sees everything.
            stmt = stmt.where(MCPServer.status == "active")
        return [_to_response(row) for row in db.scalars(stmt).all()]


@router.get("/{application_id}", response_model=MCPServerResponse)
def get_app(
    application_id: UUID,
    principal: Principal = Depends(get_current_principal),
) -> MCPServerResponse:
    """Cache-aside lookup. 404 for unknown OR non-active-to-non-admin.

    This is THE hot path — every Resolve-with-AI turn on every consumer
    hits this endpoint. Kept cache-first to make it a single Redis read
    on the happy path.
    """
    cached = cache.get_cached(application_id)
    if cached is not None:
        if cached.get("status") == "active" or principal.is_admin:
            return MCPServerResponse.model_validate(cached)

    with session_scope() as db:
        server = db.get(MCPServer, application_id)
        if server is None:
            raise HTTPException(status_code=404, detail="No MCP server registered for this application")
        if server.status != "active" and not principal.is_admin:
            raise HTTPException(status_code=404, detail="No MCP server registered for this application")
        cache.set_cached(application_id, _to_cache(server))
        return _to_response(server)


@router.post("", response_model=MCPServerResponse, status_code=status.HTTP_201_CREATED)
def create_app(
    body: MCPServerCreate,
    principal: Principal = Depends(require_admin),
) -> MCPServerResponse:
    """Register an MCP server. Pure KV store — no discovery call.

    The optional ``capabilities`` field on the request body carries the
    caller-supplied capability declaration (severities the server handles,
    declared actions, etc.). Consumers may use it as a hint or ignore it
    and query the target's ``aims_discover_capabilities`` tool themselves
    at consumption time. The router NEVER interviews the target.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        existing = db.get(MCPServer, body.application_id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"An MCP server is already registered for application {body.application_id}",
            )
        server = MCPServer(
            application_id=body.application_id,
            name=body.name,
            transport=body.transport,
            endpoint_url=body.endpoint_url,
            auth_type=body.auth_type,
            auth_ref=body.auth_ref,
            capabilities_snapshot=body.capabilities or {},
            server_metadata=body.server_metadata,
            status=body.status,
            owner_email=body.owner_email,
            created_at=now,
            updated_at=now,
            updated_by=principal.sub,
        )
        db.add(server)
        audit(db, body.application_id, "insert", None, server, principal.sub)
        db.flush()
        cache.set_cached(body.application_id, _to_cache(server))
        logger.info(
            "mcp_server registered app_id=%s name=%s endpoint=%s by=%s",
            body.application_id,
            body.name,
            body.endpoint_url,
            principal.sub,
        )
        return _to_response(server)


@router.patch("/{application_id}", response_model=MCPServerResponse)
def patch_app(
    application_id: UUID,
    body: MCPServerUpdate,
    principal: Principal = Depends(require_admin),
) -> MCPServerResponse:
    """Partial update. Pure KV store — no re-discovery call.

    If the caller wants to refresh the capabilities snapshot, they include
    it in the PATCH body (usually after re-interviewing the target
    themselves).
    """
    with session_scope() as db:
        server = db.get(MCPServer, application_id)
        if server is None:
            raise HTTPException(status_code=404, detail="Not found")

        before = MCPServer(**{c.name: getattr(server, c.name) for c in MCPServer.__table__.columns})

        changed = body.model_dump(exclude_unset=True, by_alias=True)
        # Alias-mapped fields need to write to the ORM column name.
        if "metadata" in changed:
            server.server_metadata = changed.pop("metadata")
        if "capabilities" in changed:
            server.capabilities_snapshot = changed.pop("capabilities")
        for k, v in changed.items():
            setattr(server, k, v)

        server.updated_at = datetime.now(timezone.utc)
        server.updated_by = principal.sub
        audit(db, application_id, "update", before, server, principal.sub)
        cache.invalidate(application_id)
        return _to_response(server)


@router.delete("/{application_id}", status_code=204)
def delete_app(
    application_id: UUID,
    principal: Principal = Depends(require_admin),
) -> None:
    """Soft-delete → status='deprecated'. Row stays for audit trail."""
    with session_scope() as db:
        server = db.get(MCPServer, application_id)
        if server is None:
            raise HTTPException(status_code=404, detail="Not found")
        before = MCPServer(**{c.name: getattr(server, c.name) for c in MCPServer.__table__.columns})
        server.status = "deprecated"
        server.updated_at = datetime.now(timezone.utc)
        server.updated_by = principal.sub
        audit(db, application_id, "delete", before, server, principal.sub)
        cache.invalidate(application_id)
