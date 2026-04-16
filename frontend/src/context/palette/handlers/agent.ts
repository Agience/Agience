import { extractInformation } from '../../../api/agent';
import { PaletteState } from '../palette.types';
import type { Artifact } from '../../workspace/workspace.types';

type AgentRunContext = {
  userId: string;
  workspaceId: string;
  selectedArtifactIds: string[];
};

function inferSourceAndArtifacts(selectedArtifactIds: string[]): {
  sourceArtifactId: string | null;
  contextArtifactIds: string[];
} {
  const ids = (selectedArtifactIds || []).map(String).filter(Boolean);
  const sourceArtifactId = ids[0] ?? null;
  const contextArtifactIds = sourceArtifactId ? ids.slice(1).filter((id) => id !== sourceArtifactId) : [];
  return { sourceArtifactId, contextArtifactIds };
}

export async function agentHandler(state: PaletteState, ctx: AgentRunContext): Promise<PaletteState> {
  const selectedTool = state.panelData.tools.tools?.[0] ?? 'extract_information';
  const toolName = String(selectedTool || '').trim() || 'extract_information';

  const outputArtifacts: Artifact[] = [];

  if (!ctx.workspaceId) {
    outputArtifacts.push({
      id: 'palette-error',
      content: 'No active workspace selected.',
      context: JSON.stringify({
        content_type: 'text/plain',
        type: 'palette-error',
        title: 'Palette error',
      }),
      state: 'draft',
    });
  } else if (toolName === 'extract_information') {
    const { sourceArtifactId, contextArtifactIds } = inferSourceAndArtifacts(ctx.selectedArtifactIds);
    if (!sourceArtifactId) {
      outputArtifacts.push({
        id: 'palette-error',
        content: 'Select one or more artifacts, then run Extract information.',
        context: JSON.stringify({
          content_type: 'text/plain',
          type: 'palette-error',
          title: 'Palette',
        }),
        state: 'draft',
      });
    } else {
      const result = await extractInformation(ctx.workspaceId, sourceArtifactId, contextArtifactIds);
      const created = Array.isArray(result.created_artifact_ids) ? result.created_artifact_ids.length : 0;
      const warning = result.warning ? `\n\nWarning: ${result.warning}` : '';

      outputArtifacts.push({
        id: 'palette-output',
        content:
          `Ran MCP tool: extract_information\n` +
          `source_artifact_id: ${sourceArtifactId}\n` +
          `context_artifact_ids: ${(contextArtifactIds || []).join(', ') || '(none)'}\n` +
          `created_artifacts: ${created}${warning}`,
        context: JSON.stringify({
          content_type: 'application/json',
          type: 'palette-output',
          title: 'Palette output',
          generatedBy: 'palette',
          tool: 'extract_information',
          workspace_id: ctx.workspaceId,
          source_artifact_id: sourceArtifactId,
          context_artifact_ids: contextArtifactIds,
          created_artifact_ids: result.created_artifact_ids,
        }),
        state: 'draft',
      });
    }
  } else {
    // Generic tool dispatch without a known server is ambiguous under the
    // artifact-invoke model. The tool name alone doesn't tell us which
    // server owns it. Tool discovery (e.g. search via discover_tools) should
    // return a server id; once the palette UI surfaces that, generic
    // invocation can POST /artifacts/{server_id}/invoke with body.name=tool.
    outputArtifacts.push({
      id: 'palette-output',
      content:
        `Generic operator dispatch without a server id is not supported yet.\n` +
        `Selected tool: ${toolName}.\n` +
        `Use extract_information, or pick a tool from tool discovery (which knows the owning server).`,
      context: JSON.stringify({
        content_type: 'text/plain',
        type: 'palette-error',
        title: 'Palette',
      }),
      state: 'draft',
    });
  }

  return {
    ...state,
    panelData: {
      ...state.panelData,
      output: {
        ...state.panelData.output,
        artifacts: outputArtifacts,
      },
    },
  };
}
