"""Router-owned auth. Standalone service — no coupling to any caller's
identity system.

Two mechanisms, configurable via ``AUTH_MODE``:

* ``jwt`` — validate ``Authorization: Bearer <hmac-jwt>`` against the
  router's OWN ``JWT_SECRET``. Callers get a JWT however they like
  (their own auth service, a static long-lived token, whatever); the
  router only cares that the signature validates. Claims we read:
  ``sub``, ``exp``, and either ``role`` or ``roles`` for the role check.
* ``api_key`` — validate ``X-API-Key: <key>`` against the
  ``API_KEYS_ADMIN`` and ``API_KEYS_READER`` env lists. Each key is
  mapped to a role at boot. This is the intended mode for infra
  automations that don't want to run a JWT issuer.

``both`` accepts either; ``none`` allows anonymous (dev only unless
``AUTH_ALLOW_NONE_IN_PROD=true`` is set, in which case the router logs
a warning per request).
"""

import logging
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)


class Principal:
    """Everything we need from the authenticated caller — no more, no less."""

    def __init__(self, sub: str, role: str, source: str):
        self.sub = sub
        self.role = role
        self.source = source  # 'jwt' | 'api_key' | 'anonymous'

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def roles(self) -> list[str]:
        # Back-compat alias for any code that expects a roles list.
        return [self.role]


def _anonymous_principal() -> Principal:
    return Principal(sub="anonymous", role="admin", source="anonymous")


def _principal_from_jwt(token: str) -> Optional[Principal]:
    if not settings.JWT_SECRET:
        return None
    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.PyJWTError as exc:
        logger.warning("jwt decode failed: %s", exc)
        return None
    # Accept either single-value `role` or list-shaped `roles`. Everything
    # falls back to `reader` if unspecified.
    single = claims.get("role")
    plural = claims.get("roles")
    if single:
        role = "admin" if str(single).lower() == "admin" else "reader"
    elif isinstance(plural, list):
        role = "admin" if any(str(r).lower() == "admin" for r in plural) else "reader"
    elif isinstance(plural, str):
        role = "admin" if plural.lower() == "admin" else "reader"
    else:
        role = "reader"
    return Principal(sub=str(claims["sub"]), role=role, source="jwt")


def _principal_from_api_key(key: str) -> Optional[Principal]:
    role = settings.api_key_index.get(key)
    if not role:
        return None
    # Use a truncated key prefix as the sub so audit logs are useful
    # without leaking the whole key.
    hint = f"api_key:{key[:8]}…"
    return Principal(sub=hint, role=role, source="api_key")


def get_current_principal(request: Request) -> Principal:
    mode = settings.AUTH_MODE
    if mode == "none":
        if not settings.AUTH_ALLOW_NONE_IN_PROD:
            # Boot-time refusal is the sharper signal, but a request-time
            # check catches the config-drift case where AUTH_MODE gets
            # flipped at runtime via a rollout.
            logger.warning("AUTH_MODE=none served an anonymous request path=%s", request.url.path)
        return _anonymous_principal()

    # --- JWT (or the JWT half of `both`) ---
    if mode in ("jwt", "both"):
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :]
            principal = _principal_from_jwt(token)
            if principal is not None:
                return principal

    # --- API key (or the API-key half of `both`) ---
    if mode in ("api_key", "both"):
        key = request.headers.get("X-API-Key", "")
        if key:
            principal = _principal_from_api_key(key)
            if principal is not None:
                return principal
        # Some callers pass API keys in Authorization: ApiKey <key>; support it.
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("ApiKey "):
            key = auth_header[len("ApiKey ") :]
            principal = _principal_from_api_key(key)
            if principal is not None:
                return principal

    # No mechanism matched — 401 with helpful hint.
    accepted = (
        "Bearer <jwt>" if mode == "jwt"
        else "X-API-Key <key>" if mode == "api_key"
        else "Bearer <jwt> or X-API-Key <key>"
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Authentication required: {accepted}",
        headers={"WWW-Authenticate": "Bearer, ApiKey"},
    )


def require_admin(p: Principal = Depends(get_current_principal)) -> Principal:
    if not p.is_admin:
        raise HTTPException(status_code=403, detail="admin role required")
    return p
