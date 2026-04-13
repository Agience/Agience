from __future__ import annotations

import subprocess
import json
import logging
from typing import Any, Dict, List, Optional
import httpx

from .contracts import MCPPrompt, MCPResourceDesc, MCPServerConfig, MCPServerInfo, MCPTool
from .security import sanitize_headers

logger = logging.getLogger(__name__)


class MCPClient:
    """Base class for MCP client implementations."""
    
    def __init__(self, config: MCPServerConfig):
        self.config = config
    
    def list_tools(self) -> List[MCPTool]:
        """List available tools from the MCP server."""
        raise NotImplementedError
    
    def list_resources(self) -> List[MCPResourceDesc]:
        """List available resources from the MCP server."""
        raise NotImplementedError
    
    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke a tool with arguments and return the result."""
        raise NotImplementedError

    def list_prompts(self) -> List[MCPPrompt]:
        """List available prompts from the MCP server."""
        raise NotImplementedError

    def read_resource(self, uri: str) -> Dict[str, Any]:
        """Read a resource by URI and return its contents."""
        raise NotImplementedError
    
    def close(self):
        """Clean up any resources."""
        pass


class StdioMCPClient(MCPClient):
    """MCP client that communicates via stdio subprocess."""
    
    def __init__(self, config: MCPServerConfig):
        super().__init__(config)
        self.process: Optional[subprocess.Popen] = None
        
    def _ensure_connected(self):
        """Start subprocess if not already running."""
        if self.process is None:
            transport = self.config.transport
            if not transport.command:
                raise ValueError("stdio transport requires command")
            
            env = dict(transport.env) if transport.env else None
            self.process = subprocess.Popen(
                [transport.command] + (transport.args or []),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=transport.cwd,
                env=env,
                text=True,
            )
    
    def _send_request(self, method: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Send JSON-RPC request and receive response."""
        self._ensure_connected()
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise RuntimeError("MCP process not available")
        
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {}
        }
        
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            
            response_line = self.process.stdout.readline()
            if not response_line:
                raise RuntimeError("No response from MCP server")
            
            response = json.loads(response_line)
            if "error" in response:
                raise RuntimeError(f"MCP error: {response['error']}")
            
            return response.get("result", {})
        except Exception as e:
            logger.error(f"MCP stdio communication error: {e}")
            raise
    
    def list_tools(self) -> List[MCPTool]:
        """List available tools."""
        try:
            result = self._send_request("tools/list")
            tools_data = result.get("tools", [])
            return [
                MCPTool(
                    name=t.get("name", ""),
                    description=t.get("description"),
                    input_schema=t.get("inputSchema")
                )
                for t in tools_data
            ]
        except Exception as e:
            logger.error(f"Failed to list tools: {e}")
            return []
    
    def list_resources(self) -> List[MCPResourceDesc]:
        """List available resources."""
        try:
            result = self._send_request("resources/list")
            resources_data = result.get("resources", [])
            return [
                MCPResourceDesc(
                    id=r.get("uri", r.get("id", "")),
                    kind=r.get("kind", r.get("mimeType", "text")),
                    uri=r.get("uri"),
                    title=r.get("name"),
                    text=r.get("text"),
                    content_type=r.get("mimeType"),
                    props={}
                )
                for r in resources_data
            ]
        except Exception as e:
            logger.error(f"Failed to list resources: {e}")
            return []

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke a tool."""
        result = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })
        return result

    def list_prompts(self) -> List[MCPPrompt]:
        try:
            result = self._send_request("prompts/list")
            prompts_data = result.get("prompts", [])
            out: List[MCPPrompt] = []
            for p in prompts_data:
                if not isinstance(p, dict):
                    continue
                out.append(
                    MCPPrompt(
                        name=p.get("name", ""),
                        description=p.get("description"),
                        arguments=p.get("arguments"),
                    )
                )
            return out
        except Exception as e:
            logger.error(f"Failed to list prompts: {e}")
            return []

    def read_resource(self, uri: str) -> Dict[str, Any]:
        result = self._send_request("resources/read", {"uri": uri})
        contents = result.get("contents")
        if isinstance(contents, list) and contents:
            return contents[0]
        return {}

    def close(self):
        """Terminate subprocess."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None


class HTTPMCPClient(MCPClient):
    """MCP client that communicates via HTTP via MCP Streamable HTTP transport.
    
    Implements the required initialize handshake to obtain a session ID
    before sending any other requests.
    """
    
    def __init__(self, config: MCPServerConfig):
        super().__init__(config)
        # Sanitize headers from env dict (for auth tokens, etc.)
        raw_headers = dict(config.transport.env) if config.transport.env else {}
        headers = sanitize_headers(raw_headers)
        # Merge runtime-resolved headers (from auth resolution) — these take precedence
        if config.runtime_headers:
            resolved = sanitize_headers(dict(config.runtime_headers))
            headers.update(resolved)
        # MCP Streamable HTTP requires Accept to include both JSON and SSE
        headers.setdefault("Accept", "application/json, text/event-stream")
        self.client = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=5.0),
            headers=headers,
        )
        self.base_url: Optional[str] = None
        self._session_id: Optional[str] = None
        self._initialized: bool = False
        
    def _ensure_connected(self):
        """Discover MCP endpoint and perform initialize handshake."""
        if self._initialized:
            return
        
        if self.base_url is None:
            transport = self.config.transport
            if not transport.well_known:
                raise ValueError("HTTP transport requires well_known URL")
            
            try:
                if transport.well_known.endswith("/.well-known/mcp.json"):
                    response = self.client.get(transport.well_known)
                    response.raise_for_status()
                    mcp_config = response.json() if response.content else {}

                    endpoint = None
                    if isinstance(mcp_config, dict):
                        endpoint = mcp_config.get("endpoint")
                        endpoints = mcp_config.get("endpoints")
                        if not endpoint and isinstance(endpoints, dict):
                            endpoint = endpoints.get("http") or endpoints.get("sse")

                    base = transport.well_known.replace("/.well-known/mcp.json", "")
                    if isinstance(endpoint, str) and endpoint:
                        if endpoint.startswith("http://") or endpoint.startswith("https://"):
                            self.base_url = endpoint
                        elif endpoint.startswith("/"):
                            self.base_url = base.rstrip("/") + endpoint
                        else:
                            self.base_url = base.rstrip("/") + "/" + endpoint
                    else:
                        self.base_url = base
                else:
                    self.base_url = transport.well_known
            except Exception as e:
                logger.error(f"Failed to discover MCP endpoint: {e}")
                self.base_url = transport.well_known
        
        # Perform MCP initialize handshake to obtain session ID.
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "agience-platform", "version": "1.0.0"},
            },
        }
        try:
            response = self.client.post(self.base_url, json=init_request)
            if response.status_code >= 400:
                logger.error(
                    "MCP initialize failed %s from %s -- body: %s",
                    response.status_code, self.base_url, response.text[:500],
                )
            response.raise_for_status()

            # Capture session ID from response header.
            session_id = response.headers.get("mcp-session-id")
            if session_id:
                self._session_id = session_id
                self.client.headers["mcp-session-id"] = session_id

            # Send initialized notification (required by MCP protocol).
            notif = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
            self.client.post(self.base_url, json=notif)
            self._initialized = True

        except Exception as e:
            logger.error(f"MCP initialize handshake failed: {e}")
            raise
    
    def _send_request(self, method: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Send JSON-RPC request via HTTP POST (session ID injected automatically)."""
        self._ensure_connected()
        if not self.base_url:
            raise RuntimeError("MCP endpoint not configured")
        
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {}
        }
        
        try:
            response = self.client.post(self.base_url, json=request)
            if response.status_code >= 400:
                logger.error(
                    "MCP HTTP %s from %s -- body: %s",
                    response.status_code, self.base_url, response.text[:500],
                )
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                result = self._parse_sse_response(response.text)
            else:
                result = response.json()
            
            if "error" in result:
                raise RuntimeError(f"MCP error: {result['error']}")
            
            return result.get("result", {})
        except Exception as e:
            logger.error(f"MCP HTTP communication error: {e}")
            raise

    @staticmethod
    def _parse_sse_response(text: str) -> Dict[str, Any]:
        """Extract JSON-RPC response from SSE event stream."""
        data_lines = []
        for line in text.splitlines():
            if line.startswith("data: "):
                data_lines.append(line[6:])
        # The last data payload is the JSON-RPC response
        for data in reversed(data_lines):
            try:
                return json.loads(data)
            except (json.JSONDecodeError, ValueError):
                continue
        raise RuntimeError("No valid JSON-RPC response found in SSE stream")
    
    def list_tools(self) -> List[MCPTool]:
        """List available tools."""
        try:
            result = self._send_request("tools/list")
            tools_data = result.get("tools", [])
            return [
                MCPTool(
                    name=t.get("name", ""),
                    description=t.get("description"),
                    input_schema=t.get("inputSchema")
                )
                for t in tools_data
            ]
        except Exception as e:
            logger.error(f"Failed to list tools: {e}")
            return []
    
    def list_resources(self) -> List[MCPResourceDesc]:
        """List available resources."""
        try:
            result = self._send_request("resources/list")
            resources_data = result.get("resources", [])
            return [
                MCPResourceDesc(
                    id=r.get("uri", r.get("id", "")),
                    kind=r.get("kind", r.get("mimeType", "text")),
                    uri=r.get("uri"),
                    title=r.get("name"),
                    text=r.get("text"),
                    content_type=r.get("mimeType"),
                    props={}
                )
                for r in resources_data
            ]
        except Exception as e:
            logger.error(f"Failed to list resources: {e}")
            return []

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke a tool."""
        result = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })
        return result

    def list_prompts(self) -> List[MCPPrompt]:
        try:
            result = self._send_request("prompts/list")
            prompts_data = result.get("prompts", [])
            out: List[MCPPrompt] = []
            for p in prompts_data:
                if not isinstance(p, dict):
                    continue
                out.append(
                    MCPPrompt(
                        name=p.get("name", ""),
                        description=p.get("description"),
                        arguments=p.get("arguments"),
                    )
                )
            return out
        except Exception as e:
            logger.error(f"Failed to list prompts: {e}")
            return []

    def read_resource(self, uri: str) -> Dict[str, Any]:
        result = self._send_request("resources/read", {"uri": uri})
        contents = result.get("contents")
        if isinstance(contents, list) and contents:
            return contents[0]
        return {}

    def close(self):
        """Close HTTP client."""
        self.client.close()


def create_client(config: MCPServerConfig) -> MCPClient:
    """Factory function to create appropriate MCP client based on transport type."""
    transport_type = config.transport.type.lower()
    
    if transport_type == "stdio":
        return StdioMCPClient(config)
    elif transport_type == "http":
        return HTTPMCPClient(config)
    else:
        raise ValueError(f"Unsupported MCP transport type: {transport_type}")


def fetch_server_info(config: MCPServerConfig) -> MCPServerInfo:
    """Connect to MCP server and fetch tools/resources.
    
    Args:
        config: MCP server configuration with transport details
        
    Returns:
        MCPServerInfo with live tool and resource listings
    """
    try:
        client = create_client(config)
        try:
            tools = client.list_tools()
            resources = client.list_resources()
            prompts = client.list_prompts()
            
            return MCPServerInfo(
                server=config.id,
                name=config.label,
                tools=tools,
                resources=resources,
                prompts=prompts,
                status="ok"
            )
        finally:
            client.close()
    except Exception as e:
        logger.error(f"Failed to fetch server info for {config.id}: {e}")
        return MCPServerInfo(
            server=config.id,
            name=config.label,
            tools=[],
            resources=[],
            status="error",
            message=str(e)
        )
