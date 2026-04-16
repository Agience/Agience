"""
agience-server-ophan � MCP Server
====================================
Finance, Economic Logic & Performance: value transfers, crypto, fiat, double-entry
ledger, reconciliation, invoicing, market data, resource allocation, budgeting,
performance measurement, licensing, entitlements, and commercial operations.

Ophan is the economic operations layer. It executes and records value transfers
(crypto and fiat), maintains a double-entry ledger, reconciles accounts across
banks and exchanges, tracks resource usage and budgets, measures system
performance, and produces the auditable financial, licensing, and metrics cards
that form the foundation of platform economics.

Tools
-----
  send_payment       � Initiate a value transfer (crypto or fiat)
  record_transaction � Record an external transaction as a double-entry ledger card
  get_transaction    � Fetch a transaction by ID or on-chain hash
  list_transactions  � List transactions for an account over a date range
  fetch_statement    � Import a bank/exchange/payroll statement as workspace cards
  get_balance        � Get current balance for a wallet, bank account, or ledger account
  reconcile_account  � Match transactions against a statement and flag discrepancies
  create_invoice     � Generate an invoice card (accounts receivable)
  apply_payment      � Mark an invoice as paid and update the ledger
  run_report         � Produce a P&L, balance sheet, or cash-flow report card
  get_price          � Get current or historical price for a crypto or equity ticker
  get_market_data    � Fetch OHLCV data for a symbol over a date range
  track_wallet       � Monitor a blockchain wallet address for incoming activity
  get_portfolio      � Retrieve holdings from a connected brokerage or exchange
  calculate_pnl      � Calculate realised/unrealised P&L
  track_resource_usage � Track resource consumption and allocation
  get_metrics         � Get system performance and efficiency metrics
  calculate_budget    � Calculate or project budget for a workspace or project
    issue_license       � Create and sign a license artifact from entitlement inputs
    renew_license       � Extend or replace an existing license artifact
    revoke_license      � Revoke a license and record the compliance event
    review_installation � Inspect installation and activation state
    record_usage_snapshot � Ingest aggregate metering or usage snapshots
    run_licensing_report � Produce licensing or entitlement report cards
    check_llm_allowance  � Pre-invocation rate limit check for LLM usage
    record_llm_usage     � Record LLM token usage and convert to VU

Auth
----
  PLATFORM_INTERNAL_SECRET  ⬩ Shared deployment secret for client_credentials token exchange
  AGIENCE_API_URI           ⬩ Base URI of the agience-core backend

Transport
---------
  MCP_TRANSPORT=streamable-http (default for Agience)
  MCP_HOST=0.0.0.0
  MCP_PORT=8090
"""

from __future__ import annotations

import asyncio
import base64
from contextvars import ContextVar
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("agience-server-ophan")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s - %(name)s - %(message)s")

AGIENCE_API_URI: str = os.getenv("AGIENCE_API_URI", "http://localhost:8081").rstrip("/")
PLATFORM_INTERNAL_SECRET: str | None = os.getenv("PLATFORM_INTERNAL_SECRET")
OPHAN_CLIENT_ID: str = "agience-server-ophan"
MCP_TRANSPORT: str = os.getenv("MCP_TRANSPORT", "streamable-http")
MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.getenv("MCP_PORT", "8090"))

# Stripe (SaaS billing) — all optional; tools guard on STRIPE_SECRET_KEY
STRIPE_SECRET_KEY: str | None = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET: str | None = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID_PRO: str | None = os.getenv("STRIPE_PRICE_ID_PRO")
STRIPE_PRICE_ID_POWER: str | None = os.getenv("STRIPE_PRICE_ID_POWER")
STRIPE_PRICE_ID_VU_TOPUP: str | None = os.getenv("STRIPE_PRICE_ID_VU_TOPUP")

SUBSCRIPTION_CARD_MIME = "application/vnd.agience.subscription+json"

LICENSE_CARD_MIME = "application/vnd.agience.license+json"
ENTITLEMENT_CARD_MIME = "application/vnd.agience.entitlement+json"
INSTALLATION_CARD_MIME = "application/vnd.agience.license-installation+json"
USAGE_CARD_MIME = "application/vnd.agience.license-usage+json"
EVENT_CARD_MIME = "application/vnd.agience.license-event+json"
ORGANIZATION_CARD_MIME = "application/vnd.agience.organization+json"
KNOWN_LICENSING_ENTITLEMENTS = {
    "host_standard",
    "white_label_branding",
    "relay_distribution",
    "oem_distribution",
    "licensing_operations",
    "delegated_licensing_operations",
}
ADVANCED_OPERATIONS_ENTITLEMENTS = {"licensing_operations", "delegated_licensing_operations"}
_CURRENT_OPERATOR_CLAIMS: ContextVar[dict[str, Any] | None] = ContextVar(
    "ophan_current_operator_claims",
    default=None,
)


class OphanToolError(RuntimeError):
    """Raised when an Ophan tool cannot complete a request safely."""


# ---------------------------------------------------------------------------
# Platform auth — client_credentials token exchange
# ---------------------------------------------------------------------------

_token_state: dict = {"access_token": None, "expires_at": 0.0}
_token_lock = asyncio.Lock()


async def _exchange_token() -> str | None:
    """Exchange kernel credentials for a platform JWT; refreshes 60 s before expiry."""
    if not PLATFORM_INTERNAL_SECRET:
        return None

    import time

    async with _token_lock:
        if _token_state["access_token"] and time.time() < _token_state["expires_at"] - 60:
            return _token_state["access_token"]

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{AGIENCE_API_URI}/auth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": OPHAN_CLIENT_ID,
                    "client_secret": PLATFORM_INTERNAL_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            body = resp.json()

        token = body["access_token"]
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        _token_state["access_token"] = token
        _token_state["expires_at"] = float(payload.get("exp", time.time() + 43200))
        return token


async def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    token = await _exchange_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ---------------------------------------------------------------------------
# Shared authentication infrastructure (AgieceServerAuth)
# ---------------------------------------------------------------------------

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent.parent / "_shared"))
from agience_server_auth import AgieceServerAuth as _AgieceServerAuth

_auth = _AgieceServerAuth(OPHAN_CLIENT_ID, AGIENCE_API_URI)


async def _user_headers() -> dict[str, str]:
    """Return headers with the verified delegation JWT, or fall back to server token."""
    return await _auth.user_headers(_exchange_token)


async def server_startup() -> None:
    """Run Ophan startup tasks: Core JWKS fetch + server key registration."""
    await _auth.startup(_exchange_token)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _profiles_root() -> Path:
    return _repo_root() / "packaging" / "profiles"


def _backend_dir() -> Path:
    return _repo_root() / "backend"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _future_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


@lru_cache(maxsize=1)
def _licensing_service():
    backend_dir = str(_backend_dir())
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from services import licensing_service

    return licensing_service


# Licensing scope pattern — inlined from core/scopes.py to avoid backend import.
import re as _re
_LICENSING_SCOPE_PATTERN = _re.compile(r"^licensing:entitlement:([a-zA-Z0-9_\-]+)$")


def _extract_licensing_entitlements(scopes: list[str]) -> set[str]:
    """Extract entitlement names from explicit licensing scopes."""
    entitlements: set[str] = set()
    for scope in scopes:
        match = _LICENSING_SCOPE_PATTERN.match(scope)
        if match:
            entitlements.add(match.group(1))
    return entitlements


def _normalize_entitlements(values: Optional[list[str]]) -> set[str]:
    return {item.strip() for item in (values or []) if item and item.strip()}


def _resource_filter_values(resource_filters: Any, key: str) -> list[str]:
    if not isinstance(resource_filters, dict):
        return []
    raw = resource_filters.get(key)
    if raw == "*":
        return ["*"]
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if item and str(item).strip()]


def _current_operator_claims() -> dict[str, Any]:
    claims = _CURRENT_OPERATOR_CLAIMS.get()
    if not claims:
        raise OphanToolError(
            "This operation requires a verified operator token with licensing authorization."
        )
    return claims


def _set_current_operator_claims_for_test(claims: Optional[dict[str, Any]]) -> object:
    return _CURRENT_OPERATOR_CLAIMS.set(claims)


def _reset_current_operator_claims_for_test(token: object) -> None:
    _CURRENT_OPERATOR_CLAIMS.reset(token)


def _require_workspace_access(claims: dict[str, Any], workspace_id: str, action: str) -> None:
    allowed = _resource_filter_values(claims.get("resource_filters"), "workspaces")
    if not allowed or "*" in allowed:
        return
    if workspace_id in allowed:
        return
    raise OphanToolError(
        f"{action} is gated. The verified operator token cannot access workspace '{workspace_id}'."
    )


def _resolve_operator_entitlements(workspace_id: str, action: str) -> set[str]:
    claims = _current_operator_claims()
    _require_workspace_access(claims, workspace_id, action)

    scopes = _as_string_list(claims.get("scopes"))
    entitlements = _extract_licensing_entitlements(scopes)
    return {item for item in entitlements if item in KNOWN_LICENSING_ENTITLEMENTS}


def _current_operator_summary(workspace_id: str, action: str) -> dict[str, Any]:
    claims = _current_operator_claims()
    return {
        "subject_id": claims.get("sub"),
        "client_id": claims.get("client_id"),
        "api_key_id": claims.get("api_key_id"),
        "authorized_entitlements": sorted(_resolve_operator_entitlements(workspace_id, action)),
    }


def _verify_operator_token(token: str) -> Optional[dict[str, Any]]:
    """Verify an operator JWT using Core's JWKS (fetched at startup)."""
    return _auth.verify_core_jwt(token)


def _extract_bearer_token(scope: dict[str, Any]) -> Optional[str]:
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name.decode("latin-1").lower() != "authorization":
            continue
        value = raw_value.decode("latin-1")
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return token.strip()
    return None


async def _send_unauthorized(send: Any, message: str) -> None:
    body = json.dumps({"jsonrpc": "2.0", "error": {"code": -32001, "message": message}}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def create_server_app() -> Any:
    """Return the Ophan ASGI app with operator token verification.

    Note: ophan uses operator token verification (not delegation JWT middleware)
    because ophan tools require operator-scoped claims for entitlement checks.
    AgieceServerAuth instance handles JWKS fetch and server key registration.

    Also mounts /webhooks/stripe for raw Stripe webhook delivery (no auth — Stripe
    signature verification handles authentication internally).
    """
    mcp_app = streamable_http_app()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == "/webhooks/stripe" and scope.get("method", "").upper() == "POST":
                await _handle_stripe_webhook_http(scope, receive, send)
                return
        await mcp_app(scope, receive, send)

    return app


def streamable_http_app() -> Any:
    """Wrap the MCP ASGI app with optional operator token extraction.

    Auth is permissive at the transport level — if a valid operator JWT is
    present it is verified and stored in ``_CURRENT_OPERATOR_CLAIMS``;
    otherwise the request proceeds without claims.  Individual tools gate
    access via ``_current_operator_claims()`` which raises if claims are
    absent.

    This matches the other persona servers' pattern and allows MCP protocol
    calls (e.g. type discovery at startup) to succeed without a token.
    """
    inner_app = mcp.streamable_http_app()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await inner_app(scope, receive, send)
            return

        token = _extract_bearer_token(scope)
        if token:
            claims = _verify_operator_token(token)
            if claims:
                context_token = _CURRENT_OPERATOR_CLAIMS.set(claims)
                try:
                    await inner_app(scope, receive, send)
                finally:
                    _CURRENT_OPERATOR_CLAIMS.reset(context_token)
                return

        await inner_app(scope, receive, send)

    return app


def _parse_json_argument(raw: Optional[str], field_name: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OphanToolError(f"{field_name} must be valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise OphanToolError(f"{field_name} must decode to a JSON object.")
    return parsed


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None and str(item)]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _organization_card_index(cards: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for card in cards:
        artifact_id = card.get("id")
        if artifact_id is None:
            continue
        index[str(artifact_id)] = (card, _parse_artifact_context(card))
    return index


def _organization_context_from_index(
    index: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    organization_artifact_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    item = index.get(str(organization_artifact_id))
    if item is None:
        raise OphanToolError(f"No organization card found for id '{organization_artifact_id}'.")
    card, context = item
    if context.get("content_type") != ORGANIZATION_CARD_MIME:
        raise OphanToolError(f"Card '{organization_artifact_id}' is not an organization card.")
    return card, context


def _profile_policy_id(profile_name: str) -> Optional[str]:
    refs = _as_string_list(_load_profile(profile_name).get("policy_refs"))
    return refs[0] if refs else None


def _organization_display_name(context: dict[str, Any], fallback_id: str) -> str:
    identity = _as_dict(context.get("identity"))
    return str(
        identity.get("display_name")
        or identity.get("legal_name")
        or context.get("title")
        or fallback_id
    )


def _resolve_public_license_posture(
    organization_artifact_id: str,
    organization_context: dict[str, Any],
    profile_name: str,
    profile_definition: dict[str, Any],
    index: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    identity = _as_dict(organization_context.get("identity"))
    licensing = _as_dict(organization_context.get("licensing"))
    packaging = _as_dict(licensing.get("packaging"))
    compliance = _as_dict(licensing.get("compliance"))

    entity_kind = str(identity.get("entity_kind") or "").strip().lower()
    if not entity_kind:
        raise OphanToolError("Organization card is missing 'identity.entity_kind'.")

    packaging_flags = {
        "managed_service": _coerce_bool(packaging.get("offers_managed_service")),
        "hosted_service": _coerce_bool(packaging.get("offers_hosted_service")),
        "oem": _coerce_bool(packaging.get("offers_oem")),
        "embedded": _coerce_bool(packaging.get("offers_embedded")),
        "white_label": _coerce_bool(packaging.get("offers_white_label")),
    }

    # AGPL compliance flags from organization card
    proprietary_modifications = _coerce_bool(compliance.get("proprietary_modifications"))
    closed_source_service = _coerce_bool(compliance.get("closed_source_service"))
    source_disclosed = _coerce_bool(compliance.get("source_disclosed"))

    profile_control_class = str(profile_definition.get("license_class") or "")
    commercial_by_profile = profile_control_class in {"white-label", "oem-embedded", "relay-client"} or profile_name == "managed-host"

    reasons: list[str] = []

    # Track 1 � Copyleft trigger: proprietary modifications or closed-source service without source disclosure
    if commercial_by_profile:
        reasons.append(f"Profile '{profile_name}' is a commercial distribution mode.")
    if proprietary_modifications and not source_disclosed:
        reasons.append("Organization uses proprietary modifications without disclosing source (AGPL copyleft opt-out).")
    if closed_source_service and not source_disclosed:
        reasons.append("Organization offers a closed-source managed service without disclosing source (AGPL copyleft opt-out).")

    # Track 2 � Trademark trigger: white-label always requires commercial license
    if packaging_flags["white_label"]:
        reasons.append("Organization declares white-label packaging (trademark license required regardless of AGPL compliance).")

    # Other packaging flags that imply commercial use
    for flag_name in ("managed_service", "hosted_service", "oem", "embedded"):
        if packaging_flags[flag_name]:
            reasons.append(f"Organization declares {flag_name.replace('_', ' ')} packaging in its organization card.")

    requires_license = bool(reasons)
    resolved_profile = profile_name
    resolved_policy_id = _profile_policy_id(profile_name)
    control_class = profile_control_class
    if not requires_license and profile_name in {"standard", "community-self-host"}:
        resolved_profile = "community-self-host"
        resolved_policy_id = _profile_policy_id("community-self-host")
        control_class = "community-self-host"
        reasons.append("Organization is AGPL-compliant with no white-label use; qualifies for community self-host.")

    return {
        "status": "ok",
        "organization_artifact_id": organization_artifact_id,
        "organization_name": _organization_display_name(organization_context, organization_artifact_id),
        "entity_kind": entity_kind,
        "input_profile": profile_name,
        "resolved_profile": resolved_profile,
        "resolved_policy_id": resolved_policy_id,
        "control_class": control_class,
        "requires_license": requires_license,
        "decision": "commercial" if requires_license else "community",
        "reasons": reasons,
        "compliance": {
            "proprietary_modifications": proprietary_modifications,
            "closed_source_service": closed_source_service,
            "source_disclosed": source_disclosed,
            "packaging": packaging_flags,
        },
    }


async def _resolve_license_posture_from_workspace(
    workspace_id: str,
    organization_artifact_id: str,
    profile_name: str,
) -> dict[str, Any]:
    cards = await _list_workspace_artifacts(workspace_id)
    index = _organization_card_index(cards)
    _card, context = _organization_context_from_index(index, organization_artifact_id)
    profile_definition = _load_profile(profile_name)
    result = _resolve_public_license_posture(
        organization_artifact_id,
        context,
        profile_name,
        profile_definition,
        index,
    )
    result["workspace_id"] = workspace_id
    return result


def _license_branding_mode(control_class: Optional[str], branding_scope: list[str], features: dict[str, Any]) -> Optional[str]:
    if control_class == "white-label":
        return "white-label"
    if branding_scope:
        return "white-label"
    if bool(features.get("allow_white_label")):
        return "white-label"
    return None


def _artifact_digest(payload: dict[str, Any]) -> str:
    return _licensing_service().digest_signed_payload(payload)


def _issue_signed_license_artifact(**kwargs: Any) -> dict[str, Any]:
    try:
        return _licensing_service().issue_license_artifact(**kwargs)
    except RuntimeError as exc:
        raise OphanToolError(str(exc)) from exc


def _issue_signed_activation_lease(**kwargs: Any) -> dict[str, Any]:
    try:
        return _licensing_service().issue_activation_lease(**kwargs)
    except RuntimeError as exc:
        raise OphanToolError(str(exc)) from exc


def _get_signed_license_artifact(context: dict[str, Any]) -> dict[str, Any]:
    artifact = context.get("signed_artifact")
    if not isinstance(artifact, dict):
        raise OphanToolError("License card does not contain a signed_artifact payload.")
    return artifact


@lru_cache(maxsize=32)
def _load_profile(profile_name: str) -> dict[str, Any]:
    path = _profiles_root() / f"{profile_name}.json"
    if not path.exists():
        raise OphanToolError(f"Unknown profile '{profile_name}'.")
    return json.loads(path.read_text(encoding="utf-8"))


def _profile_required_entitlements(profile_name: str) -> set[str]:
    return set(_load_profile(profile_name).get("required_entitlements", []))


def _profile_license_class(profile_name: str) -> Optional[str]:
    return _load_profile(profile_name).get("license_class")


def _profile_surface(profile_name: str) -> Optional[str]:
    return _load_profile(profile_name).get("product_surface")


def _require_any_licensing_entitlement(granted: set[str], action: str) -> None:
    if granted & KNOWN_LICENSING_ENTITLEMENTS:
        return
    raise OphanToolError(
        f"{action} is gated. The verified operator token does not grant any licensing entitlements."
    )


def _require_entitlements(granted: set[str], required: set[str], action: str) -> None:
    missing = sorted(required - granted)
    if missing:
        raise OphanToolError(
            f"{action} is gated. Missing required entitlements: {', '.join(missing)}."
        )


async def _request(method: str, path: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        response = await client.request(
            method,
            f"{AGIENCE_API_URI}{path}",
            headers=await _headers(),
            json=payload,
            timeout=60,
        )
    if response.status_code >= 400:
        raise OphanToolError(f"Platform request failed: {response.status_code} � {response.text[:300]}")
    if not response.text:
        return {}
    return response.json()


async def _create_workspace_artifact(workspace_id: str, context: dict[str, Any], content: str) -> dict[str, Any]:
    return await _request(
        "POST",
        f"/workspaces/{workspace_id}/artifacts",
        {
            "context": context,
            "content": content,
        },
    )


async def _update_workspace_artifact(
    workspace_id: str,
    artifact_id: str,
    *,
    context: Optional[dict[str, Any]] = None,
    content: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if context is not None:
        payload["context"] = context
    if content is not None:
        payload["content"] = content
    return await _request("PATCH", f"/workspaces/{workspace_id}/artifacts/{artifact_id}", payload)


async def _list_workspace_artifacts(workspace_id: str) -> list[dict[str, Any]]:
    response = await _request("GET", f"/workspaces/{workspace_id}/artifacts")
    items = response.get("items")
    if isinstance(items, list):
        return items
    return []


def _parse_artifact_context(card: dict[str, Any]) -> dict[str, Any]:
    raw = card.get("context")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


async def _find_workspace_artifact(
    workspace_id: str,
    *,
    content_type: str,
    identity_key: str,
    identity_value: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    for card in await _list_workspace_artifacts(workspace_id):
        context = _parse_artifact_context(card)
        if context.get("content_type") != content_type:
            continue
        if context.get(identity_key) == identity_value:
            return card, context
    raise OphanToolError(
        f"No card found in workspace '{workspace_id}' for {identity_key}='{identity_value}'."
    )


def _title_from_prefix(prefix: str, identifier: str) -> str:
    return f"{prefix} {identifier}"


def _summarize_limits(limits: dict[str, Any]) -> str:
    if not limits:
        return "No explicit limits recorded."
    return ", ".join(f"{key}={value}" for key, value in sorted(limits.items()))


def _artifact_id_from_response(response: dict[str, Any]) -> Optional[str]:
    return response.get("id") or response.get("card", {}).get("id")


mcp = FastMCP(
    "agience-server-ophan",
    instructions=(
        "You are Ophan, the economic operations layer of the Agience platform. "
        "You execute and record value transfers, maintain double-entry ledgers, reconcile "
        "accounts, track market data, measure resource usage and performance, manage "
        "budgets, and operate licensing and commercial entitlement workflows. Every financial or "
        "licensing action must be recorded as an auditable card. "
        "Treat financial credentials with extreme care � always retrieve them per-request "
        "from the platform secrets service."
    ),
)

from artifact_helpers import register_types_manifest
register_types_manifest(mcp, "ophan", __file__)


# ---------------------------------------------------------------------------
# Tool stubs (Phase 1i implementation)
# ---------------------------------------------------------------------------

@mcp.tool(description="Initiate a value transfer � crypto (on-chain) or fiat (bank/wire)")
async def send_payment(
    amount: str,
    currency: str,
    recipient: str,
    workspace_id: str,
    memo: Optional[str] = None,
) -> str:
    return "TODO: send_payment not yet implemented."


@mcp.tool(description="Record an external transaction as a double-entry ledger card")
async def record_transaction(
    workspace_id: str,
    amount: str,
    currency: str,
    debit_account: str,
    credit_account: str,
    memo: Optional[str] = None,
    external_id: Optional[str] = None,
) -> str:
    return "TODO: record_transaction not yet implemented."


@mcp.tool(description="Fetch a transaction by ID or on-chain hash")
async def get_transaction(
    transaction_id: str,
    workspace_id: str,
) -> str:
    return "TODO: get_transaction not yet implemented."


@mcp.tool(description="List transactions for an account over a date range")
async def list_transactions(
    workspace_id: str,
    account: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
) -> str:
    return "TODO: list_transactions not yet implemented."


@mcp.tool(description="Import a bank, exchange, or payroll statement as workspace cards")
async def fetch_statement(
    workspace_id: str,
    source: str,
    since: Optional[str] = None,
) -> str:
    return "TODO: fetch_statement not yet implemented."


@mcp.tool(description="Get current balance for a wallet, bank account, or ledger account")
async def get_balance(
    account: str,
    workspace_id: str,
) -> str:
    return "TODO: get_balance not yet implemented."


@mcp.tool(description="Match transactions against a statement and flag discrepancies")
async def reconcile_account(
    workspace_id: str,
    account: str,
    statement_artifact_id: str,
) -> str:
    return "TODO: reconcile_account not yet implemented."


@mcp.tool(description="Generate an invoice card (accounts receivable)")
async def create_invoice(
    workspace_id: str,
    amount: str,
    currency: str,
    recipient: str,
    due_date: Optional[str] = None,
) -> str:
    return "TODO: create_invoice not yet implemented."


@mcp.tool(description="Mark an invoice as paid and update the ledger")
async def apply_payment(
    workspace_id: str,
    invoice_artifact_id: str,
    payment_amount: str,
    payment_date: Optional[str] = None,
) -> str:
    return "TODO: apply_payment not yet implemented."


@mcp.tool(description="Produce a P&L, balance sheet, or cash-flow report card")
async def run_report(
    workspace_id: str,
    report_type: str = "pnl",
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> str:
    return "TODO: run_report not yet implemented."


@mcp.tool(description="Get current or historical price for a crypto or equity ticker")
async def get_price(
    symbol: str,
    date: Optional[str] = None,
) -> str:
    return "TODO: get_price not yet implemented."


@mcp.tool(description="Fetch OHLCV data for a symbol over a date range")
async def get_market_data(
    symbol: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    interval: str = "1d",
) -> str:
    return "TODO: get_market_data not yet implemented."


@mcp.tool(description="Monitor a blockchain wallet address for incoming activity")
async def track_wallet(
    address: str,
    network: str = "ethereum",
    workspace_id: Optional[str] = None,
) -> str:
    return "TODO: track_wallet not yet implemented."


@mcp.tool(description="Retrieve holdings from a connected brokerage or exchange")
async def get_portfolio(
    workspace_id: str,
    source: Optional[str] = None,
) -> str:
    return "TODO: get_portfolio not yet implemented."


@mcp.tool(description="Calculate realised/unrealised P&L for on-chain or brokerage positions")
async def calculate_pnl(
    workspace_id: str,
    account: Optional[str] = None,
    since: Optional[str] = None,
) -> str:
    return "TODO: calculate_pnl not yet implemented."


@mcp.tool(description="Track resource consumption and allocation across workspaces or projects")
async def track_resource_usage(
    workspace_id: str,
    resource_type: Optional[str] = None,
    since: Optional[str] = None,
) -> str:
    """Return resource usage metrics (API calls, storage, compute) for a workspace."""
    return "TODO: track_resource_usage not yet implemented."


@mcp.tool(description="Get system performance and efficiency metrics")
async def get_metrics(
    metric_type: str = "all",
    scope: Optional[str] = None,
    since: Optional[str] = None,
) -> str:
    """
    Args:
        metric_type: 'all', 'cost', 'throughput', 'latency', 'usage'.
        scope: Optional scope (workspace ID, collection ID, or 'system').
        since: ISO 8601 timestamp for metric window start.
    """
    return "TODO: get_metrics not yet implemented."


@mcp.tool(description="Calculate or project budget for a workspace or project")
async def calculate_budget(
    workspace_id: str,
    period: str = "month",
    include_projections: bool = False,
) -> str:
    """
    Args:
        workspace_id: Workspace to budget for.
        period: Budget period � 'week', 'month', 'quarter', 'year'.
        include_projections: Whether to include forward-looking projections.
    """
    return "TODO: calculate_budget not yet implemented."


@mcp.tool(description="Resolve whether an organization is AGPL-compliant (community self-host) or needs a commercial license")
async def resolve_license_posture(
    workspace_id: str,
    organization_artifact_id: str,
    profile: str = "standard",
) -> str:
    try:
        claims = _current_operator_claims()
        _require_workspace_access(claims, workspace_id, "resolve_license_posture")
        return _json_result(await _resolve_license_posture_from_workspace(workspace_id, organization_artifact_id, profile))
    except OphanToolError as exc:
        return f"Error: {exc}"


@mcp.tool(description="Create and sign a license artifact from entitlement inputs or approved policy parameters")
async def issue_license(
    workspace_id: str,
    policy_id: str,
    account_id: str,
    profile: str,
    organization_artifact_id: Optional[str] = None,
    expires_at: Optional[str] = None,
    runtime_role: str = "standard",
    branding_scope: Optional[list[str]] = None,
    limits: Optional[str] = None,
    features: Optional[str] = None,
    downstream_customer: Optional[str] = None,
) -> str:
    try:
        posture: Optional[dict[str, Any]] = None
        if organization_artifact_id:
            posture = await _resolve_license_posture_from_workspace(workspace_id, organization_artifact_id, profile)
            if not posture["requires_license"]:
                return _json_result(
                    {
                        "status": "not_required",
                        "workspace_id": workspace_id,
                        "organization_artifact_id": organization_artifact_id,
                        "account_id": account_id,
                        "posture": posture,
                    }
                )
            if profile == "community-self-host":
                raise OphanToolError(
                    "Organization requires a commercial license; use the commercial 'standard' profile instead of 'community-self-host'."
                )

        granted = _resolve_operator_entitlements(workspace_id, "issue_license")
        operator = _current_operator_summary(workspace_id, "issue_license")
        required = ADVANCED_OPERATIONS_ENTITLEMENTS | _profile_required_entitlements(profile)
        _require_entitlements(granted, required, "issue_license")

        profile_definition = _load_profile(profile)
        issued_at = _now_iso()
        entitlement_id = f"ent_{uuid4().hex[:12]}"
        license_id = f"lic_{uuid4().hex[:12]}"
        effective_expires_at = expires_at or _future_iso(365)
        parsed_limits = _parse_json_argument(limits, "limits")
        parsed_features = _parse_json_argument(features, "features")
        control_class = _profile_license_class(profile)
        product_surface = _profile_surface(profile)
        license_entitlements = sorted(_profile_required_entitlements(profile))
        effective_branding_scope = branding_scope or []
        signed_artifact = _issue_signed_license_artifact(
            license_id=license_id,
            entitlement_id=entitlement_id,
            account_id=account_id,
            issued_at=issued_at,
            not_before=issued_at,
            expires_at=effective_expires_at,
            policy_id=policy_id,
            runtime_roles=[runtime_role],
            distribution_profiles=[profile],
            product_surface=product_surface,
            branding_mode=_license_branding_mode(control_class, effective_branding_scope, parsed_features),
            branding_scope=effective_branding_scope,
            state="active",
            offline_allowed=bool(profile_definition.get("offline_supported", True)),
            require_activation=True,
            require_reporting=True,
            offline_lease_days=30 if profile_definition.get("offline_supported", True) else None,
            enforcement_profile=control_class,
            limits=parsed_limits,
            features=parsed_features,
            reporting={
                "dimensions": _as_string_list(profile_definition.get("meter_dimensions")),
                "snapshot_interval_hours": 24,
            },
            entitlements=license_entitlements,
            attributes={
                "downstream_customer": downstream_customer,
            }
            if downstream_customer
            else {},
            extensions={
                "issued_by": "ophan",
                "profile": profile,
            },
        )

        entitlement_context = {
            "type": "entitlement",
            "title": _title_from_prefix("Entitlement", entitlement_id),
            "content_type": ENTITLEMENT_CARD_MIME,
            "entitlement_id": entitlement_id,
            "account_id": account_id,
            "policy_id": policy_id,
            "profile": profile,
            "runtime_roles": [runtime_role],
            "state": "active",
            "required_entitlements": license_entitlements,
            "operator": operator,
            "branding_scope": effective_branding_scope,
            "downstream_customer": downstream_customer,
            "issued_at": issued_at,
            "expires_at": effective_expires_at,
        }
        entitlement_content = (
            f"Entitlement {entitlement_id} for account {account_id} under policy {policy_id}. "
            f"Profile {profile}; required entitlements: {', '.join(license_entitlements) or 'none'}."
        )
        entitlement_artifact = await _create_workspace_artifact(workspace_id, entitlement_context, entitlement_content)

        license_context = {
            "type": "license",
            "title": _title_from_prefix("License", license_id),
            "content_type": LICENSE_CARD_MIME,
            "license_id": license_id,
            "entitlement_id": entitlement_id,
            "account_id": account_id,
            "policy_id": policy_id,
            "control_class": control_class,
            "product_surface": product_surface,
            "runtime_roles": [runtime_role],
            "distribution_profiles": [profile],
            "branding_scope": effective_branding_scope,
            "state": "active",
            "issued_at": issued_at,
            "not_before": issued_at,
            "expires_at": effective_expires_at,
            "artifact_status": "issued",
            "artifact_ref": f"ophan://licenses/{license_id}",
            "artifact_hash": _artifact_digest(signed_artifact),
            "signed_artifact": signed_artifact,
            "limits": parsed_limits,
            "features": parsed_features,
            "entitlements": license_entitlements,
            "downstream_customer": downstream_customer,
        }
        license_content = (
            f"License {license_id} is active for account {account_id}. "
            f"Profile {profile}; limits: {_summarize_limits(parsed_limits)}."
        )
        license_artifact = await _create_workspace_artifact(workspace_id, license_context, license_content)

        event_context = {
            "type": "license-event",
            "title": _title_from_prefix("Licensing Event", f"issued-{license_id}"),
            "content_type": EVENT_CARD_MIME,
            "event_type": "license_issued",
            "severity": "info",
            "account_id": account_id,
            "license_id": license_id,
            "entitlement_id": entitlement_id,
            "policy_id": policy_id,
            "profile": profile,
            "created_at": issued_at,
        }
        event_content = (
            f"Issued license {license_id} for account {account_id} under profile {profile}."
        )
        event_artifact = await _create_workspace_artifact(workspace_id, event_context, event_content)

        return _json_result(
            {
                "status": "issued",
                "workspace_id": workspace_id,
                "license_id": license_id,
                "entitlement_id": entitlement_id,
                **({"posture": posture} if posture else {}),
                "artifact": signed_artifact,
                "created_cards": {
                    "entitlement_artifact_id": _artifact_id_from_response(entitlement_artifact),
                    "license_artifact_id": _artifact_id_from_response(license_artifact),
                    "event_artifact_id": _artifact_id_from_response(event_artifact),
                },
                "required_entitlements": sorted(required),
            }
        )
    except OphanToolError as exc:
        return f"Error: {exc}"


@mcp.tool(description="Extend, replace, or reissue an existing license artifact")
async def renew_license(
    workspace_id: str,
    license_id: str,
    expires_at: Optional[str] = None,
) -> str:
    try:
        granted = _resolve_operator_entitlements(workspace_id, "renew_license")
        _require_entitlements(granted, ADVANCED_OPERATIONS_ENTITLEMENTS, "renew_license")

        card, context = await _find_workspace_artifact(
            workspace_id,
            content_type=LICENSE_CARD_MIME,
            identity_key="license_id",
            identity_value=license_id,
        )
        renewed_at = _now_iso()
        existing_artifact = _get_signed_license_artifact(context)
        updated_expires_at = expires_at or _future_iso(365)
        renewal_count = int(context.get("renewal_count", 0)) + 1
        signed_artifact = _issue_signed_license_artifact(
            license_id=license_id,
            entitlement_id=str(context.get("entitlement_id") or ""),
            account_id=str(context.get("account_id") or ""),
            issued_at=renewed_at,
            not_before=renewed_at,
            expires_at=updated_expires_at,
            policy_id=str(context.get("policy_id") or existing_artifact["product"]["policy_id"]),
            runtime_roles=_as_string_list(context.get("runtime_roles")) or _as_string_list(existing_artifact["product"].get("runtime_roles")),
            distribution_profiles=_as_string_list(context.get("distribution_profiles")) or _as_string_list(existing_artifact["product"].get("distribution_profiles")),
            product_surface=context.get("product_surface") or existing_artifact["product"].get("product_surface"),
            branding_mode=(existing_artifact.get("branding") or {}).get("mode"),
            branding_scope=_as_string_list(context.get("branding_scope")) or _as_string_list((existing_artifact.get("branding") or {}).get("scope")),
            state="active",
            offline_allowed=bool(existing_artifact.get("controls", {}).get("offline_allowed", True)),
            require_activation=bool(existing_artifact.get("controls", {}).get("require_activation", True)),
            require_reporting=bool(existing_artifact.get("controls", {}).get("require_reporting", True)),
            offline_lease_days=existing_artifact.get("controls", {}).get("offline_lease_days"),
            enforcement_profile=existing_artifact.get("controls", {}).get("enforcement_profile") or context.get("control_class"),
            limits=_as_dict(context.get("limits")) or _as_dict(existing_artifact.get("limits")),
            features=_as_dict(context.get("features")) or _as_dict(existing_artifact.get("features")),
            reporting=_as_dict(existing_artifact.get("reporting")),
            entitlements=_as_string_list(context.get("entitlements")) or _as_string_list(existing_artifact.get("entitlements")),
            attributes=_as_dict(existing_artifact.get("attributes")),
            extensions={
                **_as_dict(existing_artifact.get("extensions")),
                "renewed_at": renewed_at,
                "renewal_count": renewal_count,
            },
        )
        updated_context = {
            **context,
            "state": "active",
            "expires_at": updated_expires_at,
            "last_renewed_at": renewed_at,
            "renewal_count": renewal_count,
            "artifact_status": "issued",
            "artifact_hash": _artifact_digest(signed_artifact),
            "signed_artifact": signed_artifact,
        }
        updated_content = (
            f"License {license_id} renewed. New expiry {updated_context['expires_at']}."
        )
        updated_artifact = await _update_workspace_artifact(
            workspace_id,
            card["id"],
            context=updated_context,
            content=updated_content,
        )
        event_context = {
            "type": "license-event",
            "title": _title_from_prefix("Licensing Event", f"renewed-{license_id}"),
            "content_type": EVENT_CARD_MIME,
            "event_type": "license_renewed",
            "severity": "info",
            "license_id": license_id,
            "account_id": context.get("account_id"),
            "created_at": renewed_at,
        }
        event_artifact = await _create_workspace_artifact(
            workspace_id,
            event_context,
            f"Renewed license {license_id}; expires {updated_context['expires_at']}.",
        )
        return _json_result(
            {
                "status": "renewed",
                "workspace_id": workspace_id,
                "license_id": license_id,
                "license_artifact_id": _artifact_id_from_response(updated_artifact),
                "event_artifact_id": _artifact_id_from_response(event_artifact),
                "expires_at": updated_context["expires_at"],
                "artifact": signed_artifact,
            }
        )
    except OphanToolError as exc:
        return f"Error: {exc}"


@mcp.tool(description="Revoke a license and record the resulting compliance event")
async def revoke_license(
    workspace_id: str,
    license_id: str,
    reason: Optional[str] = None,
) -> str:
    try:
        granted = _resolve_operator_entitlements(workspace_id, "revoke_license")
        _require_entitlements(granted, ADVANCED_OPERATIONS_ENTITLEMENTS, "revoke_license")

        card, context = await _find_workspace_artifact(
            workspace_id,
            content_type=LICENSE_CARD_MIME,
            identity_key="license_id",
            identity_value=license_id,
        )
        revoked_at = _now_iso()
        revoke_reason = reason or "No reason provided."
        existing_artifact = _get_signed_license_artifact(context)
        signed_artifact = _issue_signed_license_artifact(
            license_id=license_id,
            entitlement_id=str(context.get("entitlement_id") or ""),
            account_id=str(context.get("account_id") or ""),
            issued_at=str(context.get("issued_at") or revoked_at),
            not_before=str(context.get("not_before") or revoked_at),
            expires_at=str(context.get("expires_at") or revoked_at),
            policy_id=str(context.get("policy_id") or existing_artifact["product"]["policy_id"]),
            runtime_roles=_as_string_list(context.get("runtime_roles")) or _as_string_list(existing_artifact["product"].get("runtime_roles")),
            distribution_profiles=_as_string_list(context.get("distribution_profiles")) or _as_string_list(existing_artifact["product"].get("distribution_profiles")),
            product_surface=context.get("product_surface") or existing_artifact["product"].get("product_surface"),
            branding_mode=(existing_artifact.get("branding") or {}).get("mode"),
            branding_scope=_as_string_list(context.get("branding_scope")) or _as_string_list((existing_artifact.get("branding") or {}).get("scope")),
            state="revoked",
            offline_allowed=bool(existing_artifact.get("controls", {}).get("offline_allowed", True)),
            require_activation=bool(existing_artifact.get("controls", {}).get("require_activation", True)),
            require_reporting=bool(existing_artifact.get("controls", {}).get("require_reporting", True)),
            offline_lease_days=existing_artifact.get("controls", {}).get("offline_lease_days"),
            enforcement_profile=existing_artifact.get("controls", {}).get("enforcement_profile") or context.get("control_class"),
            limits=_as_dict(context.get("limits")) or _as_dict(existing_artifact.get("limits")),
            features=_as_dict(context.get("features")) or _as_dict(existing_artifact.get("features")),
            reporting=_as_dict(existing_artifact.get("reporting")),
            entitlements=_as_string_list(context.get("entitlements")) or _as_string_list(existing_artifact.get("entitlements")),
            attributes=_as_dict(existing_artifact.get("attributes")),
            extensions={
                **_as_dict(existing_artifact.get("extensions")),
                "revoked_at": revoked_at,
                "revoke_reason": revoke_reason,
            },
        )
        updated_context = {
            **context,
            "state": "revoked",
            "revoked_at": revoked_at,
            "revoke_reason": revoke_reason,
            "artifact_status": "revoked",
            "artifact_hash": _artifact_digest(signed_artifact),
            "signed_artifact": signed_artifact,
        }
        updated_artifact = await _update_workspace_artifact(
            workspace_id,
            card["id"],
            context=updated_context,
            content=f"License {license_id} was revoked. Reason: {revoke_reason}",
        )
        event_context = {
            "type": "license-event",
            "title": _title_from_prefix("Licensing Event", f"revoked-{license_id}"),
            "content_type": EVENT_CARD_MIME,
            "event_type": "license_revoked",
            "severity": "warning",
            "license_id": license_id,
            "account_id": context.get("account_id"),
            "reason": revoke_reason,
            "created_at": revoked_at,
        }
        event_artifact = await _create_workspace_artifact(
            workspace_id,
            event_context,
            f"Revoked license {license_id}. Reason: {revoke_reason}",
        )
        return _json_result(
            {
                "status": "revoked",
                "workspace_id": workspace_id,
                "license_id": license_id,
                "license_artifact_id": _artifact_id_from_response(updated_artifact),
                "event_artifact_id": _artifact_id_from_response(event_artifact),
                "artifact": signed_artifact,
            }
        )
    except OphanToolError as exc:
        return f"Error: {exc}"


@mcp.tool(description="Inspect installation state, activation status, and lease freshness")
async def review_installation(
    workspace_id: str,
    install_id: Optional[str] = None,
    instance_id: Optional[str] = None,
    device_id: Optional[str] = None,
    license_id: Optional[str] = None,
    profile: Optional[str] = None,
) -> str:
    try:
        granted = _resolve_operator_entitlements(workspace_id, "review_installation")
        _require_any_licensing_entitlement(granted, "review_installation")

        cards = await _list_workspace_artifacts(workspace_id)
        installation_artifacts: list[dict[str, Any]] = []
        matched_artifact: Optional[dict[str, Any]] = None
        matched_context: Optional[dict[str, Any]] = None
        for card in cards:
            context = _parse_artifact_context(card)
            if context.get("content_type") != INSTALLATION_CARD_MIME:
                continue
            installation_artifacts.append({"card": card, "context": context})
            if install_id and context.get("install_id") == install_id:
                matched_artifact, matched_context = card, context
            if instance_id and context.get("instance_id") == instance_id:
                matched_artifact, matched_context = card, context
            if device_id and context.get("device_id") == device_id:
                matched_artifact, matched_context = card, context

        if not any([install_id, instance_id, device_id]):
            return _json_result(
                {
                    "status": "ok",
                    "workspace_id": workspace_id,
                    "count": len(installation_artifacts),
                    "installations": [
                        {
                            "artifact_id": item["card"].get("id"),
                            "install_id": item["context"].get("install_id"),
                            "license_id": item["context"].get("license_id"),
                            "profile": item["context"].get("profile"),
                            "compliance_state": item["context"].get("compliance_state"),
                            "lease_expires_at": item["context"].get("lease_expires_at"),
                        }
                        for item in installation_artifacts
                    ],
                }
            )

        reviewed_at = _now_iso()
        if matched_artifact and matched_context:
            activation_lease = None
            if matched_context.get("license_id") and matched_context.get("profile"):
                _, linked_license_context = await _find_workspace_artifact(
                    workspace_id,
                    content_type=LICENSE_CARD_MIME,
                    identity_key="license_id",
                    identity_value=str(matched_context["license_id"]),
                )
                signed_artifact = _get_signed_license_artifact(linked_license_context)
                preflight = _licensing_service().preflight_license(str(matched_context["profile"]), signed_artifact)
                if not preflight.verification.valid or not preflight.compatibility.allowed:
                    raise OphanToolError(
                        f"Cannot issue activation lease for install '{matched_context.get('install_id')}'."
                    )
                runtime_role = str(
                    matched_context.get("runtime_role")
                    or _load_profile(str(matched_context["profile"])).get("runtime_role")
                    or signed_artifact["product"]["runtime_roles"][0]
                )
                offline_lease_days = signed_artifact.get("controls", {}).get("offline_lease_days") or 30
                lease_expires_at = _future_iso(int(offline_lease_days))
                activation_lease = _issue_signed_activation_lease(
                    lease_id=f"lease_{uuid4().hex[:12]}",
                    license_id=str(matched_context["license_id"]),
                    install_id=str(matched_context.get("install_id") or matched_artifact["id"]),
                    runtime_role=runtime_role,
                    issued_at=reviewed_at,
                    not_before=reviewed_at,
                    lease_expires_at=lease_expires_at,
                    instance_id=matched_context.get("instance_id"),
                    device_id=matched_context.get("device_id"),
                    profile=str(matched_context["profile"]),
                    heartbeat_interval_hours=24 if signed_artifact.get("controls", {}).get("require_reporting", True) else None,
                    reporting=_as_dict(signed_artifact.get("reporting")),
                    attributes={"workspace_id": workspace_id},
                    extensions={"issued_by": "ophan"},
                )

            updated_context = {
                **matched_context,
                "last_reviewed_at": reviewed_at,
                "compliance_state": matched_context.get("compliance_state", "active"),
                **(
                    {
                        "last_validated_at": reviewed_at,
                        "lease_expires_at": activation_lease["lease_expires_at"],
                        "activation_lease_ref": f"ophan://leases/{activation_lease['lease_id']}",
                        "activation_lease_hash": _artifact_digest(activation_lease),
                        "activation_lease": activation_lease,
                    }
                    if activation_lease
                    else {}
                ),
            }
            updated_artifact = await _update_workspace_artifact(
                workspace_id,
                matched_artifact["id"],
                context=updated_context,
                content=(
                    f"Installation {updated_context.get('install_id', matched_artifact['id'])} reviewed at {reviewed_at}."
                ),
            )
            return _json_result(
                {
                    "status": "reviewed",
                    "workspace_id": workspace_id,
                    "installation_artifact_id": _artifact_id_from_response(updated_artifact),
                    "install_id": updated_context.get("install_id"),
                    "compliance_state": updated_context.get("compliance_state"),
                    **({"activation_lease": activation_lease} if activation_lease else {}),
                }
            )

        created_install_id = install_id or f"inst_{uuid4().hex[:12]}"
        activation_lease = None
        effective_profile = profile
        if license_id:
            _, linked_license_context = await _find_workspace_artifact(
                workspace_id,
                content_type=LICENSE_CARD_MIME,
                identity_key="license_id",
                identity_value=license_id,
            )
            signed_artifact = _get_signed_license_artifact(linked_license_context)
            if not effective_profile:
                effective_profile = (_as_string_list(linked_license_context.get("distribution_profiles")) or _as_string_list(signed_artifact["product"].get("distribution_profiles")) or [None])[0]
            if effective_profile:
                preflight = _licensing_service().preflight_license(str(effective_profile), signed_artifact)
                if not preflight.verification.valid or not preflight.compatibility.allowed:
                    raise OphanToolError(
                        f"Cannot issue activation lease for install '{created_install_id}'."
                    )
                runtime_role = str(
                    _load_profile(str(effective_profile)).get("runtime_role")
                    or signed_artifact["product"]["runtime_roles"][0]
                )
                offline_lease_days = signed_artifact.get("controls", {}).get("offline_lease_days") or 30
                activation_lease = _issue_signed_activation_lease(
                    lease_id=f"lease_{uuid4().hex[:12]}",
                    license_id=license_id,
                    install_id=created_install_id,
                    runtime_role=runtime_role,
                    issued_at=reviewed_at,
                    not_before=reviewed_at,
                    lease_expires_at=_future_iso(int(offline_lease_days)),
                    instance_id=instance_id,
                    device_id=device_id,
                    profile=str(effective_profile),
                    heartbeat_interval_hours=24 if signed_artifact.get("controls", {}).get("require_reporting", True) else None,
                    reporting=_as_dict(signed_artifact.get("reporting")),
                    attributes={"workspace_id": workspace_id},
                    extensions={"issued_by": "ophan"},
                )
            else:
                raise OphanToolError("A licensed installation review requires a profile.")

        installation_context = {
            "type": "license-installation",
            "title": _title_from_prefix("Installation", created_install_id),
            "content_type": INSTALLATION_CARD_MIME,
            "install_id": created_install_id,
            "license_id": license_id,
            "instance_id": instance_id,
            "device_id": device_id,
            "profile": effective_profile,
            "runtime_role": (
                activation_lease["runtime_role"]
                if activation_lease
                else "standard"
            ),
            "compliance_state": "active" if license_id else "needs-license",
            "last_validated_at": reviewed_at,
            "lease_expires_at": activation_lease["lease_expires_at"] if activation_lease else _future_iso(30),
            "last_reviewed_at": reviewed_at,
            **(
                {
                    "activation_lease_ref": f"ophan://leases/{activation_lease['lease_id']}",
                    "activation_lease_hash": _artifact_digest(activation_lease),
                    "activation_lease": activation_lease,
                }
                if activation_lease
                else {}
            ),
        }
        installation_artifact = await _create_workspace_artifact(
            workspace_id,
            installation_context,
            f"Installation {created_install_id} reviewed at {reviewed_at}.",
        )
        return _json_result(
            {
                "status": "observed",
                "workspace_id": workspace_id,
                "installation_artifact_id": _artifact_id_from_response(installation_artifact),
                "install_id": created_install_id,
                "compliance_state": installation_context["compliance_state"],
                **({"activation_lease": activation_lease} if activation_lease else {}),
            }
        )
    except OphanToolError as exc:
        return f"Error: {exc}"


@mcp.tool(description="Ingest or reconcile aggregate licensing and metering snapshots")
async def record_usage_snapshot(
    workspace_id: str,
    snapshot_artifact_id: Optional[str] = None,
    snapshot_payload: Optional[str] = None,
    account_id: Optional[str] = None,
    license_id: Optional[str] = None,
) -> str:
    try:
        granted = _resolve_operator_entitlements(workspace_id, "record_usage_snapshot")
        _require_any_licensing_entitlement(granted, "record_usage_snapshot")
        payload = _parse_json_argument(snapshot_payload, "snapshot_payload")
        captured_at = payload.get("captured_at") or _now_iso()
        usage_id = payload.get("usage_id") or f"usage_{uuid4().hex[:12]}"
        usage_context = {
            "type": "license-usage",
            "title": _title_from_prefix("Usage Snapshot", usage_id),
            "content_type": USAGE_CARD_MIME,
            "usage_id": usage_id,
            "account_id": account_id or payload.get("account_id"),
            "license_id": license_id or payload.get("license_id"),
            "captured_at": captured_at,
            "reporting_period": payload.get("reporting_period"),
            "usage": payload.get("usage", {}),
            "allowances": payload.get("allowances", {}),
            "source_artifact_id": snapshot_artifact_id,
            "state": "recorded",
        }
        usage_artifact = await _create_workspace_artifact(
            workspace_id,
            usage_context,
            f"Usage snapshot {usage_id} recorded at {captured_at}.",
        )

        created_cards = {"usage_artifact_id": _artifact_id_from_response(usage_artifact)}
        overages = payload.get("overages")
        if isinstance(overages, dict) and overages:
            event_context = {
                "type": "license-event",
                "title": _title_from_prefix("Licensing Event", f"usage-{usage_id}"),
                "content_type": EVENT_CARD_MIME,
                "event_type": "usage_threshold_warning",
                "severity": "warning",
                "license_id": usage_context.get("license_id"),
                "account_id": usage_context.get("account_id"),
                "created_at": captured_at,
                "details": overages,
            }
            event_artifact = await _create_workspace_artifact(
                workspace_id,
                event_context,
                f"Usage snapshot {usage_id} recorded overage indicators: {json.dumps(overages, sort_keys=True)}",
            )
            created_cards["event_artifact_id"] = _artifact_id_from_response(event_artifact)

        return _json_result(
            {
                "status": "recorded",
                "workspace_id": workspace_id,
                "usage_id": usage_id,
                "created_cards": created_cards,
            }
        )
    except OphanToolError as exc:
        return f"Error: {exc}"


@mcp.tool(description="Produce entitlement, installation, renewal, or overage report cards")
async def run_licensing_report(
    workspace_id: str,
    report_type: str = "entitlements",
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> str:
    try:
        granted = _resolve_operator_entitlements(workspace_id, "run_licensing_report")
        _require_entitlements(granted, ADVANCED_OPERATIONS_ENTITLEMENTS, "run_licensing_report")

        cards = await _list_workspace_artifacts(workspace_id)
        family = {
            ENTITLEMENT_CARD_MIME: "entitlements",
            LICENSE_CARD_MIME: "licenses",
            INSTALLATION_CARD_MIME: "installations",
            USAGE_CARD_MIME: "usage_snapshots",
            EVENT_CARD_MIME: "events",
        }
        counts = {value: 0 for value in family.values()}
        active_licenses = 0
        revoked_licenses = 0
        warnings = 0
        for card in cards:
            context = _parse_artifact_context(card)
            content_type = context.get("content_type")
            bucket = family.get(content_type)
            if bucket:
                counts[bucket] += 1
            if content_type == LICENSE_CARD_MIME and context.get("state") == "active":
                active_licenses += 1
            if content_type == LICENSE_CARD_MIME and context.get("state") == "revoked":
                revoked_licenses += 1
            if content_type == EVENT_CARD_MIME and context.get("severity") == "warning":
                warnings += 1

        generated_at = _now_iso()
        markdown = "\n".join(
            [
                f"# Licensing Report — {report_type}",
                "",
                f"Generated: {generated_at}",
                f"Window: {since or 'beginning'} to {until or 'now'}",
                "",
                "## Summary",
                f"- Active licenses: {active_licenses}",
                f"- Revoked licenses: {revoked_licenses}",
                f"- Warning events: {warnings}",
                "",
                "## Card Counts",
                *[f"- {label.replace('_', ' ').title()}: {count}" for label, count in counts.items()],
            ]
        )
        report_context = {
            "type": "licensing-report",
            "title": f"Licensing Report — {report_type}",
            "content_type": "text/markdown",
            "report_type": report_type,
            "generated_at": generated_at,
            "since": since,
            "until": until,
            "counts": counts,
            "active_licenses": active_licenses,
            "revoked_licenses": revoked_licenses,
            "warning_events": warnings,
        }
        report_artifact = await _create_workspace_artifact(workspace_id, report_context, markdown)
        return _json_result(
            {
                "status": "generated",
                "workspace_id": workspace_id,
                "report_type": report_type,
                "report_artifact_id": _artifact_id_from_response(report_artifact),
                "summary": report_context,
            }
        )
    except OphanToolError as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# LLM Usage Metering
# ---------------------------------------------------------------------------

# VU (Value Units) conversion rates per 1K tokens, keyed by model prefix
_VU_RATES: dict[str, float] = {
    "gpt-5": 1.0,
    "gpt-4o": 2.0,
    "gpt-4": 3.0,
    "claude-sonnet": 2.0,
    "claude-opus": 5.0,
    "claude-haiku": 0.5,
}

# Tier rate limits
_TIER_LIMITS: dict[str, dict[str, int]] = {
    "free": {"requests_per_minute": 10, "tokens_per_minute": 10000, "tokens_per_day": 100000, "vu_per_month": 100},
    "pro": {"requests_per_minute": 60, "tokens_per_minute": 100000, "tokens_per_day": 2000000, "vu_per_month": 2000},
    "power": {"requests_per_minute": 120, "tokens_per_minute": 500000, "tokens_per_day": 10000000, "vu_per_month": 10000},
}

# In-memory usage counters (per-process; production should use persistent storage)
_usage_counters: dict[str, dict[str, Any]] = {}


def _get_vu_rate(model: str) -> float:
    """Look up VU rate for a model by matching prefix."""
    model_lower = model.lower()
    for prefix, rate in _VU_RATES.items():
        if model_lower.startswith(prefix):
            return rate
    return 2.0  # default rate


def _get_user_counter(user_id: str) -> dict[str, Any]:
    """Get or create a per-user usage counter, resetting expired windows."""
    if user_id not in _usage_counters:
        _usage_counters[user_id] = {
            "requests_this_minute": 0,
            "tokens_this_minute": 0,
            "tokens_this_day": 0,
            "vu_this_month": 0.0,
            "last_minute_reset": _now_iso(),
            "last_day_reset": _now_iso(),
            "last_month_reset": _now_iso(),
        }
    counter = _usage_counters[user_id]
    _maybe_reset_counters(counter)
    return counter


def _maybe_reset_counters(counter: dict[str, Any]) -> None:
    """Reset rate-limit windows that have elapsed."""
    now = datetime.now(timezone.utc)
    try:
        last_minute = datetime.fromisoformat(counter["last_minute_reset"].replace("Z", "+00:00"))
        if (now - last_minute).total_seconds() >= 60:
            counter["requests_this_minute"] = 0
            counter["tokens_this_minute"] = 0
            counter["last_minute_reset"] = _now_iso()
    except (ValueError, KeyError):
        counter["last_minute_reset"] = _now_iso()

    try:
        last_day = datetime.fromisoformat(counter["last_day_reset"].replace("Z", "+00:00"))
        if (now - last_day).total_seconds() >= 86400:
            counter["tokens_this_day"] = 0
            counter["last_day_reset"] = _now_iso()
    except (ValueError, KeyError):
        counter["last_day_reset"] = _now_iso()

    try:
        last_month = datetime.fromisoformat(counter["last_month_reset"].replace("Z", "+00:00"))
        if (now - last_month).days >= 30:
            counter["vu_this_month"] = 0.0
            counter["last_month_reset"] = _now_iso()
    except (ValueError, KeyError):
        counter["last_month_reset"] = _now_iso()


@mcp.tool(
    description=(
        "Check whether an LLM invocation is allowed given the user's current usage "
        "and the connection's tier/rate limits. Call before each LLM invocation."
    )
)
async def check_llm_allowance(
    user_id: str,
    tier: str = "free",
    estimated_tokens: int = 0,
) -> str:
    """Pre-invocation rate limit check.

    Args:
        user_id: ID of the user making the request.
        tier: Subscription tier (free, pro, power, custom).
        estimated_tokens: Estimated tokens for the upcoming request.
    """
    limits = _TIER_LIMITS.get(tier, _TIER_LIMITS["free"])
    counter = _get_user_counter(user_id)

    # Check requests per minute
    if counter["requests_this_minute"] >= limits["requests_per_minute"]:
        return json.dumps({
            "allowed": False,
            "reason": f"Rate limit exceeded: {limits['requests_per_minute']} requests/minute for {tier} tier.",
            "remaining_vu": max(0, limits["vu_per_month"] - counter["vu_this_month"]),
        })

    # Check tokens per day
    if counter["tokens_this_day"] + estimated_tokens > limits["tokens_per_day"]:
        return json.dumps({
            "allowed": False,
            "reason": f"Daily token limit exceeded for {tier} tier.",
            "remaining_vu": max(0, limits["vu_per_month"] - counter["vu_this_month"]),
        })

    return json.dumps({
        "allowed": True,
        "reason": None,
        "remaining_vu": max(0, limits["vu_per_month"] - counter["vu_this_month"]),
    })


@mcp.tool(
    description=(
        "Record LLM token usage after a successful invocation. Converts tokens to VU "
        "(Value Units) and updates the user's usage counters."
    )
)
async def record_llm_usage(
    user_id: str,
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    workspace_id: Optional[str] = None,
) -> str:
    """Post-invocation metering.

    Args:
        user_id: ID of the user who made the request.
        provider: LLM provider (openai, anthropic, etc.).
        model: Model identifier.
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens generated.
        workspace_id: Workspace where the invocation occurred.
    """
    total_tokens = input_tokens + output_tokens
    vu_rate = _get_vu_rate(model)
    vu_consumed = (total_tokens / 1000.0) * vu_rate

    counter = _get_user_counter(user_id)
    counter["requests_this_minute"] += 1
    counter["tokens_this_minute"] += total_tokens
    counter["tokens_this_day"] += total_tokens
    counter["vu_this_month"] += vu_consumed

    log.info(
        "LLM usage recorded � user=%s provider=%s model=%s tokens=%d vu=%.2f",
        user_id, provider, model, total_tokens, vu_consumed,
    )

    return json.dumps({
        "status": "recorded",
        "tokens_consumed": total_tokens,
        "vu_consumed": round(vu_consumed, 2),
        "vu_this_month": round(counter["vu_this_month"], 2),
    })


# ---------------------------------------------------------------------------
# SaaS Billing (Stripe) — plan definitions, checkout, sync
# ---------------------------------------------------------------------------

_PLAN_LIMITS: dict[str, dict[str, int]] = {
    "free":  {"max_workspaces": 1,  "max_artifacts": 500,    "vu_limit": 100},
    "pro":   {"max_workspaces": 3,  "max_artifacts": 10_000, "vu_limit": 2_000},
    "power": {"max_workspaces": 10, "max_artifacts": 100_000, "vu_limit": 10_000},
}

# VUs granted per purchased top-up pack
VU_TOPUP_PACK_SIZE = 500


def _stripe_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY)


def _get_stripe():
    import stripe as _stripe
    _stripe.api_key = STRIPE_SECRET_KEY
    return _stripe


def _price_id_to_plan(price_id: str) -> str:
    """Reverse-map a Stripe price ID to a plan key."""
    if price_id == STRIPE_PRICE_ID_PRO:
        return "pro"
    if price_id == STRIPE_PRICE_ID_POWER:
        return "power"
    return "free"


def _limits_to_plan(limits: dict) -> str:
    """Reverse-map numeric limits to a plan name (best guess for display)."""
    for plan, plan_limits in _PLAN_LIMITS.items():
        if plan_limits.get("vu_limit") == limits.get("vu_limit"):
            return plan
    return "free"


async def _sync_limits_to_core(person_id: str, plan: str) -> None:
    """Push numeric limits to Core's entitlement cache. The ONE sync channel."""
    limits = _PLAN_LIMITS.get(plan, _PLAN_LIMITS["free"])
    try:
        await _request("POST", "/internal/gate/set-limits", {
            "person_id": person_id,
            **limits,
        })
    except Exception as exc:
        log.error("Failed to sync limits to Core for person=%s plan=%s: %s", person_id, plan, exc)


async def _add_vu_to_core(person_id: str, vu_amount: int) -> None:
    """Additively increase the VU limit for a person (top-up). Reads current limits first."""
    try:
        usage_data = await _request("GET", f"/internal/gate/usage/{person_id}")
        limits = usage_data.get("limits", _PLAN_LIMITS["free"])
        current_vu = limits.get("vu_limit") or _PLAN_LIMITS["free"]["vu_limit"]
        await _request("POST", "/internal/gate/set-limits", {
            "person_id": person_id,
            "max_workspaces": limits.get("max_workspaces"),
            "max_artifacts": limits.get("max_artifacts"),
            "vu_limit": current_vu + vu_amount,
        })
        log.info("Added %s VU credits for person=%s (new limit=%s)", vu_amount, person_id, current_vu + vu_amount)
    except Exception as exc:
        log.error("Failed to add VU credits for person=%s amount=%s: %s", person_id, vu_amount, exc)


@mcp.tool(description="Create a Stripe Checkout Session for a subscription upgrade. Returns the checkout URL.")
async def create_checkout_session(
    plan: str,
    person_id: str,
    email: Optional[str] = None,
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
) -> str:
    """Start a Stripe subscription checkout flow.

    Args:
        plan: Target plan key ('pro' or 'power').
        person_id: The person upgrading.
        email: Pre-fill email on checkout page.
        success_url: Redirect after successful checkout.
        cancel_url: Redirect on cancel.
    """
    if not _stripe_enabled():
        raise OphanToolError("Stripe billing is not configured.")

    price_map = {"pro": STRIPE_PRICE_ID_PRO, "power": STRIPE_PRICE_ID_POWER}
    price_id = price_map.get(plan)
    if not price_id:
        raise OphanToolError(f"Unknown plan '{plan}'. Must be 'pro' or 'power'.")

    stripe = _get_stripe()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        metadata={"person_id": person_id, "plan_key": plan},
        customer_email=email,
        success_url=success_url or f"{AGIENCE_API_URI.replace(':8081', ':5173')}/settings?billing=success",
        cancel_url=cancel_url or f"{AGIENCE_API_URI.replace(':8081', ':5173')}/settings?billing=cancel",
    )
    return _json_result({"status": "checkout_created", "checkout_url": session.url, "session_id": session.id})


@mcp.tool(description="Create a Stripe Customer Portal session for subscription management. Returns the portal URL.")
async def create_portal_session(
    stripe_customer_id: str,
    return_url: Optional[str] = None,
) -> str:
    """Open the Stripe billing portal for an existing customer.

    Args:
        stripe_customer_id: The Stripe customer ID.
        return_url: URL to redirect after portal session.
    """
    if not _stripe_enabled():
        raise OphanToolError("Stripe billing is not configured.")

    stripe = _get_stripe()
    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=return_url or f"{AGIENCE_API_URI.replace(':8081', ':5173')}/settings",
    )
    return _json_result({"status": "portal_created", "portal_url": session.url})


@mcp.tool(description="Create a Stripe Checkout Session for a one-time VU top-up purchase. Returns the checkout URL.")
async def create_vu_topup(
    person_id: str,
    stripe_customer_id: Optional[str] = None,
    quantity: int = 1,
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
) -> str:
    """Purchase additional VU credits via Stripe one-time payment.

    Args:
        person_id: The person buying credits.
        stripe_customer_id: Existing Stripe customer ID (optional).
        quantity: Number of VU packs to buy.
        success_url: Redirect after payment.
        cancel_url: Redirect on cancel.
    """
    if not _stripe_enabled():
        raise OphanToolError("Stripe billing is not configured.")
    if not STRIPE_PRICE_ID_VU_TOPUP:
        raise OphanToolError("VU top-up pricing is not configured.")

    stripe = _get_stripe()
    params: dict[str, Any] = {
        "mode": "payment",
        "line_items": [{"price": STRIPE_PRICE_ID_VU_TOPUP, "quantity": quantity}],
        "metadata": {"person_id": person_id, "topup": "vu", "topup_quantity": str(quantity)},
        "success_url": success_url or f"{AGIENCE_API_URI.replace(':8081', ':5173')}/settings?vu=success",
        "cancel_url": cancel_url or f"{AGIENCE_API_URI.replace(':8081', ':5173')}/settings?vu=cancel",
    }
    if stripe_customer_id:
        params["customer"] = stripe_customer_id
    session = stripe.checkout.Session.create(**params)
    return _json_result({"status": "topup_created", "checkout_url": session.url, "session_id": session.id})


@mcp.tool(description="Activate a subscription after Stripe checkout. Creates artifacts and syncs limits to Core.")
async def activate_subscription(
    person_id: str,
    plan: str,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
) -> str:
    """Record a new subscription activation.

    Creates entitlement + subscription + event artifacts in the user's inbox,
    then syncs numeric limits to Core's gate.

    Args:
        person_id: The person whose subscription was activated.
        plan: Plan key ('pro' or 'power').
        stripe_customer_id: Stripe customer ID.
        stripe_subscription_id: Stripe subscription ID.
    """
    issued_at = _now_iso()
    limits = _PLAN_LIMITS.get(plan, _PLAN_LIMITS["free"])

    # 1. Entitlement artifact
    entitlement_context = {
        "content_type": ENTITLEMENT_CARD_MIME,
        "type": "entitlement",
        "entitlement_id": f"ent_{uuid4().hex[:12]}",
        "account_id": person_id,
        "plan": plan,
        "status": "active",
        "limits": limits,
        "issued_at": issued_at,
    }
    await _create_workspace_artifact(person_id, entitlement_context, f"SaaS {plan} plan entitlement activated.")

    # 2. Subscription artifact
    sub_context = {
        "content_type": SUBSCRIPTION_CARD_MIME,
        "type": "subscription",
        "subscription_id": f"sub_{uuid4().hex[:12]}",
        "account_id": person_id,
        "plan": plan,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "status": "active",
        "activated_at": issued_at,
    }
    await _create_workspace_artifact(person_id, sub_context, f"Stripe subscription for {plan} plan.")

    # 3. Event artifact
    event_context = {
        "content_type": EVENT_CARD_MIME,
        "type": "license-event",
        "event_type": "subscription_activated",
        "account_id": person_id,
        "plan": plan,
        "timestamp": issued_at,
        "severity": "info",
    }
    await _create_workspace_artifact(person_id, event_context, f"Subscription activated: {plan} plan.")

    # 4. Sync limits to Core
    await _sync_limits_to_core(person_id, plan)

    return _json_result({"status": "activated", "person_id": person_id, "plan": plan})


@mcp.tool(description="Update a subscription (plan change or cancellation). Syncs new limits to Core.")
async def update_subscription(
    person_id: str,
    new_plan: str,
) -> str:
    """Handle a subscription plan change or cancellation.

    Args:
        person_id: The person whose subscription changed.
        new_plan: New plan key ('free' for cancellation, 'pro', 'power').
    """
    event_context = {
        "content_type": EVENT_CARD_MIME,
        "type": "license-event",
        "event_type": "subscription_updated",
        "account_id": person_id,
        "new_plan": new_plan,
        "timestamp": _now_iso(),
        "severity": "info",
    }
    await _create_workspace_artifact(person_id, event_context, f"Subscription updated to {new_plan}.")
    await _sync_limits_to_core(person_id, new_plan)
    return _json_result({"status": "updated", "person_id": person_id, "plan": new_plan})


async def _dispatch_stripe_event(event_type: str, event_data: str) -> str:
    """Internal Stripe event dispatcher — called only from _handle_stripe_webhook_http.

    NOT exposed as an MCP tool to prevent unauthorized invocation by MCP clients.
    The HTTP endpoint validates the Stripe-Signature before calling this.
    """
    data = json.loads(event_data) if isinstance(event_data, str) else event_data

    if event_type == "checkout.session.completed":
        person_id = data.get("metadata", {}).get("person_id")
        plan = data.get("metadata", {}).get("plan_key", "pro")
        if not person_id:
            return _json_result({"status": "skipped", "reason": "No person_id in metadata"})

        # VU top-up (one-time payment) — actually increment the limit in Core
        if data.get("mode") == "payment" and data.get("metadata", {}).get("topup") == "vu":
            quantity = int(data.get("metadata", {}).get("topup_quantity", "1"))
            vu_added = quantity * VU_TOPUP_PACK_SIZE
            await _add_vu_to_core(person_id, vu_added)
            return _json_result({"status": "vu_topup_applied", "person_id": person_id, "vu_added": vu_added})

        # Subscription checkout
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("subscription")
        return await activate_subscription(
            person_id=person_id,
            plan=plan,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
        )

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        person_id = data.get("metadata", {}).get("person_id")
        if not person_id:
            return _json_result({"status": "skipped", "reason": "No person_id in metadata"})

        if event_type == "customer.subscription.deleted":
            return await update_subscription(person_id=person_id, new_plan="free")

        # Determine new plan from price
        items = data.get("items", {}).get("data", [])
        plan = "free"
        if items:
            price_id = items[0].get("price", {}).get("id", "")
            plan = _price_id_to_plan(price_id)
        return await update_subscription(person_id=person_id, new_plan=plan)

    return _json_result({"status": "ignored", "event_type": event_type})


@mcp.tool(
    description="Fetch billing summary: plan, limits, and current usage. Called by the billing settings UI."
)
async def get_billing_summary(person_id: str) -> str:
    """Return plan + limits + usage for the billing settings view.

    Reads from Core's gate service and reverse-maps limits to plan name.

    Args:
        person_id: The person to query.
    """
    try:
        usage_data = await _request("GET", f"/internal/gate/usage/{person_id}")
    except Exception:
        # Gate service not available or person has no data — return free defaults
        usage_data = {
            "limits": _PLAN_LIMITS["free"],
            "usage": {"workspaces": 0, "artifacts": 0, "vu": 0},
        }

    limits = usage_data.get("limits", _PLAN_LIMITS["free"])
    usage = usage_data.get("usage", {})
    plan = _limits_to_plan(limits)

    # Look up Stripe customer ID from subscription artifacts in person's inbox
    stripe_customer_id: Optional[str] = None
    try:
        cards = await _list_workspace_artifacts(person_id)
        for card in cards:
            ctx = _parse_artifact_context(card)
            if ctx.get("content_type") == SUBSCRIPTION_CARD_MIME:
                stripe_customer_id = ctx.get("stripe_customer_id")
                break
    except Exception:
        pass

    return _json_result({
        "plan": plan,
        "max_workspaces": limits.get("max_workspaces"),
        "max_artifacts": limits.get("max_artifacts"),
        "vu_included": limits.get("vu_limit"),
        "workspaces_used": usage.get("workspaces", 0),
        "artifacts_used": usage.get("artifacts", 0),
        "vu_used": usage.get("vu", 0),
        "vu_remaining": max(0, (limits.get("vu_limit") or 0) - usage.get("vu", 0)),
        "stripe_customer_id": stripe_customer_id,
    })


# ---------------------------------------------------------------------------
# Stripe webhook HTTP endpoint (outside MCP — raw ASGI)
# ---------------------------------------------------------------------------

# In-memory idempotency set — deduplicates Stripe retries within a process lifetime.
# Capped to prevent unbounded growth; evicted on cap (rare for well-behaved webhooks).
_processed_stripe_events: set[str] = set()
_PROCESSED_EVENTS_MAX = 10_000


async def _handle_stripe_webhook_http(scope: dict, receive: Any, send: Any) -> None:
    """Handle POST /webhooks/stripe — validates Stripe-Signature and dispatches."""
    body_parts = []
    while True:
        msg = await receive()
        body_parts.append(msg.get("body", b""))
        if not msg.get("more_body", False):
            break
    body = b"".join(body_parts)

    # Extract Stripe-Signature header
    sig_header = None
    for header_name, header_value in scope.get("headers", []):
        if header_name == b"stripe-signature":
            sig_header = header_value.decode("utf-8")
            break

    if not sig_header or not STRIPE_WEBHOOK_SECRET:
        resp_body = json.dumps({"error": "Missing signature or webhook secret not configured"}).encode()
        await send({"type": "http.response.start", "status": 400,
                     "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(resp_body)).encode())]})
        await send({"type": "http.response.body", "body": resp_body})
        return

    try:
        import stripe as _stripe
        _stripe.api_key = STRIPE_SECRET_KEY
        event = _stripe.Webhook.construct_event(body, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as exc:
        resp_body = json.dumps({"error": f"Webhook verification failed: {exc}"}).encode()
        await send({"type": "http.response.start", "status": 400,
                     "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(resp_body)).encode())]})
        await send({"type": "http.response.body", "body": resp_body})
        return

    # Idempotency check — skip already-processed events (handles Stripe retries)
    event_id = event.get("id", "")
    if event_id and event_id in _processed_stripe_events:
        resp_body = json.dumps({"status": "already_processed", "event_id": event_id}).encode()
        await send({"type": "http.response.start", "status": 200,
                     "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(resp_body)).encode())]})
        await send({"type": "http.response.body", "body": resp_body})
        return

    if event_id:
        if len(_processed_stripe_events) >= _PROCESSED_EVENTS_MAX:
            _processed_stripe_events.clear()
        _processed_stripe_events.add(event_id)

    # Dispatch to internal handler (NOT an MCP tool — Stripe-Signature already verified above)
    try:
        result = await _dispatch_stripe_event(
            event_type=event["type"],
            event_data=json.dumps(event["data"]["object"]),
        )
        resp_body = result.encode() if isinstance(result, str) else json.dumps(result).encode()
    except Exception as exc:
        log.exception("Stripe webhook processing error")
        resp_body = json.dumps({"error": str(exc)}).encode()

    await send({"type": "http.response.start", "status": 200,
                 "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(resp_body)).encode())]})
    await send({"type": "http.response.body", "body": resp_body})


# ---------------------------------------------------------------------------
# UI Resources
# ---------------------------------------------------------------------------

@mcp.resource("ui://ophan/billing-settings.html")
async def billing_settings_html() -> str:
    """Serve the billing settings page for the platform shell."""
    view_path = Path(__file__).parent / "ui" / "billing" / "billing-settings.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://ophan/vnd.agience.account.html")
async def account_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.account+json."""
    view_path = Path(__file__).parent / "ui" / "application" / "vnd.agience.account+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://ophan/vnd.agience.entitlement.html")
async def entitlement_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.entitlement+json."""
    view_path = Path(__file__).parent / "ui" / "application" / "vnd.agience.entitlement+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://ophan/vnd.agience.invoice.html")
async def invoice_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.invoice+json."""
    view_path = Path(__file__).parent / "ui" / "application" / "vnd.agience.invoice+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://ophan/vnd.agience.license.html")
async def license_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.license+json."""
    view_path = Path(__file__).parent / "ui" / "application" / "vnd.agience.license+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://ophan/vnd.agience.license-event.html")
async def license_event_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.license-event+json."""
    view_path = Path(__file__).parent / "ui" / "application" / "vnd.agience.license-event+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://ophan/vnd.agience.license-installation.html")
async def license_installation_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.license-installation+json."""
    view_path = Path(__file__).parent / "ui" / "application" / "vnd.agience.license-installation+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://ophan/vnd.agience.license-usage.html")
async def license_usage_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.license-usage+json."""
    view_path = Path(__file__).parent / "ui" / "application" / "vnd.agience.license-usage+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://ophan/vnd.agience.market.html")
async def market_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.market+json."""
    view_path = Path(__file__).parent / "ui" / "application" / "vnd.agience.market+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://ophan/vnd.agience.portfolio.html")
async def portfolio_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.portfolio+json."""
    view_path = Path(__file__).parent / "ui" / "application" / "vnd.agience.portfolio+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://ophan/vnd.agience.transaction.html")
async def transaction_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.transaction+json."""
    view_path = Path(__file__).parent / "ui" / "application" / "vnd.agience.transaction+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting agience-server-ophan � transport=%s port=%s", MCP_TRANSPORT, MCP_PORT)
    if MCP_TRANSPORT == "streamable-http":
        import uvicorn
        uvicorn.run(create_server_app(), host=MCP_HOST, port=MCP_PORT)
    else:
        mcp.run()
