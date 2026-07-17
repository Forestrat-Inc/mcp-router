"""Forward an aims_propose_resolution call to the registered MCP server.

Auth resolution happens here (server-side of the router) so the agent
doesn't need to mount every MCP server's KV secret. Every capability
short-circuit — severity, alert_type match against the server's
declared handles — happens here too, so we surface 204 to the agent
when the server exists but doesn't want this incident.
"""

import logging
import re
from typing import Optional

import httpx

from app.config import settings
from app.models import MCPServer
from app.schemas.mcp_server import (
    DiscoveryHandles,
    ProposeRequest,
    ProposeResponse,
)
from app.services.discovery import _resolve_kv_secret

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
    """POST tools/call aims_propose_resolution to the registered server.

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

    headers = {"Content-Type": "application/json"}
    if server.auth_type == "bearer" and server.auth_ref:
        token = _resolve_kv_secret(server.auth_ref)
        if token:
            headers["Authorization"] = f"Bearer {token}"

    # Cap the timeout at the router's global setting; server can go lower via
    # its declared max_response_ms.
    server_ms = int(caps.get("max_response_ms") or settings.RESOLUTION_CALL_TIMEOUT_MS)
    timeout_s = min(server_ms, settings.RESOLUTION_CALL_TIMEOUT_MS) / 1000.0

    payload = {
        "jsonrpc": "2.0",
        "id": req.constraints.correlation_id or "propose",
        "method": "tools/call",
        "params": {
            "name": "aims_propose_resolution",
            "arguments": req.model_dump(mode="json"),
        },
    }

    endpoint = f"{server.endpoint_url.rstrip('/')}/tools/call"
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise ProposeError(f"transport error: {type(exc).__name__}: {exc}") from exc

    if resp.status_code >= 500:
        raise ProposeError(f"HTTP {resp.status_code} from server: {resp.text[:300]}")
    if resp.status_code == 401:
        raise ProposeError(
            "server returned 401 — auth_ref may be wrong or the KV secret isn't mounted"
        )
    if resp.status_code >= 400:
        raise ProposeError(f"HTTP {resp.status_code} from server: {resp.text[:300]}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise ProposeError(f"non-JSON body: {exc}") from exc

    # Spec: result.content[0].text is JSON-encoded string. Permissive: result
    # may be the parsed object directly. Same shape handling as discovery.
    result = body.get("result")
    if isinstance(result, dict) and "content" in result:
        content = result["content"]
        if isinstance(content, list) and content and content[0].get("type") == "text":
            import json as _json
            try:
                result = _json.loads(content[0]["text"])
            except (ValueError, KeyError) as exc:
                raise ProposeError(f"result.content[0].text invalid JSON: {exc}") from exc

    if not isinstance(result, dict):
        raise ProposeError(f"unexpected response shape (keys={list(body.keys())})")

    # server_name isn't in the tool response — inject from the registration
    # snapshot so the agent-side rendering always has a display name.
    result.setdefault("server_name", server.name)

    try:
        return ProposeResponse.model_validate(result)
    except Exception as exc:
        raise ProposeError(f"response failed validation: {exc}") from exc
