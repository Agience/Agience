/**
 * McpAppHost — MCP Apps-compliant iframe host component.
 *
 * Renders a `ui://` resource (HTML View) inside a sandboxed iframe and
 * implements the host side of the MCP Apps JSON-RPC protocol over postMessage.
 *
 * Spec: https://modelcontextprotocol.io/extensions/apps/overview (SEP-1865)
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { Artifact } from '@/context/workspace/workspace.types';
import { useWorkspace } from '@/hooks/useWorkspace';
import { useWorkspaces } from '@/hooks/useWorkspaces';
import { useArtifactContent } from '@/hooks/useArtifactContent';
import { subscribeEvents } from '@/api/events';
import { proxyToolCall } from '@/api/mcp';
import { buildHostContext } from './hostContext';
import { buildCspMetaTag } from './csp';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface McpAppHostProps {
  /** The artifact this view is rendering. */
  artifact: Artifact;
  /** The HTML content to render (fetched from a `ui://` resource). */
  html: string;
  /** The MCP server that owns this view (from presentation.json resource_server). */
  resourceServer?: string;
  /** Optional CSP metadata from the `_meta.ui.csp` resource field. */
  csp?: CspDomains;
  /** Callback when the view requests opening an artifact. */
  onOpenArtifact?: (artifact: Artifact) => void;
  /** Callback when the view requests opening a collection. */
  onOpenCollection?: (collectionId: string) => void;
}

export interface CspDomains {
  connectDomains?: string[];
  resourceDomains?: string[];
  frameDomains?: string[];
  baseUriDomains?: string[];
}

interface JsonRpcRequest {
  jsonrpc: '2.0';
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

interface JsonRpcNotification {
  jsonrpc: '2.0';
  method: string;
  params?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function McpAppHost({
  artifact,
  html,
  resourceServer: _resourceServer,
  csp,
  onOpenArtifact,
  onOpenCollection,
}: McpAppHostProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [initialized, setInitialized] = useState(false);
  const { updateArtifact, createArtifact } = useWorkspace();
  const { activeWorkspaceId } = useWorkspaces();
  const { content: resolvedContent } = useArtifactContent(artifact);

  // Assemble the HTML with CSP meta tag injected
  const srcdocHtml = buildCspMetaTag(csp) + html;

  // ------ postMessage handler ------
  const handleMessage = useCallback(
    (event: MessageEvent) => {
      const iframe = iframeRef.current;
      if (!iframe || event.source !== iframe.contentWindow) return;

      const data = event.data;
      if (!data || data.jsonrpc !== '2.0') return;

      // JSON-RPC request (has `id` — expects response)
      if (typeof data.id === 'number') {
        handleRequest(data as JsonRpcRequest);
      }
      // JSON-RPC notification (no `id`)
      else if (typeof data.method === 'string') {
        handleNotification(data as JsonRpcNotification);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [artifact, updateArtifact, createArtifact, activeWorkspaceId, onOpenArtifact, onOpenCollection],
  );

  function sendResponse(id: number, result: unknown) {
    iframeRef.current?.contentWindow?.postMessage(
      { jsonrpc: '2.0', id, result },
      '*',
    );
  }

  function sendError(id: number, code: number, message: string) {
    iframeRef.current?.contentWindow?.postMessage(
      { jsonrpc: '2.0', id, error: { code, message } },
      '*',
    );
  }

  function sendNotification(method: string, params: Record<string, unknown>) {
    iframeRef.current?.contentWindow?.postMessage(
      { jsonrpc: '2.0', method, params },
      '*',
    );
  }

  function handleRequest(req: JsonRpcRequest) {
    switch (req.method) {
      case 'ui/initialize': {
        const hostContext = buildHostContext({
          artifact,
          workspaceId: activeWorkspaceId ?? '',
        });
        sendResponse(req.id, {
          protocolVersion: '2026-01-26',
          hostInfo: { name: 'agience', version: '0.1.0' },
          hostCapabilities: {
            openLinks: {},
            serverTools: {},
            logging: {},
          },
          hostContext,
        });
        setInitialized(true);

        // After initialize, send the artifact data as tool-result
        sendNotification('ui/notifications/tool-result', {
          content: [
            {
              type: 'text',
              text: JSON.stringify({
                artifact_id: artifact.id,
                context: typeof artifact.context === 'string'
                  ? JSON.parse(artifact.context)
                  : artifact.context,
                content: resolvedContent,
              }),
            },
          ],
        });
        break;
      }

      case 'tools/call': {
        handleToolCall(req);
        break;
      }

      case 'ui/open-link': {
        const url = (req.params as { url?: string })?.url;
        if (url) {
          // Check if it's an internal artifact/collection link
          if (url.startsWith('agience://artifact/') && onOpenArtifact) {
            // Internal navigation — not implemented yet, respond OK
          } else if (url.startsWith('agience://collection/') && onOpenCollection) {
            const collectionId = url.replace('agience://collection/', '');
            onOpenCollection(collectionId);
          } else {
            window.open(url, '_blank', 'noopener,noreferrer');
          }
          sendResponse(req.id, {});
        } else {
          sendError(req.id, -32000, 'Invalid URL');
        }
        break;
      }

      case 'ui/message':
      case 'ui/update-model-context':
      case 'ui/request-display-mode':
        // Acknowledge but don't act on these yet
        sendResponse(req.id, {});
        break;

      case 'ping':
        sendResponse(req.id, {});
        break;

      case 'ui/resource-teardown':
        sendResponse(req.id, {});
        break;

      default:
        sendError(req.id, -32601, `Method not found: ${req.method}`);
    }
  }

  async function handleToolCall(req: JsonRpcRequest) {
    const params = req.params as { name?: string; arguments?: Record<string, unknown> } | undefined;
    const toolName = params?.name;
    const toolArgs = params?.arguments ?? {};

    if (!toolName) {
      sendError(req.id, -32602, 'Missing tool name');
      return;
    }

    // Handle platform tools that the host provides directly
    switch (toolName) {
      case 'update_artifact': {
        try {
          const id = (toolArgs.id as string) ?? String(artifact.id);
          const patch = toolArgs.patch as Record<string, unknown> | undefined;
          if (patch) {
            await updateArtifact({ id, ...(patch as Record<string, unknown>) } as Parameters<typeof updateArtifact>[0]);
          }
          sendResponse(req.id, { content: [{ type: 'text', text: 'OK' }] });
        } catch (e) {
          sendError(req.id, -32000, `Update failed: ${e}`);
        }
        return;
      }

      case 'create_artifact': {
        try {
          const newArtifact = await createArtifact((toolArgs as unknown) as Parameters<typeof createArtifact>[0]);
          sendResponse(req.id, {
            content: [{ type: 'text', text: JSON.stringify(newArtifact) }],
          });
        } catch (e) {
          sendError(req.id, -32000, `Create failed: ${e}`);
        }
        return;
      }

      case 'create_api_key': {
        try {
          const { createAPIKey } = await import('@/api/apiKeys');
          const created = await createAPIKey((toolArgs as unknown) as Parameters<typeof createAPIKey>[0]);
          sendResponse(req.id, {
            content: [{ type: 'text', text: JSON.stringify(created) }],
          });
        } catch (e) {
          sendError(req.id, -32000, `Create API key failed: ${e}`);
        }
        return;
      }

      case 'delete_api_key': {
        try {
          const { deleteAPIKey } = await import('@/api/apiKeys');
          const id = toolArgs.id as string;
          await deleteAPIKey(id);
          sendResponse(req.id, { content: [{ type: 'text', text: 'OK' }] });
        } catch (e) {
          sendError(req.id, -32000, `Delete API key failed: ${e}`);
        }
        return;
      }

      default: {
        // Route through the artifact itself — POST /artifacts/{artifact.id}/invoke.
        // The artifact's type.json declares how to dispatch (server + tool).
        // The frontend never needs to know which server owns the tool.
        try {
          const result = await proxyToolCall(
            toolName,
            toolArgs,
            artifact.id,
            activeWorkspaceId || undefined,
          );
          sendResponse(req.id, result);
        } catch (e) {
          sendError(req.id, -32000, `Tool call failed: ${e}`);
        }
      }
    }
  }

  function handleNotification(notif: JsonRpcNotification) {
    switch (notif.method) {
      case 'ui/notifications/initialized':
        // View confirms initialization complete
        break;

      case 'ui/notifications/size-changed': {
        const { height } = (notif.params ?? {}) as { height?: number };
        if (height && iframeRef.current) {
          iframeRef.current.style.height = `${height}px`;
        }
        break;
      }

      case 'notifications/message':
        // Logging from the view
        console.debug('[McpApp]', notif.params);
        break;

      default:
        break;
    }
  }

  // ------ Lifecycle ------
  useEffect(() => {
    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [handleMessage]);

  // Send host context updates when theme/dimensions change
  useEffect(() => {
    if (!initialized) return;
    const hostContext = buildHostContext({
      artifact,
      workspaceId: activeWorkspaceId ?? '',
    });
    sendNotification('ui/notifications/host-context-changed', hostContext as Record<string, unknown>);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifact, activeWorkspaceId]);

  // Relay chat streaming events from the events WebSocket to the iframe
  useEffect(() => {
    if (!initialized) return;

    const unsub = subscribeEvents(
      {
        container_id: activeWorkspaceId ?? undefined,
        artifact_id: artifact.id,
        event_names: ['artifact.chat.*'],
      },
      (evt) => {
        const payload = (evt.payload ?? {}) as Record<string, unknown>;
        if (evt.event === 'artifact.chat.delta') {
          sendNotification('ui/notifications/chat-delta', payload);
        } else if (evt.event === 'artifact.chat.status') {
          sendNotification('ui/notifications/chat-status', payload);
        }
      },
    );

    return unsub;
  }, [initialized, artifact.id, activeWorkspaceId]);

  return (
    <iframe
      ref={iframeRef}
      srcDoc={srcdocHtml}
      sandbox="allow-scripts"
      style={{ width: '100%', height: '100%', border: 'none' }}
      title="MCP App View"
    />
  );
}
