// palette.types.ts
import { ReactNode } from 'react';
import { Artifact } from '../workspace/workspace.types';
import { Collection } from '../collections/collection.types';

export interface PaletteProviderProps {
  children: ReactNode;
}

export type PanelKey =
  | 'input'
  | 'resources'
  | 'context'
  | 'prompts'
  | 'instructions'
  | 'tools'
  | 'knowledge'
  | 'options'
  | 'agent'
  | 'targets'
  | 'output';

// Per-panel data - stores actual domain objects
export interface InputPanelData {
  artifacts: Artifact[];
  text: string;
}

export interface ResourcesPanelData {
  artifacts: Artifact[];
  resources: Array<{
    server: string;
    serverName?: string;
    uri: string;
    title?: string;
    contentType?: string;
    resourceKind?: string;
  }>;
}

export interface ContextPanelData {
  artifacts: Artifact[];
}

export interface PromptsPanelData {
  artifacts: Artifact[];
  selectedId?: string;
}

export interface InstructionsPanelData {
  artifacts: Artifact[];
  text: string;
}

export interface ToolsPanelData {
  tools: string[];
}

export interface KnowledgePanelData {
  artifacts: Artifact[];
}

export interface OptionsPanelData {
  config: Record<string, unknown>;
}

export interface AgentPanelData {
  config: Record<string, unknown>;
}

export interface TargetsPanelData {
  collections: Collection[];
}

export interface OutputPanelData {
  artifacts: Artifact[];
}

// Combined type for all panel data
export interface PanelData {
  input: InputPanelData;
  resources: ResourcesPanelData;
  context: ContextPanelData;
  prompts: PromptsPanelData;
  instructions: InstructionsPanelData;
  tools: ToolsPanelData;
  knowledge: KnowledgePanelData;
  options: OptionsPanelData;
  agent: AgentPanelData;
  targets: TargetsPanelData;
  output: OutputPanelData;
}

// PaletteState stores panel data directly
export interface PaletteState {
  // Panel data - uses PanelData type
  panelData: PanelData;
  
  // Runtime state
  panelStatus: Record<PanelKey, PanelStatus>;
  updatePanelStatus: (key: PanelKey, status: PanelStatus) => void;
}

export type StepHandler = (state: PaletteState) => Promise<PaletteState>;
export type PanelStatus = 'never' | 'running' | 'ran' | 'paused';