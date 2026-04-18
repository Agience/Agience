# db/arango_init.py
"""
ArangoDB initialization - creates database, collections, and indexes.

Unified artifact store: one `artifacts` collection, `collection_artifacts`
edge collection, with `content_type` discriminator on container artifacts.
"""
import logging
from arango.client import ArangoClient
from arango.database import StandardDatabase
from arango.exceptions import DatabaseCreateError, CollectionCreateError

logger = logging.getLogger(__name__)


def init_arangodb(host: str, port: int, username: str, password: str, db_name: str) -> StandardDatabase:
    """
    Initialize ArangoDB: create database, collections, indexes, and graph.
    Returns the database instance ready for use.
    """
    client = ArangoClient(hosts=f"http://{host}:{port}")
    sys_db = client.db("_system", username=username, password=password)

    db_created = False
    if not sys_db.has_database(db_name):
        try:
            sys_db.create_database(db_name)
            db_created = True
            logger.info(f"Created database: {db_name}")
        except DatabaseCreateError as e:
            logger.error(f"Failed to create database {db_name}: {e}")
            raise

    db = client.db(db_name, username=username, password=password)

    document_collections = [
        "artifacts",
        "grants",
        "api_keys",
        "server_credentials",
        "server_keys",
        "commits",
        "commit_items",
        "people",
        "platform_settings",
        "passkey_credentials",
        "otp_codes",
        "entitlement_cache",
        "usage_tallies",
    ]

    edge_collections = [
        "collection_artifacts",  # _from: artifacts/{id}, _to: artifacts/{root_id}
    ]

    created_document_collections = 0
    created_edge_collections = 0

    for coll_name in document_collections:
        if not db.has_collection(coll_name):
            try:
                db.create_collection(coll_name)
                created_document_collections += 1
                logger.info(f"Created collection: {coll_name}")
            except CollectionCreateError as e:
                logger.warning(f"Collection {coll_name} may already exist: {e}")

    for edge_name in edge_collections:
        if not db.has_collection(edge_name):
            try:
                db.create_collection(edge_name, edge=True)
                created_edge_collections += 1
                logger.info(f"Created edge collection: {edge_name}")
            except CollectionCreateError as e:
                logger.warning(f"Edge collection {edge_name} may already exist: {e}")

    created_indexes = _create_indexes(db)
    graph_created = _create_graph(db)
    _backfill_edge_fields(db)

    logger.info(
        "ArangoDB schema ensure complete (db_created=%s, collections_created=%d, edge_collections_created=%d, indexes_created=%d, graph_created=%s)",
        db_created,
        created_document_collections,
        created_edge_collections,
        created_indexes,
        graph_created,
    )
    return db


def _index_exists(collection, fields: list, index_type: str = "hash") -> bool:
    try:
        indexes = collection.indexes()
        for idx in indexes:
            if idx.get("type") == index_type and idx.get("fields") == fields:
                return True
        return False
    except Exception:
        return False


def _create_indexes(db: StandardDatabase) -> int:
    """Create indexes for common query patterns."""
    created_indexes = 0

    # --- artifacts indexes (unified store) -----------------------------
    artifacts = db.collection("artifacts")

    # Fast collection listing by state.
    if not _index_exists(artifacts, ["collection_id", "state"]):
        artifacts.add_hash_index(fields=["collection_id", "state"], unique=False)
        created_indexes += 1
        logger.info("Created index: artifacts.(collection_id,state)")

    # Version history for a root concept.
    if not _index_exists(artifacts, ["root_id", "state"]):
        artifacts.add_hash_index(fields=["root_id", "state"], unique=False)
        created_indexes += 1
        logger.info("Created index: artifacts.(root_id,state)")

    # Scoped version lookup — NOT unique; multiple committed versions co-exist
    # as history. The "one draft per (root_id, collection_id)" invariant is
    # enforced in application logic.
    if not _index_exists(artifacts, ["root_id", "collection_id"]):
        artifacts.add_hash_index(fields=["root_id", "collection_id"], unique=False)
        created_indexes += 1
        logger.info("Created index: artifacts.(root_id,collection_id)")

    # created_by for user-scoped queries.
    if not _index_exists(artifacts, ["created_by"]):
        artifacts.add_hash_index(fields=["created_by"], unique=False)
        created_indexes += 1
        logger.info("Created index: artifacts.created_by")

    # Slug uniqueness (sparse — most artifacts have no slug).
    if not _index_exists(artifacts, ["slug"]):
        artifacts.add_hash_index(fields=["slug"], unique=True, sparse=True)
        created_indexes += 1
        logger.info("Created index: artifacts.slug (unique, sparse)")

    # Container listing by type (workspace / collection discriminator).
    if not _index_exists(artifacts, ["content_type"]):
        artifacts.add_hash_index(fields=["content_type"], unique=False, sparse=True)
        created_indexes += 1
        logger.info("Created index: artifacts.content_type (sparse)")

    # Owner-scoped container queries.
    if not _index_exists(artifacts, ["content_type", "created_by"]):
        artifacts.add_hash_index(fields=["content_type", "created_by"], unique=False, sparse=True)
        created_indexes += 1
        logger.info("Created index: artifacts.(content_type,created_by) (sparse)")

    # --- grants indexes ------------------------------------------------
    grants = db.collection("grants")

    if not _index_exists(grants, ["resource_id", "state"], "hash"):
        grants.add_hash_index(fields=["resource_id", "state"], unique=False)
        created_indexes += 1
        logger.info("Created index: grants.(resource_id,state)")

    if not _index_exists(grants, ["grantee_id", "state"], "hash"):
        grants.add_hash_index(fields=["grantee_id", "state"], unique=False)
        created_indexes += 1
        logger.info("Created index: grants.(grantee_id,state)")

    if not _index_exists(grants, ["grantee_type", "grantee_id", "state"], "hash"):
        grants.add_hash_index(fields=["grantee_type", "grantee_id", "state"], unique=False)
        created_indexes += 1
        logger.info("Created index: grants.(grantee_type,grantee_id,state)")

    if not _index_exists(grants, ["resource_id", "grantee_type", "state"], "hash"):
        grants.add_hash_index(fields=["resource_id", "grantee_type", "state"], unique=False)
        created_indexes += 1
        logger.info("Created index: grants.(resource_id,grantee_type,state)")

    if not _index_exists(grants, ["expires_at"], "hash"):
        grants.add_hash_index(fields=["expires_at"], unique=False, sparse=True)
        created_indexes += 1
        logger.info("Created index: grants.expires_at (sparse)")

    # --- api_keys indexes ----------------------------------------------
    api_keys = db.collection("api_keys")

    if not _index_exists(api_keys, ["key_hash"]):
        api_keys.add_hash_index(fields=["key_hash"], unique=True)
        created_indexes += 1
        logger.info("Created index: api_keys.key_hash (unique)")

    if not _index_exists(api_keys, ["user_id"]):
        api_keys.add_hash_index(fields=["user_id"], unique=False)
        created_indexes += 1
        logger.info("Created index: api_keys.user_id")

    if not _index_exists(api_keys, ["is_active"]):
        api_keys.add_hash_index(fields=["is_active"], unique=False)
        created_indexes += 1
        logger.info("Created index: api_keys.is_active")

    # --- server_credentials indexes ------------------------------------
    server_credentials = db.collection("server_credentials")

    if not _index_exists(server_credentials, ["client_id"]):
        server_credentials.add_hash_index(fields=["client_id"], unique=True)
        created_indexes += 1
        logger.info("Created index: server_credentials.client_id (unique)")

    if not _index_exists(server_credentials, ["user_id"]):
        server_credentials.add_hash_index(fields=["user_id"], unique=False)
        created_indexes += 1
        logger.info("Created index: server_credentials.user_id")

    if not _index_exists(server_credentials, ["is_active"]):
        server_credentials.add_hash_index(fields=["is_active"], unique=False)
        created_indexes += 1
        logger.info("Created index: server_credentials.is_active")

    # --- commits / commit_items indexes --------------------------------
    commits = db.collection("commits")

    if not _index_exists(commits, ["collection_id"]):
        commits.add_hash_index(fields=["collection_id"], unique=False)
        created_indexes += 1
        logger.info("Created index: commits.collection_id")

    commit_items = db.collection("commit_items")

    if not _index_exists(commit_items, ["commit_id"]):
        commit_items.add_hash_index(fields=["commit_id"], unique=False)
        created_indexes += 1
        logger.info("Created index: commit_items.commit_id")

    if not _index_exists(commit_items, ["artifact_root_id"]):
        commit_items.add_hash_index(fields=["artifact_root_id"], unique=False)
        created_indexes += 1
        logger.info("Created index: commit_items.artifact_root_id")

    # --- people indexes ------------------------------------------------
    people = db.collection("people")

    if not _index_exists(people, ["email"]):
        people.add_hash_index(fields=["email"], unique=True)
        created_indexes += 1
        logger.info("Created index: people.email (unique)")

    if not _index_exists(people, ["oidc_provider", "oidc_subject"]):
        people.add_hash_index(fields=["oidc_provider", "oidc_subject"], unique=True, sparse=True)
        created_indexes += 1
        logger.info("Created index: people.(oidc_provider,oidc_subject) (unique, sparse)")

    if not _index_exists(people, ["google_id"]):
        people.add_hash_index(fields=["google_id"], unique=True, sparse=True)
        created_indexes += 1
        logger.info("Created index: people.google_id (unique, sparse)")

    # --- platform_settings / passkey / otp -----------------------------
    platform_settings = db.collection("platform_settings")
    if not _index_exists(platform_settings, ["category"]):
        platform_settings.add_hash_index(fields=["category"], unique=False)
        created_indexes += 1
        logger.info("Created index: platform_settings.category")

    passkey_credentials = db.collection("passkey_credentials")
    if not _index_exists(passkey_credentials, ["person_id"]):
        passkey_credentials.add_hash_index(fields=["person_id"], unique=False)
        created_indexes += 1
        logger.info("Created index: passkey_credentials.person_id")

    otp_codes = db.collection("otp_codes")
    if not _index_exists(otp_codes, ["email"]):
        otp_codes.add_hash_index(fields=["email"], unique=False)
        created_indexes += 1
        logger.info("Created index: otp_codes.email")

    if not _index_exists(otp_codes, ["expires_at"], "ttl"):
        otp_codes.add_ttl_index(fields=["expires_at"], expiry_time=0)
        created_indexes += 1
        logger.info("Created index: otp_codes.expires_at (TTL)")

    # --- collection_artifacts edge indexes -----------------------------
    edges = db.collection("collection_artifacts")

    if not _index_exists(edges, ["root_id"]):
        edges.add_hash_index(fields=["root_id"], unique=False)
        created_indexes += 1
        logger.info("Created index: collection_artifacts.root_id")

    if not _index_exists(edges, ["order_key"]):
        edges.add_persistent_index(fields=["order_key"], unique=False, sparse=True)
        created_indexes += 1
        logger.info("Created index: collection_artifacts.order_key (persistent, sparse)")

    logger.info("Index creation complete")
    return created_indexes


def _create_graph(db: StandardDatabase) -> bool:
    """Create graph definition for traversal queries."""
    graph_name = "agience_graph"

    if db.has_graph(graph_name):
        return False

    edge_definitions = [
        {
            "edge_collection": "collection_artifacts",
            "from_vertex_collections": ["artifacts"],
            "to_vertex_collections": ["artifacts"],
        },
    ]

    try:
        db.create_graph(name=graph_name, edge_definitions=edge_definitions)
        logger.info(f"Created graph: {graph_name}")
        return True
    except Exception as e:
        logger.warning(f"Graph {graph_name} may already exist: {e}")
        return False


def _backfill_edge_fields(db: StandardDatabase) -> None:
    """Backfill ``origin`` and ``propagate`` on edges missing these fields.

    Sets creation-edge defaults: ``origin: true``, ``propagate: null``
    (null = all actions propagate).
    """
    try:
        cursor = db.aql.execute(
            """
            FOR e IN collection_artifacts
              FILTER !HAS(e, "origin")
              UPDATE e WITH { origin: true, propagate: null } IN collection_artifacts
              RETURN 1
            """
        )
        count = len(list(cursor))
        if count > 0:
            logger.info("Backfilled origin/propagate on %d edges", count)
    except Exception as e:
        logger.warning("Edge backfill failed (will retry next startup): %s", e)


def get_arangodb_connection(host: str, port: int, username: str, password: str, db_name: str) -> StandardDatabase:
    client = ArangoClient(hosts=f"http://{host}:{port}")
    return client.db(db_name, username=username, password=password)
