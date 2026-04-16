"""Unit tests for services.agent_service.

Covers the unified `invoke()` dispatch:
  - Task agent path: callable invoked with (db, arango_db, user_id, **params)
  - LLM path: messages built from instructions/capabilities/context/input
  - Missing input + no agent → ValueError
  - Billing gate: VU limit enforced when BILLING_ENFORCEMENT_ENABLED, 402 raised
  - Billing tally written after successful invocation
  - invoke_structured: unknown operator returns no-op response
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from services import agent_service


class TestTaskAgentDispatch:
    def test_calls_agent_callable_with_kwargs(self):
        captured = {}

        def fake_agent(*, db, arango_db, user_id, **kwargs):
            captured.update(
                {"db": db, "arango_db": arango_db, "user_id": user_id, **kwargs}
            )
            return {"result": "ok"}

        out = agent_service.invoke(
            db="db-handle",
            user_id="user-1",
            arango_db="arango-handle",
            agent=fake_agent,
            agent_params={"foo": "bar"},
        )
        assert out == {"result": "ok"}
        assert captured["db"] == "db-handle"
        assert captured["arango_db"] == "arango-handle"
        assert captured["user_id"] == "user-1"
        assert captured["foo"] == "bar"

    def test_task_agent_skips_llm_path(self):
        with patch("services.agent_service.llm_service.complete") as create:
            agent_service.invoke(
                db=MagicMock(), user_id="u", agent=lambda **kw: None
            )
        create.assert_not_called()


class TestLLMPath:
    def test_missing_input_and_agent_raises(self):
        with pytest.raises(ValueError, match="agent or input"):
            agent_service.invoke(db=MagicMock(), user_id="u")

    def test_builds_messages_with_system_and_context(self):
        captured = {}

        def fake_complete(db, user_id, messages, **kwargs):
            captured["messages"] = messages
            captured.update(kwargs)
            return ("the answer", MagicMock())

        with (
            patch("core.config.BILLING_ENFORCEMENT_ENABLED", False),
            patch(
                "services.agent_service.llm_service.complete",
                side_effect=fake_complete,
            ),
        ):
            out = agent_service.invoke(
                db=MagicMock(),
                user_id="u",
                input="what is x?",
                instructions="Be brief.",
                capabilities=["search"],
                context=["Context line 1", "Context line 2"],
                model="gpt-4o-mini",
            )

        msgs = captured["messages"]
        assert msgs[0]["role"] == "system"
        assert "Be brief." in msgs[0]["content"]
        assert "Capabilities: search" in msgs[0]["content"]
        assert msgs[1]["role"] == "user"
        assert "Context line 1" in msgs[1]["content"]
        assert msgs[-1]["content"] == "what is x?"
        assert out.output == "the answer"

    def test_messages_without_instructions_or_context(self):
        captured = {}

        def fake_complete(db, user_id, messages, **kwargs):
            captured["messages"] = messages
            return ("ok", MagicMock())

        with (
            patch("core.config.BILLING_ENFORCEMENT_ENABLED", False),
            patch(
                "services.agent_service.llm_service.complete",
                side_effect=fake_complete,
            ),
        ):
            agent_service.invoke(db=MagicMock(), user_id="u", input="hello")

        msgs = captured["messages"]
        # No system message when neither instructions nor capabilities supplied.
        assert all(m["role"] != "system" for m in msgs)
        assert msgs[-1]["content"] == "hello"

    def test_openai_error_propagates(self):
        with (
            patch("core.config.BILLING_ENFORCEMENT_ENABLED", False),
            patch(
                "services.agent_service.llm_service.complete",
                side_effect=RuntimeError("upstream"),
            ),
        ):
            with pytest.raises(RuntimeError):
                agent_service.invoke(db=MagicMock(), user_id="u", input="x")


class TestBillingGate:
    def test_vu_limit_exceeded_raises_402(self):
        with (
            patch("core.config.BILLING_ENFORCEMENT_ENABLED", True),
            patch(
                "services.gate_service.get_or_default_limits",
                return_value={"vu_limit": 5},
            ),
            patch("services.gate_service.get_tally", return_value=4),
            patch("services.agent_service.llm_service.complete") as create,
        ):
            with pytest.raises(HTTPException) as ei:
                agent_service.invoke(db=MagicMock(), user_id="u", input="x")
        assert ei.value.status_code == 402
        create.assert_not_called()

    def test_vu_limit_unset_means_no_gate(self):
        with (
            patch("core.config.BILLING_ENFORCEMENT_ENABLED", True),
            patch(
                "services.gate_service.get_or_default_limits",
                return_value={"vu_limit": None},
            ),
            patch("services.gate_service.get_tally", return_value=0),
            patch("services.gate_service.add_tally") as add,
            patch(
                "services.agent_service.llm_service.complete",
                return_value=("answer", MagicMock()),
            ),
        ):
            out = agent_service.invoke(db=MagicMock(), user_id="u", input="x")
        assert out.output == "answer"
        add.assert_called_once()

    def test_successful_invoke_records_vu_tally(self):
        with (
            patch("core.config.BILLING_ENFORCEMENT_ENABLED", True),
            patch(
                "services.gate_service.get_or_default_limits",
                return_value={"vu_limit": 1000},
            ),
            patch("services.gate_service.get_tally", return_value=0),
            patch("services.gate_service.add_tally") as add,
            patch(
                "services.agent_service.llm_service.complete",
                return_value=("ok", MagicMock()),
            ),
        ):
            agent_service.invoke(db=MagicMock(), user_id="u", input="x")
        add.assert_called_once()
        # Cost == 5 VU per the service constant.
        # Signature: add_tally(db, user_id, "vu", current_month, vu_cost)
        assert add.call_args[0][4] == 5


class TestInvokeStructured:
    def test_unknown_operator_returns_noop_response(self):
        from api.agents.contracts import AgentRequest

        out = agent_service.invoke_structured(
            db=MagicMock(),
            user_id="u",
            request=AgentRequest(operator="not-a-real-op"),
        )
        assert out.actions == []
        assert any("Unknown operator" in m for m in out.messages)
