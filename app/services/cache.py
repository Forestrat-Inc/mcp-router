"""Redis cache — cache-aside, 5-min TTL, invalidate on write.

Failure mode: any Redis error is logged + swallowed; the read falls through
to Postgres and the write completes but doesn't try again. Never let the
cache take the router down.
"""

import json
import logging
from typing import Any, Optional
from uuid import UUID

import redis

from app.config import settings

logger = logging.getLogger(__name__)

_client: Optional[redis.Redis] = None


def _get_client() -> Optional[redis.Redis]:
    global _client
    if _client is not None:
        return _client
    try:
        _client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=1.5,
            socket_connect_timeout=1.5,
        )
        _client.ping()
        logger.info("redis connected url=%s", _mask(settings.REDIS_URL))
        return _client
    except Exception as exc:
        logger.warning(
            "redis unreachable — cache-aside disabled (falls through to PG) err=%s",
            type(exc).__name__,
        )
        _client = None
        return None


def _mask(url: str) -> str:
    # rediss://user:pass@host:6380/3  →  rediss://user:***@host:6380/3
    import re
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", url)


def _key(app_id: UUID) -> str:
    return f"{settings.REDIS_KEY_PREFIX}:app:{app_id}"


def get_cached(app_id: UUID) -> Optional[dict]:
    r = _get_client()
    if r is None:
        return None
    try:
        raw = r.get(_key(app_id))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("cache read failed app_id=%s err=%s", app_id, type(exc).__name__)
        return None


def set_cached(app_id: UUID, value: dict) -> None:
    r = _get_client()
    if r is None:
        return
    try:
        r.set(_key(app_id), json.dumps(value, default=str), ex=settings.CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.warning("cache write failed app_id=%s err=%s", app_id, type(exc).__name__)


def invalidate(app_id: UUID) -> None:
    r = _get_client()
    if r is None:
        return
    try:
        r.delete(_key(app_id))
    except Exception as exc:
        logger.warning("cache del failed app_id=%s err=%s", app_id, type(exc).__name__)
