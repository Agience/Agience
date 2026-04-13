from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class MCPTool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None


class MCPResourceDesc(BaseModel):
    id: str
    kind: str = Field(description="file | url | text | other")
    uri: Optional[str] = None
    title: Optional[str] = None
    text: Optional[str] = None
    content_type: Optional[str] = None
    props: Dict[str, Any] = Field(default_factory=dict)


class MCPPrompt(BaseModel):
    name: str
    description: Optional[str] = None
    arguments: Optional[List[Dict[str, Any]]] = None


class MCPServerInfo(BaseModel):
    server: str
    name: Optional[str] = None
    tools: List[MCPTool] = Field(default_factory=list)
    resources: List[MCPResourceDesc] = Field(default_factory=list)
    prompts: List[MCPPrompt] = Field(default_factory=list)
    status: str = "ok"
    message: Optional[str] = None


class MCPServerTransport(BaseModel):
    type: str = Field(description="stdio | http")
    # stdio
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    cwd: Optional[str] = None
    env: Dict[str, str] = Field(default_factory=dict)
    # http
    well_known: Optional[str] = Field(
        default=None,
        description="Base URL for .well-known/mcp.json or direct MCP endpoint",
    )


class MCPAuthConfig(BaseModel):
    """Auth configuration parsed from an MCP server artifact's context.

    type values:
      "oauth2"   — OAuth2 flow; token obtained from a stored refresh token or
                   bearer-only access token referenced by authorizer_id.
      "api_key"  — Stored secret injected directly as a header (no expiry logic).
      "static"   — Literal header + value (dev/testing only).
      "none"     — No auth required.
    """
    type: str = Field(description="oauth2 | api_key | static | none")
    authorizer_id: Optional[str] = None   # artifact ID of the authorizer (oauth2)
    secret_id: Optional[str] = None        # secret ID (api_key)
    header: Optional[str] = None           # header name (api_key / static)
    value: Optional[str] = None            # literal value (static only)


class MCPServerConfig(BaseModel):
    id: str
    label: str
    transport: MCPServerTransport
    notes: Optional[str] = None
    auth: Optional[MCPAuthConfig] = None
    runtime_headers: Dict[str, str] = Field(default_factory=dict)
