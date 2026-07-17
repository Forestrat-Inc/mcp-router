"""mcp-router entrypoint."""

import logging

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.admin import router as admin_router
from app.api.apps import router as apps_router
from app.config import settings
from app.db import session_scope
from app.logging_config import configure_logging

configure_logging(service_name="mcp-router")
logger = logging.getLogger(__name__)


app = FastAPI(
    title="AIMS MCP Router",
    description=(
        "application_id → MCP server lookup. Discovery-validated at "
        "registration; cache-aside via Redis; source of truth in Postgres."
    ),
    version="0.1.0",
    redirect_slashes=False,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers mount at the same path the ingress passes through. NGINX ingress
# path `/api/mcp` (pathType: Prefix) forwards the FULL path — so what the
# app sees is `/api/mcp/apps`, `/api/mcp/admin/...`, etc. Same convention
# as aims-rule-engine (which serves /api/sources, /api/rules directly).
app.include_router(apps_router, prefix="/api/mcp/apps", tags=["apps"])
app.include_router(admin_router, prefix="/api/mcp/admin", tags=["admin"])


async def _health_impl() -> dict:
    """Verifies DB connectivity."""
    try:
        with session_scope() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.exception("health check DB error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"DB unreachable: {type(exc).__name__}",
        )
    return {"status": "healthy", "service": "mcp-router"}


# Two mount points on purpose:
#   /health, /ready               — hit by the kubelet startupProbe /
#                                   liveness / readiness on the pod IP
#                                   directly, no ingress in the path.
#   /api/mcp/health, /api/mcp/ready — reachable through the public ingress
#                                     for external monitoring or curl smokes.
for path in ("/", "/health", "/ready", "/api/mcp", "/api/mcp/", "/api/mcp/health", "/api/mcp/ready"):
    tag = "ops"
    if path in ("/", "/api/mcp", "/api/mcp/"):
        app.add_api_route(path, lambda: {"service": "mcp-router"}, tags=[tag])
    else:
        app.add_api_route(path, _health_impl, tags=[tag])
