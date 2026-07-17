"""Reference stub MCP server — implements the AIMS resolution contract.

Purpose: provide a canned server the mcp-router can register and the
agent-service can call end-to-end for smoke tests. Real teams onboard by
implementing this same shape in their own service.

Behavior:
  * aims_discover_capabilities → declares handles for Trading UI's alert
    types + protocol_version="1", read_only_default=true.
  * aims_propose_resolution → echoes the input incident and returns a
    canned analysis + one diagnostic action + one remediation action.
    When ``metadata.stub_response`` is set to "unable" on the incoming
    incident, returns unable_to_diagnose=true so the agent-side path
    for that response shape can be exercised.

Not for production — no auth, no rate limit, no real logic. AKS pod for
in-cluster access only.
"""

import json
import logging
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="AIMS MCP reference stub", version="0.1.0")


DISCOVER_RESPONSE = {
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


def _wrap_result(payload: Dict[str, Any], call_id: str) -> Dict[str, Any]:
    """Wrap a tool result in MCP JSON-RPC + text-content shape."""
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
    }


def _propose_response(args: Dict[str, Any]) -> Dict[str, Any]:
    incident = args.get("incident") or {}
    similar = (args.get("context") or {}).get("similar_past_incidents") or []
    runbooks = (args.get("context") or {}).get("runbooks") or []
    stub_flag = (incident.get("metadata") or {}).get("stub_response")

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


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "healthy", "service": "mcp-stub"}


@app.post("/mcp/tools/call")
async def tools_call(request: Request) -> Dict[str, Any]:
    body = await request.json()
    method = body.get("method")
    params = body.get("params") or {}
    tool_name = params.get("name")
    args = params.get("arguments") or {}
    call_id = body.get("id") or "stub-call"

    logger.info("tool_call name=%s method=%s id=%s", tool_name, method, call_id)

    if method != "tools/call":
        raise HTTPException(status_code=400, detail=f"unsupported method: {method}")

    if tool_name == "aims_discover_capabilities":
        return _wrap_result(DISCOVER_RESPONSE, call_id)

    if tool_name == "aims_propose_resolution":
        return _wrap_result(_propose_response(args), call_id)

    raise HTTPException(status_code=400, detail=f"unknown tool: {tool_name}")
