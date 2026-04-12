import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../api', () => {
  return {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    put: vi.fn(),
  };
});

import { get, post, patch, del, put } from '../api';
import {
  listCollections,
  createCollection,
  getCollection,
  updateCollection,
  deleteCollection,
  createGrant,
  listGrants,
  getGrant,
  updateGrant,
  deleteGrant,
  listCollectionArtifacts,
  getCollectionArtifact,
  getCollectionArtifactContentUrl,
  addArtifactToCollection,
  removeArtifactFromCollection,
  listCollectionCommits,
  getCollectionArtifactsBatchGlobal,
} from '../collections';

describe('api/collections', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.removeItem('access_token');
  });

  // Collections CRUD
  it('listCollections calls GET /artifacts/containers?type=collection', async () => {
    get.mockResolvedValueOnce([{ id: 'c1', name: 'Collection' }]);
    const res = await listCollections();
    expect(get).toHaveBeenCalledWith('/artifacts/containers?type=collection');
    expect(res).toEqual([{ id: 'c1', name: 'Collection' }]);
  });

  it('createCollection calls POST /artifacts with collection content_type', async () => {
    post.mockResolvedValueOnce({ id: 'c-new', name: 'New', description: 'D' });
    const res = await createCollection({ name: 'New', description: 'D' });
    expect(post).toHaveBeenCalledWith('/artifacts', { name: 'New', description: 'D', content_type: 'application/vnd.agience.collection+json' });
    expect(res.id).toBe('c-new');
  });

  it('getCollection calls GET /artifacts/:id', async () => {
    get.mockResolvedValueOnce({ id: 'c1', name: 'Test' });
    const res = await getCollection('c1');
    expect(get).toHaveBeenCalledWith('/artifacts/c1');
    expect(res.id).toBe('c1');
  });

  it('getCollection with grant key ignores grant key (no longer used)', async () => {
    get.mockResolvedValueOnce({ id: 'c1', name: 'Test' });
    await getCollection('c1', 'k-abc');
    expect(get).toHaveBeenCalledWith('/artifacts/c1');
  });

  it('updateCollection calls PATCH /artifacts/:id', async () => {
    patch.mockResolvedValueOnce({ id: 'c1', name: 'Updated' });
    const res = await updateCollection('c1', { name: 'Updated' });
    expect(patch).toHaveBeenCalledWith('/artifacts/c1', { name: 'Updated' });
    expect(res.name).toBe('Updated');
  });

  it('deleteCollection calls DELETE /artifacts/:id', async () => {
    del.mockResolvedValueOnce(undefined);
    await deleteCollection('c1');
    expect(del).toHaveBeenCalledWith('/artifacts/c1');
  });

  // Grants CRUD
  it('createGrant calls POST /grants with resource_id', async () => {
    post.mockResolvedValueOnce({ id: 's1', name: 'Grant', can_read: true });
    const res = await createGrant('c1', {
      name: 'Grant',
      can_read: true,
    });
    expect(post).toHaveBeenCalledWith('/grants', expect.objectContaining({
      resource_id: 'c1',
      resource_type: 'collection',
      name: 'Grant',
    }));
    expect(res.id).toBe('s1');
  });

  it('listGrants calls GET /grants?resource_id=...', async () => {
    get.mockResolvedValueOnce([{ id: 's1', name: 'Grant 1' }]);
    const res = await listGrants('c1');
    expect(get).toHaveBeenCalledWith('/grants?resource_id=c1&resource_type=collection');
    expect(res).toEqual([{ id: 's1', name: 'Grant 1' }]);
  });

  it('getGrant calls GET /grants/:id', async () => {
    get.mockResolvedValueOnce({ id: 's1', name: 'Grant' });
    const res = await getGrant('s1');
    expect(get).toHaveBeenCalledWith('/grants/s1');
    expect(res.id).toBe('s1');
  });

  it('updateGrant calls PATCH /grants/:id', async () => {
    patch.mockResolvedValueOnce({ id: 's1', name: 'Updated Grant', can_read: true });
    const res = await updateGrant('s1', { name: 'Updated Grant' });
    expect(patch).toHaveBeenCalledWith('/grants/s1', { name: 'Updated Grant' });
    expect(res.id).toBe('s1');
  });

  it('deleteGrant calls DELETE /grants/:id', async () => {
    del.mockResolvedValueOnce(undefined);
    await deleteGrant('s1');
    expect(del).toHaveBeenCalledWith('/grants/s1');
  });

  // Collection artifacts
  it('listCollectionArtifacts calls GET /artifacts/list?container_id=...', async () => {
    get.mockResolvedValueOnce({ items: [{ id: 'v1', root_id: 'r1' }] });
    const res = await listCollectionArtifacts('c1');
    expect(get).toHaveBeenCalledWith('/artifacts/list?container_id=c1');
    expect(res).toEqual([{ id: 'v1', root_id: 'r1' }]);
  });

  it('listCollectionArtifacts with grant key ignores grant key', async () => {
    get.mockResolvedValueOnce({ items: [] });
    await listCollectionArtifacts('c1', 'k-abc');
    expect(get).toHaveBeenCalledWith('/artifacts/list?container_id=c1');
  });

  it('getCollectionArtifact calls GET /artifacts/:rootId', async () => {
    get.mockResolvedValueOnce({ id: 'v1', root_id: 'r1', content: 'test' });
    const res = await getCollectionArtifact('c1', 'r1');
    expect(get).toHaveBeenCalledWith('/artifacts/r1');
    expect(res.root_id).toBe('r1');
  });

  it('getCollectionArtifactContentUrl calls GET /artifacts/:rootId/content-url', async () => {
    get.mockResolvedValueOnce({ url: 'https://cdn.example/file.pdf', expires_in: 300 });
    const res = await getCollectionArtifactContentUrl('c1', 'r1');
    expect(get).toHaveBeenCalledWith('/artifacts/r1/content-url');
    expect(res.url).toBe('https://cdn.example/file.pdf');
  });

  it('addArtifactToCollection calls POST /artifacts with container_id', async () => {
    post.mockResolvedValueOnce({ id: 'v2', root_id: 'r1' });
    const res = await addArtifactToCollection('c1', 'v1');
    expect(post).toHaveBeenCalledWith('/artifacts', { container_id: 'c1', source_artifact_id: 'v1' });
    expect(res.id).toBe('v2');
  });

  it('removeArtifactFromCollection calls POST /artifacts/:rootId/remove', async () => {
    post.mockResolvedValueOnce(undefined);
    await removeArtifactFromCollection('c1', 'r1');
    expect(post).toHaveBeenCalledWith('/artifacts/r1/remove', { container_id: 'c1' });
  });

  it('listCollectionCommits calls GET /artifacts/:id/commits', async () => {
    get.mockResolvedValueOnce([
      { id: 'cm1', message: 'Commit', confirmation: 'human_affirmed', changeset_type: 'manual' },
    ]);
    const res = await listCollectionCommits('c1');
    expect(get).toHaveBeenCalledWith('/artifacts/c1/commits');
    expect(res[0].id).toBe('cm1');
  });

  // Batch fetch
  it('getCollectionArtifactsBatchGlobal POSTs to /artifacts/batch (no collection scoping)', async () => {
    post.mockResolvedValueOnce({
      artifacts: [{ id: 'v3', root_id: 'r3', content: 'C' }],
    });
    const res = await getCollectionArtifactsBatchGlobal(['r3']);
    expect(post).toHaveBeenCalledWith('/artifacts/batch', { artifact_ids: ['r3'] });
    expect(res[0].root_id).toBe('r3');
  });

  it('getCollectionArtifactsBatchGlobal returns empty array on empty artifacts key', async () => {
    post.mockResolvedValueOnce({ artifacts: [] });
    const res = await getCollectionArtifactsBatchGlobal(['r99']);
    expect(res).toEqual([]);
  });
});
