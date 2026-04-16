# entities/grant.py
"""
Grant entity -- unified authorization record.

CRUDIASO permission model: Create, Read, Update, Delete, Invoke, Add, Share, Admin.

S (Share) = ability to create invites / share the resource.
O (Admin) = meta-permission to manage grants on the resource.

See: .dev/features/unified-artifact-api.md
"""

from typing import Optional, Dict, Any
from entities.base import BaseEntity


class Grant(BaseEntity):
    """
    A grant links a principal (user, api_key, invite, group, grant_key) to a
    resource (artifact) with CRUDIASO permissions.
    """

    PREFIX = "Grant"

    # Valid grantee types
    GRANTEE_USER = "user"
    GRANTEE_API_KEY = "api_key"
    GRANTEE_INVITE = "invite"
    GRANTEE_GROUP = "group"
    GRANTEE_GRANT_KEY = "grant_key"

    # Valid states
    STATE_ACTIVE = "active"
    STATE_REVOKED = "revoked"
    STATE_PENDING_ACCEPT = "pending_accept"

    # Valid effects
    EFFECT_ALLOW = "allow"
    EFFECT_DENY = "deny"

    # Role presets --- named permission bundles for the share/invite flow.
    #
    # Use via ``Grant.permissions_for_role(role_name)`` rather than reading
    # this dict directly so new roles can be added without callers needing
    # to know about every flag.
    ROLE_PRESETS: Dict[str, Dict[str, bool]] = {
        "viewer": {
            "can_read": True,
        },
        "editor": {
            "can_create": True,
            "can_read": True,
            "can_update": True,
            "can_delete": True,
        },
        "collaborator": {
            "can_create": True,
            "can_read": True,
            "can_update": True,
            "can_delete": True,
            "can_invoke": True,
            "can_add": True,
            "can_share": True,
        },
        "admin": {
            "can_create": True,
            "can_read": True,
            "can_update": True,
            "can_delete": True,
            "can_invoke": True,
            "can_add": True,
            "can_share": True,
            "can_admin": True,
        },
    }

    @classmethod
    def permissions_for_role(cls, role: str) -> Dict[str, bool]:
        """Return the CRUDIASO bit pattern for a named role.

        Raises :class:`ValueError` if *role* is not a known preset.
        """
        preset = cls.ROLE_PRESETS.get(role)
        if preset is None:
            raise ValueError(
                f"Unknown role {role!r}; valid roles: {sorted(cls.ROLE_PRESETS)}"
            )
        return dict(preset)

    def __init__(
        self,
        resource_type: str,           # "artifact" or "collection"
        resource_id: str,             # artifact_id
        grantee_type: str,            # "user" | "api_key" | "invite" | "group" | "grant_key"
        grantee_id: str,              # user_id | api_key.id | claim_token_hash | group_artifact_id
        granted_by: str,              # user_id of the issuer
        effect: str = "allow",        # "allow" | "deny"
        # CRUDIASO permission flags
        can_create: bool = False,
        can_read: bool = True,
        can_update: bool = False,
        can_delete: bool = False,
        can_invoke: bool = False,
        can_add: bool = False,
        can_share: bool = False,
        can_admin: bool = False,
        # Identity requirements
        requires_identity: bool = False,
        read_requires_identity: Optional[bool] = None,
        write_requires_identity: Optional[bool] = None,
        invoke_requires_identity: Optional[bool] = None,
        # Invite targeting (grantee_type == "invite" only)
        target_entity: Optional[str] = None,         # email, user_id, google_id, domain, etc.
        target_entity_type: Optional[str] = None,    # "email" | "user_id" | "google_id" | "domain"
        max_claims: Optional[int] = None,            # None = unlimited, 0 = frozen, 1 = single-use
        claims_count: int = 0,
        # Lifecycle
        state: str = "active",
        id: Optional[str] = None,
        name: Optional[str] = None,
        notes: Optional[str] = None,
        granted_at: Optional[str] = None,
        expires_at: Optional[str] = None,
        accepted_by: Optional[str] = None,
        accepted_at: Optional[str] = None,
        revoked_by: Optional[str] = None,
        revoked_at: Optional[str] = None,
        created_time: Optional[str] = None,
        modified_time: Optional[str] = None,
    ):
        super().__init__(id=id, created_time=created_time, modified_time=modified_time)
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.grantee_type = grantee_type
        self.grantee_id = grantee_id
        self.granted_by = granted_by
        self.effect = effect
        # CRUDIASO
        self.can_create = can_create
        self.can_read = can_read
        self.can_update = can_update
        self.can_delete = can_delete
        self.can_invoke = can_invoke
        self.can_add = can_add
        self.can_share = can_share
        self.can_admin = can_admin
        # Identity requirements
        self.requires_identity = requires_identity
        self.read_requires_identity = read_requires_identity
        self.write_requires_identity = write_requires_identity
        self.invoke_requires_identity = invoke_requires_identity
        # Invite targeting
        self.target_entity = target_entity
        self.target_entity_type = target_entity_type
        self.max_claims = max_claims
        self.claims_count = claims_count
        # Lifecycle
        self.state = state
        self.name = name
        self.notes = notes
        self.granted_at = granted_at or self.created_time
        self.expires_at = expires_at
        self.accepted_by = accepted_by
        self.accepted_at = accepted_at
        self.revoked_by = revoked_by
        self.revoked_at = revoked_at

    def is_active(self) -> bool:
        return self.state == self.STATE_ACTIVE

    def to_dict(self) -> Dict[str, Any]:
        base = self.to_dict_base()
        base.update({
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "grantee_type": self.grantee_type,
            "grantee_id": self.grantee_id,
            "granted_by": self.granted_by,
            "effect": self.effect,
            # CRUDIASO
            "can_create": self.can_create,
            "can_read": self.can_read,
            "can_update": self.can_update,
            "can_delete": self.can_delete,
            "can_invoke": self.can_invoke,
            "can_add": self.can_add,
            "can_share": self.can_share,
            "can_admin": self.can_admin,
            # Identity
            "requires_identity": self.requires_identity,
            "read_requires_identity": self.read_requires_identity,
            "write_requires_identity": self.write_requires_identity,
            "invoke_requires_identity": self.invoke_requires_identity,
            # Invite
            "target_entity": self.target_entity,
            "target_entity_type": self.target_entity_type,
            "max_claims": self.max_claims,
            "claims_count": self.claims_count,
            # Lifecycle
            "state": self.state,
            "name": self.name,
            "notes": self.notes,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "accepted_by": self.accepted_by,
            "accepted_at": self.accepted_at,
            "revoked_by": self.revoked_by,
            "revoked_at": self.revoked_at,
        })
        return base

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Grant":
        base = cls.from_dict_base(data)
        can_update = data.get("can_update", False)
        return cls(
            id=base["id"],
            resource_type=data.get("resource_type", ""),
            resource_id=data.get("resource_id", ""),
            grantee_type=data.get("grantee_type", ""),
            grantee_id=data.get("grantee_id", ""),
            granted_by=data.get("granted_by", ""),
            effect=data.get("effect", "allow"),
            # CRUDIASO
            can_create=data.get("can_create", False),
            can_read=data.get("can_read", True),
            can_update=can_update,
            can_delete=data.get("can_delete", False),
            can_invoke=data.get("can_invoke", False),
            can_add=data.get("can_add", False),
            # Legacy fallbacks for DB docs written before the CRUDIASO rename:
            # ``can_search`` → ``can_share`` (S repurposed from Search to Share)
            # ``can_own``    → ``can_admin`` (O renamed for clarity)
            can_share=data.get("can_share", data.get("can_search", data.get("can_read", False))),
            can_admin=data.get("can_admin", data.get("can_own", False)),
            # Identity
            requires_identity=data.get("requires_identity", False),
            read_requires_identity=data.get("read_requires_identity"),
            write_requires_identity=data.get("write_requires_identity"),
            invoke_requires_identity=data.get("invoke_requires_identity"),
            # Invite
            target_entity=data.get("target_entity"),
            target_entity_type=data.get("target_entity_type"),
            max_claims=data.get("max_claims"),
            claims_count=data.get("claims_count", 0),
            # Lifecycle
            state=data.get("state", "active"),
            name=data.get("name"),
            notes=data.get("notes"),
            granted_at=data.get("granted_at"),
            expires_at=data.get("expires_at"),
            accepted_by=data.get("accepted_by"),
            accepted_at=data.get("accepted_at"),
            revoked_by=data.get("revoked_by"),
            revoked_at=data.get("revoked_at"),
            created_time=base["created_time"],
            modified_time=base["modified_time"],
        )
