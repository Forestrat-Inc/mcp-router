"""Pydantic request/response models."""

import re
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.config import settings

# Rudimentary "does this look like plaintext, not a KV pointer?" shape check.
# Not a security guarantee — it catches the sleep-deprived-at-2am case where
# someone pastes a raw token instead of the kv:// URI.
_KV_URI_RE = re.compile(r"^kv://[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")

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
    server_metadata: Dict[str, Any] = Field(default_factory=dict, alias="metadata")
    owner_email: Optional[str] = None
    status: str = "active"
    # Guardrail explained in docs/mcp-router/contracts.md §7 — required only
    # when discovery response returns read_only_default=false.
    i_accept_write_capable_server: bool = False

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("transport")
    @classmethod
    def _transport_supported(cls, v: str) -> str:
        if v not in VALID_TRANSPORTS:
            raise ValueError(f"transport must be one of {sorted(VALID_TRANSPORTS)}")
        if v != "http":
            raise ValueError(f"transport '{v}' not implemented in v1; only 'http' is supported")
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
        # Reject inline secrets by shape. Not perfect but catches the obvious.
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


class DiscoveryHandles(BaseModel):
    severities: List[str] = Field(default_factory=list)
    alert_types: List[str] = Field(default_factory=list)
    metric_patterns: List[str] = Field(default_factory=list)
    error_signatures: List[str] = Field(default_factory=list)


class DiscoveryDeclaredAction(BaseModel):
    id: str
    reversible: bool = True
    requires_approval: bool = True


class DiscoveryResponse(BaseModel):
    """Shape returned by aims_discover_capabilities on the MCP server."""

    protocol_version: str
    server_name: str
    server_version: str = ""
    handles: DiscoveryHandles = Field(default_factory=DiscoveryHandles)
    declared_actions: List[DiscoveryDeclaredAction] = Field(default_factory=list)
    max_response_ms: int = 10000
    read_only_default: bool = True


# ── Propose (agent → router → MCP server) ────────────────────────────────
# The router's /propose endpoint takes a typed payload from the agent and
# forwards it to the MCP server. Passing the ontology-derived similar
# incidents + runbooks as-is; the schema mirrors the contract §2.2 input.


class ProposeIncidentBlock(BaseModel):
    id: str
    external_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    severity: str
    status: Optional[str] = None
    application_id: str
    instance_id: Optional[str] = None
    created_at: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProposeSimilarPast(BaseModel):
    id: str
    title: str
    similarity_score: float
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_notes: Optional[str] = None
    resolution_id: Optional[str] = None


class ProposeRunbook(BaseModel):
    title: str
    source: str = "confluence"
    url: Optional[str] = None
    excerpt: Optional[str] = None


class ProposeContext(BaseModel):
    similar_past_incidents: List[ProposeSimilarPast] = Field(default_factory=list)
    runbooks: List[ProposeRunbook] = Field(default_factory=list)


class ProposeConstraints(BaseModel):
    read_only: bool = True
    must_confirm_before_action: bool = True
    max_thinking_ms: int = 30000
    correlation_id: Optional[str] = None


class ProposeRequest(BaseModel):
    incident: ProposeIncidentBlock
    context: ProposeContext = Field(default_factory=ProposeContext)
    constraints: ProposeConstraints = Field(default_factory=ProposeConstraints)


class ProposeRecommendedAction(BaseModel):
    kind: str  # diagnostic | remediation | escalation | information
    description: str
    reversible: bool = True
    requires_approval: bool = True
    action_id: Optional[str] = None
    action_args: Dict[str, Any] = Field(default_factory=dict)
    estimated_impact: Optional[str] = None


class ProposeResponse(BaseModel):
    """Passed back to the agent verbatim."""

    server_name: str
    confidence: float = 0.0
    analysis: str = ""
    recommended_actions: List[ProposeRecommendedAction] = Field(default_factory=list)
    next_investigations: List[str] = Field(default_factory=list)
    cited_past_incidents: List[str] = Field(default_factory=list)
    cited_runbooks: List[str] = Field(default_factory=list)
    unable_to_diagnose: bool = False
    reasons: List[str] = Field(default_factory=list)
