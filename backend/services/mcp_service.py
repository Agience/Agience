"""MCP service  -- artifact-native implementation.

MCP server configurations are stored as ``application/vnd.agience.mcp-server+json``
artifacts inside workspaces.  The old preferences-blob registry has been removed; all
CRUD happens through the standard artifact API.

The special ``agience-core`` server is always available (built-in).  For every
other server the client passes a *artifact_id* that points to an mcp-server artifact in
the active workspace.  Workspace artifacts are stored in ArangoDB.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

from mcp_client.adapter import fetch_server_info
from mcp_client.contracts import MCPServerInfo, MCPServerConfig
from mcp_client.local import create_agience_core_client

from arango.database import StandardDatabase

from . import collection_service as col_svc
from . import desktop_host_relay_service
from . import auth_service
from . import server_registry
from db import arango as arango_db_module
from db.arango import (
    get_active_collection_ids_for_user as db_get_active_collection_ids,
    list_collection_artifacts as _db_list_collection_artifacts,
)

logger = logging.getLogger(__name__)


def _dict_to_ns(d: dict):
    """Wrap an arango_ws dict as a SimpleNamespace so attribute access works."""
    from types import SimpleNamespace
    return SimpleNamespace(**d)


# ---------------------------------------------------------------------------
# Type-registry-driven config parsing (C2 residual resolution)
#
# Core MUST NOT parse `vnd.agience.mcp-server+json` artifact.context directly.
# The parser is declared by the type definition itself and resolved at runtime
# through the type registry. This keeps Core type-blind per P2.
# ---------------------------------------------------------------------------


def _extract_content_type(artifact) -> Optional[str]:
    """Read `content_type` from an artifact's context payload.

    Accepts either a namespace/dict artifact with a `context` attribute or
    a raw dict. Returns None if the content type cannot be determined.
    """
    ctx = getattr(artifact, "context", None)
    if ctx is None and isinstance(artifact, dict):
        ctx = artifact.get("context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            return None
    if not isinstance(ctx, dict):
        return None
    content_type = ctx.get("content_type")
    return content_type if isinstance(content_type, str) and content_type else None


def _parse_mcp_server_artifact(artifact) -> Optional[MCPServerConfig]:
    """Parse an MCP server artifact via the type-declared handler.

    Resolves `handlers.mcp_server_config` from the artifact's MIME through
    `types_service.resolve_capability_target`, then loads the declared native
    target via `handler_registry.get_native_target`. Returns None if the type
    does not declare a parser or the parser cannot be resolved/invoked.

    Core never imports the parser directly — this indirection is what moves
    the schema knowledge out of Core and into the type definition owned by
    the server that defines the type (Nexus, for `vnd.agience.mcp-server+json`).
    """
    content_type = _extract_content_type(artifact)
    if not content_type:
        return None

    from services.types_service import resolve_capability_target
    from services.handler_registry import get_native_target

    target_name = resolve_capability_target(content_type, "mcp_server_config")
    if not target_name:
        return None

    parser = get_native_target(target_name)
    if parser is None:
        logger.warning(
            "Type '%s' declares mcp_server_config handler '%s' but target is not resolvable",
            content_type, target_name,
        )
        return None

    try:
        return parser(artifact)
    except Exception:
        logger.exception("mcp_server_config parser failed for content type %s", content_type)
        return None


class AuthExpiredError(Exception):
    """Raised when an MCP server's auth token has expired and user must re-authenticate."""
    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_server_config(
    db: StandardDatabase,
    user_id: str,
    workspace_id: Optional[str],
    server_id: str,
) -> Optional[MCPServerConfig]:
    """Resolve an MCP server config.

    Resolution order:
      1. ``agience-core`` -- returns None (caller handles built-in locally).
      2. Manifest-registered builtin -- config from ``server_registry``.
      3. Workspace artifact -- if *workspace_id* is provided, looks up the artifact
         in that workspace (ArangoDB).
      4. Collection artifact -- resolves globally via ownership or grants (ArangoDB).

    Raises :class:`ValueError` if the server cannot be resolved.
    """
    if server_id == "agience-core":
        return None  # caller handles built-in separately

    # Check manifest builtins — by name or by bootstrap UUID.
    if server_registry.get_entry(server_id) is not None:
        return server_registry.build_http_config(server_id)
    if server_registry.is_builtin_id(server_id):
        return server_registry.build_http_config_by_id(server_id)

    # Try workspace artifact first (if workspace context is available)
    if workspace_id:
        ws = arango_db_module.get_collection_by_id(db, workspace_id)
        if ws and ws.created_by == user_id:
            artifact = arango_db_module.get_artifact(db, server_id)
            if artifact and artifact.collection_id == workspace_id:
                config = _parse_mcp_server_artifact(artifact)
                if config:
                    return config
                raise ValueError(f"Artifact '{server_id}' is not a valid MCP server")

    # Resolve globally via collection grants (ArangoDB)
    try:
        collection_artifact = col_svc.get_artifact_by_id_for_user(
            db,
            user_id=user_id,
            grant_key=None,
            artifact_root_id=server_id,
        )
        if collection_artifact:
            config = _parse_mcp_server_artifact(collection_artifact)
            if config:
                return config
    except Exception:
        pass  # fall through to error

    raise ValueError(f"MCP server '{server_id}' not found or access denied")


def _resolve_auth_headers(
    db: StandardDatabase,
    user_id: str,
    config: MCPServerConfig,
    workspace_id: Optional[str] = None,
) -> Dict[str, str]:
    """Resolve runtime auth headers for an MCP server config.

    Auth is transport-level: the result is injected as HTTP headers when
    creating the MCP client connection — once per call, not per tool.

    Three strategies (all local — no MCP round-trips):
      oauth2   — fetch stored bearer token via secrets_service; check expiry.
                 Token was stored by Seraph during the OAuth callback flow.
      api_key  — decrypt stored secret and inject as a Bearer (or named) header.
      static   — literal header value (dev/testing only).

    Raises :class:`AuthExpiredError` when an oauth2 token has expired and the
    user must re-authenticate through the OAuth callback flow.
    """
    if not config.auth:
        return {}

    auth = config.auth
    from services import secrets_service

    if auth.type == "oauth2" and auth.authorizer_id:
        try:
            # Find the stored bearer token for this authorizer
            secrets = secrets_service.list_secrets(
                db, user_id,
                authorizer_id=auth.authorizer_id,
                secret_type="bearer_token",
            )
            if not secrets:
                # No bearer_token cached yet. Check whether a refresh_token exists —
                # if so, the caller should invoke Seraph's provide_access_token tool
                # (which will exchange, cache a fresh bearer_token, and retry).
                refresh_secrets = secrets_service.list_secrets(
                    db, user_id,
                    authorizer_id=auth.authorizer_id,
                    secret_type="oauth_refresh_token",
                )
                if not refresh_secrets:
                    raise AuthExpiredError(
                        f"No stored credentials for server '{config.id}'. "
                        "Connect the account via the Authorizer before using this server."
                    )
                # Refresh token exists — instruct the caller to exchange it.
                raise AuthExpiredError(
                    f"No cached access token for server '{config.id}'. "
                    "Call Seraph's provide_access_token to obtain and cache a fresh token, "
                    "then retry."
                )

            secret = secrets[0]

            # Check expiry before decrypting
            expires_at = getattr(secret, "expires_at", "") or ""
            if expires_at:
                from datetime import datetime, timezone
                try:
                    exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    if exp_dt < datetime.now(timezone.utc):
                        raise AuthExpiredError(
                            f"Bearer token for server {config.id} has expired. "
                            "Re-authenticate to continue."
                        )
                except AuthExpiredError:
                    raise
                except Exception:
                    pass  # unparseable expiry — proceed and let the server reject if needed

            token = secrets_service.decrypt_value(secret.encrypted_value)
            if token:
                return {"Authorization": f"Bearer {token}"}

        except AuthExpiredError:
            raise
        except Exception:
            logger.exception("Failed to resolve oauth2 auth for server %s", config.id)

        return {}

    elif auth.type == "api_key" and auth.secret_id:
        try:
            secrets = secrets_service.list_secrets(db, user_id, secret_id=auth.secret_id)
            if secrets:
                token = secrets_service.decrypt_value(secrets[0].encrypted_value)
                if token:
                    header = auth.header or "Authorization"
                    value = f"Bearer {token}" if header.lower() == "authorization" else token
                    return {header: value}
        except Exception:
            logger.exception("Failed to resolve api_key auth for server %s", config.id)
        return {}

    elif auth.type == "static" and auth.header and auth.value:
        return {auth.header: auth.value}

    return {}


def _get_server_config_from_collections(
    arango_db: StandardDatabase,
    user_id: str,
    server_id: str,
) -> Optional[MCPServerConfig]:
    """Try to resolve an MCP server config from user-accessible collections."""
    collection_ids = db_get_active_collection_ids(arango_db, user_id)
    for col_id in collection_ids:
        try:
            artifacts = [__import__("entities.artifact", fromlist=["Artifact"]).Artifact.from_dict(r) for r in _db_list_collection_artifacts(arango_db, col_id)]
        except Exception:
            continue
        for artifact in artifacts:
            if artifact.root_id != server_id and artifact.id != server_id:
                continue
            config = _parse_mcp_server_artifact(artifact)
            if config:
                return config
    return None


def _collect_mcp_servers_from_collections(
    arango_db: StandardDatabase,
    user_id: str,
) -> List[MCPServerInfo]:
    """Gather MCP server artifacts from all user-accessible collections."""
    servers: List[MCPServerInfo] = []
    seen_ids: set = set()
    collection_ids = db_get_active_collection_ids(arango_db, user_id)

    for col_id in collection_ids:
        try:
            artifacts = [__import__("entities.artifact", fromlist=["Artifact"]).Artifact.from_dict(r) for r in _db_list_collection_artifacts(arango_db, col_id)]
        except Exception:
            continue
        for artifact in artifacts:
            # Gate on type-declared handler: if the artifact's MIME does not
            # declare an `mcp_server_config` handler, it is not an MCP server
            # artifact and is skipped. The parser itself checks this, but
            # inspecting MIME first lets us short-circuit dedup work.
            content_type = _extract_content_type(artifact)
            if not content_type:
                continue
            from services.types_service import resolve_capability_target
            if not resolve_capability_target(content_type, "mcp_server_config"):
                continue
            art_id = artifact.root_id or artifact.id
            if art_id in seen_ids:
                continue
            seen_ids.add(art_id)

            config = _parse_mcp_server_artifact(artifact)
            if not config:
                servers.append(MCPServerInfo(server=art_id, status="error", message="Invalid server config"))
                continue
            try:
                info = fetch_server_info(config)
                servers.append(info)
            except Exception as e:
                servers.append(MCPServerInfo(server=art_id, status="error", message=str(e)))

    return servers


def list_servers_from_collection(
    db: StandardDatabase,
    collection_id: str,
) -> List[MCPServerInfo]:
    """Return live info for MCP servers in a specific collection.

    Reads all artifacts from *collection_id*, filters for those whose
    content type declares an ``mcp_server_config`` handler, parses their
    configs, and fetches live server info.  Used by ``discover_tools``
    when a workspace has a ``tools`` binding.
    """
    from entities.artifact import Artifact

    servers: List[MCPServerInfo] = []
    try:
        artifacts = [
            Artifact.from_dict(r)
            for r in _db_list_collection_artifacts(db, collection_id)
        ]
    except Exception:
        logger.exception("list_servers_from_collection(%s): artifact load failed", collection_id)
        return servers

    for artifact in artifacts:
        content_type = _extract_content_type(artifact)
        if not content_type:
            continue
        from services.types_service import resolve_capability_target
        if not resolve_capability_target(content_type, "mcp_server_config"):
            continue

        art_id = artifact.root_id or artifact.id
        config = _parse_mcp_server_artifact(artifact)
        if not config:
            servers.append(MCPServerInfo(server=art_id, status="error", message="Invalid server config"))
            continue
        try:
            info = fetch_server_info(config)
            servers.append(info)
        except Exception as e:
            servers.append(MCPServerInfo(server=art_id, status="error", message=str(e)))

    return servers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_servers_for_workspace(db: StandardDatabase, user_id: str, workspace_id: str) -> List[MCPServerInfo]:
    """Return live info for all MCP servers in a workspace.

    MCP server configs are stored as ``application/vnd.agience.mcp-server+json``
    artifacts in the workspace.  The built-in ``agience-core`` server is always
    prepended.
    """
    servers: List[MCPServerInfo] = []

    # agience-core is always available (built-in, no artifact required)
    try:
        client = create_agience_core_client(db, db, user_id)
        try:
            servers.append(MCPServerInfo(
                server="agience-core",
                name="Agience Core",
                tools=client.list_tools(),
                resources=client.list_resources(),
                prompts=client.list_prompts(),
                status="ok",
            ))
        finally:
            client.close()
    except Exception as e:
        servers.append(MCPServerInfo(
            server="agience-core",
            name="Agience Core",
            status="error",
            message=f"Failed to load Agience Core: {e}",
        ))

    desktop_host = desktop_host_relay_service.get_desktop_host_server_info(user_id)
    if desktop_host is not None:
        servers.append(MCPServerInfo(
            server="desktop-host",
            name=desktop_host.get("display_name") or desktop_host.get("device_id") or "Desktop Host",
            tools=desktop_host_relay_service.get_desktop_host_tools(),
            status="ok",
            message=desktop_host.get("display_name") or desktop_host.get("device_id") or "Connected",
        ))
        servers.extend(desktop_host_relay_service.get_local_mcp_server_infos(user_id))

    # External servers  -- read from mcp-server artifacts in the workspace
    ws = arango_db_module.get_collection_by_id(db, workspace_id)
    if not ws or ws.created_by != user_id:
        return servers

    from entities.artifact import Artifact
    for artifact_dict in arango_db_module.list_collection_artifacts(db, workspace_id):
        artifact = Artifact.from_dict(artifact_dict)
        config = _parse_mcp_server_artifact(artifact)
        if not config:
            continue

        try:
            info = fetch_server_info(config)
            servers.append(info)
        except Exception as e:
            servers.append(MCPServerInfo(server=artifact.id or "", status="error", message=str(e)))

    # Also include MCP servers from user-accessible collections
    try:
        collection_servers = _collect_mcp_servers_from_collections(db, user_id)
        existing_ids = {s.server for s in servers}
        for cs in collection_servers:
            if cs.server not in existing_ids:
                servers.append(cs)
    except Exception:
        logger.exception("Failed to load MCP servers from collections for user %s", user_id)

    return servers


def list_all_servers_for_user(db: StandardDatabase, user_id: str) -> List[MCPServerInfo]:
    """Return live info for all MCP servers accessible to a user.

    Aggregates: built-in servers + desktop host + collection-committed servers.
    No workspace binding required.
    """
    servers: List[MCPServerInfo] = []

    # agience-core built-in
    try:
        client = create_agience_core_client(db, db, user_id)
        try:
            servers.append(MCPServerInfo(
                server="agience-core",
                name="Agience Core",
                tools=client.list_tools(),
                resources=client.list_resources(),
                prompts=client.list_prompts(),
                status="ok",
            ))
        finally:
            client.close()
    except Exception as e:
        servers.append(MCPServerInfo(
            server="agience-core",
            name="Agience Core",
            status="error",
            message=f"Failed to load Agience Core: {e}",
        ))

    # Desktop host
    desktop_host = desktop_host_relay_service.get_desktop_host_server_info(user_id)
    if desktop_host is not None:
        servers.append(MCPServerInfo(
            server="desktop-host",
            name=desktop_host.get("display_name") or desktop_host.get("device_id") or "Desktop Host",
            tools=desktop_host_relay_service.get_desktop_host_tools(),
            status="ok",
            message=desktop_host.get("display_name") or desktop_host.get("device_id") or "Connected",
        ))
        servers.extend(desktop_host_relay_service.get_local_mcp_server_infos(user_id))

    # Built-in persona servers
    for entry in server_registry.all_entries():
        try:
            config = server_registry.build_http_config(entry.name)
            info = fetch_server_info(config)
            servers.append(info)
        except Exception as e:
            server_id = server_registry.get_id(entry.name) or entry.name
            servers.append(MCPServerInfo(server=server_id, name=entry.title, status="error", message=str(e)))

    # Collection-committed servers
    try:
        collection_servers = _collect_mcp_servers_from_collections(db, user_id)
        existing_ids = {s.server for s in servers}
        for cs in collection_servers:
            if cs.server not in existing_ids:
                servers.append(cs)
    except Exception:
        logger.exception("Failed to load MCP servers from collections for user %s", user_id)

    return servers


def import_resources_as_artifacts(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    server_artifact_id: str,
    resources: List[Dict],
) -> List[str]:
    """Create workspace artifacts for selected MCP resources.

    ``server_artifact_id`` is the artifact ID of an mcp-server artifact (or ``"agience-core"``).
    Returns list of created artifact IDs.
    """

    ws = arango_db_module.get_collection_by_id(db, workspace_id)
    if not ws or ws.created_by != user_id:
        raise ValueError("Workspace not found")

    from services.workspace_service import create_workspace_artifacts_bulk

    items: List[dict] = []
    for r in resources or []:
        kind = r.get("kind") or "text"
        uri = r.get("uri")
        text = r.get("text") or ""
        title = r.get("title") or uri or kind

        ctx = {
            "content_type": "application/vnd.agience.resource+json",
            "content_source": "mcp-resource",
            "server": server_artifact_id,
            "kind": kind,
            "uri": uri,
            "title": title,
            "props": r.get("props") or {},
        }
        content = text if isinstance(text, str) else json.dumps(text)
        items.append({"context": json.dumps(ctx), "content": content})

    created = create_workspace_artifacts_bulk(db, user_id, workspace_id, items)
    return [a.id for a in created if a.id]


# ---------------------------------------------------------------------------
# Phase 7 — Dispatcher native targets
#
# Thin wrappers that adapt the existing service functions to the
# `(artifact, body, ctx)` signature expected by handler_registry.NativeHandler.
# These are the targets referenced from vnd.agience.mcp-server+json's
# operations.resources_read and operations.resources_import blocks.
#
# Custom operations on MCP server artifacts route through
# POST /artifacts/{server_id}/op/{op_name}, which hits the operation
# dispatcher, which looks up the native target by dotted name and calls it.
# ---------------------------------------------------------------------------


def dispatch_resources_read(artifact: Dict, body: Dict, ctx) -> Dict:
    """Native dispatcher target for `operations.resources_read`.

    Reads an MCP resource from the server represented by `artifact`. The
    target URI and optional workspace scope come from the request body.
    """
    if not isinstance(body, dict):
        raise ValueError("resources_read requires a JSON object body")

    uri = body.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("resources_read requires body.uri (string)")

    # root_id is the stable cross-version identifier; _key changes on every edit.
    # platform_topology and _get_server_config both key off root_id.
    server_artifact_id = artifact.get("root_id") or artifact.get("_key") or artifact.get("id")
    if not server_artifact_id:
        raise ValueError("Cannot resolve server artifact id from dispatch target")

    workspace_id = body.get("workspace_id")
    return read_resource(
        db=ctx.arango_db,
        user_id=ctx.user_id,
        server_artifact_id=str(server_artifact_id),
        uri=uri.strip(),
        workspace_id=workspace_id if isinstance(workspace_id, str) else None,
    )


def dispatch_resources_import(artifact: Dict, body: Dict, ctx) -> Dict:
    """Native dispatcher target for `operations.resources_import`.

    Materializes a list of MCP resources as workspace artifacts. The
    `workspace_id` and `resources` list come from the request body.
    Returns the list of created artifact IDs.
    """
    if not isinstance(body, dict):
        raise ValueError("resources_import requires a JSON object body")

    workspace_id = body.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("resources_import requires body.workspace_id (string)")

    resources = body.get("resources")
    if not isinstance(resources, list):
        raise ValueError("resources_import requires body.resources (array)")

    # root_id is the stable cross-version identifier; _key changes on every edit.
    # platform_topology and _get_server_config both key off root_id.
    server_artifact_id = artifact.get("root_id") or artifact.get("_key") or artifact.get("id")
    if not server_artifact_id:
        raise ValueError("Cannot resolve server artifact id from dispatch target")

    created_ids = import_resources_as_artifacts(
        db=ctx.arango_db,
        user_id=ctx.user_id,
        workspace_id=workspace_id.strip(),
        server_artifact_id=str(server_artifact_id),
        resources=resources,
    )
    return {"created_artifact_ids": created_ids, "count": len(created_ids)}


def invoke_tool(
    db: StandardDatabase,
    user_id: str,
    server_artifact_id: str,
    tool_name: str,
    arguments: Dict,
    workspace_id: Optional[str] = None,
) -> Dict:
    """Invoke an MCP tool.

    Routes based on Identity + Server + Host:
      - Identity: *user_id* (from JWT/API key)
      - Server: *server_artifact_id* -- server name, bootstrap UUID, or artifact UUID
      - Host: resolved from server config (local, HTTP, desktop relay)

    For built-in persona servers a delegation JWT is issued (RFC 8693 style:
    ``sub`` = user, ``act.sub`` = server) and injected as the transport
    Authorization header.  The persona server's middleware captures it so that
    the user identity flows at the protocol level --- never as a tool argument.

    Raises :class:`ValueError` if the server cannot be resolved.
    """
    from mcp_client.adapter import create_client

    if server_artifact_id == "agience-core":
        client = create_agience_core_client(db, db, user_id)
        try:
            return client.call_tool(tool_name, arguments)
        finally:
            client.close()

    if server_artifact_id == "desktop-host" or server_artifact_id.startswith("local-mcp:"):
        return desktop_host_relay_service.relay_manager.invoke_tool_for_user_sync(
            user_id=user_id,
            workspace_id=workspace_id,
            server_id=server_artifact_id,
            tool_name=tool_name,
            arguments=arguments,
        )

    # Check manifest-registered builtins — accepts name or bootstrap UUID.
    builtin_entry = server_registry.get_entry(server_artifact_id)
    if builtin_entry is None:
        name = server_registry.get_name_by_id(server_artifact_id)
        if name is not None:
            builtin_entry = server_registry.get_entry(name)
    if builtin_entry is not None:
        builtin_config = server_registry.build_http_config(builtin_entry.name)
        if user_id:
            delegation = auth_service.issue_delegation_token(builtin_entry.client_id, user_id)
            builtin_config.runtime_headers = {"Authorization": f"Bearer {delegation}"}
        client = create_client(builtin_config)
        try:
            return client.call_tool(tool_name, arguments)
        finally:
            client.close()

    # Try workspace resolution first, then fall back to collections
    config = None
    if workspace_id is not None:
        try:
            config = _get_server_config(db, user_id, workspace_id, server_artifact_id)
        except ValueError:
            pass  # fall through to collection lookup

    if config is None:
        config = _get_server_config_from_collections(db, user_id, server_artifact_id)

    if config is None:
        raise ValueError(
            f"MCP server '{server_artifact_id}' not found in workspace or accessible collections"
        )

    # Resolve runtime auth headers
    auth_headers = _resolve_auth_headers(db, user_id, config, workspace_id)
    if auth_headers:
        config.runtime_headers = auth_headers

    client = create_client(config)
    try:
        return client.call_tool(tool_name, arguments)
    finally:
        client.close()


def read_resource(
    db: StandardDatabase,
    user_id: str,
    server_artifact_id: str,
    uri: str,
    workspace_id: Optional[str] = None,
) -> Dict:
    """Read an MCP resource.

    Routes based on Identity + Server + Host.  See :func:`invoke_tool` for
    resolution details.
    """
    from mcp_client.adapter import create_client

    if server_artifact_id == "agience-core":
        client = create_agience_core_client(db, db, user_id)
        try:
            return client.read_resource(uri)
        finally:
            client.close()

    config = _get_server_config(db, user_id, workspace_id, server_artifact_id)

    # Resolve runtime auth headers
    auth_headers = _resolve_auth_headers(db, user_id, config, workspace_id)
    if auth_headers:
        config.runtime_headers = auth_headers

    client = create_client(config)
    try:
        return client.read_resource(uri)
    finally:
        client.close()
