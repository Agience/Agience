"""
agience-server-verso � MCP Server
====================================
Reasoning & Workflow: synthesis, coordination, evaluation, training feedback.

Verso handles the reasoning layer � synthesizing information from multiple
sources, coordinating multi-step workflows, evaluating outputs, and
collecting training feedback. It is the orchestration engine that ties
retrieval, analysis, and action into coherent results.

Pipeline position: Reasoning & workflow execution.

Tools
-----
  synthesize              � Synthesize information from multiple sources via LLM
  run_workflow            � Execute a defined multi-step workflow
  invoke_llm              � Invoke an LLM using a connection artifact (credentials via Seraph, metering via Ophan)
  list_llm_defaults       � List platform-default LLM connection definitions
  chain_tasks             � Chain multiple MCP tool calls sequentially
  schedule_action         � Schedule a deferred action for future execution
  evaluate_output         � Evaluate quality/accuracy of generated output
  submit_feedback         � Submit evaluation feedback for training improvement

Auth
----
  PLATFORM_INTERNAL_SECRET  ⬩ Shared deployment secret for client_credentials token exchange
  AGIENCE_API_URI           ⬩ Base URI of the agience-core backend

Transport
---------
  MCP_TRANSPORT=streamable-http (default for Agience)
  MCP_HOST=0.0.0.0
  MCP_PORT=8088
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from agience_server_auth import AgieceServerAuth as _AgieceServerAuth

log = logging.getLogger("agience-server-verso")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s - %(name)s - %(message)s")

AGIENCE_API_URI: str = os.getenv("AGIENCE_API_URI", "http://localhost:8081").rstrip("/")
PLATFORM_INTERNAL_SECRET: str | None = os.getenv("PLATFORM_INTERNAL_SECRET")
VERSO_CLIENT_ID: str = "agience-server-verso"
MCP_TRANSPORT: str = os.getenv("MCP_TRANSPORT", "streamable-http")
MCP_HOST: str = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.getenv("MCP_PORT", "8088"))

_auth = _AgieceServerAuth(VERSO_CLIENT_ID, AGIENCE_API_URI)


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
                    "client_id": VERSO_CLIENT_ID,
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
# Auth wrappers (delegated to _shared/agience_server_auth.py)
# ---------------------------------------------------------------------------


async def _user_headers() -> dict[str, str]:
    return await _auth.user_headers(_exchange_token)


def _get_delegation_user_id() -> str:
    return _auth.get_delegation_user_id()


mcp = FastMCP(
    "agience-server-verso",
    instructions=(
        "You are Verso, the Agience reasoning and workflow server. "
        "Use Verso to synthesize information from multiple sources, "
        "coordinate multi-step workflows, evaluate output quality, "
        "and collect training feedback for continuous improvement."
    ),
)

from artifact_helpers import register_types_manifest
register_types_manifest(mcp, "verso", __file__)


# ---------------------------------------------------------------------------
# Tool: synthesize
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Synthesize information from multiple sources via LLM. "
        "Combines card content, search results, or raw input into "
        "a coherent, evidence-backed response."
    )
)
async def synthesize(
    input: str,
    artifact_ids: Optional[list[str]] = None,
    workspace_id: Optional[str] = None,
    model: str = "gpt-4o-mini",
) -> str:
    """
    Args:
        input: Question, prompt, or synthesis instruction.
        artifact_ids: Optional list of card IDs to use as context.
        workspace_id: Optional workspace for scoping.
        model: LLM model to use for synthesis.
    """
    # TODO(agents-invoke-removal): the former implementation dispatched to a
    # backend agent plugin "verso:synthesize" via /agents/invoke, but no such
    # plugin exists and inlining would recurse into this tool. Callers that
    # need LLM synthesis should invoke `invoke_llm` directly with a prepared
    # connection artifact and messages. Leaving this stub for manual design
    # review.
    _ = (input, artifact_ids, workspace_id, model)  # unused — kept for stable tool signature
    return json.dumps({"error": "synthesize is not yet implemented"})


# ---------------------------------------------------------------------------
# Workflow helpers
# ---------------------------------------------------------------------------

_CONDITION_OPS = {
    "eq":  lambda a, b: a == b,
    "ne":  lambda a, b: a != b,
    "gt":  lambda a, b: a is not None and b is not None and a > b,
    "gte": lambda a, b: a is not None and b is not None and a >= b,
    "lt":  lambda a, b: a is not None and b is not None and a < b,
    "lte": lambda a, b: a is not None and b is not None and a <= b,
}


def _evaluate_condition(condition: dict[str, Any], state_context: dict[str, Any]) -> bool:
    """Evaluate a workflow step condition against the State Artifact's context.

    Returns True if the step should execute, False if it should be skipped.
    Supported operators: eq, ne, gt, gte, lt, lte.
    """
    field = condition.get("field")
    if not field:
        return True  # no field specified = always execute

    value = state_context.get(field)

    for op_name, op_fn in _CONDITION_OPS.items():
        if op_name in condition:
            return op_fn(value, condition[op_name])

    # No recognised operator � default to execute
    return True


async def _get_artifact(client: httpx.AsyncClient, workspace_id: str, artifact_id: str) -> dict:
    """Fetch an artifact, trying the workspace first then collection batch lookup.

    LLM connection artifacts live in collections (not workspaces), so a
    workspace-only lookup would 404.  The fallback uses the global
    ``POST /collections/artifacts/batch`` endpoint.
    """
    # Try workspace first (covers most artifacts)
    resp = await client.get(
        f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts/{artifact_id}",
        headers=await _headers(),
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json()

    # Fallback: search collections by root_id batch
    batch_resp = await client.post(
        f"{AGIENCE_API_URI}/collections/artifacts/batch",
        headers=await _headers(),
        json={"root_ids": [artifact_id]},
        timeout=30,
    )
    if batch_resp.status_code == 200:
        results = batch_resp.json()
        if isinstance(results, list) and results:
            return results[0]

    # Neither path found it � raise the original workspace error
    resp.raise_for_status()
    return resp.json()


async def _patch_artifact_context(
    client: httpx.AsyncClient,
    workspace_id: str,
    artifact_id: str,
    context_updates: dict[str, Any],
) -> None:
    """PATCH a workspace artifact's context with the given updates (merged)."""
    # Fetch current context to merge
    artifact = await _get_artifact(client, workspace_id, artifact_id)
    current_ctx = artifact.get("context", {})
    if isinstance(current_ctx, str):
        try:
            current_ctx = json.loads(current_ctx)
        except json.JSONDecodeError:
            current_ctx = {}

    current_ctx.update(context_updates)

    await client.patch(
        f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts/{artifact_id}",
        headers=await _headers(),
        json={"context": current_ctx},
        timeout=30,
    )


async def _dispatch_child_transform(
    client: httpx.AsyncClient,
    workspace_id: str,
    transform_id: str,
    extra_params: dict[str, Any],
) -> dict:
    """Invoke a child Transform via POST /artifacts/{id}/invoke.

    This is the canonical artifact-invoke path. The operation_dispatcher
    reads the transform's type.json, enforces grants, and routes to the
    right handler (direct MCP tool for run.type=mcp-tool, or back to
    verso:execute_transform for workflow/llm/transform-ref types).
    """
    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "params": extra_params or {},
    }
    resp = await client.post(
        f"{AGIENCE_API_URI}/artifacts/{transform_id}/invoke",
        headers=await _headers(),
        json=body,
        timeout=300,  # pipeline steps can be long-running
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Tool: run_workflow
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Execute a multi-step workflow defined by a Transform artifact. "
        "Reads steps from the artifact's run block, evaluates conditions "
        "against a State Artifact, dispatches each step via the platform, "
        "and handles bounded retry loops."
    )
)
async def run_workflow(
    workflow_artifact_id: str,
    workspace_id: Optional[str] = None,
    params: Optional[str] = None,
) -> str:
    """
    Args:
        workflow_artifact_id: ID of the workflow Transform artifact.
        workspace_id: Workspace containing the workflow and state artifacts.
        params: JSON string of additional parameters (must include state_artifact_id).
    """
    if not workspace_id:
        return "Error: workspace_id is required for workflow execution."

    extra: dict[str, Any] = {}
    if params:
        try:
            extra = json.loads(params)
        except json.JSONDecodeError:
            return f"Error: params is not valid JSON: {params[:200]}"

    state_artifact_id = extra.get("state_artifact_id")

    async with httpx.AsyncClient() as client:
        # 1. Fetch the workflow Transform artifact
        try:
            workflow_artifact = await _get_artifact(client, workspace_id, workflow_artifact_id)
        except httpx.HTTPStatusError as exc:
            return f"Error: failed to fetch workflow artifact {workflow_artifact_id}: {exc.response.status_code}"

        ctx = workflow_artifact.get("context", {})
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except json.JSONDecodeError:
                return "Error: workflow artifact has invalid JSON context."

        # Find the run block
        run = (
            ctx.get("run")
            or (ctx.get("transform") or {}).get("run")
            or (ctx.get("order") or {}).get("run")
        )
        if not run or run.get("type") != "workflow":
            return f"Error: artifact {workflow_artifact_id} is not a workflow (run.type={run.get('type') if run else 'missing'})."

        steps = run.get("steps") or []
        retry_config = run.get("retry")

        if not steps:
            return "Workflow has no steps."

        # 2. Execute steps
        results: list[dict[str, Any]] = []
        retry_count = 0
        retries_performed = 0

        async def _execute_steps_from(start_index: int) -> str | None:
            """Execute steps starting from start_index. Returns error string or None."""
            for i in range(start_index, len(steps)):
                step = steps[i]
                step_name = step.get("name", f"step_{i}")
                child_transform_id = step.get("transform_id")

                if not child_transform_id:
                    return f"Error: step {i} ({step_name}) is missing transform_id."

                # Evaluate condition if present
                condition = step.get("condition")
                if condition and state_artifact_id:
                    try:
                        state_art = await _get_artifact(client, workspace_id, state_artifact_id)
                        state_ctx = state_art.get("context", {})
                        if isinstance(state_ctx, str):
                            state_ctx = json.loads(state_ctx)
                    except Exception:
                        state_ctx = {}

                    if not _evaluate_condition(condition, state_ctx):
                        log.info("Workflow step %d (%s) skipped � condition not met", i, step_name)
                        results.append({"step": i, "name": step_name, "status": "skipped"})
                        continue

                # Update state artifact with current step info
                if state_artifact_id:
                    try:
                        await _patch_artifact_context(client, workspace_id, state_artifact_id, {
                            "status": "running",
                            "current_step": step_name,
                            "step_index": i,
                        })
                    except Exception as exc:
                        log.warning("Failed to update state artifact step info: %s", exc)

                # Build params for child dispatch
                child_params = dict(extra)
                step_mapping = step.get("input_mapping") or {}
                for k, v in step_mapping.items():
                    if isinstance(v, str) and v.startswith("$."):
                        resolved = extra.get(v[2:])
                        if resolved is not None:
                            child_params[k] = resolved
                    else:
                        child_params[k] = v

                # Dispatch child transform
                log.info("Workflow step %d (%s) � dispatching transform %s", i, step_name, child_transform_id)
                try:
                    result = await _dispatch_child_transform(
                        client, workspace_id, child_transform_id, child_params,
                    )
                    results.append({"step": i, "name": step_name, "status": "completed", "result": result})
                except httpx.HTTPStatusError as exc:
                    error_msg = f"Step {i} ({step_name}) failed: HTTP {exc.response.status_code}"
                    try:
                        error_msg += f" � {exc.response.text[:300]}"
                    except Exception:
                        pass
                    results.append({"step": i, "name": step_name, "status": "error", "error": error_msg})

                    # Update state with error
                    if state_artifact_id:
                        try:
                            await _patch_artifact_context(client, workspace_id, state_artifact_id, {
                                "status": "error",
                                "error": error_msg,
                            })
                        except Exception:
                            pass
                    return error_msg
                except Exception as exc:
                    error_msg = f"Step {i} ({step_name}) failed: {exc}"
                    results.append({"step": i, "name": step_name, "status": "error", "error": error_msg})
                    if state_artifact_id:
                        try:
                            await _patch_artifact_context(client, workspace_id, state_artifact_id, {
                                "status": "error",
                                "error": error_msg,
                            })
                        except Exception:
                            pass
                    return error_msg

            return None  # success

        # Initial execution from step 0
        error = await _execute_steps_from(0)
        if error:
            return error

        # 3. Retry logic
        if retry_config and state_artifact_id:
            on_field = retry_config.get("on_field")
            max_retries = retry_config.get("max_retries", 0)
            restart_from = retry_config.get("restart_from_step", 0)

            # Read initial retry_count from state (may have been set by a previous run)
            try:
                state_art = await _get_artifact(client, workspace_id, state_artifact_id)
                state_ctx = state_art.get("context", {})
                if isinstance(state_ctx, str):
                    state_ctx = json.loads(state_ctx)
                retry_count = state_ctx.get("retry_count", 0)
            except Exception:
                pass

            while retry_count < max_retries:
                # Re-fetch state to check retry condition
                try:
                    state_art = await _get_artifact(client, workspace_id, state_artifact_id)
                    state_ctx = state_art.get("context", {})
                    if isinstance(state_ctx, str):
                        state_ctx = json.loads(state_ctx)
                except Exception:
                    break

                should_retry = state_ctx.get(on_field)
                if not should_retry:
                    break

                retry_count += 1
                retries_performed += 1
                log.info("Workflow retry %d/%d � restarting from step %d", retry_count, max_retries, restart_from)

                # Update retry_count in state
                try:
                    await _patch_artifact_context(client, workspace_id, state_artifact_id, {
                        "retry_count": retry_count,
                    })
                except Exception as exc:
                    log.warning("Failed to update retry_count in state: %s", exc)

                error = await _execute_steps_from(restart_from)
                if error:
                    return error

        # 4. Mark completed
        if state_artifact_id:
            try:
                await _patch_artifact_context(client, workspace_id, state_artifact_id, {
                    "status": "completed",
                    "error": None,
                })
            except Exception as exc:
                log.warning("Failed to mark state as completed: %s", exc)

        completed = sum(1 for r in results if r.get("status") == "completed")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        return f"Workflow completed: {completed} steps executed, {skipped} skipped, {retries_performed} retries."


# ---------------------------------------------------------------------------
# Tool: execute_transform � Transform artifact execution (migrated from agents/)
# ---------------------------------------------------------------------------

_MAX_TRANSFORM_DEPTH = 10


def _resolve_input_mapping(mapping: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Resolve '$.field' and '$.field[N]' references in mapping against params."""
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, str) and value.startswith("$."):
            path = value[2:]
            if "[" in path:
                field, rest = path.split("[", 1)
                try:
                    idx = int(rest.rstrip("]"))
                except ValueError:
                    idx = 0
                raw = params.get(field)
                resolved = raw[idx] if isinstance(raw, list) and len(raw) > idx else None
            else:
                resolved = params.get(path)
            if resolved is not None:
                result[key] = resolved
        else:
            result[key] = value
    return result


@mcp.tool(
    description=(
        "Execute a Transform artifact. Reads the artifact's run block and dispatches "
        "based on run.type: 'mcp-tool' calls an MCP tool directly, 'transform-ref' "
        "delegates to another Transform artifact, 'workflow' runs the multi-step "
        "workflow engine."
    )
)
async def execute_transform(
    workspace_id: str,
    transform_id: str,
    params: Optional[str] = None,
    depth: int = 0,
) -> str:
    """
    Args:
        workspace_id: Workspace containing the Transform artifact.
        transform_id: ID of the Transform artifact to execute.
        params: JSON string of additional parameters (e.g. artifact IDs, input values).
        depth: Internal recursion depth guard � do not set manually.
    """
    if not workspace_id or not transform_id:
        return json.dumps({"error": "workspace_id and transform_id are required"})

    if depth >= _MAX_TRANSFORM_DEPTH:
        return json.dumps({"error": f"Transform recursion depth exceeded (max {_MAX_TRANSFORM_DEPTH})"})

    invoke_params: dict[str, Any] = {}
    if params:
        try:
            invoke_params = json.loads(params)
        except json.JSONDecodeError:
            return json.dumps({"error": f"params is not valid JSON: {params[:200]}"})

    invoke_params["workspace_id"] = workspace_id
    invoke_params["transform_id"] = transform_id

    # Fetch the transform artifact
    async with httpx.AsyncClient() as client:
        try:
            transform_artifact = await _get_artifact(client, workspace_id, transform_id)
        except httpx.HTTPStatusError as exc:
            return json.dumps({"error": f"Transform artifact not found: {transform_id} (HTTP {exc.response.status_code})"})

    # Parse context
    raw_ctx = transform_artifact.get("context", {})
    if isinstance(raw_ctx, str):
        try:
            raw_ctx = json.loads(raw_ctx)
        except json.JSONDecodeError:
            return json.dumps({"error": "Transform artifact has invalid JSON context"})
    ctx = raw_ctx if isinstance(raw_ctx, dict) else {}

    # Inject artifact metadata so input_mapping can reference them
    invoke_params["_artifact_id"] = transform_id
    invoke_params["_artifact_content"] = transform_artifact.get("content")

    run = (
        ctx.get("run")
        or (ctx.get("transform") or {}).get("run")
        or (ctx.get("order") or {}).get("run")
    )
    if not run:
        return json.dumps({"error": "Transform artifact has no 'run' block"})

    run_type = run.get("type")

    # --- mcp-tool: call an MCP tool on a named server ---
    if run_type == "mcp-tool":
        # Resolve server via relationship edge
        server = None
        async with httpx.AsyncClient() as client:
            try:
                rel_resp = await client.get(
                    f"{AGIENCE_API_URI}/artifacts/{transform_id}/relationships",
                    headers=await _headers(),
                    params={"relationship": "server"},
                    timeout=30,
                )
                rel_resp.raise_for_status()
                rels = rel_resp.json()
                if rels:
                    server = rels[0].get("target_id")
            except httpx.HTTPStatusError:
                pass

        # Legacy fallback for pre-migration artifacts
        if not server:
            server = run.get("server_artifact_id")
        if not server:
            return json.dumps({"error": "Transform has no server relationship edge"})

        tool = run.get("tool")
        if not tool:
            return json.dumps({"error": "Transform run block is missing 'tool'"})

        input_mapping = run.get("input_mapping") or {}
        tool_args = _resolve_input_mapping(input_mapping, invoke_params)

        # Dispatch via the artifact-native invoke path
        # (POST /artifacts/{server}/invoke with {name, arguments, workspace_id}).
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{AGIENCE_API_URI}/artifacts/{server}/invoke",
                    headers=await _headers(),
                    json={
                        "name": tool,
                        "arguments": tool_args,
                        "workspace_id": workspace_id,
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                return json.dumps(resp.json(), indent=2)
            except httpx.HTTPStatusError as exc:
                return json.dumps({"error": f"Tool execution failed: HTTP {exc.response.status_code} � {exc.response.text[:300]}"})

    # --- transform-ref: delegate to another Transform artifact ---
    elif run_type in {"transform-ref", "order-ref"}:
        child_transform_id = run.get("transform_id")
        if not child_transform_id:
            return json.dumps({"error": "transform-ref run block is missing 'transform_id'"})

        child_params = dict(invoke_params)
        input_mapping = run.get("input_mapping") or {}
        mapped = _resolve_input_mapping(input_mapping, invoke_params)
        child_params.update(mapped)
        child_params["transform_id"] = child_transform_id

        return await execute_transform(
            workspace_id=workspace_id,
            transform_id=child_transform_id,
            params=json.dumps({k: v for k, v in child_params.items() if not k.startswith("_")}),
            depth=depth + 1,
        )

    # --- workflow: delegate to run_workflow ---
    elif run_type == "workflow":
        workflow_params = {k: v for k, v in invoke_params.items()
                          if k not in ("transform_id", "_artifact_id", "_artifact_content")
                          and not k.startswith("_")}
        return await run_workflow(
            workflow_artifact_id=transform_id,
            workspace_id=workspace_id,
            params=json.dumps(workflow_params) if workflow_params else None,
        )

    # --- llm: delegate to invoke_llm (same server, sister tool) ---
    elif run_type == "llm":
        connection = run.get("connection_artifact_id") or run.get("connection")
        if not connection:
            return json.dumps({"error": "LLM run block is missing 'connection_artifact_id'"})

        prompt_template = run.get("prompt") or run.get("system_prompt") or ""
        user_input = invoke_params.get("input", "")
        input_mapping = run.get("input_mapping") or {}
        mapped = _resolve_input_mapping(input_mapping, invoke_params)

        try:
            prompt = prompt_template.format(**mapped) if mapped else prompt_template
        except (KeyError, IndexError):
            # Template referenced a key we don't have; send it as-is rather
            # than failing --- the LLM may still handle placeholders gracefully.
            prompt = prompt_template

        messages: list[dict] = []
        if prompt:
            messages.append({"role": "system", "content": prompt})
        if user_input:
            messages.append({"role": "user", "content": user_input})
        elif not prompt:
            messages.append({"role": "user", "content": json.dumps(mapped)})

        return await invoke_llm(
            connection_artifact_id=connection,
            workspace_id=workspace_id or "",
            messages=json.dumps(messages),
            temperature=float(run.get("temperature", 0.7)),
            max_output_tokens=int(run.get("max_output_tokens", 2048)),
        )

    elif run_type == "webhook":
        return json.dumps({"error": "Transform run type 'webhook' is not yet implemented \u2014 spec not finalized"})

    else:
        return json.dumps({"error": f"Unknown Transform run type: {run_type!r}"})


# ---------------------------------------------------------------------------
# Tool: chain_tasks
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Chain multiple MCP tool calls sequentially. "
        "Each step's output feeds into the next step's input. "
        "Returns the final result of the chain."
    )
)
async def chain_tasks(
    steps: str,
    workspace_id: Optional[str] = None,
) -> str:
    """
    Args:
        steps: JSON array of step definitions, each with 'server', 'tool', 'arguments'.
        workspace_id: Workspace context for the chain.
    """
    return "TODO: chain_tasks not yet implemented."


# ---------------------------------------------------------------------------
# Tool: schedule_action
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Schedule a deferred action for future execution. "
        "Creates a task card that will be executed at the specified time or interval."
    )
)
async def schedule_action(
    workspace_id: str,
    action: str,
    cron: str = "0 8 * * 1",
    params: Optional[str] = None,
) -> str:
    """
    Args:
        workspace_id: Workspace for the task card.
        action: Action identifier or tool name to execute.
        cron: Cron expression for schedule (default: Mondays at 08:00).
        params: JSON string of action parameters.
    """
    return f"TODO: schedule_action not yet implemented. action={action}, cron={cron}"


# ---------------------------------------------------------------------------
# Tool: evaluate_output
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Evaluate the quality and accuracy of generated output. "
        "Scores content against criteria like relevance, completeness, "
        "coherence, and factual accuracy."
    )
)
async def evaluate_output(
    content: str,
    criteria: Optional[str] = None,
    reference: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> str:
    """
    Args:
        content: The generated output to evaluate.
        criteria: Optional JSON string of evaluation criteria and weights.
        reference: Optional reference/ground-truth text to compare against.
        workspace_id: Optional workspace context.
    """
    return "TODO: evaluate_output not yet implemented."


# ---------------------------------------------------------------------------
# Tool: submit_feedback
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Submit evaluation feedback for training improvement. "
        "Records human or automated quality judgments that can be used "
        "to improve future reasoning and generation."
    )
)
async def submit_feedback(
    artifact_id: str,
    rating: Optional[int] = None,
    feedback: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> str:
    """
    Args:
        artifact_id: ID of the card being evaluated.
        rating: Numeric quality rating (1-5).
        feedback: Free-text feedback or correction.
        workspace_id: Optional workspace context.
    """
    return f"TODO: submit_feedback not yet implemented. artifact_id={artifact_id}"


# ---------------------------------------------------------------------------
# Tool: transcribe_artifact
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Transcribe a video or audio artifact using AWS Transcribe Streaming. "
        "Operates on any video/* or audio/* artifact � downloads the media, "
        "extracts audio via ffmpeg, streams to AWS Transcribe, and creates "
        "a text/markdown transcript artifact in the workspace."
    )
)
async def transcribe_artifact(
    workspace_id: str,
    artifact_id: str,
    credential_artifact_id: Optional[str] = None,
    language_code: str = "en-US",
    title: Optional[str] = None,
) -> str:
    """
    Args:
        workspace_id: Workspace containing the media artifact.
        artifact_id: The video/mp4 or audio/* artifact to transcribe.
        credential_artifact_id: ID of a plain application/json artifact whose context holds aws_access_key_id, aws_secret_access_key (encrypted via Seraph), and aws_region.
                               If omitted, uses platform default credentials.
        language_code: AWS Transcribe language code (default: en-US).
        title: Optional title for the output transcript artifact.
    """
    import asyncio
    import subprocess
    import tempfile

    # 1. Fetch the media artifact metadata.
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts/{artifact_id}",
            headers=await _headers(),
        )
    if resp.status_code != 200:
        return json.dumps({"error": f"Failed to fetch artifact: {resp.status_code}"})
    artifact = resp.json()
    artifact_ctx = artifact.get("context", {})
    if isinstance(artifact_ctx, str):
        try:
            artifact_ctx = json.loads(artifact_ctx)
        except Exception:
            artifact_ctx = {}
    content_type = artifact_ctx.get("content_type", "")
    if not (content_type.startswith("video/") or content_type.startswith("audio/")):
        return json.dumps({"error": f"Artifact content type '{content_type}' is not video or audio"})

    # 2. Get content URL.
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AGIENCE_API_URI}/artifacts/{artifact_id}/content-url",
            headers=await _headers(),
        )
    if resp.status_code != 200:
        return json.dumps({"error": f"Failed to get content URL: {resp.status_code}"})
    content_url = resp.json().get("url")
    if not content_url:
        return json.dumps({"error": "No content URL returned"})

    # 3. Get AWS credentials.
    aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_region = os.getenv("AWS_TRANSCRIBE_AWS_REGION", "us-east-1")

    if credential_artifact_id:
        # Call Seraph's provide_aws_credentials tool via the artifact-native
        # invoke path (POST /artifacts/seraph/invoke).
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                seraph_resp = await client.post(
                    f"{AGIENCE_API_URI}/artifacts/seraph/invoke",
                    headers=await _headers(),
                    json={
                        "name": "provide_aws_credentials",
                        "arguments": {
                            "credential_artifact_id": credential_artifact_id,
                            "workspace_id": workspace_id,
                        },
                        "workspace_id": workspace_id,
                    },
                )
            if seraph_resp.status_code == 200:
                resp_data = seraph_resp.json()
                # Platform invoke returns the MCP tool result directly
                result = resp_data.get("result") or resp_data
                content_list = result.get("content") if isinstance(result, dict) else []
                if isinstance(content_list, list) and content_list:
                    creds_text = content_list[0].get("text", "{}")
                    creds = json.loads(creds_text)
                    aws_access_key_id = creds.get("aws_access_key_id", aws_access_key_id)
                    aws_secret_access_key = creds.get("aws_secret_access_key", aws_secret_access_key)
                    aws_region = creds.get("aws_region", aws_region)
        except Exception as exc:
            log.warning("Failed to get credentials from Seraph: %s � falling back to env vars", exc)

    if not aws_access_key_id or not aws_secret_access_key:
        return json.dumps({"error": "No AWS credentials available. Provide credential_artifact_id or set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env vars."})

    # 4. Download the media file.
    with tempfile.NamedTemporaryFile(suffix=".media", delete=False) as tmp_media:
        media_path = tmp_media.name
    try:
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            resp = await client.get(content_url)
        if resp.status_code != 200:
            return json.dumps({"error": f"Failed to download media: {resp.status_code}"})
        with open(media_path, "wb") as f:
            f.write(resp.content)

        # 5. Run ffmpeg to extract PCM audio.
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", media_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-af", "pan=mono|c0=0.5*c0+0.5*c1,volume=2",
            "-ar", "16000",
            "-f", "s16le",
            "-",
        ]

        proc = await asyncio.to_thread(
            subprocess.Popen,
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # 6. Stream to AWS Transcribe.
        try:
            from amazon_transcribe.client import TranscribeStreamingClient
            from amazon_transcribe.handlers import TranscriptResultStreamHandler
            from amazon_transcribe.model import TranscriptEvent
        except ImportError:
            return json.dumps({"error": "amazon-transcribe package not installed"})

        transcript_segments: list[str] = []

        class _Handler(TranscriptResultStreamHandler):
            async def handle_transcript_event(self, transcript_event: TranscriptEvent) -> None:
                results = transcript_event.transcript.results
                if not results:
                    return
                for result in results:
                    if not result.is_partial:
                        text = "".join([alt.transcript for alt in result.alternatives])
                        if text.strip():
                            transcript_segments.append(text.strip())

        # The amazon-transcribe SDK resolves credentials from the standard
        # boto chain (env vars, config files, IAM role).  For per-request
        # credentials we set them in the environment before creating the
        # client.  This is safe in single-worker async mode (one event loop
        # thread) but would need a credential provider override if running
        # with multiple OS threads.
        _prev_key = os.environ.get("AWS_ACCESS_KEY_ID")
        _prev_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
        _prev_region = os.environ.get("AWS_DEFAULT_REGION")
        try:
            os.environ["AWS_ACCESS_KEY_ID"] = aws_access_key_id
            os.environ["AWS_SECRET_ACCESS_KEY"] = aws_secret_access_key
            os.environ["AWS_DEFAULT_REGION"] = aws_region

            transcribe_client = TranscribeStreamingClient(region=aws_region)
            transcribe_stream = await transcribe_client.start_stream_transcription(
                language_code=language_code,
                media_sample_rate_hz=16000,
                media_encoding="pcm",
            )

            handler = _Handler(transcribe_stream.output_stream)

            async def write_chunks():
                assert proc.stdout is not None
                CHUNK_SIZE = 16000 * 2  # 1 second of 16kHz 16-bit mono
                while True:
                    chunk = await asyncio.to_thread(proc.stdout.read, CHUNK_SIZE)
                    if not chunk:
                        break
                    await transcribe_stream.input_stream.send_audio_event(audio_chunk=chunk)
                await transcribe_stream.input_stream.end_stream()

            await asyncio.gather(write_chunks(), handler.handle_events())
        finally:
            # Restore previous env vars.
            for key, prev in [
                ("AWS_ACCESS_KEY_ID", _prev_key),
                ("AWS_SECRET_ACCESS_KEY", _prev_secret),
                ("AWS_DEFAULT_REGION", _prev_region),
            ]:
                if prev is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prev

        proc.wait()
        full_transcript = "\n\n".join(transcript_segments)

        if not full_transcript.strip():
            return json.dumps({
                "status": "no_transcript",
                "artifact_id": artifact_id,
                "message": "No speech detected in the media file.",
            })

        # 7. Create transcript artifact in workspace.
        artifact_title = title or artifact_ctx.get("title") or "Transcript"
        if not artifact_title.startswith("Transcript"):
            artifact_title = f"Transcript \u2014 {artifact_title}"

        transcript_context = {
            "content_type": "text/markdown",
            "title": artifact_title,
            "type": "transcript",
            "source_artifact_id": artifact_id,
            "language_code": language_code,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts",
                headers=await _headers(),
                json={"context": transcript_context, "content": full_transcript},
            )
        if resp.status_code not in (200, 201):
            return json.dumps({"error": f"Failed to create transcript artifact: {resp.status_code}"})

        transcript_artifact = resp.json()

        return json.dumps({
            "status": "success",
            "transcript_artifact_id": transcript_artifact.get("id"),
            "source_artifact_id": artifact_id,
            "title": artifact_title,
            "character_count": len(full_transcript),
            "segment_count": len(transcript_segments),
        })

    finally:
        try:
            os.unlink(media_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Resource: Transform HTML View
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tool: invoke_llm
# ---------------------------------------------------------------------------

# Provider-specific env var names for platform-default credential resolution
_PROVIDER_ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "google": "GOOGLE_AI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}

# Default API endpoints per provider
_PROVIDER_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "azure": None,  # requires custom endpoint
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "mistral": "https://api.mistral.ai/v1",
}


def _is_responses_api_model(model: str) -> bool:
    """Detect GPT-5+ models that use the OpenAI Responses API."""
    m = model.lower()
    return m.startswith("gpt-5") or m.startswith("o3") or m.startswith("o4")


async def _call_openai(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float,
    max_output_tokens: int,
) -> dict:
    """Call OpenAI Chat Completions or Responses API."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    if _is_responses_api_model(model):
        # Responses API for GPT-5+
        body: dict[str, Any] = {
            "model": model,
            "input": messages,
            "max_output_tokens": max_output_tokens,
        }
        resp = await client.post(f"{endpoint}/responses", headers=headers, json=body, timeout=120)
    else:
        # Chat Completions API
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        resp = await client.post(f"{endpoint}/chat/completions", headers=headers, json=body, timeout=120)

    resp.raise_for_status()
    return resp.json()


async def _call_anthropic(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float,
    max_output_tokens: int,
) -> dict:
    """Call Anthropic Messages API."""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    # Separate system message from conversation
    system_text = ""
    conv_messages = []
    for m in messages:
        if m.get("role") == "system":
            system_text += m.get("content", "") + "\n"
        else:
            conv_messages.append(m)

    body: dict[str, Any] = {
        "model": model,
        "messages": conv_messages or [{"role": "user", "content": "Hello"}],
        "max_tokens": max_output_tokens,
        "temperature": temperature,
    }
    if system_text.strip():
        body["system"] = system_text.strip()

    resp = await client.post(f"{endpoint}/v1/messages", headers=headers, json=body, timeout=120)
    resp.raise_for_status()
    return resp.json()


def _extract_text(provider: str, response: dict) -> str:
    """Extract text content from a provider API response."""
    if provider == "openai":
        # Chat Completions
        choices = response.get("choices")
        if choices:
            return choices[0].get("message", {}).get("content", "")
        # Responses API
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            return part.get("text", "")
        return response.get("output_text", "")
    elif provider == "anthropic":
        content = response.get("content", [])
        return "".join(b.get("text", "") for b in content if b.get("type") == "text")
    return str(response)


def _extract_token_usage(provider: str, response: dict) -> dict:
    """Extract token usage from a provider API response."""
    if provider == "openai":
        usage = response.get("usage", {})
        return {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }
    elif provider == "anthropic":
        usage = response.get("usage", {})
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }
    return {"input_tokens": 0, "output_tokens": 0}


@mcp.tool(
    description=(
        "Invoke an LLM using a connection artifact. Resolves credentials via Seraph, "
        "checks rate limits via Ophan, dispatches to the provider API, and records "
        "usage metrics. Returns the LLM response text."
    )
)
async def invoke_llm(
    connection_artifact_id: str,
    workspace_id: str,
    messages: str,
    temperature: float = 0.7,
    max_output_tokens: int = 2048,
) -> str:
    """
    Args:
        connection_artifact_id: ID of the LLM Connection artifact in the workspace.
        workspace_id: Workspace containing the connection artifact.
        messages: JSON-encoded array of message objects [{role, content}, ...].
        temperature: Sampling temperature (0.0-2.0).
        max_output_tokens: Maximum tokens to generate.
    """
    # 1. Parse messages
    try:
        msg_list = json.loads(messages) if isinstance(messages, str) else messages
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid messages JSON"})

    async with httpx.AsyncClient() as client:
        # 2. Fetch the LLM Connection artifact
        try:
            artifact = await _get_artifact(client, workspace_id, connection_artifact_id)
        except httpx.HTTPStatusError as exc:
            return json.dumps({"error": f"Failed to fetch connection artifact: {exc.response.status_code}"})

        ctx = artifact.get("context", {})
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except json.JSONDecodeError:
                return json.dumps({"error": "Connection artifact has invalid context"})

        provider = ctx.get("provider", "openai")
        model = ctx.get("model", "gpt-4o-mini")
        endpoint = ctx.get("endpoint") or _PROVIDER_ENDPOINTS.get(provider)
        creds_ref = ctx.get("credentials_ref", {})

        if not endpoint:
            return json.dumps({"error": f"No endpoint configured for provider '{provider}'"})

        # 3. Resolve credentials via Seraph
        api_key = None
        resolution = creds_ref.get("resolution", "platform_default")

        if resolution == "platform_default":
            # Read from provider-specific env var
            env_key = _PROVIDER_ENV_KEYS.get(provider)
            api_key = os.getenv(env_key) if env_key else None
        else:
            # Delegate to Seraph for secret resolution via the artifact-native
            # invoke path. User identity is carried by the delegation JWT at
            # transport level.
            try:
                seraph_resp = await client.post(
                    f"{AGIENCE_API_URI}/artifacts/seraph/invoke",
                    headers=await _user_headers(),
                    json={
                        "name": "resolve_llm_credentials",
                        "arguments": {
                            "credentials_ref": json.dumps(creds_ref),
                        },
                        "workspace_id": workspace_id,
                    },
                    timeout=15,
                )
                if seraph_resp.status_code == 200:
                    resp_data = seraph_resp.json()
                    result = resp_data.get("result") or resp_data
                    content_list = result.get("content") if isinstance(result, dict) else []
                    if isinstance(content_list, list) and content_list:
                        result_text = content_list[0].get("text", "{}")
                        creds_result = json.loads(result_text)
                        api_key = creds_result.get("api_key")
            except Exception as exc:
                log.warning("Seraph credential resolution failed: %s", exc)

            # Fallback to env var if Seraph resolution didn't produce a key
            if not api_key:
                env_key = _PROVIDER_ENV_KEYS.get(provider)
                api_key = os.getenv(env_key) if env_key else None

        if not api_key:
            return json.dumps({"error": f"No API key available for provider '{provider}'. Add your own key or configure platform defaults."})

        # 4. Check rate limits via Ophan (artifact-native invoke)
        tier = ctx.get("tier", "free")
        try:
            ophan_check = await client.post(
                f"{AGIENCE_API_URI}/artifacts/ophan/invoke",
                headers=await _headers(),
                json={
                    "name": "check_llm_allowance",
                    "arguments": {
                        "user_id": _get_delegation_user_id(),
                        "tier": tier,
                        "estimated_tokens": max_output_tokens,
                    },
                    "workspace_id": workspace_id,
                },
                timeout=10,
            )
            if ophan_check.status_code == 200:
                check_data = ophan_check.json()
                # Parse the tool result
                check_text = check_data.get("result", {}).get("content", [{}])[0].get("text", "{}")
                if not check_text:
                    check_text = "{}"
                allowance = json.loads(check_text) if isinstance(check_text, str) else check_text
                if not allowance.get("allowed", True):
                    return json.dumps({
                        "error": f"Rate limit exceeded: {allowance.get('reason', 'Unknown')}",
                        "remaining_vu": allowance.get("remaining_vu", 0),
                    })
        except Exception as exc:
            log.warning("Ophan rate-limit check failed (proceeding anyway): %s", exc)

        # 5. Call the provider API (Seraph for creds, Ophan for rate check done above)
        try:
            if provider in ("openai", "azure"):
                raw_response = await _call_openai(client, endpoint, api_key, model, msg_list, temperature, max_output_tokens)
            elif provider == "anthropic":
                raw_response = await _call_anthropic(client, endpoint, api_key, model, msg_list, temperature, max_output_tokens)
            else:
                # Generic OpenAI-compatible endpoint (Mistral, local, etc.)
                raw_response = await _call_openai(client, endpoint, api_key, model, msg_list, temperature, max_output_tokens)
        except httpx.HTTPStatusError as exc:
            return json.dumps({"error": f"Provider API error: {exc.response.status_code} {exc.response.text[:300]}"})
        except Exception as exc:
            return json.dumps({"error": f"Provider call failed: {exc}"})

        # 6. Extract response text and usage
        text = _extract_text(provider, raw_response)
        usage = _extract_token_usage(provider, raw_response)

        # 7. Record usage via Ophan (artifact-native invoke)
        try:
            await client.post(
                f"{AGIENCE_API_URI}/artifacts/ophan/invoke",
                headers=await _headers(),
                json={
                    "name": "record_llm_usage",
                    "arguments": {
                        "user_id": _get_delegation_user_id(),
                        "provider": provider,
                        "model": model,
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "workspace_id": workspace_id,
                    },
                    "workspace_id": workspace_id,
                },
                timeout=10,
            )
        except Exception as exc:
            log.warning("Ophan usage recording failed (non-fatal): %s", exc)

        log.info(
            "invoke_llm complete � provider=%s model=%s input_tokens=%d output_tokens=%d",
            provider, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
        )

        return json.dumps({
            "text": text,
            "provider": provider,
            "model": model,
            "usage": usage,
        })


# ---------------------------------------------------------------------------
# Tool: list_llm_defaults
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "List available platform-default LLM connections. "
        "Returns the models and tiers provided by the platform. "
        "Artifacts are seeded by Core at startup; this tool is informational. "
        "Reads the type definition from the LLM Connection type.json."
    )
)
async def list_llm_defaults() -> str:
    """Return the LLM Connection type schema and supported providers."""
    type_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.llm-connection+json" / "type.json"
    try:
        type_def = json.loads(type_path.read_text(encoding="utf-8"))
        schema = type_def.get("context_schema", {})
        providers = schema.get("provider", {}).get("enum", [])
        tiers = schema.get("tier", {}).get("enum", [])
        return json.dumps({
            "type": "application/vnd.agience.llm-connection+json",
            "providers": providers,
            "tiers": tiers,
            "description": type_def.get("description", ""),
        }, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"Failed to read type definition: {exc}"})


# ---------------------------------------------------------------------------
# Tool: install_package
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Install a package into a target workspace. "
        "Reads the package manifest, resolves MCP server dependencies "
        "(creating missing server artifacts), copies content artifacts "
        "into the workspace, and rewrites package-scoped references to "
        "local IDs. Dispatched from the package type's invoke operation."
    )
)
async def install_package(
    transform_id: str,
    workspace_id: Optional[str] = None,
    target_workspace_id: Optional[str] = None,
    dry_run: bool = False,
) -> str:
    """Install the package artifact ``transform_id`` into ``target_workspace_id``.

    ``transform_id`` is the package manifest artifact ID (named that way
    because it's what the operation dispatcher injects as the invoke
    subject). ``target_workspace_id`` defaults to ``workspace_id`` if
    omitted.
    """
    target_ws = target_workspace_id or workspace_id
    if not target_ws:
        return json.dumps({"error": "target_workspace_id (or workspace_id) required"})

    async with httpx.AsyncClient() as client:
        # 1. Fetch the package manifest.
        pkg = await _get_artifact(client, workspace_id or target_ws, transform_id)
        pkg_ctx = pkg.get("context") or {}
        if isinstance(pkg_ctx, str):
            try:
                pkg_ctx = json.loads(pkg_ctx)
            except json.JSONDecodeError:
                return json.dumps({"error": "Package context is not valid JSON"})

        pkg_block = pkg_ctx.get("package") or {}
        if not pkg_block:
            return json.dumps({"error": "Artifact is not a package (missing context.package)"})

        pkg_id = pkg_block.get("id") or "unknown"
        pkg_version = pkg_block.get("version") or "0.0.0"
        contents = pkg_block.get("contents") or []
        deps = (pkg_block.get("dependencies") or {}).get("servers") or []
        linking = (pkg_block.get("install") or {}).get("linking") or []

        plan: dict[str, Any] = {
            "package_id": pkg_id,
            "package_version": pkg_version,
            "target_workspace_id": target_ws,
            "servers": {"existing": [], "would_create": [], "created": []},
            "artifacts": {"ref_map": {}, "created": []},
            "rewrites_applied": 0,
        }

        # 2. Resolve server dependencies. Check each required server and
        #    record whether we'd create it or it already exists.
        existing_servers = await _list_workspace_servers(client, target_ws)
        existing_by_name = {s.get("name"): s for s in existing_servers if s.get("name")}

        for dep in deps:
            name = dep.get("name")
            if not name:
                continue
            if name in existing_by_name:
                plan["servers"]["existing"].append(name)
                continue
            if dry_run:
                plan["servers"]["would_create"].append(name)
                continue
            try:
                created = await _create_server_artifact(client, target_ws, dep)
                plan["servers"]["created"].append({
                    "name": name,
                    "artifact_id": created.get("id"),
                })
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to create server dependency %s: %s", name, exc)
                plan["servers"]["created"].append({"name": name, "error": str(exc)})

        # 3. Copy content artifacts into the target workspace, building a
        #    ref_map from package-scoped ref -> new local artifact_id.
        ref_map: dict[str, str] = {}
        for entry in contents:
            ref = entry.get("artifact_ref")
            src_id = entry.get("artifact_id")
            if not ref or not src_id:
                continue
            if dry_run:
                ref_map[ref] = f"<pending:{src_id}>"
                continue
            try:
                new_id = await _copy_artifact_to_workspace(
                    client, src_id, target_ws, role=entry.get("role"),
                )
                ref_map[ref] = new_id
                plan["artifacts"]["created"].append({
                    "ref": ref, "role": entry.get("role"), "artifact_id": new_id,
                })
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to copy artifact %s: %s", src_id, exc)
                plan["artifacts"]["created"].append({
                    "ref": ref, "role": entry.get("role"), "error": str(exc),
                })

        plan["artifacts"]["ref_map"] = ref_map

        # 4. Apply link rewriting so imported artifacts reference each other
        #    by new local ID rather than package-scoped URI.
        if not dry_run:
            for rule in linking:
                from_ref = rule.get("from_ref")
                if from_ref not in ref_map:
                    continue
                new_id = ref_map[from_ref]
                rewrites = rule.get("rewrite") or []
                for r in rewrites:
                    path = r.get("path")
                    value_template = r.get("value") or ""
                    # Single supported pattern: ${workspace_artifact_id(<ref>)}
                    # resolves to the freshly-copied artifact ID for that ref.
                    resolved = _resolve_rewrite_value(value_template, ref_map)
                    if path and resolved is not None:
                        try:
                            await _patch_artifact_path(
                                client, target_ws, new_id, path, resolved,
                            )
                            plan["rewrites_applied"] += 1
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "Rewrite failed (%s.%s): %s", new_id, path, exc,
                            )

        plan["status"] = "dry_run" if dry_run else "installed"
        return json.dumps(plan, indent=2)


async def _list_workspace_servers(
    client: httpx.AsyncClient, workspace_id: str,
) -> list[dict]:
    """List MCP server artifacts already in the target workspace."""
    try:
        resp = await client.get(
            f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts",
            headers=await _headers(),
            params={"content_type": "application/vnd.agience.mcp-server+json"},
            timeout=30,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        artifacts = data if isinstance(data, list) else data.get("artifacts", [])
        out: list[dict] = []
        for a in artifacts:
            ctx = a.get("context") or {}
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except json.JSONDecodeError:
                    ctx = {}
            out.append({"id": a.get("id"), "name": ctx.get("name"), "context": ctx})
        return out
    except Exception:
        return []


async def _create_server_artifact(
    client: httpx.AsyncClient, workspace_id: str, dep: dict,
) -> dict:
    """Create a vnd.agience.mcp-server+json artifact from a package dep entry."""
    ctx = {
        "name": dep.get("name"),
        "transport": dep.get("transport", "http"),
    }
    if dep.get("endpoint"):
        ctx["endpoint"] = dep["endpoint"]
    if dep.get("repo_url"):
        ctx["repo_url"] = dep["repo_url"]
    payload = {
        "content_type": "application/vnd.agience.mcp-server+json",
        "context": ctx,
        "workspace_id": workspace_id,
    }
    resp = await client.post(
        f"{AGIENCE_API_URI}/artifacts",
        headers=await _headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


async def _copy_artifact_to_workspace(
    client: httpx.AsyncClient,
    source_id: str,
    target_workspace_id: str,
    role: Optional[str] = None,
) -> str:
    """Copy a source artifact's content + context into a new artifact in the target workspace.

    Returns the new artifact ID. Adds ``package_role`` to the copy's
    context for traceability.
    """
    # Fetch via collection-batch so we can read from either workspace or
    # published collection.
    batch = await client.post(
        f"{AGIENCE_API_URI}/collections/artifacts/batch",
        headers=await _headers(),
        json={"root_ids": [source_id]},
        timeout=30,
    )
    batch.raise_for_status()
    results = batch.json()
    if not isinstance(results, list) or not results:
        raise ValueError(f"Source artifact {source_id} not found")
    source = results[0]

    ctx = source.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except json.JSONDecodeError:
            ctx = {}
    if role:
        ctx["package_role"] = role

    payload = {
        "content_type": source.get("content_type"),
        "context": ctx,
        "content": source.get("content") or "",
        "workspace_id": target_workspace_id,
    }
    resp = await client.post(
        f"{AGIENCE_API_URI}/artifacts",
        headers=await _headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("id")


def _resolve_rewrite_value(template: str, ref_map: dict[str, str]) -> Optional[str]:
    """Resolve ``${workspace_artifact_id(<ref>)}`` tokens in a rewrite value.

    Returns None if the template references a ref that's not in ref_map.
    """
    import re
    match = re.match(
        r"^\$\{workspace_artifact_id\(([^)]+)\)\}$", template.strip(),
    )
    if not match:
        # Literal value; passthrough.
        return template
    ref = match.group(1).strip().strip("'\"")
    return ref_map.get(ref)


async def _patch_artifact_path(
    client: httpx.AsyncClient,
    workspace_id: str,
    artifact_id: str,
    path: str,
    value: Any,
) -> None:
    """Set a dotted path inside an artifact's context to *value* (PATCH merge)."""
    artifact = await _get_artifact(client, workspace_id, artifact_id)
    ctx = artifact.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except json.JSONDecodeError:
            ctx = {}

    # Walk the dotted path, creating dicts as needed.
    parts = path.split(".")
    node: dict = ctx
    for key in parts[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    node[parts[-1]] = value

    await client.patch(
        f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts/{artifact_id}",
        headers=await _headers(),
        json={"context": ctx},
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Tool: export_package
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Populate a package manifest from workspace contents. "
        "Walks the named workspace (or subset by artifact_ids), generates "
        "stable package-scoped references, and updates the package artifact's "
        "context.package.contents + dependencies.servers. Dispatched from "
        "the package type's export operation."
    )
)
async def export_package(
    transform_id: str,
    workspace_id: Optional[str] = None,
    artifact_ids: Optional[list[str]] = None,
) -> str:
    """Populate package ``transform_id`` with contents from ``workspace_id``.

    Only inspects draft + committed artifacts in the workspace; archived
    artifacts are skipped. Server dependencies are inferred from each
    transform artifact's ``server`` relationship edge.
    """
    ws = workspace_id
    if not ws:
        return json.dumps({"error": "workspace_id required"})

    async with httpx.AsyncClient() as client:
        # 1. Read the package manifest.
        pkg = await _get_artifact(client, ws, transform_id)
        pkg_ctx = pkg.get("context") or {}
        if isinstance(pkg_ctx, str):
            try:
                pkg_ctx = json.loads(pkg_ctx)
            except json.JSONDecodeError:
                return json.dumps({"error": "Package context is not valid JSON"})
        pkg_block = pkg_ctx.get("package") or {}
        pkg_id = pkg_block.get("id")
        if not pkg_id:
            return json.dumps({"error": "Package is missing context.package.id"})

        # 2. List workspace artifacts (filtered if artifact_ids given).
        artifacts = await _list_workspace_artifacts(client, ws)
        if artifact_ids:
            wanted = set(artifact_ids)
            artifacts = [a for a in artifacts if a.get("id") in wanted]

        # 3. Build contents[] and dependencies.servers[].
        contents: list[dict[str, Any]] = []
        server_names: set[str] = set()

        for a in artifacts:
            aid = a.get("id")
            ctype = a.get("content_type") or ""
            if ctype == "application/vnd.agience.package+json":
                continue  # don't include the package itself in its own contents
            ctx = a.get("context") or {}
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except json.JSONDecodeError:
                    ctx = {}

            role = _infer_package_role(ctype, ctx)
            slug = ctx.get("slug") or (ctx.get("title") or aid).lower().replace(" ", "-")
            contents.append({
                "artifact_ref": f"agience://packages/{pkg_id}/{role}/{slug}",
                "artifact_id": aid,
                "role": role,
                "content_type": ctype,
                "slug": slug,
            })

            # Harvest server dependencies via relationship edges.
            if ctype == "application/vnd.agience.transform+json":
                try:
                    rel_resp = await client.get(
                        f"{AGIENCE_API_URI}/artifacts/{aid}/relationships",
                        headers=await _headers(),
                        params={"relationship": "server"},
                        timeout=30,
                    )
                    rel_resp.raise_for_status()
                    for rel in rel_resp.json():
                        tid = rel.get("target_id")
                        if tid:
                            server_names.add(tid)
                except httpx.HTTPStatusError:
                    pass

        # 4. Merge into the existing package context (preserve publisher,
        #    version, etc. that the author set manually).
        pkg_block["contents"] = contents
        deps = pkg_block.setdefault("dependencies", {})
        existing_deps = deps.get("servers") or []
        existing_ids = {d.get("artifact_id") for d in existing_deps if d.get("artifact_id")}
        for server_id in sorted(server_names):
            if server_id not in existing_ids:
                existing_deps.append({"artifact_id": server_id})
        deps["servers"] = existing_deps

        pkg_ctx["package"] = pkg_block

        # 5. Write back to the package artifact.
        await client.patch(
            f"{AGIENCE_API_URI}/workspaces/{ws}/artifacts/{transform_id}",
            headers=await _headers(),
            json={"context": pkg_ctx},
            timeout=30,
        )

        return json.dumps({
            "status": "exported",
            "package_id": pkg_id,
            "contents_count": len(contents),
            "server_deps_count": len(existing_deps),
        }, indent=2)


async def _list_workspace_artifacts(
    client: httpx.AsyncClient, workspace_id: str,
) -> list[dict]:
    resp = await client.get(
        f"{AGIENCE_API_URI}/workspaces/{workspace_id}/artifacts",
        headers=await _headers(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("artifacts", [])


def _infer_package_role(content_type: str, context: dict) -> str:
    """Infer a package role string from content type + context."""
    if content_type == "application/vnd.agience.transform+json":
        return "transform"
    if content_type == "application/vnd.agience.mcp-server+json":
        return "server"
    if content_type == "application/vnd.agience.prompts+json":
        return "prompt"
    if content_type.startswith("text/markdown"):
        return "docs"
    ctx_type = context.get("type")
    if ctx_type == "prompt":
        return "prompt"
    if ctx_type == "docs":
        return "docs"
    return "artifact"


# ---------------------------------------------------------------------------
# Resources: HTML Views
# ---------------------------------------------------------------------------

@mcp.resource("ui://verso/vnd.agience.transform.html")
async def transform_html_view() -> str:
    """Standalone MCP Apps HTML view for vnd.agience.transform+json artifacts."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.transform+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://verso/vnd.agience.evaluation.html")
async def evaluation_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.evaluation+json."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.evaluation+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


@mcp.resource("ui://verso/vnd.agience.llm-connection.html")
async def llm_connection_viewer_html() -> str:
    """Serve the viewer HTML for vnd.agience.llm-connection+json."""
    view_path = pathlib.Path(__file__).parent / "ui" / "application" / "vnd.agience.llm-connection+json" / "view.html"
    return view_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Standard server interface (used by _host and standalone)
# ---------------------------------------------------------------------------

def create_server_app():
    """Return the Verso ASGI app with verified middleware and startup hooks."""
    return _auth.create_app(mcp, _exchange_token)


async def server_startup() -> None:
    """Run Verso startup tasks: Core JWKS fetch + server key registration."""
    await _auth.startup(_exchange_token)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting agience-server-verso � transport=%s port=%s", MCP_TRANSPORT, MCP_PORT)
    if MCP_TRANSPORT == "streamable-http":
        import uvicorn
        uvicorn.run(create_server_app(), host=MCP_HOST, port=MCP_PORT)
    else:
        mcp.run()
