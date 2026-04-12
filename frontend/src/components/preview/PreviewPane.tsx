// "Preview Pane" – right-hand panel for inline artifact editing/preview (distinct from Artifact Preview tiles).
import { useMemo, useState } from 'react';
import { FileText, Tag, FolderInput, Trash2, Archive, Send } from 'lucide-react';
import { useWorkspace } from '../../hooks/useWorkspace';
import { CardEditor } from './CardEditor';
import { CollectionPicker } from '../modals/CollectionPicker';
import { BulkTagDialog, type BulkTagApplyRequest } from '../modals/BulkTagDialog';
import { addArtifactToCollection } from '../../api/collections';
import { useConfirm } from '../../context/dialog/useConfirm';
import { BULK_CONFIRM, BUTTON_LABELS } from '@/constants/strings';
import { getContentType } from '@/registry/content-types';
import ContainerCardViewer from '@/components/containers/ContainerCardViewer';
import ViewCardViewer from '@/components/view/ViewCardViewer';
import { safeParseArtifactContext, stringifyArtifactContext } from '@/utils/artifactContext';

interface PreviewPaneProps {
  /** ID of artifact to preview (null = no selection) */
  artifactId: string | null;
  /** Multiple artifact IDs (for multi-select summary) */
  selectedArtifactIds?: string[];
  /** Control collection picker visibility from outside */
  openCollectionPicker?: boolean;
  /** Callback when collection picker is closed */
  onCollectionPickerClose?: () => void;
  /** Callback when close button clicked */
  onClose?: () => void;
  /** Callback when a container view requests opening a collection on the desktop */
  onOpenCollection?: (collectionId: string) => void;
}

/**
 * PreviewPane – right-hand Preview Pane for artifact preview/editing.
 *
 * Shows selected artifact with inline editing capabilities.
 * No separate modal - editing happens directly in this panel.
 *
 * Handles three states:
 * 1. No artifact selected - Empty state
 * 2. Single artifact selected - Full inline editor
 * 3. Multiple artifacts selected - Summary view
 * 
 * @example
 * ```tsx
 * <PreviewPane
 *   artifactId={selectedArtifactId}
 *   selectedArtifactIds={multipleIds}
 *   openCollectionPicker={showPicker}
 *   onCollectionPickerClose={() => setShowPicker(false)}
 *   onClose={() => setSelectedArtifactId(null)}
 * />
 * ```
 */
export function PreviewPane({ 
  artifactId, 
  selectedArtifactIds = [], 
  onClose,
  onOpenCollection,
}: PreviewPaneProps) {
  const isMultiSelect = selectedArtifactIds.length > 1;
  const hasSingleSelection = selectedArtifactIds.length === 1;
  
  // Determine which artifact to show
  const displayArtifactId = artifactId || (hasSingleSelection ? selectedArtifactIds[0] : null);

  const { artifacts } = useWorkspace();
  const displayArtifact = useMemo(
    () => artifacts.find((c) => String(c.id) === String(displayArtifactId)) || null,
    [artifacts, displayArtifactId]
  );
  const displayContentType = useMemo(
    () => (displayArtifact ? getContentType(displayArtifact) : null),
    [displayArtifact]
  );

  if (!displayArtifactId && selectedArtifactIds.length === 0) {
    return <EmptyState />;
  }

  if (isMultiSelect) {
    return <MultiSelectSummary 
      count={selectedArtifactIds.length} 
      onClose={onClose}
    />;
  }

  if (displayArtifact && displayContentType?.id === 'view') {
    return (
      <ViewCardViewer
        artifact={displayArtifact}
        mode="preview"
        onOpenCollection={onOpenCollection}
      />
    );
  }

  if (displayArtifact && displayContentType?.isContainer) {
    return <ContainerCardViewer artifact={displayArtifact} mode="tree" onOpenCollection={onOpenCollection} />;
  }

  return (
    <CardEditor 
      artifactId={displayArtifactId!} 
      onClose={onClose} 
    />
  );
}

/**
 * EmptyState - No artifact selected
 */
function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-full px-8 text-center">
      <FileText className="w-16 h-16 text-gray-300 mb-4" />
      <h3 className="text-lg font-semibold text-gray-700 mb-2">No card selected</h3>
      <p className="text-sm text-gray-500">
        Select a card from the list to preview its content here
      </p>
    </div>
  );
}

/**
 * MultiSelectSummary - Show summary when multiple artifacts selected
 */
function MultiSelectSummary({ count, onClose }: { count: number; onClose?: () => void }) {
  const { artifacts, selectedArtifactIds, removeArtifact, updateArtifact, unselectAllArtifacts } = useWorkspace();
  const dangerConfirm = useConfirm();
  const [showCollectionPicker, setShowCollectionPicker] = useState(false);
  const [showBulkTagDialog, setShowBulkTagDialog] = useState(false);
  
  // Get selected artifacts with their titles
  const selectedArtifacts = useMemo(() => {
    return artifacts
      .filter(c => selectedArtifactIds.includes(String(c.id)))
      .map(c => {
        let ctx: { title?: string; filename?: string; [key: string]: unknown } = {};
        try {
          ctx = typeof c.context === 'string' ? JSON.parse(c.context) : (c.context || {});
        } catch (error) {
          // If context is not valid JSON, treat it as plain text or use empty object
          console.warn('Failed to parse artifact context:', c.id, error);
          ctx = {};
        }
        return {
          id: String(c.id),
          title: ctx.title || ctx.filename || 'Untitled Card',
          state: c.state,
        };
      });
  }, [artifacts, selectedArtifactIds]);

  const handleBulkMove = () => {
    setShowCollectionPicker(true);
  };

  const handleBulkSelectCollections = async (collectionIds: string[]) => {
    // Add all selected artifacts to the chosen collections via edge operations
    for (const artifactId of selectedArtifactIds) {
      for (const collectionId of collectionIds) {
        await addArtifactToCollection(collectionId, artifactId);
      }
    }
    setShowCollectionPicker(false);
    unselectAllArtifacts();
  };

  const handleBulkAddTags = () => {
    setShowBulkTagDialog(true);
  };

  const handleBulkApplyTags = async ({ tags, replaceExisting }: BulkTagApplyRequest) => {
    for (const artifactId of selectedArtifactIds) {
      const artifact = artifacts.find((entry) => String(entry.id) === artifactId);
      if (!artifact) continue;

      const context = safeParseArtifactContext(artifact.context);
      const currentTags = Array.isArray(context.tags) ? context.tags.map(String) : [];
      await updateArtifact({
        id: artifactId,
        context: stringifyArtifactContext({
          ...context,
          tags: replaceExisting ? tags : Array.from(new Set([...currentTags, ...tags])),
        }),
      });
    }
  };

  const handleSendToAI = () => {
    alert('Send to AI agent feature coming soon!');
  };

  const handleBulkDelete = async () => {
    const confirmed = await dangerConfirm.confirm({
      title: BULK_CONFIRM.DELETE_TITLE(count),
      description: BULK_CONFIRM.DELETE_DESCRIPTION(count),
      confirmLabel: BUTTON_LABELS.DELETE,
      cancelLabel: BUTTON_LABELS.CANCEL
    });
    
    if (confirmed) {
      for (const artifactId of selectedArtifactIds) {
        await removeArtifact(artifactId);
      }
      unselectAllArtifacts();
      onClose?.();
    }
  };

  const handleBulkArchive = async () => {
    for (const artifactId of selectedArtifactIds) {
      await updateArtifact({ id: artifactId, state: 'archived' });
    }
    unselectAllArtifacts();
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
        <h2 className="text-base font-semibold text-gray-900">
          {count} Cards Selected
        </h2>
        <button
          onClick={() => {
            unselectAllArtifacts();
            onClose?.();
          }}
          className="p-1 hover:bg-gray-100 rounded transition-colors"
          aria-label="Clear selection"
        >
          <span className="text-xl leading-none text-gray-500">×</span>
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-4 py-4">
        <p className="text-sm text-gray-500 mb-4">
          Perform bulk actions on all selected artifacts
        </p>

        {/* List of selected artifacts */}
        <div className="space-y-2 mb-6">
          {selectedArtifacts.slice(0, 10).map((artifact) => (
            <div key={artifact.id} className="flex items-center gap-2 text-sm">
              <div className="w-2 h-2 rounded-full bg-blue-500 flex-shrink-0"></div>
              <span className="text-gray-700 truncate flex-1">{artifact.title}</span>
              {artifact.state === 'draft' && (
                <span className="px-1.5 py-0.5 text-xs font-medium bg-green-100 text-green-700 rounded flex-shrink-0">
                  New
                </span>
              )}
              {artifact.state === 'committed' && (
                <span className="px-1.5 py-0.5 text-xs font-medium bg-amber-100 text-amber-700 rounded flex-shrink-0">
                  Modified
                </span>
              )}
              {artifact.state === 'archived' && (
                <span className="px-1.5 py-0.5 text-xs font-medium bg-red-100 text-red-700 rounded flex-shrink-0">
                  Archived
                </span>
              )}
            </div>
          ))}
          {count > 10 && (
            <p className="text-xs text-gray-400 ml-4">
              ... and {count - 10} more
            </p>
          )}
        </div>
      </div>

      {/* Bulk Actions */}
      <div className="px-4 py-3 border-t border-gray-200 space-y-2">
        <button
          onClick={handleBulkMove}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm font-medium text-blue-600 bg-blue-50 rounded hover:bg-blue-100 transition-colors"
        >
          <FolderInput className="w-4 h-4" />
          Move to Collection
        </button>
        <button
          onClick={handleBulkAddTags}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded hover:bg-gray-200 transition-colors"
        >
          <Tag className="w-4 h-4" />
          Add Tags
        </button>
        <button
          onClick={handleSendToAI}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm font-medium text-purple-600 bg-purple-50 rounded hover:bg-purple-100 transition-colors"
        >
          <Send className="w-4 h-4" />
          Send to AI Agent
        </button>
        <button
          onClick={handleBulkArchive}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm font-medium text-orange-600 bg-orange-50 rounded hover:bg-orange-100 transition-colors"
        >
          <Archive className="w-4 h-4" />
          Archive All
        </button>
        <button
          onClick={handleBulkDelete}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm font-medium text-red-600 bg-red-50 rounded hover:bg-red-100 transition-colors"
        >
          <Trash2 className="w-4 h-4" />
          Delete All
        </button>
      </div>

      {/* Collection Picker Modal */}
      <CollectionPicker
        open={showCollectionPicker}
        onClose={() => setShowCollectionPicker(false)}
        onSelect={handleBulkSelectCollections}
        multiple={true}
        title={`Move ${selectedArtifactIds.length} Artifacts to Collection`}
      />

      <BulkTagDialog
        open={showBulkTagDialog}
        onClose={() => setShowBulkTagDialog(false)}
        onApply={handleBulkApplyTags}
        selectedCount={selectedArtifactIds.length}
      />
    </div>
  );
}
