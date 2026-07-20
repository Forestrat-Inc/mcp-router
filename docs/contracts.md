# MCP Router — contracts every registered MCP server must abide by

> **Audience.** Teams onboarding an application to AIMS "Resolve with AI"
> so that incidents routed to their `application_id` can be diagnosed +
> (optionally) remediated via their own MCP server.
>
> **Non-goals.** This doc doesn't cover the MCP protocol itself
> (transport, tool-list handshake, resource negotiation) — read the
> upstream Model Context Protocol spec for that. This doc is the
> **AIMS-side conventions layered on top** of vanilla MCP: the tool
> names AIMS calls, the payload shapes AIMS sends, the response shapes
> AIMS parses.

---

## 1. The 30-second mental model

```
  User clicks "Resolve with AI" on incident I in application A
      │
      ▼
  agent-service                           mcp-router                     application A's MCP server
  ┌──────────────────────┐    (1) GET   ┌──────────────────┐             ┌─────────────────────────┐
  │ context digest       │ ────────────►│ /apps/{A}        │             │ tools/list              │
  │  · incident I        │              │ → {endpoint,     │             │  aims_discover_…        │
  │  · similar past      │              │    auth_ref,     │             │  aims_propose_res…      │
  │    incidents from KG │              │    capabilities} │             │  aims_execute_action?   │
  │  · runbooks          │              └──────────────────┘             └─────────────────────────┘
  └──────────┬───────────┘                                                          ▲
             │                                                                      │
             │ (2) if capabilities match this incident, invoke                       │
             └──────────────────────────────────────────────────────────────────────┘
                     aims_propose_resolution({ incident, context, constraints })
             ◄──────────────────────────────────────────────────────────────────────
                     { confidence, analysis, recommended_actions[], ... }
                                                        │
                                                        ▼
                                       Merged into agent's LangGraph context
                                       LLM writes final resolution using both
                                       the KG-derived past incidents AND the
                                       application-owned MCP proposal.
```

The contract is small on purpose: **discovery + one required tool + one optional tool**. If a team wants to expose more (metrics readers, log tailers, whatever), they use standard MCP `tools/list` — the agent will surface those to the LLM naturally. But the two AIMS-specific tools below are the load-bearing bits AIMS itself calls deterministically.

---

## 2. The three tools

### 2.1 `aims_discover_capabilities` (REQUIRED)

A cheap, side-effect-free introspection call. Answers: **should the router route this incident here at all?**

**Input:** empty object `{}`.

**Output** (JSON, all fields required unless marked optional):

```json
{
  "protocol_version": "1",
  "server_name": "trading-ui-resolver",
  "server_version": "0.4.1",
  "handles": {
    "severities": ["P1", "P2", "P3"],
    "alert_types": ["calcite_formula", "text_contains", "threshold_numeric"],
    "metric_patterns": ["order_.*", "fill_.*", "^unfilled_qty"],
    "error_signatures": ["^GRPC_UNAVAILABLE", "connection reset"]
  },
  "declared_actions": [
    { "id": "restart-order-worker", "reversible": true, "requires_approval": true },
    { "id": "flush-recon-cache",    "reversible": true, "requires_approval": false }
  ],
  "max_response_ms": 20000,
  "read_only_default": true
}
```

Field notes:

- **`protocol_version`**: string `"1"` today. Rev this when the payload shape breaks — AIMS refuses to invoke a server whose `protocol_version` it doesn't recognize.
- **`handles.*`**: all four are OPTIONAL filters. If a server declares `severities: ["P1", "P2"]`, AIMS won't invoke it for a P4 incident even if `application_id` matches. Empty/missing = "I'll take anything for this app." Regex arrays use Python `re.search`.
- **`declared_actions[]`**: only relevant if the server implements `aims_execute_action`. Otherwise omit.
- **`max_response_ms`**: server's own SLA. AIMS will use this as its call timeout, capped at 60000 ms.
- **`read_only_default: true`** means the server promises `aims_propose_resolution` alone never causes side effects. Setting it to `false` requires the AIMS operator to opt in when registering the server (see §7).

The router **caches this response for 5 minutes** per `application_id`. Servers that need to hot-reload their capabilities should call `POST https://aims-az.forestrat.ai/api/mcp/admin/invalidate/{application_id}` after any change.

---

### 2.2 `aims_propose_resolution` (REQUIRED)

The main workhorse. AIMS invokes this once per Resolve-with-AI click.

**Input:**

```json
{
  "incident": {
    "id": "66c7ca95-1077-4e16-b309-4553082a7ca1",
    "external_id": "rule-engine:2e7141c1-...:smoke-1784122274",
    "title": "[trading-ui] Unfilled orders spiked",
    "description": "Alert: Unfilled orders spiked ...",
    "severity": "P2",
    "status": "OPEN",
    "application_id": "59de013d-8cdf-44e0-89a1-dfad8c325d39",
    "instance_id": "4c320348-0428-4d02-a93e-8c2677e4b9cc",
    "created_at": "2026-07-15T13:31:24.937762Z",
    "metadata": {
      "alert_type": "calcite_formula",
      "metric": "order_qty - filled",
      "operator": ">",
      "threshold": 500.0,
      "threshold_text": "",
      "matching_row_count": 27,
      "account_id": "ACCT-4711",
      "condition_text": "SUM(order_qty - filled) > 500"
    }
  },
  "context": {
    "similar_past_incidents": [
      {
        "id": "prior-uuid-1",
        "title": "Unfilled orders spiked on ACCT-4711",
        "similarity_score": 0.91,
        "resolved_at": "2026-05-12T09:14:00Z",
        "resolved_by": "kaushal@forestrat.ai",
        "resolution_notes": "Restarted order-router pod-3; downstream fill feed was stuck on stale connection. Fix: alerted on connection age → auto-recycle >5m.",
        "resolution_id": "resolution-uuid-1"
      },
      { "...": "up to 5, sorted DESC by similarity_score" }
    ],
    "runbooks": [
      {
        "title": "Order reconciliation triage",
        "source": "confluence",
        "url": "https://forestrat.atlassian.net/wiki/spaces/TO/pages/12345",
        "excerpt": "First check ORDER_ROUTER_HEARTBEAT metric ..."
      }
    ],
    "recent_deploys": [ "OPTIONAL: only if AIMS knows about them" ]
  },
  "constraints": {
    "read_only": true,
    "must_confirm_before_action": true,
    "max_thinking_ms": 30000,
    "correlation_id": "agent-turn-uuid-…"
  }
}
```

Field notes:

- **`incident.metadata`** is the source-registered event payload verbatim (from rule-engine's field_schema). Servers should reach for their own domain-specific fields there.
- **`context.similar_past_incidents`** is populated from the ontology graph via `search_similar` (multi-vector KNN over symptom + resolution embeddings). Top 5 by `similarity_score`. Empty array if the graph has nothing similar — always treat empty as "no signal", not "error".
- **`context.runbooks`** are Confluence chunks retrieved via ontology's hybrid search using the incident title as the query. Empty array is fine.
- **`constraints.correlation_id`** appears in AIMS logs and Langfuse traces. Echo it in server-side logs for cross-service debugging.

**Output:**

```json
{
  "confidence": 0.72,
  "analysis": "The unfilled orders spike on ACCT-4711 matches the May 12 incident signature (91% similarity). That incident was resolved by restarting order-router pod-3 after it stuck on a stale downstream fill connection. Current metric `order_qty - filled = 27 rows > 500 threshold` is consistent with a stalled worker rather than a genuine order backlog.",
  "recommended_actions": [
    {
      "kind": "diagnostic",
      "description": "Check order-router pod-3's ORDER_ROUTER_HEARTBEAT age. If > 5 minutes, worker is stuck.",
      "reversible": true,
      "requires_approval": false,
      "action_id": null
    },
    {
      "kind": "remediation",
      "description": "Restart order-router pod-3 to recycle the stale fill connection.",
      "reversible": true,
      "requires_approval": true,
      "action_id": "restart-order-worker",
      "action_args": { "pod": "order-router-pod-3" },
      "estimated_impact": "5-second order routing pause, no data loss"
    }
  ],
  "next_investigations": [
    "Compare current fill-feed connection age vs healthy baseline",
    "Check whether ACCT-4711 has other stuck orders across other services"
  ],
  "cited_past_incidents": ["prior-uuid-1"],
  "cited_runbooks": ["https://forestrat.atlassian.net/wiki/spaces/TO/pages/12345"],
  "unable_to_diagnose": false
}
```

Or when the server has nothing useful:

```json
{
  "confidence": 0.0,
  "analysis": "This incident doesn't match any known trading-ui alert signature.",
  "recommended_actions": [],
  "next_investigations": [],
  "cited_past_incidents": [],
  "cited_runbooks": [],
  "unable_to_diagnose": true,
  "reasons": ["no matching alert_type", "no similar past incidents in server's own history"]
}
```

Field notes:

- **`confidence` in [0.0, 1.0]**. 0.7+ is "I'm pretty sure", 0.3 is "here's a guess, verify". The agent uses this to decide how prominently to surface the proposal to the user.
- **`recommended_actions[].kind`**: one of `diagnostic | remediation | escalation | information`. Diagnostics are always safe to run; remediation requires user approval (see §5); escalation is "page a human"; information is context/observation with no action.
- **`recommended_actions[].reversible`** is a HARD claim from the server. Non-reversible actions require operator confirmation even if the agent auto-approves; today AIMS never auto-approves anything (see §5).
- **`recommended_actions[].action_id`** must match an entry in the server's `declared_actions[]` from discovery. `null` means "this is a description only, not something you can ask me to execute later."
- **`cited_past_incidents[]` / `cited_runbooks[]`** are provenance so the agent can render "the MCP server thinks this because of X" in the UI.
- **`unable_to_diagnose: true`** is a **first-class response**, not an error. Servers MUST return this shape rather than raising when they have nothing useful. It lets AIMS say "the application-specific resolver didn't recognize this — falling back to generic AI reasoning" gracefully.

---

### 2.3 `aims_execute_action` (OPTIONAL)

Only for servers that can do more than recommend — actually restart pods, flush caches, page an on-call rotation, etc.

**Input:**

```json
{
  "incident_id": "66c7ca95-1077-4e16-b309-4553082a7ca1",
  "action_id": "restart-order-worker",
  "action_args": { "pod": "order-router-pod-3" },
  "approval": {
    "confirmed_by": "kaushal@forestrat.ai",
    "confirmed_at": "2026-07-16T12:34:56Z",
    "correlation_id": "agent-turn-uuid-…",
    "idempotency_key": "restart-order-worker:pod-3:2026-07-16T12:34"
  }
}
```

**Output:**

```json
{
  "status": "executed | in_progress | rejected",
  "evidence": {
    "logs_url": "https://…",
    "before_snapshot": { "any": "diagnostic evidence" },
    "after_snapshot": { "any": "post-action evidence" },
    "duration_ms": 4823
  },
  "next_recommended_check": "Verify ORDER_ROUTER_HEARTBEAT age < 30s within 60 seconds.",
  "rollback_available": true,
  "rollback_hint": "Contact ops if pod won't come back — no automated rollback for this action."
}
```

Field notes:

- **`approval.confirmed_by`** is the JWT `sub` of the human who clicked the confirm button in AIMS. Servers should trust this — the auth layer already verified the token.
- **`approval.idempotency_key`** MUST be honored — a duplicate call with the same key returns the original response, doesn't re-execute.
- **`status: "in_progress"`** is allowed for async actions. The agent polls via a follow-up `aims_execute_action_status(idempotency_key)` (not yet spec'd — deferred to v2).
- **`status: "rejected"`** means the server refused (e.g. approval token wasn't for this action). Include a `reason` field.
- Servers implementing this tool MUST guarantee actions are additive-and-loggable — if you execute, you leave a trail (see §6).

---

## 3. Discovery + registration flow

Team X wants their application to be resolvable by AIMS:

1. Team X **builds their MCP server** implementing the three tools above. Deployed anywhere reachable (their own cluster, ours, external).
2. Team X **registers** with the AIMS mcp-router (ADMIN-gated):

```bash
POST https://aims-az.forestrat.ai/api/mcp/apps
Authorization: Bearer $AIMS_ADMIN_TOKEN
Content-Type: application/json

{
  "application_id": "59de013d-8cdf-44e0-89a1-dfad8c325d39",
  "name": "trading-ui-resolver",
  "transport": "http",
  "endpoint_url": "https://mcp-trading.internal.forestrat.ai/mcp",
  "auth_type": "bearer",
  "auth_ref": "kv://forestrat-kv/trading-ui-mcp-token",
  "owner_email": "trading-ui-team@forestrat.ai",
  "status": "active"
}
```

3. The router **calls `aims_discover_capabilities` immediately** at registration time. If it fails or returns an unrecognized `protocol_version`, the registration is `400`d — the record is not persisted.
4. The router **caches** the discovery response (5-min TTL) and stores a snapshot in `mcp_server.capabilities_snapshot` for auditability. Refetch is transparent.

Registration is per-application, not per-instance. If Trading UI dev/staging/prod need different MCP servers, use environment-specific `application_id`s (which you'd get by creating separate applications in the AIMS registry per env).

---

## 4. Transport

**Streamable-HTTP MCP is the only supported transport.** This is the modern MCP network transport (spec revision `2025-03-26`). The router uses the official Python MCP client SDK (`mcp>=1.2.0`) — servers should be built with `fastmcp`, the reference `mcp-python` server, the TypeScript `@modelcontextprotocol/sdk`, or any other streamable-HTTP MCP implementation.

Not stdio (subprocess only; wrong network model). Not classic SSE (deprecated by the spec; two-endpoint dance + persistent connection fights AKS ingress). Not WebSocket (never in the MCP spec).

Streamable-HTTP means:

- **Single endpoint** the client POSTs to (e.g. `http://your-svc.your-ns.svc.cluster.local:PORT/mcp/`).
- **Every request declares** `Accept: application/json, text/event-stream` — the server may respond with a plain JSON body OR upgrade the response to an SSE stream. The client library handles both transparently.
- **Session lifecycle**: the client sends `initialize` first, receives a session id in the `Mcp-Session-Id` response header, and echoes that header on all subsequent requests in the session. The SDK handles this — servers don't do bookkeeping.
- **Tool call wire format**:

```json
{
  "jsonrpc": "2.0",
  "id": "<uuid>",
  "method": "tools/call",
  "params": {
    "name": "aims_propose_resolution",
    "arguments": { ...see §2.2... }
  }
}
```

But **do not build this by hand** — use an MCP SDK. Bespoke `httpx.post` against `/tools/call` skips the initialize handshake and misses the Accept/session headers; spec-compliant servers reject it with `406 Not Acceptable` or `400 Missing session ID`.

Servers may also implement `tools/list` to advertise beyond the required three tools — the agent will surface those tools to the LLM in "MCP-enriched" mode.

**Registration `endpoint_url`**: use the URL the SDK will POST to. For fastmcp mounted at `/mcp/`, register `http://your-svc:PORT/mcp/` — with the trailing slash. Bare `/mcp` responds `307 Temporary Redirect` to `/mcp/`; the SDK follows redirects, but avoiding the round-trip is cheap.

---

## 5. Approval + auto-execution

**Today: AIMS never auto-executes any `aims_execute_action`.** Every remediation requires a human confirm click in the agent UI. The `requires_approval: false` flag in `declared_actions[]` is a HINT — the agent lowers friction ("just click yes") but the click is still required.

We may later add per-application auto-approval policies (e.g. "diagnostic actions with `reversible: true` may auto-run"), but the wire format is designed so that day-one every action goes through a human. Servers that don't want to implement approval flows can simply not implement `aims_execute_action` at all — they'll be purely advisory. **This is expected for most applications.**

---

## 6. Auth

- **AIMS → MCP server**: AIMS passes the token retrieved from KV via `auth_ref`. Auth header is `Authorization: Bearer <token>`. Rotation is on the team's side — rotate the KV secret, no AIMS restart needed (SPC picks up on next call... though see the CSI-secret-refresh gotcha in the AKS operator runbook (whichever runbook lives in the deployment repo — Forestrat AIMS has one in `docs/azure/architecture.md § 11`), may need a router pod restart in practice until CSI Secret Store's mirror-refresh limitation is worked around).
- **MCP server → AIMS callbacks**: none in v1. The MCP server does not make outbound calls to AIMS. If a server needs to look up more incident context, that's a v2 feature (`aims_fetch_incident_context(id)`).
- **JWT `sub` propagation on `aims_execute_action`**: the user who clicked the confirm button appears in `approval.confirmed_by`. Servers should log this for audit trail.

---

## 7. Registration semantics — `read_only_default`

- Servers declaring `read_only_default: true` (the default) can be registered by any AIMS ADMIN without extra ceremony.
- Servers declaring `read_only_default: false` require the registration payload to include `"i_accept_write_capable_server": true` — a small ergonomic guardrail so "write-capable" doesn't slip in unnoticed. Documented as a footgun in the API reference.

---

## 8. Error handling

Servers MUST return a valid JSON response for every AIMS call, even on failure. The only acceptable non-200 status is:
- **500** — server has crashed (AIMS caches the failure for 30s and skips this app on subsequent Resolve clicks until the cache expires)

Every other error state is expressed via the response body:
- Cannot diagnose → `unable_to_diagnose: true`
- Rate-limited → `unable_to_diagnose: true, reasons: ["rate_limited", "retry_after": 30]`
- Auth invalid → HTTP 401 (mcp-router logs and marks the server as `status = disabled` after 5 consecutive 401s over 5 minutes)

Do NOT throw uncaught exceptions. Do NOT return `null` for required fields. AIMS parses response strictly and will treat malformed responses as `unable_to_diagnose: true` with a warning logged.

---

## 9. What the agent does with the response

Simplified from `agent-service`:

```
1. GET /api/mcp/apps/{incident.application_id}
   → 404: skip MCP entirely, fall through to generic AI reasoning
   → 200: proceed
2. Check server.capabilities.handles.severities includes incident.severity
   → no: skip MCP, fall through
   → yes: proceed
3. Invoke aims_propose_resolution with incident + context.similar_past_incidents + context.runbooks
   → any HTTP/parse failure: log, skip MCP, fall through
   → unable_to_diagnose=true: mention in agent response but proceed with generic AI
   → confidence < 0.3: include as a low-signal hint
   → confidence >= 0.3: promote to top-of-response, cite the past incidents + runbooks the MCP cited
4. Render the LLM final response with the MCP proposal woven in via a system-message injection.
5. If any recommended_action.requires_approval and user clicks the confirm button:
   → aims_execute_action(action_id, approval) → surface the execution outcome in the chat.
```

The **agent-service change is additive**. Existing "Resolve with AI" continues to work when the router returns 404 (no MCP registered for the app), server times out, or the discovery capabilities don't match the incident.

---

## 10. What team X SHOULDN'T put in their MCP server

- **PII / secrets in `analysis` text.** The `analysis` string lands in the AIMS incident description, visible to everyone who can read the incident.
- **Free-form SQL / arbitrary code in `recommended_actions[].description`.** Descriptions are natural-language for humans; anything programmatic goes in `action_args` behind an `action_id`.
- **Rely on `context.similar_past_incidents` being non-empty.** New AIMS installs, fresh applications, or narrow-similarity incidents will hand you `[]`. Handle it.
- **Reach back into AIMS state directly.** Everything you need should be in the request; if you need more, add fields to the source's field_schema in rule-engine so it arrives in `incident.metadata`.

---

## 11. Versioning

- `protocol_version` is on both discovery output AND the JSON-RPC method payload. Bump when payload shape breaks.
- AIMS supports **only the current `protocol_version` and one previous** at any time. Deprecations get 60 days of dual-support then the old version is refused.
- Additive fields (new optional fields in inputs or outputs) don't require a version bump — servers that ignore unknown fields are forward-compatible.

---

## 12. Reference implementations

- **Stub server** (test-only): `backend/aims-mcp-router/tests/stub_server/` — 80 lines of FastAPI that answers discovery + returns a canned `aims_propose_resolution` response echoing the input. Used in e2e smoke tests.
- **Trading UI resolver**: not yet built — trading-ui-team target for phase 2.
- **AIMS's own generic MCP server**: not built — could be a v3 "if no app-specific server is registered, use this internal one as a fallback."

---

## 13. Open decisions before this ships to the first team

1. **Do we want a shared "AIMS-hosted generic" MCP server** for teams that don't want to run their own but do want structured resolution based on their incidents' history? Adds ops surface but lowers the bar to onboarding.
2. **Should `aims_execute_action` be OPT-IN per operator** (each user configures "yes I want approval-based execution" in AIMS settings) or per-application (server declares it once, enabled for everyone)? Design assumes the latter.
3. **`unable_to_diagnose` telemetry** — should we count these in a metric so we can surface "the trading-ui MCP hasn't been able to diagnose 40% of incidents this week — worth checking whether its capabilities are stale"? Cheap to add, not in v1 scope.
