"""Thin sync wrapper around the official MCP client SDK.

The mcp SDK is fully async — it drives streamable-HTTP servers over an
SSE-per-response wire, does the initialize/notifications handshake, tracks
Mcp-Session-Id, and parses tool results out of the JSON-RPC envelope.

Our FastAPI endpoints are sync ``def`` (SQLAlchemy sync session, tight
audit-log transactions), so rather than convert every caller to ``async
def`` we run each MCP call in its own event loop via ``asyncio.run``.
Overhead is ~1ms per call — negligible next to the ~50ms handshake +
whatever the downstream server takes. If per-call handshake latency ever
matters we can pool sessions here without touching call sites.

Every failure — transport, session, tool-level, unparseable content —
raises MCPCallError. Discovery + propose map it to their own domain
exceptions with the router-specific messaging on top.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


class MCPCallError(RuntimeError):
    """Failed to complete an MCP tool call against a downstream server."""


def call_tool_sync(
    *,
    endpoint_url: str,
    tool_name: str,
    arguments: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Initialize a streamable-HTTP MCP session, call one tool, tear down.

    Returns the JSON-decoded tool payload. Contract for our tools is that
    the response is a single text-content item whose text is a JSON object
    (or, when the server sets it, ``structured_content`` directly).
    """
    async def _run() -> Dict[str, Any]:
        # timeout controls the initial connect/handshake; sse_read_timeout
        # caps how long we'll wait between SSE frames once the stream is up.
        # For our synchronous tool-call pattern both are the same budget.
        td = timedelta(seconds=timeout_s)
        async with streamablehttp_client(
            endpoint_url,
            headers=headers or {},
            timeout=td,
            sse_read_timeout=td,
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
                return _extract_payload(result, tool_name)

    try:
        return asyncio.run(_run())
    except MCPCallError:
        raise
    except Exception as exc:
        # asyncio.run can surface CancelledError, transport failures,
        # protocol errors — funnel all of them into MCPCallError so callers
        # get one exception type to translate.
        raise MCPCallError(
            f"{tool_name} failed: {type(exc).__name__}: {exc}"
        ) from exc


def _extract_payload(result: Any, tool_name: str) -> Dict[str, Any]:
    """Get the JSON payload from a CallToolResult.

    Prefer ``structured_content`` (newer SDK convention) if present. Fall
    back to parsing ``content[0].text`` as JSON (the durable convention:
    a single TextContent whose ``text`` is a JSON-encoded object).
    """
    if getattr(result, "isError", False):
        content_snippet = _first_text(result) or "<no text content>"
        raise MCPCallError(
            f"{tool_name} returned tool-level error: {content_snippet[:300]}"
        )

    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and structured:
        return structured

    text = _first_text(result)
    if text is None:
        raise MCPCallError(f"{tool_name} returned no parseable content")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise MCPCallError(
            f"{tool_name} content is not valid JSON: {exc}: {text[:200]}"
        ) from exc
    if not isinstance(payload, dict):
        raise MCPCallError(
            f"{tool_name} JSON payload is not an object (got {type(payload).__name__})"
        )
    return payload


def _first_text(result: Any) -> Optional[str]:
    content = getattr(result, "content", None) or []
    if not content:
        return None
    first = content[0]
    text = getattr(first, "text", None)
    return text if isinstance(text, str) else None
