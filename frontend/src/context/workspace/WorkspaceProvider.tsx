// src/context/workspace/WorkspaceProvider.tsx
import {
  useState, useEffect, useCallback, useMemo, useRef, ReactNode, MouseEvent,
} from 'react';
import {
  listWorkspaceArtifacts,
  addArtifactToWorkspace,
  importCollectionArtifactToWorkspace,
  updateWorkspaceArtifact,
  removeWorkspaceArtifact,
  revertWorkspaceArtifact,
  commitWorkspace,
  previewWorkspaceCommit,
  orderWorkspaceArtifacts,
  subscribeWorkspaceEvents,
} from '../../api/workspaces';
import { WorkspaceContext } from './WorkspaceContext';
import { Artifact } from './workspace.types';
import { useWorkspaces } from '../workspaces/WorkspacesContext';
import { toast } from 'sonner';
import { stringifyArtifactContext } from '@/utils/artifactContext';
import { midKey, afterKey } from '../../utils/fractional-index';
import { extractInformation } from '../../api/agent';
import type {
  WorkspaceCommitRequest,
  WorkspaceCommitResponse,
} from '../../api/types/workspace_commit';

type UpdateArtifactPayload = {
  state?: Artifact['state'];
  context?: string;
  content?: string;
};

type ListWorkspaceArtifactsResult = { items: Artifact[]; order_version?: number };

function compareByOrderKeyStable(a: Artifact, b: Artifact) {
  const ka = (a.order_key || '');
  const kb = (b.order_key || '');
  const primary = ka.localeCompare(kb);
  if (primary !== 0) return primary;

  const ta = new Date(a.created_time || 0).getTime();
  const tb = new Date(b.created_time || 0).getTime();
  if (ta !== tb) return ta - tb;

  return String(a.id || '').localeCompare(String(b.id || ''));
}

function expandToFullWorkspaceOrder(allArtifacts: Artifact[], orderedIdsIn: string[]): string[] {
  const full = [...allArtifacts]
    .sort(compareByOrderKeyStable)
    .map(c => String(c.id))
    .filter(Boolean);

  const fullSet = new Set(full);
  const orderedIds = orderedIdsIn.map(String).filter(id => fullSet.has(id));

  // If already full coverage, nothing to expand.
  if (orderedIds.length === full.length) return orderedIds;

  // Permute only the provided IDs within the existing "visible" slots.
  // This keeps all non-mentioned artifacts exactly where they are, and avoids
  // sending a partial list to the backend (which can lead to confusing results).
  const visibleSet = new Set(orderedIds);
  const it = orderedIds[Symbol.iterator]();
  const next = full.map((id) => {
    if (!visibleSet.has(id)) return id;
    const n = it.next();
    return n.done ? id : String(n.value);
  });

  // If any orderedIds weren't consumed (shouldn't happen), append them.
  const remaining = Array.from(it);
  if (remaining.length) return [...next, ...remaining.map(String)];
  return next;
}


export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { activeWorkspace } = useWorkspaces();

  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  // Artifacts currently shown in center panel (workspace, collection, or search results)
  const [displayedArtifacts, setDisplayedArtifacts] = useState<Artifact[]>([]);
  const [selectedArtifactIds, setSelectedArtifactIds] = useState<string[]>([]);
  const [anchorId, setAnchorId] = useState<string | null>(null);
  const [orderVersion, setOrderVersion] = useState<number | null>(null);
  const [isCommitting, setIsCommitting] = useState(false);
  const [commitPreview, setCommitPreview] = useState<WorkspaceCommitResponse | null>(null);

  const newArtifactHandlerRef = useRef<((artifact: Artifact) => void) | null>(null);
  const commitTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // central loader
  const refreshArtifacts = useCallback(async (workspaceId: string) => {
    const res = await listWorkspaceArtifacts(workspaceId);
    const { items, order_version } = res as unknown as ListWorkspaceArtifactsResult;
    setArtifacts(items);
    if (typeof order_version === 'number') setOrderVersion(order_version);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!activeWorkspace?.id) {
        setArtifacts([]);
        setSelectedArtifactIds([]);
        setOrderVersion(null);
        setCommitPreview(null);
        return;
      }
      try {
        await refreshArtifacts(activeWorkspace.id);
        if (!cancelled) {
          setCommitPreview(null);
        }
      } catch (err) {
        if (!cancelled) {
          console.error('Failed to load artifacts', err);
          setArtifacts([]);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [activeWorkspace?.id, refreshArtifacts]);

  // Cleanup commit timeout on unmount
  useEffect(() => {
    return () => {
      if (commitTimeoutRef.current) {
        clearTimeout(commitTimeoutRef.current);
      }
    };
  }, []);

  // Subscribe to real-time workspace change events via SSE.
  // Opens when a workspace is selected; auto-closes on workspace switch or unmount.
  useEffect(() => {
    if (!activeWorkspace?.id) return;
    const workspaceId = activeWorkspace.id;

    const unsubscribe = subscribeWorkspaceEvents(workspaceId, {
      onArtifactCreated: (artifact) => {
        setArtifacts(prev => {
          // Guard against duplicates from optimistic local creates
          if (prev.some(c => String(c.id) === String(artifact.id))) return prev;
          return [...prev, artifact as Artifact];
        });
      },
      onArtifactUpdated: (artifact) => {
        setArtifacts(prev =>
          prev.map(c =>
            String(c.id) === String(artifact.id)
              ? { ...c, ...artifact } as Artifact
              : c
          )
        );
      },
      onArtifactDeleted: (artifactId) => {
        setArtifacts(prev => prev.filter(c => String(c.id) !== String(artifactId)));
        setSelectedArtifactIds(prev => prev.filter(id => id !== String(artifactId)));
      },
      onWorkspaceRefreshed: () => {
        refreshArtifacts(workspaceId);
      },
    });

    return unsubscribe;
  }, [activeWorkspace?.id, refreshArtifacts]);

  const addArtifact = useCallback(
    async (partial: Partial<Artifact>) => {
      if (!activeWorkspace?.id) return;
      const payload = {
        content: partial.content ?? '',
        context: stringifyArtifactContext(partial.context ?? { content_type: 'text/plain', preview_text: '' }),
        // Don't send collection_ids - let server default to [user_id]
      };
      try {
        const created = await addArtifactToWorkspace(activeWorkspace.id, payload);
        setArtifacts(prev => [...prev, created as Artifact]);
      } catch (err) {
        console.error('Failed to create artifact', err);
      }
    },
    [activeWorkspace?.id]
  );

  // Add a pre-created artifact to local state (e.g., from upload initiation)
  const addExistingArtifact = useCallback((artifact: Artifact) => {
    setArtifacts(prev => [...prev, artifact]);
  }, []);

  // New: create artifact, return it, and insert at a specific index
  const createArtifact = useCallback(
    async (partial: Partial<Artifact>, insertIndex?: number): Promise<Artifact | null> => {
      if (!activeWorkspace?.id) return null;
      
      // Compute order_key client-side for instant positioning
      let order_key: string;
      if (typeof insertIndex === 'number') {
        const before = insertIndex > 0 ? artifacts[insertIndex - 1] : null;
        const after = insertIndex < artifacts.length ? artifacts[insertIndex] : null;
        order_key = midKey(before?.order_key || null, after?.order_key || null);
      } else {
        // Append to end
        const lastArtifact = artifacts[artifacts.length - 1];
        order_key = afterKey(lastArtifact?.order_key || null);
      }

      const payload: {
        content: string;
        context: string;
        order_key: string;
        content_type?: string;
      } = {
        content: partial.content ?? '',
        context: stringifyArtifactContext(partial.context ?? { content_type: 'text/plain', preview_text: '' }),
        order_key, // Client-computed fractional index
      };
      if (partial.content_type) {
        payload.content_type = partial.content_type;
      }
      
      try {
        const created = await addArtifactToWorkspace(activeWorkspace.id, payload) as Artifact;

        // Optimistic insert at correct position
        setArtifacts(prev => {
          if (typeof insertIndex === 'number') {
            const next = [...prev];
            next.splice(insertIndex, 0, created);
            return next;
          }
          return [...prev, created];
        });

        return created;
      } catch (err) {
        console.error('Failed to create artifact', err);
        return null;
      }
    },
    [activeWorkspace?.id, artifacts]
  );

  const updateArtifact = useCallback(
    async (patchIn: Partial<Artifact>): Promise<void> => {
      if (!activeWorkspace?.id) return;

      // Optimistic update
      setArtifacts(prev =>
        prev.map(c =>
          String(c.id) === String(patchIn.id)
            ? { ...c, ...patchIn } as Artifact
            : c
        )
      );

      // Build minimal payload - only send fields that are explicitly set
      const payload: UpdateArtifactPayload = {};
      if (typeof patchIn.state !== 'undefined') payload.state = patchIn.state as Artifact['state'];
      if (typeof patchIn.context !== 'undefined') payload.context = stringifyArtifactContext(patchIn.context);
      if (typeof patchIn.content !== 'undefined') payload.content = patchIn.content;

      try {
        const updated = await updateWorkspaceArtifact(
          activeWorkspace.id,
          String(patchIn.id),
          payload
        );
        setArtifacts(prev =>
          prev.map(c => (String(c.id) === String(patchIn.id)
            ? { ...c, ...updated } as Artifact
            : c))
        );
      } catch (err) {
        console.error('Failed to update artifact', err);
        try {
          await refreshArtifacts(activeWorkspace.id);
        } catch { /* ignore */ }
      }
    },
    [activeWorkspace?.id, refreshArtifacts]
  );

  const removeArtifact = useCallback(
    async (id: string) => {
      if (!activeWorkspace?.id) return;
      try {
        await removeWorkspaceArtifact(activeWorkspace.id, String(id));
        setArtifacts(prev => prev.filter(c => String(c.id) !== String(id) && String(c.root_id) !== String(id)));
        setSelectedArtifactIds(prev => prev.filter(cid => cid !== String(id)));
      } catch (err) {
        console.error('Failed to remove artifact', err);
      }
    },
    [activeWorkspace?.id]
  );

  const revertArtifact = useCallback(
    async (id: string) => {
      if (!activeWorkspace?.id) return;
      try {
        const updated = await revertWorkspaceArtifact(activeWorkspace.id, String(id));
        setArtifacts(prev =>
          prev.map(c => (String(c.id) === String(id)
            ? { ...c, ...updated } as Artifact
            : c))
        );
        toast.success('Reverted to last committed version');
      } catch (err) {
        console.error('Failed to revert artifact', err);
        toast.error('Revert failed');
        try {
          await refreshArtifacts(activeWorkspace.id);
        } catch { /* ignore */ }
      }
    },
    [activeWorkspace?.id, refreshArtifacts]
  );


  const orderArtifacts = useCallback(
    async (orderedIds: string[]) => {
      if (!activeWorkspace?.id) return;

      // Ensure we always persist a full-workspace ordering.
      // Some UIs can call this with only the currently-visible IDs (e.g., filtered/search).
      const orderedIdsFull = expandToFullWorkspaceOrder(artifacts, orderedIds);

      // Optimistic update: reorder the artifacts array immediately
      setArtifacts(prev => {
        const applyIds = expandToFullWorkspaceOrder(prev, orderedIdsFull);
        const byId = new Map(prev.map(c => [String(c.id), c]));
        const nextUnkeyed: Artifact[] = [];
        for (const id of applyIds) {
          const c = byId.get(String(id));
          if (c) nextUnkeyed.push(c);
        }
        // Include any artifacts not in orderedIds at the end
        for (const c of prev) {
          if (!applyIds.includes(String(c.id))) nextUnkeyed.push(c);
        }

        // IMPORTANT: Browser "manual" sorting uses order_key.
        // The backend /order endpoint returns only {version}, not updated keys.
        // To keep UX responsive and deterministic, we update order_key locally.
        let prevKey: string | null = null;
        const next = nextUnkeyed.map((c) => {
          const nextKey = afterKey(prevKey);
          prevKey = nextKey;
          return { ...c, order_key: nextKey };
        });

        return next;
      });

      // Use bulk /order endpoint - server computes new order_keys
      try {
        const res = await orderWorkspaceArtifacts(
          activeWorkspace.id,
          orderedIdsFull,
          orderVersion ?? undefined
        );
        if (typeof res?.version === 'number') setOrderVersion(res.version);
      } catch (err: unknown) {
        console.error('[ORDER] Error:', err);
        // On error, refetch to recover correct state
        const fresh = await listWorkspaceArtifacts(activeWorkspace.id) as unknown as ListWorkspaceArtifactsResult;
        setArtifacts(fresh.items);
        if (typeof fresh.order_version === 'number') setOrderVersion(fresh.order_version);
        toast.error('Could not save order');
      }
    },
    [activeWorkspace?.id, orderVersion, artifacts]
  );

  const importArtifactsByRootIds = useCallback(
    async (rootIdsIn: string[], insertIndex: number) => {
      if (!activeWorkspace?.id) return;

      const rootIds = rootIdsIn.map(String).map(v => v.trim()).filter(Boolean);
      if (rootIds.length === 0) return;

      const workspaceId = activeWorkspace.id;

      // Import sequentially to preserve user drag order and avoid noisy parallel errors
      for (const rootId of rootIds) {
        try {
          await importCollectionArtifactToWorkspace(workspaceId, rootId);
        } catch {
          // Ignore: not readable / already exists / etc
        }
      }

      // Fetch authoritative artifacts list + version, then compute the new ordering
      let fresh: ListWorkspaceArtifactsResult;
      try {
        const res = await listWorkspaceArtifacts(workspaceId);
        fresh = res as unknown as ListWorkspaceArtifactsResult;
      } catch (err) {
        console.error('Failed to refresh artifacts after import', err);
        return;
      }

      const normalized = fresh.items ?? [];
      if (typeof fresh.order_version === 'number') setOrderVersion(fresh.order_version);

      // Determine which workspace artifact IDs correspond to the dropped root IDs
      const idsToInsert = rootIds
        .map((rid) => normalized.find((c) => String(c.root_id || '') === rid)?.id)
        .filter((v): v is NonNullable<typeof v> => v != null)
        .map(String)
        .filter(Boolean);

      // If nothing new was inserted, just sync to fresh state
      if (idsToInsert.length === 0) {
        setArtifacts(normalized);
        return;
      }

      const sorted = [...normalized].sort(compareByOrderKeyStable);
      const currentOrder = sorted.map((c) => String(c.id)).filter(Boolean);
      const remaining = currentOrder.filter((id) => !idsToInsert.includes(id));

      const clampedIndex = Math.max(0, Math.min(insertIndex, remaining.length));
      const nextOrder = [
        ...remaining.slice(0, clampedIndex),
        ...idsToInsert,
        ...remaining.slice(clampedIndex),
      ];

      // Optimistically update local ordering + order_key for responsive UI
      const byId = new Map(sorted.map((c) => [String(c.id), c] as const));
      let prevKey: string | null = null;
      const nextArtifacts: Artifact[] = [];

      for (const id of nextOrder) {
        const artifact = byId.get(String(id));
        if (!artifact) continue;
        const nextKey = afterKey(prevKey);
        prevKey = nextKey;
        nextArtifacts.push({ ...artifact, order_key: nextKey });
      }

      // Include any artifacts not covered (shouldn't happen) at the end
      for (const artifact of sorted) {
        const id = String(artifact.id);
        if (!id || nextOrder.includes(id)) continue;
        const nextKey = afterKey(prevKey);
        prevKey = nextKey;
        nextArtifacts.push({ ...artifact, order_key: nextKey });
      }

      setArtifacts(nextArtifacts);

      // Persist ordering server-side
      try {
        const res = await orderWorkspaceArtifacts(
          workspaceId,
          nextOrder,
          typeof fresh.order_version === 'number' ? fresh.order_version : orderVersion ?? undefined
        );
        if (typeof res?.version === 'number') setOrderVersion(res.version);
      } catch (err) {
        console.error('Failed to save order after import', err);
        try {
          await refreshArtifacts(workspaceId);
        } catch {
          // ignore
        }
      }
    },
    [activeWorkspace?.id, orderVersion, refreshArtifacts]
  );

  // selection
  const clearSelection = useCallback(() => {
    setSelectedArtifactIds([]);
    setAnchorId(null);
  }, []);

  const selectAllArtifacts = useCallback(() => {
    setSelectedArtifactIds(artifacts.map(c => String(c.id)));
  }, [artifacts]);

  const selectArtifact = useCallback(
    (id: string, event: MouseEvent) => {
      const isShift = event.shiftKey;
      const isMeta = event.metaKey || event.ctrlKey;

      setSelectedArtifactIds(prev => {
        if (isShift && anchorId) {
          const ids = artifacts.map(c => String(c.id));
          const start = ids.indexOf(anchorId);
          const end = ids.indexOf(id);
          if (start === -1 || end === -1) return prev;
          const [lo, hi] = start < end ? [start, end] : [end, start];
          const range = ids.slice(lo, hi + 1);
          return Array.from(new Set([...prev, ...range]));
        }

        if (isMeta) {
          setAnchorId(id);
          return prev.includes(id) ? prev.filter(cid => cid !== id) : [...prev, id];
        }

        setAnchorId(id);
        return [id];
      });

      if (!isShift) setAnchorId(id);
    },
    [anchorId, artifacts]
  );

  const createNewArtifact = useCallback(() => {
    const draft: Artifact = {
      content: '',
      context: JSON.stringify({ content_type: 'text/plain', preview_text: '' }),
      state: 'draft',
    };
    newArtifactHandlerRef.current?.(draft);
  }, []);

  const registerNewArtifactHandler = useCallback((fn: (artifact: Artifact) => void) => {
    newArtifactHandlerRef.current = fn;
  }, []);

  const clearCommitPreview = useCallback(() => {
    setCommitPreview(null);
  }, []);

  const fetchCommitPreview = useCallback(
    async (input?: WorkspaceCommitRequest) => {
      if (!activeWorkspace?.id) {
        setCommitPreview(null);
        return null;
      }
      try {
        const response = await previewWorkspaceCommit(activeWorkspace.id, {
          ...input,
          dry_run: true,
        });
        setCommitPreview(response);
        return response;
      } catch (err) {
        console.error('Failed to preview workspace commit', err);
        toast.error('Failed to load commit preview');
        setCommitPreview(null);
        return null;
      }
    },
    [activeWorkspace?.id]
  );

  const commitCurrentWorkspace = useCallback((input?: WorkspaceCommitRequest) => {
    if (!activeWorkspace?.id || isCommitting) return;

    // Debounce: clear any pending commit
    if (commitTimeoutRef.current) {
      clearTimeout(commitTimeoutRef.current);
    }

    const requestPayload = input;

    // Debounce for 300ms
    commitTimeoutRef.current = setTimeout(async () => {
      setIsCommitting(true);
      try {
        console.log('[Commit] Starting commit for workspace', activeWorkspace.id);
        const response = await commitWorkspace(activeWorkspace.id, requestPayload);
        console.log('[Commit] Response:', {
          updated: response.updated_workspace_artifacts.length,
          deleted: response.deleted_workspace_artifact_ids.length,
          skipped: response.skipped_workspace_artifact_ids.length,
          deletedIds: response.deleted_workspace_artifact_ids
        });

        // Build sets for efficient lookup
        const deletedIds = new Set(response.deleted_workspace_artifact_ids.map(String));
        const updatedMap = new Map(
          response.updated_workspace_artifacts.map(c => [String(c.id), c as Artifact])
        );
        
        // Update state: remove deleted artifacts and update modified artifacts
        setArtifacts(prev => {
          const filtered = prev.filter(artifact => !deletedIds.has(String(artifact.id)));
          return filtered.map(artifact => {
            const updated = updatedMap.get(String(artifact.id));
            return updated ? { ...artifact, ...updated } as Artifact : artifact;
          });
        });
        setCommitPreview(null);
        toast.success('Workspace committed');
      } catch (err) {
        console.error('Failed to commit workspace', err);
        toast.error('Commit failed');
      } finally {
        setIsCommitting(false);
      }
    }, 300);
  }, [activeWorkspace?.id, isCommitting]);

  const extractInformationFromArtifact = useCallback(
    async (sourceArtifactId: string, contextArtifactIds?: string[]) => {
      if (!activeWorkspace?.id) return;
      if (!sourceArtifactId) return;

      try {
        const result = await extractInformation(activeWorkspace.id, sourceArtifactId, contextArtifactIds);

        await refreshArtifacts(activeWorkspace.id);

        const created = Array.isArray(result.created_artifact_ids) ? result.created_artifact_ids.length : 0;
        if (result.warning) {
          toast.warning(result.warning);
        } else {
          toast.success(`Extracted ${created} note${created === 1 ? '' : 's'}`);
        }
      } catch (err) {
        console.error('Failed to extract information', err);
        toast.error('Extract failed');
      }
    },
    [activeWorkspace?.id, refreshArtifacts]
  );

  const extractInformationFromSelection = useCallback(
    async (options?: { sourceArtifactId?: string }) => {
      if (!activeWorkspace?.id) return;

      const forcedSourceId = options?.sourceArtifactId ? String(options.sourceArtifactId) : null;
      const selectedIds = selectedArtifactIds.map(String);

      const sourceId = forcedSourceId || anchorId || selectedIds[0] || null;
      if (!sourceId) {
        toast.error('Select one or more cards to extract from');
        return;
      }

      const artifactIds = selectedIds.filter((id) => id !== sourceId);
      await extractInformationFromArtifact(sourceId, artifactIds);
    },
    [activeWorkspace?.id, selectedArtifactIds, anchorId, extractInformationFromArtifact]
  );

  const value = useMemo(
    () => ({
      artifacts,
      displayedArtifacts,
      setDisplayedArtifacts,
      selectedArtifactIds,
      isCommitting,
      addArtifact,
      addExistingArtifact,
      createArtifact,
      updateArtifact,
      removeArtifact,
      revertArtifact,
      orderArtifacts,
      importArtifactsByRootIds,
      refreshArtifacts,
      selectArtifact,
      selectAllArtifacts,
      unselectAllArtifacts: clearSelection,
      createNewArtifact,
      commitCurrentWorkspace,
      commitPreview,
      fetchCommitPreview,
      clearCommitPreview,
      extractInformationFromArtifact,
      extractInformationFromSelection,
      registerNewArtifactHandler,
    }),
    [
      artifacts,
      displayedArtifacts,
      selectedArtifactIds,
      isCommitting,
      addArtifact,
      addExistingArtifact,
      createArtifact,
      updateArtifact,
      removeArtifact,
      revertArtifact,
      orderArtifacts,
      importArtifactsByRootIds,
      refreshArtifacts,
      selectArtifact,
      selectAllArtifacts,
      clearSelection,
      createNewArtifact,
      commitCurrentWorkspace,
      commitPreview,
      fetchCommitPreview,
      clearCommitPreview,
      extractInformationFromArtifact,
      extractInformationFromSelection,
      registerNewArtifactHandler,
    ]
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}
