"""CRUD for MCP server registrations."""

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
    ProposeRequest,
    ProposeResponse,
)
from app.services import cache
from app.services.audit import audit
from app.services.discovery import DiscoveryError, call_discover
from app.services.propose import CapabilityMissError, ProposeError, forward_propose

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
    """Cache-aside lookup. 404 for unknown OR non-active-to-non-admin."""
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
    """Register an MCP server. Calls discovery synchronously — a server that
    can't answer discovery is rejected here, not silently persisted."""

    # Interview the server BEFORE we persist anything.
    try:
        discovery = call_discover(body.endpoint_url, body.auth_type, body.auth_ref)
    except DiscoveryError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"MCP discovery failed: {exc}",
        )

    if not discovery.read_only_default and not body.i_accept_write_capable_server:
        raise HTTPException(
            status_code=400,
            detail=(
                "Server declares read_only_default=false. Set "
                "'i_accept_write_capable_server': true in the request body if "
                "you intend to register a write-capable server."
            ),
        )

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
            capabilities_snapshot=discovery.model_dump(),
            server_metadata=body.server_metadata,
            status=body.status,
            owner_email=body.owner_email,
            created_at=now,
            updated_at=now,
            updated_by=principal.sub,
        )
        db.add(server)
        audit(db, body.application_id, "insert", None, server, principal.sub)
        db.flush()  # populate defaults before snapshot
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
    """Partial update. If endpoint_url or auth changes, we re-discover to
    catch stale registrations early."""
    with session_scope() as db:
        server = db.get(MCPServer, application_id)
        if server is None:
            raise HTTPException(status_code=404, detail="Not found")

        # Snapshot before mutation for the audit row.
        before = MCPServer(**{c.name: getattr(server, c.name) for c in MCPServer.__table__.columns})

        changed = body.model_dump(exclude_unset=True, by_alias=True)
        if "metadata" in changed:
            server.server_metadata = changed.pop("metadata")
        for k, v in changed.items():
            setattr(server, k, v)

        # If the endpoint or auth changed, re-discover — a broken update
        # should fail loudly, not silently persist.
        if any(k in changed for k in ("endpoint_url", "auth_type", "auth_ref")):
            try:
                discovery = call_discover(server.endpoint_url, server.auth_type, server.auth_ref)
                server.capabilities_snapshot = discovery.model_dump()
                if not discovery.read_only_default:
                    logger.warning(
                        "server %s declares read_only_default=false via PATCH; grandfathered in",
                        application_id,
                    )
            except DiscoveryError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"MCP re-discovery failed after PATCH: {exc}",
                )

        server.updated_at = datetime.now(timezone.utc)
        server.updated_by = principal.sub
        audit(db, application_id, "update", before, server, principal.sub)
        cache.invalidate(application_id)
        return _to_response(server)


@router.post("/{application_id}/propose", response_model=Optional[ProposeResponse])
def propose_via(
    application_id: UUID,
    body: ProposeRequest,
    principal: Principal = Depends(get_current_principal),
    response=None,
) -> Optional[ProposeResponse]:
    """Forward an aims_propose_resolution call to the registered server.

    Response codes:
      200 — proposal returned by the server
      204 — server exists but its declared handles don't match this incident
      404 — no active server registered for this application
      502 — server errored or returned an unparseable payload
    """
    from fastapi import Response
    from fastapi.responses import JSONResponse

    # Try cache first for the lookup — avoids a PG hit per propose.
    cached = cache.get_cached(application_id)
    server: Optional[MCPServer] = None
    if cached is not None and cached.get("status") == "active":
        with session_scope() as db:
            server = db.get(MCPServer, application_id)
    else:
        with session_scope() as db:
            server = db.get(MCPServer, application_id)

    if server is None or server.status != "active":
        raise HTTPException(status_code=404, detail="No active MCP server for this application")

    logger.info(
        "propose forward app_id=%s server=%s by=%s corr=%s",
        application_id,
        server.name,
        principal.sub,
        body.constraints.correlation_id,
    )

    try:
        proposal = forward_propose(server, body)
    except CapabilityMissError as exc:
        logger.info("propose 204 app_id=%s reason=%s", application_id, exc)
        return JSONResponse(status_code=204, content=None)
    except ProposeError as exc:
        logger.warning("propose 502 app_id=%s err=%s", application_id, exc)
        raise HTTPException(status_code=502, detail=f"MCP server error: {exc}")

    return proposal


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
