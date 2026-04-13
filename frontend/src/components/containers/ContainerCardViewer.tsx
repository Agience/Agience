import { useEffect, useMemo, useState } from 'react';
import type { Artifact } from '@/context/workspace/workspace.types';
import { getContentType, type ViewMode } from '@/registry/content-types';
import { listWorkspaceMCPServers } from '@/api/mcp';
import type { MCPServerConfig, MCPServerInfo, MCPResource } from '@/api/mcp';
import { useWorkspaces } from '@/hooks/useWorkspaces';
import { cn } from '@/lib/utils';
import ServerIcon from '@/components/icons/ServerIcon';
import { useWorkspace } from '@/hooks/useWorkspace';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { LayoutGrid, LayoutList, MoreHorizontal } from 'lucide-react';

interface ContainerCardViewerProps {
  artifact: Artifact;
  /**
   * Explicit view mode override.
   * - Preview pane passes "tree" to emphasize hierarchy
   * - Floating windows omit this so the artifact/content-type default applies (typically "grid")
   */
  mode?: ViewMode;
  onOpenCollection?: (collectionId: string) => void;
  onAssignToCollection?: (collectionId: string, dataTransfer: DataTransfer) => void;
}

/**
 * ContainerCardViewer
 *
 * Minimal MVP viewer for container artifacts (Resources, Tools, Prompts).
 * Reuses the visual language of the sidebar tree sections so that
 * workspace artifacts like "Resources", "Prompts", and "Tools" feel
 * consistent with the left rail.
 */
function useContainerMcpData() {
  const { activeWorkspaceId } = useWorkspaces();
  const [infos, setInfos] = useState<Record<string, MCPServerInfo>>({});

  // Derive server configs from live info
  const servers: MCPServerConfig[] = useMemo(
    () => Object.entries(infos).map(([id, info]) => ({ id, label: info.name || id, icon: info.icon })),
    [infos]
  );

  // Fetch per-workspace server info when workspace changes
  useEffect(() => {
    if (!activeWorkspaceId) {
      setInfos({});
      return;
    }
    listWorkspaceMCPServers(activeWorkspaceId)
      .then((list) => {
        const map: Record<string, MCPServerInfo> = {};
        list.forEach((info) => {
          map[info.server] = info;
        });
        setInfos(map);
      })
      .catch((err) => console.error('ContainerCardViewer: listWorkspaceMCPServers failed', err));
  }, [activeWorkspaceId]);

  return { servers, infos, activeWorkspaceId };
}

export default function ContainerCardViewer({ artifact, mode, onOpenCollection, onAssignToCollection }: ContainerCardViewerProps) {
  const contentType = useMemo(() => getContentType(artifact), [artifact]);
  const { updateArtifact } = useWorkspace();
  const { servers, infos } = useContainerMcpData();

  // Derive persisted layout from artifact.context (artifact-defined behavior)
  const layout = useMemo(() => {
    try {
      const raw = typeof artifact.context === 'string' ? JSON.parse(artifact.context || '{}') : (artifact.context || {});
      const rawLayout = (raw && typeof raw.layout === 'object' ? raw.layout : {}) as {
        default_mode?: string;
        group_by?: string;
      };
      return {
        defaultMode: rawLayout.default_mode || contentType.defaultMode || 'grid',
        groupBy: rawLayout.group_by || (contentType.containerVariant === 'resources' ? 'server' : undefined),
      };
    } catch {
      return {
        defaultMode: contentType.defaultMode || 'grid',
        groupBy: contentType.containerVariant === 'resources' ? 'server' : undefined,
      };
    }
  }, [artifact.context, contentType]);

  // Preview explicitly passes mode="tree"; floating windows omit mode so they
  // use the artifact/content-type default (usually "grid"). We still surface the
  // artifact-defined defaultMode in the header and allow changing it via the
  // dropdown so layout is persisted in artifact.context.
  const effectiveMode = (mode as ViewMode | undefined) ?? (layout.defaultMode as ViewMode);

  if (!contentType.isContainer) {
    return null;
  }

  const isTreeMode = effectiveMode === 'tree';

  // Container viewer header + mode dropdown; body switches between tree and grid
  // representations of the same underlying container contents.
  return (
    <div className="h-full w-full flex flex-col bg-white">
      <div className="px-4 py-2 border-b flex items-center justify-between">
        <div className="flex items-center gap-2">
          <contentType.icon size={16} />
          <div className="text-sm font-semibold text-gray-900 truncate">
            {contentType.label}
          </div>
        </div>
        <div className="flex items-center gap-1 text-xs text-gray-500">
          <span className="capitalize">{effectiveMode}</span>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-700"
                aria-label="Container layout options"
              >
                <MoreHorizontal className="w-4 h-4" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[10rem]">
              <DropdownMenuLabel>View layout</DropdownMenuLabel>
              <DropdownMenuItem
                onClick={() =>
                  handlePersistLayout(artifact, updateArtifact, { default_mode: 'tree' })
                }
              >
                <LayoutList className="w-4 h-4" />
                <span className="ml-1">Tree (preview)</span>
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() =>
                  handlePersistLayout(artifact, updateArtifact, { default_mode: 'grid' })
                }
              >
                <LayoutGrid className="w-4 h-4" />
                <span className="ml-1">Grid (desktop)</span>
              </DropdownMenuItem>
              {contentType.containerVariant === 'resources' && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuLabel>Group</DropdownMenuLabel>
                  <DropdownMenuItem
                    onClick={() =>
                      handlePersistLayout(artifact, updateArtifact, { group_by: 'server' })
                    }
                  >
                    <span className="ml-1">By server (default)</span>
                  </DropdownMenuItem>
                  {/* Future: additional group_by modes (collection, type, time, author) */}
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2">
        {isTreeMode ? (
          <>
            {contentType.containerVariant === 'resources' && (
              <ResourcesTree servers={servers} infos={infos} onOpenCollection={onOpenCollection} onAssignToCollection={onAssignToCollection} />
            )}
            {contentType.containerVariant === 'tools' && (
              <ToolsTree servers={servers} infos={infos} />
            )}
            {contentType.containerVariant === 'prompts' && (
              <PromptsTree servers={servers} infos={infos} />
            )}

            {contentType.id !== 'resources' &&
              contentType.id !== 'tools' &&
              contentType.id !== 'prompts' && (
                <div className="text-xs text-gray-500">
                  Container type <span className="font-mono">{contentType.id}</span> –
                  detailed view coming soon.
                </div>
              )}
          </>
        ) : (
          <>
            {contentType.containerVariant === 'resources' && (
              <ResourcesGrid servers={servers} infos={infos} onOpenCollection={onOpenCollection} onAssignToCollection={onAssignToCollection} />
            )}
            {contentType.containerVariant === 'tools' && (
              <ToolsGrid servers={servers} infos={infos} />
            )}
            {contentType.containerVariant === 'prompts' && (
              <PromptsGrid servers={servers} infos={infos} />
            )}

            {contentType.id !== 'resources' &&
              contentType.id !== 'tools' &&
              contentType.id !== 'prompts' && (
                <div className="text-xs text-gray-500">
                  Grid view for container type <span className="font-mono">{contentType.id}</span> is not yet
                  implemented.
                </div>
              )}
          </>
        )}
      </div>
    </div>
  );
}

// --- Trees ---------------------------------------------------------------

interface TreeProps {
  servers: MCPServerConfig[];
  infos: Record<string, MCPServerInfo>;
}

interface ResourcesTreeProps extends TreeProps {
  onOpenCollection?: (collectionId: string) => void;
  onAssignToCollection?: (collectionId: string, dataTransfer: DataTransfer) => void;
}

// Persist layout changes back into the artifact context so that containers can
// fully own their own view behavior (MVP: default_mode + group_by).
function handlePersistLayout(
  artifact: Artifact,
  updateArtifact: (patch: Partial<Artifact>) => Promise<void>,
  layoutPatch: { default_mode?: string; group_by?: string }
) {
  try {
    const raw = typeof artifact.context === 'string' ? JSON.parse(artifact.context || '{}') : (artifact.context || {});
    const nextLayout = {
      ...(typeof raw.layout === 'object' && raw.layout ? raw.layout : {}),
      ...layoutPatch,
    };
    const nextCtx = {
      ...raw,
      layout: nextLayout,
    };
    void updateArtifact({ id: artifact.id, context: JSON.stringify(nextCtx) });
  } catch (err) {
    console.error('ContainerCardViewer: failed to persist layout', err);
  }
}

function ResourcesTree({ servers, infos, onOpenCollection, onAssignToCollection }: ResourcesTreeProps) {
  // Build the same "by-collection" tree the sidebar shows.
  const grouped = useMemo(() => {
    const byServer = new Map<
      string,
      {
        serverId: string;
        serverName: string;
        icon?: string;
        collections: Array<{
          id: string;
          name: string;
          description?: string;
        }>;
      }
    >();

    Object.entries(infos).forEach(([serverId, info]) => {
      const cfg = servers.find((s) => s.id === serverId);
      const serverName = cfg?.label || serverId;
      const serverIcon = info.icon || cfg?.icon;

      (info.resources || []).forEach((res: MCPResource) => {
        const isCollection = res.kind === 'collection' || res.kind === 'agience.collection';
        if (!isCollection) return;
        let resourceId = res.id || res.uri || '';
        if (typeof resourceId === 'string' && resourceId.startsWith('collection:')) {
          resourceId = resourceId.substring('collection:'.length);
        }
        const bucketKey = serverId;
        let bucket = byServer.get(bucketKey);
        if (!bucket) {
          bucket = {
            serverId,
            serverName,
            icon: serverIcon,
            collections: [],
          };
          byServer.set(bucketKey, bucket);
        }
        bucket.collections.push({
          id: String(resourceId),
          name: res.title || res.id || 'Untitled',
          description: res.text,
        });
      });
    });

    // Stable ordering by server name then collection name
    const serversArr = Array.from(byServer.values()).sort((a, b) =>
      a.serverName.localeCompare(b.serverName)
    );
    serversArr.forEach((bucket) => {
      bucket.collections.sort((a, b) => a.name.localeCompare(b.name));
    });
    return serversArr;
  }, [servers, infos]);

  if (grouped.length === 0) {
    return <EmptyTreeState label="No collections available" />;
  }

  return (
    <div className="ml-3 border-l border-border">
      {grouped.map((bucket) => (
        <div key={bucket.serverId} className="mb-1">
          <div className="flex items-center gap-2 px-2 h-6 text-xs text-muted-foreground font-medium">
            <ServerIcon
              icon={bucket.icon}
              type="resources"
              size={14}
              className="flex-shrink-0"
            />
            <span className="truncate">{bucket.serverName}</span>
          </div>
          {bucket.collections.map((c) => (
            <div
              key={`${bucket.serverId}-${c.id}`}
              className={cn(
                'pl-6 pr-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r'
              )}
              title={c.description || c.name}
              onDragOver={(e) => {
                if (!onAssignToCollection) return;
                e.preventDefault();
                e.stopPropagation();
                if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
              }}
              onDrop={(e) => {
                if (!onAssignToCollection) return;
                e.preventDefault();
                e.stopPropagation();
                onAssignToCollection(c.id, e.dataTransfer);
              }}
              onClick={() => onOpenCollection?.(c.id)}
            >
              <span className="text-sm truncate flex-1">{c.name}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function ResourcesGrid({ servers, infos, onOpenCollection, onAssignToCollection }: ResourcesTreeProps) {
  const items = useMemo(
    () =>
      buildResourcesCollections(servers, infos).map((entry) => ({
        id: `${entry.serverId}:${entry.id}`,
        collectionId: entry.id,
        serverName: entry.serverName,
        name: entry.name,
        description: entry.description,
        icon: entry.icon,
      })),
    [servers, infos]
  );

  if (items.length === 0) {
    return <EmptyTreeState label="No collections available" />;
  }

  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3">
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          className="flex flex-col items-stretch text-left border border-gray-200 rounded-lg px-3 py-2 bg-white shadow-sm hover:shadow cursor-pointer transition-shadow"
          onDragOver={(e) => {
            if (!onAssignToCollection) return;
            e.preventDefault();
            e.stopPropagation();
            if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
          }}
          onDrop={(e) => {
            if (!onAssignToCollection) return;
            e.preventDefault();
            e.stopPropagation();
            onAssignToCollection(item.collectionId, e.dataTransfer);
          }}
          onClick={() => onOpenCollection?.(item.collectionId)}
        >
          <div className="flex items-center gap-2 mb-1 text-xs text-gray-500">
            <ServerIcon icon={item.icon} type="resources" size={14} className="flex-shrink-0" />
            <span className="truncate">{item.serverName}</span>
          </div>
          <div className="text-sm font-medium text-gray-900 truncate" title={item.name}>
            {item.name}
          </div>
          {item.description && (
            <div className="mt-1 text-[11px] text-gray-500 line-clamp-2" title={item.description}>
              {item.description}
            </div>
          )}
        </button>
      ))}
    </div>
  );
}

function ToolsTree({ servers, infos }: TreeProps) {
  const serverRows = useMemo(() => buildServerToolsRows(servers, infos), [servers, infos]);

  if (serverRows.length === 0) {
    return <EmptyTreeState label="No MCP servers configured" />;
  }

  return (
    <div className="ml-3 border-l border-border">
      {serverRows.map((s) => (
        <div
          key={s.id}
          className="px-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r"
        >
          <ServerIcon icon={s.icon} type="tool" size={16} className="text-muted-foreground flex-shrink-0" />
          <span className="text-sm truncate flex-1">{s.name}</span>
          {s.toolCount > 0 && (
            <span className="text-[10px] text-muted-foreground font-medium">{s.toolCount}</span>
          )}
        </div>
      ))}
    </div>
  );
}

function ToolsGrid({ servers, infos }: TreeProps) {
  const serverRows = useMemo(() => buildServerToolsRows(servers, infos), [servers, infos]);

  if (serverRows.length === 0) {
    return <EmptyTreeState label="No MCP servers configured" />;
  }

  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3">
      {serverRows.map((s) => (
        <div
          key={s.id}
          className="flex flex-col border border-gray-200 rounded-lg px-3 py-2 bg-white shadow-sm"
        >
          <div className="flex items-center gap-2 mb-1">
            <ServerIcon
              icon={s.icon}
              type="tool"
              size={16}
              className="text-muted-foreground flex-shrink-0"
            />
            <span className="text-sm font-medium truncate">{s.name}</span>
          </div>
          <div className="text-[11px] text-muted-foreground">
            {s.toolCount === 1 ? '1 tool' : `${s.toolCount} tools`}
          </div>
        </div>
      ))}
    </div>
  );
}

function PromptsTree({ servers, infos }: TreeProps) {
  const serverRows = useMemo(() => buildServerPromptsRows(servers, infos), [servers, infos]);

  if (serverRows.length === 0) {
    return <EmptyTreeState label="No MCP servers configured" />;
  }

  return (
    <div className="ml-3 border-l border-border">
      {serverRows.map((s) => (
        <div
          key={s.id}
          className="px-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r"
        >
          <ServerIcon icon={s.icon} type="prompt" size={16} className="text-muted-foreground flex-shrink-0" />
          <span className="text-sm truncate flex-1">{s.name}</span>
          {s.promptCount > 0 && (
            <span className="text-[10px] text-muted-foreground font-medium">{s.promptCount}</span>
          )}
        </div>
      ))}
    </div>
  );
}

function PromptsGrid({ servers, infos }: TreeProps) {
  const serverRows = useMemo(() => buildServerPromptsRows(servers, infos), [servers, infos]);

  if (serverRows.length === 0) {
    return <EmptyTreeState label="No MCP servers configured" />;
  }

  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3">
      {serverRows.map((s) => (
        <div
          key={s.id}
          className="flex flex-col border border-gray-200 rounded-lg px-3 py-2 bg-white shadow-sm"
        >
          <div className="flex items-center gap-2 mb-1">
            <ServerIcon
              icon={s.icon}
              type="prompt"
              size={16}
              className="text-muted-foreground flex-shrink-0"
            />
            <span className="text-sm font-medium truncate">{s.name}</span>
          </div>
          <div className="text-[11px] text-muted-foreground">
            {s.promptCount === 1 ? '1 prompt' : `${s.promptCount} prompts`}
          </div>
        </div>
      ))}
    </div>
  );
}

function EmptyTreeState({ label }: { label: string }) {
  return (
    <div className="px-2 h-7 text-sm text-muted-foreground flex items-center">
      {label}
    </div>
  );
}

// --- Shared builders for tree + grid views -------------------------------

function buildResourcesCollections(servers: MCPServerConfig[], infos: Record<string, MCPServerInfo>) {
  const out: Array<{
    serverId: string;
    serverName: string;
    icon?: string;
    id: string;
    name: string;
    description?: string;
  }> = [];

  Object.entries(infos).forEach(([serverId, info]) => {
    const cfg = servers.find((s) => s.id === serverId);
    const serverName = cfg?.label || serverId;
    const serverIcon = info.icon || cfg?.icon;

    (info.resources || []).forEach((res: MCPResource) => {
      const isCollection = res.kind === 'collection' || res.kind === 'agience.collection';
      if (!isCollection) return;
      let resourceId = res.id || res.uri || '';
      if (typeof resourceId === 'string' && resourceId.startsWith('collection:')) {
        resourceId = resourceId.substring('collection:'.length);
      }
      out.push({
        serverId,
        serverName,
        icon: serverIcon,
        id: String(resourceId),
        name: res.title || res.id || 'Untitled',
        description: res.text,
      });
    });
  });

  // Stable ordering by server then name
  out.sort((a, b) => {
    const s = a.serverName.localeCompare(b.serverName);
    if (s !== 0) return s;
    return a.name.localeCompare(b.name);
  });

  return out;
}

function buildServerToolsRows(servers: MCPServerConfig[], infos: Record<string, MCPServerInfo>) {
  const ids = Array.from(new Set([...servers.map((s) => s.id), ...Object.keys(infos)]));
  return ids.map((id) => {
    const info = infos[id];
    const cfg = servers.find((s) => s.id === id);
    const toolCount = info?.tools?.length || 0;
    return {
      id,
      name: cfg?.label || id,
      icon: info?.icon,
      toolCount,
    };
  });
}

function buildServerPromptsRows(servers: MCPServerConfig[], infos: Record<string, MCPServerInfo>) {
  const ids = Array.from(new Set([...servers.map((s) => s.id), ...Object.keys(infos)]));
  return ids.map((id) => {
    const info = infos[id];
    const cfg = servers.find((s) => s.id === id);
    const promptCount = info?.prompts?.length || 0;
    return {
      id,
      name: cfg?.label || id,
      icon: info?.icon,
      promptCount,
    };
  });
}

// Lightweight tree-only preview for Artifact Preview tiles.
// Renders the same Resources/Tools/Prompts trees used by containers/sidebar
// without the ContainerCardViewer header/chrome.
export function ContainerTreePreview({ artifact }: { artifact: Artifact }) {
  const contentType = useMemo(() => getContentType(artifact), [artifact]);
  const { servers, infos } = useContainerMcpData();

  if (!contentType.isContainer) return null;

  return (
    <>
      {contentType.containerVariant === 'resources' && (
        <ResourcesTree servers={servers} infos={infos} />
      )}
      {contentType.containerVariant === 'tools' && (
        <ToolsTree servers={servers} infos={infos} />
      )}
      {contentType.containerVariant === 'prompts' && (
        <PromptsTree servers={servers} infos={infos} />
      )}
    </>
  );
}
