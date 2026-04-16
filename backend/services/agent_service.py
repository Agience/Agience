# services/agent_service.py
import logging
from typing import List, Optional, Dict, Any, Callable
from arango.database import StandardDatabase
from api.agents.invoke import InvokeResult
from api.agents.contracts import AgentRequest, AgentResponse
from services import llm_service

logger = logging.getLogger(__name__)

def invoke(
    *,
    db: StandardDatabase,
    user_id: str,
    input: Optional[str] = None,
    context: Optional[List[str]] = None,
    instructions: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
    model: str = "gpt-4o-mini",
    # Task agent mode (optional)
    arango_db=None,
    agent: Optional[Callable[..., object]] = None,
    agent_params: Optional[Dict[str, Any]] = None,
) -> InvokeResult | object:
    """Unified agent invocation.

    - Task agents: provide `agent` (callable) and `agent_params`; executes directly and returns structured result.
    - LLM agents: provide `input` and optional context/instructions/capabilities; returns text output.
    """

    # Task agent dispatch
    if agent is not None:
        params = agent_params or {}
        return agent(
            db=db,
            arango_db=arango_db,
            user_id=user_id,
            **params,
        )

    if input is None:
        raise ValueError("Either agent or input must be provided")

    # Gate: enforce VU limit for LLM invocations
    from core import config
    vu_cost = 5  # 1 standard agent run = 5 VU
    current_month = None
    if config.BILLING_ENFORCEMENT_ENABLED:
        from datetime import datetime, timezone
        from fastapi import HTTPException, status
        from services import gate_service
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")
        limits = gate_service.get_or_default_limits(db, user_id)
        vu_used = gate_service.get_tally(db, user_id, "vu", current_month)
        if limits["vu_limit"] is not None and vu_used + vu_cost > limits["vu_limit"]:
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                detail={"code": "VU_LIMIT", "limit": limits["vu_limit"], "used": vu_used},
                headers={"X-Upgrade-Reason": "vu_limit"},
            )

    # Build messages for LLM
    messages = []
    system_parts: List[str] = []
    if instructions:
        system_parts.append(instructions)
    if capabilities:
        system_parts.append(f"Capabilities: {', '.join(capabilities)}")
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    if context:
        messages.append({"role": "user", "content": "Context:\n" + "\n".join(context)})
    messages.append({"role": "user", "content": input or ""})

    try:
        output, resp = llm_service.complete(
            db, user_id, messages,
            model=model,
            temperature=0.7,
            max_output_tokens=1024,
        )
    except Exception as e:
        logger.error("LLM API error in agent_service.invoke", exc_info=e)
        raise

    # Record VU usage after successful invocation
    if config.BILLING_ENFORCEMENT_ENABLED:
        try:
            from services import gate_service
            from datetime import datetime, timezone
            if current_month is None:
                current_month = datetime.now(timezone.utc).strftime("%Y-%m")
            gate_service.add_tally(db, user_id, "vu", current_month, vu_cost)
        except Exception:
            logger.warning("Failed to record VU tally", exc_info=True)

    return InvokeResult(output=output)


def invoke_structured(
    *,
    db: StandardDatabase,
    user_id: str,
    request: AgentRequest,
) -> AgentResponse:
    """Structured invocation that returns proposed actions.

    Non-breaking addition: does not mutate DB. Apply actions in a follow-up
    service call (future) when request.options.preview == False.
    """
    operator_name = (request.operator or "").strip()

    # Fallback: no-op
    return AgentResponse(
        actions=[],
        messages=[f"Unknown operator '{operator_name}'. No actions produced."],
    )
