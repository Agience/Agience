import { ReactNode, useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import {
  listWorkspaces,
  createWorkspace as apiCreateWorkspace,
  updateWorkspace as apiUpdateWorkspace,
  deleteWorkspace as apiDeleteWorkspace,
} from '../../api/workspaces';
import { WorkspacesContext, WorkspacesContextType } from './WorkspacesContext';
import { Workspace } from '../workspace/workspace.types';
import { WorkspaceUpdate } from '../../api/types/workspace';
import { useAuth } from '../../hooks/useAuth';
import { usePreferences } from '../../hooks/usePreferences';

export function WorkspacesProvider({ children }: { children: ReactNode }) {
  const { isAuthenticated, loading, user } = useAuth();
  const { preferences, isLoading: preferencesLoading, updatePreferences } = usePreferences();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(null);

  const dockedWorkspaceTabIds = useMemo(() => {
    const raw = preferences.browser?.dockedWorkspaceCardIds;
    return Array.isArray(raw) ? raw.map(String).filter(Boolean) : undefined;
  }, [preferences.browser?.dockedWorkspaceCardIds]);

  // Deep link: read artifactId from URL path and workspace from query param.
  const { artifactId: urlArtifactId } = useParams<{ artifactId?: string }>();
  const [searchParams] = useSearchParams();
  const urlWorkspaceId = searchParams.get('workspace');
  const deepLinkApplied = useRef(false);

  // Load workspaces once authenticated
  useEffect(() => {
    if (loading || !isAuthenticated) return;

    listWorkspaces()
      .then(data => {
        setWorkspaces(data);
      })
      .catch(err => {
        console.error('Failed to load workspaces', err);
      });
  }, [isAuthenticated, loading]);

  useEffect(() => {
    if (loading || !isAuthenticated || preferencesLoading) return;

    if (workspaces.length === 0) {
      setActiveWorkspaceId(null);
      return;
    }

    const visibleWorkspaces = dockedWorkspaceTabIds
      ? workspaces.filter((workspace) => dockedWorkspaceTabIds.includes(workspace.id))
      : workspaces;

    if (visibleWorkspaces.length === 0) {
      // No workspaces are visible - show empty dock state
      setActiveWorkspaceId(null);
      return;
    }

    setActiveWorkspaceId((current) => {
      if (current && visibleWorkspaces.some((workspace) => workspace.id === current)) {
        return current;
      }
      // Prefer inbox (id === user.id) if visible
      const inbox = user?.id ? visibleWorkspaces.find((workspace) => workspace.id === user.id) : undefined;
      return inbox?.id || visibleWorkspaces[0]?.id || null;
    });
  }, [dockedWorkspaceTabIds, isAuthenticated, loading, preferencesLoading, workspaces, user?.id, updatePreferences, preferences.browser]);

  // Deep link: if URL has /:artifactId (or ?workspace=xyz) matching a
  // known workspace, activate it on first load.
  useEffect(() => {
    if (deepLinkApplied.current || workspaces.length === 0) return;
    const targetWsId = urlWorkspaceId || urlArtifactId;
    if (targetWsId) {
      const found = workspaces.find((w) => w.id === targetWsId);
      if (found) {
        setActiveWorkspaceId(found.id);
        deepLinkApplied.current = true;
      }
    }
  }, [workspaces, urlArtifactId, urlWorkspaceId]);

  // Create
  const createWorkspace = useCallback(async (
    name: string,
    description: string = '',
    options?: { activate?: boolean }
  ): Promise<Workspace> => {
    const created = await apiCreateWorkspace({ name, description });
    setWorkspaces(prev => [...prev, created]);
    if (dockedWorkspaceTabIds) {
      void updatePreferences({
        browser: { ...preferences.browser, dockedWorkspaceCardIds: [...dockedWorkspaceTabIds, created.id] },
      });
    }
    if (options?.activate !== false) {
      setActiveWorkspaceId(created.id);
    }
    return created;
  }, [dockedWorkspaceTabIds, preferences.browser, updatePreferences]);

  // Update
  const updateWorkspace = useCallback(
    async (updates: Partial<Workspace> & { id: string }) => {
      const { id, name, description } = updates;

      const payload: WorkspaceUpdate = {
        ...(name !== undefined ? { name } : {}),
        ...(description !== undefined ? { description } : {}),
      };

      const updated = await apiUpdateWorkspace(id, payload);
      setWorkspaces(prev => prev.map(w => (w.id === updated.id ? updated : w)));
    },
    []
  );

  // Delete
  const deleteWorkspace = useCallback(
    async (id: string) => {
      await apiDeleteWorkspace(id);
      setWorkspaces(prev => {
        const remaining = prev.filter(w => w.id !== id);
        if (activeWorkspaceId === id) {
          const nextVisible = dockedWorkspaceTabIds
            ? remaining.find((workspace) => dockedWorkspaceTabIds.includes(workspace.id))
            : remaining[0];
          setActiveWorkspaceId(nextVisible?.id || null);
        }
        return remaining;
      });
    },
    [activeWorkspaceId, dockedWorkspaceTabIds]
  );

  // Active workspace
  const activeWorkspace = useMemo(
    () => workspaces.find(w => w.id === activeWorkspaceId) || null,
    [workspaces, activeWorkspaceId]
  );

  const value = useMemo<WorkspacesContextType>(
    () => ({
      workspaces,
      activeWorkspace,
      activeWorkspaceId,
      createWorkspace,
      updateWorkspace,
      deleteWorkspace,
      setActiveWorkspaceId,
    }),
    [workspaces, activeWorkspace, activeWorkspaceId, createWorkspace, updateWorkspace, deleteWorkspace]
  );

  return (
    <WorkspacesContext.Provider value={value}>
      {children}
    </WorkspacesContext.Provider>
  );
}
