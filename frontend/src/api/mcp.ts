// frontend/src/api/mcp.ts
import api from './api';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const UI_URI_HOST_PATTERN = /^ui:\/\/([^/]+)\//i;

const serverArtifactIdCache = new Map<string, string>();

function normalizeServerRef(value: string): string {
  return value.trim().toLowerCase().replace(/[\s_]+/g, '-');
}

function cacheServerInfo(serverInfo: MCPServerInfo): void {
  const serverId = String(serverInfo.server ?? '').trim();
  if (!serverId) return;

  serverArtifactIdCache.set(serverId, serverId);

  const normalizedName = normalizeServerRef(serverInfo.name ?? '');
  if (normalizedName) {
    serverArtifactIdCache.set(normalizedName, serverId);
  }

  for (const resource of serverInfo.resources ?? []) {
    const uri = String(resource.uri ?? '').trim();
    if (!uri) continue;

    const match = UI_URI_HOST_PATTERN.exec(uri);
    if (!match?.[1]) continue;

    serverArtifactIdCache.set(normalizeServerRef(match[1]), serverId);
  }
}

async function resolveServerArtifactId(serverRef: string, workspaceId?: string): Promise<string> {
  const trimmedRef = serverRef.trim();
  if (!trimmedRef) {
    throw new Error('Missing MCP server artifact UUID');
  }

  if (UUID_PATTERN.test(trimmedRef)) {
    return trimmedRef;
  }

  const normalizedRef = normalizeServerRef(trimmedRef);
  const cached = serverArtifactIdCache.get(trimmedRef) ?? serverArtifactIdCache.get(normalizedRef);
  if (cached) {
    return cached;
  }

  const resolutionErrors: string[] = [];

  if (workspaceId) {
    try {
      const workspaceServers = await listWorkspaceMCPServers(workspaceId);
      workspaceServers.forEach(cacheServerInfo);
      const workspaceResolved = serverArtifactIdCache.get(trimmedRef) ?? serverArtifactIdCache.get(normalizedRef);
      if (workspaceResolved) {
        return workspaceResolved;
      }
    } catch (error) {
      resolutionErrors.push(error instanceof Error ? error.message : String(error));
    }
  }

  try {
    const allServers = await listAllMCPServers();
    allServers.forEach(cacheServerInfo);
    const globalResolved = serverArtifactIdCache.get(trimmedRef) ?? serverArtifactIdCache.get(normalizedRef);
    if (globalResolved) {
      return globalResolved;
    }
  } catch (error) {
    resolutionErrors.push(error instanceof Error ? error.message : String(error));
  }

  if (resolutionErrors.length > 0) {
    throw new Error(
      `Failed to resolve MCP server '${serverRef}' to an artifact UUID: ${resolutionErrors.join('; ')}`,
    );
  }

  throw new Error(`Unknown MCP server reference '${serverRef}'. Expected a server artifact UUID.`);
}

export function __clearServerArtifactIdCacheForTests(): void {
  serverArtifactIdCache.clear();
}

export interface MCPServerTransport {
  type: 'stdio' | 'http';
  command?: string;
  args?: string[];
  cwd?: string;
  env?: Record<string, string>;
  well_known?: string;
}

export interface MCPTool {
  name: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  icon?: string; // Tool-specific icon from _meta.icon
}

export interface MCPResource {
  id: string;
  kind: string;
  uri?: string;
  title?: string;
  text?: string;
  content_type?: string;
  contentType?: string; // API returns content_type but frontend uses camelCase
  props?: Record<string, unknown>;
  icon?: string; // Resource-specific icon from _meta.icon
}

export interface MCPResourceContents {
  uri: string;
  name: string;
  title?: string;
  mimeType?: string;
  contentType?: string;
  text?: string;
  blob?: string;
}

export interface MCPPrompt {
  name: string;
  description?: string;
  arguments?: Array<{
    name: string;
    description?: string;
    required?: boolean;
  }>;
  icon?: string; // Prompt-specific icon from _meta.icon
}

export interface MCPServerInfo {
  server: string;
  name?: string;
  tools: MCPTool[];
  resources: MCPResource[];
  prompts?: MCPPrompt[];
  status: string;
  message?: string;
  icon?: string; // Icon from serverInfo._meta.icon (URL, data URI, emoji, or "agience")
}

/**
 * Lightweight config record derived from MCPServerInfo (no separate endpoint).
 * Used as display metadata alongside the live MCPServerInfo.
 */
export interface MCPServerConfig {
  id: string;
  label: string;
  icon?: string;
}

/**
 * List all MCP servers accessible to the current user across all sources
 * (built-in, desktop host, collection-committed). No workspace binding required.
 */
export async function listAllMCPServers(): Promise<MCPServerInfo[]> {
  const response = await api.get<MCPServerInfo[]>('/mcp/servers');
  return response.data;
}

/**
 * List MCP servers available to a workspace with their tools and resources.
 *
 * Server IDs can be built-in IDs such as "agience-core" or artifact UUIDs
 * of mcp-server+json artifacts.
 */
export async function listWorkspaceMCPServers(workspaceId: string): Promise<MCPServerInfo[]> {
  const response = await api.get<MCPServerInfo[]>(`/mcp/workspaces/${workspaceId}/servers`);
  return response.data;
}

/**
 * Import MCP resources as workspace artifacts.
 *
 * Phase 7D — routes through the artifact dispatcher's custom-operation
 * endpoint (`POST /artifacts/{server_id}/op/resources_import`). The
 * server-side handler is `mcp_service.dispatch_resources_import`.
 */
export async function importMCPResources(
  workspaceId: string,
  server: string,
  resources: MCPResource[]
): Promise<{ count: number; artifact_ids: string[] }> {
  const serverArtifactId = await resolveServerArtifactId(server, workspaceId);
  const response = await api.post<{ created_artifact_ids: string[]; count: number }>(
    `/artifacts/${encodeURIComponent(serverArtifactId)}/op/resources_import`,
    {
      workspace_id: workspaceId,
      resources,
    }
  );
  return {
    count: response.data.count,
    artifact_ids: response.data.created_artifact_ids,
  };
}

/**
 * Read MCP resource contents from an external MCP server.
 *
 * Phase 7D — routes through the artifact dispatcher's custom-operation
 * endpoint (`POST /artifacts/{server_id}/op/resources_read`). The
 * server-side handler is `mcp_service.dispatch_resources_read`.
 *
 * ``server`` is the artifact UUID of a ``vnd.agience.mcp-server+json``
 * artifact — including the seeded built-in persona artifacts.
 *
 * ``workspaceId`` is an optional context hint for server resolution.
 *
 * Note: content-type handlers and agents should NOT call this directly.
 * Use ``POST /agents/invoke`` with the appropriate transform/params instead.
 */
export async function readMCPResource(
  server: string,
  uri: string,
  workspaceId?: string,
): Promise<MCPResourceContents> {
  const serverArtifactId = await resolveServerArtifactId(server, workspaceId);
  const response = await api.post<MCPResourceContents>(
    `/artifacts/${encodeURIComponent(serverArtifactId)}/op/resources_read`,
    { uri, workspace_id: workspaceId }
  );
  return response.data;
}

/**
 * Read a `ui://` resource for rendering in a McpAppHost iframe.
 *
 * Returns the HTML content string for the view.
 */
export async function readUiResource(
  server: string,
  uri: string,
  workspaceId?: string,
): Promise<{ html: string; csp?: Record<string, string[]> }> {
  const result = await readMCPResource(server, uri, workspaceId);
  return {
    html: result.text ?? (result.blob ? atob(result.blob) : ''),
  };
}

/**
 * Proxy a tools/call request from an MCP App iframe to the backend.
 *
 * Phase 7D — routes through the unified artifact invoke endpoint
 * (`POST /artifacts/{server_id}/invoke`) which dispatches via the
 * operation registry. The body shape changed from `{tool, arguments, ...}`
 * to `{name, arguments, ...}` because the dispatcher reads the tool name
 * from `body.name` per the type definition's
 * `operations.invoke.dispatch.tool_ref = $.body.name`.
 */
export async function proxyToolCall(
  toolName: string,
  args: Record<string, unknown>,
  serverArtifactId: string = 'agience-core',
  workspaceId?: string,
): Promise<{ content: Array<{ type: string; text?: string }> }> {
  const resolvedServerArtifactId = await resolveServerArtifactId(serverArtifactId, workspaceId);
  const response = await api.post(
    `/artifacts/${encodeURIComponent(resolvedServerArtifactId)}/invoke`,
    { name: toolName, arguments: args, workspace_id: workspaceId },
    { timeout: 0 },  // no timeout — invoke runs unbounded LLM + tool chains; deltas stream via WebSocket
  );
  return response.data as { content: Array<{ type: string; text?: string }> };
}

