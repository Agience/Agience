// "Browser" – center column artifact browser/workspace showing the Artifact Grid/List for the active source.
import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import { useWorkspace } from "../../hooks/useWorkspace";
import { useWorkspaces } from "../../hooks/useWorkspaces";
import { useAuth } from "../../hooks/useAuth";
import { usePreferences } from '../../hooks/usePreferences';
import { useWorkspaceSearch } from "../../hooks/useWorkspaceSearch";
import CardList from "../common/CardList";
import CardGrid from "../common/CardGrid";
import ArtifactEditModal from "../common/CardDetailModal";
import FilterBar from "./FilterBar";
import FilterChips from "./FilterChips";
import EmptyState from "./EmptyState";
import MultiCardToolbar from "./MultiCardToolbar";
import { CollectionPicker } from '../modals/CollectionPicker';
import { BulkTagDialog, type BulkTagApplyRequest } from '../modals/BulkTagDialog';
import { Artifact } from "../../context/workspace/workspace.types";
import { ArtifactCreate, ArtifactUpdate } from "../../api/types";
import { addArtifactToWorkspace, importCollectionArtifactToWorkspace, initiateUpload, updateUploadStatus, getWorkspaceArtifactsBatchGlobal } from "../../api/workspaces";
import { addArtifactToCollection, getCollectionArtifactsBatchGlobal } from "../../api/collections";
import { midKey } from "../../utils/fractional-index";
import { uploadWithProgress, uploadMultipart } from "../../utils/upload";
import type { ActiveSource } from "../../types/workspace";
import { getCollectionArtifacts, subscribeCollectionEvents } from "../../api/collections";
import { getContentTypeCategory } from "../../utils/search";
import type { SearchResponse, SearchHit } from "../../api/types/search";
import type { WorkspaceCommitResponse, ArtifactCommitChange } from "../../api/types/workspace_commit";
import { CommitReviewDialog } from "../workspace/CommitReviewDialog";
import { useConfirm } from "@/context/dialog/useConfirm";
import { CARD_CONFIRM, BULK_CONFIRM, BUTTON_LABELS, SHORTCUTS } from "@/constants/strings";
import { useShortcuts } from "@/context/shortcuts/useShortcuts";
import BrowserHeader from "./BrowserHeader";
import { deriveArtifactCountArtifacts } from "./artifacts";
import { safeParseArtifactContext, stringifyArtifactContext } from '@/utils/artifactContext';
import { getContentTypeById } from '@/registry/content-types';
import CardTypePickerDialog from './CardTypePickerDialog';
import { WORKSPACE_CONTENT_TYPE } from '@/utils/content-type';
import { AGIENCE_DRAG_CONTENT_TYPE, getDroppedArtifactIds } from '@/dnd/agienceDrag';
import { toast } from 'sonner';
import { removeWorkspaceArtifact } from '@/api/workspaces';
import { removeArtifactFromCollection } from '@/api/collections';

const INLINE_BYTES_MAX = 128 * 1024; // 128 KiB (~32K tokens, good chunk size for LLM processing)
const HIDDEN_CONTENT_TYPE_FILTERS_STORAGE_KEY = 'agience.browser.hiddenMimeCategories';
const STACKED_ARTIFACT_FAMILIES_STORAGE_KEY = 'agience.browser.stackArtifacts';

type ArtifactState = 'all' | 'draft' | 'committed' | 'archived';
type SortOption = 'recent' | 'title' | 'created' | 'committed' | 'manual';
type ViewOption = 'grid' | 'list';



function getArtifactContentTypeCategory(artifact: Artifact): string {
  try {
    const context = typeof artifact.context === 'string' ? JSON.parse(artifact.context) : artifact.context;
    const ct = context?.content_type || '';
    return getContentTypeCategory(ct);
  } catch {
    return 'unknown';
  }
}

interface BrowserProps {
  activeSource: ActiveSource;
  /** Currently selected artifact ID for preview pane */
  selectedArtifactId?: string | null;
  /** Resolved artifacts currently visible in the search panel. */
  searchResultArtifacts?: Artifact[];
  /** Callback when an artifact is selected */
  onArtifactSelect?: (artifactId: string | null) => void;
  /** Callback when an artifact should be opened (e.g., floating window) */
  onOpenArtifact?: (artifact: Artifact, options?: { startInEditMode?: boolean }) => void;
  /** Callback when assigning collections (opens collection picker) */
  onAssignCollections?: (artifactId: string) => void;
  /** External search results from AdvancedSearch (global search) */
  externalSearchResults?: SearchResponse | null;
  /** Sort mode for search results */
  searchSortMode?: 'relevance' | 'recency';
  /** Callback when sort mode changes */
  onSearchSortChange?: (mode: 'relevance' | 'recency') => void;
  /** Aperture value for search results */
  searchAperture?: number;
  /** Callback when aperture changes */
  onSearchApertureChange?: (value: number) => void;
}

export default function Browser({ 
  activeSource, 
  selectedArtifactId, 
  searchResultArtifacts = [],
  onArtifactSelect,
  onOpenArtifact,
  onAssignCollections, 
  externalSearchResults = null,
  searchSortMode = 'relevance',
  onSearchSortChange,
  searchAperture = 0.5,
  onSearchApertureChange,
}: BrowserProps) {
  // TODO: Use selectedArtifactId to highlight the selected artifact in the view
  void selectedArtifactId; // Suppress unused warning - will be used for artifact highlighting
  const {
    artifacts,
    displayedArtifacts = [],
    selectedArtifactIds,
    removeArtifact,
    revertArtifact,
    selectArtifact,
    selectAllArtifacts,
    unselectAllArtifacts,
    addArtifact,
    addExistingArtifact,
    createArtifact,
    updateArtifact,
    registerNewArtifactHandler,
    orderArtifacts,
    commitCurrentWorkspace,
      importArtifactsByRootIds,
    commitPreview,
    fetchCommitPreview,
    clearCommitPreview,
    isCommitting,
  } = useWorkspace();
  // setDisplayedArtifacts is exposed by WorkspaceProvider; it may be undefined for older contexts
  const { setDisplayedArtifacts } = useWorkspace();
  const { preferences, updatePreferences } = usePreferences();
  const dangerConfirm = useConfirm();


  const latestArtifactsRef = useRef(artifacts);
  useEffect(() => { latestArtifactsRef.current = artifacts; }, [artifacts]);

  const { workspaces, activeWorkspace, createWorkspace, setActiveWorkspaceId, updateWorkspace } = useWorkspaces();
  const { user } = useAuth();
  // The Inbox workspace has the same ID as the current user (backend convention)
  const inboxWorkspaceId = user?.id ?? null;
  // Use activeWorkspace ID, or fall back to inboxWorkspaceId if viewing a workspace, or empty string
  const workspaceId = activeWorkspace?.id || (activeSource?.type === 'workspace' ? inboxWorkspaceId : "") || "";
  const [editArtifact, setEditArtifact] = useState<ArtifactCreate | ArtifactUpdate | null>(null);
  const [isArtifactTypePickerOpen, setIsArtifactTypePickerOpen] = useState(false);
  const [sourceArtifacts, setSourceArtifacts] = useState<Artifact[]>([]); // For collection/MCP artifacts

  // Load artifacts based on activeSource
  useEffect(() => {
    if (!activeSource) {
      setSourceArtifacts([]);
      return;
    }

    if (activeSource.type === 'workspace') {
      // Use workspace artifacts from context
      setSourceArtifacts(artifacts);
    } else if (activeSource.type === 'collection') {
      // Load collection artifacts
      getCollectionArtifacts(activeSource.id)
        .then((collectionArtifacts) => {
          setSourceArtifacts(collectionArtifacts as Artifact[]);
        })
        .catch((error) => {
          console.error('Failed to load collection artifacts:', error);
          setSourceArtifacts([]);
        });
    } else if (activeSource.type === 'mcp-server') {
      // MCP servers show empty for now
      setSourceArtifacts([]);
    }
  }, [activeSource, artifacts]);

  // Subscribe to real-time collection change events via SSE
  useEffect(() => {
    if (!activeSource || activeSource.type !== 'collection') return;
    const collectionId = activeSource.id;

    const unsubscribe = subscribeCollectionEvents(collectionId, {
      onArtifactCreated: (artifact) => {
        setSourceArtifacts(prev => {
          const key = String((artifact as Artifact).root_id ?? artifact.id);
          if (prev.some(c => String(c.root_id ?? c.id) === key)) return prev;
          return [...prev, artifact as Artifact];
        });
      },
      onArtifactUpdated: (artifact) => {
        setSourceArtifacts(prev =>
          prev.map(c => {
            const cKey = String(c.root_id ?? c.id);
            const aKey = String((artifact as Artifact).root_id ?? artifact.id);
            return cKey === aKey ? { ...c, ...artifact } as Artifact : c;
          })
        );
      },
      onArtifactDeleted: (artifactId) => {
        setSourceArtifacts(prev =>
          prev.filter(c => String(c.root_id ?? c.id) !== String(artifactId))
        );
      },
      onCollectionRefreshed: () => {
        getCollectionArtifacts(collectionId)
          .then(arts => setSourceArtifacts(arts as Artifact[]))
          .catch(err => console.error('Failed to reload collection artifacts:', err));
      },
    });

    return unsubscribe;
  }, [activeSource]);

  // Filter/Sort/View state
  const [activeFilter, setActiveFilter] = useState<ArtifactState>('all');
  const [sortBy, setSortBy] = useState<SortOption>('recent');
  const [viewMode, setViewMode] = useState<ViewOption>('grid');
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [isReviewOpen, setIsReviewOpen] = useState(false);
  const [isPreviewLoading, setIsPreviewLoading] = useState(false);
  const [selectedPlanArtifactIds, setSelectedPlanArtifactIds] = useState<string[]>([]);
  const [showBulkCollectionPicker, setShowBulkCollectionPicker] = useState(false);
  const [showBulkTagDialog, setShowBulkTagDialog] = useState(false);
  const [stackArtifactFamilies, setStackArtifactFamilies] = useState<boolean>(() => {
    try {
      const raw = localStorage.getItem(STACKED_ARTIFACT_FAMILIES_STORAGE_KEY);
      return raw ? raw === 'true' : true;
    } catch {
      return true;
    }
  });
  const [collapsedArtifactSections, setCollapsedArtifactSections] = useState<Record<string, boolean>>({
    transcripts: true,
    'stream-sessions': true,
    'palette-outputs': true,
  });

  // For MVP: workspaces are primarily manually ordered (drag-reorder).
  // Default to manual sort when viewing a workspace so reorder doesn't appear to "snap back".
  useEffect(() => {
    const sourceType = activeSource?.type;
    if (!sourceType) return;

    if (sourceType === 'workspace') {
      setSortBy((prev) => (prev === 'manual' ? prev : 'manual'));
    } else {
      setSortBy((prev) => (prev === 'recent' ? prev : 'recent'));
    }
  }, [activeSource?.type]);
  
  // State for resolved display artifacts (handles async search artifact fetching)
  const [resolvedDisplayArtifacts, setResolvedDisplayArtifacts] = useState<Artifact[]>(sourceArtifacts);
  
  // Search metadata index - separate from artifact.context (which is user-visible)
  // Maps artifact root_id or id to search score
  const [, setSearchMetadataIndex] = useState<Map<string, { score: number }>>(new Map());
  
  // Local workspace search (not used when global search results are provided)
  const {
    searchQuery,
    filteredArtifacts: searchFilteredArtifacts,
    // recentSearches, // Not used - search is now controlled by MainHeader
    // updateSearch, // Not used - global search now via AdvancedSearch component
    clearSearch,
    isSearchActive,
    // resultCount, // Not displayed - count shown in MainHeader
    // totalCount, // Not displayed - count shown in MainHeader
  } = useWorkspaceSearch(sourceArtifacts);
  
  // Convert SearchHit to Artifact for display
  const convertSearchHitsToArtifacts = useCallback(async (hits: SearchHit[]): Promise<Artifact[]> => {
    // Search now returns IDs only - need to fetch artifacts from source
    // Strategy:
    // 1. Check which artifacts already exist in sourceArtifacts (local cache)
    // 2. Batch fetch missing artifacts from backend (grouped by workspace/collection)
    // 3. Merge and return artifacts IN ORIGINAL SEARCH ORDER
    
    if (!hits || hits.length === 0) {
      console.log('[Search] No hits to convert');
      return [];
    }
    
    console.log(`[Search] Converting ${hits.length} hits, sourceArtifacts=${sourceArtifacts.length}`);
    
    // Map search hits by root_id for quick lookup
    const hitsByRootId = new Map(hits.map(hit => [hit.root_id, hit]));
    
    // Find artifacts that already exist in sourceArtifacts (local cache)
    const existingArtifactsByRootId = new Map<string, Artifact>();
    sourceArtifacts.forEach(artifact => {
      const rootId = artifact.root_id || artifact.id;
      if (rootId && hitsByRootId.has(rootId)) {
        existingArtifactsByRootId.set(rootId, artifact);
      }
    });
    
    console.log(`[Search] Found ${existingArtifactsByRootId.size} cached artifacts in sourceArtifacts`);
    
    // Find hits that need to be fetched
    const hitsToFetch = hits.filter(hit => !existingArtifactsByRootId.has(hit.root_id));
    console.log(`[Search] Need to batch fetch ${hitsToFetch.length} artifacts from backend`);
    
    // Fetch missing artifacts from backend using batch endpoints
    let fetchedArtifacts: Artifact[] = [];
    if (hitsToFetch.length > 0) {
      try {
        const batchFetchPromises: Promise<Artifact[]>[] = [];
        
        // Batch fetch workspace artifacts (global search across all workspaces)
        const workspaceHits = hitsToFetch.filter(h => !h.collection_id);
        if (workspaceHits.length > 0) {
          const artifactIds = workspaceHits.map(h => h.version_id);  // Use version_id (artifact ID), not document ID
          console.log(`[Search] Batch fetching ${artifactIds.length} artifacts from all accessible workspaces`);
          
          batchFetchPromises.push(
            getWorkspaceArtifactsBatchGlobal(artifactIds)
              .catch(err => {
                console.error(`[Search] Failed to batch fetch workspace artifacts:`, err);
                return [];
              })
          );
        }
        
        // Batch fetch collection artifacts (global search across all collections)
        const collectionHits = hitsToFetch.filter(h => h.collection_id);
        if (collectionHits.length > 0) {
          const artifactIds = collectionHits.map(h => h.version_id);
          console.log(`[Search] Batch fetching ${artifactIds.length} artifacts from all accessible collections`);
          
          batchFetchPromises.push(
            getCollectionArtifactsBatchGlobal(artifactIds)
              .catch(err => {
                console.error(`[Search] Failed to batch fetch collection artifacts:`, err);
                return [];
              })
          );
        }
        
        // Wait for all batch fetches to complete
        const results = await Promise.all(batchFetchPromises);
        fetchedArtifacts = results.flat();
        console.log(`[Search] Batch fetched ${fetchedArtifacts.length} artifacts from backend`);
      } catch (err) {
        console.error('[Search] Error batch fetching artifacts:', err);
      }
    }
    
    // Create lookup maps for fetched artifacts by root_id and version/artifact id.
    const fetchedArtifactsByRootId = new Map<string, Artifact>();
    const fetchedArtifactsById = new Map<string, Artifact>();
    fetchedArtifacts.forEach(artifact => {
      if (artifact.root_id) {
        fetchedArtifactsByRootId.set(artifact.root_id, artifact);
      }
      if (artifact.id) {
        fetchedArtifactsById.set(String(artifact.id), artifact);
      }
    });
    
    // Build final result array in ORIGINAL SEARCH ORDER
    // This preserves the backend's relevance ranking
    const orderedArtifacts: Artifact[] = [];
    const metadataIndex = new Map<string, { score: number }>();
    
    for (const hit of hits) {
      // Look up artifact from either cache or fetched results
      const artifact = existingArtifactsByRootId.get(hit.root_id)
        || fetchedArtifactsByRootId.get(hit.root_id)
        || fetchedArtifactsById.get(hit.version_id);
      
      if (!artifact) {
        console.warn(`[Search] Artifact not found for hit: ${hit.root_id}`);
        continue;
      }

      const hydratedArtifact: Artifact = {
        ...artifact,
        id: artifact.id ?? hit.version_id,
        root_id: artifact.root_id ?? hit.root_id,
        collection_id: artifact.collection_id ?? hit.collection_id,
      };

      orderedArtifacts.push(hydratedArtifact);
      
      // Store search metadata separately (not in artifact.context)
      const artifactKey = hydratedArtifact.root_id || hydratedArtifact.id;
      if (artifactKey) {
        metadataIndex.set(artifactKey, { score: hit.score });
      }
    }
    
    // Update metadata index state
    setSearchMetadataIndex(metadataIndex);
    
    console.log(`[Search] Returning ${orderedArtifacts.length} artifacts in search order`);
    return orderedArtifacts;
  }, [sourceArtifacts]);
  
  useEffect(() => {
    if (!commitPreview) {
      setSelectedPlanArtifactIds([]);
    }
  }, [commitPreview]);

  // Pending artifacts = drafts that need to be committed
  const pendingArtifacts = useMemo(() => {
    if (activeSource?.type !== 'workspace') return [];
    return artifacts.filter(a => a.state === 'draft');
  }, [activeSource?.type, artifacts]);

  const planTotalArtifacts = commitPreview?.plan.total_artifacts ?? 0;
  const blockedCollections = commitPreview?.plan.blocked_collections?.length ?? 0;
  const selectedCount = selectedPlanArtifactIds.length;
  const pendingCount = Math.max(planTotalArtifacts, pendingArtifacts.length);
  const headerPublishCount = selectedCount > 0 ? selectedCount : planTotalArtifacts > 0 ? planTotalArtifacts : pendingArtifacts.length;
  const canReview = activeSource?.type === 'workspace' && (pendingCount > 0 || blockedCollections > 0);

  const syncSelectionToPlan = useCallback((previewResponse?: WorkspaceCommitResponse | null) => {
    if (!previewResponse) {
      setSelectedPlanArtifactIds([]);
      return;
    }
    const selectable = previewResponse.plan.artifacts
      .filter((artifact: ArtifactCommitChange) => artifact.action !== 'skipped')
      .map((artifact: ArtifactCommitChange) => artifact.artifact_id);
    setSelectedPlanArtifactIds(selectable);
  }, []);

  const handleRefreshPreview = useCallback(async (): Promise<WorkspaceCommitResponse | null> => {
    setIsPreviewLoading(true);
    const result = await fetchCommitPreview();
    setIsPreviewLoading(false);
    syncSelectionToPlan(result);
    return result;
  }, [fetchCommitPreview, syncSelectionToPlan]);

  const handleOpenReview = useCallback(async () => {
    if (!canReview) return;
    setIsPreviewLoading(true);
    const result = await fetchCommitPreview();
    setIsPreviewLoading(false);
    if (result) {
      syncSelectionToPlan(result);
      setIsReviewOpen(true);
    }
  }, [canReview, fetchCommitPreview, syncSelectionToPlan]);

  const handlePublish = useCallback(() => {
    if (selectedPlanArtifactIds.length === 0) {
      return;
    }
    const token = commitPreview?.commit_token ?? undefined;
    setIsReviewOpen(false);
    clearCommitPreview();
    commitCurrentWorkspace({ artifact_ids: selectedPlanArtifactIds, commit_token: token });
    setSelectedPlanArtifactIds([]);
  }, [clearCommitPreview, commitCurrentWorkspace, commitPreview, selectedPlanArtifactIds]);

  const handleDialogOpenChange = useCallback((open: boolean) => {
    if (!open) {
      setIsReviewOpen(false);
      clearCommitPreview();
      setSelectedPlanArtifactIds([]);
      return;
    }
    setIsReviewOpen(true);
    if (!commitPreview) {
      void handleRefreshPreview();
    } else {
      syncSelectionToPlan(commitPreview);
    }
  }, [clearCommitPreview, commitPreview, handleRefreshPreview, syncSelectionToPlan]);

  const handleTogglePlanArtifact = useCallback((artifactId: string) => {
    const planArtifact = commitPreview?.plan.artifacts.find(artifact => artifact.artifact_id === artifactId);
    if (planArtifact && planArtifact.action === 'skipped') {
      return;
    }
    setSelectedPlanArtifactIds(prev => (prev.includes(artifactId)
      ? prev.filter(id => id !== artifactId)
      : [...prev, artifactId]));
  }, [commitPreview]);

  const handleSelectAllPlanArtifacts = useCallback(() => {
    if (!commitPreview) return;
    const selectable = commitPreview.plan.artifacts
      .filter((artifact: ArtifactCommitChange) => artifact.action !== 'skipped')
      .map((artifact: ArtifactCommitChange) => artifact.artifact_id);
    setSelectedPlanArtifactIds(selectable);
  }, [commitPreview]);

  const handleClearPlanSelection = useCallback(() => {
    setSelectedPlanArtifactIds([]);
  }, []);

  // Resolve display artifacts from search results or source artifacts
  useEffect(() => {
    const resolveArtifacts = async () => {
      // If search was performed (externalSearchResults is not null), show those results
      // even if empty (0 hits) - this lets us show "0 results" message
      if (externalSearchResults !== null) {
        if (externalSearchResults.hits.length > 0) {
          const artifacts = await convertSearchHitsToArtifacts(externalSearchResults.hits);
          setResolvedDisplayArtifacts(artifacts);
        } else {
          // Empty search results - show empty state
          setResolvedDisplayArtifacts([]);
          setSearchMetadataIndex(new Map()); // Clear metadata
        }
      } else {
        // No search - show source artifacts (inbox or collection)
        setResolvedDisplayArtifacts(sourceArtifacts);
        setSearchMetadataIndex(new Map()); // Clear metadata when not searching
      }
    };
    
    resolveArtifacts();
  }, [externalSearchResults, sourceArtifacts, convertSearchHitsToArtifacts]);

  // Publish the currently-displayed artifacts to workspace context so preview can render non-workspace artifacts
  useEffect(() => {
    if (typeof setDisplayedArtifacts === 'function') setDisplayedArtifacts(resolvedDisplayArtifacts);
  }, [resolvedDisplayArtifacts, setDisplayedArtifacts]);
  
  // Register keyboard shortcuts for artifact selection
  const { registerShortcut } = useShortcuts();
  
  useEffect(() => {
    // Only register shortcuts for workspace views (not collections or search results)
    if (activeSource?.type !== 'workspace') return;

    const cleanups: (() => void)[] = [];

    // Select all artifacts
    cleanups.push(registerShortcut({
      id: 'workspace:select-all',
      label: SHORTCUTS.SELECT_ALL.LABEL,
      group: 'Workspace',
      combos: ['mod+a'],
      handler: (e) => {
        e.preventDefault();
        selectAllArtifacts();
      },
      options: {
        description: SHORTCUTS.SELECT_ALL.DESCRIPTION
      }
    }));

    // Clear selection
    cleanups.push(registerShortcut({
      id: 'workspace:clear-selection',
      label: SHORTCUTS.CLEAR_SELECTION.LABEL,
      group: 'Workspace',
      combos: ['escape'],
      handler: (e) => {
        // Only clear if artifacts are selected
        if (selectedArtifactIds.length > 0) {
          e.preventDefault();
          unselectAllArtifacts();
        }
      },
      options: {
        description: SHORTCUTS.CLEAR_SELECTION.DESCRIPTION
      }
    }));

    return () => {
      cleanups.forEach(cleanup => cleanup());
    };
  }, [activeSource?.type, registerShortcut, selectAllArtifacts, selectedArtifactIds.length, unselectAllArtifacts]);
  
  // When showing search results (even if empty), bypass workspace search
  const isShowingSearchResults = externalSearchResults !== null;
  
  // Additional filter chips state
  const [activeStateFilters, setActiveStateFilters] = useState<Set<string>>(new Set());
  const [activeContentTypeFilters, setActiveContentTypeFilters] = useState<Set<string>>(new Set());
  const [hiddenContentTypeFilters, setHiddenContentTypeFilters] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(HIDDEN_CONTENT_TYPE_FILTERS_STORAGE_KEY);
      if (!raw) return new Set();
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? new Set(parsed.map(String)) : new Set();
    } catch {
      return new Set();
    }
  });

  useEffect(() => {
    localStorage.setItem(HIDDEN_CONTENT_TYPE_FILTERS_STORAGE_KEY, JSON.stringify(Array.from(hiddenContentTypeFilters)));
  }, [hiddenContentTypeFilters]);

  useEffect(() => {
    localStorage.setItem(STACKED_ARTIFACT_FAMILIES_STORAGE_KEY, String(stackArtifactFamilies));
  }, [stackArtifactFamilies]);
  
  const bulkSelectedArtifacts = useMemo(
    () => artifacts.filter((artifact) => selectedArtifactIds.includes(String(artifact.id))),
    [artifacts, selectedArtifactIds]
  );

  // Calculate counts for filter bar (use resolvedDisplayArtifacts which includes search results)
  const counts = useMemo(() => ({
    total: resolvedDisplayArtifacts.length,
    new: resolvedDisplayArtifacts.filter((c: Artifact) => c.state === 'draft').length,
    modified: resolvedDisplayArtifacts.filter((c: Artifact) => c.state === 'committed').length,
    archived: resolvedDisplayArtifacts.filter((c: Artifact) => c.state === 'archived').length,
  }), [resolvedDisplayArtifacts]);

  // Filter and sort artifacts
  const filteredArtifacts = useMemo(() => {
    // When showing search results, skip workspace search filtering
    let result = isShowingSearchResults ? resolvedDisplayArtifacts : searchFilteredArtifacts;

    // Strip the workspace container artifact card. In the unified model, the workspace
    // container artifact uses the workspace ID as both id/root_id and should never
    // render as a normal card inside its own workspace view.
    if (activeSource?.type === 'workspace' && workspaceId) {
      result = result.filter((c: Artifact) => {
        const artifactId = String(c.id ?? '');
        const rootId = String(c.root_id ?? '');
        return artifactId !== workspaceId && rootId !== workspaceId;
      });
    }

    if (hiddenContentTypeFilters.size > 0) {
      result = result.filter((c: Artifact) => !hiddenContentTypeFilters.has(getArtifactContentTypeCategory(c)));
    }

    // Apply state filter from FilterBar
    if (activeFilter !== 'all') {
      result = result.filter((c: Artifact) => c.state === activeFilter);
    }
    
    // Apply additional state filters from FilterChips
    if (activeStateFilters.size > 0) {
      result = result.filter((c: Artifact) => activeStateFilters.has(c.state || ''));
    }
    
    // Apply MIME type filters from FilterChips
    if (activeContentTypeFilters.size > 0) {
      result = result.filter((c: Artifact) => activeContentTypeFilters.has(getArtifactContentTypeCategory(c)));
    }

    // Apply sort
    result = [...result].sort((a, b) => {
      switch (sortBy) {
        case 'title': {
          // Extract title from context or use empty string
          const getTitleFromContext = (artifact: Artifact) => {
            try {
              const ctx = typeof artifact.context === 'string' ? JSON.parse(artifact.context) : artifact.context;
              return ctx?.filename || ctx?.title || '';
            } catch {
              return '';
            }
          };
          const titleA = getTitleFromContext(a);
          const titleB = getTitleFromContext(b);
          return titleA.localeCompare(titleB);
        }
        case 'created':
          return (new Date(b.created_time || 0).getTime()) - (new Date(a.created_time || 0).getTime());
        case 'committed':
          return (new Date(b.modified_time || b.created_time || 0).getTime()) -
                 (new Date(a.modified_time || a.created_time || 0).getTime());
        case 'manual':
          // Use order_key for manual sorting
          {
            const ka = (a.order_key || '');
            const kb = (b.order_key || '');
            const primary = ka.localeCompare(kb);
            if (primary !== 0) return primary;

            const ta = new Date(a.created_time || 0).getTime();
            const tb = new Date(b.created_time || 0).getTime();
            if (ta !== tb) return ta - tb;

            return String(a.id || '').localeCompare(String(b.id || ''));
          }
        case 'recent':
        default:
          return (new Date(b.created_time || 0).getTime()) - (new Date(a.created_time || 0).getTime());
      }
    });

    return result;
  }, [searchFilteredArtifacts, resolvedDisplayArtifacts, isShowingSearchResults, activeFilter, activeStateFilters, activeContentTypeFilters, hiddenContentTypeFilters, sortBy, activeSource?.type, workspaceId]);

  const artifactCountArtifacts = useMemo(
    () => deriveArtifactCountArtifacts({
      searchFilteredArtifacts,
      resolvedDisplayArtifacts,
      isShowingSearchResults,
      activeSourceType: activeSource?.type,
    }),
    [searchFilteredArtifacts, resolvedDisplayArtifacts, isShowingSearchResults, activeSource?.type]
  );
  const stackedArtifacts = useMemo(() => buildArtifactSections(filteredArtifacts), [filteredArtifacts]);
  const artifactCountsByOwnerId = useMemo(() => buildArtifactCountsByOwnerId(artifactCountArtifacts), [artifactCountArtifacts]);
  const shouldUseArtifactStacks =
    stackArtifactFamilies &&
    activeSource?.type === 'workspace' &&
    !isShowingSearchResults &&
    stackedArtifacts.sections.length > 0;
  const showFloatingToolbar = activeSource?.type === 'workspace' && bulkSelectedArtifacts.length > 0;
  const bulkHasNew = useMemo(
    () => bulkSelectedArtifacts.some(a => a.state === 'draft'),
    [bulkSelectedArtifacts]
  );
  const bulkHasCommitted = useMemo(
    () => bulkSelectedArtifacts.some(a => a.state === 'committed'),
    [bulkSelectedArtifacts]
  );
  
  // Handlers for filter chips
  const handleStateFilterToggle = useCallback((state: string) => {
    setActiveStateFilters(prev => {
      const next = new Set(prev);
      if (next.has(state)) {
        next.delete(state);
      } else {
        next.add(state);
      }
      return next;
    });
  }, []);
  
  const handleContentTypeFilterToggle = useCallback((contentType: string) => {
    setActiveContentTypeFilters(prev => {
      const next = new Set(prev);
      if (next.has(contentType)) {
        next.delete(contentType);
      } else {
        next.add(contentType);
      }
      return next;
    });
  }, []);

  const handleHiddenContentTypeToggle = useCallback((contentType: string) => {
    setHiddenContentTypeFilters(prev => {
      const next = new Set(prev);
      if (next.has(contentType)) {
        next.delete(contentType);
      } else {
        next.add(contentType);
      }
      return next;
    });

    setActiveContentTypeFilters(prev => {
      if (!prev.has(contentType)) return prev;
      const next = new Set(prev);
      next.delete(contentType);
      return next;
    });
  }, []);

  const handleClearAllFilters = useCallback(() => {
    setActiveStateFilters(new Set());
    setActiveContentTypeFilters(new Set());
    setHiddenContentTypeFilters(new Set());
  }, []);

  const toggleArtifactSection = useCallback((sectionId: string) => {
    setCollapsedArtifactSections((prev) => ({
      ...prev,
      [sectionId]: !(prev[sectionId] ?? true),
    }));
  }, []);

  const handleBulkMove = useCallback(() => {
    setShowBulkCollectionPicker(true);
  }, []);

  const handleBulkSelectCollections = useCallback(async (collectionIds: string[]) => {
    for (const artifactId of selectedArtifactIds) {
      // Add artifact to each selected collection via edge operations
      for (const collectionId of collectionIds) {
        try {
          await addArtifactToCollection(collectionId, artifactId);
        } catch {
          // Ignore errors (e.g., already exists)
        }
      }
    }
    setShowBulkCollectionPicker(false);
    unselectAllArtifacts();
  }, [selectedArtifactIds, unselectAllArtifacts]);

  const handleBulkAddTags = useCallback(async () => {
    setShowBulkTagDialog(true);
  }, []);

  const handleBulkApplyTags = useCallback(async ({ tags, replaceExisting }: BulkTagApplyRequest) => {
    for (const artifact of bulkSelectedArtifacts) {
      const context = safeParseArtifactContext(artifact.context);
      const currentTags = Array.isArray(context.tags) ? context.tags.map(String) : [];
      await updateArtifact({
        id: String(artifact.id),
        context: stringifyArtifactContext({
          ...context,
          tags: replaceExisting ? tags : Array.from(new Set([...currentTags, ...tags])),
        }),
      });
    }
  }, [bulkSelectedArtifacts, updateArtifact]);

  const browserPrefs = useMemo(
    () => ((preferences.browser as Record<string, unknown> | undefined) ?? {}),
    [preferences.browser]
  );

  const dockedWorkspaceTabIdsPref = useMemo(() => {
    const raw = (browserPrefs as { dockedWorkspaceCardIds?: unknown }).dockedWorkspaceCardIds;
    return Array.isArray(raw) ? raw.map(String).filter(Boolean) : undefined;
  }, [browserPrefs]);

  // If the artifact is a workspace card that points to a docked workspace tab, undock that workspace.
  const undockWorkspaceIfNeeded = useCallback((artifact: Artifact) => {
    const ctx = safeParseArtifactContext(artifact.context);
    if (ctx.content_type !== WORKSPACE_CONTENT_TYPE) return;
    const wsId = typeof ctx.workspace_id === 'string' ? ctx.workspace_id : undefined;
    if (!wsId || (dockedWorkspaceTabIdsPref && !dockedWorkspaceTabIdsPref.includes(wsId))) return;
    if (!workspaces.some(w => w.id === wsId)) return;

    const nextDocked = dockedWorkspaceTabIdsPref
      ? dockedWorkspaceTabIdsPref.filter(id => id !== wsId)
      : workspaces.filter(w => w.id !== wsId).map(w => w.id);

    void updatePreferences({ browser: { ...browserPrefs, dockedWorkspaceCardIds: nextDocked } });
  }, [browserPrefs, dockedWorkspaceTabIdsPref, updatePreferences, workspaces]);

  const handleBulkArchive = useCallback(async () => {
    const items = bulkSelectedArtifacts.filter(a => a.state === 'committed');
    const count = items.length;
    if (count === 0) return;

    for (const artifact of items) {
      undockWorkspaceIfNeeded(artifact);
      await updateArtifact({ id: String(artifact.id), state: 'archived' });
    }
    unselectAllArtifacts();
  }, [bulkSelectedArtifacts, unselectAllArtifacts, updateArtifact, undockWorkspaceIfNeeded]);

  const handleBulkDrop = useCallback(async () => {
    const items = bulkSelectedArtifacts.filter(a => a.state === 'committed');
    const count = items.length;
    if (count === 0) return;

    for (const artifact of items) {
      undockWorkspaceIfNeeded(artifact);
      await removeArtifact(String(artifact.id));
    }
    unselectAllArtifacts();
  }, [bulkSelectedArtifacts, removeArtifact, unselectAllArtifacts, undockWorkspaceIfNeeded]);

  const handleBulkDelete = useCallback(async () => {
    const items = bulkSelectedArtifacts.filter(a => a.state === 'draft');
    const count = items.length;
    if (count === 0) return;
    const confirmed = await dangerConfirm.confirm({
      title: BULK_CONFIRM.DELETE_TITLE(count),
      description: BULK_CONFIRM.DELETE_DESCRIPTION(count),
      confirmLabel: BUTTON_LABELS.DELETE,
      cancelLabel: BUTTON_LABELS.CANCEL
    });

    if (!confirmed) return;

    for (const artifact of items) {
      undockWorkspaceIfNeeded(artifact);
      await removeArtifact(String(artifact.id));
    }
    unselectAllArtifacts();
  }, [dangerConfirm, bulkSelectedArtifacts, removeArtifact, unselectAllArtifacts, undockWorkspaceIfNeeded]);

  // Entire-workspace dropzone state
  const [, setDragDepth] = useState(0);
  const [, setWorkspaceDragKind] = useState<'file' | 'artifacts' | null>(null);
  const depthRef = useRef(0);

  const isFileDrag = (dt: DataTransfer | null | undefined) =>
    !!dt?.types?.includes("Files") || !!dt?.types?.includes("public.file-url");

  // Accept artifact drags on workspace background (append to end)
  const isArtifactsDrag = (dt: DataTransfer | null | undefined) => {
    if (!dt?.types) return false;
    const types = Array.from(dt.types);
    return (
      types.includes(AGIENCE_DRAG_CONTENT_TYPE) ||
      types.includes("application/json") ||
      types.includes("text/plain")
    );
  };

  const resetWorkspaceDragState = useCallback(() => {
    depthRef.current = 0;
    setDragDepth(0);
    setWorkspaceDragKind(null);
  }, []);

  useEffect(() => {
    window.addEventListener('dragend', resetWorkspaceDragState);
    window.addEventListener('drop', resetWorkspaceDragState);
    return () => {
      window.removeEventListener('dragend', resetWorkspaceDragState);
      window.removeEventListener('drop', resetWorkspaceDragState);
    };
  }, [resetWorkspaceDragState]);

  const isInlineText = (file: File) => {
    if (file.size > INLINE_BYTES_MAX) return false;
    const t = (file.type || "").toLowerCase();
    if (t.startsWith("text/")) return true;
    return ["application/json", "application/xml", "application/x-yaml", "application/javascript", "text/markdown"].includes(t);
  };

  useEffect(() => {
    const unsubscribe = registerNewArtifactHandler((artifact: ArtifactCreate | ArtifactUpdate) => {
      setEditArtifact(artifact);
    });
    return unsubscribe;
  }, [registerNewArtifactHandler]);

  const handleSave = async (artifact: ArtifactCreate | ArtifactUpdate) => {
    if ("id" in artifact && artifact.id) {
      await updateArtifact(artifact as ArtifactUpdate & { id: string });
    } else {
      await addArtifact(artifact as ArtifactCreate);
    }
    setEditArtifact(null);
  };

  const handleClose = () => setEditArtifact(null);

  const handleOpenTypePicker = useCallback(() => {
    if (activeSource?.type !== 'workspace') return;
    setIsArtifactTypePickerOpen(true);
  }, [activeSource?.type]);

  // [+] creates a new workspace (the backend indexes its container artifact for search),
  // then switches to the new workspace. No card is dropped into the current workspace.
  const handleCreateWorkspace = useCallback(async () => {
    const ws = await createWorkspace('Untitled Workspace', '', { activate: false });
    setActiveWorkspaceId(ws.id);
  }, [createWorkspace, setActiveWorkspaceId]);

  // Double-click tab to rename workspace. Also updates the workspace container artifact
  // artifact so the new name is reflected in search results.
  const handleRenameWorkspace = useCallback(async (id: string, name: string) => {
    await updateWorkspace({ id, name });
  }, [updateWorkspace]);

  // ---------------------------------------------------------------------------
  // Resolve dropped search-result IDs against known artifact pools.
  // Returns classified buckets so callers can route each artifact correctly.
  // ---------------------------------------------------------------------------
  type ResolvedDrop = {
    /** Collection-backed artifacts importable by root_id */
    collectionImports: string[];
    /** Workspace-native artifacts that need to be copied (content + context) */
    workspaceCopies: Artifact[];
    /** IDs already present in the active workspace (skip / reorder only) */
    alreadyPresent: string[];
    /** IDs not found in any known artifact pool — treated as collection root_ids */
    unresolved: string[];
  };

  const resolveDroppedIds = useCallback((draggedIds: string[]): ResolvedDrop => {
    const pool = [...searchResultArtifacts, ...resolvedDisplayArtifacts, ...artifacts, ...sourceArtifacts];
    const activeWsId = activeWorkspace?.id;

    const result: ResolvedDrop = {
      collectionImports: [],
      workspaceCopies: [],
      alreadyPresent: [],
      unresolved: [],
    };

    for (const id of draggedIds) {
      // Find the artifact in any available pool (search results, workspace, source)
      const artifact = pool.find(
        a => String(a.id ?? '') === id || String(a.root_id ?? '') === id,
      );

      if (!artifact) {
        // Not found in any pool — assume it's a collection root_id from a non-search drop
        result.unresolved.push(id);
        continue;
      }

      // Already in the active workspace?
      if (artifact.collection_id && artifact.collection_id === activeWsId) {
        result.alreadyPresent.push(id);
        continue;
      }

      // Has a root_id we can import from?
      const rootId = artifact.root_id ? String(artifact.root_id).trim() : '';
      if (rootId) {
        result.collectionImports.push(rootId);
        continue;
      }

      // Artifact without root_id — copy it
      if (artifact.collection_id) {
        result.workspaceCopies.push(artifact);
        continue;
      }

      // No root_id and no collection_id — treat as unresolved
      result.unresolved.push(id);
    }

    return result;
  }, [activeWorkspace?.id, artifacts, resolvedDisplayArtifacts, searchResultArtifacts, sourceArtifacts]);

  // ---------------------------------------------------------------------------
  // Unified drop handler: resolves IDs first, then dispatches import or copy.
  // ---------------------------------------------------------------------------
  const handleResolvedDrop = useCallback(async (
    draggedIds: string[],
    insertIndex: number,
    dragPayload?: {
      rootIds?: string[];
      versionIds?: string[];
      sourceType?: string;
      workspaceId?: string;
    },
  ) => {
    if (activeSource?.type === 'collection') {
      const targetCollectionId = activeSource.id;
      if (!targetCollectionId) return;

      if (dragPayload?.sourceType === 'workspace' || dragPayload?.workspaceId) {
        for (const id of draggedIds) {
          try {
            await addArtifactToCollection(targetCollectionId, String(id));
          } catch {
            // Ignore errors (e.g., already exists)
          }
        }
        return;
      }

      const versionIds = Array.isArray(dragPayload?.versionIds)
        ? dragPayload.versionIds.map(String).map((value) => value.trim()).filter(Boolean)
        : [];
      const fallbackVersionIds = draggedIds
        .map((id) => {
          const artifact = [...searchResultArtifacts, ...resolvedDisplayArtifacts, ...artifacts, ...sourceArtifacts]
            .find((candidate) => String(candidate.id ?? '') === id || String(candidate.root_id ?? '') === id);
          return artifact?.id ? String(artifact.id).trim() : '';
        })
        .filter(Boolean);
      const idsToAdd = [...versionIds, ...fallbackVersionIds]
        .filter((value, index, array) => array.indexOf(value) === index)
        .filter((value) => value !== targetCollectionId);

      for (const versionId of idsToAdd) {
        await addArtifactToCollection(targetCollectionId, versionId);
      }

      if (targetCollectionId === activeSource.id) {
        const refreshed = await getCollectionArtifacts(targetCollectionId);
        setSourceArtifacts(refreshed as Artifact[]);
      }
      return;
    }

    // Prevent dropping a workspace onto itself (container cannot contain itself)
    const filteredDraggedIds = draggedIds.filter(id => id !== activeWorkspace?.id);
    if (filteredDraggedIds.length === 0) {
      // All dropped artifacts were self-drops; nothing to do
      return;
    }

    const { collectionImports, workspaceCopies, unresolved } = resolveDroppedIds(filteredDraggedIds);

    // 1. Collection imports — combine classified root_ids, unresolved IDs, and any root_ids from the drag payload
    const fallbackImportIds = Array.isArray(dragPayload?.rootIds) ? dragPayload.rootIds.filter(Boolean) : [];
    const importIds = [...collectionImports, ...unresolved, ...fallbackImportIds]
      .filter((value, index, array) => array.indexOf(value) === index);
    if (importIds.length > 0) {
      importArtifactsByRootIds(importIds, insertIndex);
    }

    // 2. Workspace-native copies — create new artifacts with same content + context
    if (workspaceCopies.length > 0) {
      let offset = importIds.length;
      for (const src of workspaceCopies) {
        await createArtifact(
          { content: src.content ?? '', context: src.context ?? '' },
          insertIndex + offset,
        );
        offset += 1;
      }
    }
  }, [activeSource, activeWorkspace, artifacts, createArtifact, importArtifactsByRootIds, resolveDroppedIds, resolvedDisplayArtifacts, searchResultArtifacts, sourceArtifacts]);

  const createWorkspaceFromDroppedArtifacts = useCallback(async (draggedIds: string[]) => {
    // Prevent dropping a workspace onto itself (container cannot contain itself)
    const filteredDraggedIds = draggedIds.filter(id => id !== activeWorkspace?.id);
    if (filteredDraggedIds.length === 0) {
      // All dropped artifacts were self-drops; nothing to do
      return false;
    }

    const { collectionImports, workspaceCopies, unresolved } = resolveDroppedIds(filteredDraggedIds);
    const importIds = [...collectionImports, ...unresolved];
    if (importIds.length === 0 && workspaceCopies.length === 0) return false;

    const ws = await createWorkspace('Untitled Workspace', '', { activate: false });

    for (const rootId of importIds) {
      try {
        await importCollectionArtifactToWorkspace(ws.id, rootId);
      } catch {
        // Ignore unreadable or already-imported artifacts.
      }
    }

    for (const src of workspaceCopies) {
      try {
        await addArtifactToWorkspace(ws.id, {
          content: src.content ?? '',
          context: typeof src.context === 'string' ? src.context : stringifyArtifactContext(src.context ?? {}),
        });
      } catch {
        // Ignore copy failures so one bad artifact doesn't block the workspace creation.
      }
    }

    setActiveWorkspaceId(ws.id);
    return true;
  }, [activeWorkspace, createWorkspace, resolveDroppedIds, setActiveWorkspaceId]);

  // Drop a workspace card on the header: check workspace_id from context or use the
  // artifact's root_id directly. If no matching workspace, create one on the fly.
  const handleDropWorkspace = useCallback(async (draggedIds: string[]) => {
    // Prevent dropping a workspace onto itself (container cannot contain itself)
    const filteredDraggedIds = draggedIds.filter(id => id !== activeWorkspace?.id);
    if (filteredDraggedIds.length === 0) {
      // All dropped artifacts were self-drops; nothing to do
      return;
    }

    const allArtifacts = [...searchResultArtifacts, ...resolvedDisplayArtifacts, ...artifacts, ...displayedArtifacts, ...sourceArtifacts];

    for (const id of filteredDraggedIds) {
      if (workspaces.some(w => String(w.id) === String(id))) {
        if (dockedWorkspaceTabIdsPref) {
          const nextDocked = dockedWorkspaceTabIdsPref.includes(id)
            ? dockedWorkspaceTabIdsPref
            : [...dockedWorkspaceTabIdsPref, id];
          if (nextDocked.length !== dockedWorkspaceTabIdsPref.length) {
            void updatePreferences({ browser: { ...browserPrefs, dockedWorkspaceCardIds: nextDocked } });
          }
        }
        setActiveWorkspaceId(id);
        return;
      }

      const artifact = allArtifacts.find(a => String(a.id ?? '') === id || String(a.root_id ?? '') === id);
      if (!artifact) continue;
      const ctx = safeParseArtifactContext(artifact.context);
      if (ctx.content_type !== WORKSPACE_CONTENT_TYPE) continue;

      const wsId =
        (typeof ctx.workspace_id === 'string' ? ctx.workspace_id : undefined) ??
        workspaces.find(w =>
          String(w.id) === String(artifact.id ?? '') ||
          String(w.id) === String(artifact.root_id ?? '') ||
          String(w.id) === String(artifact.collection_id ?? '')
        )?.id;

      if (wsId && workspaces.some(w => String(w.id) === wsId)) {
        if (dockedWorkspaceTabIdsPref) {
          const nextDocked = dockedWorkspaceTabIdsPref.includes(wsId)
            ? dockedWorkspaceTabIdsPref
            : [...dockedWorkspaceTabIdsPref, wsId];
          if (nextDocked.length !== dockedWorkspaceTabIdsPref.length) {
            void updatePreferences({ browser: { ...browserPrefs, dockedWorkspaceCardIds: nextDocked } });
          }
        }
        setActiveWorkspaceId(wsId);
        return;
      }

      const title = typeof ctx.title === 'string' ? ctx.title : 'Untitled Workspace';
      const ws = await createWorkspace(title, '');
      if (artifact.id) {
        void updateArtifact({
          id: String(artifact.id),
          context: stringifyArtifactContext({ ...ctx, workspace_id: ws.id }),
        });
      }
      setActiveWorkspaceId(ws.id);
      return;
    }

    const { collectionImports, workspaceCopies, unresolved } = resolveDroppedIds(draggedIds);
    const hasItemsToImport = [...collectionImports, ...unresolved, ...workspaceCopies].length > 0;
    if (hasItemsToImport) {
      await createWorkspaceFromDroppedArtifacts(draggedIds);
    }
  }, [activeWorkspace, artifacts, browserPrefs, createWorkspace, createWorkspaceFromDroppedArtifacts, displayedArtifacts, dockedWorkspaceTabIdsPref, resolveDroppedIds, resolvedDisplayArtifacts, searchResultArtifacts, setActiveWorkspaceId, sourceArtifacts, updateArtifact, updatePreferences, workspaces]);

  const handleSelectArtifactType = useCallback(async (contentType: string) => {
    const type = getContentTypeById(contentType);
    setIsArtifactTypePickerOpen(false);
    const created = await createArtifact({
      content_type: contentType,
      content: '',
      context: stringifyArtifactContext({
        title: `Untitled ${type?.label || 'Artifact'}`,
      }),
    });
    if (!created) return;
    const startInEditMode = Boolean(type?.states.includes('edit'));
    onOpenArtifact?.(created, startInEditMode ? { startInEditMode: true } : undefined);
  }, [createArtifact, onOpenArtifact]);

  // Upload files: inline text stored directly, binary files uploaded to S3
  const uploadFilesAt = useCallback(async (files: File[], insertIndex: number) => {
    if (!workspaceId || !files.length) return;
    
    const newArtifactIds: string[] = [];
    
    // Phase 1: Initiate all uploads and create all artifacts immediately (shows all artifacts at once)
    const uploadTasks: Array<{
      file: File;
      artifactId: string;
      mode: 'inline' | 'put' | 'multipart';
      url?: string;
    }> = [];
    
    for (let i = 0; i < files.length; i++) {
      const file = files[i];

      // Small text files: store content directly in artifact (no S3)
      if (isInlineText(file)) {
        const text = await file.text();
        const inlineCtx = {
                filename: file.name,
                mime: file.type || "text/plain",
                content_type: file.type || "text/plain",
                size: file.size,
                processing: { strategy: "deterministic", status: "ready" },
              };
        const created: Artifact | ArtifactCreate | null = await (createArtifact
          ? createArtifact({
              context: JSON.stringify(inlineCtx),
              content: text
            }, insertIndex + i)
          : (async () => {
              await addArtifact({
                context: JSON.stringify(inlineCtx),
                content: text
              });
              return null;
            })());
        const artifactId = String((created && 'id' in created ? created.id : undefined) ?? latestArtifactsRef.current[latestArtifactsRef.current.length - 1]?.id);
        if (artifactId) {
          newArtifactIds.push(artifactId);
          uploadTasks.push({ file, artifactId, mode: 'inline' });
        }
      } else {
        // Large or binary files: upload to S3, backend creates artifact
        // Compute order_key to place at correct position
        const targetIndex = insertIndex + i;
        const prevKey = targetIndex > 0 ? latestArtifactsRef.current[targetIndex - 1]?.order_key : null;
        const nextKey = targetIndex < latestArtifactsRef.current.length ? latestArtifactsRef.current[targetIndex]?.order_key : null;
        const order_key = midKey(prevKey || null, nextKey || null);
        
        const init = await initiateUpload(workspaceId, {
          filename: file.name,
          content_type: file.type || "application/octet-stream",
          size: file.size,
          order_key,
        });
        const artifactId = init.upload_id;
        newArtifactIds.push(artifactId);

        // Add the artifact to local state immediately so it appears during upload
        addExistingArtifact(init.artifact as unknown as Artifact);

        // Queue upload task for phase 2
        if (init.mode === "put" && init.url) {
          uploadTasks.push({ file, artifactId, mode: 'put', url: init.url });
        } else if (init.mode === "multipart") {
          uploadTasks.push({ file, artifactId, mode: 'multipart' });
        } else {
          throw new Error(
            `Unexpected upload mode: ${init.mode}. Please contact support.`
          );
        }
      }
    }
    
    // Phase 2: Process uploads sequentially (artifacts already visible)
    for (const task of uploadTasks) {
      if (task.mode === 'inline') {
        // Already completed in phase 1
        continue;
      }

      try {
        if (task.mode === 'put' && task.url) {
          // Single PUT for files <= 100MB
          await uploadWithProgress(workspaceId, task.artifactId, task.url, task.file, (progress) => {
            // Update local artifact state immediately for UI responsiveness
            if (updateArtifact) {
              const ctx = latestArtifactsRef.current.find(c => c.id === task.artifactId)?.context;
              if (ctx) {
                try {
                  const parsed = JSON.parse(ctx);
                  parsed.upload = { ...parsed.upload, status: "uploading", progress };
                  updateArtifact({ id: task.artifactId, context: JSON.stringify(parsed) });
                } catch { /* ignore JSON parse errors */ }
              }
            }
          });

          // Notify backend that upload is complete
          const completedArtifact = await updateUploadStatus(workspaceId, task.artifactId, { status: "complete" });
          if (updateArtifact) {
            updateArtifact(completedArtifact);
          }
        } else if (task.mode === 'multipart') {
          // Multipart upload for files > 100MB
          await uploadMultipart(workspaceId, task.artifactId, task.file, (progress) => {
            // Update local artifact state
            if (updateArtifact) {
              const ctx = latestArtifactsRef.current.find(c => c.id === task.artifactId)?.context;
              if (ctx) {
                try {
                  const parsed = JSON.parse(ctx);
                  parsed.upload = { ...parsed.upload, status: "uploading", progress };
                  updateArtifact({ id: task.artifactId, context: JSON.stringify(parsed) });
                } catch { /* ignore */ }
              }
            }
          });

          // Get latest artifact context for upload metadata
          const artifact = latestArtifactsRef.current.find(c => c.id === task.artifactId);
          let context_patch = undefined;
          if (artifact && artifact.context) {
            try {
              const parsed = JSON.parse(artifact.context);
              if (parsed.upload) {
                context_patch = { upload: parsed.upload };
              }
            } catch { /* ignore */ }
          }
          // Update local state with completed artifact
          const completedArtifact = await updateUploadStatus(workspaceId, task.artifactId, { status: "complete", context_patch });
          if (updateArtifact) {
            updateArtifact(completedArtifact);
          }
        }
      } catch (err) {
        const filename = task.file.name;
        toast.error(`Upload failed: ${filename}`, {
          description: err instanceof Error ? err.message : 'Unknown error',
        });
        // Mark upload as failed on backend
        try {
          await updateUploadStatus(workspaceId, task.artifactId, { status: "failed" });
        } catch { /* best-effort */ }
      }
    }
    
    // No need to refresh - artifacts were added to local state during initiation
    // and updated during upload progress and completion
    
    // Artifacts are created with correct order_key and inserted at correct position
    // No need to reorder after upload
  }, [workspaceId, createArtifact, addArtifact, addExistingArtifact, updateArtifact, latestArtifactsRef]);

  // Simplified drag handlers - just for visual feedback if needed
  const onDragEnter = useCallback((e: React.DragEvent) => {
    if (!(isFileDrag(e.dataTransfer) || isArtifactsDrag(e.dataTransfer))) return;
    depthRef.current += 1;
    setDragDepth(depthRef.current);
    setWorkspaceDragKind(isFileDrag(e.dataTransfer) ? 'file' : 'artifacts');
  }, []);

  const onDragLeave = useCallback((e: React.DragEvent) => {
    if (!(isFileDrag(e.dataTransfer) || isArtifactsDrag(e.dataTransfer))) return;
    depthRef.current = Math.max(0, depthRef.current - 1);
    setDragDepth(depthRef.current);
    if (depthRef.current === 0) {
      setWorkspaceDragKind(null);
    }
  }, []);

  const hasRealDockedWorkspace = Boolean(activeWorkspace?.id);
  const shouldShowNoSourceState = !activeSource || (activeSource.type === 'workspace' && !hasRealDockedWorkspace);

  // Workspace-level drop handlers for empty state (when CardGrid isn't rendered)
  const onWorkspaceDragOver = useCallback((e: React.DragEvent) => {
    const artifactDrag = isArtifactsDrag(e.dataTransfer);
    const fileDrag = isFileDrag(e.dataTransfer);

    if (!workspaceId) {
      if (!shouldShowNoSourceState || !artifactDrag) return;
    } else if (!(fileDrag || artifactDrag)) {
      return;
    }
    
    e.preventDefault();
    e.stopPropagation();
    setWorkspaceDragKind(fileDrag ? 'file' : 'artifacts');
    try { 
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'; 
    } catch { 
      // ignore
    }
  }, [shouldShowNoSourceState, workspaceId]);

  const parseDraggedArtifactIds = useCallback((e: React.DragEvent): string[] => {
    const resolvedIds = getDroppedArtifactIds(e.dataTransfer);
    if (resolvedIds.length > 0) return resolvedIds;

    try {
      const raw = e.dataTransfer.getData('application/x-agience-artifact');
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && Array.isArray(parsed.ids)) {
          return parsed.ids.map(String).filter(Boolean);
        }
      }
    } catch {
      // ignore
    }

    try {
      const raw = e.dataTransfer.getData('application/json') || e.dataTransfer.getData('text/plain');
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed?.ids)) return parsed.ids.map(String).filter(Boolean);
      if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean);
    } catch {
      const raw = e.dataTransfer.getData('text/plain');
      if (raw) return raw.split(',').map((value) => value.trim()).filter(Boolean);
    }

    return [];
  }, []);
  const onWorkspaceDrop = useCallback(async (e: React.DragEvent) => {
    const artifactDrag = isArtifactsDrag(e.dataTransfer);
    const fileDrag = isFileDrag(e.dataTransfer);

    if (!workspaceId) {
      if (!shouldShowNoSourceState || !artifactDrag) return;
    } else if (!(fileDrag || artifactDrag)) {
      return;
    }
    
    e.preventDefault();
    e.stopPropagation();
    resetWorkspaceDragState();

    if (artifactDrag) {
      const draggedIds = parseDraggedArtifactIds(e);
      if (draggedIds.length > 0) {
        if (!workspaceId) {
          await createWorkspaceFromDroppedArtifacts(draggedIds);
        } else {
          handleResolvedDrop(draggedIds, latestArtifactsRef.current.length);
        }
      }
      return;
    }
    
    const files = Array.from(e.dataTransfer.files || []);
    if (files.length) {
      uploadFilesAt(files, latestArtifactsRef.current.length);
    }
  }, [workspaceId, shouldShowNoSourceState, createWorkspaceFromDroppedArtifacts, handleResolvedDrop, parseDraggedArtifactIds, resetWorkspaceDragState, uploadFilesAt]);

  // Tracks drag kind for potential future indicators (e.g. BrowserHeader drop targets)
  // isWorkspaceDropActive kept for reference: activeSource?.type === 'workspace' && dragDepth > 0 && workspaceDragKind !== null

  // Themed pale background for browser area
  const browserBg = isShowingSearchResults
    ? 'bg-blue-50'
    : activeSource?.type === 'workspace'
    ? 'bg-purple-50'
    : activeSource?.type === 'collection'
    ? 'bg-blue-50'
    : 'bg-white';

  const handleBrowserRemove = useCallback(async (artifact: Artifact) => {
    if (!artifact.id) return;

    const actx = safeParseArtifactContext(artifact.context);
    const actxContentType = actx.content_type;
    const resolvedWsId =
      (typeof actx.workspace_id === 'string' ? actx.workspace_id : null) ??
      workspaces.find(w =>
        String(w.id) === String(artifact.id ?? '') ||
        String(w.id) === String(artifact.root_id ?? '') ||
        String(w.id) === String(artifact.collection_id ?? '')
      )?.id ?? null;
    const isInboxRef = actxContentType === WORKSPACE_CONTENT_TYPE && resolvedWsId === inboxWorkspaceId;
    const isPermanent = !isInboxRef && (artifact.state === 'draft');

    if (isPermanent) {
      const confirmed = await dangerConfirm.confirm({
        title: CARD_CONFIRM.DELETE_PERMANENT_TITLE,
        description: CARD_CONFIRM.DELETE_PERMANENT_DESCRIPTION,
        confirmLabel: CARD_CONFIRM.DELETE_PERMANENT_CONFIRM,
        cancelLabel: BUTTON_LABELS.CANCEL
      });

      if (!confirmed) return;

      undockWorkspaceIfNeeded(artifact);
      await removeArtifact(String(artifact.id));
      return;
    }

    const removableId = String(artifact.root_id ?? artifact.id);
    if (!removableId) return;

    undockWorkspaceIfNeeded(artifact);

    if (activeSource?.type === 'collection') {
      await removeArtifactFromCollection(activeSource.id, removableId);
      setSourceArtifacts(prev => prev.filter(candidate => String(candidate.root_id ?? candidate.id) !== removableId));
      return;
    }

    if (activeSource?.type === 'workspace' && workspaceId) {
      await removeWorkspaceArtifact(workspaceId, removableId);
      setSourceArtifacts(prev => prev.filter(candidate => String(candidate.root_id ?? candidate.id) !== removableId));
      return;
    }

    await removeArtifact(String(artifact.id));
  }, [activeSource?.id, activeSource?.type, dangerConfirm, inboxWorkspaceId, removeArtifact, undockWorkspaceIfNeeded, workspaceId, workspaces]);

  const renderArtifactSection = useCallback((sectionArtifacts: Artifact[], sectionKey: string) => {
    if (sectionArtifacts.length === 0) return null;

    if (viewMode === 'grid') {
      return (
        <div className={sectionKey === 'primary' ? 'px-4 pt-4 pb-4 min-h-full' : 'px-4 pt-4 pb-4'}>
          <CardGrid
            artifacts={sectionArtifacts}
            artifactCountsById={artifactCountsByOwnerId}
            selectable
            draggable={sectionKey === 'primary'}
            fillHeight={true}
            selectedIds={selectedArtifactIds}
            isSelected={(id) => selectedArtifactIds.includes(id)}
            activeSource={activeSource}
            isShowingSearchResults={isShowingSearchResults}
            onArtifactMouseDown={(id, e) => {
              selectArtifact(id, e);
              onArtifactSelect?.(id);
            }}
            onOpenArtifact={onOpenArtifact}
            onAssignCollections={(artifact) => {
              if (artifact.id) onAssignCollections?.(String(artifact.id));
            }}

            onRemove={handleBrowserRemove}
            onAddToWorkspace={(artifact: Artifact) => {
              addExistingArtifact(artifact);
            }}
            onRevert={async (artifact: Artifact) => {
              if (!artifact.id) return;
              await revertArtifact(String(artifact.id));
            }}
            onOrder={sectionKey === 'primary' ? ((ids) => orderArtifacts(ids)) : undefined}
            onFileDrop={sectionKey === 'primary' && !!workspaceId ? ((index, files) => uploadFilesAt(files, index)) : undefined}
            onArtifactDrop={sectionKey === 'primary' && (!!workspaceId || activeSource?.type === 'collection')
              ? ((index, draggedIds, dragPayload) => handleResolvedDrop(draggedIds, index, dragPayload))
              : undefined}
          />
        </div>
      );
    }

    return (
      <div className="px-4 pt-4 pb-4">
        <CardList
          artifacts={sectionArtifacts}
          artifactCountsById={artifactCountsByOwnerId}
          selectable
          draggable={sectionKey === 'primary'}
          isSelected={(id) => selectedArtifactIds.includes(id)}
          onArtifactMouseDown={(id, e) => {
            selectArtifact(id, e);
            onArtifactSelect?.(id);
          }}
          onEditArtifactOpen={() => {
            // Preview pane uses current selection.
          }}
          onOpenArtifact={onOpenArtifact}
          onAssignCollections={(artifact: Artifact) => {
            if (artifact.id) onAssignCollections?.(String(artifact.id));
          }}
          onRemove={handleBrowserRemove}
          onRevert={async (artifact: Artifact) => {
            if (!artifact.id) return;
            await revertArtifact(String(artifact.id));
          }}
          onArchive={async (artifact: Artifact) => {
            if (!artifact.id || !updateArtifact) return;
            undockWorkspaceIfNeeded(artifact);
            await updateArtifact({ id: String(artifact.id), state: 'archived' });
          }}
          onRestore={(artifact: Artifact) => {
            if (artifact.id && updateArtifact) {
              updateArtifact({ id: String(artifact.id), state: 'committed' });
            }
          }}
          onReorder={sectionKey === 'primary' ? ((artifactId, targetIndex) => {
            const newOrder = [...sectionArtifacts];
            const fromIndex = newOrder.findIndex(c => String(c.id) === artifactId);
            if (fromIndex !== -1) {
              const [artifact] = newOrder.splice(fromIndex, 1);
              newOrder.splice(targetIndex, 0, artifact);
              orderArtifacts(newOrder.map(c => String(c.id)));
            }
          }) : undefined}
          onFileDrop={sectionKey === 'primary' && !!workspaceId ? ((files, index) => uploadFilesAt(files, index || 0)) : undefined}
        />
      </div>
    );
  }, [activeSource, addExistingArtifact, artifactCountsByOwnerId, handleBrowserRemove, handleResolvedDrop, isShowingSearchResults, onAssignCollections, onArtifactSelect, onOpenArtifact, orderArtifacts, revertArtifact, selectArtifact, selectedArtifactIds, undockWorkspaceIfNeeded, updateArtifact, uploadFilesAt, viewMode, workspaceId]);

  return (
    <div className="h-full flex flex-col">
      
      <BrowserHeader
        activeSource={activeSource}
        onToggleFilters={() => setFiltersOpen(!filtersOpen)}
        filtersOpen={filtersOpen}
        hasActiveFilter={activeFilter !== 'all'}
        activeFilter={activeFilter}
        viewMode={viewMode}
        onViewModeChange={(mode) => setViewMode(mode)}
        isShowingSearchResults={isShowingSearchResults}
        sortMode={searchSortMode}
        onSortChange={onSearchSortChange}
        aperture={searchAperture}
        onApertureChange={onSearchApertureChange}
        pendingChangeCount={headerPublishCount}
        onReviewChanges={handleOpenReview}
        isReviewLoading={isPreviewLoading}
        isCommitting={isCommitting}
        canReview={canReview}
        onNewArtifact={handleOpenTypePicker}
        onCreateWorkspace={handleCreateWorkspace}
        onDropWorkspace={handleDropWorkspace}
        onRenameWorkspace={handleRenameWorkspace}
      />

      {/* Filter Chips - Always visible so the grey bar stays as a consistent landmark */}
      <FilterChips
        artifacts={sourceArtifacts}
        activeStates={activeStateFilters}
        activeContentTypes={activeContentTypeFilters}
        hiddenContentTypes={hiddenContentTypeFilters}
        viewMode={viewMode}
        onViewModeChange={(mode) => setViewMode(mode)}
        onStateToggle={handleStateFilterToggle}
        onContentTypeToggle={handleContentTypeFilterToggle}
        onHiddenContentTypeToggle={handleHiddenContentTypeToggle}
        onClearAll={handleClearAllFilters}
      />

      {activeSource?.type === 'workspace' && stackedArtifacts.sections.length > 0 && (
        <div className="border-b border-gray-200 bg-white px-4 py-2">
          <button
            type="button"
            onClick={() => setStackArtifactFamilies((prev) => !prev)}
            className={`inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${stackArtifactFamilies ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'}`}
          >
            {stackArtifactFamilies ? 'Artifact stacks on' : 'Artifact stacks off'}
          </button>
          <span className="ml-3 text-xs text-gray-500">
            Group artifacts by owning source artifact when possible, with family fallbacks for anything unresolved.
          </span>
        </div>
      )}

      {/* Filter Bar (with Sort and View) - Sliding Drawer */}
      <FilterBar
        counts={counts}
        activeFilter={activeFilter}
        onFilterChange={setActiveFilter}
        sortBy={sortBy}
        onSortChange={setSortBy}
        viewMode={viewMode}
        onViewChange={(view) => setViewMode(view === 'compact' ? 'list' : view)}
        isOpen={filtersOpen}
      />

      {/* Main Content Area */}
      <div
        id="workspace-root"
        data-accept-drop="workspace"
        className={[
          "relative flex-1 overflow-y-auto transition-shadow",
          browserBg,
          "ring-0"
        ].join(" ")}
        onClick={(e) => {
          // Deselect when clicking anywhere in the workspace that isn't on a card
          if (!(e.target as Element).closest('[data-testid^="artifact-"]')) {
            unselectAllArtifacts();
          }
        }}
        onDoubleClick={(e) => {
          if (activeSource?.type !== 'workspace') return;
          const target = e.target as Element;
          if (target.closest('[data-testid^="artifact-"]')) return;
          handleOpenTypePicker();
        }}
        onDragEnter={onDragEnter}
        onDragLeave={onDragLeave}
        onDragOver={onWorkspaceDragOver}
        onDrop={onWorkspaceDrop}
      >
        {/* Empty States */}
        {shouldShowNoSourceState ? (
          <EmptyState type="no-source" />
        ) : filteredArtifacts.length === 0 && isSearchActive ? (
          <EmptyState 
            type="no-search-results" 
            searchQuery={searchQuery}
            onClearSearch={clearSearch}
          />
        ) : filteredArtifacts.length === 0 ? (
          <EmptyState type="no-artifacts" />
        ) : (
          shouldUseArtifactStacks ? (
            <div className="pb-4">
              {stackedArtifacts.primaryArtifacts.length > 0 && renderArtifactSection(stackedArtifacts.primaryArtifacts, 'primary')}
              {stackedArtifacts.sections.map((section) => {
                const collapsed = collapsedArtifactSections[section.id] ?? section.defaultCollapsed ?? true;
                return (
                  <div key={section.id} className="px-4 pb-4">
                    <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
                      <button
                        type="button"
                        onClick={() => toggleArtifactSection(section.id)}
                        className="flex w-full items-center justify-between px-4 py-3 text-left"
                      >
                        <div>
                          <div className="text-sm font-semibold text-slate-900">{section.title}</div>
                          <div className="text-xs text-slate-500">{section.artifacts.length} cards</div>
                        </div>
                        <div className="text-xs font-medium text-slate-600">{collapsed ? 'Expand' : 'Collapse'}</div>
                      </button>
                      {!collapsed && renderArtifactSection(section.artifacts, section.id)}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            renderArtifactSection(filteredArtifacts, 'primary')
          )
        )}
      </div>

      {/* Multi-Artifact Toolbar */}
      {showFloatingToolbar && (
        <MultiCardToolbar
          selectedCount={selectedArtifactIds.length}
          onArchive={handleBulkArchive}
          onAddTags={handleBulkAddTags}
          onMove={handleBulkMove}
          onDelete={handleBulkDelete}
          onDrop={handleBulkDrop}
          onClear={unselectAllArtifacts}
          moveLabel="Assign to Collections"
          hasNew={bulkHasNew}
          hasCommitted={bulkHasCommitted}
        />
      )}

      <CollectionPicker
        open={showBulkCollectionPicker}
        onClose={() => setShowBulkCollectionPicker(false)}
        onSelect={handleBulkSelectCollections}
        multiple={true}
        title={`Assign ${selectedArtifactIds.length} Artifact${selectedArtifactIds.length === 1 ? '' : 's'} to Collections`}
      />

      <BulkTagDialog
        open={showBulkTagDialog}
        onClose={() => setShowBulkTagDialog(false)}
        onApply={handleBulkApplyTags}
        selectedCount={selectedArtifactIds.length}
      />

      {/* Edit Modal */}
      {editArtifact && (
        <ArtifactEditModal
          artifact={editArtifact as unknown as Artifact}
          onSave={handleSave}
          onClose={handleClose}
        />
      )}

      <CardTypePickerDialog
        open={isArtifactTypePickerOpen}
        onOpenChange={setIsArtifactTypePickerOpen}
        onSelect={handleSelectArtifactType}
      />

      <CommitReviewDialog
        open={isReviewOpen}
        onOpenChange={handleDialogOpenChange}
        preview={commitPreview}
        isLoading={isPreviewLoading}
        isCommitting={isCommitting}
        onRefresh={handleRefreshPreview}
        onPublish={handlePublish}
        artifacts={artifacts}
        selectedArtifactIds={selectedPlanArtifactIds}
        onToggleArtifact={handleTogglePlanArtifact}
        onSelectAll={handleSelectAllPlanArtifacts}
        onClearSelection={handleClearPlanSelection}
      />
    </div>
  );
}

type ArtifactSection = {
  id: string;
  title: string;
  artifacts: Artifact[];
  defaultCollapsed?: boolean;
};

function isArtifactArtifact(artifact: Artifact): boolean {
  const context = safeParseArtifactContext(artifact.context);
  const type = typeof context.type === 'string' ? context.type : '';

  return type === 'transcript' || type === 'palette-output' || Boolean(context.source_artifact_id);
}

function getArtifactFallbackSectionId(artifact: Artifact): string | null {
  const context = safeParseArtifactContext(artifact.context);
  const type = typeof context.type === 'string' ? context.type : '';

  if (type === 'transcript') return 'transcripts';
  if (type === 'palette-output') return 'palette-outputs';
  if (context.source_artifact_id && type !== 'transcript') return 'stream-sessions';
  return null;
}

function resolveArtifactOwnerArtifact(artifact: Artifact, artifactsById: Map<string, Artifact>): Artifact | null {
  const visited = new Set<string>();
  let current: Artifact | null = artifact;

  for (let depth = 0; depth < 8; depth += 1) {
    if (!current) return null;

    const context = safeParseArtifactContext(current.context);
    const nextId = typeof context.source_artifact_id === 'string' ? context.source_artifact_id : '';
    if (!nextId) {
      return current.id && String(current.id) !== String(artifact.id) ? current : null;
    }

    const normalizedId = String(nextId);
    if (visited.has(normalizedId)) return null;
    visited.add(normalizedId);

    current = artifactsById.get(normalizedId) ?? null;
  }

  return null;
}

function getArtifactTitle(artifact: Artifact): string {
  const context = safeParseArtifactContext(artifact.context);
  return String(context.title || context.filename || artifact.content || 'Untitled').trim() || 'Untitled';
}

function buildArtifactSections(artifacts: Artifact[]): { primaryArtifacts: Artifact[]; sections: ArtifactSection[] } {
  const primaryArtifacts: Artifact[] = [];
  const sections = new Map<string, ArtifactSection>();
  const artifactsById = new Map(artifacts.map((artifact) => [String(artifact.id), artifact]));
  const ownerOrder: string[] = [];

  for (const artifact of artifacts) {
    if (!isArtifactArtifact(artifact)) {
      primaryArtifacts.push(artifact);
      continue;
    }

    const ownerArtifact = resolveArtifactOwnerArtifact(artifact, artifactsById);
    if (ownerArtifact) {
      const ownerId = String(ownerArtifact.id);
      const sectionId = `owner:${ownerId}`;

      if (!sections.has(sectionId)) {
        ownerOrder.push(sectionId);
        sections.set(sectionId, {
          id: sectionId,
          title: `${getArtifactTitle(ownerArtifact)} Artifacts`,
          artifacts: [],
          defaultCollapsed: true,
        });
      }

      sections.get(sectionId)!.artifacts.push(artifact);
      continue;
    }

    const sectionId = getArtifactFallbackSectionId(artifact);
    if (!sectionId) {
      primaryArtifacts.push(artifact);
      continue;
    }

    if (!sections.has(sectionId)) {
      sections.set(
        sectionId,
        sectionId === 'transcripts'
          ? { id: sectionId, title: 'Transcripts', artifacts: [], defaultCollapsed: true }
          : sectionId === 'stream-sessions'
            ? { id: sectionId, title: 'Stream Sessions', artifacts: [], defaultCollapsed: true }
            : { id: sectionId, title: 'Transform Outputs', artifacts: [], defaultCollapsed: true }
      );
    }

    sections.get(sectionId)!.artifacts.push(artifact);
  }

  return {
    primaryArtifacts,
    sections: [
      ...ownerOrder.map((id) => sections.get(id)).filter((section): section is ArtifactSection => Boolean(section)),
      ...Array.from(sections.entries())
        .filter(([id]) => !id.startsWith('owner:'))
        .map(([, section]) => section),
    ],
  };
}

function buildArtifactCountsByOwnerId(artifacts: Artifact[]): Record<string, number> {
  const artifactsById = new Map(artifacts.map((artifact) => [String(artifact.id), artifact]));
  const counts: Record<string, number> = {};

  for (const artifact of artifacts) {
    if (!isArtifactArtifact(artifact)) continue;
    const ownerArtifact = resolveArtifactOwnerArtifact(artifact, artifactsById);
    if (!ownerArtifact?.id) continue;

    const ownerId = String(ownerArtifact.id);
    counts[ownerId] = (counts[ownerId] ?? 0) + 1;
  }

  return counts;
}
