import { useState, useEffect, useMemo, useRef } from 'react';
import { FiPlus, FiMoreVertical, FiSave, FiX } from 'react-icons/fi';
import { FolderOpen, Loader2, Upload } from 'lucide-react';
import type { ActiveSource } from '../../types/workspace';
import { CollectionResponse } from '../../api/types';
import { listCollections } from '../../api/collections';

import { useWorkspace } from '../../hooks/useWorkspace';
import { useWorkspaces } from '../../hooks/useWorkspaces';
import { usePreferences } from '../../hooks/usePreferences';
import { isAgienceDrag, getDroppedArtifactIds } from '@/dnd/agienceDrag';

type ViewOption = 'grid' | 'list';

type BrowserHeaderProps = {
  activeSource: ActiveSource;
  onToggleFilters: () => void;
  filtersOpen: boolean;
  hasActiveFilter: boolean;
  activeFilter: string;
  viewMode: ViewOption;
  onViewModeChange: (mode: ViewOption) => void;
  isShowingSearchResults?: boolean;
  sortMode?: 'relevance' | 'recency';
  onSortChange?: (mode: 'relevance' | 'recency') => void;
  aperture?: number;
  onApertureChange?: (value: number) => void;
  pendingChangeCount?: number;
  onReviewChanges?: () => void;
  isReviewLoading?: boolean;
  isCommitting?: boolean;
  canReview?: boolean;
  onNewArtifact?: () => void;
  onCreateWorkspace?: () => void;
  onDropWorkspace?: (draggedIds: string[]) => void;
  onRenameWorkspace?: (id: string, name: string) => void;
};

export default function BrowserHeader({
  activeSource,
  isShowingSearchResults = false,
  pendingChangeCount = 0,
  onReviewChanges,
  isReviewLoading = false,
  isCommitting = false,
  canReview = false,
  onNewArtifact,
  onCreateWorkspace,
  onDropWorkspace,
  onRenameWorkspace,
}: BrowserHeaderProps) {
  const { createArtifact } = useWorkspace();
  const { preferences, updatePreferences } = usePreferences();
  const [collections, setCollections] = useState<CollectionResponse[]>([]);
  const [isDropActive, setIsDropActive] = useState(false);
  const { workspaces, activeWorkspace, setActiveWorkspaceId } = useWorkspaces();
  const [editingTabId, setEditingTabId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState('');
  const editInputRef = useRef<HTMLInputElement>(null);

  // Load collections list once
  useEffect(() => {
    listCollections().then(setCollections).catch(console.error);
  }, []);

  const canSaveView = !isShowingSearchResults && activeSource?.type === 'collection';

  const handleSaveView = async () => {
    if (!canSaveView) return;
    const collectionId = activeSource.id;
    const collection = collections.find(c => c.id === collectionId);
    const name = collection?.name || 'Collection';
    await createArtifact({
      content: '',
      context: JSON.stringify({
        mime: 'application/vnd.agience.view+json',
        title: `${name} view`,
        target: { kind: 'collection', id: collectionId },
      }),
    });
  };

  const browserPrefs = useMemo(
    () => ((preferences.browser as Record<string, unknown> | undefined) ?? {}),
    [preferences.browser]
  );

  const dockedTabIds = useMemo(() => {
    const raw = (browserPrefs as { dockedWorkspaceCardIds?: unknown }).dockedWorkspaceCardIds;
    return Array.isArray(raw) ? raw.map(String).filter(Boolean) : undefined;
  }, [browserPrefs]);

  const visibleWorkspaces = useMemo(
    () => (dockedTabIds ? workspaces.filter(w => dockedTabIds.includes(w.id)) : workspaces),
    [workspaces, dockedTabIds]
  );

  const showDockHint = visibleWorkspaces.length === 0;

  const hasActiveWorkspace = activeSource?.type === 'workspace' && Boolean(activeWorkspace?.id);
  const reviewDisabled = !hasActiveWorkspace || isCommitting || isReviewLoading || !onReviewChanges || !canReview;

  const handleTabClick = (id: string) => {
    if (activeWorkspace?.id === id && activeSource?.type !== 'workspace') {
      // Force re-derive activeSource when switching back from non-workspace view
      setActiveWorkspaceId(null);
      setTimeout(() => setActiveWorkspaceId(id), 0);
      return;
    }
    setActiveWorkspaceId(id);
  };

  const handleCloseTab = (workspaceId: string) => {
    const nextDocked = dockedTabIds
      ? dockedTabIds.filter(id => id !== workspaceId)
      : workspaces.filter(w => w.id !== workspaceId).map(w => w.id);

    void updatePreferences({
      browser: { ...browserPrefs, dockedWorkspaceCardIds: nextDocked },
    });

    if (activeWorkspace?.id === workspaceId) {
      const next = workspaces.find(
        (w) => w.id !== workspaceId && (dockedTabIds ? dockedTabIds.includes(w.id) : true),
      );
      setActiveWorkspaceId(next?.id ?? null);
    }
  };

  const handleStartRename = (workspaceId: string, currentName: string) => {
    setEditingTabId(workspaceId);
    setEditingName(currentName);
    setTimeout(() => editInputRef.current?.select(), 0);
  };

  const handleFinishRename = () => {
    if (editingTabId && editingName.trim()) {
      onRenameWorkspace?.(editingTabId, editingName.trim());
    }
    setEditingTabId(null);
    setEditingName('');
  };

  return (
    <div className="bg-white">
      {/* Top row: workspace tabs + controls */}
      <div className="flex h-14 items-center justify-between border-b border-gray-200 bg-white pl-4 pr-4">
        <div className="flex-1 flex items-center gap-2 min-w-0 overflow-hidden">
          <div
            className={`flex items-center gap-1 overflow-x-auto scrollbar-thin w-full ${isDropActive ? 'bg-sky-50/50 rounded' : ''}`}
            role="tablist"
            aria-label="Tabs"
            onDragOver={(event) => {
              if (!isAgienceDrag(event.dataTransfer)) {
                setIsDropActive(false);
                return;
              }
              event.preventDefault();
              event.dataTransfer.dropEffect = 'move';
              setIsDropActive(true);
            }}
            onDragLeave={(event) => {
              // Only deactivate when leaving the tab area entirely
              if (!(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node)) {
                setIsDropActive(false);
              }
            }}
            onDrop={(event) => {
              setIsDropActive(false);
              if (!isAgienceDrag(event.dataTransfer)) return;
              const draggedIds = getDroppedArtifactIds(event.dataTransfer);
              if (!draggedIds.length) return;
              event.preventDefault();
              event.stopPropagation();
              onDropWorkspace?.(draggedIds);
            }}
          >
            {showDockHint && (
              <div
                className={`flex min-w-0 flex-1 items-center rounded-md border border-dashed px-4 py-2 text-sm transition-colors ${isDropActive ? 'border-sky-400 bg-sky-50 text-sky-800' : 'border-slate-300 bg-slate-50 text-slate-600'}`}
                aria-label="Dock a workspace here"
              >
                <span className="truncate">Find a workspace and dock it here.</span>
              </div>
            )}

            {visibleWorkspaces.map((workspace) => {
              const isActive = activeWorkspace?.id === workspace.id;
              const isEditing = editingTabId === workspace.id;
              return (
                <div key={workspace.id} className="group relative flex-shrink-0">
                  {isEditing ? (
                    <input
                      ref={editInputRef}
                      value={editingName}
                      onChange={(e) => setEditingName(e.target.value)}
                      onBlur={handleFinishRename}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') handleFinishRename();
                        if (e.key === 'Escape') { setEditingTabId(null); setEditingName(''); }
                      }}
                      className="h-10 pl-4 pr-10 rounded-sm border border-purple-400 text-sm font-medium bg-white focus:outline-none focus:ring-1 focus:ring-purple-400"
                      autoFocus
                    />
                  ) : (
                    <button
                      type="button"
                      role="tab"
                      aria-selected={isActive}
                      onClick={() => handleTabClick(workspace.id)}
                      onDoubleClick={() => handleStartRename(workspace.id, workspace.name)}
                      className={`h-10 pl-4 pr-10 rounded-sm border text-sm font-medium whitespace-nowrap transition-none ${isActive ? 'bg-gradient-to-r from-purple-400/20 via-pink-400/20 to-blue-400/20 border-purple-300/40 text-purple-900' : 'bg-white/50 border-purple-200/30 text-gray-700 hover:bg-purple-50/30 hover:border-purple-300/40'}`}
                      title={`${workspace.name} (double-click to rename)`}
                    >
                      {workspace.name}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); handleCloseTab(workspace.id); }}
                    className="absolute right-1.5 top-1/2 -translate-y-1/2 w-5 h-5 inline-flex items-center justify-center rounded border border-gray-200 bg-white text-gray-500 transition-none hover:text-gray-700 hover:border-gray-300"
                    title="Close tab"
                    aria-label={`Close ${workspace.name}`}
                  >
                    <FiX className="w-3.5 h-3.5" />
                  </button>
                </div>
              );
            })}

            {/* [+] button: creates workspace on click */}
            <button
              type="button"
              onClick={() => onCreateWorkspace?.()}
              className={`flex-shrink-0 h-10 w-10 inline-flex items-center justify-center rounded-sm border border-dashed bg-white/50 text-gray-700 transition-colors ${isDropActive ? 'border-sky-400 bg-sky-50 text-sky-700' : 'border-purple-200/30 hover:bg-purple-50/30 hover:border-purple-300/40'}`}
              title="New workspace (or drop a workspace card onto this tab bar)"
              aria-label="New workspace"
            >
              <FiPlus className="w-4 h-4" />
            </button>
          </div>
        </div>

        <div className="flex items-center gap-2 ml-4">
          {/* New Artifact */}
          {!isShowingSearchResults && hasActiveWorkspace && (
            <button
              onClick={onNewArtifact}
              className="h-10 w-10 inline-flex items-center justify-center rounded bg-purple-600 text-white hover:bg-purple-700"
              title="New Artifact"
            >
              <FiPlus size={16} />
            </button>
          )}

          {/* Save current collection view as View */}
          {canSaveView && (
            <button
              onClick={handleSaveView}
              className="h-10 w-10 inline-flex items-center justify-center rounded border border-teal-500 text-teal-700 hover:bg-teal-50"
              title="Save this collection view as a View artifact"
            >
              <FiSave size={16} />
            </button>
          )}

          {/* Review Changes */}
          {!isShowingSearchResults && (
            <button
              onClick={onReviewChanges}
              disabled={reviewDisabled}
              className={`relative h-10 w-10 inline-flex items-center justify-center rounded transition-colors ${reviewDisabled ? 'bg-gray-200 text-gray-400' : 'bg-blue-600 text-white hover:bg-blue-700'}`}
              title={isReviewLoading || isCommitting ? 'Processing…' : hasActiveWorkspace ? 'Review changes' : 'Dock a workspace to review changes'}
            >
              {!isShowingSearchResults && pendingChangeCount > 0 && (
                <span className="absolute -top-1 -right-1 min-w-[16px] px-1 rounded-full bg-blue-500 text-[9px] leading-tight text-white font-semibold flex items-center justify-center shadow-sm">
                  {pendingChangeCount}
                </span>
              )}
              {isReviewLoading || isCommitting ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Upload className="h-4 w-4" />
              )}
            </button>
          )}

          {/* Kebab for future actions */}
          <button className="h-10 w-10 inline-flex items-center justify-center rounded hover:bg-accent" title="More">
            <FiMoreVertical size={16} />
          </button>
        </div>
      </div>

      {/* Bottom row: only rendered when showing search results label */}
      {isShowingSearchResults && (
        <div className="flex h-10 items-center justify-between border-b border-gray-200 bg-gray-100 px-4 text-[11px] text-muted-foreground">
          <div className="flex items-center gap-2 min-w-0">
            <FolderOpen size={14} className="text-purple-600" />
            <span className="font-medium text-purple-700 truncate">Search results</span>
          </div>
        </div>
      )}
    </div>
  );
}

