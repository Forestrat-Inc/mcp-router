# mcp-router

> Standalone `application_id → MCP server` registry. Not tied to any
> particular caller. Any system with authenticated clients can register
> MCP servers, look them up, and delegate `aims_propose_resolution` calls
> through the router so the target server's auth stays server-side.
>
> This directory is **self-contained**. It has its own Dockerfile,
> Alembic chain, K8s manifests, CI workflow, and dependency file. It
> currently lives in the AIMS monorepo alongside the AIMS agent (the
> first caller), but it is deliberately built to `git subtree split`
> into its own repo whenever the operator wants.

## What it does

- Stores a `mcp_server` record per `application_id` (Postgres, schema `mcp_router`).
- Validates registrations by calling the target server's
  `aims_discover_capabilities` synchronously — unreachable / bad
  `protocol_version` / write-capable-not-acknowledged all refuse the
  registration with `400` (never persist a broken record).
- Serves `POST /api/mcp/apps/{application_id}/propose` — the agent calls
  this once per resolve, router forwards to the target server's
  `tools/call aims_propose_resolution` with server-side-resolved auth
  (Key Vault, per registration).
- Cache-aside on Redis with a 5-min TTL.
- Audit shadow table (`mcp_server_history`) captures every INSERT /
  UPDATE / DELETE with before + after JSON snapshots.

Contract every registered MCP server must implement:
[`../docs/mcp-router/contracts.md`](../docs/mcp-router/contracts.md).

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
| `POST`   | `/api/mcp/apps/{application_id}/propose`        | reader — forwards to the target MCP server |
| `POST`   | `/api/mcp/admin/invalidate/{application_id}`    | admin |
| `GET`    | `/api/mcp/admin/history/{application_id}`       | admin |
| `GET`    | `/health`, `/ready`                             | none  |

Response codes for `/propose`:
- **200** — proposal returned by the target server
- **204** — server exists but its `handles` (declared capabilities) don't match this incident
- **404** — no active server registered for this `application_id`
- **502** — target server errored or returned an unparseable payload

The AIMS agent maps every non-200 to a fail-soft `None`, so
Resolve-with-AI never breaks because the router or target server had a
bad day.

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
