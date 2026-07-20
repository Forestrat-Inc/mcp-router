"""Reference stub MCP server — implements the AIMS resolution contract as
a *real, spec-compliant streamable-HTTP MCP server* using fastmcp.

Purpose: provide a canned server the mcp-router can register and the
agent-service can call end-to-end for smoke tests. Real teams onboard by
implementing this same shape in their own service.

What makes this a real MCP server (vs the earlier bespoke JSON endpoint):
  * ``initialize`` / ``notifications/initialized`` handshake handled by fastmcp
  * ``tools/list`` / ``tools/call`` handled by fastmcp
  * ``Mcp-Session-Id`` header management handled by fastmcp
  * SSE-per-response responses (``Accept: text/event-stream``)

Tools:
  * ``aims_discover_capabilities`` — declares handles + declared_actions.
  * ``aims_propose_resolution`` — echoes the input incident and returns a
    canned analysis + one diagnostic action + one remediation action.
    When ``incident.metadata.stub_response == "unable"``, returns
    ``unable_to_diagnose=true`` so the agent's alternate render path can
    be exercised.

Not for production — no auth, no rate limit, no real reasoning. AKS pod
for in-cluster access only.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastmcp import FastMCP
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


mcp = FastMCP(name="mcp-stub", version="0.1.0")


DISCOVER_RESPONSE: Dict[str, Any] = {
    "protocol_version": "1",
    "server_name": "mcp-stub",
    "server_version": "0.1.0",
    "handles": {
        "severities": ["P1", "P2", "P3", "P4"],
        "alert_types": ["calcite_formula", "text_contains", "threshold_numeric"],
        "metric_patterns": [],
        "error_signatures": [],
    },
    "declared_actions": [
        {"id": "stub-restart-worker", "reversible": True, "requires_approval": True},
        {"id": "stub-flush-cache", "reversible": True, "requires_approval": False},
    ],
    "max_response_ms": 15000,
    "read_only_default": True,
}


@mcp.tool()
def aims_discover_capabilities() -> Dict[str, Any]:
    """Return this server's capability declaration. Called once by the
    router at registration time."""
    logger.info("discover_called")
    return DISCOVER_RESPONSE


@mcp.tool()
def aims_propose_resolution(
    incident: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
    constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a canned proposal for the incoming incident.

    ``stub_response="unable"`` in incident.metadata triggers the
    unable_to_diagnose path so the agent's alternate rendering can be
    smoke-tested.
    """
    ctx = context or {}
    similar = ctx.get("similar_past_incidents") or []
    runbooks = ctx.get("runbooks") or []
    stub_flag = (incident.get("metadata") or {}).get("stub_response")

    logger.info(
        "propose_called incident_id=%s severity=%s similar=%d runbooks=%d",
        incident.get("id"), incident.get("severity"), len(similar), len(runbooks),
    )

    if stub_flag == "unable":
        return {
            "server_name": "mcp-stub",
            "confidence": 0.0,
            "analysis": "Stub server configured to return unable_to_diagnose for this test path.",
            "recommended_actions": [],
            "next_investigations": [],
            "cited_past_incidents": [],
            "cited_runbooks": [],
            "unable_to_diagnose": True,
            "reasons": ["stub_response=unable set on incident.metadata"],
        }

    cited_past = [s["id"] for s in similar[:2] if isinstance(s, dict) and "id" in s]
    cited_runbooks = [r["url"] for r in runbooks[:2] if isinstance(r, dict) and r.get("url")]

    analysis_lines = [
        f"Stub analysis of incident '{incident.get('title', '<untitled>')}'.",
        f"Severity={incident.get('severity')} status={incident.get('status')}.",
    ]
    if cited_past:
        analysis_lines.append(
            f"Matches {len(cited_past)} prior incident(s) with high similarity — "
            f"strongest score {similar[0].get('similarity_score', 0):.2f}."
        )
    else:
        analysis_lines.append("No prior similar incidents in the passed context.")
    if incident.get("metadata"):
        analysis_lines.append(f"Signal fields: {sorted((incident.get('metadata') or {}).keys())}.")

    return {
        "server_name": "mcp-stub",
        "confidence": 0.82 if cited_past else 0.45,
        "analysis": " ".join(analysis_lines),
        "recommended_actions": [
            {
                "kind": "diagnostic",
                "description": "Check the affected worker's heartbeat age. Stall > 5 min → stuck.",
                "reversible": True,
                "requires_approval": False,
                "action_id": None,
                "action_args": {},
                "estimated_impact": "read-only, no runtime effect",
            },
            {
                "kind": "remediation",
                "description": "Restart the affected worker to recycle its downstream connection.",
                "reversible": True,
                "requires_approval": True,
                "action_id": "stub-restart-worker",
                "action_args": {"target": "affected-worker"},
                "estimated_impact": "5-second pause on affected workflow",
            },
        ],
        "next_investigations": [
            "Correlate current metric value with prior baseline.",
            "Confirm no upstream data source is throttling.",
        ],
        "cited_past_incidents": cited_past,
        "cited_runbooks": cited_runbooks,
        "unable_to_diagnose": False,
    }


# ── HTTP surface ────────────────────────────────────────────────────────
# fastmcp gives us a Starlette app for the streamable-HTTP MCP endpoint.
# We front it with a tiny FastAPI parent so we can add a plain /health for
# the kubelet readinessProbe (the MCP endpoint itself needs a full MCP
# handshake and won't answer a bare GET).
#
# Lifespan gotcha: fastmcp's streamable_http_app() has its own lifespan
# (task groups, session manager). Mounting a sub-app does NOT run its
# lifespan by default — we have to propagate it to the parent's lifespan
# or the MCP endpoint 500s on the first request with "task group not
# initialised." Call streamable_http_app() ONCE and reuse.

_mcp_asgi_app = mcp.streamable_http_app()

app = FastAPI(
    title="AIMS MCP reference stub",
    version="0.1.0",
    lifespan=_mcp_asgi_app.router.lifespan_context,
)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "healthy", "service": "mcp-stub"}


# MCP mount — the router's endpoint_url should be
#   http://mcp-stub.mcp.svc.cluster.local:9000/mcp/
# with the trailing slash. Bare /mcp responds 307 to /mcp/ by design.
app.mount("/mcp", _mcp_asgi_app)
