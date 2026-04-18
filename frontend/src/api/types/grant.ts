// src/api/types/grant.ts
//
// Types aligned with the /grants router (grants_router.py).

// Body you POST to /grants
export interface GrantCreate {
  resource_id: string;
  grantee_type?: string;        // "user" | "invite"
  grantee_id?: string;          // user_id for direct grant; omit for invite
  // CRUDEASIO permission flags
  can_create?: boolean;
  can_read?: boolean;
  can_update?: boolean;
  can_delete?: boolean;
  can_evict?: boolean;
  can_invoke?: boolean;
  can_add?: boolean;
  can_share?: boolean;
  can_admin?: boolean;
  // Invite targeting (optional)
  target_entity?: string;
  target_entity_type?: string;
  max_claims?: number;
  requires_identity?: boolean;
  name?: string;
  notes?: string;
  expires_at?: string;
  state?: string;
}

// Body you PATCH to /grants/{grant_id}
export interface GrantUpdate {
  max_claims?: number;
  state?: string;
  name?: string;
  notes?: string;
  expires_at?: string;
  can_create?: boolean;
  can_read?: boolean;
  can_update?: boolean;
  can_delete?: boolean;
  can_evict?: boolean;
  can_invoke?: boolean;
  can_add?: boolean;
  can_share?: boolean;
  can_admin?: boolean;
}

// What grant endpoints return
export interface GrantResponse {
  id: string;
  resource_id: string;
  grantee_type: string;
  grantee_id: string;
  granted_by: string;
  can_create: boolean;
  can_read: boolean;
  can_update: boolean;
  can_delete: boolean;
  can_evict: boolean;
  can_invoke: boolean;
  can_add: boolean;
  can_share: boolean;
  can_admin: boolean;
  requires_identity: boolean;
  read_requires_identity?: boolean | null;
  write_requires_identity?: boolean | null;
  invoke_requires_identity?: boolean | null;
  target_entity?: string | null;
  target_entity_type?: string | null;
  max_claims?: number | null;
  claims_count: number;
  state: string;
  name?: string | null;
  notes?: string | null;
  granted_at: string;
  expires_at?: string | null;
  revoked_at?: string | null;
  revoked_by?: string | null;
  accepted_at?: string | null;
  accepted_by?: string | null;
  created_time: string;
  modified_time: string;
  // Only present on invite creation response
  claim_token?: string;
}

export type Grant = GrantResponse;
