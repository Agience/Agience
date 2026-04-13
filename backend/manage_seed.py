#!/usr/bin/env python3
"""
manage_seed.py -- Platform inbox-seed collection management tool.

Run from the backend/ directory so that imports resolve correctly.

Actions
-------
seed         Ensure the platform "inbox seeds" collection exists (created empty if missing).
             Called automatically at every backend startup -- idempotent.

grant-write  Give a user write access to the inbox-seed collection so they can
             author / edit seed content via the normal UI.  Idempotent.

revoke       Remove a user's grant on the inbox-seed collection.

migrate      Back-fill existing users: issue read grants to the inbox-seed collection
             and grant access to other platform-owned collections.
             Idempotent -- safe to re-run.

Options
-------
--user        User ID (required for grant-write and revoke).
              Find your user_id from the JWT sub claim or the people table.
--dry-run     Print what would happen without making changes.

Workflow (fresh install)
------------------------
  1. Start backend -- seed collection is created automatically (empty).
  2. Grant write access to the admin user:
       python manage_seed.py --action grant-write --user <your-user-id>
  3. Open the app, find "Agience Inbox Seeds" in your collections, and add artifacts.
    4. New user sign-ups will automatically receive the platform collection grants.
  5. For existing users, run migrate:
       python manage_seed.py --action migrate
  6. Revoke admin write access when done:
       python manage_seed.py --action revoke --user <your-user-id>

Examples
--------
  python manage_seed.py --action seed
  python manage_seed.py --action grant-write --user <your-user-id>
  python manage_seed.py --action revoke --user <your-user-id>
  python manage_seed.py --action migrate
  python manage_seed.py --action migrate --dry-run
"""

import argparse
import logging

from arango.database import StandardDatabase
from core import config
from core.config import AGIENCE_PLATFORM_USER_ID
from schemas.arango.initialize import init_arangodb
from services.seed_content_service import (
    INBOX_SEED_COLLECTION_SLUG,
    ensure_inbox_seed_collection,
)
from services.bootstrap_types import (
    INBOX_SEEDS_COLLECTION_SLUG,
    START_HERE_COLLECTION_SLUG,
    PLATFORM_ARTIFACTS_COLLECTION_SLUG,
    ALL_SERVERS_COLLECTION_SLUG,
    ALL_TOOLS_COLLECTION_SLUG,
)
from services.platform_topology import get_id, pre_resolve_platform_ids
from services.platform_settings_service import settings as platform_settings

_ADMIN_WRITE_SLUGS = [
    INBOX_SEEDS_COLLECTION_SLUG,
    START_HERE_COLLECTION_SLUG,
    PLATFORM_ARTIFACTS_COLLECTION_SLUG,
    ALL_SERVERS_COLLECTION_SLUG,
    ALL_TOOLS_COLLECTION_SLUG,
]

_SLUG_NAMES = {
    INBOX_SEEDS_COLLECTION_SLUG: "Agience Inbox Seeds",
    START_HERE_COLLECTION_SLUG: "Start Here",
    PLATFORM_ARTIFACTS_COLLECTION_SLUG: "Platform Artifacts",
    ALL_SERVERS_COLLECTION_SLUG: "All Servers",
    ALL_TOOLS_COLLECTION_SLUG: "All Tools",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("manage_seed")


# â"â" DB connections â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"

def connect() -> StandardDatabase:
    logger.info("Connecting to ArangoDB at %s:%s db=%s...", config.ARANGO_HOST, config.ARANGO_PORT, config.ARANGO_DATABASE)
    db = init_arangodb(
        host=config.ARANGO_HOST,
        port=config.ARANGO_PORT,
        username=config.ARANGO_USERNAME,
        password=config.ARANGO_PASSWORD,
        db_name=config.ARANGO_DATABASE,
    )
    logger.info("Connected.")
    return db


# â"â" Actions â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"â"

def action_seed(db: StandardDatabase, dry_run: bool) -> None:
    """Ensure the inbox-seed collection exists (idempotent, creates empty)."""
    if dry_run:
        logger.info("[DRY-RUN] Would ensure inbox-seed collection exists (slug=%s)", INBOX_SEED_COLLECTION_SLUG)
        return
    col_id = ensure_inbox_seed_collection(db)
    if col_id:
        logger.info("Inbox-seed collection ready (id=%s)", col_id)
    else:
        logger.error("Failed to ensure inbox-seed collection.")


def action_populate(db: StandardDatabase, dry_run: bool) -> None:
    """Populate all inbox-seed sub-collections (Start Here, Platform Artifacts, All Servers, All Tools)."""
    if dry_run:
        logger.info("[DRY-RUN] Would populate: Start Here, Platform Artifacts, All Servers, All Tools.")
        return
    from services.inbox_seeds_content_service import ensure_all_seed_sub_collections
    logger.info("Populating inbox-seed sub-collections...")
    ensure_all_seed_sub_collections(db)
    logger.info("Inbox-seed sub-collections populated successfully.")


def action_grant_write(db: StandardDatabase, user_id: str, dry_run: bool) -> None:
    """Grant admin write access to all inbox-seed collections (parent + all sub-collections)."""
    from db.arango import upsert_user_collection_grant

    if dry_run:
        for slug in _ADMIN_WRITE_SLUGS:
            logger.info("[DRY-RUN] Would grant user %s write access to %s", user_id, _SLUG_NAMES.get(slug, slug))
        return

    for slug in _ADMIN_WRITE_SLUGS:
        col_id = get_id(slug)
        if not col_id:
            logger.warning("Skipping grant for unresolved slug: %s", slug)
            continue
        grant, changed = upsert_user_collection_grant(
            db,
            user_id=user_id,
            collection_id=col_id,
            granted_by=AGIENCE_PLATFORM_USER_ID,
            can_read=True,
            can_update=True,
            name=f"Admin write grant -- {_SLUG_NAMES.get(slug, slug)}",
        )
        if changed:
            logger.info("Granted write access: user=%s  collection=%s (%s)  grant_id=%s", user_id, col_id, _SLUG_NAMES.get(slug, slug), grant.id)


def action_revoke(db: StandardDatabase, user_id: str, dry_run: bool) -> None:
    """Revoke all active grants for a user on the inbox-seed collection."""
    from db.arango import get_active_grants_for_principal_resource, update_grant
    from datetime import datetime, timezone

    col_id = get_id(INBOX_SEEDS_COLLECTION_SLUG)
    grants = get_active_grants_for_principal_resource(
        db, grantee_id=user_id, resource_type="collection", resource_id=col_id
    )
    if not grants:
        logger.info("No active grant for user=%s  collection=%s -- nothing to revoke.", user_id, col_id)
        return

    for g in grants:
        if dry_run:
            logger.info("[DRY-RUN] Would revoke grant %s (user=%s  collection=%s)", g.id, user_id, col_id)
        else:
            now = datetime.now(timezone.utc).isoformat()
            g.state = "revoked"
            g.modified_time = now
            update_grant(db, g)
            logger.info("Revoked grant %s (user=%s  collection=%s)", g.id, user_id, col_id)


def action_migrate(arango_db: StandardDatabase, dry_run: bool) -> None:
    """
    Back-fill all existing users:
    1. Issue a read grant to the inbox-seed collection (idempotent).
    2. Grant access to other platform-owned collections (for example the current host collection).
    """
    from db.arango_identity import list_all_people
    from services.seed_content_service import apply_platform_collections_to_user

    col_id = ensure_inbox_seed_collection(arango_db)
    if not col_id:
        logger.error("Inbox-seed collection could not be ensured. Aborting migration.")
        return

    people = list_all_people(arango_db)
    logger.info("Found %d users to migrate.", len(people))
    success = errors = 0

    for person in people:
        person_id = person.get("id") or person.get("_key")
        label = person.get("email", "") or person_id
        if dry_run:
            logger.info("  [DRY-RUN] %s -- would apply inbox seeds.", label)
            continue
        try:
            apply_platform_collections_to_user(arango_db, user_id=person_id)
            success += 1
        except Exception as exc:
            logger.warning("  Migration failed for %s: %s", label, exc)
            errors += 1

    logger.info("Migration complete: %d succeeded, %d errors.", success, errors)


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Manage Agience platform inbox-seed collection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--action",
        choices=["seed", "populate", "grant-write", "revoke", "migrate"],
        required=True,
        help="What to do.",
    )
    parser.add_argument(
        "--user",
        help="User ID (required for grant-write and revoke).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without writing anything.",
    )
    args = parser.parse_args()

    if args.action in ("grant-write", "revoke") and not args.user:
        parser.error(f"--user is required for --action {args.action}")

    db = connect()
    platform_settings.load_all(db)
    pre_resolve_platform_ids(db)
    from services import server_registry
    server_registry.populate_ids()

    if args.action == "seed":
        action_seed(db, args.dry_run)
    elif args.action == "populate":
        action_populate(db, args.dry_run)
    elif args.action == "grant-write":
        action_grant_write(db, args.user, args.dry_run)
    elif args.action == "revoke":
        action_revoke(db, args.user, args.dry_run)
    elif args.action == "migrate":
        action_migrate(db, args.dry_run)


if __name__ == "__main__":
    main()
