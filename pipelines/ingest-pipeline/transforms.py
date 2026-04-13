"""
Transform artifact definitions for the Agience multi-modal ingestion pipeline.

Each entry is a dict describing one Transform artifact to create in the workspace.
The "slug" field is used as a stable idempotency key — re-running install.py
will update existing artifacts rather than creating duplicates.

Swapping extractors
-------------------
Users who have a third-party MCP server (e.g. a Docling server) can update
the "pdf-text-extract" transform's run.server and run.tool to point at it.
The server must return the same output shape as Astra's document_text_extract:

    {
        "status": "ok",
        "source_artifact_id": str,
        "text_artifact_id": str,
        "title": str,
        "length": int,
        "pages": int,
        "method": str,        # e.g. "docling"
        "content_hash": str,
        "images": [...],      # optional — populated by advanced extractors
        "tables": [...]       # optional
    }
"""

TRANSFORMS: list[dict] = [
    # ------------------------------------------------------------------
    # Step 1 — Deduplication
    # ------------------------------------------------------------------
    {
        "slug": "ingest-dedup",
        "context": {
            "content_type": "application/vnd.agience.transform+json",
            "title": "Deduplication Check",
            "description": (
                "Computes a SHA-256 hash of the artifact content and checks the workspace "
                "for prior ingestions of the same file. Returns 'unique' or 'duplicate'."
            ),
            "transform": {"kind": "ingest", "subtype": "dedup"},
            "run": {
                "type": "mcp-tool",
                "server": "astra",
                "tool": "deduplicate",
                "input_mapping": {
                    "workspace_id": "$.workspace_id",
                    "artifact_id": "$.artifacts[0]",
                },
            },
            "input": {
                "description": "A workspace artifact to check for duplicates.",
                "artifacts": {"min": 1, "max": 1},
            },
            "output": {
                "description": 'JSON: {"status": "unique"|"duplicate", "content_hash": str, ...}',
            },
        },
    },

    # ------------------------------------------------------------------
    # Step 2 — PDF Text Extraction (built-in PyPDF)
    # To use Docling or another advanced extractor, update run.server and
    # run.tool to point at your registered third-party MCP server artifact.
    # ------------------------------------------------------------------
    {
        "slug": "ingest-pdf-extract",
        "context": {
            "content_type": "application/vnd.agience.transform+json",
            "title": "PDF Text Extraction",
            "description": (
                "Extracts text from a PDF artifact using Astra's built-in PyPDF extractor "
                "and creates a derived text/markdown artifact. "
                "Swap run.server / run.tool to use a Docling or other third-party MCP server."
            ),
            "transform": {"kind": "ingest", "subtype": "pdf-extract"},
            "run": {
                "type": "mcp-tool",
                "server": "astra",
                "tool": "document_text_extract",
                "input_mapping": {
                    "workspace_id": "$.workspace_id",
                    "source_artifact_id": "$.artifacts[0]",
                },
            },
            "input": {
                "description": "A PDF artifact in the workspace (must have content_key in S3).",
                "artifacts": {"min": 1, "max": 1},
            },
            "output": {
                "description": (
                    "JSON: {status, source_artifact_id, text_artifact_id, title, "
                    "length, pages, method, content_hash, images, tables}"
                ),
            },
        },
    },

    # ------------------------------------------------------------------
    # Step 3 — LLM Metadata Extraction
    # Uses the workspace LLM connection. Expects $.extracted_text in params.
    # ------------------------------------------------------------------
    {
        "slug": "ingest-extract-metadata",
        "context": {
            "content_type": "application/vnd.agience.transform+json",
            "title": "Document Metadata Extraction",
            "description": (
                "Uses an LLM to extract structured bibliographic, classification, and "
                "thematic metadata from document text. Pass the extracted text as "
                "the 'input' invoke parameter."
            ),
            "transform": {"kind": "ingest", "subtype": "metadata-extract"},
            "run": {
                "type": "llm",
                "prompt": (
                    "You are a document metadata extraction assistant.\n\n"
                    "Extract structured metadata from the document text provided.\n\n"
                    "Return ONLY a valid JSON object with exactly these fields:\n"
                    "- published_by: the publishing organization or author name (string, or null)\n"
                    "- published_date: publication date in YYYY-MM-DD format (string, or null)\n"
                    "- document_type: one of Report, Correspondence, Forecast, Instructional, "
                    "Analysis, Transcript, Schedule, Other\n"
                    "- formality: one of High, Medium, Low\n"
                    "- sector: primary industry sector (string, e.g. 'Technology', 'Finance', "
                    "'Healthcare', 'Government', 'Energy', 'Other')\n"
                    "- issues: list of key topics or issues covered (array of short strings, "
                    "max 8 items)\n\n"
                    "Return only the JSON object — no markdown fences, no explanation."
                ),
                "temperature": 0.2,
                "max_output_tokens": 512,
                "input_mapping": {
                    "input": "$.input",
                },
            },
            "input": {
                "description": "Pass extracted document text as the 'input' invoke parameter.",
            },
            "output": {
                "description": (
                    "JSON: {published_by, published_date, document_type, formality, sector, issues}"
                ),
            },
        },
    },

    # ------------------------------------------------------------------
    # Step 4 — Apply Metadata to Source Artifact
    # ------------------------------------------------------------------
    {
        "slug": "ingest-apply-metadata",
        "context": {
            "content_type": "application/vnd.agience.transform+json",
            "title": "Apply Extracted Metadata",
            "description": (
                "Writes LLM-extracted metadata JSON into an artifact's context.metadata field. "
                "The metadata fields become searchable immediately via the search pipeline."
            ),
            "transform": {"kind": "ingest", "subtype": "metadata-apply"},
            "run": {
                "type": "mcp-tool",
                "server": "astra",
                "tool": "apply_metadata",
                "input_mapping": {
                    "workspace_id": "$.workspace_id",
                    "artifact_id": "$.artifacts[0]",
                    "metadata": "$.metadata_json",
                },
            },
            "input": {
                "description": (
                    "Requires 'metadata_json' param (JSON string) and one artifact reference "
                    "(the artifact to enrich)."
                ),
                "artifacts": {"min": 1, "max": 1},
            },
            "output": {
                "description": 'JSON: {"status": "ok", "artifact_id": str, "fields_applied": [...]}',
            },
        },
    },

    # ------------------------------------------------------------------
    # Orchestrator — Full Ingest Pipeline (single invocation)
    # Chains: dedup → pdf-extract → metadata-extract → metadata-apply
    # ------------------------------------------------------------------
    {
        "slug": "ingest-pipeline",
        "context": {
            "content_type": "application/vnd.agience.transform+json",
            "title": "Ingest Pipeline",
            "description": (
                "Run the full document ingestion pipeline on a PDF artifact in one step: "
                "deduplication → text extraction → LLM metadata extraction → apply metadata. "
                "Drop a PDF onto this transform to ingest it."
            ),
            "transform": {"kind": "ingest", "subtype": "pipeline"},
            "run": {
                "type": "mcp-tool",
                "server": "astra",
                "tool": "ingest_pipeline",
                "input_mapping": {
                    "workspace_id": "$.workspace_id",
                    "artifact_id": "$.artifacts[0]",
                },
            },
            "input": {
                "description": "A PDF artifact in the workspace to ingest.",
                "artifacts": {"min": 1, "max": 1},
            },
            "output": {
                "description": (
                    "JSON: {status, artifact_id, text_artifact_id, pages, content_hash, "
                    "metadata: {published_by, published_date, document_type, formality, "
                    "sector, issues}, steps: [...]}"
                ),
            },
            "drop": {
                "enabled": True,
                "label": "Drop PDF to ingest",
                "accepts": ["application/pdf"],
            },
        },
    },
]
