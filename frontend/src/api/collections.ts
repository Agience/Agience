// frontend/src/api/collections.ts

import { get, post, patch, del } from './api';
import { COLLECTION_CONTENT_TYPE } from '@/utils/content-type';
import { subscribeEvents, type BusEvent } from './events';
import {
  CollectionResponse,
  CollectionCommitResponse,
  CollectionCreate,
  CollectionUpdate,
  GrantResponse,
  GrantCreate,
  GrantUpdate,
  ArtifactResponse,
} from './types';

// list all collections
export function listCollections(): Promise<CollectionResponse[]> {
  return get('/artifacts/containers?type=collection');
}

// get a single collection
export function getCollection(id: string, _grantKey?: string): Promise<CollectionResponse> {
  return get(`/artifacts/${id}`);
}

export function listCollectionCommits(collectionId: string): Promise<CollectionCommitResponse[]> {
  return get(`/artifacts/${collectionId}/commits`);
}

// create a new collection
export function createCollection(
  input: CollectionCreate
): Promise<CollectionResponse> {
  return post('/artifacts', { ...input, content_type: COLLECTION_CONTENT_TYPE });
}

// update name/description
export function updateCollection(
  id: string,
  input: CollectionUpdate
): Promise<CollectionResponse> {
  return patch(`/artifacts/${id}`, input);
}

// delete collection
export function deleteCollection(id: string): Promise<void> {
  return del(`/artifacts/${id}`);
}

// grants — now calling /grants endpoints
export function listGrants(collectionId: string): Promise<GrantResponse[]> {
  return get(`/grants?resource_id=${encodeURIComponent(collectionId)}`);
}

export function createGrant(
  collectionId: string,
  input: Omit<GrantCreate, 'resource_id'>
): Promise<GrantResponse> {
  return post('/grants', { resource_id: collectionId, ...input });
}

export function updateGrant(
  grantId: string,
  input: GrantUpdate
): Promise<GrantResponse> {
  return patch(`/grants/${grantId}`, input);
}

export function deleteGrant(grantId: string): Promise<void> {
  return del(`/grants/${grantId}`);
}

export function getGrant(grantId: string): Promise<GrantResponse> {
  return get(`/grants/${grantId}`);
}

// artifacts
export function listCollectionArtifacts(collectionId: string, _grantKey?: string): Promise<ArtifactResponse[]> {
  return get<{ items: ArtifactResponse[] }>(`/artifacts/list?container_id=${encodeURIComponent(collectionId)}`)
    .then(res => res.items);
}

export const getCollectionArtifacts = listCollectionArtifacts;

export function getCollectionArtifact(_collectionId: string, rootId: string, _grantKey?: string): Promise<ArtifactResponse> {
  return get(`/artifacts/${rootId}`);
}

export function getCollectionArtifactContentUrl(
  _collectionId: string,
  rootId: string,
  _grantKey?: string,
): Promise<{ url: string; expires_in: number | null; filename?: string }> {
  return get(`/artifacts/${rootId}/content-url`);
}

export function addArtifactToCollection(_collectionId: string, versionId: string): Promise<ArtifactResponse> {
  // TODO: Review — the PUT /artifacts/{container_id} endpoint may be more appropriate
  return post(`/artifacts`, { container_id: _collectionId, source_artifact_id: versionId });
}

export function removeArtifactFromCollection(collectionId: string, rootId: string): Promise<void> {
  return post(`/artifacts/${rootId}/remove`, { container_id: collectionId });
}

// batch fetch multiple artifacts across ALL accessible collections (global search)
export async function getCollectionArtifactsBatchGlobal(
  rootIds: string[]
): Promise<ArtifactResponse[]> {
  const res = await post<{ artifacts: ArtifactResponse[] }>('/artifacts/batch', { artifact_ids: rootIds });
  return res.artifacts ?? [];
}

// === Real-time collection events (SSE) ===

import type { InvokeEventPayload } from './workspaces';

export type CollectionEventHandlers = {
  onArtifactCreated?: (artifact: ArtifactResponse) => void;
  onArtifactUpdated?: (artifact: ArtifactResponse) => void;
  onArtifactDeleted?: (artifactId: string) => void;
  onCollectionRefreshed?: () => void;
  // Operation lifecycle events (Phase 1 — Enterprise Eventing refactor).
  onInvokeStarted?: (payload: InvokeEventPayload) => void;
  onInvokeCompleted?: (payload: InvokeEventPayload) => void;
  onInvokeFailed?: (payload: InvokeEventPayload) => void;
};

/**
 * Subscribe to real-time collection change events via the unified /events
 * WebSocket. Thin adapter over `subscribeEvents` in api/events.ts.
 */
export function subscribeCollectionEvents(
  collectionId: string,
  handlers: CollectionEventHandlers,
): () => void {
  return subscribeEvents(
    {
      container_id: collectionId,
      event_names: [
        'artifact.created',
        'artifact.updated',
        'artifact.deleted',
        'collection.refreshed',
        'artifact.invoke.started',
        'artifact.invoke.completed',
        'artifact.invoke.failed',
      ],
    },
    (evt: BusEvent) => {
      const payload = evt.payload || {};
      switch (evt.event) {
        case 'artifact.created': {
          const a = (payload as { artifact?: ArtifactResponse }).artifact;
          if (a) handlers.onArtifactCreated?.(a);
          return;
        }
        case 'artifact.updated': {
          const a = (payload as { artifact?: ArtifactResponse }).artifact;
          if (a) handlers.onArtifactUpdated?.(a);
          return;
        }
        case 'artifact.deleted': {
          const id = (payload as { artifact_id?: string }).artifact_id;
          if (id) handlers.onArtifactDeleted?.(id);
          return;
        }
        case 'collection.refreshed':
          handlers.onCollectionRefreshed?.();
          return;
        case 'artifact.invoke.started':
          handlers.onInvokeStarted?.(payload as unknown as InvokeEventPayload);
          return;
        case 'artifact.invoke.completed':
          handlers.onInvokeCompleted?.(payload as unknown as InvokeEventPayload);
          return;
        case 'artifact.invoke.failed':
          handlers.onInvokeFailed?.(payload as unknown as InvokeEventPayload);
          return;
      }
    },
  );
}
