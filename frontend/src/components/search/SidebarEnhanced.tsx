// components/sidebar/SidebarEnhanced.tsx
import { useCallback, useState, useEffect, useRef } from 'react';
import { toast } from 'sonner';
import { importCollectionArtifactToWorkspace, moveArtifactToWorkspace } from '../../api/workspaces';
import {
  MoreVertical,
  PlusCircle,  
  Proportions,
  FolderOpen,
  ChevronRight,
  ChevronDown,
  Eye,
  EyeOff,
  Pencil,
  Trash2,
  Share2,
  Workflow,
  Clock,
  User,
  FileType,
} from 'lucide-react';
import ServerIcon from '../icons/ServerIcon';
import { useWorkspaces } from '../../hooks/useWorkspaces';
import { useWorkspace } from '../../context/workspace/WorkspaceContext';
import { useAuth } from '../../hooks/useAuth';
import { Workspace } from '../../context/workspace/workspace.types';
import type { ActiveSource } from '../../types/workspace';
import CollectionDetailModal from '../modals/CollectionDetailModal';
import WorkspaceDetailModal from '../modals/WorkspaceDetailModal';
import { Button } from '@/components/ui/button';
import { IconButton } from '@/components/ui/icon-button';
import CardGrid from '../common/CardGrid';
import { getCollectionArtifacts } from '../../api/collections';
import { Artifact } from '../../context/workspace/workspace.types';
import {
  listWorkspaceMCPServers,
  type MCPServerInfo,
} from '../../api/mcp';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { cn } from '@/lib/utils';
import { usePreferences } from '../../hooks/usePreferences';
import AdvancedSearch from '../browser/AdvancedSearch';
import type { SearchResponse } from '../../api/types/search';

type SettingsSection = 'profile' | 'general' | 'llm-keys' | 'demo-data';

interface SidebarEnhancedProps {
  activeSource: ActiveSource;
  onActiveSourceChange: (source: ActiveSource) => void;
  collapsed: boolean;
  sidebarWidth?: number; // Width in pixels for proper drawer positioning
  centerPanelWidth?: number; // Width in pixels of the center panel (browser)
  onOpenSettings?: (section: SettingsSection, collectionId?: string) => void;
  onToolsDrawerChange?: (drawer: ToolsDrawerState | null) => void;
  // Sidebar-scoped search (feeds Browser via MainLayout)
  onSearchResults?: (results: SearchResponse) => void;
  onSearchClear?: () => void;
  searchSortMode?: 'relevance' | 'recency';
  onSearchSortChange?: (mode: 'relevance' | 'recency') => void;
  searchAperture?: number;
  onSearchApertureChange?: (value: number) => void;
  clearSearchTrigger?: number;
  searchResults?: SearchResponse | null;
}

type SectionType = 'workspaces' | 'resources' | 'tools';

type ResourcesViewMode = 'by-collection' | 'by-type' | 'by-time' | 'by-author';

export interface ToolsDrawerState {
  serverId: string;
  serverName: string;
  serverIcon?: string;
}

export interface ResourcesDrawerState {
  collectionId: string;
  collectionName: string;
  collectionIcon?: string;
}

export default function SidebarEnhanced({ 
  activeSource, 
  onActiveSourceChange,
  collapsed,
  sidebarWidth,
  centerPanelWidth,
  onToolsDrawerChange,
  onSearchResults,
  onSearchClear,
  searchSortMode,
  onSearchSortChange,
  searchAperture,
  onSearchApertureChange,
  clearSearchTrigger,
  searchResults,
}: SidebarEnhancedProps) {
  const [showCollectionsModal, setShowCollectionsModal] = useState(false);
  const [showWorkspaceModal, setShowWorkspaceModal] = useState(false);
  const [isCreatingWorkspace, setIsCreatingWorkspace] = useState(false);
  const [editingWorkspace, setEditingWorkspace] = useState<Workspace | null>(null);
  const [drawerSection, setDrawerSection] = useState<SectionType | null>(null);
  const [openDropdownId, setOpenDropdownId] = useState<string | null>(null);
  const [deleteConfirmItem, setDeleteConfirmItem] = useState<{ type: 'workspace' | 'collection'; id: string; name: string } | null>(null);
  
  const { preferences, updatePreferences } = usePreferences();
  const [expandedSections, setExpandedSections] = useState<Set<SectionType>>(() => {
    const savedExpanded = preferences.sidebarSections?.expanded || ['workspaces', 'resources', 'tools'];
    return new Set(savedExpanded.filter((s): s is SectionType => 
      s === 'workspaces' || s === 'resources' || s === 'tools'
    ));
  });
  
  const [mcpServerInfos, setMcpServerInfos] = useState<Record<string, MCPServerInfo>>({});
    
  // Section-specific view modes
  const [resourcesViewMode, setResourcesViewMode] = useState<ResourcesViewMode>(() => {
    return preferences.sidebarSections?.resourcesViewMode || 'by-collection';
  });
  
  // Drawer states for tools and resources - only one can be open at a time
  const [toolsDrawer, setToolsDrawer] = useState<ToolsDrawerState | null>(null);
  const [resourcesDrawer, setResourcesDrawer] = useState<ResourcesDrawerState | null>(null); // Drawer states for tools and resources - only one can be open at a time
  const [drawerAnimating, setDrawerAnimating] = useState(false);
  
  // Collection artifacts for Resources drawer
  const [collectionArtifacts, setCollectionArtifacts] = useState<Artifact[]>([]);

  // Currently unused search callbacks are kept for future sidebar search UX wiring
  void onSearchSortChange;
  void onSearchApertureChange;
  
  // Convert MCP tools to Artifact format for drawer display
  const toolsToArtifacts = useCallback((serverId: string): Artifact[] => {
    const serverInfo = mcpServerInfos[serverId];
    if (!serverInfo?.tools) return [];

    const artifacts: Artifact[] = [];
    let index = 0;

    for (const tool of serverInfo.tools) {
      const toolName = tool.name?.trim();
      if (!toolName) continue;

      const description = tool.description;
      const inputSchema = tool.input_schema;
      const icon = tool.icon;

      artifacts.push({
        id: `${serverId}-tool-${toolName}`,
        content: toolName,
        context: JSON.stringify({
          type: 'mcp-tool',
          serverId,
          toolName,
          description,
          inputSchema,
          icon,
        }),
        state: 'committed' as const,
        order_key: String(index).padStart(10, '0'),
      });
      index += 1;
    }

    return artifacts;
  }, [mcpServerInfos]);
  
  // Track actual sidebar width for drawer positioning
  const sidebarRef = useRef<HTMLDivElement>(null);
  const [actualSidebarWidth, setActualSidebarWidth] = useState<number>(sidebarWidth || 256);
  
  // Calculate drawer width: from sidebar right edge to 100px before center panel right edge
  const drawerWidth = centerPanelWidth ? centerPanelWidth - 100 : 'auto';

  // Update actual sidebar width when it changes (ResizeObserver)
  useEffect(() => {
    if (!sidebarRef.current) return;
    
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setActualSidebarWidth(entry.contentRect.width);
      }
    });
    
    observer.observe(sidebarRef.current);
    return () => observer.disconnect();
  }, []);

  // Handle drawer opening with fade animation
  useEffect(() => {
    if (toolsDrawer || resourcesDrawer) {
      // Immediate fade-in for locked appearance
      setDrawerAnimating(true);
    } else {
      setDrawerAnimating(false);
    }
  }, [toolsDrawer, resourcesDrawer]);
  
  // Fetch collection artifacts when Resources drawer opens
  useEffect(() => {
    if (resourcesDrawer) {
      getCollectionArtifacts(resourcesDrawer.collectionId)
        .then(setCollectionArtifacts)
        .catch(error => {
          console.error('Failed to fetch collection artifacts:', error);
          setCollectionArtifacts([]);
        });
    } else {
      setCollectionArtifacts([]);
    }
  }, [resourcesDrawer]);

  // Notify parent when drawer state changes
  useEffect(() => {
    onToolsDrawerChange?.(toolsDrawer);
  }, [toolsDrawer, onToolsDrawerChange]);

  const toggleResourcesViewMode = useCallback(() => {
    const modes: ResourcesViewMode[] = ['by-collection', 'by-type', 'by-time', 'by-author'];
    const currentIndex = modes.indexOf(resourcesViewMode);
    const nextMode = modes[(currentIndex + 1) % modes.length];
    setResourcesViewMode(nextMode);
    updatePreferences({
      sidebarSections: {
        ...preferences.sidebarSections,
        resourcesViewMode: nextMode
      }
    });
  }, [resourcesViewMode, preferences, updatePreferences]);
  
  const { user } = useAuth() ?? {};
  const {
    workspaces,
    activeWorkspace,
    setActiveWorkspaceId,
    createWorkspace,
    updateWorkspace,
    deleteWorkspace,
  } = useWorkspaces();

  const { removeArtifact, artifacts, importArtifactsByRootIds } = useWorkspace();

  // Fetch MCP server details (tools/resources) when active workspace changes
  useEffect(() => {
    if (!activeWorkspace) {
      setMcpServerInfos({});
      return;
    }

    listWorkspaceMCPServers(activeWorkspace.id)
      .then((infos) => {
        const infoMap: Record<string, MCPServerInfo> = {};
        infos.forEach(info => {
          infoMap[info.server] = info;
        });
        setMcpServerInfos(infoMap);
      })
      .catch(console.error);
  }, [activeWorkspace]);

  // Initialize activeSource with activeWorkspace
  useEffect(() => {
    if (activeWorkspace && !activeSource) {
      onActiveSourceChange({ type: 'workspace', id: activeWorkspace.id });
    }
  }, [activeWorkspace, activeSource, onActiveSourceChange]);

  // Save expanded sections to preferences
  useEffect(() => {
    const nextExpanded = Array.from(expandedSections);
    const currentExpanded = preferences.sidebarSections?.expanded || [];
    const normCurrent = [...currentExpanded].slice().sort().join('|');
    const normNext = [...nextExpanded].slice().sort().join('|');
    if (normCurrent === normNext) return;

    updatePreferences({
      sidebarSections: {
        ...preferences.sidebarSections,
        expanded: nextExpanded
      }
    });
  }, [expandedSections, updatePreferences, preferences.sidebarSections]);

  const toggleSection = useCallback((section: SectionType) => {
    setExpandedSections(prev => {
      const next = new Set(prev);
      if (next.has(section)) {
        next.delete(section);
      } else {
        next.add(section);
      }
      return next;
    });
  }, []);

  // Sort workspaces: Inbox always first, then by order_key
  const sortedWorkspaces = useCallback(() => {
    if (!user) return workspaces;
    return [...workspaces].sort((a, b) => {
      // Inbox workspace (id === created_by) always first
      const aIsInbox = a.id === a.created_by;
      const bIsInbox = b.id === b.created_by;
      if (aIsInbox && !bIsInbox) return -1;
      if (!aIsInbox && bIsInbox) return 1;
      // Both inbox or neither: sort by order_key (lexicographic)
      return (a.order_key || 'U').localeCompare(b.order_key || 'U');
    });
  }, [workspaces, user]);

  const openCreateWorkspace = useCallback(() => {
    setIsCreatingWorkspace(true);
    setEditingWorkspace(null);
    setShowWorkspaceModal(true);
  }, []);

  const openEditWorkspace = useCallback((ws: Workspace) => {
    setIsCreatingWorkspace(false);
    setEditingWorkspace(ws);
    setShowWorkspaceModal(true);
  }, []);

  const handleWorkspaceSave = useCallback(async (name: string, description: string) => {
    if (isCreatingWorkspace) {
      await createWorkspace(name, description);
    } else if (editingWorkspace) {
      await updateWorkspace({ id: editingWorkspace.id, name, description });
    }
    setShowWorkspaceModal(false);
  }, [createWorkspace, updateWorkspace, isCreatingWorkspace, editingWorkspace]);

  // Hide/Show handlers
  const handleHideWorkspace = useCallback((wsId: string) => {
    const hidden = preferences.sidebarSections?.hiddenWorkspaces || [];
    const updated = [...hidden, wsId];
    updatePreferences({
      sidebarSections: {
        ...preferences.sidebarSections,
        hiddenWorkspaces: updated
      }
    });
  }, [preferences, updatePreferences]);

  const handleShowWorkspace = useCallback((wsId: string) => {
    const hidden = preferences.sidebarSections?.hiddenWorkspaces || [];
    const updated = hidden.filter(id => id !== wsId);
    updatePreferences({
      sidebarSections: {
        ...preferences.sidebarSections,
        hiddenWorkspaces: updated
      }
    });
  }, [preferences, updatePreferences]);

  // Show All toggle handlers
  const handleToggleShowAll = useCallback((type: 'workspaces' | 'collections' | 'mcp-servers') => {
    if (type === 'workspaces') {
      const current = preferences.sidebarSections?.showAllWorkspaces ?? false;
      updatePreferences({
        sidebarSections: {
          ...preferences.sidebarSections,
          showAllWorkspaces: !current
        }
      });
    } else if (type === 'collections') {
      const current = preferences.sidebarSections?.showAllCollections ?? false;
      updatePreferences({
        sidebarSections: {
          ...preferences.sidebarSections,
          showAllCollections: !current
        }
      });
    } else if (type === 'mcp-servers') {
      const current = preferences.sidebarSections?.showAllMcpServers ?? false;
      updatePreferences({
        sidebarSections: {
          ...preferences.sidebarSections,
          showAllMcpServers: !current
        }
      });
    }
  }, [preferences, updatePreferences]);

  // Artifact drop handlers
  const handleWorkspaceDrop = useCallback(async (workspaceId: string, artifactId: string, sourceWorkspaceId?: string) => {
    // If dropping on same workspace, do nothing
    if (sourceWorkspaceId === workspaceId) {
      console.log('[Sidebar] Artifact already in target workspace, no action needed');
      return;
    }

    if (!sourceWorkspaceId) {
      // Collection/search -> workspace import path. Use root IDs to import the
      // current head version and preserve latest context values.
      try {
        if (activeWorkspace?.id === workspaceId) {
          await importArtifactsByRootIds([artifactId], artifacts.length);
        } else {
          await importCollectionArtifactToWorkspace(workspaceId, artifactId);
        }
        toast.success('Artifact imported into workspace');
      } catch (error) {
        console.error('[Sidebar] Failed to import artifact into workspace:', error);
        toast.error('Failed to import artifact into workspace');
      }
      return;
    }

    try {
      console.log('[Sidebar] Moving artifact:', { artifactId, from: sourceWorkspaceId, to: workspaceId });
      
      // Call backend API to move artifact
      await moveArtifactToWorkspace(sourceWorkspaceId, artifactId, workspaceId);
      
      // Update local state: remove from source workspace if it's active
      if (activeWorkspace?.id === sourceWorkspaceId) {
        // If we're viewing the source workspace, remove the artifact from view
        removeArtifact(artifactId);
      }
      
      // Show success toast
      const targetWorkspace = workspaces.find(w => w.id === workspaceId);
      const toast = await import('react-hot-toast');
      toast.default.success(`Artifact moved to ${targetWorkspace?.name || 'workspace'}`);
      
      // Refresh target workspace if needed
      if (activeWorkspace?.id === workspaceId) {
        // Trigger refresh by re-setting the workspace
        setActiveWorkspaceId(workspaceId);
      }
    } catch (error) {
      console.error('[Sidebar] Failed to move artifact:', error);
      const toast = await import('react-hot-toast');
      toast.default.error('Failed to move artifact');
    }
  }, [activeWorkspace, workspaces, setActiveWorkspaceId, removeArtifact, importArtifactsByRootIds, artifacts.length]);

  const searchScope = activeSource?.type === 'workspace'
    ? 'workspace'
    : activeSource?.type === 'collection'
      ? 'collection'
      : 'global';

  const searchScopeId = activeSource?.id ?? '';

  // Unified, friendlier search copy inspired by Google Keep
  const searchPlaceholder = 'Find something...';

  // Collapsed sidebar - icon only
  if (collapsed) {
    return (
      <>
        <div className="flex flex-col h-full w-16 bg-white border-r border-border">
          {/* Icon buttons - compact spacing to match expanded */}
          <div className="flex flex-col items-center py-2 gap-1">
            <IconButton
              size="md"
              variant="ghost"
              active={drawerSection === 'workspaces'}
              onClick={() => setDrawerSection('workspaces')}
              title="Workspaces"
            >
              <Proportions />
            </IconButton>

            <IconButton
              size="md"
              variant="ghost"
              active={drawerSection === 'resources'}
              onClick={() => setDrawerSection('resources')}
              title="Resources"
            >
              <FolderOpen />
            </IconButton>

            <IconButton
              size="md"
              variant="ghost"
              active={drawerSection === 'tools'}
              onClick={() => setDrawerSection('tools')}
              title="Tools"
            >
              <Workflow />
            </IconButton>
          </div>
        </div>

        {/* Drawer for expanded sections */}
        <Sheet open={drawerSection !== null} onOpenChange={(open: boolean) => !open && setDrawerSection(null)}>
          <SheetContent side="left" style={{ width: '256px' }} className="p-0 [&>button]:focus-visible:ring-0 [&>button]:focus:outline-none [&>button]:focus:ring-0 [&>button]:outline-none [&>button]:cursor-pointer">
            <SheetHeader className="px-4 py-3 border-b">
              <SheetTitle>
                {drawerSection === 'workspaces' && 'Workspaces'}
                {drawerSection === 'resources' && 'Resources'}
                {drawerSection === 'tools' && 'Tools'}
              </SheetTitle>
            </SheetHeader>
            <div className="overflow-y-auto h-[calc(100%-60px)]">
              {drawerSection === 'workspaces' && renderWorkspacesContent()}
              {drawerSection === 'resources' && renderResourcesContent()}
              {drawerSection === 'tools' && renderToolsContent()}
            </div>
          </SheetContent>
        </Sheet>
      </>
    );
  }

  // Helper render functions
  function renderWorkspacesContent() {
    return (
      <div>
        {sortedWorkspaces().map((ws) => (
          <div
            key={ws.id}
            className={cn(
              "group relative h-7 cursor-pointer flex items-center rounded transition-colors",
              activeSource?.type === 'workspace' && activeSource.id === ws.id
                ? 'bg-primary/10' 
                : 'hover:bg-accent/50'
            )}
            onClick={() => {
              setActiveWorkspaceId(ws.id);
              onActiveSourceChange({ type: 'workspace', id: ws.id });
              setDrawerSection(null);
            }}
          >
            <Proportions size={16} className="mr-2 flex-shrink-0" />
            <div className="truncate text-sm flex-1">
              {ws.name}
            </div>
            <DropdownMenu open={openDropdownId === `ws-${ws.id}`} onOpenChange={(open) => setOpenDropdownId(open ? `ws-${ws.id}` : null)}>
              <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
                <Button
                    variant="ghost"
                    size="icon"
                    className={cn(
                      "h-6 w-6 flex-shrink-0 p-0",
                      openDropdownId === `ws-${ws.id}` ? "opacity-100" : "opacity-0 group-hover:opacity-100",
                      activeSource?.type === 'workspace' && activeSource.id === ws.id
                        ? "hover:bg-accent-foreground/10"
                        : "hover:bg-gray-200"
                    )}
                  >
                    <MoreVertical size={14} />
                  </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                <DropdownMenuItem onClick={() => openEditWorkspace(ws)}>
                  <Pencil className="mr-2 h-4 w-4" />
                  Modify
                </DropdownMenuItem>
                <DropdownMenuItem>
                  <EyeOff className="mr-2 h-4 w-4" />
                  Hide
                </DropdownMenuItem>
                {ws.id !== ws.created_by && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem 
                      className="text-destructive"
                      onClick={() => setDeleteConfirmItem({ type: 'workspace', id: ws.id, name: ws.name })}
                    >
                      <Trash2 className="mr-2 h-4 w-4" />
                      Delete
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ))}
      </div>
    );
  }

  function renderResourcesContent() {
    // Mirrors the expanded "Resources" section rendering, but scoped for the collapsed drawer.
    return (
      <div className="pb-1">
        <div className="ml-5 border-l border-border">
          {(() => {
            const allResources: Array<{
              id: string;
              name: string;
              description?: string;
              serverId: string;
              serverName: string;
              isCollection: boolean;
              icon?: string;
              contentType?: string;
              kind: string;
            }> = [];

            Object.entries(mcpServerInfos).forEach(([serverId, serverInfo]) => {
              const serverName = serverInfo.name || serverId;

              (serverInfo.resources || []).forEach((resource) => {
                  let resourceId = resource.id || resource.uri || '';
                  const isCollection = resource.kind === 'collection' || resource.kind === 'agience.collection';

                  if (isCollection && typeof resourceId === 'string' && resourceId.startsWith('collection:')) {
                    resourceId = resourceId.substring('collection:'.length);
                  }

                  allResources.push({
                    id: resourceId,
                    name: resource.title || resource.id || 'Untitled',
                    description: resource.text,
                    serverId,
                    serverName,
                    isCollection,
                    icon: resource.icon || serverInfo.icon,
                    contentType: resource.contentType || resource.content_type,
                    kind: resource.kind,
                  });
                });
            });

            if (allResources.length === 0) {
              return (
                <div className="px-2 h-7 text-sm text-muted-foreground flex items-center">
                  No resources
                </div>
              );
            }

            const getContentTypeCategory = (contentType?: string, kind?: string): string => {
              if (!contentType) {
                if (kind === 'collection' || kind === 'agience.collection') return 'Collections';
                return 'Other';
              }

              if (contentType.startsWith('text/')) return 'Documents';
              if (contentType.startsWith('image/')) return 'Images';
              if (contentType.startsWith('video/')) return 'Videos';
              if (contentType.startsWith('audio/')) return 'Audio';
              if (contentType.includes('pdf')) return 'Documents';
              if (contentType.includes('document') || contentType.includes('word')) return 'Documents';
              if (contentType.includes('spreadsheet') || contentType.includes('excel')) return 'Spreadsheets';
              if (contentType.includes('presentation') || contentType.includes('powerpoint')) return 'Presentations';

              return 'Other';
            };

            if (resourcesViewMode === 'by-collection') {
              const collections = allResources.filter(r => r.isCollection);
              if (collections.length === 0) {
                return (
                  <div className="px-2 h-7 text-sm text-muted-foreground flex items-center">
                    No collections available
                  </div>
                );
              }

              return collections.map((collection) => {
                const isDrawerOpen = resourcesDrawer?.collectionId === collection.id;
                return (
                  <div
                    key={`${collection.serverId}-${collection.id}`}
                    className={cn(
                      "px-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r",
                      isDrawerOpen && "bg-accent/70"
                    )}
                    title={collection.description || collection.name}
                    onClick={() => {
                      setToolsDrawer(null);
                      if (isDrawerOpen) {
                        setResourcesDrawer(null);
                      } else {
                        setResourcesDrawer({
                          collectionId: collection.id,
                          collectionName: collection.name,
                          collectionIcon: collection.icon,
                        });
                      }
                    }}
                  >
                    <ServerIcon icon={collection.icon} type="resources" size={16} className="text-muted-foreground flex-shrink-0" />
                    <span className="text-sm truncate flex-1">{collection.name}</span>
                    {isDrawerOpen && <span className="ml-auto text-muted-foreground">›</span>}
                  </div>
                );
              });
            }

            if (resourcesViewMode === 'by-type') {
              const grouped = new Map<string, typeof allResources>();
              allResources.forEach(resource => {
                const category = getContentTypeCategory(resource.contentType, resource.kind);
                if (!grouped.has(category)) grouped.set(category, []);
                grouped.get(category)!.push(resource);
              });

              return Array.from(grouped.entries()).map(([category, resources]) => (
                <div key={category}>
                  <div className="px-2 h-6 text-sm font-medium text-muted-foreground flex items-center">
                    {category}
                  </div>
                  {resources.map((resource) => {
                    const isDrawerOpen = resource.isCollection && resourcesDrawer?.collectionId === resource.id;
                    return (
                      <div
                        key={`${resource.serverId}-${resource.id}`}
                        className={cn(
                          "px-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r",
                          isDrawerOpen && "bg-accent/70"
                        )}
                        title={resource.description || resource.name}
                        onClick={() => {
                          if (resource.isCollection) {
                            setToolsDrawer(null);
                            if (isDrawerOpen) {
                              setResourcesDrawer(null);
                            } else {
                              setResourcesDrawer({
                                collectionId: resource.id,
                                collectionName: resource.name,
                                collectionIcon: resource.icon,
                              });
                            }
                          }
                        }}
                      >
                        <ServerIcon icon={resource.icon} type="resources" size={16} className="text-muted-foreground flex-shrink-0" />
                        <span className="text-sm truncate">{resource.name}</span>
                        {isDrawerOpen && <span className="ml-auto text-muted-foreground">›</span>}
                      </div>
                    );
                  })}
                </div>
              ));
            }

            return allResources.map((resource) => {
              const isDrawerOpen = resource.isCollection && resourcesDrawer?.collectionId === resource.id;
              return (
                <div
                  key={`${resource.serverId}-${resource.id}`}
                  className={cn(
                    "px-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r",
                    isDrawerOpen && "bg-accent/70"
                  )}
                  title={resource.description || resource.name}
                  onClick={() => {
                    if (resource.isCollection) {
                      setToolsDrawer(null);
                      if (isDrawerOpen) {
                        setResourcesDrawer(null);
                      } else {
                        setResourcesDrawer({
                          collectionId: resource.id,
                          collectionName: resource.name,
                          collectionIcon: resource.icon,
                        });
                      }
                    }
                  }}
                >
                  <ServerIcon icon={resource.icon} type="resources" size={16} className="text-muted-foreground flex-shrink-0" />
                  <span className="text-sm truncate">{resource.name}</span>
                  {isDrawerOpen && <span className="ml-auto text-muted-foreground">›</span>}
                </div>
              );
            });
          })()}
        </div>
      </div>
    );
  }

  function renderToolsContent() {
    // Mirrors the expanded "Tools" section rendering, but scoped for the collapsed drawer.
    return (
      <div className="pb-1">
        <div className="ml-5 border-l border-border">
          {(() => {
            const ids = Object.keys(mcpServerInfos);

            if (ids.length === 0) {
              return (
                <div className="px-2 h-7 text-sm text-muted-foreground flex items-center">
                  No MCP servers configured
                </div>
              );
            }

            const servers = ids.map(serverId => {
              const serverInfo = mcpServerInfos[serverId];
              return {
                id: serverId,
                name: serverInfo?.server || serverId,
                icon: serverInfo?.icon,
              };
            });

            return servers.map((server) => {
              const isActive = toolsDrawer?.serverId === server.id;
              return (
                <div
                  key={server.id}
                  className={`px-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r ${isActive ? 'bg-accent/70' : ''}`}
                  onClick={() => {
                    if (isActive) {
                      setToolsDrawer(null);
                    } else {
                      setResourcesDrawer(null);
                      setToolsDrawer({ serverId: server.id, serverName: server.name, serverIcon: server.icon });
                    }
                  }}
                >
                  <ServerIcon icon={server.icon} type="tool" size={16} className="text-muted-foreground flex-shrink-0" />
                  <span className="text-sm truncate flex-1">{server.name}</span>
                  {isActive && <span className="text-sm">›</span>}
                </div>
              );
            });
          })()}
        </div>
      </div>
    );
  }

  // Expanded sidebar
  return (
    <div ref={sidebarRef} className="flex flex-col h-full bg-white relative z-20">
      {/* Search bar at top of sidebar */}
      <div className="border-b border-border bg-white/95 backdrop-blur-sm">
        {/* Top row: search pill */}
        <div className="px-4 pt-2 pb-1 h-16 flex items-center">
          <AdvancedSearch
            scope={searchScope}
            scopeId={searchScope === 'global' ? '' : searchScopeId}
            placeholder={searchPlaceholder}
            enableSuggestions={false}
            onResults={onSearchResults}
            onClear={onSearchClear}
            sortMode={searchSortMode}
            aperture={searchAperture}
            onApertureChange={onSearchApertureChange}
            clearTrigger={clearSearchTrigger}
          />
        </div>
        {/* Bottom spacer row to match BrowserHeader height */}
        <div className="px-4 pb-1" />
              </div>

      {/* Sections or search results below search */}
      <div className="flex-1 overflow-y-auto py-2" style={{ scrollbarGutter: 'stable' }}>
        {searchResults && (
          <div className="px-3 pb-2">
            <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-1">
              Results
            </div>
            <div className="text-xs text-muted-foreground mb-3">
              {searchResults.hits.length === 0
                ? 'No matches yet. Try refining your search.'
                : `${searchResults.hits.length} match${searchResults.hits.length === 1 ? '' : 'es'} – full artifacts shown in the workspace.`}
            </div>
          </div>
        )}

        {!searchResults && (
          <>
        {/* Workspaces Section */}
        <SectionHeader
          id="workspaces"
          title="Workspaces"
          expanded={expandedSections.has('workspaces')}
          onToggle={() => toggleSection('workspaces')}
          onNew={openCreateWorkspace}
          onShowAll={() => handleToggleShowAll('workspaces')}
          openDropdownId={openDropdownId}
          onOpenDropdownChange={setOpenDropdownId}
          hasHiddenItems={(preferences.sidebarSections?.hiddenWorkspaces?.length ?? 0) > 0}
          showAllEnabled={preferences.sidebarSections?.showAllWorkspaces ?? false}
        />
        {expandedSections.has('workspaces') && (
          <div className="pb-1">
            <div className="ml-5 border-l border-border">
            {sortedWorkspaces().map((ws) => {
              const isHidden = preferences.sidebarSections?.hiddenWorkspaces?.includes(ws.id) ?? false;
              const showAll = preferences.sidebarSections?.showAllWorkspaces ?? false;
              if (isHidden && !showAll) return null;
              
              return (
              <SidebarItem
                key={ws.id}
                icon={Proportions}
                label={ws.name}
                active={activeSource?.type === 'workspace' && activeSource.id === ws.id}
                onClick={() => {
                  setActiveWorkspaceId(ws.id);
                  onActiveSourceChange({ type: 'workspace', id: ws.id });
                }}
                onRename={() => openEditWorkspace(ws)}
                onHide={() => isHidden ? handleShowWorkspace(ws.id) : handleHideWorkspace(ws.id)}
                onDeleteClick={() => setDeleteConfirmItem({ type: 'workspace', id: ws.id, name: ws.name })}
                canDelete={ws.id !== ws.created_by}
                canModify={true}
                itemType="workspace"
                isHidden={isHidden}
                showAll={showAll}
                itemId={ws.id}
                onArtifactDrop={(artifactId, sourceWorkspaceId) => 
                  handleWorkspaceDrop(ws.id, artifactId, sourceWorkspaceId)
                }
              />
            );
            })}
            {workspaces.length === 0 && (
              <div className="text-muted-foreground text-sm text-center py-2 px-2">
                No workspaces
              </div>
            )}
            </div>
          </div>
        )}

        {/* Resources Section */}
        <SectionHeader
          id="resources"
          title="Resources"
          expanded={expandedSections.has('resources')}
          onToggle={() => toggleSection('resources')}
          openDropdownId={openDropdownId}
          onOpenDropdownChange={setOpenDropdownId}
          resourcesViewMode={resourcesViewMode}
          onResourcesViewModeToggle={toggleResourcesViewMode}
          hideNewButton={true}
        />
        {expandedSections.has('resources') && (
          <div className="pb-1">
            <div className="ml-5 border-l border-border">
            {(() => {
              // Get all collections/resources from all MCP servers
              const allResources: Array<{
                id: string;
                name: string;
                description?: string;
                serverId: string;
                serverName: string;
                serverIcon?: string;
                isCollection: boolean;
                icon?: string;
                contentType?: string;
                kind: string;
              }> = [];

              // Add MCP resources (collections and other resources)
              Object.entries(mcpServerInfos).forEach(([serverId, serverInfo]) => {
                const serverName = serverInfo.name || serverId;

                (serverInfo.resources || []).forEach((resource) => {
                    let resourceId = resource.id || resource.uri || '';
                    // Check if this is a collection based on kind field
                    const isCollection = resource.kind === 'collection' || resource.kind === 'agience.collection';

                    // Strip "collection:" prefix if present (MCP protocol includes it, but backend expects just UUID)
                    if (isCollection && typeof resourceId === 'string' && resourceId.startsWith('collection:')) {
                      resourceId = resourceId.substring('collection:'.length);
                    }

                    allResources.push({
                      id: resourceId,
                      name: resource.title || resource.id || 'Untitled',
                      description: resource.text,
                      serverId,
                      serverName,
                      isCollection: isCollection,
                      icon: resource.icon || serverInfo.icon, // Prefer resource icon, fallback to server icon
                      contentType: resource.contentType || resource.content_type,
                      kind: resource.kind,
                    });
                  });
              });

                if (allResources.length === 0) {
                  return (
                    <div className="px-2 h-7 text-sm text-muted-foreground flex items-center">
                      No resources
                    </div>
                  );
                }

                // Helper function to categorize contentType
                const getContentTypeCategory = (contentType?: string, kind?: string): string => {
                  if (!contentType) {
                    if (kind === 'collection' || kind === 'agience.collection') return 'Collections';
                    return 'Other';
                  }

                  if (contentType.startsWith('text/')) return 'Documents';
                  if (contentType.startsWith('image/')) return 'Images';
                  if (contentType.startsWith('video/')) return 'Videos';
                  if (contentType.startsWith('audio/')) return 'Audio';
                  if (contentType.includes('pdf')) return 'Documents';
                  if (contentType.includes('document') || contentType.includes('word')) return 'Documents';
                  if (contentType.includes('spreadsheet') || contentType.includes('excel')) return 'Spreadsheets';
                  if (contentType.includes('presentation') || contentType.includes('powerpoint')) return 'Presentations';

                  return 'Other';
                };

                // by-collection (default): Show only collections
                if (resourcesViewMode === 'by-collection') {
                  const collections = allResources.filter(r => r.isCollection);
                  
                  if (collections.length === 0) {
                    return (
                      <div className="px-2 h-7 text-sm text-muted-foreground flex items-center">
                        No collections available
                      </div>
                    );
                  }

                  return collections.map((collection) => {
                    const isDrawerOpen = resourcesDrawer?.collectionId === collection.id;
                    
                    return (
                      <div
                        key={`${collection.serverId}-${collection.id}`}
                        className={cn(
                          "px-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r",
                          isDrawerOpen && "bg-accent/70"
                        )}
                        title={collection.description || collection.name}
                        onClick={() => {
                          // Close other drawers
                          setToolsDrawer(null);
                          // Toggle this drawer
                          if (isDrawerOpen) {
                            setResourcesDrawer(null);
                          } else {
                            setResourcesDrawer({
                              collectionId: collection.id,
                              collectionName: collection.name,
                              collectionIcon: collection.icon,
                            });
                          }
                        }}
                      >
                        <ServerIcon icon={collection.icon} type="resources" size={16} className="text-muted-foreground flex-shrink-0" />
                        <span className="text-sm truncate flex-1">{collection.name}</span>
                        {isDrawerOpen && <span className="ml-auto text-muted-foreground">›</span>}
                      </div>
                    );
                  });
                }
                
                // Group resources by view mode
                if (resourcesViewMode === 'by-type') {
                  // Group by contentType category
                  const grouped = new Map<string, typeof allResources>();
                  allResources.forEach(resource => {
                    const category = getContentTypeCategory(resource.contentType, resource.kind);
                    if (!grouped.has(category)) {
                      grouped.set(category, []);
                    }
                    grouped.get(category)!.push(resource);
                  });

                  return Array.from(grouped.entries()).map(([category, resources]) => (
                    <div key={category}>
                      <div className="px-2 h-6 text-sm font-medium text-muted-foreground flex items-center">
                        {category}
                      </div>
                      {resources.map((resource) => {
                        const isDrawerOpen = resource.isCollection && resourcesDrawer?.collectionId === resource.id;
                        const resourceIcon = resource.icon;
                        
                        return (
                          <div
                            key={`${resource.serverId}-${resource.id}`}
                            className={cn(
                              "px-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r",
                              isDrawerOpen && "bg-accent/70"
                            )}
                            title={resource.description || resource.name}
                            onClick={() => {
                              if (resource.isCollection) {
                                // Close other drawers
                                setToolsDrawer(null);
                                // Toggle this drawer
                                if (isDrawerOpen) {
                                  setResourcesDrawer(null);
                                } else {
                                  setResourcesDrawer({
                                    collectionId: resource.id,
                                    collectionName: resource.name,
                                    collectionIcon: resource.icon,
                                  });
                                }
                              }
                            }}
                          >
                            <ServerIcon icon={resourceIcon} type="resources" size={16} className="text-muted-foreground flex-shrink-0" />
                            <span className="text-sm truncate">{resource.name}</span>
                            {isDrawerOpen && <span className="ml-auto text-muted-foreground">›</span>}
                          </div>
                        );
                      })}
                    </div>
                  ));
                }

                // by-time and by-author: flat list (not yet fully implemented)
                return allResources.map((resource) => {
                  const isDrawerOpen = resource.isCollection && resourcesDrawer?.collectionId === resource.id;
                  const resourceIcon = resource.icon;
                  
                  // All resources rendered the same way (collections are read-only from MCP servers)
                  return (
                    <div
                      key={`${resource.serverId}-${resource.id}`}
                      className={cn(
                        "px-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r",
                        isDrawerOpen && "bg-accent/70"
                      )}
                      title={resource.description || resource.name}
                      onClick={() => {
                        if (resource.isCollection) {
                          // Close other drawers
                          setToolsDrawer(null);
                          // Toggle this drawer
                          if (isDrawerOpen) {
                            setResourcesDrawer(null);
                          } else {
                            setResourcesDrawer({
                              collectionId: resource.id,
                              collectionName: resource.name,
                              collectionIcon: resource.icon,
                            });
                          }
                        }
                      }}
                    >
                      <ServerIcon icon={resourceIcon} type="resources" size={16} className="text-muted-foreground flex-shrink-0" />
                      <span className="text-sm truncate">{resource.name}</span>
                      {isDrawerOpen && <span className="ml-auto text-muted-foreground">›</span>}
                    </div>
                  );
                });
            })()}
            </div>
          </div>
        )}

        {/* Tools Section */}
        <SectionHeader
          id="tools"
          title="Tools"
          expanded={expandedSections.has('tools')}
          onToggle={() => toggleSection('tools')}
          onShowAll={() => handleToggleShowAll('mcp-servers')}
          openDropdownId={openDropdownId}
          onOpenDropdownChange={setOpenDropdownId}
          hasHiddenItems={(preferences.sidebarSections?.hiddenMcpServers?.length ?? 0) > 0}
          showAllEnabled={preferences.sidebarSections?.showAllMcpServers ?? false}
          newButtonText="Add"
        />
        {expandedSections.has('tools') && (
          <div className="pb-1">
            <div className="ml-5 border-l border-border">
            {(() => {
                // Group tools by server (discovered from workspace mcp-server artifacts)
                const ids = Object.keys(mcpServerInfos);

                if (ids.length === 0) {
                  return (
                    <div className="px-2 h-7 text-sm text-muted-foreground flex items-center">
                      No MCP servers configured
                    </div>
                  );
                }

                const servers = ids.map(serverId => {
                  const serverInfo = mcpServerInfos[serverId];
                  const toolCount = serverInfo?.tools?.length || 0;
                  return {
                    id: serverId,
                    name: serverInfo?.server || serverId,
                    icon: serverInfo?.icon,
                    toolCount,
                  };
                });

                return servers.map((server) => {
                  const isActive = toolsDrawer?.serverId === server.id;
                  return (
                    <div
                      key={server.id}
                      className={`px-2 h-7 text-sm hover:bg-accent/50 cursor-pointer flex items-center gap-2 rounded-r ${isActive ? 'bg-accent/70' : ''}`}
                      onClick={() => {
                        // Toggle: close if same drawer is clicked, otherwise open new one and close others
                        if (isActive) {
                          setToolsDrawer(null);
                        } else {
                          setResourcesDrawer(null); // Close resources drawer
                          setToolsDrawer({ serverId: server.id, serverName: server.name, serverIcon: server.icon });
                        }
                      }}
                    >
                      <ServerIcon icon={server.icon} type="tool" size={16} className="text-muted-foreground flex-shrink-0" />
                      <span className="text-sm truncate flex-1">{server.name}</span>
                      {isActive && <span className="text-sm">›</span>}
                    </div>
                  );
                });
            })()}
            </div>
          </div>
        )}

        {/* End sections fragment shown when no search is active */}
        </>
        )}
      </div>

      {/* Modals */}
      <CollectionDetailModal
        open={showCollectionsModal}
        onClose={() => setShowCollectionsModal(false)}
      />

      <WorkspaceDetailModal
        open={showWorkspaceModal}
        mode={isCreatingWorkspace ? 'create' : 'edit'}
        initial={{ name: editingWorkspace?.name ?? '', description: editingWorkspace?.description ?? '' }}
        onSave={handleWorkspaceSave}
        onClose={() => setShowWorkspaceModal(false)}
      />

      <ConfirmDeleteModal
        open={!!deleteConfirmItem}
        itemType={deleteConfirmItem?.type}
        itemName={deleteConfirmItem?.name}
        onConfirm={async () => {
          if (!deleteConfirmItem) return;
          
          try {
            if (deleteConfirmItem.type === 'workspace') {
              await deleteWorkspace(deleteConfirmItem.id);
              if (activeSource?.id === deleteConfirmItem.id) {
                onActiveSourceChange({ type: 'workspace', id: workspaces[0]?.id || '' });
              }
            }
            setDeleteConfirmItem(null);
          } catch (error) {
            console.error('Delete failed:', error);
          }
        }}
        onCancel={() => setDeleteConfirmItem(null)}
      />

      {/* Tools drawer (legacy bottom style) */}
      {toolsDrawer && (
        <div 
          className="fixed bg-white border-r border-b border-border shadow-sm z-10 transition-opacity duration-150"
          style={{
            left: `${actualSidebarWidth}px`,
            width: typeof drawerWidth === 'number' ? `${drawerWidth}px` : drawerWidth,
            bottom: '48px',
            height: '380px',
            opacity: drawerAnimating ? 1 : 0,
            borderLeft: '1px solid hsl(var(--border))',
          }}
        >
          {/* Header bar */}
          <div className="flex items-center justify-between px-4 py-2 border bg-gray-50/80 rounded-t">
            <div className="flex items-center gap-2.5">
              <ServerIcon icon={toolsDrawer.serverIcon} type="tool" size={18} />
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold">{toolsDrawer.serverName}</span>
                <span className="text-xs text-gray-500 font-medium">Tools</span>
              </div>
            </div>
            <button 
              onClick={() => setToolsDrawer(null)} 
              className="p-1 hover:bg-gray-200 rounded-sm transition-colors"
              aria-label="Close"
            >
              <svg width="14" height="14" viewBox="0 0 15 15" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M11.7816 4.03157C12.0062 3.80702 12.0062 3.44295 11.7816 3.2184C11.5571 2.99385 11.193 2.99385 10.9685 3.2184L7.50005 6.68682L4.03164 3.2184C3.80708 2.99385 3.44301 2.99385 3.21846 3.2184C2.99391 3.44295 2.99391 3.80702 3.21846 4.03157L6.68688 7.49999L3.21846 10.9684C2.99391 11.193 2.99391 11.557 3.21846 11.7816C3.44301 12.0061 3.80708 12.0061 4.03164 11.7816L7.50005 8.31316L10.9685 11.7816C11.193 12.0061 11.5571 12.0061 11.7816 11.7816C12.0062 11.557 12.0062 11.193 11.7816 10.9684L8.31322 7.49999L11.7816 4.03157Z" fill="currentColor" fillRule="evenodd" clipRule="evenodd"></path>
              </svg>
            </button>
          </div>
          
          {/* Content area with CardGrid */}
          <div className="overflow-y-auto bg-white p-4" style={{ height: 'calc(100% - 45px)' }}>
            <CardGrid
              artifacts={toolsToArtifacts(toolsDrawer.serverId)}
              selectable={true}
              draggable={false}
              editable={false}
              inPanel={true}
              fillHeight={false}
              activeSource={{ type: 'mcp-server', id: toolsDrawer.serverId }}
            />
          </div>
        </div>
      )}

      {/* Resources drawer (legacy bottom style) */}
      {resourcesDrawer && (
        <div 
          className="fixed bg-white border-r border-b border-border shadow-sm z-10 transition-opacity duration-150"
          style={{
            left: `${actualSidebarWidth}px`,
            width: typeof drawerWidth === 'number' ? `${drawerWidth}px` : drawerWidth,
            bottom: '48px',
            height: '380px',
            opacity: drawerAnimating ? 1 : 0,
            borderLeft: '1px solid hsl(var(--border))',
          }}
        >
          {/* Header bar */}
          <div className="flex items-center justify-between px-4 py-2.5 border bg-gray-50/80 rounded-t">
            <div className="flex items-center gap-2.5">
              <ServerIcon icon={resourcesDrawer.collectionIcon} type="resources" size={18} />
              <span className="text-sm font-semibold">{resourcesDrawer.collectionName}</span>
            </div>
            <button 
              onClick={() => setResourcesDrawer(null)} 
              className="p-1 hover:bg-gray-200 rounded-sm transition-colors"
              aria-label="Close"
            >
              <svg width="14" height="14" viewBox="0 0 15 15" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M11.7816 4.03157C12.0062 3.80702 12.0062 3.44295 11.7816 3.2184C11.5571 2.99385 11.193 2.99385 10.9685 3.2184L7.50005 6.68682L4.03164 3.2184C3.80708 2.99385 3.44301 2.99385 3.21846 3.2184C2.99391 3.44295 2.99391 3.80702 3.21846 4.03157L6.68688 7.49999L3.21846 10.9684C2.99391 11.193 2.99391 11.557 3.21846 11.7816C3.44301 12.0061 3.80708 12.0061 4.03164 11.7816L7.50005 8.31316L10.9685 11.7816C11.193 12.0061 11.5571 12.0061 11.7816 11.7816C12.0062 11.557 12.0062 11.193 11.7816 10.9684L8.31322 7.49999L11.7816 4.03157Z" fill="currentColor" fillRule="evenodd" clipRule="evenodd"></path>
              </svg>
            </button>
          </div>
          
          {/* Content area with CardGrid */}
          <div className="overflow-y-auto bg-white p-4" style={{ height: 'calc(100% - 45px)' }}>
            <CardGrid
              artifacts={collectionArtifacts}
              selectable={true}
              draggable={false}
              editable={false}
              inPanel={true}
              fillHeight={false}
              activeSource={{ type: 'collection', id: resourcesDrawer.collectionId }}
            />
          </div>
        </div>
      )}
    </div>
  );
};

// Helper Components
// Section Header Component
interface SectionHeaderProps {
  title: string;
  expanded: boolean;
  onToggle: () => void;
  onNew?: () => void;
  onShowAll?: () => void;
  id: string;
  openDropdownId: string | null;
  onOpenDropdownChange: (id: string | null) => void;
  newButtonText?: string; // Custom text for "New" button
  hasHiddenItems?: boolean; // Show "Show All" only if there are hidden items
  showAllEnabled?: boolean; // Whether "Show All" mode is currently enabled
  resourcesViewMode?: ResourcesViewMode; // For Resources section only
  onResourcesViewModeToggle?: () => void; // Toggle resources view modes
  hideNewButton?: boolean; // Hide the "New" button for read-only sections
}

function SectionHeader({ title, expanded, onToggle, onNew, onShowAll, id, openDropdownId, onOpenDropdownChange, newButtonText = 'New', hasHiddenItems = false, showAllEnabled = false, resourcesViewMode, onResourcesViewModeToggle, hideNewButton = false }: SectionHeaderProps) {
  const isMenuOpen = openDropdownId === id;
  return (
    <div 
      className="group flex items-center px-3 h-8 mt-2 first:mt-0 border-b border-gray-200"
    >
      {/* Chevron - compact */}
      <button
        onClick={onToggle}
        className="h-4 w-4 flex-shrink-0 p-0 flex items-center justify-center text-muted-foreground hover:text-foreground transition-colors"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
      </button>

      {/* Title - Jira style: uppercase, small, medium weight */}
      <span 
        className="text-xs font-medium text-muted-foreground uppercase tracking-wide flex-1 cursor-pointer ml-1"
        onClick={onToggle}
      >
        {title}
      </span>

      {/* Context menu - show on hover or when open */}
      <DropdownMenu open={isMenuOpen} onOpenChange={(open) => onOpenDropdownChange(open ? id : null)}>
        <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
          <IconButton
            size="xs"
            variant="ghost"
            className={cn(
              isMenuOpen ? "opacity-100" : "opacity-0 group-hover:opacity-100"
            )}
          >
            <MoreVertical size={14} />
          </IconButton>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          {!hideNewButton && onNew && (
            <DropdownMenuItem onClick={onNew}>
              <PlusCircle className="mr-2 h-4 w-4" />
              {newButtonText}
            </DropdownMenuItem>
          )}
          {hasHiddenItems && onShowAll && (
            <DropdownMenuItem onClick={onShowAll}>
              {showAllEnabled ? (
                <>
                  <Eye className="mr-2 h-4 w-4" />
                  Show Less
                </>
              ) : (
                <>
                  <Eye className="mr-2 h-4 w-4" />
                  Show All
                </>
              )}
            </DropdownMenuItem>
          )}
          {resourcesViewMode && onResourcesViewModeToggle && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={onResourcesViewModeToggle}>
                {resourcesViewMode === 'by-collection' && (
                  <>
                    <FolderOpen className="mr-2 h-4 w-4" />
                    Group by Collection
                  </>
                )}
                {resourcesViewMode === 'by-type' && (
                  <>
                    <FileType className="mr-2 h-4 w-4" />
                    Group by Type
                  </>
                )}
                {resourcesViewMode === 'by-time' && (
                  <>
                    <Clock className="mr-2 h-4 w-4" />
                    Group by Time
                  </>
                )}
                {resourcesViewMode === 'by-author' && (
                  <>
                    <User className="mr-2 h-4 w-4" />
                    Group by Author
                  </>
                )}
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

// Sidebar Item Component
interface SidebarItemProps {
  icon: React.ComponentType<{ size?: number; className?: string }>;
  label: string;
  active: boolean;
  onClick: () => void;
  onRename: () => void;
  onHide: () => void;
  onDeleteClick: () => void;
  isHidden?: boolean;
  showAll?: boolean;
  canDelete?: boolean;
  canModify?: boolean;
  itemType: 'workspace' | 'collection' | 'mcp-server';
  onShare?: () => void;  // For collections
  itemId?: string; // Workspace or collection ID for drop operations
  onArtifactDrop?: (artifactId: string, sourceWorkspaceId?: string) => void;
}

function SidebarItem({ 
  icon: Icon, 
  label, 
  active, 
  onClick,
  onRename,
  onHide,
  onDeleteClick,
  isHidden = false,
  showAll = false,
  canDelete = true,
  canModify = true,
  itemType,
  onShare,
  onArtifactDrop,
}: SidebarItemProps) {
  const [isDropTarget, setIsDropTarget] = useState(false);

  const parseDropPayload = (dt: DataTransfer) => {
    try {
      const raw = dt.getData('application/x-agience-artifact') || dt.getData('application/json');
      if (raw) {
        const parsed = JSON.parse(raw) as {
          ids?: unknown;
          rootIds?: unknown;
          workspaceId?: unknown;
          sourceWorkspaceId?: unknown;
        };
        const ids = Array.isArray(parsed.ids)
          ? parsed.ids.map(String).filter(Boolean)
          : [];
        const rootIds = Array.isArray(parsed.rootIds)
          ? parsed.rootIds.map(String).filter(Boolean)
          : [];

        if (ids.length > 0 || rootIds.length > 0) {
          return {
            ids: ids.length > 0 ? ids : rootIds,
            rootIds: rootIds.length > 0 ? rootIds : undefined,
            sourceWorkspaceId: typeof parsed.workspaceId === 'string'
              ? parsed.workspaceId
              : typeof parsed.sourceWorkspaceId === 'string'
                ? parsed.sourceWorkspaceId
                : undefined,
          };
        }
      }
    } catch {
      // fall through
    }

    const rawText = dt.getData('text/plain');
    if (rawText) {
      return {
        ids: rawText.split(',').map((value) => value.trim()).filter(Boolean),
        rootIds: undefined,
        sourceWorkspaceId: undefined,
      };
    }

    return {
      ids: [] as string[],
      rootIds: undefined as string[] | undefined,
      sourceWorkspaceId: undefined as string | undefined,
    };
  };

  return (
    <div className="">
      <div
        className={cn(
          "group cursor-pointer rounded px-2 h-7 flex items-center gap-2 hover:bg-accent/50 transition-colors relative",
          active && "bg-primary/10",
          isHidden && !showAll && "opacity-50",
          isDropTarget && "ring-1 ring-primary/40 bg-primary/5"
        )}
        onDragOver={(e) => {
          if (!onArtifactDrop) return;
          e.preventDefault();
          e.stopPropagation();
          setIsDropTarget(true);
          if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
        }}
        onDragLeave={() => {
          if (!onArtifactDrop) return;
          setIsDropTarget(false);
        }}
        onDrop={(e) => {
          if (!onArtifactDrop) return;
          e.preventDefault();
          e.stopPropagation();
          setIsDropTarget(false);
          const { ids, rootIds, sourceWorkspaceId } = parseDropPayload(e.dataTransfer);
          ids.forEach((artifactId, index) => {
            // For imports (no source workspace), prefer root IDs so we import the
            // latest head artifact, not a stale historical version from search.
            const idForDrop = !sourceWorkspaceId
              ? (rootIds?.[index] || artifactId)
              : artifactId;
            onArtifactDrop(idForDrop, sourceWorkspaceId);
          });
        }}
      >
        {/* Main clickable area for item */}
        <div className="flex items-center gap-2 flex-1 min-w-0" onClick={onClick}>
          <Icon size={16} className="text-foreground/70 flex-shrink-0" />
          <span className="text-sm truncate flex-1">{label}</span>
        </div>
        {/* Three dots menu visible on hover */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button 
              className="opacity-0 group-hover:opacity-100 flex items-center justify-center hover:bg-accent rounded p-0.5"
              onClick={(e) => e.stopPropagation()}
            >
              <MoreVertical size={14} className="text-muted-foreground" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
          {canModify && (
            <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onRename(); }}>
              <Pencil className="mr-2 h-4 w-4" />
              Rename
            </DropdownMenuItem>
          )}
          {itemType === 'collection' && onShare && (
            <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onShare(); }}>
              <Share2 className="mr-2 h-4 w-4" />
              Share
            </DropdownMenuItem>
          )}
          <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onHide(); }}>
            {isHidden ? (
              <>
                <Eye className="mr-2 h-4 w-4" />
                Show
              </>
            ) : (
              <>
                <EyeOff className="mr-2 h-4 w-4" />
                Hide
              </>
            )}
          </DropdownMenuItem>
          {canDelete && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem 
                className="text-destructive"
                onClick={(e) => { e.stopPropagation(); onDeleteClick(); }}
              >
                <Trash2 className="mr-2 h-4 w-4" />
                Delete
              </DropdownMenuItem>
            </>
          )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}

function ConfirmDeleteModal({
  open,
  itemType,
  itemName,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  itemType?: 'workspace' | 'collection' | 'mcp-server';
  itemName?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;

  const typeLabel = itemType === 'mcp-server' ? 'MCP server' : itemType;

  return (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete {typeLabel}?</DialogTitle>
          <DialogDescription>
            Are you sure you want to delete <strong>{itemName}</strong>? This action cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={async () => {
              try {
                await onConfirm();
                if (itemType === 'workspace') {
                  toast.success('Workspace deleted');
                } else if (itemType === 'collection') {
                  toast.success('Collection deleted');
                } else if (itemType === 'mcp-server') {
                  toast.success('MCP server deleted');
                }
                onCancel();
              } catch (error) {
                console.error('Delete failed:', error);
              }
            }}
          >
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}