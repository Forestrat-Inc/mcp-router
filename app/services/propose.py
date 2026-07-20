"""Forward an aims_propose_resolution call to the registered MCP server.

Auth resolution happens here (server-side of the router) so the agent
doesn't need to mount every MCP server's KV secret. Every capability
short-circuit — severity, alert_type match against the server's
declared handles — happens here too, so we surface 204 to the agent
when the server exists but doesn't want this incident.

Uses the official MCP client SDK via ``mcp_client.call_tool_sync`` — a
real streamable-HTTP client that does the initialize handshake and parses
SSE-per-response frames. Bespoke HTTP against ``/tools/call`` breaks on
real MCP servers (they return ``406 Missing session ID``).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from app.config import settings
from app.models import MCPServer
from app.schemas.mcp_server import (
    DiscoveryHandles,
    ProposeRequest,
    ProposeResponse,
)
from app.services import mcp_client
from app.services.discovery import _attach_auth

logger = logging.getLogger(__name__)


class ProposeError(RuntimeError):
    """The MCP server rejected the call or produced an unparseable response."""


class CapabilityMissError(ProposeError):
    """Server exists but declared handles don't include this incident.

    Raised so the API layer can distinguish "no proposal because server
    doesn't want it" (204 to the caller) from "no proposal because the
    server is broken" (502 to the caller).
    """


def _capability_match(handles: DiscoveryHandles, req: ProposeRequest) -> tuple[bool, list[str]]:
    """Cheap filter: does this incident fall inside the server's declared
    handles? Empty/omitted handles arrays mean "no filter — take everything."
    """
    misses: list[str] = []

    if handles.severities and req.incident.severity not in handles.severities:
        misses.append(f"severity={req.incident.severity!r} not in {handles.severities}")

    if handles.alert_types:
        alert_type = req.incident.metadata.get("alert_type")
        if alert_type and alert_type not in handles.alert_types:
            misses.append(f"alert_type={alert_type!r} not in {handles.alert_types}")

    if handles.metric_patterns:
        metric = req.incident.metadata.get("metric")
        if metric:
            if not any(re.search(p, metric) for p in handles.metric_patterns):
                misses.append(f"metric={metric!r} matches none of {handles.metric_patterns}")

    return (not misses, misses)


def forward_propose(server: MCPServer, req: ProposeRequest) -> Optional[ProposeResponse]:
    """Real MCP client call to aims_propose_resolution. Sync wrapper —
    the MCP SDK is async but the FastAPI handler is sync.

    Returns:
      - a validated ProposeResponse on success
      - raises CapabilityMissError if the server doesn't handle this incident
      - raises ProposeError on transport / parse failure
    """
    caps = server.capabilities_snapshot or {}
    handles = DiscoveryHandles.model_validate(caps.get("handles", {}))
    match, misses = _capability_match(handles, req)
    if not match:
        raise CapabilityMissError(
            f"server {server.name!r} does not handle this incident: {'; '.join(misses)}"
        )

    headers: dict = {}
    _attach_auth(headers, server.auth_type, server.auth_ref)

    # Cap the timeout at the router's global setting; server can go lower via
    # its declared max_response_ms.
    server_ms = int(caps.get("max_response_ms") or settings.RESOLUTION_CALL_TIMEOUT_MS)
    timeout_s = min(server_ms, settings.RESOLUTION_CALL_TIMEOUT_MS) / 1000.0

    try:
        payload = mcp_client.call_tool_sync(
            endpoint_url=server.endpoint_url,
            tool_name="aims_propose_resolution",
            arguments=req.model_dump(mode="json"),
            headers=headers,
            timeout_s=timeout_s,
        )
    except mcp_client.MCPCallError as exc:
        msg = str(exc)
        if "401" in msg or "Unauthorized" in msg:
            raise ProposeError(
                f"server returned 401 — auth_ref may be wrong or the KV secret isn't "
                f"mounted at /mnt/secrets/<name>. Details: {msg}"
            ) from exc
        raise ProposeError(f"MCP propose call failed: {msg}") from exc

    # server_name isn't guaranteed to be in the tool response — inject from
    # the registration snapshot so the agent-side rendering always has a
    # display name.
    payload.setdefault("server_name", server.name)

    try:
        response = ProposeResponse.model_validate(payload)
    except Exception as exc:
        raise ProposeError(f"response failed validation: {exc}") from exc

    logger.info(
        "propose_ok server=%s incident=%s confidence=%.2f actions=%d unable=%s",
        server.name,
        req.incident.id,
        response.confidence,
        len(response.recommended_actions),
        response.unable_to_diagnose,
    )
    return response
