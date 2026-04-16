/**
 * api/agent.ts — server-tool invocation helpers.
 *
 * All tool invocations go through the canonical
 * ``POST /artifacts/{server_id}/invoke`` path. The MCP server artifact's
 * ``invoke`` operation (declared in ``vnd.agience.mcp-server+json/type.json``)
 * resolves ``body.name`` as the tool to call on that server.
 *
 * Identity is always derived server-side from the auth token.
 */

import { post } from './api';

// ──────────────────────────────────────────────────────────────────────────────
// Demo data
// ──────────────────────────────────────────────────────────────────────────────

interface LoadDemoDataRequest {
  topics?: string[];
  num_workspaces?: number;
  artifacts_per_workspace?: number;
  include_agience_guide?: boolean;
}

interface LoadDemoDataResponse {
  workspaces_created: number;
  workspace_ids: string[];
  workspace_artifacts_created: number;
  agience_guide_added: boolean;
  message: string;
}

/**
 * Generate AI-powered demo data.
 *
 * Dispatches to Verso's ``load_demo_data`` tool (demo-data authoring
 * lives in the reasoning server since it needs the LLM to synthesize
 * content).
 */
export async function loadDemoData(request: LoadDemoDataRequest): Promise<LoadDemoDataResponse> {
  return post<LoadDemoDataResponse, unknown>(
    `/artifacts/verso/invoke`,
    {
      name: 'load_demo_data',
      arguments: {
        topics: request.topics,
        num_workspaces: request.num_workspaces,
        artifacts_per_workspace: request.artifacts_per_workspace,
        include_agience_guide: request.include_agience_guide,
      },
    },
  );
}

// ──────────────────────────────────────────────────────────────────────────────
// Information extraction
// ──────────────────────────────────────────────────────────────────────────────

export interface ExtractUnitsResponse {
  workspace_id: string;
  source_artifact_id: string;
  created_artifact_ids: string[];
  unit_count?: number;
  warning?: string;
}

/**
 * Extract structured information units from an artifact.
 *
 * Dispatches to Aria's ``extract_units`` tool. Creates new workspace
 * artifacts for each extracted unit; callers should refresh workspace
 * artifacts after this returns.
 */
export async function extractUnits(
  workspace_id: string,
  source_artifact_id: string,
  artifact_artifact_ids?: string[]
): Promise<ExtractUnitsResponse> {
  return post<ExtractUnitsResponse, unknown>(
    `/artifacts/aria/invoke`,
    {
      name: 'extract_units',
      workspace_id,
      arguments: {
        workspace_id,
        source_artifact_id,
        artifact_artifact_ids: artifact_artifact_ids ?? [],
      },
    },
  );
}

/** Alias for {@link extractUnits} — preferred name for content-type handlers. */
export const extractInformation = extractUnits;
