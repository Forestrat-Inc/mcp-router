"""Pydantic-settings config for mcp-router.

Standalone service — no coupling to any particular caller. Auth is
configurable to fit any deployment story: JWT (with the router's OWN
secret, not the caller's), API keys (each mapped to a role), or both.

Env-var convention: bare names, no service prefix. Deployments that
need to disambiguate (e.g. sharing an env file across services) prefix
at the deployment layer via K8s envFrom.
"""

from typing import Dict, List, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    # ── Runtime
    LOG_LEVEL: str = "INFO"
    PORT: int = 8003

    # ── Postgres
    DATABASE_URL: str = "postgresql://mcp:mcp@localhost:5432/mcp"
    DB_SCHEMA: str = "mcp_router"

    # ── Redis (cache)
    REDIS_URL: str = "redis://localhost:6379/3"
    REDIS_KEY_PREFIX: str = "mcp"
    CACHE_TTL_SECONDS: int = 300

    # ── Auth ──────────────────────────────────────────────────────────
    # `none`    — allow anonymous (dev only; the router refuses to boot
    #             in this mode unless AUTH_ALLOW_NONE_IN_PROD=true too)
    # `jwt`     — accept `Authorization: Bearer <hmac-jwt>` signed with
    #             JWT_SECRET
    # `api_key` — accept `X-API-Key: <key>` where the key is one of
    #             API_KEYS_ADMIN or API_KEYS_READER
    # `both`    — accept either — first match wins
    AUTH_MODE: Literal["none", "jwt", "api_key", "both"] = "none"

    # Router's OWN secret. NOT shared with any caller's identity system.
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"

    # API keys — comma-separated. Each key becomes a Principal with the
    # corresponding role. Rotate by adding the new value first (comma-
    # separated), rolling callers, then dropping the old one.
    API_KEYS_ADMIN: str = ""
    API_KEYS_READER: str = ""

    # Explicit escape hatch. Set to `true` alongside AUTH_MODE=none to
    # allow anonymous access in a hardened env (behind a Zero Trust
    # network layer that already authenticates at the edge). Router logs
    # a WARNING on every request in this mode so it's not silent drift.
    AUTH_ALLOW_NONE_IN_PROD: bool = False

    # ── MCP-server call timeouts
    DISCOVERY_TIMEOUT_MS: int = 10000
    RESOLUTION_CALL_TIMEOUT_MS: int = 30000
    HTTP_MAX_RETRIES: int = 0  # no automatic retries — server outages should surface

    # ── CORS
    CORS_ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # ── KV URI shape check on registered auth_ref values
    KV_URI_PREFIX: str = "kv://"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def admin_keys(self) -> List[str]:
        return [k.strip() for k in self.API_KEYS_ADMIN.split(",") if k.strip()]

    @property
    def reader_keys(self) -> List[str]:
        return [k.strip() for k in self.API_KEYS_READER.split(",") if k.strip()]

    @property
    def api_key_index(self) -> Dict[str, str]:
        """Map every configured key → its role. Admin wins on collision."""
        idx: Dict[str, str] = {}
        for k in self.reader_keys:
            idx[k] = "reader"
        for k in self.admin_keys:
            idx[k] = "admin"
        return idx


settings = Settings()
