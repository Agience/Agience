"""Unit tests for services.workspace_service.

Covers the artifact-lifecycle spine:
  - Commit token HMAC round-trip + tampering detection
  - CommitActor resolution for user vs api_key principals
  - _safe_parse_context tolerates None / bad JSON
  - Workspace CRUD: create, get/owner-mismatch 404, update, delete
  - Artifact create / list / get with collection-scope guard
  - update_artifact state transitions:
      draft  → edited (dirty in-place)
      committed → _ensure_draft promotes to new draft (no committed mutation)
      archive toggle
      no-op when nothing dirty
      409 on editing archived
  - delete_artifact removes edges only when no other versions remain
  - revert_artifact drops draft and returns latest committed
  - commit_workspace_to_collections: dry_run plan, full apply (state flip),
    actor stamping, batch_commit_drafts call shape
  - move_workspace_artifact picks a fractional mid_key between neighbours
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from entities.artifact import Artifact as ArtifactEntity
from entities.collection import Collection as CollectionEntity, WORKSPACE_CONTENT_TYPE
from services import workspace_service as ws_svc


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_s3_put():
    """Stub out content_service.put_text_direct so no real S3 calls are made.

    workspace_service._store_content_in_s3 uploads content to S3 on every
    create/update. For small text (< 128 KB) the content is also kept inline;
    for large content it is cleared from the artifact (stored in S3 only).
    """
    with patch("services.content_service.put_text_direct") as mock_put:
        mock_put.return_value = None
        yield mock_put


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ws(owner: str = "user-1", wid: str = "ws-1") -> CollectionEntity:
    return CollectionEntity(
        id=wid,
        name="My WS",
        created_by=owner,
        content_type=WORKSPACE_CONTENT_TYPE,
        context="",
    )


def _artifact(
    aid: str = "a-1",
    root_id: str | None = None,
    state: str = ArtifactEntity.STATE_DRAFT,
    collection_id: str = "ws-1",
    context: str = '{"content_type":"text/plain"}',
    content: str = "hello",
) -> ArtifactEntity:
    return ArtifactEntity(
        id=aid,
        root_id=root_id or aid,
        collection_id=collection_id,
        context=context,
        content=content,
        state=state,
        created_by="user-1",
        modified_by="user-1",
        created_time="2026-04-07T00:00:00+00:00",
        modified_time="2026-04-07T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Commit token HMAC
# ---------------------------------------------------------------------------

class TestCommitToken:
    def test_round_trip_valid(self):
        tok = ws_svc.generate_commit_token("ws-1", "user-1", ["a", "b"])
        assert ws_svc.validate_commit_token(tok, "ws-1", "user-1", ["a", "b"])

    def test_id_order_does_not_matter(self):
        tok = ws_svc.generate_commit_token("ws-1", "user-1", ["b", "a"])
        # Same set, different order — payload sorts before signing.
        assert ws_svc.validate_commit_token(tok, "ws-1", "user-1", ["a", "b"])

    def test_tampered_workspace_id_rejected(self):
        tok = ws_svc.generate_commit_token("ws-1", "user-1", ["a"])
        assert not ws_svc.validate_commit_token(tok, "ws-2", "user-1", ["a"])

    def test_tampered_user_id_rejected(self):
        tok = ws_svc.generate_commit_token("ws-1", "user-1", ["a"])
        assert not ws_svc.validate_commit_token(tok, "ws-1", "user-2", ["a"])

    def test_tampered_artifact_set_rejected(self):
        tok = ws_svc.generate_commit_token("ws-1", "user-1", ["a"])
        assert not ws_svc.validate_commit_token(tok, "ws-1", "user-1", ["a", "extra"])

    def test_missing_or_malformed_token_rejected(self):
        assert not ws_svc.validate_commit_token(None, "ws", "u", [])
        assert not ws_svc.validate_commit_token("", "ws", "u", [])
        assert not ws_svc.validate_commit_token("no-dot", "ws", "u", [])
        assert not ws_svc.validate_commit_token("notanint.sig", "ws", "u", [])

    def test_expired_token_rejected(self):
        with patch("services.workspace_service.time.time", side_effect=[0, ws_svc._COMMIT_TOKEN_TTL_SECONDS + 1]):
            tok = ws_svc.generate_commit_token("ws-1", "u", None)
        assert not ws_svc.validate_commit_token(tok, "ws-1", "u", None)


# ---------------------------------------------------------------------------
# CommitActor / helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_resolve_commit_actor_user_principal(self):
        actor = ws_svc._resolve_commit_actor("user-1", api_key=None)
        assert actor.actor_type == "user"
        assert actor.actor_id == "user-1"
        assert actor.subject_user_id == "user-1"
        assert actor.api_key_id is None

    def test_resolve_commit_actor_api_key_principal(self):
        api_key = SimpleNamespace(id="key-9")
        actor = ws_svc._resolve_commit_actor("user-1", api_key=api_key)
        assert actor.actor_type == "api_key"
        assert actor.actor_id == "key-9"
        assert actor.subject_user_id == "user-1"
        assert actor.api_key_id == "key-9"

    def test_safe_parse_context_handles_none(self):
        assert ws_svc._safe_parse_context(None) == {}

    def test_safe_parse_context_handles_bad_json(self):
        assert ws_svc._safe_parse_context("not-json") == {}

    def test_safe_parse_context_handles_non_object_json(self):
        assert ws_svc._safe_parse_context("[1,2,3]") == {}

    def test_safe_parse_context_returns_dict(self):
        assert ws_svc._safe_parse_context('{"k":1}') == {"k": 1}

    def test_ensure_json_str_default(self):
        assert ws_svc._ensure_json_str(None) == "{}"

    def test_now_iso_returns_string(self):
        now = ws_svc._now_iso()
        assert isinstance(now, str) and "T" in now


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------

class TestWorkspaceCrud:
    def test_create_workspace_uses_user_id_for_inbox(self):
        db = MagicMock()
        with (
            patch("services.workspace_service.arango.create_collection") as create,
            patch("services.collection_service.ensure_collection_descriptor") as ensure,
        ):
            ws = ws_svc.create_workspace(db, "user-1", "Inbox", is_inbox=True)
        assert ws.id == "user-1"
        assert ws.content_type == WORKSPACE_CONTENT_TYPE
        create.assert_called_once()
        ensure.assert_called_once_with(db, ws)

    def test_create_workspace_generates_uuid_for_normal(self):
        db = MagicMock()
        with (
            patch("services.workspace_service.arango.create_collection"),
            patch("services.collection_service.ensure_collection_descriptor") as ensure,
        ):
            ws = ws_svc.create_workspace(db, "user-1", "Project")
        assert ws.id != "user-1"
        assert ws.created_by == "user-1"
        ensure.assert_called_once_with(db, ws)

    def test_get_workspace_owner_mismatch_404(self):
        db = MagicMock()
        with patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws(owner="someone-else")):
            with pytest.raises(HTTPException) as ei:
                ws_svc.get_workspace(db, "user-1", "ws-1")
        assert ei.value.status_code == 404

    def test_get_workspace_missing_404(self):
        db = MagicMock()
        with patch("services.workspace_service.arango.get_collection_by_id", return_value=None):
            with pytest.raises(HTTPException) as ei:
                ws_svc.get_workspace(db, "user-1", "ws-1")
        assert ei.value.status_code == 404

    def test_update_workspace_renames_when_dirty(self):
        db = MagicMock()
        ws = _ws()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=ws),
            patch("services.workspace_service.arango.update_collection") as update,
            patch("services.collection_service.ensure_collection_descriptor") as ensure,
        ):
            out = ws_svc.update_workspace(db, "user-1", "ws-1", name="New", description=None)
        assert out.name == "New"
        update.assert_called_once()
        ensure.assert_called_once_with(db, ws)

    def test_update_workspace_noop_when_no_change(self):
        db = MagicMock()
        ws = _ws()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=ws),
            patch("services.workspace_service.arango.update_collection") as update,
            patch("services.collection_service.ensure_collection_descriptor") as ensure,
        ):
            ws_svc.update_workspace(db, "user-1", "ws-1", name="My WS", description=None)
        update.assert_not_called()
        ensure.assert_not_called()


# ---------------------------------------------------------------------------
# Artifact CRUD
# ---------------------------------------------------------------------------

class TestArtifactCrud:
    def test_get_workspace_artifact_returns_when_in_workspace(self):
        db = MagicMock()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=_artifact()),
        ):
            out = ws_svc.get_workspace_artifact(db, "user-1", "ws-1", "a-1")
        assert out.id == "a-1"

    def test_get_workspace_artifact_404_when_collection_mismatch(self):
        db = MagicMock()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch(
                "services.workspace_service.arango.get_artifact",
                return_value=_artifact(collection_id="other-ws"),
            ),
        ):
            with pytest.raises(HTTPException) as ei:
                ws_svc.get_workspace_artifact(db, "user-1", "ws-1", "a-1")
        assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# update_artifact state machine
# ---------------------------------------------------------------------------

class TestUpdateArtifactStateMachine:
    @pytest.fixture(autouse=True)
    def _silence_side_effects(self):
        with (
            patch("services.workspace_service._dispatch_handlers"),
            patch("services.workspace_service._emit_event"),
        ):
            yield

    def test_archive_toggle_marks_archived(self):
        db = MagicMock()
        art = _artifact(state=ArtifactEntity.STATE_DRAFT)
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch("services.workspace_service.arango.update_artifact") as upd,
        ):
            out = ws_svc.update_artifact(
                db, "user-1", "ws-1", "a-1", state=ArtifactEntity.STATE_ARCHIVED
            )
        assert out.state == ArtifactEntity.STATE_ARCHIVED
        upd.assert_called_once()

    def test_unarchive_back_to_draft(self):
        db = MagicMock()
        art = _artifact(state=ArtifactEntity.STATE_ARCHIVED)
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch("services.workspace_service.arango.update_artifact"),
        ):
            out = ws_svc.update_artifact(
                db, "user-1", "ws-1", "a-1", state=ArtifactEntity.STATE_DRAFT
            )
        assert out.state == ArtifactEntity.STATE_DRAFT

    def test_editing_archived_without_unarchive_raises_409(self):
        db = MagicMock()
        art = _artifact(state=ArtifactEntity.STATE_ARCHIVED)
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
        ):
            with pytest.raises(HTTPException) as ei:
                ws_svc.update_artifact(
                    db, "user-1", "ws-1", "a-1", content="new"
                )
        assert ei.value.status_code == 409

    def test_editing_committed_promotes_to_new_draft_with_same_root(self):
        db = MagicMock()
        committed = _artifact(state=ArtifactEntity.STATE_COMMITTED)
        # No existing draft for this root.
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=committed),
            patch("services.workspace_service.arango.get_draft_artifact", return_value=None),
            patch("services.workspace_service.arango.create_artifact") as create_new,
            patch("services.workspace_service.arango.update_artifact"),
        ):
            out = ws_svc.update_artifact(
                db, "user-1", "ws-1", "a-1", content="edited", reindex=False
            )
        # A new draft was created with the same root_id, distinct id.
        create_new.assert_called_once()
        new_draft = create_new.call_args[0][1]
        assert new_draft.root_id == committed.root_id
        assert new_draft.id != committed.id
        assert new_draft.state == ArtifactEntity.STATE_DRAFT
        assert out.content == "edited"

    def test_store_content_in_s3_keeps_small_text_inline_clears_large(self):
        small_content = "x" * 1024
        large_content = "x" * (131_072 + 1)
        context = '{"content_type":"text/plain"}'

        small_key, small_inline = ws_svc._store_content_in_s3("a-small", small_content, context)
        large_key, large_inline = ws_svc._store_content_in_s3("a-large", large_content, context)

        assert small_key == "artifacts/a-small.content"
        assert small_inline == small_content
        assert large_key == "artifacts/a-large.content"
        assert large_inline == ""

    def test_editing_committed_reuses_existing_draft(self):
        db = MagicMock()
        committed = _artifact(aid="committed-id", state=ArtifactEntity.STATE_COMMITTED)
        existing_draft = _artifact(aid="draft-id", root_id=committed.root_id)
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=committed),
            patch(
                "services.workspace_service.arango.get_draft_artifact",
                return_value=existing_draft,
            ),
            patch("services.workspace_service.arango.create_artifact") as create_new,
            patch("services.workspace_service.arango.update_artifact"),
        ):
            ws_svc.update_artifact(
                db, "user-1", "ws-1", "committed-id", content="x", reindex=False
            )
        create_new.assert_not_called()

    def test_noop_when_nothing_dirty(self):
        db = MagicMock()
        art = _artifact(content="same", context="same-ctx")
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch("services.workspace_service.arango.update_artifact") as upd,
        ):
            out = ws_svc.update_artifact(
                db, "user-1", "ws-1", "a-1", content="same", context="same-ctx", reindex=False
            )
        upd.assert_not_called()
        assert out is art

    def test_update_content_persists_new_value(self):
        """Editing content on a draft artifact stores the new value and marks dirty."""
        db = MagicMock()
        art = _artifact(content="old content")
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch("services.workspace_service.arango.update_artifact") as upd,
        ):
            out = ws_svc.update_artifact(
                db, "user-1", "ws-1", "a-1", content="new content", reindex=False
            )
        upd.assert_called_once()
        assert out.content == "new content"

    def test_update_context_persists_new_value(self):
        """Editing context on a draft artifact stores the new value and marks dirty."""
        db = MagicMock()
        art = _artifact(context='{"content_type":"text/plain","title":"old"}')
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch("services.workspace_service.arango.update_artifact") as upd,
        ):
            new_ctx = '{"content_type":"text/plain","title":"new"}'
            out = ws_svc.update_artifact(
                db, "user-1", "ws-1", "a-1", context=new_ctx, reindex=False
            )
        upd.assert_called_once()
        assert out.context == new_ctx

    def test_update_content_and_context_together(self):
        """Both content and context can be updated in a single call."""
        db = MagicMock()
        art = _artifact(content="old", context='{"content_type":"text/plain"}')
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch("services.workspace_service.arango.update_artifact") as upd,
        ):
            out = ws_svc.update_artifact(
                db, "user-1", "ws-1", "a-1",
                content="new", context='{"content_type":"text/markdown"}',
                reindex=False,
            )
        upd.assert_called_once()
        assert out.content == "new"


# ---------------------------------------------------------------------------
# delete_artifact / revert_artifact
# ---------------------------------------------------------------------------

class TestDeleteRevert:
    @pytest.fixture(autouse=True)
    def _silence(self):
        with (
            patch("services.workspace_service._dispatch_handlers"),
            patch("services.workspace_service._emit_event"),
            patch("search.ingest.pipeline_unified.delete_artifact_from_index"),
            patch("search.ingest.pipeline_unified.enqueue_index_artifact"),
        ):
            yield

    def test_delete_drops_edges_when_no_other_versions(self):
        db = MagicMock()
        art = _artifact()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch("services.workspace_service.arango.list_version_history", return_value=[art]),
            patch("services.workspace_service.arango.get_draft_artifact", return_value=art),
            patch("services.workspace_service.arango.delete_artifact"),
            patch("services.workspace_service.arango.remove_all_edges_for_root") as remove_edges,
        ):
            ws_svc.delete_artifact(db, "user-1", "ws-1", "a-1")
        remove_edges.assert_called_once_with(db, art.root_id)

    def test_delete_keeps_edges_when_other_versions_exist(self):
        db = MagicMock()
        art = _artifact(aid="a-1")
        sibling = _artifact(aid="a-2", root_id=art.root_id)
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch(
                "services.workspace_service.arango.list_version_history",
                return_value=[art, sibling],
            ),
            patch("services.workspace_service.arango.get_draft_artifact", return_value=art),
            patch("services.workspace_service.arango.delete_artifact"),
            patch("services.workspace_service.arango.remove_all_edges_for_root") as remove_edges,
        ):
            ws_svc.delete_artifact(db, "user-1", "ws-1", "a-1")
        remove_edges.assert_not_called()

    def test_revert_drops_draft_returns_committed(self):
        db = MagicMock()
        draft = _artifact(state=ArtifactEntity.STATE_DRAFT)
        committed = _artifact(aid="committed-id", root_id=draft.root_id, state=ArtifactEntity.STATE_COMMITTED)
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=draft),
            patch(
                "services.workspace_service.arango.get_latest_committed_artifact",
                return_value=committed,
            ),
            patch("services.workspace_service.arango.delete_artifact") as del_a,
        ):
            out = ws_svc.revert_artifact(db, db, "user-1", "ws-1", "a-1")
        assert out is committed
        del_a.assert_called_once_with(db, draft.id)

    def test_revert_returns_target_when_not_a_draft(self):
        db = MagicMock()
        committed = _artifact(state=ArtifactEntity.STATE_COMMITTED)
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=committed),
        ):
            out = ws_svc.revert_artifact(db, db, "user-1", "ws-1", "a-1")
        assert out is committed

    def test_revert_returns_none_when_no_committed_anchor(self):
        db = MagicMock()
        draft = _artifact(state=ArtifactEntity.STATE_DRAFT)
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=draft),
            patch(
                "services.workspace_service.arango.get_latest_committed_artifact",
                return_value=None,
            ),
        ):
            assert ws_svc.revert_artifact(db, db, "user-1", "ws-1", "a-1") is None


# ---------------------------------------------------------------------------
# commit_workspace_to_collections
# ---------------------------------------------------------------------------

class TestCommitWorkspace:
    @pytest.fixture(autouse=True)
    def _silence(self):
        with (
            patch("services.workspace_service._emit_event"),
            patch("search.ingest.pipeline_unified.enqueue_index_artifact"),
        ):
            yield

    def test_dry_run_returns_plan_without_mutation(self):
        db = MagicMock()
        drafts = [_artifact("d-1"), _artifact("d-2")]
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.list_draft_artifacts", return_value=drafts),
            patch("services.workspace_service.arango.batch_commit_drafts") as batch,
        ):
            resp = ws_svc.commit_workspace_to_collections(
                db, db, "user-1", "ws-1", dry_run=True
            )
        batch.assert_not_called()
        assert resp.dry_run is True
        assert resp.plan.total_artifacts == 2
        # commit_token only present on dry-run / no-op responses
        assert resp.commit_token is not None

    def test_apply_flips_state_via_batch_commit(self):
        db = MagicMock()
        drafts = [_artifact("d-1"), _artifact("d-2")]
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.list_draft_artifacts", return_value=drafts),
            patch(
                "services.workspace_service.arango.batch_commit_drafts", return_value=2
            ) as batch,
            patch("services.workspace_service.arango.create_commit"),
            patch("services.workspace_service.arango.create_commit_items"),
        ):
            resp = ws_svc.commit_workspace_to_collections(
                db, db, "user-1", "ws-1", dry_run=False
            )
        batch.assert_called_once()
        assert batch.call_args.kwargs["collection_id"] == "ws-1"
        assert sorted(batch.call_args.kwargs["artifact_ids"]) == ["d-1", "d-2"]
        assert resp.dry_run is False
        assert resp.commit_token is None

    def test_filters_by_artifact_ids_subset(self):
        db = MagicMock()
        drafts = [_artifact("d-1"), _artifact("d-2"), _artifact("d-3")]
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.list_draft_artifacts", return_value=drafts),
            patch("services.workspace_service.arango.batch_commit_drafts", return_value=2),
            patch("services.workspace_service.arango.create_commit"),
            patch("services.workspace_service.arango.create_commit_items"),
        ):
            resp = ws_svc.commit_workspace_to_collections(
                db, db, "user-1", "ws-1", artifact_ids=["d-1", "d-3"], dry_run=False
            )
        assert {c.artifact_id for c in resp.plan.artifacts} == {"d-1", "d-3"}

    def test_no_drafts_returns_empty_plan(self):
        db = MagicMock()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.list_draft_artifacts", return_value=[]),
        ):
            resp = ws_svc.commit_workspace_to_collections(
                db, db, "user-1", "ws-1", dry_run=False
            )
        assert resp.plan.total_artifacts == 0
        assert resp.updated_workspace_artifacts == []

    def test_actor_resolution_user_principal(self):
        db = MagicMock()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.list_draft_artifacts", return_value=[]),
        ):
            resp = ws_svc.commit_workspace_to_collections(
                db, db, "user-1", "ws-1", dry_run=True
            )
        assert resp.actor.actor_type == "user"
        assert resp.actor.actor_id == "user-1"


# ---------------------------------------------------------------------------
# Move / order
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# remove_artifact_from_workspace
# ---------------------------------------------------------------------------

class TestRemoveArtifactFromWorkspace:
    @pytest.fixture(autouse=True)
    def _silence(self):
        with patch("services.workspace_service._emit_event"):
            yield

    def test_404_when_artifact_not_found(self):
        db = MagicMock()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=None),
            patch("services.workspace_service.arango.get_edge", return_value=None),
            patch("services.workspace_service.arango.get_current_in_collection", return_value=None),
        ):
            with pytest.raises(HTTPException) as ei:
                ws_svc.remove_artifact_from_workspace(db, "user-1", "ws-1", "missing")
        assert ei.value.status_code == 404

    def test_removes_edge_for_committed_artifact(self):
        """Committed artifact: edge removed, artifact doc kept."""
        db = MagicMock()
        art = _artifact(state=ArtifactEntity.STATE_COMMITTED)
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch("services.workspace_service.arango.remove_artifact_from_collection") as rm_edge,
            patch("services.workspace_service.arango.delete_artifact") as del_art,
        ):
            result = ws_svc.remove_artifact_from_workspace(db, "user-1", "ws-1", "a-1")

        rm_edge.assert_called_once_with(db, "ws-1", art.root_id)
        del_art.assert_not_called()
        assert result.id == art.id

    def test_removes_edge_and_deletes_draft_doc(self):
        """Draft artifact owned by this workspace: edge removed and draft doc deleted."""
        db = MagicMock()
        art = _artifact(state=ArtifactEntity.STATE_DRAFT, collection_id="ws-1")
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch("services.workspace_service.arango.get_edge", return_value={"_id": "ws-1/a-1"}),
            patch("services.workspace_service.arango.remove_artifact_from_collection") as rm_edge,
            patch("services.workspace_service.arango.get_current_in_collection", return_value=art),
            patch("services.workspace_service.arango.delete_artifact") as del_art,
            patch("search.ingest.pipeline_unified.delete_artifact_from_index"),
        ):
            result = ws_svc.remove_artifact_from_workspace(db, "user-1", "ws-1", "a-1")

        rm_edge.assert_called_once_with(db, "ws-1", art.root_id)
        del_art.assert_called_once_with(db, art.id)
        assert result.id == art.id

    def test_falls_back_to_get_current_when_not_in_workspace(self):
        """Artifact in different collection: fallback to get_current_in_collection."""
        db = MagicMock()
        art = _artifact(state=ArtifactEntity.STATE_COMMITTED, collection_id="other-ws")
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch(
                "services.workspace_service.arango.get_current_in_collection",
                return_value=_artifact(state=ArtifactEntity.STATE_COMMITTED),
            ) as get_current,
            patch("services.workspace_service.arango.remove_artifact_from_collection"),
            patch("services.workspace_service.arango.delete_artifact") as del_art,
        ):
            ws_svc.remove_artifact_from_workspace(db, "user-1", "ws-1", "a-1")

        get_current.assert_called_once_with(db, "ws-1", "a-1")
        del_art.assert_not_called()


class TestMoveAndOrder:
    @pytest.fixture(autouse=True)
    def _silence(self):
        with patch("services.workspace_service._emit_event"):
            yield

    def test_move_artifact_404_when_not_found(self):
        db = MagicMock()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=None),
        ):
            with pytest.raises(HTTPException) as ei:
                ws_svc.move_workspace_artifact(db, "user-1", "ws-1", "missing", None, None)
        assert ei.value.status_code == 404

    def test_move_artifact_picks_mid_key_between_neighbours(self):
        db = MagicMock()
        art = _artifact()
        with (
            patch("services.workspace_service.arango.get_collection_by_id", return_value=_ws()),
            patch("services.workspace_service.arango.get_artifact", return_value=art),
            patch(
                "services.workspace_service.arango.get_edge",
                side_effect=[{"order_key": "a"}, {"order_key": "z"}],
            ),
            patch("services.workspace_service.arango.set_edge_order_key") as set_key,
        ):
            ws_svc.move_workspace_artifact(
                db, "user-1", "ws-1", "a-1", before_id="prev", after_id="next"
            )
        # Whatever mid_key('a','z') returns, it should land between them lexicographically.
        new_key = set_key.call_args[0][3]
        assert "a" < new_key < "z"
