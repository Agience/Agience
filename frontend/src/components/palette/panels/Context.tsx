import { useCallback, useRef, useState } from 'react';
import { usePalette } from '../../../hooks/usePalette';
import { useWorkspace } from '../../../hooks/useWorkspace';
import CardGrid from '../../common/CardGrid';
import { getDroppedArtifactIds } from '../../../dnd/agienceDrag';

export default function ContextPanel() {
  const { state, updatePanelData } = usePalette();
  const { artifacts } = useWorkspace();
  const [dragDepth, setDragDepth] = useState(0);
  const dropRef = useRef<HTMLDivElement>(null);

  const panelState = state.panelData.context;

  const sourceSummary = {
    input_artifacts: state.panelData.input.artifacts.length,
    input_text_chars: state.panelData.input.text.length,
    bundle_artifacts: state.panelData.resources.artifacts?.length ?? 0,
    bundle_resources: state.panelData.resources.resources?.length ?? 0,
    skill_artifacts: state.panelData.prompts.artifacts.length,
    instruction_artifacts: state.panelData.instructions.artifacts.length,
    instruction_text_chars: state.panelData.instructions.text.length,
    tools_selected: state.panelData.tools.tools.length,
    targets_selected: state.panelData.targets.collections.length,
  };

  const addArtifact = useCallback(
    (id: string) => {
      const artifact = artifacts.find((c) => String(c.id) === id);
      if (!artifact) return;

      updatePanelData('context', (prev) => {
        const existing = prev.artifacts;
        if (existing.some((c) => String(c.id) === String(artifact.id))) return prev;
        return { ...prev, artifacts: [...existing, artifact] };
      });
    },
    [artifacts, updatePanelData]
  );

  const removeArtifact = useCallback(
    (id: string) => {
      updatePanelData('context', (prev) => {
        const existing = prev.artifacts;
        return { ...prev, artifacts: existing.filter((c) => String(c.id) !== id) };
      });
    },
    [updatePanelData]
  );

  const onDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      setDragDepth(0);

      const ids = getDroppedArtifactIds(e.dataTransfer);
      if (ids.length) ids.forEach((id) => addArtifact(id));
    },
    [addArtifact]
  );

  return (
    <div
      ref={dropRef}
      onDrop={onDrop}
      onDragEnter={(e) => {
        e.preventDefault();
        setDragDepth((d) => d + 1);
      }}
      onDragLeave={(e) => {
        e.preventDefault();
        setDragDepth((d) => Math.max(0, d - 1));
      }}
      onDragOver={(e) => e.preventDefault()}
      className={`w-full min-h-[151px] rounded border bg-white resize-y overflow-auto p-2
        ${dragDepth > 0 ? 'ring-2 ring-blue-500' : 'ring-1 ring-gray-300'}
      `}
    >
      <div className="mb-2 rounded border bg-gray-50 p-2">
        <div className="text-xs font-semibold text-gray-700">Resolved summary</div>
        <div className="mt-1 text-xs text-gray-600 grid grid-cols-2 gap-x-3 gap-y-1">
          <div>Sources: {sourceSummary.input_artifacts} artifacts</div>
          <div>Sources text: {sourceSummary.input_text_chars} chars</div>
          <div>Bundles: {sourceSummary.bundle_artifacts} artifacts</div>
          <div>MCP resources: {sourceSummary.bundle_resources}</div>
          <div>Skills: {sourceSummary.skill_artifacts} artifacts</div>
          <div>Tools selected: {sourceSummary.tools_selected}</div>
          <div>Instruction artifacts: {sourceSummary.instruction_artifacts}</div>
          <div>Instruction text: {sourceSummary.instruction_text_chars} chars</div>
          <div>Targets selected: {sourceSummary.targets_selected}</div>
        </div>

        {(state.panelData.resources.resources?.length ?? 0) > 0 && (
          <div className="mt-2">
            <div className="text-[11px] font-semibold text-gray-600">Selected MCP resources</div>
            <div className="mt-1 max-h-20 overflow-auto rounded bg-white border px-2 py-1 text-[11px] text-gray-700">
              {state.panelData.resources.resources.map((r) => (
                <div key={`${r.server}::${r.uri}`} className="truncate" title={`${r.serverName || r.server} • ${r.uri}`}>
                  {r.serverName || r.server} • {r.uri}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      <CardGrid
        artifacts={panelState.artifacts}
        selectable={false}
        draggable={false}
        editable={false}
        onRemove={(artifact) => artifact.id && removeArtifact(String(artifact.id))}
      />
    </div>
  );
}
