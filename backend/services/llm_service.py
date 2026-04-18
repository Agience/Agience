"""
LLM Service -- per-workspace LLM configuration, key resolution, and multi-provider dispatch.

Handles:
- Per-workspace LLM configuration (provider, model, key binding)
- Fallback chain: workspace config -> user default -> platform settings -> env
- Multi-provider dispatch (OpenAI, Anthropic, Google, Ollama)
- Single ``complete()`` entry point for all platform LLM calls

Key storage: delegated to secrets_service (person.preferences.secrets, type="llm_key").
Key CRUD endpoints: served by secrets_router (generic /secrets API).
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple
from arango.database import StandardDatabase

from services import workspace_service as ws_svc
from services import secrets_service
from core import config
from core.dependencies import get_arango_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Provider dispatch helpers (lazy-imported SDKs to avoid import errors)
# ---------------------------------------------------------------------------

def _dispatch_openai(
    model: str,
    messages: list[dict],
    api_key: Optional[str],
    temperature: float,
    max_output_tokens: int,
) -> Tuple[str, Any]:
    from services.openai_helpers import create_chat_completion
    return create_chat_completion(
        model=model,
        messages=messages,
        api_key=api_key,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


def _dispatch_anthropic(
    model: str,
    messages: list[dict],
    api_key: Optional[str],
    temperature: float,
    max_output_tokens: int,
) -> Tuple[str, Any]:
    import anthropic

    # Anthropic requires system as a separate param, not in messages list
    system_parts = []
    non_system = []
    for msg in messages:
        if msg.get("role") == "system":
            system_parts.append(msg.get("content", ""))
        else:
            non_system.append({"role": msg["role"], "content": msg.get("content", "")})

    client = anthropic.Anthropic(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": non_system or [{"role": "user", "content": ""}],
        "max_tokens": max_output_tokens,
        "temperature": temperature,
    }
    if system_parts:
        kwargs["system"] = "\n\n".join(system_parts)

    resp = client.messages.create(**kwargs)
    text = ""
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "")
    return text.strip(), resp


def _dispatch_google(
    model: str,
    messages: list[dict],
    api_key: Optional[str],
    temperature: float,
    max_output_tokens: int,
) -> Tuple[str, Any]:
    from google import genai
    from google.genai import types

    # Build contents — system messages become system_instruction
    system_parts = []
    contents = []
    for msg in messages:
        role = msg.get("role", "user")
        content_text = msg.get("content", "")
        if role == "system":
            system_parts.append(content_text)
        else:
            # Google uses "model" instead of "assistant"
            g_role = "model" if role == "assistant" else "user"
            contents.append(types.Content(
                role=g_role,
                parts=[types.Part(text=content_text)],
            ))

    client = genai.Client(api_key=api_key)
    gen_config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    if system_parts:
        gen_config.system_instruction = "\n\n".join(system_parts)

    resp = client.models.generate_content(
        model=model,
        contents=contents or [types.Content(role="user", parts=[types.Part(text="")])],
        config=gen_config,
    )
    text = getattr(resp, "text", "") or ""
    return text.strip(), resp


def _dispatch_ollama(
    model: str,
    messages: list[dict],
    temperature: float,
    max_output_tokens: int,
) -> Tuple[str, Any]:
    """Ollama uses OpenAI-compatible API — no API key needed."""
    import openai as openai_sdk
    from services.openai_helpers import create_chat_completion

    client = openai_sdk.OpenAI(base_url=config.OLLAMA_BASE_URL, api_key="ollama")
    return create_chat_completion(
        model=model,
        messages=messages,
        client=client,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


# ---------------------------------------------------------------------------
#  Key resolution helpers
# ---------------------------------------------------------------------------

_ENV_KEY_MAP: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def _resolve_env_key(provider: str) -> Optional[str]:
    """Return the environment API key for the given provider, or None."""
    attr = _ENV_KEY_MAP.get(provider)
    return getattr(config, attr, None) if attr else None


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def complete(
    db: StandardDatabase,
    user_id: str,
    messages: list[dict],
    *,
    workspace_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_output_tokens: int = 1024,
) -> Tuple[str, Any]:
    """Single entry point for all platform LLM calls.

    Resolution cascade (first non-None wins):
    1. Explicit ``provider``/``model`` params (caller override)
    2. Workspace context ``llm.provider`` / ``llm.model``
    3. Platform settings ``ai.default_provider`` / ``ai.default_model``
    4. Hard fallback: ``"openai"`` / ``"gpt-4o-mini"``

    Returns (text, raw_response) matching openai_helpers contract.
    """
    # --- resolve provider & model via cascade ---
    resolved_provider = provider
    resolved_model = model

    if workspace_id and (resolved_provider is None or resolved_model is None):
        try:
            context = ws_svc.get_workspace_context(db, user_id, workspace_id)
            llm_cfg = context.get("llm", {}) if isinstance(context, dict) else {}
            if resolved_provider is None:
                resolved_provider = llm_cfg.get("provider")
            if resolved_model is None:
                resolved_model = llm_cfg.get("model")
        except Exception as e:
            logger.warning("Failed to read workspace LLM config: %s", e)

    if resolved_provider is None:
        resolved_provider = config.AI_DEFAULT_PROVIDER or "openai"
    if resolved_model is None:
        resolved_model = config.AI_DEFAULT_MODEL or "gpt-4o-mini"

    # --- resolve API key ---
    api_key: Optional[str] = None
    if resolved_provider != "ollama":
        if workspace_id:
            api_key = get_llm_key_for_workspace(db, user_id, workspace_id, resolved_provider)
        else:
            # Try user default, then env
            try:
                api_key = secrets_service.get_secret_value(
                    db, user_id,
                    secret_type="llm_key", provider=resolved_provider,
                )
            except Exception:
                pass
            if not api_key:
                api_key = _resolve_env_key(resolved_provider)
        if not api_key:
            logger.warning(
                "No API key for provider %s — SDK will attempt its own resolution",
                resolved_provider,
            )

    # --- dispatch to provider ---
    if resolved_provider == "anthropic":
        return _dispatch_anthropic(resolved_model, messages, api_key, temperature, max_output_tokens)
    elif resolved_provider == "google":
        return _dispatch_google(resolved_model, messages, api_key, temperature, max_output_tokens)
    elif resolved_provider == "ollama":
        return _dispatch_ollama(resolved_model, messages, temperature, max_output_tokens)
    else:
        # Default: OpenAI (covers "openai" and any unknown provider)
        return _dispatch_openai(resolved_model, messages, api_key, temperature, max_output_tokens)


def get_llm_key_for_workspace(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    provider: str = "openai",
) -> Optional[str]:
    """
    Get decrypted API key for workspace LLM invocation.

    Fallback chain:
    1. Workspace-specific key (workspace card context `llm.key_id`)
    2. User's default key for provider (type="llm_key")
    3. Agience default from environment
    """
    arango_db: StandardDatabase = next(get_arango_db())

    # 1. Check workspace-specific LLM config
    try:
        context = ws_svc.get_workspace_context(db, user_id, workspace_id)
        llm_config = context.get("llm", {}) if isinstance(context, dict) else {}

        if llm_config.get("key_id"):
            val = secrets_service.get_secret_value(
                arango_db, user_id,
                secret_type="llm_key",
                provider=provider,
                secret_id=llm_config["key_id"],
            )
            if val:
                return val
    except Exception as e:
        logger.warning("Failed to get workspace LLM config: %s", e)

    # 2. User's default key for provider
    try:
        val = secrets_service.get_secret_value(
            arango_db, user_id, secret_type="llm_key", provider=provider
        )
        if val:
            return val
    except Exception as e:
        logger.warning("Failed to get user default LLM key: %s", e)

    # 3. Agience default from environment
    env_key = _resolve_env_key(provider)
    if env_key:
        return env_key

    logger.warning("No API key found for provider %s", provider)
    return None


def set_workspace_llm(
    db: StandardDatabase,
    user_id: str,
    workspace_id: str,
    provider: str,
    model: str,
    key_id: Optional[str] = None,
):
    """
    Configure LLM for workspace.

    Args:
        db: Database session
        user_id: User ID
        workspace_id: Workspace ID
        provider: LLM provider (openai, anthropic, etc.)
        model: Model name (gpt-4, claude-3-opus, etc.)
        key_id: Optional secret ID (uses default if None)
    """
    context = ws_svc.get_workspace_context(db, user_id, workspace_id)
    if not isinstance(context, dict):
        context = {}

    context["llm"] = {
        "provider": provider,
        "model": model,
        "key_id": key_id,
    }

    ws_svc.update_workspace_context(db, user_id, workspace_id, context)


def clear_workspace_llm(db: StandardDatabase, user_id: str, workspace_id: str):
    """Remove workspace-specific LLM config (falls back to user/Agience defaults)."""
    context = ws_svc.get_workspace_context(db, user_id, workspace_id)
    if not isinstance(context, dict):
        return

    if "llm" in context:
        del context["llm"]
        ws_svc.update_workspace_context(db, user_id, workspace_id, context)
