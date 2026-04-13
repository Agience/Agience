"""Internal gate endpoints — called by kernel MCP servers only.

Ophan pushes numeric limits after Stripe subscription events.
Ophan reads usage for the billing settings UI.

Authentication: kernel server JWT (client_credentials exchange with
PLATFORM_INTERNAL_SECRET, principal_type=server, client_id in server_registry.all_client_ids()).
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from arango.database import StandardDatabase

from services import server_registry
from core.dependencies import get_arango_db
from services.auth_service import verify_token
from services import gate_service

logger = logging.getLogger(__name__)

gate_router = APIRouter(prefix="/internal/gate", tags=["Gate (internal)"])

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Auth: kernel server JWT only
# ---------------------------------------------------------------------------

def _require_kernel_server(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """Verify caller is a first-party kernel MCP server. Returns client_id."""
    if not credentials or not credentials.credentials:
        raise HTTPException(401, "Missing bearer token")

    try:
        payload = verify_token(credentials.credentials)
    except Exception:
        raise HTTPException(401, "Invalid token")

    if not payload:
        raise HTTPException(401, "Invalid token")

    if payload.get("principal_type") != "server":
        raise HTTPException(403, "Kernel server token required")

    client_id = payload.get("client_id") or payload.get("sub", "").replace("server/", "")
    if client_id not in server_registry.all_client_ids():
        raise HTTPException(403, "Not a kernel server")

    return client_id


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SetLimitsRequest(BaseModel):
    person_id: str
    max_workspaces: Optional[int] = None
    max_artifacts: Optional[int] = None
    vu_limit: Optional[int] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@gate_router.post("/set-limits", status_code=204)
def set_limits(
    body: SetLimitsRequest,
    db: StandardDatabase = Depends(get_arango_db),
    _caller: str = Depends(_require_kernel_server),
):
    """Upsert entitlement limits for a person. Called by Ophan after Stripe events."""
    gate_service.set_limits(
        db, body.person_id,
        max_workspaces=body.max_workspaces,
        max_artifacts=body.max_artifacts,
        vu_limit=body.vu_limit,
    )
    logger.info("Gate limits updated: person=%s caller=%s", body.person_id, _caller)
    return Response(status_code=204)


@gate_router.get("/usage/{person_id}")
def get_usage(
    person_id: str,
    db: StandardDatabase = Depends(get_arango_db),
    _caller: str = Depends(_require_kernel_server),
):
    """Return limits + current usage for a person. Called by Ophan for the billing UI."""
    limits = gate_service.get_or_default_limits(db, person_id)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    return {
        "person_id": person_id,
        "limits": limits,
        "usage": {
            "workspaces": gate_service.count_workspaces(db, person_id),
            "artifacts": gate_service.count_artifacts(db, person_id),
            "vu": gate_service.get_tally(db, person_id, "vu", current_month),
        },
    }
