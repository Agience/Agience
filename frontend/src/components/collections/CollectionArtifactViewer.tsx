import { useCallback, useEffect, useMemo, useState } from 'react';
import { LayoutGrid, List } from 'lucide-react';

import CardGrid from '@/components/common/CardGrid';
import CardList from '@/components/common/CardList';
import { addArtifactToCollection, listCollectionArtifacts, removeArtifactFromCollection, subscribeCollectionEvents } from '@/api/collections';
import { getDroppedArtifactIds } from '@/dnd/agienceDrag';
import { useWorkspace } from '@/hooks/useWorkspace';
import type { Artifact } from '@/context/workspace/workspace.types';
import { getStableArtifactId } from '@/utils/artifact-identifiers';

type CollectionViewMode = 'grid' | 'list';

function parseDragPayload(dt: DataTransfer) {
  const parseRaw = (raw: string) => {
    if (!raw) return null;
    try {
      return JSON.parse(raw) as {
        ids?: unknown;
        versionIds?: unknown;
        sourceType?: unknown;
        workspaceId?: unknown;
        sourceWorkspaceId?: unknown;
      };
    } catch {
      return null;
    }
  };

  const fromCustom = parseRaw(dt.getData('application/x-agience-artifact'));
  const fromJson = parseRaw(dt.getData('application/json'));
  const payload = fromCustom ?? fromJson;
  const droppedIds = getDroppedArtifactIds(dt);

  if (payload && Array.isArray(payload.ids)) {
    return {
      ids: payload.ids.map(String).filter(Boolean),
      versionIds: Array.isArray(payload.versionIds) ? payload.versionIds.map(String).filter(Boolean) : [],
      sourceType: typeof payload.sourceType === 'string' ? payload.sourceType : undefined,
      sourceWorkspaceId:
        typeof payload.workspaceId === 'string'
          ? payload.workspaceId
          : typeof payload.sourceWorkspaceId === 'string'
            ? payload.sourceWorkspaceId
            : undefined,
    };
  }

  return {
    ids: droppedIds,
      versionIds: [],
    sourceType: undefined,
    sourceWorkspaceId: undefined,
  };
}

export default function CollectionArtifactViewer({
  artifact,
  mode,
  onOpenArtifact,
}: {
  artifact: Artifact;
  mode?: string;
  onOpenArtifact?: (artifact: Artifact) => void;
}) {
  const { artifacts: workspaceArtifacts } = useWorkspace();

  // A collection artifact IS the collection. Its id is the container_id
  // for the list API. No prefix decoding or context lookups needed.
  const collectionId = useMemo(() => artifact.id ?? '', [artifact.id]);

  const [committedItems, setCommittedItems] = useState<Artifact[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [viewMode, setViewMode] = useState<CollectionViewMode>(mode === 'list' ? 'list' : 'grid');
  const [dropActive, setDropActive] = useState(false);

  useEffect(() => {
    setViewMode(mode === 'list' ? 'list' : 'grid');
  }, [mode]);

  const reloadArtifacts = useCallback(async () => {
    if (!collectionId) {
      setCommittedItems([]);
      return;
    }

    setIsLoading(true);
    try {
      const next = await listCollectionArtifacts(collectionId);
      setCommittedItems(next as Artifact[]);
    } catch (error) {
      console.error('Failed to load collection artifacts:', error);
      setCommittedItems([]);
    } finally {
      setIsLoading(false);
    }
  }, [collectionId]);

  useEffect(() => {
    void reloadArtifacts();
  }, [reloadArtifacts]);

  // Subscribe to real-time collection change events via SSE
  useEffect(() => {
    if (!collectionId) return;

    const unsubscribe = subscribeCollectionEvents(collectionId, {
      onArtifactCreated: (artifact) => {
        setCommittedItems(prev => {
          const key = String((artifact as Artifact).root_id ?? artifact.id);
          if (prev.some(c => String(c.root_id ?? c.id) === key)) return prev;
          return [...prev, artifact as Artifact];
        });
      },
      onArtifactUpdated: (artifact) => {
        setCommittedItems(prev =>
          prev.map(c => {
            const cKey = String(c.root_id ?? c.id);
            const aKey = String((artifact as Artifact).root_id ?? artifact.id);
            return cKey === aKey ? { ...c, ...artifact } as Artifact : c;
          })
        );
      },
      onArtifactDeleted: (artifactId) => {
        setCommittedItems(prev =>
          prev.filter(c => String(c.root_id ?? c.id) !== String(artifactId))
        );
      },
      onCollectionRefreshed: () => {
        void reloadArtifacts();
      },
    });

    return unsubscribe;
  }, [collectionId, reloadArtifacts]);

  const pendingWorkspaceItems = useMemo(() => {
    if (!collectionId) return [] as Artifact[];

    return workspaceArtifacts.filter((candidate) => {
      const memberIds = Array.isArray(candidate.committed_collection_ids)
        ? candidate.committed_collection_ids.map(String)
        : [];

      return memberIds.includes(collectionId);
    });
  }, [collectionId, workspaceArtifacts]);

  const items = useMemo(() => {
    const byId = new Map<string, Artifact>();

    const addItem = (candidate: Artifact) => {
      const key = String(candidate.root_id ?? getStableArtifactId(candidate) ?? '');
      if (!key) return;
      if (!byId.has(key)) byId.set(key, candidate);
    };

    committedItems.forEach(addItem);
    pendingWorkspaceItems.forEach(addItem);

    return Array.from(byId.values());
  }, [committedItems, pendingWorkspaceItems]);

  const handleRemove = useCallback(async (item: Artifact) => {
    if (!collectionId) return;

    const rootId = String(item.root_id ?? item.id ?? '').trim();
    if (!rootId) return;
    await removeArtifactFromCollection(collectionId, rootId);
    setCommittedItems((prev) => prev.filter((candidate) => String(candidate.root_id ?? candidate.id ?? '') !== rootId));
  }, [collectionId]);

  const applyDroppedArtifacts = useCallback(async (
    ids: string[],
    payload?: { sourceType?: string; workspaceId?: string; versionIds?: string[] },
  ) => {
    if (!collectionId || !ids.length) return;

    const versionIds = Array.isArray(payload?.versionIds)
      ? payload.versionIds.map(String).filter(Boolean)
      : [];
    const idsToAdd = (versionIds.length > 0 ? versionIds : ids)
      .filter((draggedId) => String(draggedId) !== String(collectionId));

    for (const draggedId of idsToAdd) {
      await addArtifactToCollection(collectionId, draggedId);
    }
    await reloadArtifacts();
  }, [collectionId, reloadArtifacts]);

  const handleDropIntoCollection = useCallback(async (dt: DataTransfer) => {
    if (!collectionId) return;
    const { ids, versionIds, sourceType, sourceWorkspaceId } = parseDragPayload(dt);
    if (!ids.length) return;
    await applyDroppedArtifacts(ids, {
      sourceType,
      workspaceId: sourceWorkspaceId,
      versionIds,
    });
  }, [applyDroppedArtifacts, collectionId]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    const hasArtifactPayload =
      getDroppedArtifactIds(e.dataTransfer).length > 0 ||
      Array.from(e.dataTransfer.types ?? []).includes('application/x-agience-artifact');
    if (!hasArtifactPayload) return;
    e.preventDefault();
    e.stopPropagation();
    setDropActive(true);
    try {
      e.dataTransfer.dropEffect = 'move';
    } catch {
      // ignore
    }
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    const current = e.currentTarget as HTMLElement;
    const related = e.relatedTarget as Node | null;
    if (!related || !current.contains(related)) {
      setDropActive(false);
    }
  }, []);

  const handleDrop = useCallback(async (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDropActive(false);
    await handleDropIntoCollection(e.dataTransfer);
  }, [handleDropIntoCollection]);

  return (
    <div
      className={`h-full min-h-0 bg-white flex flex-col ${dropActive ? 'ring-2 ring-blue-500 ring-inset' : ''}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      <div className="flex items-center justify-between border-b border-gray-200 bg-gray-50 px-4 py-2">
        <div className="text-xs text-gray-500">
          {isLoading ? 'Loading artifacts...' : `${items.length} artifact${items.length === 1 ? '' : 's'}`}
        </div>
        <div className="inline-flex items-center rounded-md border border-gray-300 bg-white p-0.5 shadow-sm">
          <button
            type="button"
            className={`inline-flex h-7 w-7 items-center justify-center rounded ${viewMode === 'grid' ? 'bg-blue-600 text-white' : 'text-gray-600 hover:bg-gray-100'}`}
            aria-label="Grid view"
            onClick={() => setViewMode('grid')}
          >
            <LayoutGrid className="h-4 w-4" />
          </button>
          <button
            type="button"
            className={`inline-flex h-7 w-7 items-center justify-center rounded ${viewMode === 'list' ? 'bg-blue-600 text-white' : 'text-gray-600 hover:bg-gray-100'}`}
            aria-label="List view"
            onClick={() => setViewMode('list')}
          >
            <List className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div className="flex-1 min-h-0">
        {viewMode === 'grid' ? (
          <div className="h-full overflow-auto px-4 py-4">
            <CardGrid
              artifacts={items}
              selectable
              draggable
              fillHeight
              activeSource={collectionId ? { type: 'collection', id: collectionId } : null}
              onOpenArtifact={onOpenArtifact}
              onRemove={handleRemove}
              onArtifactDrop={(_insertIndex, draggedIds, dragPayload) => {
                void applyDroppedArtifacts(draggedIds, {
                  sourceType: dragPayload?.sourceType,
                  workspaceId: dragPayload?.workspaceId,
                  versionIds: dragPayload?.versionIds,
                });
              }}
            />
          </div>
        ) : (
          <CardList
            artifacts={items}
            selectable
            draggable
            sourceCollectionId={collectionId || undefined}
            onOpenArtifact={onOpenArtifact}
            onRemove={handleRemove}
            onArtifactDrop={(_insertIndex, draggedIds, dragPayload) => {
              void applyDroppedArtifacts(draggedIds, {
                sourceType: dragPayload?.sourceType,
                workspaceId: dragPayload?.workspaceId,
                versionIds: dragPayload?.versionIds,
              });
            }}
          />
        )}
      </div>
    </div>
  );
}