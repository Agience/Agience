// src/context/workspace/__tests__/WorkspaceProvider.test.jsx
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { WorkspaceProvider } from '../WorkspaceProvider';
import { useWorkspace } from '../WorkspaceContext';
import { mockArtifact } from '../../../../tests/utils/helpers';

// Mock dependencies
vi.mock('../../../api/workspaces', () => ({
  listWorkspaceArtifacts: vi.fn(),
  addArtifactToWorkspace: vi.fn(),
  updateWorkspaceArtifact: vi.fn(),
  removeWorkspaceArtifact: vi.fn(),
  revertWorkspaceArtifact: vi.fn(),
  commitWorkspace: vi.fn(),
  previewWorkspaceCommit: vi.fn(),
  orderWorkspaceArtifacts: vi.fn(),
  subscribeWorkspaceEvents: vi.fn(() => () => {}),
}));

vi.mock('../../../hooks/useAuth', () => ({
  useAuth: () => ({
    user: { id: 'user-1', email: 'test@example.com' }
  })
}));

vi.mock('../../../api/agent', () => ({
  extractInformation: vi.fn(),
}));

const { useWorkspacesMock } = vi.hoisted(() => ({
  useWorkspacesMock: vi.fn().mockReturnValue({
    activeWorkspace: { id: 'ws-1', name: 'Test Workspace' }
  })
}));

vi.mock('../../workspaces/WorkspacesContext', () => ({
  useWorkspaces: (...args) => useWorkspacesMock(...args)
}));

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
  }
}));

import {
  listWorkspaceArtifacts,
  addArtifactToWorkspace,
  updateWorkspaceArtifact,
  removeWorkspaceArtifact,
  revertWorkspaceArtifact,
  previewWorkspaceCommit,
  commitWorkspace,
} from '../../../api/workspaces';

import { extractInformation } from '../../../api/agent';

// Test consumer component
function TestConsumer() {
  const workspace = useWorkspace();
  
  return (
    <div>
      <div data-testid="artifact-count">{workspace.artifacts.length}</div>
      <div data-testid="selected-count">{workspace.selectedArtifactIds.length}</div>
      <div data-testid="committing">{workspace.isCommitting ? 'yes' : 'no'}</div>
      <button onClick={() => workspace.addArtifact({ content: 'New artifact content' })}>
        Add Artifact
      </button>
      <button onClick={() => workspace.selectAllArtifacts()}>
        Select All
      </button>
      <button onClick={() => workspace.unselectAllArtifacts()}>
        Unselect All
      </button>
      <button onClick={() => workspace.extractInformationFromSelection()}>
        Extract Selection
      </button>
      <button onClick={() => workspace.fetchCommitPreview()}>
        Preview Commit
      </button>
      <div data-testid="preview-count">
        {workspace.commitPreview?.plan.total_artifacts ?? 0}
      </div>
      {workspace.artifacts.map(artifact => (
        <div key={artifact.id} data-testid={`artifact-${artifact.id}`}>
          {artifact.title || artifact.description}
        </div>
      ))}
    </div>
  );
}

describe('WorkspaceProvider', () => {
  const mockArtifacts = [
    mockArtifact({ id: '1', title: 'Artifact 1', state: 'committed' }),
    mockArtifact({ id: '2', title: 'Artifact 2', state: 'draft' }),
    mockArtifact({ id: '3', title: 'Artifact 3', state: 'draft' }),
  ];

  afterEach(() => {
    // Safety net: if a test enables fake timers and fails before cleanup,
    // subsequent tests that rely on RTL polling will hang.
    vi.useRealTimers();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    listWorkspaceArtifacts.mockResolvedValue({ items: mockArtifacts });
    extractInformation.mockResolvedValue({
      workspace_id: 'ws-1',
      source_artifact_id: '1',
      created_artifact_ids: ['10', '11'],
      unit_count: 2,
    });
    previewWorkspaceCommit.mockResolvedValue({
      workspace_id: 'ws-1',
      plan: {
        artifacts: [],
        collections: [],
        total_artifacts: 0,
        total_adds: 0,
        total_removes: 0,
        blocked_collections: [],
      },
      dry_run: true,
      updated_workspace_artifacts: [],
      deleted_workspace_artifact_ids: [],
      skipped_workspace_artifact_ids: [],
      per_collection: [],
    });
    useWorkspacesMock.mockReturnValue({
      activeWorkspace: { id: 'ws-1', name: 'Test Workspace' }
    });
  });

  describe('Extract Units Workflow', () => {
    it('uses selection to infer source + artifacts and calls extractInformation', async () => {
      const user = userEvent.setup();

      render(
        <WorkspaceProvider>
          <TestConsumer />
        </WorkspaceProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('3');
      });

      await user.click(screen.getByText('Select All'));
      await user.click(screen.getByText('Extract Selection'));

      await waitFor(() => {
        expect(extractInformation).toHaveBeenCalledWith('ws-1', '1', ['2', '3']);
      });
    });
  });

  describe('Initial Load', () => {
    it('fetches and displays workspace artifacts on mount', async () => {
      render(
        <WorkspaceProvider>
          <TestConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('3');
      });
      
      expect(listWorkspaceArtifacts).toHaveBeenCalledWith('ws-1');
      expect(screen.getByTestId('artifact-1')).toHaveTextContent('Artifact 1');
      expect(screen.getByTestId('artifact-2')).toHaveTextContent('Artifact 2');
      expect(screen.getByTestId('artifact-3')).toHaveTextContent('Artifact 3');
    });

    it('handles API errors gracefully', async () => {
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
      listWorkspaceArtifacts.mockRejectedValueOnce(new Error('Network error'));
      
      render(
        <WorkspaceProvider>
          <TestConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('0');
      });
      
      expect(consoleSpy).toHaveBeenCalledWith('Failed to load artifacts', expect.any(Error));
      
      consoleSpy.mockRestore();
    });

    it('clears artifacts when no active workspace', async () => {
      const { rerender } = render(
        <WorkspaceProvider>
          <TestConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('3');
      });
      
      // Mock no active workspace
  useWorkspacesMock.mockReturnValueOnce({ activeWorkspace: null });
      
      rerender(
        <WorkspaceProvider>
          <TestConsumer />
        </WorkspaceProvider>
      );
      
      // Artifacts should be cleared
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('0');
      });
    });
  });

  describe('Add Artifact', () => {
    it('adds new artifact via addArtifact()', async () => {
      const user = userEvent.setup();
      const newArtifact = mockArtifact({ id: '4', title: 'New Artifact', state: 'draft' });
      addArtifactToWorkspace.mockResolvedValueOnce(newArtifact);
      
      render(
        <WorkspaceProvider>
          <TestConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('3');
      });
      
      await user.click(screen.getByText('Add Artifact'));
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('4');
      });
      
      expect(addArtifactToWorkspace).toHaveBeenCalledWith('ws-1', {
        content: 'New artifact content',
        context: expect.any(String),
      });
      expect(screen.getByTestId('artifact-4')).toHaveTextContent('New Artifact');
    });

    it('handles add artifact errors', async () => {
      const user = userEvent.setup();
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
      addArtifactToWorkspace.mockRejectedValueOnce(new Error('Failed to create'));
      
      render(
        <WorkspaceProvider>
          <TestConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('3');
      });
      
      await user.click(screen.getByText('Add Artifact'));
      
      await waitFor(() => {
        expect(consoleSpy).toHaveBeenCalledWith('Failed to create artifact', expect.any(Error));
      });
      
      // Artifact count should remain 3
      expect(screen.getByTestId('artifact-count')).toHaveTextContent('3');
      
      consoleSpy.mockRestore();
    });
  });

  describe('Commit Preview', () => {
    it('fetches and stores commit preview data', async () => {
      const user = userEvent.setup();
      previewWorkspaceCommit.mockResolvedValueOnce({
        workspace_id: 'ws-1',
        plan: {
          artifacts: [],
          collections: [],
          total_artifacts: 2,
          total_adds: 1,
          total_removes: 0,
          blocked_collections: [],
        },
        dry_run: true,
        updated_workspace_artifacts: [],
        deleted_workspace_artifact_ids: [],
        skipped_workspace_artifact_ids: [],
        per_collection: [],
      });

      render(
        <WorkspaceProvider>
          <TestConsumer />
        </WorkspaceProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('3');
      });

      await user.click(screen.getByText('Preview Commit'));

      await waitFor(() => {
        expect(previewWorkspaceCommit).toHaveBeenCalledWith('ws-1', { dry_run: true });
      });

      await waitFor(() => {
        expect(screen.getByTestId('preview-count')).toHaveTextContent('2');
      });
    });
  });

  describe('Commit Workspace', () => {
    it('commits via commitCurrentWorkspace() and updates artifacts (debounced)', async () => {
      const user = userEvent.setup();
      const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});

      // Seed a preview so we can assert it clears after publish
      previewWorkspaceCommit.mockResolvedValueOnce({
        workspace_id: 'ws-1',
        plan: {
          artifacts: [],
          collections: [],
          total_artifacts: 1,
          total_adds: 1,
          total_removes: 0,
          blocked_collections: [],
        },
        dry_run: true,
        updated_workspace_artifacts: [],
        deleted_workspace_artifact_ids: [],
        skipped_workspace_artifact_ids: [],
        per_collection: [],
      });

      commitWorkspace.mockResolvedValueOnce({
        workspace_id: 'ws-1',
        plan: {
          artifacts: [],
          collections: [],
          total_artifacts: 1,
          total_adds: 1,
          total_removes: 0,
          blocked_collections: [],
        },
        dry_run: false,
        updated_workspace_artifacts: [
          {
            id: '2',
            workspace_id: 'ws-1',
            state: 'committed',
            context: JSON.stringify({ title: 'Artifact 2' }),
            content: 'Updated content',
            order_key: 'U',
            created_time: '2024-01-01T00:00:00Z',
            modified_time: '2024-01-01T00:00:00Z',
          },
        ],
        deleted_workspace_artifact_ids: [],
        skipped_workspace_artifact_ids: [],
        per_collection: [],
      });

      function CommitConsumer() {
        const workspace = useWorkspace();

        return (
          <div>
            <div data-testid="preview-count">{workspace.commitPreview?.plan.total_artifacts ?? 0}</div>
            <div data-testid="artifact-2-state">{workspace.artifacts.find(c => c.id === '2')?.state}</div>
            <button onClick={() => workspace.fetchCommitPreview()}>Preview Commit</button>
            <button onClick={() => workspace.commitCurrentWorkspace({ artifact_ids: ['2'] })}>Commit</button>
          </div>
        );
      }

      render(
        <WorkspaceProvider>
          <CommitConsumer />
        </WorkspaceProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('artifact-2-state')).toHaveTextContent('draft');
      });

      await user.click(screen.getByText('Preview Commit'));
      await waitFor(() => {
        expect(screen.getByTestId('preview-count')).toHaveTextContent('1');
      });

      // Scope fake timers to the debounce path only. RTL's waitFor relies on
      // real timers for polling, so we switch back before asserting.
      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(screen.getByText('Commit'));

          // Debounce is 300ms; flush it + any queued async work
          vi.advanceTimersByTime(350);
          await vi.runAllTimersAsync();
        });
      } finally {
        vi.useRealTimers();
        logSpy.mockRestore();
      }

      await waitFor(() => expect(commitWorkspace).toHaveBeenCalledWith('ws-1', { artifact_ids: ['2'] }));
      await waitFor(() => expect(screen.getByTestId('artifact-2-state')).toHaveTextContent('committed'));
      expect(screen.getByTestId('preview-count')).toHaveTextContent('0');
    });
  });

  describe('Artifact Selection', () => {
    it('selects all artifacts via selectAllArtifacts()', async () => {
      const user = userEvent.setup();
      
      render(
        <WorkspaceProvider>
          <TestConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('3');
      });
      
      await user.click(screen.getByText('Select All'));
      
      expect(screen.getByTestId('selected-count')).toHaveTextContent('3');
    });

    it('unselects all artifacts via unselectAllArtifacts()', async () => {
      const user = userEvent.setup();
      
      render(
        <WorkspaceProvider>
          <TestConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('3');
      });
      
      // First select all
      await user.click(screen.getByText('Select All'));
      expect(screen.getByTestId('selected-count')).toHaveTextContent('3');
      
      // Then unselect all
      await user.click(screen.getByText('Unselect All'));
      expect(screen.getByTestId('selected-count')).toHaveTextContent('0');
    });
  });

  describe('Update Artifact', () => {
    it('updates artifact state optimistically', async () => {
      updateWorkspaceArtifact.mockResolvedValueOnce({ ...mockArtifacts[0], state: 'draft' });
      
      function UpdateConsumer() {
        const workspace = useWorkspace();
        
        return (
          <div>
            <div data-testid="artifact-1-state">
              {workspace.artifacts.find(c => c.id === '1')?.state}
            </div>
            <button onClick={() => workspace.updateArtifact({ id: '1', state: 'draft' })}>
              Update Artifact
            </button>
          </div>
        );
      }
      
      const user = userEvent.setup();
      
      render(
        <WorkspaceProvider>
          <UpdateConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-1-state')).toHaveTextContent('committed');
      });
      
      await user.click(screen.getByText('Update Artifact'));
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-1-state')).toHaveTextContent('draft');
      });
      
      expect(updateWorkspaceArtifact).toHaveBeenCalledWith('ws-1', '1', { state: 'draft' });
    });
  });

  describe('Remove Artifact', () => {
    it('removes artifact from state after deletion', async () => {
      removeWorkspaceArtifact.mockResolvedValueOnce(undefined);
      
      function DeleteConsumer() {
        const workspace = useWorkspace();
        
        return (
          <div>
            <div data-testid="artifact-count">{workspace.artifacts.length}</div>
            <button onClick={() => workspace.removeArtifact('1')}>
              Delete Artifact
            </button>
          </div>
        );
      }
      
      const user = userEvent.setup();
      
      render(
        <WorkspaceProvider>
          <DeleteConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('3');
      });
      
      await user.click(screen.getByText('Delete Artifact'));
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-count')).toHaveTextContent('2');
      });
      
      expect(removeWorkspaceArtifact).toHaveBeenCalledWith('ws-1', '1');
    });
  });

  describe('Revert Artifact', () => {
    it('reverts artifact to unmodified state', async () => {
      revertWorkspaceArtifact.mockResolvedValueOnce({ ...mockArtifacts[1], state: 'committed' });
      
      function RevertConsumer() {
        const workspace = useWorkspace();
        
        return (
          <div>
            <div data-testid="artifact-2-state">
              {workspace.artifacts.find(c => c.id === '2')?.state}
            </div>
            <button onClick={() => workspace.revertArtifact('2')}>
              Revert Artifact
            </button>
          </div>
        );
      }
      
      const user = userEvent.setup();
      
      render(
        <WorkspaceProvider>
          <RevertConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-2-state')).toHaveTextContent('draft');
      });
      
      await user.click(screen.getByText('Revert Artifact'));
      
      await waitFor(() => {
        expect(screen.getByTestId('artifact-2-state')).toHaveTextContent('committed');
      });
      
      expect(revertWorkspaceArtifact).toHaveBeenCalledWith('ws-1', '2');
    });
  });

  describe('Displayed Artifacts', () => {
    it('provides displayedArtifacts state management', async () => {
      function DisplayConsumer() {
        const workspace = useWorkspace();
        
        return (
          <div>
            <div data-testid="displayed-count">
              {workspace.displayedArtifacts?.length ?? 0}
            </div>
            <button onClick={() => workspace.setDisplayedArtifacts?.([mockArtifacts[0]])}>
              Set Displayed
            </button>
          </div>
        );
      }
      
      const user = userEvent.setup();
      
      render(
        <WorkspaceProvider>
          <DisplayConsumer />
        </WorkspaceProvider>
      );
      
      await waitFor(() => {
        expect(screen.getByTestId('displayed-count')).toHaveTextContent('0');
      });
      
      await user.click(screen.getByText('Set Displayed'));
      
      expect(screen.getByTestId('displayed-count')).toHaveTextContent('1');
    });
  });
});
