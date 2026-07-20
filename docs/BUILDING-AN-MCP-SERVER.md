# Building an MCP server that AIMS can talk to

> **Audience.** Any Forestrat team whose application shows up in AIMS as
> an incident source and wants AIMS's "Resolve with AI" to invoke their
> own agent for triage / diagnosis / recommendations.
>
> **What you'll ship.** A streamable-HTTP MCP server exposing two tools
> (`aims_discover_capabilities` + `aims_propose_resolution`), an
> in-cluster ClusterIP service, a bearer token in Key Vault, and one PR
> against the mcp-router to register it. **Estimated size: 1 day for
> Phase-1 stub end-to-end, 2-3 days including wiring your real agent.**
>
> **Wire contract** (what your server MUST return + what AIMS sends):
> [`contracts.md`](contracts.md). Read that AFTER this guide — this is
> the tutorial, that is the reference.

---

## Table of contents

1. [The 60-second mental model](#1-the-60-second-mental-model)
2. [Prerequisites](#2-prerequisites)
3. [Build the server (fastmcp reference)](#3-build-the-server-fastmcp-reference)
4. [Auth middleware — bearer token](#4-auth-middleware--bearer-token)
5. [Deploy to Kubernetes](#5-deploy-to-kubernetes)
6. [Provision the bearer token in Key Vault](#6-provision-the-bearer-token-in-key-vault)
7. [Register with the mcp-router](#7-register-with-the-mcp-router)
8. [Verification checklist](#8-verification-checklist)
9. [Wiring your real agent into `aims_propose_resolution`](#9-wiring-your-real-agent-into-aims_propose_resolution)
10. [Best practices](#10-best-practices)
11. [Gotchas we've hit (learn from our pain)](#11-gotchas-weve-hit-learn-from-our-pain)
12. [Updating an existing registration](#12-updating-an-existing-registration)
13. [Reference implementations](#13-reference-implementations)

---

## 1. The 60-second mental model

```
   User clicks "Resolve with AI" on incident I in application A
       │
       ▼
   aims-agent-service                              your MCP server
   ┌────────────────────┐                          ┌──────────────────────────┐
   │ (1) GET /apps/{A}  │───────► mcp-router       │  streamable-HTTP MCP     │
   │     from registry  │◄─────── returns          │                          │
   │  ─ endpoint_url    │         registration     │  @tool                   │
   │  ─ auth_ref (kv://)│                          │  aims_discover_…()       │
   │  ─ capabilities    │                          │  aims_propose_…(…)       │
   └─────────┬──────────┘                          │                          │
             │                                     │  Your real agent runs    │
             │ (2) read the bearer from            │  inside aims_propose_…   │
             │     /mnt/secrets/<name>             │                          │
             │                                     └──────────────────────────┘
             │ (3) open MCP session, call                     ▲
             │     aims_propose_resolution({incident,        │
             │       context, constraints})                  │
             └──────────────────────────────────────────────┘
                        response: { analysis, recommended_actions, ... }
```

- **mcp-router is a service-discovery registry** — it stores `application_id → server` mappings. It never calls your server.
- **AIMS agent calls your server directly** — via the streamable-HTTP `mcp` SDK. So you don't need to know anything about the router's internals.
- **Your server hosts the domain-specific "agent"** — usually a wrapper around your existing LangGraph/Claude/whatever that reasons about YOUR system's incidents.

---

## 2. Prerequisites

Before writing any code, confirm:

- [ ] **You have an `application_id` in AIMS.** Ask the AIMS team if you don't know it — it's a UUID like `59de013d-8cdf-44e0-89a1-dfad8c325d39`. If your app isn't in AIMS's application registry yet, register it first via `POST /api/v1/applications` (see AIMS docs). **One MCP server per application_id.**
- [ ] **You can create a ClusterIP service in your AKS namespace.** No public ingress needed for the MCP endpoint (see §5).
- [ ] **You can write a secret to `forestrat-kv`** OR you can hand a token to the AIMS admin over a secure channel (see §6).
- [ ] **You know what "resolve with AI" should mean for your app** — what tools would you WANT an agent to have when triaging your incidents? What actions would you WANT it to recommend? (You can start with a Phase-1 stub and iterate — see §9.)

---

## 3. Build the server (fastmcp reference)

**Framework choice: `fastmcp>=2.9`.** Anthropic-maintained (from v2 onwards; the older v0.4.x is stdio+SSE only — you need 2.x for streamable-HTTP). Ships an ASGI-app builder with `@mcp.tool()` decorators.

```bash
pip install "fastmcp>=2.9.0,<4.0.0" "fastapi>=0.115" "uvicorn[standard]>=0.31"
```

### 3.1 The two required tools

`your_service/mcp/server.py`:

```python
"""Your team's MCP server — implements the AIMS resolution contract.

Two tools:
  - aims_discover_capabilities: called once at registration; declares
    what this server handles + what actions it might recommend.
  - aims_propose_resolution: called every incident; runs your agent,
    returns analysis + recommended_actions.

Bearer auth is added by the parent FastAPI app via BearerAuthMiddleware
(see auth.py). This file focuses on tools.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP(name="my-service-mcp", version="0.1.0")


# ── Tool 1: discovery ───────────────────────────────────────────────────

@mcp.tool()
def aims_discover_capabilities() -> Dict[str, Any]:
    """Return this server's capability declaration. Called once at
    registration. Must be fast (< 2s) and cannot depend on live LLM
    calls — it's metadata."""
    logger.info("discover_called")
    return {
        "protocol_version": "1",
        "server_name": "my-service-mcp",
        "server_version": "0.1.0",
        "handles": {
            # Only incidents matching ALL non-empty filters get routed
            # here. Leaving a filter empty means "accept anything on
            # this dimension." See §10 for tuning guidance.
            "severities":       ["P1", "P2", "P3"],
            "alert_types":      ["threshold_numeric", "text_contains"],
            "metric_patterns":  [r"^my_service\.", r"^myapp\."],
            "error_signatures": [],
        },
        "declared_actions": [
            # Every action your agent might recommend must appear here.
            # requires_approval=true means the agent-side UI shows a
            # "Confirm" button before the action is executed.
            #
            # (Execution is a v2 feature — today AIMS only surfaces
            # actions as recommendations; there is no aims_execute_action
            # call yet. But list them anyway for forward compat + UI hints.)
            {"id": "read_metric_history", "reversible": True,  "requires_approval": False},
            {"id": "restart_worker",      "reversible": False, "requires_approval": True},
        ],
        # Your SLA. AIMS caps its call timeout at this OR its own global
        # (30s) — whichever is shorter.
        "max_response_ms": 15000,
        # Keep True unless you know you want write-capable propose calls.
        # See §10 for the write-action guardrail.
        "read_only_default": True,
    }


# ── Tool 2: propose resolution ──────────────────────────────────────────

@mcp.tool()
def aims_propose_resolution(
    incident: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
    constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Called for every incident that passes the handles filter.

    Structure of `incident`, `context`, `constraints`: see contracts.md
    §2.2. TL;DR — you get the full incident + up to 5 similar past
    incidents from AIMS's knowledge graph + up to 5 runbooks.
    """
    ctx = context or {}
    con = constraints or {}
    correlation_id = con.get("correlation_id")

    logger.info(
        "propose_called incident_id=%s severity=%s corr=%s",
        incident.get("id"), incident.get("severity"), correlation_id,
    )

    # ── Phase 1: return a stub so the plumbing lights up end-to-end
    #    before you wire in your real agent. Ship this first, verify
    #    AIMS renders it, then move to Phase 2.
    similar = ctx.get("similar_past_incidents") or []
    return {
        "server_name": "my-service-mcp",
        "confidence": 0.5,
        "analysis": (
            f"[Phase-1 stub — my-service agent not yet wired in.] "
            f"Received incident {incident.get('title', '<untitled>')!r} "
            f"(severity {incident.get('severity')}). "
            f"AIMS cited {len(similar)} similar past incident(s)."
        ),
        "recommended_actions": [],
        "next_investigations": [
            "Check my_service dashboard for the affected component.",
        ],
        "cited_past_incidents": [s["id"] for s in similar[:2] if isinstance(s, dict) and "id" in s],
        "cited_runbooks": [],
        "unable_to_diagnose": False,
    }

    # ── Phase 2: replace the stub above with your real agent invocation.
    #    See §9 for the pattern.


# ── ASGI app wiring ─────────────────────────────────────────────────────
# fastmcp exposes a Starlette app. Its lifespan MUST run for the session
# manager to init — see §11 gotcha #2. We propagate it to the parent
# FastAPI's lifespan so kubelet gets a real /health endpoint while the
# MCP endpoint stays behind auth.

from fastapi import FastAPI  # noqa: E402


_mcp_asgi_app = mcp.streamable_http_app()  # call ONCE — reused below

app = FastAPI(
    title="my-service MCP",
    version="0.1.0",
    lifespan=_mcp_asgi_app.router.lifespan_context,  # ← REQUIRED
)


@app.get("/health")
def health() -> Dict[str, str]:
    """No auth. Kubelet readiness + liveness."""
    return {"status": "ok", "service": "my-service-mcp"}


# Mount the MCP endpoint at /mcp — the router registers your endpoint_url
# with the trailing slash: `http://<svc>.<ns>.svc.cluster.local:<port>/mcp/`
app.mount("/mcp", _mcp_asgi_app)
```

**Run locally:**

```bash
uvicorn your_service.mcp.server:app --host 0.0.0.0 --port 3000
# Then verify:
curl -X POST http://localhost:3000/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":"init","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# → 200 with `event: message\ndata: {...serverInfo...}\n\n`
```

---

## 4. Auth middleware — bearer token

fastmcp doesn't do auth; you add it as ASGI middleware on the parent FastAPI. The AIMS agent sends `Authorization: Bearer <token>` on every request; you validate against a known-good list.

Two viable places to store the accepted-tokens list:

| Storage | Rotation cost | Best when |
|---|---|---|
| **KV secret via CSI mount** (recommended) | Update KV, redeploy, done | You already have workload-identity for KV |
| **Flat JSON in a K8s Secret** | Update Secret, redeploy | Multiple keys per server (per-consumer isolation) |

`your_service/mcp/auth.py` (KV secret variant):

```python
"""Bearer auth middleware — validates Authorization: Bearer <token>
against the value in a CSI-mounted KV secret.

The AIMS agent uses one token per MCP server; adding a second consumer
means either sharing the token or introducing a small allowlist file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Mount path is set by your K8s SPC — see §5 for the manifest.
_TOKEN_PATH = Path("/mnt/secrets/my-service-mcp-router-token")


def _load_token() -> Optional[str]:
    if not _TOKEN_PATH.exists():
        logger.warning("mcp_bearer_not_mounted path=%s", _TOKEN_PATH)
        return None
    return _TOKEN_PATH.read_text().strip()


# Loaded once at import time. Redeploy to rotate.
_ACCEPTED_TOKEN = _load_token()


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # /health stays unauthenticated for kubelet.
        if request.url.path in ("/health", "/healthz", "/readyz"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        token = auth[7:].strip()
        if _ACCEPTED_TOKEN is None or token != _ACCEPTED_TOKEN:
            raise HTTPException(401, "unknown token")
        return await call_next(request)
```

Then in your server.py, add:

```python
from your_service.mcp.auth import BearerAuthMiddleware
app.add_middleware(BearerAuthMiddleware)
```

**Multi-consumer variant** (flat JSON): swap `_ACCEPTED_TOKEN: str` for `_ACCEPTED_TOKENS: dict[str, str]` where the map is `{token: consumer_label}`. Middleware becomes `if token not in _ACCEPTED_TOKENS: raise 401`. Stash `request.state.mcp_caller = _ACCEPTED_TOKENS[token]` for logging. See the AIMS Trading UI handoff for a complete example of this pattern.

---

## 5. Deploy to Kubernetes

### 5.1 Deployment + Service + SPC

Assumes you already have:
- A namespace + workload identity federated with `forestrat-kv` reader
- A SecretProviderClass that mounts your other secrets

Add:

**`k8s/service.yaml`** — a ClusterIP so aims-agent can hit you in-cluster:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service-mcp
  namespace: my-service         # ← your namespace
spec:
  selector:
    app: my-service-mcp         # ← matches your Deployment labels
  ports:
    - port: 3000
      targetPort: 3000
      name: http
```

**`k8s/secretproviderclass.yaml`** — add the token to your SPC's `objects` array:

```yaml
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: my-service-secrets
  namespace: my-service
spec:
  provider: azure
  parameters:
    usePodIdentity: "false"
    useVMManagedIdentity: "false"
    clientID: "<your-managed-identity-client-id>"
    keyvaultName: "forestrat-kv"
    tenantId:   "abb71f25-7b08-4934-afb1-1d28988d5219"
    objects: |
      array:
        # ... your other secrets ...
        - |
          objectName: my-service-mcp-router-token
          objectType: secret
  # No secretObjects entry needed if the middleware reads the CSI mount
  # directly (recommended, avoids the CSI mirror-refresh gotcha — see §11).
```

**Deployment volumeMount:**

```yaml
containers:
  - name: my-service-mcp
    image: ...
    ports:
      - containerPort: 3000
    readinessProbe:
      httpGet:
        path: /health
        port: 3000
      initialDelaySeconds: 3
      periodSeconds: 5
    volumeMounts:
      - name: secrets
        mountPath: /mnt/secrets
        readOnly: true
volumes:
  - name: secrets
    csi:
      driver: secrets-store.csi.k8s.io
      readOnly: true
      volumeAttributes:
        secretProviderClass: my-service-secrets
```

### 5.2 NetworkPolicy (recommended, optional)

Restrict incoming to the `aims` namespace (where aims-agent runs). Everything else stays external-facing per your existing setup.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: my-service-mcp-from-aims
  namespace: my-service
spec:
  podSelector:
    matchLabels:
      app: my-service-mcp
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: aims
      ports:
        - port: 3000
          protocol: TCP
```

---

## 6. Provision the bearer token in Key Vault

```bash
# 1. Generate a random token
NEW_TOKEN="mcp_$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-43)"

# 2. Store in KV (name it after your service + the consumer)
az keyvault secret set --vault-name forestrat-kv \
  --name my-service-mcp-router-token \
  --value "$NEW_TOKEN"

# 3. Verify (should return prefix)
az keyvault secret show --vault-name forestrat-kv \
  --name my-service-mcp-router-token --query 'value' -o tsv | cut -c1-12
```

**Naming convention:** `<consumer>-<yourservice>-mcp-router-token`. Today the only consumer is `aims-agent`, so if you cut a single dedicated token: `aims-agent-my-service-mcp-router-token`. If you use the AIMS pre-existing pattern name it after your service alone: `my-service-mcp-router-token` is fine when there's only one consumer.

**Rotation:** update the KV secret, redeploy your MCP server pod (the middleware reads the mount at import time). Consumers (aims-agent) need a redeploy too because they mount the same secret in their SPC.

**Ask the AIMS team to add the KV secret to aims-agent's SPC.** Send them:

> Please add `my-service-mcp-router-token` to `backend/aims-agent-service/k8s/secretproviderclass.yaml` `objects` array so the CSI driver mounts it into the agent pod at `/mnt/secrets/my-service-mcp-router-token`. That's how the agent authenticates against my MCP server.

---

## 7. Register with the mcp-router

Once your MCP pod is up + reachable + the token is in KV + the AIMS agent's SPC includes the KV secret name, hand this to the AIMS team (or run it yourself if you have the admin key):

```bash
export MCP_ADMIN_KEY="<get-from-forestrat-kv/mcp-router-api-keys-admin>"

curl -X POST https://aims-az.forestrat.ai/api/mcp/apps \
  -H "X-API-Key: $MCP_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "application_id": "<your-AIMS-app-uuid>",
    "name":           "my-service-mcp",
    "transport":      "http",
    "endpoint_url":   "http://my-service-mcp.my-service.svc.cluster.local:3000/mcp/",
    "auth_type":      "bearer",
    "auth_ref":       "kv://forestrat-kv/my-service-mcp-router-token",
    "owner_email":    "my-team@forestrat.ai",
    "status":         "active",
    "capabilities": {
      "protocol_version": "1",
      "server_name": "my-service-mcp",
      "server_version": "0.1.0",
      "handles": {
        "severities":  ["P1", "P2", "P3"],
        "alert_types": ["threshold_numeric", "text_contains"],
        "metric_patterns": ["^my_service\\."]
      },
      "declared_actions": [
        {"id": "read_metric_history", "reversible": true,  "requires_approval": false},
        {"id": "restart_worker",      "reversible": false, "requires_approval": true}
      ],
      "read_only_default": true,
      "max_response_ms": 15000
    }
  }'
```

Note:
- **Trailing slash on `endpoint_url`** — `/mcp/` not `/mcp`. Bare `/mcp` returns a 307 to `/mcp/` and while the mcp SDK follows redirects, it's a wasted round-trip.
- **`auth_ref` MUST be `kv://<vault>/<name>` form** — the router rejects raw tokens by shape check.
- **`capabilities` is a SNAPSHOT you provide** — the router doesn't call your server to fetch this. It's what aims-agent's UI displays + what consumers use as a hint. Aims-agent doesn't currently use `capabilities.handles` to filter (it invokes your `aims_discover_capabilities` if it wants to re-verify), but populating it is good for humans reading the registration.

On success you get `201` with the record. On failure (409 — already registered, 400 — bad payload shape), the response body has the reason.

**Alternative — use the mcp-router UI**: [https://aims-az.forestrat.ai/api/mcp/ui/](https://aims-az.forestrat.ai/api/mcp/ui/) has a form for the same fields.

---

## 8. Verification checklist

Walk these in order:

- [ ] **Local test** — `curl POST http://localhost:3000/mcp/` with `initialize` returns 200 + serverInfo (see §3 curl).
- [ ] **In-cluster health** — from a temp pod in the aims namespace: `kubectl -n aims run smoke --rm -it --image=curlimages/curl -- curl -sf http://my-service-mcp.my-service.svc.cluster.local:3000/health` → 200.
- [ ] **In-cluster auth** — same, but hit `/mcp/` with the bearer: should get 200 back on `initialize`. Without the bearer: 401.
- [ ] **Registration** — `curl -H "X-API-Key: $MCP_ADMIN_KEY" https://aims-az.forestrat.ai/api/mcp/apps/<your-app-id>` returns 200 with the record.
- [ ] **Registration visible in UI** — [https://aims-az.forestrat.ai/api/mcp/ui/](https://aims-az.forestrat.ai/api/mcp/ui/) shows your entry as `active`.
- [ ] **AIMS agent CSI mount** — ask AIMS team to verify `kubectl -n aims exec deploy/aims-agent -- ls /mnt/secrets | grep my-service-mcp-router-token` returns your token filename.
- [ ] **End-to-end** — trigger a real (or synthetic) incident for your application in AIMS, click **Resolve with AI**, watch the response render. Your Phase-1 stub text should appear.
- [ ] **Logs on your side** — `kubectl -n my-service logs deploy/my-service-mcp` should show `propose_called incident_id=… corr=aims-…` per invocation.

---

## 9. Wiring your real agent into `aims_propose_resolution`

The Phase-1 stub proves plumbing. Phase 2 is where actual value shows up.

### 9.1 Pattern

```python
from your_service.agent import invoke_service_agent  # ← YOUR existing agent

@mcp.tool()
def aims_propose_resolution(
    incident: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
    constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ctx = context or {}
    con = constraints or {}

    # 1. Build the input your agent understands.
    agent_input = {
        "problem_summary":     incident.get("title", ""),
        "problem_description": incident.get("description", ""),
        "severity":            incident.get("severity"),
        "metric":              incident.get("metadata", {}).get("metric"),
        "affected_account":    incident.get("metadata", {}).get("account_id"),
        "similar_past":        ctx.get("similar_past_incidents", []),
        "runbooks":            ctx.get("runbooks", []),
        # HARD CONSTRAINT — do not remove:
        # constraints.read_only=true means your agent may reason about
        # remediations but MUST NOT execute them. Gate your write-tools
        # behind this flag or use a distinct "propose-mode" prompt that
        # only exposes read tools.
        "read_only_mode":      con.get("read_only", True),
        "budget_ms":           con.get("max_thinking_ms", 30_000),
    }

    # 2. Invoke YOUR agent. Whatever it is — LangGraph, Claude Agent SDK,
    #    raw Anthropic Messages loop. Wrap it here.
    result = invoke_service_agent(agent_input)

    # 3. Normalize to the ProposeResponse shape. If your agent already
    #    emits structured JSON, this is a direct map. If it emits prose,
    #    either prompt it to emit JSON or do a small "extract to JSON"
    #    second-pass LLM call (see §10 for tradeoff).
    return {
        "server_name":           "my-service-mcp",
        "confidence":            float(result.confidence),
        "analysis":              result.analysis_text,
        "recommended_actions": [
            {
                "kind":              a.kind,           # diagnostic | remediation | escalation | information
                "description":       a.description,
                "reversible":        a.reversible,
                "requires_approval": a.requires_approval,
                "action_id":         a.action_id,      # MUST be in your declared_actions[] from discovery
                "action_args":       a.args,
                "estimated_impact":  a.impact,
            }
            for a in result.actions
        ],
        "next_investigations":   result.next_steps,
        "cited_past_incidents":  result.cited_past_ids,   # from similar_past
        "cited_runbooks":        result.cited_runbook_urls,
        "unable_to_diagnose":    result.unable,
        "reasons":               result.unable_reasons,
    }
```

### 9.2 Structured output — pick one

Your agent probably emits prose today. To fit the `ProposeResponse` shape:

**(a) Prompt for JSON directly.** Add to your agent's system prompt:

> "When you have a proposal, return ONLY the following JSON structure: `{...schema...}`. Do not include prose outside the JSON block."

Parse with `json.loads`. Handles ~95% of cases with a modern LLM. Fall back to (b) on parse failure.

**(b) Two-pass — reason, then extract.** After your agent finishes reasoning, make one small LLM call: "Given this analysis: `{analysis}`, extract into JSON matching this schema." Slightly more reliable, adds ~1s latency.

For Phase 2, ship (a). If proposals come back garbled, add (b) as a fallback layer.

### 9.3 Time budget

`constraints.max_thinking_ms` is your budget (default 30_000). The AIMS user sees a spinner during this call; over ~10s and the UX suffers. If your agent regularly needs longer, either:

- Cap tool calls inside your agent (`max_iterations=5`)
- Use response streaming (fastmcp supports upgrading the response to SSE mid-call — see fastmcp docs)

---

## 10. Best practices

### `handles` — tune to your actual scope

- **`severities`**: leave empty to accept all, or specify which you can genuinely help with. Empty means "all four (P1/P2/P3/P4) will be routed to me."
- **`alert_types`**: list only the alert types your agent can reason about. Common values today: `threshold_numeric`, `text_contains`, `calcite_formula`. Ask AIMS what values their sources actually emit.
- **`metric_patterns`**: **most important** — Python regexes tested against `incident.metadata.metric`. Include your service's metric namespace(s) so alerts from other systems never reach you (`^my_service\.`, `^myapp\.`). Empty means "any metric" — usually wrong.
- **`error_signatures`**: reserved for future use.

Every filter is AND-combined. A P4 alert with `alert_type=threshold_numeric` and `metric=other_svc.foo` misses on both the severity and metric filters and is skipped.

### `declared_actions` — be honest, be minimal

- List every action ID your agent might emit in `recommended_actions[].action_id`. Actions with IDs not in this list get dropped by the router with a warning.
- Mark `requires_approval: true` on anything with side effects — the agent-side UI shows a "Confirm" button before execution.
- `reversible: true` is aspirational metadata — the agent uses it as a hint when framing recommendations.
- Start with a small list. Adding new actions later is a re-registration.

### `read_only_default: true` — keep it

Even when your agent has write tools available internally, mark propose as read-only. Writes come via a separate `aims_execute_action` call (v2, not yet shipped). Setting `read_only_default: false` requires the operator to explicitly opt in at register time and unlocks NO new capability today.

### Confidence scoring

- `1.0`: I'm certain this is the exact same fix that worked last time.
- `0.7-0.9`: strong prior with mild variance.
- `0.4-0.6`: plausible hypothesis, needs verification.
- `0.1-0.3`: I have a guess but low certainty.
- `< 0.1` or `unable_to_diagnose: true`: I can't do better than "here's what I looked at."

AIMS UI downgrades framing below 0.3 ("possible cause" instead of "root cause is").

### Cite generously

Every past incident you reference goes in `cited_past_incidents` (as an incident ID). Every runbook you draw from goes in `cited_runbooks` (as a URL). These render in the AIMS UI as chips users can click. Citing helps humans trust your proposal AND feeds AIMS's flywheel — cited resolutions become higher-signal training data for future retrievals.

### Logging

- Log entry + exit on both tools with structured key=value: `propose_called incident_id=… corr=…` / `propose_completed corr=… actions=… unable=…`.
- Log the `correlation_id` from `constraints.correlation_id` — makes it easy to join your logs with AIMS's Langfuse trace.
- **Don't log full incident bodies** at INFO. IDs + counts + outcomes only. Bodies contain sensitive prod data.

### Response time

Target < 5s p95. If you're doing multi-tool agent reasoning, this is tight but achievable. If you need > 10s regularly, tell AIMS so they can bump their timeout — or start streaming.

---

## 11. Gotchas we've hit (learn from our pain)

### #1 — fastmcp version pin

`fastmcp` had two lineages:
- `<= 0.4.x` — the original Anthropic-owned package, **stdio + SSE only, no streamable-HTTP**
- `>= 2.x` — jlowin's fork that took over active development, adds streamable-HTTP

**Pin `fastmcp>=2.9.0,<4.0.0`**. If you pin the old range you get 0.4.1 and `AttributeError: 'FastMCP' object has no attribute 'streamable_http_app'` at import time.

### #2 — Sub-app lifespan doesn't run automatically

If you `FastAPI().mount("/mcp", mcp.streamable_http_app())` without propagating the lifespan, the MCP endpoint 500s on the first request with something like "task group not initialized." fastmcp needs its lifespan to run to initialize the session manager.

Fix (see §3):
```python
_mcp_asgi_app = mcp.streamable_http_app()      # call ONCE
app = FastAPI(lifespan=_mcp_asgi_app.router.lifespan_context)
app.mount("/mcp", _mcp_asgi_app)               # reuse the same instance
```

### #3 — Trailing slash on `/mcp`

fastmcp mounts at `/mcp/`. `POST /mcp` (no trailing slash) returns `307 Temporary Redirect` to `/mcp/`. Register with the trailing slash to avoid the round-trip.

### #4 — `Accept: application/json, text/event-stream` is required

Streamable-HTTP MCP servers reject requests without both content types in `Accept` — `406 Not Acceptable: Client must accept both application/json and text/event-stream`. The `mcp` SDK sends this correctly; if you're building a bespoke client (don't), you have to set it manually.

### #5 — CSI Secret Store mirror doesn't hot-refresh

When you add a new secret to the SPC, the CSI driver mounts it — but the mirrored K8s Secret (`secretObjects`) doesn't refresh in-place. Consumers reading from the mirrored Secret see stale data until the secret is deleted and re-synced:

```bash
kubectl -n <ns> delete secret <mirrored-secret-name>
kubectl -n <ns> rollout restart deployment/<pod>
```

**If your middleware reads the CSI mount directly** (`/mnt/secrets/<name>`, as in the §4 example) this isn't your problem. If you use `secretObjects`, factor this into your rotation runbook.

### #6 — Debugging 401 from your MCP server

If AIMS gets 401 back from your MCP:

1. Check the token in KV: `az keyvault secret show --vault-name forestrat-kv --name my-service-mcp-router-token --query value -o tsv | cut -c1-12`
2. Check the token in your CSI mount: `kubectl -n <ns> exec <pod> -- head -c 12 /mnt/secrets/my-service-mcp-router-token`
3. Check the token in aims-agent's CSI mount: `kubectl -n aims exec deploy/aims-agent -- head -c 12 /mnt/secrets/my-service-mcp-router-token`
4. All three prefixes must match. Mismatch = one side has a stale mount → CSI refresh procedure (§gotcha 5).

### #7 — Session-scoped chats reused across tool calls

The `mcp` SDK opens a new session per request by default (stateless). Don't try to correlate calls across MCP sessions server-side — treat each `aims_propose_resolution` invocation as independent. If you need cross-call state, key it on `constraints.correlation_id` (unique per Resolve-with-AI turn).

---

## 12. Updating an existing registration

To change the URL, auth, or capabilities of an already-registered server:

```bash
curl -X PATCH https://aims-az.forestrat.ai/api/mcp/apps/<application_id> \
  -H "X-API-Key: $MCP_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "endpoint_url": "http://new-svc/mcp/",
    "capabilities": {
      "handles": {"metric_patterns": ["^new_pattern\\."]},
      ...
    }
  }'
```

Every PATCH field is optional; omitted fields keep their old value. The router does NOT re-interview your MCP server — the `capabilities` you PATCH becomes the new snapshot.

**To fully deprecate a registration:**

```bash
curl -X DELETE https://aims-az.forestrat.ai/api/mcp/apps/<application_id> \
  -H "X-API-Key: $MCP_ADMIN_KEY"
```

Soft-delete → `status='deprecated'`. Row stays for audit trail.

---

## 13. Reference implementations

- **`reference-stub/`** in this repo — a working canned MCP server. ~150 LOC, full fastmcp setup, both tools implemented with stub responses. Deployed to the `mcp` namespace as `mcp-stub` for router smoke-testing. Copy this before writing from scratch.
- **`trading-ui-mcp`** (Forestrat internal — Trading UI's own MCP server). Currently Phase 1 — same shape as this guide's Phase 1, wiring for their real agent in progress. If you're on Trading UI's team, look at their implementation before rolling your own.

---

## Quick reference card

| Thing | Value |
|---|---|
| Framework | `fastmcp>=2.9.0,<4.0.0` |
| Transport | streamable-HTTP (`POST /mcp/`) |
| Required tools | `aims_discover_capabilities`, `aims_propose_resolution` |
| Auth | `Authorization: Bearer <token>`; token in KV |
| Registration endpoint | `POST https://aims-az.forestrat.ai/api/mcp/apps` |
| Registration UI | `https://aims-az.forestrat.ai/api/mcp/ui/` |
| Admin key location | `forestrat-kv/mcp-router-api-keys-admin` |
| Wire contract | [`contracts.md`](contracts.md) |
| Reference server | [`../reference-stub/app.py`](../reference-stub/app.py) |
| Router repo | [`Forestrat-Inc/mcp-router`](https://github.com/Forestrat-Inc/mcp-router) |
