"""Call the MCP server's aims_discover_capabilities on registration.

We interview the server *before* persisting the registration — a server
that can't answer discovery is rejected here, not silently persisted.

Uses the official MCP client SDK via ``mcp_client.call_tool_sync`` — a
real streamable-HTTP client that does the initialize handshake, tracks the
session id, and parses SSE-per-response frames. Bespoke HTTP against
``/tools/call`` isn't spec-compliant and breaks on real MCP servers (they
return ``406 Missing session ID`` — see the trading-ui-mcp registration
notes in docs/contracts.md §5).

Auth: if the registered server declares auth_type=bearer with a KV
auth_ref, we resolve the KV secret via the CSI mount and pass it as a
header on every request in the MCP session. Any auth_type without a
usable ``auth_ref`` calls discovery unauthenticated — servers requiring
auth on discovery should return 401, which surfaces here as a clear
"secret not mounted; add to keyvault-mcp-router SPC" error.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from app.config import settings
from app.schemas.mcp_server import DiscoveryResponse
from app.services import mcp_client

logger = logging.getLogger(__name__)

_KV_URI_RE = re.compile(r"^kv://([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)$")

# Protocol versions we understand. Adding one is a code change (a new one may
# ship payload-shape changes that need adapting).
SUPPORTED_PROTOCOL_VERSIONS = {"1"}


class DiscoveryError(RuntimeError):
    """Raised by call_discover when the server can't be interviewed cleanly."""


def _resolve_kv_secret(auth_ref: str) -> Optional[str]:
    """Resolve kv://<vault>/<name> → secret value via the CSI mount.

    We deliberately do NOT reach out to Azure Key Vault directly; the CSI
    Secret Store driver already mounts every KV secret the pod's SPC lists at
    /mnt/secrets/<name>. That mount is the authoritative source at runtime.
    Servers registered with a secret NOT already in the SPC will get None back
    here and the discovery call will go unauthenticated — the server should
    return 401 in that case, which becomes an actionable "add the secret to
    the SPC" error at registration.
    """
    match = _KV_URI_RE.match(auth_ref)
    if not match:
        return None
    _vault, name = match.groups()
    path = f"/mnt/secrets/{name}"
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning(
            "kv secret not mounted; add %s to keyvault-mcp-router SPC to auth this server",
            name,
        )
        return None
    except OSError as exc:
        logger.warning("kv secret read failed name=%s err=%s", name, type(exc).__name__)
        return None


def _attach_auth(headers: dict, auth_type: str, auth_ref: Optional[str]) -> None:
    """Resolve the auth_ref secret and attach the right header.

    Kept out of the call sites so discovery + propose share exactly one
    place where the mapping ``auth_type → header shape`` lives.
    """
    if not auth_ref:
        return
    secret = _resolve_kv_secret(auth_ref)
    if not secret:
        return
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif auth_type == "api_key":
        headers["X-API-Key"] = secret


def call_discover(endpoint_url: str, auth_type: str, auth_ref: Optional[str]) -> DiscoveryResponse:
    """Real MCP client call to aims_discover_capabilities. Sync wrapper —
    the MCP SDK is async but the FastAPI handler is sync."""
    headers: dict = {}
    _attach_auth(headers, auth_type, auth_ref)

    timeout_s = settings.DISCOVERY_TIMEOUT_MS / 1000.0

    try:
        payload = mcp_client.call_tool_sync(
            endpoint_url=endpoint_url,
            tool_name="aims_discover_capabilities",
            arguments={},
            headers=headers,
            timeout_s=timeout_s,
        )
    except mcp_client.MCPCallError as exc:
        msg = str(exc)
        # Common 401 shape from the SDK: "HTTP 401" or "Unauthorized" in
        # the transport error. Map to the actionable "check KV" message.
        if "401" in msg or "Unauthorized" in msg:
            raise DiscoveryError(
                f"server returned 401 to discovery — auth_ref may be wrong or the "
                f"KV secret isn't mounted at /mnt/secrets/<name>. Details: {msg}"
            ) from exc
        raise DiscoveryError(f"MCP discovery call failed: {msg}") from exc

    try:
        discovery = DiscoveryResponse.model_validate(payload)
    except Exception as exc:
        raise DiscoveryError(f"discovery payload failed validation: {exc}") from exc

    if discovery.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise DiscoveryError(
            f"protocol_version={discovery.protocol_version!r} not supported "
            f"(this router speaks {sorted(SUPPORTED_PROTOCOL_VERSIONS)})"
        )

    logger.info(
        "discovery_ok endpoint=%s server=%s protocol=%s handles_severities=%s handles_alerts=%s",
        endpoint_url,
        discovery.server_name,
        discovery.protocol_version,
        discovery.handles.severities,
        discovery.handles.alert_types,
    )
    return discovery
