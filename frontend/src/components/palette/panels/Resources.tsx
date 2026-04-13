import { useCallback, useEffect, useMemo, useState } from 'react';
import { listWorkspaceMCPServers, type MCPResource } from '../../../api/mcp';
import { useWorkspaces } from '../../../context/workspaces/WorkspacesContext';
import CardGrid from '../../common/CardGrid';
import { Artifact } from '../../../context/workspace/workspace.types';
import { useWorkspace } from '../../../hooks/useWorkspace';
import { AGIENCE_DRAG_CONTENT_TYPE, getAgienceDragPayload, getDroppedArtifactIds, isAgienceDrag, setAgienceDragData } from '../../../dnd/agienceDrag';
import { usePalette } from '../../../hooks/usePalette';
import { PROMPTS_CONTENT_TYPE } from '@/utils/content-type';

export default function ResourcesPanel() {
  const { state, updatePanelData } = usePalette();
  const panelState = state.panelData.resources;
  const { artifacts: workspaceArtifacts } = useWorkspace();
  const { activeWorkspace } = useWorkspaces();
  const workspaceId = activeWorkspace?.id ?? '';

  const [dragDepth, setDragDepth] = useState(0);
  const [loading, setLoading] = useState(false);
  const [resources, setResources] = useState<Array<{ server: string; serverName?: string; resource: MCPResource }>>([]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!workspaceId) {
        setResources([]);
        return;
      }
      setLoading(true);
      try {
        const servers = await listWorkspaceMCPServers(workspaceId);
        if (cancelled) return;
        const flattened = servers.flatMap((s) =>
          (s.resources ?? []).map((r) => ({ server: s.server, serverName: s.name, resource: r }))
        );
        setResources(flattened);
      } catch (err) {
        if (!cancelled) {
          console.error('Failed to list workspace MCP resources', err);
          setResources([]);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  const selectedKeySet = useMemo(() => {
    return new Set(panelState.resources.map((r) => `${r.server}::${r.uri}`));
  }, [panelState.resources]);

  const toggleResource = useCallback(
    (server: string, resource: MCPResource, serverName?: string) => {
      const uri = resource.uri ?? '';
      if (!uri) return;

      updatePanelData('resources', (prev) => {
        const key = `${server}::${uri}`;
        const existing = prev.resources;
        const exists = existing.some((r) => `${r.server}::${r.uri}` === key);
        const updated = exists
          ? existing.filter((r) => `${r.server}::${r.uri}` !== key)
          : [
              ...existing,
              {
                server,
                serverName,
                uri,
                title: resource.title,
                contentType: resource.contentType ?? resource.content_type,
                resourceKind: resource.kind,
              },
            ];
        return { ...prev, resources: updated };
      });
    },
    [updatePanelData]
  );

  const onDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      if (!isAgienceDrag(e.dataTransfer)) return;
      e.preventDefault();
      e.stopPropagation();
      setDragDepth(0);

      const payload = getAgienceDragPayload(e.dataTransfer);
      if (payload?.kind === 'resource') {
        const found = resources.find(
          (r) => r.server === payload.server && (r.resource.uri ?? '') === payload.uri
        );
        if (found) toggleResource(found.server, found.resource, found.serverName);
        return;
      }

      if (payload?.kind === 'prompt' && payload.body) {
        const id = typeof crypto !== 'undefined' && 'randomUUID' in crypto
          ? `bundle-${crypto.randomUUID()}`
          : `bundle-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const newArtifact: Artifact = {
          id,
          context: JSON.stringify({
            content_type: PROMPTS_CONTENT_TYPE,
            type: 'prompt',
            title: payload.name ? `Bundle • ${payload.name}` : 'Bundle • Prompt',
            prompt: {
              prompt_id: payload.prompt_id,
              name: payload.name,
              body_content_type: payload.contentType,
            },
          }),
          content: payload.body,
          state: 'committed',
        };
        updatePanelData('resources', (prev) => ({
          ...prev,
          artifacts: [...(prev.artifacts ?? []), newArtifact],
        }));
        return;
      }

      if (payload?.kind === 'text' && payload.text) {
        const id = typeof crypto !== 'undefined' && 'randomUUID' in crypto
          ? `bundle-${crypto.randomUUID()}`
          : `bundle-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const newArtifact: Artifact = {
          id,
          context: JSON.stringify({
            content_type: 'text/plain',
            type: 'text',
            title: 'Bundle • Text',
          }),
          content: payload.text,
          state: 'committed',
        };
        updatePanelData('resources', (prev) => ({
          ...prev,
          artifacts: [...(prev.artifacts ?? []), newArtifact],
        }));
        return;
      }

      const ids = getDroppedArtifactIds(e.dataTransfer);
      if (ids.length) {
        updatePanelData('resources', (prev) => {
          const existingIds = new Set((prev.artifacts ?? []).map((c) => String(c.id)));
          const toAdd = ids
            .map((id) => workspaceArtifacts.find((c) => String(c.id) === String(id)))
            .filter((c): c is Artifact => !!c)
            .filter((c) => !existingIds.has(String(c.id)));
          if (!toAdd.length) return prev;
          return { ...prev, artifacts: [...(prev.artifacts ?? []), ...toAdd] };
        });
      }
    },
    [resources, toggleResource, updatePanelData, workspaceArtifacts]
  );

  const onDragEnter = useCallback((e: React.DragEvent) => {
    if (!e.dataTransfer?.types?.includes(AGIENCE_DRAG_CONTENT_TYPE)) return;
    e.preventDefault();
    setDragDepth((prev) => prev + 1);
  }, []);

  const onDragLeave = useCallback((e: React.DragEvent) => {
    if (!e.dataTransfer?.types?.includes(AGIENCE_DRAG_CONTENT_TYPE)) return;
    e.preventDefault();
    setDragDepth((prev) => Math.max(prev - 1, 0));
  }, []);

  const isDragActive = dragDepth > 0;

  return (
    <div
      className="w-full"
      onDrop={onDrop}
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDragOver={(e) => e.preventDefault()}
    >
      <div
        className={
          `mb-2 overflow-hidden rounded border bg-white transition-all duration-150 ` +
          (isDragActive ? 'max-h-24 p-2 ring-2 ring-blue-500' : 'max-h-0 p-0 border-transparent')
        }
        aria-hidden={!isDragActive}
      >
        <div className="text-sm text-gray-600">Drop artifacts / resources / text here to add bundles</div>
      </div>

      {(panelState.artifacts?.length ?? 0) > 0 && (
        <div className="mb-2 rounded border bg-white p-2">
          <div className="text-xs font-semibold text-gray-600 mb-1">Bundle artifacts</div>
          <CardGrid
            artifacts={panelState.artifacts}
            selectable={false}
            draggable={true}
            editable={false}
            onRemove={(artifact) => {
              updatePanelData('resources', (prev) => ({
                ...prev,
                artifacts: (prev.artifacts ?? []).filter((c) => String(c.id) !== String(artifact.id)),
              }));
            }}
          />
        </div>
      )}

      <div className="max-h-40 overflow-y-auto">
        {!workspaceId && (
          <div className="px-2 py-1 text-sm text-gray-500">Select a workspace to view resources.</div>
        )}
        {workspaceId && loading && (
          <div className="px-2 py-1 text-sm text-gray-500">Loading resources…</div>
        )}
        {workspaceId && !loading && resources.length === 0 && (
          <div className="px-2 py-1 text-sm text-gray-500">No resources available.</div>
        )}

        {resources.map(({ server, serverName, resource }) => {
          const uri = resource.uri ?? '';
          const title = resource.title ?? resource.id ?? uri;
          const key = `${server}::${uri}`;
          const checked = !!uri && selectedKeySet.has(key);

          return (
            <div
              key={key}
              draggable={!!uri}
              onDragStart={(e) => {
                if (!uri) return;
                setAgienceDragData(e.dataTransfer, {
                  kind: 'resource',
                  server,
                  uri,
                  title,
                  contentType: resource.contentType ?? resource.content_type,
                  resourceKind: resource.kind,
                });
                e.dataTransfer.effectAllowed = 'copy';
              }}
              className="flex justify-between items-center px-2 py-1 hover:bg-gray-100 rounded"
              title={`${server} • ${uri}`}
            >
              <span className="truncate">{title}</span>
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  className="sr-only peer"
                  checked={checked}
                  onChange={() => toggleResource(server, resource, serverName)}
                />
                <div className="w-8 h-5 bg-gray-200 peer-checked:bg-blue-500 rounded-full transition-all duration-100" />
                <div className="absolute left-0.5 top-0.5 w-4 h-4 bg-white rounded-full shadow transform peer-checked:translate-x-3 transition-transform duration-100" />
              </label>
            </div>
          );
        })}
      </div>
    </div>
  );
}
