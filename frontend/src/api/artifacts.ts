// frontend/src/api/artifacts.ts
//
// Unified artifact and grant API — calls the new /artifacts and /grants
// endpoints that replace the workspace/collection-scoped routes.
//
// Existing workspaces.ts and collections.ts remain untouched for now;
// consumers will migrate incrementally.

import { get, post, patch, del } from './api';
import type { ArtifactResponse } from './types/artifact';
import type { GrantResponse } from './types/grant';
import type { SearchResponse } from './types/search';
import type { InvokeResponse } from './types/invoke';

// ─────────────────────────────────────────────────────────────────────────────
// Artifact CRUD
// ─────────────────────────────────────────────────────────────────────────────

/** Create an artifact within a container (workspace or collection). */
export function createArtifact(
  containerId: string,
  context: string,
  content?: string,
): Promise<ArtifactResponse> {
  return post(`/artifacts`, { container_id: containerId, context, content });
}

/** Get a single artifact by ID. */
export function getArtifact(artifactId: string): Promise<ArtifactResponse> {
  return get(`/artifacts/${artifactId}`);
}

/** Update an artifact (partial patch). */
export function updateArtifact(
  artifactId: string,
  updates: Record<string, unknown>,
): Promise<ArtifactResponse> {
  return patch(`/artifacts/${artifactId}`, updates);
}

/** Delete an artifact. */
export function deleteArtifact(artifactId: string): Promise<void> {
  return del(`/artifacts/${artifactId}`);
}

/** Fetch child artifacts of a parent artifact. */
export function getChildren(
  artifactId: string,
  params?: { content_type?: string; workspace_id?: string },
): Promise<ArtifactResponse[]> {
  return get(`/artifacts/${artifactId}/children`, { params });
}

/**
 * Add an item to a container (workspace or collection).
 *
 * Analogous to createArtifact but semantically represents adding an existing
 * concept (e.g. importing a collection artifact into a workspace).
 */
export function addItemToContainer(
  containerId: string,
  context: string,
  content?: string,
): Promise<ArtifactResponse> {
  return post(`/artifacts`, { container_id: containerId, context, content });
}

/**
 * Invoke an artifact as an operator (Transform execution).
 *
 * Calls the unified invoke endpoint scoped to a specific artifact.
 */
export function invokeArtifact(
  artifactId: string,
  input?: string,
  params?: Record<string, unknown>,
  workspaceId?: string,
  artifactIds?: string[],
): Promise<InvokeResponse> {
  return post(`/artifacts/${artifactId}/invoke`, {
    input,
    params,
    workspace_id: workspaceId,
    artifacts: artifactIds,
  });
}

/**
 * Search artifacts across all accessible containers.
 *
 * Wraps the unified /artifacts/search endpoint with optional scope and
 * content-type filters.
 */
export function searchArtifacts(
  queryText: string,
  options?: {
    scope?: string[];
    contentTypes?: string[];
  },
): Promise<SearchResponse> {
  return post(`/artifacts/search`, {
    query_text: queryText,
    scope: options?.scope,
    content_types: options?.contentTypes,
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Grant management
// ─────────────────────────────────────────────────────────────────────────────

/** Create a grant on a resource (collection, workspace, or artifact). */
export function createGrant(
  resourceId: string,
  granteeType: string,
  permissions: Record<string, boolean>,
  options?: {
    targetEntity?: string;
    targetEntityType?: string;
    maxClaims?: number;
  },
): Promise<GrantResponse> {
  return post(`/grants`, {
    resource_id: resourceId,
    grantee_type: granteeType,
    ...permissions,
    target_entity: options?.targetEntity,
    target_entity_type: options?.targetEntityType,
    max_claims: options?.maxClaims,
  });
}

/** Get a single grant by ID. */
export function getGrant(grantId: string): Promise<GrantResponse> {
  return get(`/grants/${grantId}`);
}

/** Update a grant (partial patch). */
export function updateGrant(
  grantId: string,
  updates: Record<string, unknown>,
): Promise<GrantResponse> {
  return patch(`/grants/${grantId}`, updates);
}

/** Delete a grant. */
export function deleteGrant(grantId: string): Promise<void> {
  return del(`/grants/${grantId}`);
}

/** Claim an invite grant using its token. */
export function claimInvite(token: string): Promise<GrantResponse> {
  return post(`/grants/claim`, { token });
}

/** Accept a grant that requires explicit acceptance. */
export function acceptGrant(grantId: string): Promise<GrantResponse> {
  return post(`/grants/${grantId}/accept`, {});
}
