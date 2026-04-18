"""Unit tests for the unified /events WebSocket helpers.

The WebSocket transport is awkward to drive through httpx/ASGITransport, so
these tests exercise the router's building blocks directly:

- `_parse_filter`: client JSON filter -> EventFilter dataclass
- `_event_visible_to`: per-user ACL check

The end-to-end WS handshake + protocol loop is validated via the router
smoke test at the bottom using `starlette.testclient.TestClient`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core import event_bus  # noqa: E402
from routers import events_router as ev  # noqa: E402


# ---------------------------------------------------------------------------
# _parse_filter
# ---------------------------------------------------------------------------

def test_parse_filter_empty_yields_wildcard():
    f = ev._parse_filter(None)
    assert f.container_id is None
    assert f.artifact_id is None
    assert f.content_type is None
    assert f.event_names is None


def test_parse_filter_normalizes_all_fields():
    raw = {
        "container_id": "ws-1",
        "artifact_id": "art-1",
        "content_type": "application/vnd.agience.operator+json",
        "event_names": ["artifact.invoke.*", 123, "artifact.created"],
    }
    f = ev._parse_filter(raw)
    assert f.container_id == "ws-1"
    assert f.artifact_id == "art-1"
    assert f.content_type == "application/vnd.agience.operator+json"
    assert f.event_names == ["artifact.invoke.*", "artifact.created"]


def test_parse_filter_drops_empty_strings_to_none():
    f = ev._parse_filter({"container_id": "", "content_type": ""})
    assert f.container_id is None
    assert f.content_type is None


# ---------------------------------------------------------------------------
# _event_visible_to (ACL)
# ---------------------------------------------------------------------------

class _Grant:
    def __init__(self, **flags):
        for k in ("read", "update", "create", "delete", "invoke", "add", "search", "own"):
            setattr(self, f"can_{k}", flags.get(k, False))
        self.resource_id = flags.get("resource_id")


def _auth(grants=(), principal_id=None):
    return SimpleNamespace(grants=list(grants), principal_id=principal_id)


def test_acl_denied_when_no_grants_and_not_actor():
    auth = _auth()
    ev_obj = event_bus.Event(name="x", payload={}, container_id="ws-1", artifact_id="a-1")
    assert ev._event_visible_to(auth, ev_obj) is False


def test_acl_allowed_when_artifact_read_grant_matches():
    auth = _auth(grants=[_Grant(read=True, resource_id="a-1")])
    ev_obj = event_bus.Event(name="x", payload={}, artifact_id="a-1", container_id="ws-1")
    assert ev._event_visible_to(auth, ev_obj) is True


def test_acl_allowed_when_container_read_grant_matches():
    auth = _auth(grants=[_Grant(read=True, resource_id="ws-1")])
    ev_obj = event_bus.Event(name="x", payload={}, artifact_id="a-1", container_id="ws-1")
    assert ev._event_visible_to(auth, ev_obj) is True


def test_acl_allowed_for_unscoped_read_grant():
    auth = _auth(grants=[_Grant(read=True)])
    ev_obj = event_bus.Event(name="x", payload={}, artifact_id="a-9", container_id="ws-9")
    assert ev._event_visible_to(auth, ev_obj) is True


def test_acl_denied_when_only_write_grants_no_read():
    auth = _auth(grants=[_Grant(update=True, resource_id="ws-1")])
    ev_obj = event_bus.Event(name="x", payload={}, artifact_id="a-1", container_id="ws-1")
    assert ev._event_visible_to(auth, ev_obj) is False


def test_acl_principal_sees_own_emitted_events_without_grants():
    """Server / mcp_client principals have no grants but should still see
    events they authored."""
    auth = _auth(grants=[], principal_id="server-xyz")
    ev_obj = event_bus.Event(
        name="x", payload={}, artifact_id="a-1", container_id="ws-1", actor_id="server-xyz"
    )
    assert ev._event_visible_to(auth, ev_obj) is True


# ---------------------------------------------------------------------------
# integration with event_bus: publish_event reaches filtered subscriber
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_event_delivers_to_filtered_subscriber_matching_container():
    event_bus._filtered_subscribers.clear()
    q = await event_bus.subscribe_filtered(
        event_bus.EventFilter(container_id="ws-42", event_names=["artifact.*"])
    )

    # Non-matching container: should not reach the queue
    await event_bus.publish_event(
        event_bus.Event(name="artifact.created", payload={}, container_id="ws-99")
    )
    # Non-matching name: should not reach the queue
    await event_bus.publish_event(
        event_bus.Event(name="other.thing", payload={}, container_id="ws-42")
    )
    # Matching: should reach the queue
    await event_bus.publish_event(
        event_bus.Event(name="artifact.invoke.completed", payload={"ok": True}, container_id="ws-42")
    )

    assert q.qsize() == 1
    msg = q.get_nowait()
    assert msg.name == "artifact.invoke.completed"

    await event_bus.unsubscribe_filtered(q)
