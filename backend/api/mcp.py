from __future__ import annotations

from typing import List
from pydantic import BaseModel, Field

from mcp_client.contracts import MCPPrompt as _MCPPrompt
from mcp_client.contracts import MCPServerInfo as _MCPServerInfo


class MCPTool(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict | None = None


class MCPResource(BaseModel):
    id: str
    kind: str
    uri: str | None = None
    title: str | None = None
    text: str | None = None
    props: dict = Field(default_factory=dict)


class MCPServerInfo(BaseModel):
    server: str
    name: str | None = None
    tools: List[MCPTool] = Field(default_factory=list)
    resources: List[MCPResource] = Field(default_factory=list)
    prompts: List[dict] = Field(default_factory=list)
    status: str = "ok"
    message: str | None = None

    @classmethod
    def from_core(cls, core: _MCPServerInfo) -> "MCPServerInfo":
        return cls(
            server=core.server,
            name=core.name,
            tools=[MCPTool(**t.model_dump()) for t in core.tools],
            resources=[MCPResource(**r.model_dump()) for r in core.resources],
            prompts=[p.model_dump() if isinstance(p, _MCPPrompt) else dict(p) for p in (core.prompts or [])],
            status=core.status,
            message=core.message,
        )


