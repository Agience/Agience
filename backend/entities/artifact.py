# entities/artifact.py
# Unified Artifact entity — one table, one entity, three states.
# See .dev/features/unified-artifact-store.md and
# .dev/features/container-as-artifact.md for design rationale.

from typing import Optional, Dict, Any
from entities.base import BaseEntity

# Re-export container content-type constants so importers can get them here.
WORKSPACE_CONTENT_TYPE = "application/vnd.agience.workspace+json"
COLLECTION_CONTENT_TYPE = "application/vnd.agience.collection+json"


class Artifact(BaseEntity):
    """
    Unified artifact entity. Every artifact lives in exactly one collection
    and carries a state in {draft, committed, archived}.

    Container artifacts (workspaces, collections) are distinguished by
    `content_type`. A container IS an artifact — same table, same entity.

    Ordering is NOT stored on the artifact — it lives on the
    `collection_artifacts` edge (see db/arango.py).
    """

    PREFIX = "Artifact"
    STATE_DRAFT = "draft"
    STATE_COMMITTED = "committed"
    STATE_ARCHIVED = "archived"
    VALID_STATES = {STATE_DRAFT, STATE_COMMITTED, STATE_ARCHIVED}

    def __init__(
        self,
        id: Optional[str] = None,
        root_id: Optional[str] = None,
        collection_id: str = "",
        context: str = "",
        content: str = "",
        state: str = STATE_DRAFT,
        created_by: Optional[str] = None,
        created_time: Optional[str] = None,
        modified_by: Optional[str] = None,
        modified_time: Optional[str] = None,
        # Container fields (optional — only set on container artifacts)
        name: Optional[str] = None,
        description: Optional[str] = None,
        content_type: Optional[str] = None,
    ):
        super().__init__(id=id, created_time=created_time, modified_time=modified_time)

        if state not in self.VALID_STATES:
            raise ValueError(f"Invalid state '{state}'")

        # First version of an artifact: id == root_id. The root doc persists forever
        # and is the stable target of `collection_artifacts` edges.
        self.root_id = root_id or self.id
        self.collection_id = collection_id
        self.context = context
        self.content = content
        self.state = state
        self.created_by = created_by
        self.modified_by = modified_by
        self.name = name
        self.description = description
        self.content_type = content_type

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "root_id": self.root_id,
            "collection_id": self.collection_id,
            "context": self.context,
            "content": self.content,
            "state": self.state,
            "created_by": self.created_by,
            "created_time": self.created_time,
            "modified_by": self.modified_by,
            "modified_time": self.modified_time,
        }
        if self.name is not None:
            d["name"] = self.name
        if self.description is not None:
            d["description"] = self.description
        if self.content_type is not None:
            d["content_type"] = self.content_type
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Artifact":
        return cls(
            id=data.get("id") or data.get("_key"),
            root_id=data.get("root_id"),
            collection_id=data.get("collection_id", ""),
            context=data.get("context", ""),
            content=data.get("content", ""),
            state=data.get("state", cls.STATE_DRAFT),
            created_by=data.get("created_by"),
            created_time=data.get("created_time"),
            modified_by=data.get("modified_by"),
            modified_time=data.get("modified_time"),
            name=data.get("name"),
            description=data.get("description"),
            content_type=data.get("content_type"),
        )
