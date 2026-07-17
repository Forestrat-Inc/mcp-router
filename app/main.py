"""mcp-router entrypoint."""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.admin import router as admin_router
from app.api.apps import router as apps_router
from app.config import settings
from app.db import session_scope
from app.logging_config import configure_logging

configure_logging(service_name="mcp-router")
logger = logging.getLogger(__name__)


app = FastAPI(
    title="mcp-router",
    description=(
        "application_id → MCP server registry. Discovery-validated at "
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

# API routers. Mounted at the same path the ingress passes through — NGINX
# ingress with pathType Prefix forwards the FULL path.
app.include_router(apps_router, prefix="/api/mcp/apps", tags=["apps"])
app.include_router(admin_router, prefix="/api/mcp/admin", tags=["admin"])


# ── Config UI ──────────────────────────────────────────────────────
# Single-page HTML/CSS/JS. No build step. Static assets shipped in the
# Docker image under /app/ui/. Served under /api/mcp/ui/ so the ingress
# (which routes /api/mcp) covers it without a new rule. The UI itself is
# unauthenticated (so the sign-in screen can render); every API call it
# makes is guarded by X-API-Key at the router-side.
_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if _UI_DIR.is_dir():
    app.mount("/api/mcp/ui", StaticFiles(directory=_UI_DIR, html=True), name="ui")

    # Convenience: /api/mcp/ui → 308 to /api/mcp/ui/ so the browser lands
    # on index.html when a user drops the trailing slash.
    @app.get("/api/mcp/ui", include_in_schema=False)
    def _ui_redirect() -> RedirectResponse:
        return RedirectResponse(url="/api/mcp/ui/", status_code=308)
else:
    logger.warning("ui directory %s missing — /api/mcp/ui will 404", _UI_DIR)


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
#                                   liveness / readiness on the pod IP.
#   /api/mcp/health, /api/mcp/ready — reachable through the public ingress
#                                     for external monitoring or smokes.
for path in ("/", "/health", "/ready", "/api/mcp", "/api/mcp/", "/api/mcp/health", "/api/mcp/ready"):
    tag = "ops"
    if path in ("/", "/api/mcp", "/api/mcp/"):
        app.add_api_route(path, lambda: {"service": "mcp-router"}, tags=[tag])
    else:
        app.add_api_route(path, _health_impl, tags=[tag])
