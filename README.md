# mcp-router

> Standalone `application_id → MCP server` **service-discovery / KV registry**.
> Consumers register MCP servers with an application id, look them up, then
> call the target MCP server themselves via the streamable-HTTP `mcp` SDK.
> The router does NOT proxy tool calls — it's a pure CRUD/lookup service.
>
> This is its own repo. Own Dockerfile, own Alembic chain, own K8s
> manifests, own CI workflow, own dependency file. AIMS is one caller
> among potentially many.

## What it does

- Stores a `mcp_server` record per `application_id` (Postgres, schema `mcp_router`).
- Serves `GET /api/mcp/apps/{application_id}` for consumers to resolve the
  registered `endpoint_url` + `auth_type` + `auth_ref` + capabilities snapshot.
- Cache-aside on Redis with a 5-min TTL for the read path.
- Audit shadow table (`mcp_server_history`) captures every INSERT /
  UPDATE / DELETE with before + after JSON snapshots.
- **Never talks to a target MCP server itself.** Consumers do that.

Contract every registered MCP server must implement (target-server side):
[`docs/contracts.md`](docs/contracts.md).

## Auth (router-owned, not tied to any caller's identity system)

`AUTH_MODE` env selects the mechanism(s) the router accepts:

| Mode | Header | Notes |
|---|---|---|
| `jwt` | `Authorization: Bearer <hmac-jwt>` | Signed with the router's own `JWT_SECRET`. `role` or `roles` claim controls admin vs reader. |
| `api_key` | `X-API-Key: <key>` (or `Authorization: ApiKey <key>`) | Keys come from `API_KEYS_ADMIN` / `API_KEYS_READER` comma-separated lists. Rotate by adding the new key, rolling callers, then dropping the old. |
| `both` | either | First-match wins. Recommended for production so callers pick per integration style. |
| `none` | — | Anonymous, dev-only. Router logs a WARNING per request; refuses to serve `none` in a prod-looking env unless `AUTH_ALLOW_NONE_IN_PROD=true` is set explicitly. |

Every write endpoint requires the `admin` role. Reader-role callers
get the lookup + list endpoints, `404` on nonexistent-or-disabled
apps (never `403`, to avoid existence-leak).

## Endpoints

| Method | Path | Auth |
|---|---|---|
| `GET`    | `/api/mcp/apps`                                 | reader |
| `GET`    | `/api/mcp/apps/{application_id}`                | reader |
| `POST`   | `/api/mcp/apps`                                 | admin  |
| `PATCH`  | `/api/mcp/apps/{application_id}`                | admin  |
| `DELETE` | `/api/mcp/apps/{application_id}`                | admin (soft-delete → status='deprecated') |
| `POST`   | `/api/mcp/admin/invalidate/{application_id}`    | admin |
| `GET`    | `/api/mcp/admin/history/{application_id}`       | admin |
| `GET`    | `/health`, `/ready`                             | none  |

Response codes for `GET /apps/{id}`:
- **200** — record returned (endpoint, auth, capabilities snapshot)
- **404** — no active server registered for this `application_id`

Consumers of the API (aims-agent today) map 404 / transport failures to a
fail-soft skip, so their feature (Resolve-with-AI) never breaks because
the router or target server had a bad day. The actual MCP call —
`initialize` + `session.call_tool("aims_propose_resolution", ...)` —
happens inside the consumer using the streamable-HTTP `mcp` SDK against
the target's `endpoint_url`, with a bearer/api_key resolved from a
CSI-mounted KV secret named by `auth_ref`.

## Local dev

```bash
cd mcp-router
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # fill in DATABASE_URL and AUTH_MODE
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --port 8003 --reload
```

## Deploy (AKS)

- Workflow: [`.github/workflows/deploy-mcp-router.yml`](../.github/workflows/deploy-mcp-router.yml) — gated on `#mcp` in the commit message or manual dispatch. Builds both `router` and `reference-stub` matrix entries; independent of AIMS's deploy workflow.
- Manifests: [`k8s/`](k8s/) — Deployment, Service, SecretProviderClass. The reference stub has its own [`reference-stub/k8s/`](reference-stub/k8s/) — no secrets, no SPC.
- KV secrets (prefixed `mcp-router-*` for future extraction):
  - `mcp-router-postgres-dsn`, `mcp-router-redis-url`
  - `mcp-router-jwt-secret`
  - `mcp-router-api-keys-admin`, `mcp-router-api-keys-reader`
- On AKS today the pod names are `mcp-router` and `mcp-stub`. Same namespace as AIMS (`aims`) for now — will move to its own namespace when the service is lifted out of the repo.

## Reference implementation

`reference-stub/` is a 130-line FastAPI app implementing the contract with canned responses. Register it against any application_id to smoke-test the loop:

```bash
curl -X POST https://<router-host>/api/mcp/apps \
  -H "X-API-Key: <admin-key>" -H "Content-Type: application/json" \
  -d '{
    "application_id": "<uuid>",
    "name": "mcp-stub",
    "transport": "http",
    "endpoint_url": "http://mcp-stub.<ns>.svc.cluster.local:9000/mcp",
    "auth_type": "none"
  }'
```

Toggle `incident.metadata.stub_response = "unable"` in the proposal request to exercise the `unable_to_diagnose=true` path.

## Config UI

The router ships with a single-page admin UI at **`/api/mcp/ui/`** — vanilla
HTML/CSS/JS, no build step, no separate pod. Users sign in with an
`X-API-Key` (kept only in the browser's `sessionStorage`) and can:

- List every registered MCP server, filter by name or status.
- Register a new server (client-side UUID + URL validation; the router's
  discovery call still gates persistence server-side).
- Edit endpoint, auth, status, owner.
- View the full capabilities snapshot + audit history for one server.
- Force a cache invalidate.
- Soft-delete (deprecate) a server.

Public path via the standard ingress: `https://<router-host>/api/mcp/ui/`.

## Extraction from this repo (future)

When it's time to move to its own repo:

```bash
git subtree split --prefix=mcp-router --branch=mcp-router-only
```

Files that need port after the split:

- `.github/workflows/deploy-mcp-router.yml` (currently sits at repo root — move it into the new repo's `.github/workflows/`).
- `docs/mcp-router/contracts.md` (the contract spec — move it into the new repo, keep an authoritative pointer here).

Everything else (Dockerfile, alembic, app/, k8s/, reference-stub/) is already self-contained in this directory.

## Not yet built

- Automatic re-discovery on TTL — capabilities snapshot only refreshes on explicit PATCH today
- `aims_execute_action` end-to-end forwarding — the shape is spec'd but the router forwarding + agent UI plumbing land in a follow-up
- Rate limiting per-caller — belongs on the ingress, not the router
- Multi-region — v1 is single-region. See [`../docs/Implementation-plans/06-mcp-router.md`](../docs/Implementation-plans/06-mcp-router.md) for the phase-2/3 shapes
