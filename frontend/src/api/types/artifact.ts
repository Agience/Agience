// src/api/types/artifact.ts

export type JSONValue = string | number | boolean | null | JSONValue[] | { [k: string]: JSONValue };

export type ArtifactState = 'draft' | 'committed' | 'archived';

export interface ArtifactCreate {
  content_type?: string;
  context?: string;
  content: string;
  order_key?: string;
}

export interface ArtifactUpdate {
  context?: string;
  content?: string;
  state?: ArtifactState;
  order_key?: string;
}

export interface ArtifactResponse {
  id?: string;
  root_id?: string;
  collection_id?: string;
  content_type?: string;
  name?: string;
  description?: string;
  context: string;
  content: string;
  created_by?: string;
  created_time?: string;
  modified_by?: string;
  modified_time?: string;
  state: ArtifactState;
  /** True when at least one committed version of this root_id exists. */
  has_committed_version?: boolean;
  order_key?: string;
  /**
   * Non-workspace collections this artifact belongs to (via edges).
   * Populated by `attach_committed_collection_ids()` on the backend.
   * Membership is edge-based and independent of artifact state.
   * (Backend field name is `committed_collection_ids` — a misnomer,
   * since membership is not tied to the committed state.)
   */
  committed_collection_ids?: string[];
  /** True when at least one child artifact exists (via parent_id edge). */
  has_children?: boolean;
  /** Number of child artifacts, if available. */
  child_count?: number;
}

export type Artifact = ArtifactResponse;
