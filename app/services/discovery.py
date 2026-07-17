"""Call the MCP server's aims_discover_capabilities on registration.

Failures raise DiscoveryError; caller (api/apps.py) surfaces as HTTP 400 with
a clear explanation. We DO NOT persist a server whose discovery is unreachable
— avoids "registered but broken" entries silently rotting.

Auth: if the registered server declares auth_type=bearer with a KV auth_ref,
we resolve the KV secret and attach it. For any other auth_type we make the
discovery call unauthenticated (servers implementing their own auth on a
subset of tools should still let aims_discover_capabilities through — it's a
prerequisite for onboarding).
"""

import logging
import re
from typing import Optional

import httpx

from app.config import settings
from app.schemas.mcp_server import DiscoveryResponse

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


def call_discover(endpoint_url: str, auth_type: str, auth_ref: Optional[str]) -> DiscoveryResponse:
    """POST tools/call aims_discover_capabilities against a server."""
    headers = {"Content-Type": "application/json"}
    if auth_type == "bearer" and auth_ref:
        token = _resolve_kv_secret(auth_ref)
        if token:
            headers["Authorization"] = f"Bearer {token}"

    payload = {
        "jsonrpc": "2.0",
        "id": "discover-registration",
        "method": "tools/call",
        "params": {"name": "aims_discover_capabilities", "arguments": {}},
    }

    timeout_s = settings.DISCOVERY_TIMEOUT_MS / 1000.0
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            resp = client.post(f"{endpoint_url.rstrip('/')}/tools/call", json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"unreachable ({type(exc).__name__}: {exc})") from exc

    if resp.status_code == 401:
        raise DiscoveryError(
            "server returned 401 to discovery — auth_ref may be wrong or not mounted"
        )
    if resp.status_code >= 400:
        raise DiscoveryError(f"HTTP {resp.status_code} from discovery: {resp.text[:200]}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise DiscoveryError(f"non-JSON response body: {exc}") from exc

    # MCP JSON-RPC wraps the tool result under `result.content` (spec) or
    # `result` directly (permissive). Try both.
    result = body.get("result")
    if isinstance(result, dict) and "content" in result:
        # Spec-compliant: content is a list of {type: "text", text: "<json>"}
        content = result["content"]
        if isinstance(content, list) and content and content[0].get("type") == "text":
            import json as _json
            try:
                result = _json.loads(content[0]["text"])
            except (ValueError, KeyError) as exc:
                raise DiscoveryError(f"result.content[0].text not valid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise DiscoveryError(f"unexpected response shape: keys={list(body.keys())}")

    try:
        discovery = DiscoveryResponse.model_validate(result)
    except Exception as exc:
        raise DiscoveryError(f"discovery payload failed validation: {exc}") from exc

    if discovery.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise DiscoveryError(
            f"protocol_version={discovery.protocol_version!r} not supported "
            f"(this router speaks {sorted(SUPPORTED_PROTOCOL_VERSIONS)})"
        )

    return discovery
