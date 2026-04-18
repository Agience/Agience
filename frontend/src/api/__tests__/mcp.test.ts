// src/api/__tests__/mcp.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

import api from '../api';
import {
  __clearServerArtifactIdCacheForTests,
  listWorkspaceMCPServers,
  importMCPResources,
  readMCPResource,
  proxyToolCall,
} from '../mcp';

const mockGet = api.get as ReturnType<typeof vi.fn>;
const mockPost = api.post as ReturnType<typeof vi.fn>;

describe('api/mcp — listWorkspaceMCPServers', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __clearServerArtifactIdCacheForTests();
  });

  it('calls GET /mcp/workspaces/:id/servers and returns server list', async () => {
    const servers = [
      {
        server: 'agience-core',
        tools: [{ name: 'search', description: 'Search artifacts' }],
        resources: [],
        status: 'ok',
      },
      {
        server: 'artifact-abc',
        tools: [{ name: 'list_items' }],
        resources: [{ id: 'r1', kind: 'item', uri: 'agience://r1', title: 'Resource 1' }],
        status: 'ok',
      },
    ];
    mockGet.mockResolvedValueOnce({ data: servers });

    const result = await listWorkspaceMCPServers('ws-123');

    expect(mockGet).toHaveBeenCalledWith('/mcp/workspaces/ws-123/servers');
    expect(result).toHaveLength(2);
    expect(result[0].server).toBe('agience-core');
    expect(result[1].tools[0].name).toBe('list_items');
  });

  it('returns empty list for workspace with no configured servers', async () => {
    mockGet.mockResolvedValueOnce({ data: [] });

    const result = await listWorkspaceMCPServers('ws-empty');

    expect(result).toEqual([]);
  });

  it('propagates API errors', async () => {
    mockGet.mockRejectedValueOnce(new Error('Unauthorized'));

    await expect(listWorkspaceMCPServers('ws-1')).rejects.toThrow('Unauthorized');
  });
});

describe('api/mcp — importMCPResources (Phase 7D: artifact-native dispatch)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __clearServerArtifactIdCacheForTests();
  });

  it('resolves a server name to a UUID before posting resources_import', async () => {
    mockGet.mockResolvedValueOnce({
      data: [{ server: '8ce77110-f1af-4ab3-86d8-f84305888008', name: 'Agience Core', tools: [], resources: [], status: 'ok' }],
    });
    mockPost.mockResolvedValueOnce({ data: { created_artifact_ids: ['c1', 'c2'], count: 2 } });

    const resources = [
      { id: 'r1', kind: 'item', uri: 'agience://r1', title: 'Res 1' },
      { id: 'r2', kind: 'item', uri: 'agience://r2', title: 'Res 2' },
    ];
    const result = await importMCPResources('ws-123', 'agience-core', resources);

    expect(mockPost).toHaveBeenCalledWith(
      '/artifacts/8ce77110-f1af-4ab3-86d8-f84305888008/op/resources_import',
      {
        workspace_id: 'ws-123',
        resources,
      }
    );
    expect(result.count).toBe(2);
    expect(result.artifact_ids).toEqual(['c1', 'c2']);
  });

  it('handles empty resource list (count: 0)', async () => {
    mockGet.mockResolvedValueOnce({
      data: [{ server: '8ce77110-f1af-4ab3-86d8-f84305888008', name: 'Agience Core', tools: [], resources: [], status: 'ok' }],
    });
    mockPost.mockResolvedValueOnce({ data: { created_artifact_ids: [], count: 0 } });

    const result = await importMCPResources('ws-123', 'agience-core', []);

    expect(result.count).toBe(0);
    expect(result.artifact_ids).toEqual([]);
  });

  it('works with a server artifact UUID', async () => {
    mockPost.mockResolvedValueOnce({ data: { created_artifact_ids: ['c3'], count: 1 } });

    const resources = [{ id: 'r3', kind: 'document', uri: 'file://doc.txt', title: 'Doc' }];
    await importMCPResources('ws-1', '123e4567-e89b-42d3-a456-426614174000', resources);

    const url = mockPost.mock.calls[0][0];
    expect(url).toBe('/artifacts/123e4567-e89b-42d3-a456-426614174000/op/resources_import');
    expect(mockGet).not.toHaveBeenCalled();
  });

  it('propagates import errors', async () => {
    mockGet.mockResolvedValueOnce({
      data: [{ server: '8ce77110-f1af-4ab3-86d8-f84305888008', name: 'Agience Core', tools: [], resources: [], status: 'ok' }],
    });
    mockPost.mockRejectedValueOnce(new Error('Server unreachable'));

    await expect(
      importMCPResources('ws-1', 'agience-core', [])
    ).rejects.toThrow('Server unreachable');
  });
});

describe('api/mcp — readMCPResource (Phase 7D: artifact-native dispatch)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __clearServerArtifactIdCacheForTests();
  });

  it('resolves the workspace server name to a UUID before resources_read', async () => {
    const contents = {
      uri: 'agience://collection/c1',
      name: 'My Collection',
      mimeType: 'application/json',
      text: '{"artifacts":[]}',
    };
    mockGet.mockResolvedValueOnce({
      data: [{ server: 'cf759fa7-1d53-4867-8c20-2c6d92fe4d0d', name: 'Seraph', tools: [], resources: [], status: 'ok' }],
    });
    mockPost.mockResolvedValueOnce({ data: contents });

    const result = await readMCPResource('seraph', 'agience://collection/c1', 'ws-1');

    expect(mockPost).toHaveBeenCalledWith(
      '/artifacts/cf759fa7-1d53-4867-8c20-2c6d92fe4d0d/op/resources_read',
      {
        uri: 'agience://collection/c1',
        workspace_id: 'ws-1',
      }
    );
    expect(result.uri).toBe('agience://collection/c1');
    expect(result.name).toBe('My Collection');
    expect(result.text).toBe('{"artifacts":[]}');
  });

  it('works with a server artifact UUID', async () => {
    mockPost.mockResolvedValueOnce({
      data: { uri: 'external://doc', name: 'External Doc', mimeType: 'text/plain', text: 'content' },
    });

    await readMCPResource('123e4567-e89b-42d3-a456-426614174001', 'external://doc', 'ws-1');

    const url = mockPost.mock.calls[0][0];
    expect(url).toBe('/artifacts/123e4567-e89b-42d3-a456-426614174001/op/resources_read');
    const body = mockPost.mock.calls[0][1];
    expect(body.uri).toBe('external://doc');
    expect(mockGet).not.toHaveBeenCalled();
  });

  it('propagates read errors', async () => {
    mockGet.mockResolvedValueOnce({
      data: [{ server: '8ce77110-f1af-4ab3-86d8-f84305888008', name: 'Agience Core', tools: [], resources: [], status: 'ok' }],
    });
    mockPost.mockRejectedValueOnce(new Error('Resource not found'));

    await expect(
      readMCPResource('agience-core', 'agience://missing', 'ws-1')
    ).rejects.toThrow('Resource not found');
  });

  it('fails fast when a server reference cannot be resolved to a UUID', async () => {
    mockGet.mockResolvedValueOnce({ data: [] });
    mockGet.mockResolvedValueOnce({ data: [] });

    await expect(
      readMCPResource('unknown-server', 'agience://missing', 'ws-1')
    ).rejects.toThrow("Unknown MCP server reference 'unknown-server'");

    expect(mockPost).not.toHaveBeenCalled();
  });
});

describe('api/mcp — proxyToolCall', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __clearServerArtifactIdCacheForTests();
  });

  it('resolves a server name to a UUID before invoke', async () => {
    mockGet.mockResolvedValueOnce({
      data: [{ server: '641e9cfb-2b57-4515-b912-9d0a07eb6225', name: 'Ophan', tools: [], resources: [], status: 'ok' }],
    });
    mockPost.mockResolvedValueOnce({ data: { content: [{ type: 'text', text: 'ok' }] } });

    const result = await proxyToolCall('billing.get_portal', { invoice_id: 'inv-1' }, 'ophan', 'ws-1');

    expect(mockPost).toHaveBeenCalledWith(
      '/artifacts/641e9cfb-2b57-4515-b912-9d0a07eb6225/invoke',
      { name: 'billing.get_portal', arguments: { invoice_id: 'inv-1' }, workspace_id: 'ws-1' },
      { timeout: 0 },
    );
    expect(result.content[0]?.text).toBe('ok');
  });
});


