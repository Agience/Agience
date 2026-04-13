"""Task agent: complete_authorizer_oauth

Called from the frontend OAuth callback page after the user authorizes
with an upstream provider (e.g. Google).  Exchanges the authorization code
for tokens via the Seraph MCP server and stores the refresh token.

Invoked via POST /agents/invoke with:
    agent: "complete_authorizer_oauth"
    params:
        workspace_id:        Workspace containing the Authorizer artifact
        authorizer_artifact_id: Artifact ID of the Authorizer
        authorization_code:  OAuth authorization code from callback
        code_verifier:       PKCE code verifier
        redirect_uri:        The redirect URI used in the original request
"""
from __future__ import annotations

import logging

from arango.database import StandardDatabase

from services import mcp_service, workspace_service
from services.auth_service import create_jwt_token
from core.dependencies import get_arango_db as _get_arango_db

logger = logging.getLogger(__name__)


def complete_authorizer_oauth(
    *,
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    authorizer_artifact_id: str,
    authorization_code: str,
    code_verifier: str,
    redirect_uri: str,
    **_kwargs,
):
    """Exchange an OAuth authorization code via Seraph and store the refresh token."""

    # 1. Fetch the Authorizer artifact to get its content (config JSON)
    artifact = workspace_service.get_workspace_artifact(
        db, user_id, workspace_id, authorizer_artifact_id
    )
    authorizer_config = artifact.content or "{}"
    if not artifact.content:
        try:
            import json as _json
            ctx = _json.loads(artifact.context or "{}")
            ck = ctx.get("content_key")
            if ck:
                from services.content_service import get_text_direct
                authorizer_config = get_text_direct(ck) or "{}"
        except Exception:
            authorizer_config = "{}"

    # 2. Create a short-lived JWT so Seraph can call GET /secrets on behalf of this user
    user_bearer_token = create_jwt_token(
        {"sub": user_id},
        expires_hours=1,
    )

    # 3. Call Seraph's complete_authorizer_oauth tool
    # Resolve the Seraph persona name to its seeded server artifact UUID
    arango_db = next(_get_arango_db())
    try:
        from services import server_registry
        result = mcp_service.invoke_tool(
            db=arango_db,
            user_id=user_id,
            workspace_id=None,  # Seraph is built-in, no workspace needed
            server_artifact_id=server_registry.resolve_name_to_id("seraph"),
            tool_name="complete_authorizer_oauth",
            arguments={
                "authorizer_config": authorizer_config,
                "authorizer_artifact_id": authorizer_artifact_id,
                "authorization_code": authorization_code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
                "user_bearer_token": user_bearer_token,
            },
        )
    except Exception as exc:
        logger.error("OAuth completion failed (Seraph may be unavailable): %s", exc)
        return {"error": f"OAuth completion service unavailable: {exc}"}

    return result


AGENT = complete_authorizer_oauth
