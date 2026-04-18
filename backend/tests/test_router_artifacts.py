"""Tests for routers/artifacts_router.py — the unified artifact API.

Existing coverage in test_router_artifacts_op.py covers the
POST /artifacts/{id}/op/{op_name} dispatch path. This file covers everything
else: CRUD, container creation, invoke, add-to-container, batch, search,
upload, commit/preview, content-url, list-commits, reorder, revert, move,
upload-status, multipart-part-url.

The auth dependency is overridden by the autouse conftest fixture to a user
principal (`user-123`); we patch `routers.artifacts_router.check_access` to a
no-op so the grant check doesn't reach the DB.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from main import app
from services.dependencies import AuthContext, get_auth
from entities.collection import Collection as CollectionEntity, WORKSPACE_CONTENT_TYPE
from entities.artifact import Artifact as ArtifactEntity


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_s3_put():
    """Prevent real S3 calls in workspace_service._store_content_in_s3.

    All router tests deal with the service layer, which now uploads content to
    S3 on create/update. Stub out put_text_direct so tests don't need MinIO.
    The returned content_key uses a predictable format so assertions still work.
    """
    with patch("services.content_service.put_text_direct") as mock_put:
        mock_put.return_value = None
        yield mock_put


@pytest.fixture(autouse=True)
def _patch_check_access():
    """`check_access` is the only side-effect path into the DB from this router.
    Replace it with a no-op grant for every test in this file."""
    grant = SimpleNamespace(
        can_read=True,
        can_create=True,
        can_update=True,
        can_delete=True,
        can_invoke=True,
        can_add=True,
        can_share=True,
        resource_id=None,
    )
    with patch("routers.artifacts_router.check_access", return_value=grant):
        yield grant


@pytest.fixture
def anon_client(client: AsyncClient):
    """Override auth to anonymous for negative tests."""
    app.dependency_overrides[get_auth] = lambda: AuthContext(
        user_id=None, principal_id=None, principal_type="anonymous"
    )
    yield client
    app.dependency_overrides.pop(get_auth, None)


def _coll_doc(content_type: str = WORKSPACE_CONTENT_TYPE) -> dict:
    return {
        "_key": "container-1",
        "name": "Container",
        "created_by": "user-123",
        "content_type": content_type,
    }


def _artifact_doc(state: str = "draft") -> dict:
    return {
        "_key": "art-1",
        "id": "art-1",
        "root_id": "art-1",
        "collection_id": "container-1",
        "context": '{"content_type":"text/plain"}',
        "content": "hello",
        "state": state,
        "created_by": "user-123",
    }


def _patch_db_collection(arango_mock: MagicMock, *, container_doc=None, artifact_doc=None):
    """Wire up `db.collection("artifacts")` — container-as-artifact: all docs in artifacts."""
    art = MagicMock()

    def _get(key):
        if container_doc and key == container_doc.get("_key"):
            return container_doc
        if artifact_doc and key == artifact_doc.get("_key"):
            return artifact_doc
        return None

    art.get.side_effect = _get
    arango_mock.collection.return_value = art


# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------

class TestAuthGuards:
    @pytest.mark.asyncio
    async def test_create_requires_user(self, anon_client: AsyncClient):
        r = await anon_client.post("/artifacts", json={"container_id": "c-1"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_update_requires_user(self, anon_client: AsyncClient):
        r = await anon_client.patch("/artifacts/a-1", json={"content": "x"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_delete_requires_user(self, anon_client: AsyncClient):
        r = await anon_client.delete("/artifacts/a-1")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_invoke_requires_user(self, anon_client: AsyncClient):
        r = await anon_client.post("/artifacts/a-1/invoke", json={})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_commit_requires_user(self, anon_client: AsyncClient):
        # Commit is now dispatched via /op/commit
        r = await anon_client.post("/artifacts/c-1/op/commit", json={})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /artifacts — create
# ---------------------------------------------------------------------------

class TestCreateArtifact:
    @pytest.mark.asyncio
    async def test_create_workspace_container(self, client: AsyncClient):
        ws = CollectionEntity(
            id="ws-1",
            name="My WS",
            created_by="user-123",
            content_type=WORKSPACE_CONTENT_TYPE,
            context="",
        )
        with patch(
            "services.operation_dispatcher.dispatch",
            return_value=ws.to_dict(),
        ):
            r = await client.post(
                "/artifacts",
                json={"content_type": WORKSPACE_CONTENT_TYPE, "name": "My WS"},
            )
        assert r.status_code == 201
        body = r.json()
        assert body["id"] == "ws-1"

    @pytest.mark.asyncio
    async def test_create_collection_container(self, client: AsyncClient):
        col = CollectionEntity(
            id="col-1",
            name="My Col",
            created_by="user-123",
            content_type="application/json",
            context="",
        )
        with patch(
            "services.operation_dispatcher.dispatch",
            return_value=col.to_dict(),
        ):
            r = await client.post(
                "/artifacts",
                json={"content_type": "application/vnd.agience.collection+json", "name": "My Col"},
            )
        assert r.status_code == 201

    @pytest.mark.asyncio
    async def test_create_artifact_in_unknown_container_returns_404(
        self, client: AsyncClient
    ):
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=None)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.post(
                "/artifacts",
                json={"container_id": "missing", "content": "x"},
            )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404
        assert "Container not found" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_artifact_happy_path(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=_coll_doc())
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        captured: dict = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return ArtifactEntity(
                id="art-1",
                root_id="art-1",
                collection_id="container-1",
                context='{"content_type":"text/plain"}',
                content="hello",
                state=ArtifactEntity.STATE_DRAFT,
            )

        try:
            with patch("services.workspace_service.create_workspace_artifact", side_effect=fake_create):
                r = await client.post(
                    "/artifacts",
                    json={
                        "container_id": "container-1",
                        "content": "hello",
                        "content_type": "text/plain",
                    },
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 201
        body = r.json()
        assert body["id"] == "art-1"
        assert body["collection_id"] == "container-1"
        assert "slug" not in captured

    @pytest.mark.asyncio
    async def test_create_artifact_merges_content_type_into_context(
        self, client: AsyncClient
    ):
        """When `content_type` is supplied alongside a JSON `context`, the
        router merges it into the context dict before persisting."""
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=_coll_doc())
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        captured: dict = {}

        def fake_create(**kwargs):
            entity = ArtifactEntity(
                id="art-merge",
                root_id="art-merge",
                collection_id=kwargs.get("workspace_id", "container-1"),
                context=kwargs.get("context", "{}"),
                content=kwargs.get("content", ""),
                state=ArtifactEntity.STATE_DRAFT,
            )
            captured["context"] = kwargs.get("context", "")
            return entity

        try:
            with patch("services.workspace_service.create_workspace_artifact", side_effect=fake_create):
                await client.post(
                    "/artifacts",
                    json={
                        "container_id": "container-1",
                        "context": '{"existing":"field"}',
                        "content_type": "text/plain",
                        "content": "x",
                    },
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        ctx = captured["context"]
        assert '"content_type": "text/plain"' in ctx or "content_type" in ctx
        assert "existing" in ctx


# ---------------------------------------------------------------------------
# GET /artifacts/{id} — read
# ---------------------------------------------------------------------------

class TestReadArtifact:
    @pytest.mark.asyncio
    async def test_read_returns_normalized_doc(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc={
            "_id": "artifacts/a-1",
            "_key": "a-1",
            "_rev": "_abc",
            "id": "a-1",
            "context": "{}",
            "content": "x",
            "state": "draft",
        })
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.get("/artifacts/a-1")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 200
        body = r.json()
        # Internal Arango keys stripped.
        assert "_id" not in body
        assert "_rev" not in body
        assert "_key" not in body
        assert body["id"] == "a-1"

    @pytest.mark.asyncio
    async def test_read_404_when_missing(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=None)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.get("/artifacts/missing")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_read_404_when_archived(self, client: AsyncClient):
        """Archived artifacts are filtered out by `_find_artifact`."""
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc={"_key": "a-1", "state": "archived"})
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.get("/artifacts/a-1")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_read_404_for_legacy_collection_prefixed_id(self, client: AsyncClient):
        arango = MagicMock()
        art = MagicMock()
        docs = {
            "container-1": {
                "_key": "container-1",
                "id": "container-1",
                "content_type": WORKSPACE_CONTENT_TYPE,
                "state": "draft",
            }
        }
        art.get.side_effect = lambda k: docs.get(k)
        arango.collection.return_value = art
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.get("/artifacts/collection:container-1")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        assert r.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /artifacts/{id} — update
# ---------------------------------------------------------------------------

class TestUpdateArtifact:
    @pytest.mark.asyncio
    async def test_update_routes_container_to_workspace_service(
        self, client: AsyncClient
    ):
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=_coll_doc(WORKSPACE_CONTENT_TYPE))
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        updated = CollectionEntity(
            id="container-1",
            name="Renamed",
            created_by="user-123",
            content_type=WORKSPACE_CONTENT_TYPE,
            context="",
        )
        try:
            with patch(
                "services.workspace_service.update_workspace", return_value=updated
            ) as upd:
                r = await client.patch(
                    "/artifacts/container-1",
                    json={"name": "Renamed"},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 200
        upd.assert_called_once()
        assert r.json()["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_update_artifact_routes_to_workspace_service(
        self, client: AsyncClient
    ):
        arango = MagicMock()
        # `_is_collection` returns False (artifact has collection_id), `_find_artifact` returns the doc.
        art = MagicMock()
        art.get.return_value = _artifact_doc()
        arango.collection.return_value = art

        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        updated = ArtifactEntity(
            id="art-1",
            root_id="art-1",
            collection_id="container-1",
            context="{}",
            content="new",
            state="draft",
        )
        try:
            with patch(
                "services.workspace_service.update_artifact", return_value=updated
            ):
                r = await client.patch(
                    "/artifacts/art-1",
                    json={"content": "new"},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 200
        assert r.json()["content"] == "new"

    @pytest.mark.asyncio
    async def test_update_artifact_with_content_type_routes_to_artifact_update(
        self, client: AsyncClient
    ):
        """Artifacts created via the type picker have a top-level content_type
        (e.g. text/markdown). PATCH must still route to the artifact update
        path, not the container update path. Regression test for the bug where
        _is_collection returned True for any non-None content_type."""
        arango = MagicMock()

        # Artifact with a content_type that is NOT a container type.
        typed_doc = {
            "_key": "typed-1",
            "id": "typed-1",
            "root_id": "typed-1",
            "collection_id": "container-1",
            "context": '{"content_type":"text/markdown"}',
            "content": "# old",
            "content_type": "text/markdown",
            "state": "draft",
            "created_by": "user-123",
        }

        art = MagicMock()
        art.get.side_effect = lambda key: typed_doc if key == "typed-1" else None
        arango.collection.return_value = art

        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango

        updated = ArtifactEntity(
            id="typed-1",
            root_id="typed-1",
            collection_id="container-1",
            context='{"content_type":"text/markdown"}',
            content="# new content",
            state="draft",
            content_type="text/markdown",
        )
        try:
            with patch(
                "services.workspace_service.update_artifact", return_value=updated
            ) as upd:
                r = await client.patch(
                    "/artifacts/typed-1",
                    json={"content": "# new content"},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        assert r.status_code == 200
        upd.assert_called_once()
        assert r.json()["content"] == "# new content"

    @pytest.mark.asyncio
    async def test_update_artifact_404_when_missing(self, client: AsyncClient):
        arango = MagicMock()
        art = MagicMock()
        art.get.return_value = None
        arango.collection.return_value = art

        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.patch(
                "/artifacts/missing",
                json={"content": "x"},
            )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /artifacts/{id}
# ---------------------------------------------------------------------------

class TestDeleteArtifact:
    @pytest.mark.asyncio
    async def test_delete_calls_service_and_returns_id(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=_artifact_doc())
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with patch("services.workspace_service.delete_artifact") as deleted:
                r = await client.delete("/artifacts/art-1")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        assert r.status_code == 200
        assert r.json() == {"id": "art-1", "deleted": True}
        deleted.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_404_when_missing(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=None)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.delete("/artifacts/missing")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /artifacts/{artifact_id}/remove — remove from workspace (soft)
# ---------------------------------------------------------------------------

class TestRemoveArtifactFromWorkspace:
    @pytest.mark.asyncio
    async def test_requires_auth(self, anon_client: AsyncClient):
        r = await anon_client.post("/artifacts/art-1/remove", json={"container_id": "ws-1"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_happy_path_returns_removed_response(self, client: AsyncClient):
        from entities.artifact import Artifact as ArtifactEntity
        removed = ArtifactEntity(
            id="art-1",
            root_id="art-1",
            collection_id="ws-1",
            context='{"content_type":"text/plain"}',
            content="hi",
            state=ArtifactEntity.STATE_DRAFT,
            created_by="user-123",
        )
        with patch(
            "services.workspace_service.remove_artifact_from_workspace",
            return_value=removed,
        ) as svc:
            r = await client.post("/artifacts/art-1/remove", json={"container_id": "ws-1"})

        assert r.status_code == 200
        body = r.json()
        assert body == {"id": "art-1", "removed": True, "container_id": "ws-1"}
        svc.assert_called_once()

    @pytest.mark.asyncio
    async def test_404_propagates(self, client: AsyncClient):
        from fastapi import HTTPException
        with patch(
            "services.workspace_service.remove_artifact_from_workspace",
            side_effect=HTTPException(status_code=404, detail="Artifact not found"),
        ):
            r = await client.post("/artifacts/missing/remove", json={"container_id": "ws-1"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# PUT /artifacts/{container_id} — add item to container
# ---------------------------------------------------------------------------

class TestAddItemToContainer:
    @pytest.mark.asyncio
    async def test_404_when_container_missing(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=None)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.put(
                "/artifacts/missing", json={"content": "x"}
            )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_happy_path_creates_artifact_and_links_edge(
        self, client: AsyncClient
    ):
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=_coll_doc())
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        created = ArtifactEntity(
            id="new",
            root_id="new",
            collection_id="container-1",
            context='{"content_type":"text/plain"}',
            content="hi",
            state=ArtifactEntity.STATE_DRAFT,
        )
        try:
            with (
                patch("db.arango.create_artifact", return_value=created),
                patch("db.arango.add_artifact_to_collection") as link,
            ):
                r = await client.put(
                    "/artifacts/container-1",
                    json={"content": "hi", "content_type": "text/plain"},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 201
        link.assert_called_once()


# ---------------------------------------------------------------------------
# POST /artifacts/{id}/invoke
# ---------------------------------------------------------------------------

class TestInvokeArtifact:
    @pytest.mark.asyncio
    async def test_404_when_artifact_missing(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=None)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.post("/artifacts/missing/invoke", json={"input": "x"})
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_happy_path_dispatches_invoke_op(self, client: AsyncClient):
        from unittest.mock import AsyncMock

        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=_artifact_doc())
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with patch(
                "services.operation_dispatcher.dispatch",
                new=AsyncMock(return_value={"output": "ok"}),
            ) as dispatch:
                r = await client.post(
                    "/artifacts/art-1/invoke",
                    json={"input": "do thing", "workspace_id": "ws-1"},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 200
        assert r.json() == {"output": "ok"}
        # The dispatcher is called with op_name="invoke".
        assert dispatch.call_args[0][0] == "invoke"

    @pytest.mark.asyncio
    async def test_undeclared_invoke_returns_404(self, client: AsyncClient):
        from unittest.mock import AsyncMock
        from services.operation_dispatcher import OperationNotDeclared

        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=_artifact_doc())
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with patch(
                "services.operation_dispatcher.dispatch",
                new=AsyncMock(side_effect=OperationNotDeclared("invoke not declared")),
            ):
                r = await client.post("/artifacts/art-1/invoke", json={})
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /artifacts/batch
# ---------------------------------------------------------------------------

class TestBatchFetch:
    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self, anon_client: AsyncClient):
        r = await anon_client.post("/artifacts/batch", json={"artifact_ids": ["a-1"]})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_only_accessible_artifacts(self, client: AsyncClient):
        from fastapi import HTTPException

        arango = MagicMock()
        # Two distinct artifacts; the second one's check_access raises 403.
        docs = {
            "a-1": {"_key": "a-1", "id": "a-1", "context": "{}", "state": "draft"},
            "a-2": {"_key": "a-2", "id": "a-2", "context": "{}", "state": "draft"},
        }
        art = MagicMock()
        art.get.side_effect = lambda k: docs.get(k)
        arango.collection.return_value = art

        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango

        # Override the autouse check_access fixture: a-2 is forbidden.
        def fake_check(auth, aid, action, db):
            if aid == "a-2":
                raise HTTPException(status_code=403)
            return SimpleNamespace(can_read=True)

        try:
            with patch(
                "routers.artifacts_router.check_access", side_effect=fake_check
            ):
                r = await client.post(
                    "/artifacts/batch",
                    json={"artifact_ids": ["a-1", "a-2"]},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        assert r.status_code == 200
        out = r.json()["artifacts"]
        assert [a["id"] for a in out] == ["a-1"]

    @pytest.mark.asyncio
    async def test_container_row_is_normalized_to_artifact_shape(self, client: AsyncClient):
        arango = MagicMock()
        container_doc = {
            "_key": "ws-1",
            "id": "ws-1",
            "name": "Inbox",
            "description": "Seed inbox workspace",
            "content_type": WORKSPACE_CONTENT_TYPE,
            "state": "draft",
        }
        art = MagicMock()
        art.get.side_effect = lambda k: container_doc if k == "ws-1" else None
        arango.collection.return_value = art

        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.post("/artifacts/batch", json={"artifact_ids": ["ws-1"]})
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        assert r.status_code == 200
        out = r.json()["artifacts"]
        assert len(out) == 1
        assert out[0]["id"] == "ws-1"
        assert out[0]["root_id"] == "ws-1"
        # Content defaults to "" for containers (normalization no longer
        # synthesizes content from description).
        assert out[0]["content"] == ""
        # Context defaults to "" when not set (no type-specific synthesis).
        assert out[0]["context"] == ""


# ---------------------------------------------------------------------------
# POST /artifacts/{container_id}/op/commit + /op/commit_preview
# Commit and preview are now dispatched via the operation dispatcher
# through type.json operations blocks on the workspace type.
# ---------------------------------------------------------------------------

class TestCommitArtifacts:
    @pytest.mark.asyncio
    async def test_commit_dispatches_through_op_endpoint(self, client: AsyncClient):
        """Commit is now dispatched via POST /artifacts/{id}/op/commit."""
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=_coll_doc(WORKSPACE_CONTENT_TYPE))
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with patch(
                "services.workspace_service.dispatch_commit",
                return_value={"status": "committed"},
            ), patch(
                "services.operation_dispatcher.dispatch",
                return_value={"status": "committed"},
            ):
                r = await client.post(
                    "/artifacts/container-1/op/commit",
                    json={"artifact_ids": ["a-1"]},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        # The op endpoint returns 200 on success.
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_commit_preview_dispatches_through_op_endpoint(
        self, client: AsyncClient
    ):
        """Preview is now dispatched via POST /artifacts/{id}/op/commit_preview."""
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=_coll_doc(WORKSPACE_CONTENT_TYPE))
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with patch(
                "services.operation_dispatcher.dispatch",
                return_value={"status": "preview"},
            ):
                r = await client.post(
                    "/artifacts/container-1/op/commit_preview",
                    json={},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------

class TestReorder:
    @pytest.mark.asyncio
    async def test_400_when_not_workspace(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=_coll_doc("application/json"))
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.patch(
                "/artifacts/container-1/order",
                json={"ordered_ids": ["a-1", "a-2"]},
            )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_happy_path_calls_service(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=_coll_doc(WORKSPACE_CONTENT_TYPE))
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with patch(
                "services.workspace_service.order_workspace_artifacts",
                return_value=42,
            ) as svc:
                r = await client.patch(
                    "/artifacts/container-1/order",
                    json={"ordered_ids": ["a-1", "a-2"]},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 200
        assert r.json() == {"order_version": 42}
        svc.assert_called_once()


# ---------------------------------------------------------------------------
# Content URL
# ---------------------------------------------------------------------------

class TestContentUrl:
    @pytest.mark.asyncio
    async def test_404_when_no_content_key(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(
            arango,
            artifact_doc={
                "_key": "a-1",
                "context": '{"content_type":"text/plain"}',
                "state": "draft",
            },
        )
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.get("/artifacts/a-1/content-url")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404
        assert "No downloadable content" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_happy_path_returns_signed_url(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(
            arango,
            artifact_doc={
                "_key": "a-1",
                "context": '{"content_key":"u-1/a-1.content","filename":"f.pdf","content_type":"application/pdf"}',
                "state": "draft",
            },
        )
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with patch(
                "services.content_service.generate_signed_url",
                return_value="https://signed.example/x",
            ) as gen:
                r = await client.get("/artifacts/a-1/content-url")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 200
        assert r.json() == {"url": "https://signed.example/x"}
        gen.assert_called_once_with("u-1/a-1.content", filename="f.pdf", content_type="application/pdf", server_facing=False)


# ---------------------------------------------------------------------------
# Upload initiate
# ---------------------------------------------------------------------------

class TestUploadInitiate:
    @pytest.mark.asyncio
    async def test_delegates_to_workspace_service(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango

        artifact = ArtifactEntity(
            id="a-1",
            root_id="a-1",
            collection_id="container-1",
            context="{}",
            content="",
            state=ArtifactEntity.STATE_DRAFT,
        )
        out = {"upload_id": "a-1", "mode": "single", "url": "https://s3/put", "key": "u/a-1.content"}
        try:
            with patch(
                "services.workspace_service.initiate_upload_and_create_artifact",
                return_value=(out, artifact),
            ) as initiate:
                r = await client.post(
                    "/artifacts/container-1/upload-initiate",
                    json={"filename": "f.pdf", "content_type": "application/pdf", "size": 1234},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 200
        body = r.json()
        assert body["upload_id"] == "a-1"
        assert body["mode"] == "single"
        initiate.assert_called_once()


# ---------------------------------------------------------------------------
# List commits
# ---------------------------------------------------------------------------

class TestListCommits:
    @pytest.mark.asyncio
    async def test_400_when_not_collection(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=None)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.get("/artifacts/missing/commits")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_commit_list(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, container_doc=_coll_doc())
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        commits = [
            SimpleNamespace(
                id="c-1",
                collection_id="container-1",
                message="initial",
                author_id="user-123",
                created_time="t0",
                adds=["a-1"],
                removes=[],
            ),
        ]
        try:
            with patch(
                "services.collection_service.get_commits_for_collection",
                return_value=commits,
            ):
                r = await client.get("/artifacts/container-1/commits")
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 200
        body = r.json()
        assert len(body["commits"]) == 1
        assert body["commits"][0]["id"] == "c-1"
        assert body["commits"][0]["message"] == "initial"


# ---------------------------------------------------------------------------
# Revert / Move / Upload-status / Multipart-part-url
#
# Regression cluster: these four endpoints all share the same "find the
# artifact, look up its container" pattern. Pre-fix they checked
# `source != "workspace"` against a tuple where source is always "artifacts",
# so every call returned 404 — and even if that passed, they read
# `workspace_id` instead of the unified-store `collection_id` field.
# These tests lock down the post-fix behavior so the regression cannot
# return.
# ---------------------------------------------------------------------------


class TestRevertArtifact:
    """Revert is now dispatched via POST /artifacts/{id}/op/revert."""

    @pytest.mark.asyncio
    async def test_404_when_artifact_missing(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=None)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.post("/artifacts/missing/op/revert", json={})
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_revert_dispatches_through_op_endpoint(
        self, client: AsyncClient
    ):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=_artifact_doc())
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with patch(
                "services.operation_dispatcher.dispatch",
                return_value={"status": "reverted"},
            ):
                r = await client.post("/artifacts/art-1/op/revert", json={})
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 200


class TestMoveArtifact:
    @pytest.mark.asyncio
    async def test_404_when_missing(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=None)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.post(
                "/artifacts/missing/move",
                json={"target_container_id": "ws-2"},
            )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_happy_path_passes_collection_id_as_source(
        self, client: AsyncClient
    ):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=_artifact_doc())
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango

        moved = ArtifactEntity(
            id="art-1",
            root_id="art-1",
            collection_id="ws-2",
            context="{}",
            content="x",
            state=ArtifactEntity.STATE_DRAFT,
        )
        try:
            with patch(
                "services.workspace_service.move_artifact_between_workspaces",
                return_value=moved,
            ) as svc:
                r = await client.post(
                    "/artifacts/art-1/move",
                    json={"target_container_id": "ws-2"},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        assert r.status_code == 200
        assert r.json()["collection_id"] == "ws-2"
        assert svc.call_args.kwargs["source_workspace_id"] == "container-1"
        assert svc.call_args.kwargs["target_workspace_id"] == "ws-2"


class TestUploadStatus:
    @pytest.mark.asyncio
    async def test_404_when_missing(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=None)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.patch(
                "/artifacts/missing/upload-status",
                json={"status": "uploading", "progress": 0.5},
            )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_happy_path_uses_collection_id(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=_artifact_doc())
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango

        updated = ArtifactEntity(
            id="art-1",
            root_id="art-1",
            collection_id="container-1",
            context='{"upload":{"status":"complete"}}',
            content="",
            state=ArtifactEntity.STATE_DRAFT,
        )
        try:
            with patch(
                "services.workspace_service.update_upload_status",
                return_value=updated,
            ) as svc:
                r = await client.patch(
                    "/artifacts/art-1/upload-status",
                    json={"status": "complete", "progress": 1.0},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        assert r.status_code == 200
        assert svc.call_args.kwargs["workspace_id"] == "container-1"
        assert svc.call_args.kwargs["upload_id"] == "art-1"
        assert svc.call_args.kwargs["status_value"] == "complete"


class TestMultipartPartUrl:
    @pytest.mark.asyncio
    async def test_404_when_missing(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(arango, artifact_doc=None)
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.get(
                "/artifacts/missing/multipart-part-url",
                params={"part_number": 1},
            )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_400_when_no_active_multipart_upload(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(
            arango,
            artifact_doc={
                "_key": "art-1",
                "context": '{"content_type":"text/plain"}',  # no upload block
                "state": "draft",
                "collection_id": "container-1",
            },
        )
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            r = await client.get(
                "/artifacts/art-1/multipart-part-url",
                params={"part_number": 1},
            )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)
        assert r.status_code == 400
        assert "No active multipart upload" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_happy_path_returns_signed_part_url(self, client: AsyncClient):
        arango = MagicMock()
        _patch_db_collection(
            arango,
            artifact_doc={
                "_key": "art-1",
                "context": '{"upload":{"s3_key":"u-1/art-1.content","multipart_id":"mp-9"}}',
                "state": "draft",
                "collection_id": "container-1",
            },
        )
        from core.dependencies import get_arango_db

        app.dependency_overrides[get_arango_db] = lambda: arango
        try:
            with patch(
                "services.content_service.generate_multipart_part_url",
                return_value="https://signed.example/part",
            ) as gen:
                r = await client.get(
                    "/artifacts/art-1/multipart-part-url",
                    params={"part_number": 3},
                )
        finally:
            app.dependency_overrides.pop(get_arango_db, None)

        assert r.status_code == 200
        assert r.json() == {"url": "https://signed.example/part", "part_number": 3}
        gen.assert_called_once_with("u-1/art-1.content", "mp-9", 3)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestSearchArtifacts:
    @pytest.mark.asyncio
    async def test_400_on_empty_query(self, client: AsyncClient):
        r = await client.post("/artifacts/search", json={"query_text": "   "})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_unauthenticated_search_returns_401(self, anon_client: AsyncClient):
        r = await anon_client.post("/artifacts/search", json={"query_text": "x"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_happy_path_returns_hits(self, client: AsyncClient):
        from search.accessor.search_accessor import SearchAccessor

        accessor_result = SimpleNamespace(
            hits=[
                SimpleNamespace(
                    doc_id="a-1",
                    score=1.5,
                    root_id="a-1",
                    version_id="v-1",
                    workspace_id="ws-1",
                    collection_id=None,
                    title="Test Artifact",
                    description="A test artifact",
                    content="Some content here",
                    tags=["test"],
                    highlights=None,
                ),
            ],
            total=1,
            parsed_query="x",
            corrections=[],
            used_hybrid=True,
        )
        with (
            patch(
                "services.collection_service.get_collections_for_user",
                return_value=[],
            ),
            patch.object(SearchAccessor, "search", return_value=accessor_result),
        ):
            r = await client.post(
                "/artifacts/search",
                json={"query_text": "x", "size": 10},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["hits"][0]["id"] == "a-1"
        assert body["used_hybrid"] is True
        assert body["from"] == 0
