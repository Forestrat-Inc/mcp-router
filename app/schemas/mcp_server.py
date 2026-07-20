"""Pydantic request/response models — pure registry shape."""

import re
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Rudimentary "does this look like plaintext, not a KV pointer?" shape check.
# Not a security guarantee — it catches the sleep-deprived-at-2am case where
# someone pastes a raw token instead of the kv:// URI.
_KV_URI_RE = re.compile(r"^kv://[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")

# ``http`` is the canonical value for streamable-HTTP MCP servers (the
# modern MCP network transport). The other values are declarative — the
# router doesn't call the target, so it doesn't need to interpret them —
# but we keep the validator so a nonsense value is caught at register time.
VALID_TRANSPORTS = {"http", "sse", "ws", "stdio"}
VALID_AUTH_TYPES = {"none", "bearer", "api_key", "oauth", "mtls", "header"}
VALID_STATUSES = {"active", "disabled", "deprecated"}


class MCPServerCreate(BaseModel):
    application_id: UUID
    name: str = Field(min_length=1, max_length=255)
    transport: str
    endpoint_url: str = Field(min_length=1, max_length=2048)
    auth_type: str
    auth_ref: Optional[str] = None
    # Optional caller-supplied capability declaration (severities the server
    # handles, declared_actions, protocol_version, etc.). The router never
    # interviews the target — consumers may use this as a hint to skip a
    # tools/list round-trip. Free-form JSON: shape is documented in
    # docs/contracts.md but not enforced here.
    capabilities: Optional[Dict[str, Any]] = None
    server_metadata: Dict[str, Any] = Field(default_factory=dict, alias="metadata")
    owner_email: Optional[str] = None
    status: str = "active"

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("transport")
    @classmethod
    def _transport_supported(cls, v: str) -> str:
        if v not in VALID_TRANSPORTS:
            raise ValueError(f"transport must be one of {sorted(VALID_TRANSPORTS)}")
        return v

    @field_validator("auth_type")
    @classmethod
    def _auth_type_supported(cls, v: str) -> str:
        if v not in VALID_AUTH_TYPES:
            raise ValueError(f"auth_type must be one of {sorted(VALID_AUTH_TYPES)}")
        return v

    @field_validator("status")
    @classmethod
    def _status_supported(cls, v: str) -> str:
        if v not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        return v

    @field_validator("auth_ref")
    @classmethod
    def _auth_ref_shape(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        if not _KV_URI_RE.match(v):
            raise ValueError(
                "auth_ref must be a KV URI 'kv://<vault>/<secret>' — never a raw token"
            )
        return v


class MCPServerUpdate(BaseModel):
    """PATCH — every field optional. `application_id` is immutable."""

    name: Optional[str] = None
    transport: Optional[str] = None
    endpoint_url: Optional[str] = None
    auth_type: Optional[str] = None
    auth_ref: Optional[str] = None
    capabilities: Optional[Dict[str, Any]] = None
    server_metadata: Optional[Dict[str, Any]] = Field(default=None, alias="metadata")
    owner_email: Optional[str] = None
    status: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("transport")
    @classmethod
    def _transport_supported(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return MCPServerCreate._transport_supported(v)

    @field_validator("auth_type")
    @classmethod
    def _auth_type_supported(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return MCPServerCreate._auth_type_supported(v)

    @field_validator("status")
    @classmethod
    def _status_supported(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return MCPServerCreate._status_supported(v)

    @field_validator("auth_ref")
    @classmethod
    def _auth_ref_shape(cls, v: Optional[str]) -> Optional[str]:
        return MCPServerCreate._auth_ref_shape(v)


class MCPServerResponse(BaseModel):
    application_id: UUID
    name: str
    transport: str
    endpoint_url: str
    auth_type: str
    auth_ref: Optional[str]
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    server_metadata: Dict[str, Any] = Field(default_factory=dict, alias="metadata")
    status: str
    owner_email: Optional[str]
    created_at: Any
    updated_at: Any

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)
