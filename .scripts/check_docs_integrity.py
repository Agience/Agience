"""Docs integrity checks for the current public docs model.

This repo now uses a split documentation structure:

- docs/ -> public-facing overview, getting-started, and reference docs
- .dev/  -> internal design specs, audits, plans, and future-state material

This script intentionally validates only the current docs/ surface. It aims to catch:

1) Inventory drift: a docs/ file exists but is not listed in the public inventory
2) Stale inventory entries: a path is listed in the inventory but no longer exists
3) Missing public doc status headers in Markdown files
4) Broken relative links within docs/ markdown files (replicates lychee --offline)

Exit codes:
- 0: OK
- 1: problems found
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


STATUS_RE = re.compile(r"^Status:\s*\*\*[^*]+\*\*", re.IGNORECASE | re.MULTILINE)

# Matches markdown links: [text](href) and bare <file://...> autolinks
_LINK_HREF_RE = re.compile(r"\[(?:[^\]]*)\]\(([^)]+)\)")
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "ftp://", "file://", "//")


def _is_external(href: str) -> bool:
    return href.startswith(_EXTERNAL_SCHEMES) or href.startswith("#")


def check_relative_links(repo_root: Path) -> list[str]:
    """Return BROKEN_LINK problems for every unresolvable relative link in docs/."""
    docs_root = repo_root / "docs"
    problems: list[str] = []
    for md_file in sorted(docs_root.rglob("*.md")):
        text = md_file.read_text(encoding="utf-8", errors="replace")
        for m in _LINK_HREF_RE.finditer(text):
            href = m.group(1).strip()
            if _is_external(href):
                continue
            # Strip inline anchor (path#section -> path)
            path_part = href.split("#")[0].strip()
            if not path_part:
                continue
            target = (md_file.parent / path_part).resolve()
            if not target.exists():
                rel_file = md_file.relative_to(repo_root).as_posix()
                problems.append(f"BROKEN_LINK: {rel_file} -> {path_part}")
    return problems


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def list_docs_files(repo_root: Path) -> list[Path]:
    docs_root = repo_root / "docs"
    files = [p for p in docs_root.rglob("*") if p.is_file()]
    files.sort(key=lambda p: p.relative_to(repo_root).as_posix().lower())
    return files


def ensure_docs_roots(repo_root: Path) -> list[str]:
    docs_root = repo_root / "docs"
    if not docs_root.exists():
        return ["MISSING_DOCS_DIR: docs/"]
    bad: list[str] = []
    legacy_docs = repo_root / ".docs"
    if legacy_docs.exists():
        bad.append("LEGACY_DOCS_DIR_PRESENT: .docs/")
    return bad


def parse_sources_listed_paths(sources_text: str) -> set[str]:
    found = set(re.findall(r"`(docs/[^`]+?)`", sources_text))
    return found


def has_status_banner(file_text: str) -> bool:
    head = "\n".join(file_text.splitlines()[:40])
    return STATUS_RE.search(head) is not None


def main() -> int:
    repo_root = repo_root_from_script()
    sources_path = repo_root / ".dev" / "_audits" / "sources.md"

    problems: list[str] = []

    # 0) Enforce current docs roots
    problems.extend(ensure_docs_roots(repo_root))

    if not sources_path.exists():
        problems.append("MISSING_PUBLIC_SOURCES_INVENTORY: .dev/_audits/sources.md")
        print("Docs integrity check: FAILED\n")
        for p in problems:
            print(p)
        print(f"\nTotal problems: {len(problems)}")
        return 1

    # 1) Inventory drift
    all_docs_files = list_docs_files(repo_root)
    sources_text = read_text(sources_path)
    listed = parse_sources_listed_paths(sources_text)

    for path in all_docs_files:
        rel = path.relative_to(repo_root).as_posix()
        if rel not in listed:
            problems.append(f"MISSING_IN_SOURCES: {rel}")

    # 2) Stale inventory entries
    for rel in sorted(listed):
        file_path = repo_root / rel
        if not file_path.exists():
            problems.append(f"STALE_IN_SOURCES: {rel}")

    # 3) Public markdown governance: require a Status banner
    for path in all_docs_files:
        if path.suffix.lower() != ".md":
            continue
        rel = path.relative_to(repo_root).as_posix()
        text = read_text(path)
        if not has_status_banner(text):
            problems.append(f"MISSING_STATUS_BANNER: {rel}")

    # 4) Broken relative links (equivalent to lychee --offline)
    problems.extend(check_relative_links(repo_root))

    if problems:
        print("Docs integrity check: FAILED\n")
        for p in problems:
            print(p)
        print(f"\nTotal problems: {len(problems)}")
        return 1

    print("Docs integrity check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
