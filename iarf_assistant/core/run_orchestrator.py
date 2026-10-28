from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flask import current_app, has_app_context

from carlina.core.runtime_policy import (
    FULL_ACCESS,
    MAX_TOOL_CALL_ITERATIONS,
    clamp_tool_call_iterations,
    normalize_policy_profile,
)
from carlina.core.utc import utc_iso
from carlina.runtime_environment import set_product_environment

from ..extensions import db
from ..models import (
    ChatSession,
    LLMCost,
    RuntimeArtifact,
    RuntimeSourceRecord,
    WorkflowRun,
    WorkflowStep,
    WorkflowTemplate,
)
from .agent_events import append_agent_event_to_metadata_attr, make_agent_event
from .artifact_store import ArtifactStore
from .code_session_service import CodeSessionService
from .collection_service import RuntimeCollectionService
from .document_service import DocumentService
from .execution_kernel import AgentEventSink, ToolAuthorityContext, ToolExecutor
from .execution_scope import ExecutionScope
from .project_workspace_roots import (
    ProjectWorkspaceSelection,
    authorized_project_workspace_roots,
    public_workspace_reference_for_path,
    resolve_project_workspace_access,
    resolve_project_workspace_selection,
)
from .project_workspace_service import (
    PROJECT_WORKSPACE_PROFILE_ID,
    ProjectWorkspaceService,
)
from .runs.projections import (
    fallback_run_title,
    load_run_projection,
    load_run_projections,
    serialize_workflow_run,
)
from .runtime_state import (
    create_local_file_artifact,
    create_runtime_checkpoint,
    create_workflow_run,
    create_workflow_step,
    evaluate_runtime_checkpoints,
    guard_tool_execution,
    pause_workflow_run,
    read_text_artifact_content,
    resume_workflow_run,
    set_workflow_run_status,
    set_workflow_step_status,
    update_runtime_checkpoint,
    update_workflow_run_planning,
    visible_to_user,
)
from .source_ledger import SourceLedger
from .workflow_templates import (
    active_template_definition,
    compile_template_definition,
    executable_definition_from_addon_workflow,
    read_template_storage,
)

RUN_TOOL_NAMES = {
    "runs_list",
    "runs_get",
    "runs_observe",
    "runs_list_steps",
    "runs_list_artifacts",
    "runs_read_artifact",
    "runs_read_shared_memory",
    "runs_create_collection",
    "runs_get_collection",
    "runs_upsert_collection_items",
    "runs_partition_collection_items",
    "runs_claim_collection_items",
    "runs_annotate_collection_items",
    "runs_create",
    "runs_update_understanding",
    "runs_update_plan",
    "runs_start",
    "runs_pause",
    "runs_resume",
    "runs_stop",
    "runs_add_step",
    "runs_update_step",
    "runs_add_source",
    "runs_add_evidence",
    "runs_add_artifact",
    "runs_create_text_artifact",
    "runs_quick_text_artifact",
    "runs_compile",
    "documents_inspect",
    "documents_preview_pages",
    "documents_extract_pages",
    "documents_split_pages",
    "runs_code_session_create",
    "runs_code_session_get",
    "runs_code_session_execute",
    "runs_code_session_install_dependencies",
    "runs_project_workspace_create",
    "runs_project_workspace_get",
    "runs_project_workspace_inspect",
    "runs_project_workspace_edit",
    "runs_project_workspace_execute",
    "runs_project_workspace_process",
    "runs_project_workspace_close",
    "runs_execute_python",
    "runs_execute_tool_batch",
    "runs_add_artifact_version",
    "runs_update_artifact_review",
    "runs_create_checkpoint",
    "runs_update_checkpoint",
}


def _document_tool_declarations() -> list[dict[str, Any]]:
    file_ref = {
        "type": "string",
        "description": (
            "An owner-visible artifact id/reference, resource grant id/reference, "
            "or /uploads/... path from the current user."
        ),
    }
    pages = {
        "description": "One-based page selection such as '1-3, 7' or [1, 2, 7].",
        "oneOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "integer"}},
        ],
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "documents_inspect",
                "description": (
                    "Inspect an owner-visible document without loading its full content. "
                    "Returns metadata, size, page count, and supported operations."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_ref": file_ref,
                        "run_id": {"type": "string"},
                        "step_id": {"type": "string"},
                    },
                    "required": ["file_ref"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "documents_preview_pages",
                "description": (
                    "Render only selected PDF pages to image artifacts for visual inspection. "
                    "Defaults to page 1 and never injects the whole document into context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_ref": file_ref,
                        "pages": pages,
                        "dpi": {"type": "integer", "default": 120},
                        "run_id": {"type": "string"},
                        "step_id": {"type": "string"},
                    },
                    "required": ["file_ref"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "documents_extract_pages",
                "description": (
                    "Extract text from a document or selected PDF pages into a durable artifact. "
                    "Use engine='advanced' only for OCR, layout, or conversion through Document Processor."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_ref": file_ref,
                        "pages": pages,
                        "output_format": {
                            "type": "string",
                            "enum": ["markdown", "text", "json"],
                            "default": "markdown",
                        },
                        "engine": {
                            "type": "string",
                            "enum": ["direct", "advanced", "doc_processor", "ocr", "layout"],
                            "default": "direct",
                        },
                        "force_ocr": {"type": "boolean", "default": False},
                        "run_id": {"type": "string"},
                        "step_id": {"type": "string"},
                    },
                    "required": ["file_ref"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "documents_split_pages",
                "description": (
                    "Export selected PDF pages as one selection artifact or one artifact per page."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_ref": file_ref,
                        "pages": pages,
                        "mode": {
                            "type": "string",
                            "enum": ["selection", "individual"],
                            "default": "selection",
                        },
                        "run_id": {"type": "string"},
                        "step_id": {"type": "string"},
                    },
                    "required": ["file_ref", "pages"],
                },
            },
        },
    ]


def _code_session_tool_declarations() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "runs_code_session_create",
                "description": (
                    "Create a governed, resumable Python session using the application's locked Pixi "
                    "environment. Declared resources are copied into a read-only session area. "
                    "Requested extra dependencies are recorded but never installed automatically."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "description": "Optional existing run. A lightweight ad hoc run is created when omitted.",
                        },
                        "step_id": {"type": "string"},
                        "label": {"type": "string"},
                        "resources": {
                            "type": "array",
                            "maxItems": 50,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "file_ref": {"type": "string"},
                                    "reference": {"type": "string"},
                                    "data": {},
                                    "content": {"type": "string"},
                                    "label": {"type": "string"},
                                },
                            },
                        },
                        "dependencies": {
                            "type": "array",
                            "maxItems": 20,
                            "items": {"type": "string"},
                            "description": "Optional PEP 508 package requirements to plan for explicit approval-gated installation.",
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Run metadata used only when an ad hoc run is created.",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_code_session_get",
                "description": "Inspect a governed code session, its resources, environment, dependency state, and executions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "session_id": {"type": "string"},
                    },
                    "required": ["run_id", "session_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_code_session_execute",
                "description": (
                    "Execute Python in a governed resumable session. Import `runtime` from `iarf_runtime` "
                    "to read declared resources, write outputs, call selected host tools, map bounded agent "
                    "work, emit progress, create artifacts, and persist session state. Generated code has no "
                    "connector credentials, network access, subprocess access, or unrestricted host files."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "step_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "code": {"type": "string"},
                        "input": {},
                        "timeout_seconds": {"type": "integer", "default": 60},
                        "max_output_chars": {"type": "integer", "default": 50000},
                        "idempotency_key": {
                            "type": "string",
                            "description": "Stable key for deterministic retry/resume with identical code and input.",
                        },
                    },
                    "required": ["run_id", "session_id", "code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_code_session_install_dependencies",
                "description": (
                    "Install the session's explicitly requested ephemeral dependencies using Pixi. "
                    "This network/dependency-changing operation is approval-gated and fully recorded."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "timeout_seconds": {"type": "integer", "default": 600},
                        "_approval_id": {"type": "string"},
                    },
                    "required": ["run_id", "session_id"],
                },
            },
        },
    ]


def _project_workspace_tool_declarations() -> list[dict[str, Any]]:
    workspace_context = {
        "run_id": {"type": "string"},
        "step_id": {"type": "string"},
        "workspace_id": {"type": "string"},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "runs_project_workspace_create",
                "description": (
                    "Open an explicit, configured project root as an observable project.workspace "
                    "session. Access is approval-policy guarded; full access may suppress the prompt "
                    "but never the root boundary, event trail, credential filtering, or cleanup."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "description": "Optional existing run. A lightweight ad hoc run is created when omitted.",
                        },
                        "step_id": {"type": "string"},
                        "workspace_root_id": {
                            "type": "string",
                            "maxLength": 64,
                            "description": "Opaque ID from the authorized project.workspace runtime profile.",
                        },
                        "workspace_path": {
                            "type": "string",
                            "maxLength": 1_024,
                            "description": "Optional directory path relative to the selected root.",
                        },
                        "label": {"type": "string"},
                        "access": {
                            "type": "string",
                            "enum": ["read_only", "read_write"],
                            "description": "Requested access, bounded by the configured root access.",
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Run metadata used only when an ad hoc run is created.",
                        },
                        "_approval_id": {"type": "string"},
                    },
                    "required": ["workspace_root_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_project_workspace_get",
                "description": (
                    "Inspect a project workspace and refresh its recorded edit and command actions, "
                    "including background-process completion."
                ),
                "parameters": {
                    "type": "object",
                    "properties": workspace_context,
                    "required": ["run_id", "workspace_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_project_workspace_inspect",
                "description": (
                    "List, stat, or read a UTF-8 project file beneath the declared root. "
                    "Reads are bounded and return hashes for conflict-safe structured edits."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **workspace_context,
                        "operation": {
                            "type": "string",
                            "enum": ["list", "read", "stat"],
                        },
                        "path": {
                            "type": "string",
                            "description": "Path relative to the declared workspace root.",
                        },
                        "pattern": {"type": "string", "default": "*"},
                        "max_entries": {"type": "integer", "default": 200},
                        "max_depth": {"type": "integer", "default": 4},
                        "max_chars": {"type": "integer", "default": 100000},
                        "include_hidden": {"type": "boolean", "default": False},
                    },
                    "required": ["run_id", "workspace_id", "operation"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_project_workspace_edit",
                "description": (
                    "Apply an attributable structured text-file edit beneath a read-write project "
                    "root. Supply full content or exact replacements and preferably expected_sha256. "
                    "The resulting unified diff is retained as an artifact."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **workspace_context,
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "replacements": {
                            "type": "array",
                            "maxItems": 100,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old": {"type": "string"},
                                    "new": {"type": "string"},
                                    "expected_occurrences": {
                                        "type": "integer",
                                        "default": 1,
                                    },
                                },
                                "required": ["old", "new"],
                            },
                        },
                        "expected_sha256": {"type": "string"},
                        "create": {"type": "boolean", "default": False},
                        "_approval_id": {"type": "string"},
                    },
                    "required": ["run_id", "workspace_id", "path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_project_workspace_execute",
                "description": (
                    "Run an argv-based project command without a shell. Output is compacted in chat "
                    "and retained in full artifacts. Commands are timeout-supervised and may run in "
                    "the background. Network, dependency-changing, and destructive intent must be "
                    "declared explicitly and remains approval-policy guarded."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **workspace_context,
                        "argv": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 100,
                            "items": {"type": "string"},
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Directory relative to the workspace root.",
                        },
                        "environment": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Non-credential environment overrides only.",
                        },
                        "timeout_seconds": {"type": "integer", "default": 300},
                        "background": {"type": "boolean", "default": False},
                        "max_output_chars": {
                            "type": "integer",
                            "default": 20000,
                        },
                        "allow_network": {"type": "boolean", "default": False},
                        "allow_dependency_changes": {
                            "type": "boolean",
                            "default": False,
                        },
                        "allow_destructive": {
                            "type": "boolean",
                            "default": False,
                        },
                        "_approval_id": {"type": "string"},
                    },
                    "required": ["run_id", "workspace_id", "argv"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_project_workspace_process",
                "description": (
                    "List, poll, or stop a supervised project process. Stop is always available as "
                    "an in-scope cleanup action and does not require an additional approval."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **workspace_context,
                        "operation": {
                            "type": "string",
                            "enum": ["get", "list", "stop"],
                            "default": "get",
                        },
                        "process_id": {"type": "string"},
                    },
                    "required": ["run_id", "workspace_id", "operation"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_project_workspace_close",
                "description": (
                    "Close a project workspace and stop every still-running supervised command."
                ),
                "parameters": {
                    "type": "object",
                    "properties": workspace_context,
                    "required": ["run_id", "workspace_id"],
                },
            },
        },
    ]


def get_run_tool_declarations() -> list[dict[str, Any]]:
    """Always-available model tools for durable ad hoc/template run state."""
    return [
        {
            "type": "function",
            "function": {
                "name": "runs_list",
                "description": "List durable workflow runs visible to the current user. Defaults to the current chat session when available so the chief can find session-related runs and artifacts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "Optional run status filter."},
                        "kind": {"type": "string", "enum": ["ad_hoc", "template"], "description": "Optional run kind filter."},
                        "chat_session_id": {"type": "string", "description": "Optional chat session id to filter by."},
                        "scope": {"type": "string", "enum": ["current_session", "all_visible"], "default": "current_session"},
                        "limit": {"type": "integer", "default": 20},
                    },
                },
            },
        },
        *_document_tool_declarations(),
        *_code_session_tool_declarations(),
        *_project_workspace_tool_declarations(),
        {
            "type": "function",
            "function": {
                "name": "runs_get",
                "description": "Read the current status and metadata for a durable workflow run, optionally including steps, artifacts, sources, evidence, and run working memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "include_steps": {"type": "boolean", "default": True},
                        "include_artifacts": {"type": "boolean", "default": True},
                        "include_sources": {"type": "boolean", "default": False},
                        "include_evidence": {"type": "boolean", "default": False},
                        "include_working_memory": {"type": "boolean", "default": True},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_list_steps",
                "description": "List workflow steps for a durable run with human-readable display titles, statuses, outputs, and errors.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "status": {"type": "string", "description": "Optional step status filter."},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_list_artifacts",
                "description": "List artifacts for a durable run, including human-readable labels and download URLs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "review_status": {"type": "string", "description": "Optional artifact review status filter."},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_read_artifact",
                "description": "Read a user-visible artifact by id, or the latest artifact for a run. Text and markdown artifacts include preview content so the chief can show the report to the user.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {"type": "string"},
                        "run_id": {"type": "string", "description": "Used when artifact_id is omitted; returns the most recent artifact for this run."},
                        "max_chars": {"type": "integer", "default": 20000},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_read_shared_memory",
                "description": "Read the shared JSON blackboard for a durable workflow run. Omit key to read the whole memory object.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "key": {"type": "string", "description": "Optional memory key."},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_create_collection",
                "description": "Create or update a run-scoped collection ledger for multi-item work such as WordPress posts, emails, Drive files, local files, document chunks, or research sources.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "collection_id": {"type": "string", "description": "Optional stable id; generated if omitted."},
                        "title": {"type": "string"},
                        "objective": {"type": "string", "description": "What every item in this collection is being processed to accomplish."},
                        "kind": {"type": "string", "description": "Source/work type, e.g. wordpress_posts, emails, drive_files."},
                        "description": {"type": "string"},
                        "adapter_id": {
                            "type": "string",
                            "enum": ["generic", "wordpress.posts", "gdrive.files"],
                            "default": "generic",
                            "description": "Normalization adapter for provider payloads. The same row-backed ledger is used by every adapter.",
                        },
                        "identity_keys": {"type": "array", "items": {"type": "string"}},
                        "schema": {"type": "object", "description": "Optional expected annotation/item schema."},
                        "metadata": {"type": "object"},
                    },
                    "required": ["run_id", "title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_get_collection",
                "description": "Read a run-scoped collection ledger with optional item filtering and compact status counts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "collection_id": {"type": "string"},
                        "include_items": {"type": "boolean", "default": True},
                        "status": {"type": "string", "description": "Optional item status filter."},
                        "limit": {"type": "integer", "default": 100},
                    },
                    "required": ["run_id", "collection_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_upsert_collection_items",
                "description": "Add or update source/work items in a row-backed run collection. Stable identity uses explicit identity, provider/external id, canonical URI/path, then content hash; uniqueness is enforced transactionally.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "collection_id": {"type": "string"},
                        "adapter_id": {
                            "type": "string",
                            "enum": ["generic", "wordpress.posts", "gdrive.files"],
                            "description": "Optional adapter override for this ingestion batch.",
                        },
                        "items": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["run_id", "collection_id", "items"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_partition_collection_items",
                "description": "Plan deterministic worker-sized partitions from collection items by status, item count, and estimated input-token budget. This records batch plans but does not claim or execute items.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "collection_id": {"type": "string"},
                        "partition_id": {"type": "string", "description": "Optional stable partition-plan id; generated if omitted."},
                        "status": {"type": "string", "description": "Single item status filter; ignored when statuses is provided."},
                        "statuses": {"type": "array", "items": {"type": "string"}, "description": "Eligible item statuses; defaults to pending. Legacy queued is accepted as an alias."},
                        "max_items_per_partition": {"type": "integer", "default": 10},
                        "max_input_tokens": {"type": "integer", "default": 30000},
                        "reserved_output_tokens": {"type": "integer", "default": 4000},
                        "chars_per_token": {"type": "number", "default": 4},
                        "include_annotations": {"type": "boolean", "default": False},
                        "metadata": {"type": "object"},
                    },
                    "required": ["run_id", "collection_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_claim_collection_items",
                "description": "Atomically lease pending, retryable, or expired collection items. Return the lease_token with every annotation so stale workers cannot commit after reassignment.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "collection_id": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                        "claimant": {"type": "string", "description": "Worker/model/code session id."},
                        "statuses": {"type": "array", "items": {"type": "string"}, "description": "Statuses eligible for claim; defaults to pending. Failed may be requested for a bounded retry."},
                        "lease_seconds": {"type": "integer", "default": 900},
                    },
                    "required": ["run_id", "collection_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_annotate_collection_items",
                "description": "Commit item-level outcomes with source provenance. A current lease token is required by default; expired or reassigned worker results are rejected without mutation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "collection_id": {"type": "string"},
                        "lease_token": {"type": "string", "description": "Batch lease token returned by runs_claim_collection_items."},
                        "require_lease": {"type": "boolean", "default": True, "description": "Keep true for model/worker calls. Internal orchestrator synchronization may bypass a separate public lease."},
                        "annotations": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["run_id", "collection_id", "annotations"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_create",
                "description": "Create a durable Carlina run for ad hoc or template-backed work that needs planning, provenance, review, or resumability.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["ad_hoc", "template"], "default": "ad_hoc"},
                        "workflow_id": {"type": "string", "description": "Required only for template-backed workflow runs."},
                        "template_id": {"type": "string", "description": "Optional user workflow template id for template-backed runs."},
                        "title": {"type": "string", "description": "Human-readable run name shown in workflow dashboards."},
                        "input": {"type": "object", "description": "Original user request and relevant initial inputs."},
                        "understanding": {"type": "object", "description": "Initial task understanding, if already known."},
                        "plan": {"type": "object", "description": "Initial execution plan, if already known."},
                        "status": {"type": "string", "default": "draft"},
                        "start": {"type": "boolean", "default": False},
                        "runtime_profile_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional runtime profiles intended for this run, e.g. python.scratch or python.session. "
                                "In chat scoped mode, the active turn scope remains authoritative."
                            ),
                        },
                        "model_profile_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional model profile ids intended for this run.",
                        },
                        "metadata": {"type": "object"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_update_understanding",
                "description": "Persist or merge the understanding record for a durable run: selected capabilities, retrieved guidance, quality bar, missing context, risks, and approval points.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "understanding": {"type": "object"},
                        "status": {"type": "string"},
                        "replace": {"type": "boolean", "default": False},
                    },
                    "required": ["run_id", "understanding"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_update_plan",
                "description": "Persist or merge the execution plan for a durable run, including steps, capability/tool choices, approval points, and review gates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "plan": {"type": "object"},
                        "title": {"type": "string", "description": "Optional human-readable run name update."},
                        "status": {"type": "string"},
                        "current_step": {"type": "string"},
                        "replace": {"type": "boolean", "default": False},
                    },
                    "required": ["run_id", "plan"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_create_checkpoint",
                "description": "Schedule a model-visible checkpoint or timer for a durable run. Use this for quality checks, stall monitors, progress reviews, and pause-before-side-effect gates during long or risky work.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "title": {"type": "string", "description": "Short label shown in runtime UI."},
                        "kind": {
                            "type": "string",
                            "enum": ["quality_check", "stall_timer", "progress_review", "before_side_effect", "approval_gate", "custom"],
                            "default": "custom",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["create_review_step", "pause_run", "record_due"],
                            "default": "create_review_step",
                            "description": "What the runtime should do when the checkpoint becomes due. Use create_review_step for self-checks, pause_run for gates, and record_due for passive visibility only.",
                        },
                        "reason": {"type": "string", "description": "Why this checkpoint exists."},
                        "instruction": {"type": "string", "description": "What the orchestrator should inspect or do when this checkpoint is due."},
                        "due_in_seconds": {"type": "integer", "description": "Set a timer relative to now."},
                        "due_at": {"type": "string", "description": "ISO timestamp when the checkpoint should become due."},
                        "stall_after_seconds": {
                            "type": "integer",
                            "description": "For stall_timer only: wake after this many seconds without step activity.",
                        },
                        "after_completed_steps": {"type": "integer", "description": "Make the checkpoint due after this many additional workflow steps complete."},
                        "target_completed_steps": {"type": "integer", "description": "Make the checkpoint due when total completed workflow steps reaches this count."},
                        "condition": {"type": "object", "description": "Advanced condition payload, merged with explicit timer/progress fields."},
                        "metadata": {"type": "object"},
                    },
                    "required": ["run_id", "reason"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_update_checkpoint",
                "description": "Resolve, cancel, or reopen a previously scheduled run checkpoint after the orchestrator has inspected it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "checkpoint_id": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "resolved", "cancelled"], "default": "resolved"},
                        "note": {"type": "string", "description": "Brief resolution note, for example what was checked."},
                        "metadata": {"type": "object"},
                    },
                    "required": ["run_id", "checkpoint_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_start",
                "description": "Start a planned durable run. This sets the run status to running and asks the workflow orchestrator to dispatch all runnable planned steps.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_pause",
                "description": "Pause a running durable run. This prevents new step dispatches and marks not-yet-executing steps as paused.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_resume",
                "description": "Resume a paused durable run by reopening paused steps and asking the workflow orchestrator to dispatch runnable work.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_stop",
                "description": "Alias for runs_pause. Stops new dispatches while preserving progress so the run can be resumed later.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_add_step",
                "description": "Add a durable step to a Carlina run before or during execution. Steps can depend on multiple completed parent steps and can enforce JSON input/output schemas.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "step_type": {"type": "string"},
                        "title": {"type": "string"},
                        "step_key": {"type": "string"},
                        "status": {"type": "string", "default": "planned"},
                        "sequence": {"type": "integer"},
                        "parent_step_id": {"type": "string", "description": "Legacy single dependency. Prefer depends_on_step_ids for new plans."},
                        "depends_on_step_ids": {"type": "array", "items": {"type": "string"}, "description": "Step IDs that must all complete before this step can run."},
                        "assigned_model_role": {"type": "string", "description": "A model tier (fast, medium, powerful), a direct model id, or an exact AgentProfile id."},
                        "instructions": {"type": "string", "description": "Step-specific natural-language instructions for the worker agent."},
                        "capability_ids": {"type": "array", "items": {"type": "string"}},
                        "skill_ids": {"type": "array", "items": {"type": "string"}},
                        "tool_ids": {"type": "array", "items": {"type": "string"}},
                        "tool_view_ids": {"type": "array", "items": {"type": "string"}, "description": "Selected narrowed tool-view IDs that constrain tool parameters for this step."},
                        "provider_requirements": {"type": "array", "items": {"type": "string"}},
                        "input_schema": {"type": "object", "description": "JSON Schema describing the input payload expected by this step."},
                        "output_schema": {"type": "object", "description": "JSON Schema that the worker agent must return as its final output."},
                        "completion_requirements": {
                            "type": "object",
                            "description": "Optional step postconditions enforced by the worker, for example shared memory keys and artifact filenames.",
                            "properties": {
                                "shared_memory_keys": {"type": "array", "items": {"type": "string"}},
                                "artifact_filenames": {"type": "array", "items": {"type": "string"}},
                                "requires_artifact": {"type": "boolean"},
                            },
                        },
                        "retry_policy": {
                            "type": "object",
                            "description": "Optional retry policy for transient step failures.",
                            "properties": {
                                "enabled": {"type": "boolean"},
                                "max_attempts": {"type": "integer", "minimum": 1},
                                "backoff_seconds": {"type": "number", "minimum": 0},
                                "initial_backoff_seconds": {"type": "number", "minimum": 0},
                                "backoff_multiplier": {"type": "number", "minimum": 1},
                                "max_backoff_seconds": {"type": "number", "minimum": 0},
                                "retry_on": {"type": "string", "enum": ["transient", "all", "none"]},
                            },
                        },
                        "input": {"type": "object"},
                        "metadata": {"type": "object"},
                        "make_current": {"type": "boolean", "default": False},
                    },
                    "required": ["run_id", "step_type"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_update_step",
                "description": "Update a durable run step status and execution output/error.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "step_id": {"type": "string"},
                        "status": {"type": "string"},
                        "output": {"type": "object"},
                        "error": {"type": "string"},
                        "make_current": {"type": "boolean", "default": False},
                    },
                    "required": ["step_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_add_source",
                "description": "Record a source used by a durable run, such as an email, URL, document, file, collection item, or remote object.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "source_type": {"type": "string"},
                        "uri": {"type": "string"},
                        "step_id": {"type": "string"},
                        "provider": {"type": "string"},
                        "external_id": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "content_hash": {"type": "string"},
                        "access_scope": {"type": "string", "default": "private"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["run_id", "source_type", "uri"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_add_artifact",
                "description": "Create a durable artifact record for a run and its initial version, such as a draft document, export, file, or remote object.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "artifact_type": {"type": "string", "default": "file"},
                        "step_id": {"type": "string"},
                        "job_id": {"type": "string"},
                        "source_addon_id": {"type": "string"},
                        "uri": {"type": "string"},
                        "path": {"type": "string"},
                        "remote_reference": {"type": "string"},
                        "mime_type": {"type": "string"},
                        "content_hash": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "label": {"type": "string"},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                        "change_summary": {"type": "string"},
                        "review_status": {"type": "string", "default": "draft"},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_create_text_artifact",
                "description": "Create a user-visible text or markdown file artifact for a workflow run from inline content. Use this when the chief or an author step needs to pass a report, draft, or note back to the user.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "step_id": {"type": "string"},
                        "content": {"type": "string", "description": "Full UTF-8 text content to write into the artifact file."},
                        "filename": {"type": "string", "description": "Optional filename, for example report.md."},
                        "label": {"type": "string", "description": "Human-readable artifact label shown in the runtime UI."},
                        "mime_type": {"type": "string", "default": "text/markdown"},
                        "metadata": {"type": "object"},
                        "change_summary": {"type": "string"},
                        "review_status": {"type": "string", "default": "draft"},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_observe",
                "description": (
                    "Return a compact chat-friendly observation of one durable run: bounded progress, "
                    "cost/token totals, failures, sampled item results, due checkpoints, and artifacts. "
                    "Prefer this over loading every step when monitoring a broad delegated run."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "sample_limit": {
                            "type": "integer",
                            "default": 3,
                            "description": "Maximum completed and failed step samples to return.",
                        },
                        "include_artifacts": {"type": "boolean", "default": True},
                    },
                    "required": ["run_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_quick_text_artifact",
                "description": "Create a one-shot text artifact with minimal overhead. If run_id is omitted, this creates an ad hoc run, writes the artifact, and auto-completes the run when no executable steps are pending.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string", "description": "Optional existing run id. Omit to auto-create a simple ad hoc run."},
                        "title": {"type": "string", "description": "Run title when auto-creating a run."},
                        "input": {"type": "object", "description": "Optional run input payload when auto-creating a run."},
                        "content": {"type": "string", "description": "Full UTF-8 text content to write into the artifact file."},
                        "filename": {"type": "string", "description": "Optional filename, for example report.md."},
                        "label": {"type": "string", "description": "Human-readable artifact label shown in the runtime UI."},
                        "metadata": {"type": "object"},
                        "change_summary": {"type": "string"},
                        "review_status": {"type": "string", "default": "draft"},
                        "auto_complete_run": {"type": "boolean", "default": True},
                    },
                    "required": ["content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_compile",
                "description": (
                    "Compile a durable run in one call and optionally start it. Provide the objective, "
                    "resource/capability scope, worker model and concurrency preferences, validation/retry "
                    "policy, and preview mode. For bounded corpus work, provide delegation.items or resources; "
                    "the runtime fingerprints, deduplicates, context-splits, and materializes resumable map "
                    "steps without requiring the chief to read the corpus."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "objective": {
                            "type": "string",
                            "description": "Primary objective. This is the preferred alias for goal.",
                        },
                        "goal": {"type": "string", "description": "Concise user-visible goal for the run."},
                        "user_request_summary": {"type": "string"},
                        "resources": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": (
                                "Resource descriptors or already-enumerated source items. A resource may "
                                "contain an items array; workers receive stable refs/content without the "
                                "chief reading it first."
                            ),
                        },
                        "capability_scope": {
                            "type": "object",
                            "description": (
                                "Requested Autonomous or Scoped execution scope. The current chat turn scope "
                                "remains authoritative and is bound by the runtime."
                            ),
                        },
                        "execution_strategy": {
                            "type": "string",
                            "enum": ["durable_run", "bounded_map", "exploratory_workers"],
                            "description": "High-level execution strategy; preferred alias for strategy.",
                        },
                        "strategy": {
                            "type": "string",
                            "enum": [
                                "inline_not_needed",
                                "durable_run",
                                "delegate",
                                "collection_processing",
                                "code_in_loop",
                                "code_mediated",
                            ],
                            "default": "durable_run",
                        },
                        "understanding": {"type": "object"},
                        "plan": {"type": "object", "description": "Compiled plan, including optional steps/checkpoints/parallel groups."},
                        "steps": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Optional compiled step list. Each item accepts the runs_add_step fields except run_id.",
                        },
                        "sources": {"type": "array", "items": {"type": "object"}},
                        "tools_or_capabilities": {"type": "array", "items": {"type": "string"}},
                        "model_preferences": {
                            "type": "object",
                            "properties": {
                                "worker_model": {
                                    "type": "string",
                                    "description": "Model tier, direct model id, or exact AgentProfile id.",
                                },
                                "reducer_model": {"type": "string"},
                                "model_profile_ids": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                        "concurrency": {
                            "type": "object",
                            "properties": {
                                "max_workers": {"type": "integer", "minimum": 1, "maximum": 32},
                                "parallel_tool_calls": {"type": "integer", "minimum": 1, "maximum": 10},
                            },
                        },
                        "validation": {
                            "type": "object",
                            "description": "Run/map validation rules, including an optional output_schema.",
                        },
                        "retry_policy": {
                            "type": "object",
                            "description": "Default targeted retry policy inherited by compiled map workers.",
                        },
                        "delegation": {
                            "type": "object",
                            "description": (
                                "Optional bounded map contract. Supports objective, items, exploratory_queries, "
                                "model/model_role, capability_ids, tool_ids, tool_view_ids, resource_reference_ids, "
                                "expected_schema/output_schema, max_input_tokens, reserved_output_tokens, "
                                "max_tool_calls, max_workers, retry_policy, continue_on_item_failure, and reduction."
                            ),
                            "properties": {
                                "objective": {"type": "string"},
                                "items": {"type": "array", "items": {"type": "object"}},
                                "exploratory_queries": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "model": {
                                    "type": "string",
                                    "description": "Direct worker model id, model tier, or exact AgentProfile id.",
                                },
                                "model_role": {"type": "string"},
                                "capability_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "tool_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "tool_view_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "resource_reference_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "expected_schema": {"type": "object"},
                                "output_schema": {"type": "object"},
                                "max_input_tokens": {
                                    "type": "integer",
                                    "minimum": 1000,
                                },
                                "reserved_output_tokens": {
                                    "type": "integer",
                                    "minimum": 100,
                                },
                                "max_items_per_worker": {
                                    "type": "integer",
                                    "minimum": 1,
                                },
                                "max_tool_calls": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": MAX_TOOL_CALL_ITERATIONS,
                                },
                                "max_workers": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 32,
                                },
                                "parallel_tool_calls": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 10,
                                },
                                "retry_policy": {"type": "object"},
                                "continue_on_item_failure": {"type": "boolean"},
                                "reduction": {
                                    "type": "object",
                                    "description": "Optional staged reducer after all map workers.",
                                    "properties": {
                                        "mode": {
                                            "type": "string",
                                            "enum": ["none", "worker", "artifact", "code"],
                                        },
                                        "model": {"type": "string"},
                                        "instructions": {"type": "string"},
                                        "output_schema": {"type": "object"},
                                        "max_tool_calls": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": MAX_TOOL_CALL_ITERATIONS,
                                        },
                                    },
                                    "required": ["mode"],
                                },
                            },
                        },
                        "policy": {"type": "object"},
                        "checkpoints": {"type": "array", "items": {"type": "object"}},
                        "metadata": {"type": "object"},
                        "compile_mode": {
                            "type": "string",
                            "enum": ["compile_only", "start"],
                            "description": "Preferred compile/start switch. Legacy start_preference is also accepted.",
                        },
                        "preview_mode": {
                            "type": "string",
                            "enum": ["auto", "always", "never"],
                            "description": "Whether to require a user-visible preview before start.",
                        },
                        "start_preference": {
                            "type": "string",
                            "enum": ["draft_only", "preview_then_start", "start_if_simple", "start_now"],
                            "default": "start_if_simple",
                        },
                        "preview_preference": {
                            "type": "string",
                            "enum": ["auto", "always", "never", "never_if_simple"],
                            "default": "auto",
                        },
                    },
                    "required": ["title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_execute_python",
                "description": (
                    "Execute a bounded scratch Python script for deterministic transformation, validation, "
                    "dedupe, batching, or reduction inside a durable run. The script receives JSON on stdin "
                    "with input, run id, and optionally working memory. It runs with a sanitized environment "
                    "and no connector credentials or toolkit SDK. This is not an OS sandbox and should not be "
                    "used for arbitrary external side effects."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "label": {
                            "type": "string",
                            "description": "Optional label when a lightweight ad hoc run is created automatically.",
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Optional run metadata used only when run_id is omitted.",
                        },
                        "code": {"type": "string", "description": "Python source code to execute."},
                        "input": {"description": "JSON-serializable payload passed to the script on stdin."},
                        "include_working_memory": {
                            "type": "boolean",
                            "default": True,
                            "description": "Include the run shared working memory in the stdin envelope.",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "default": 10,
                            "description": "Execution timeout, clamped by the runtime.",
                        },
                        "max_output_chars": {
                            "type": "integer",
                            "default": 20000,
                            "description": "Maximum stdout/stderr characters returned and stored.",
                        },
                        "parse_stdout_json": {
                            "type": "boolean",
                            "default": False,
                            "description": "Parse stdout as JSON and include it as parsed_stdout.",
                        },
                        "step_id": {
                            "type": "string",
                            "description": "Optional workflow step id to attach result artifacts/provenance to.",
                        },
                        "working_memory_key": {
                            "type": "string",
                            "description": "Optional run working-memory key where the execution result envelope is stored.",
                        },
                        "save_artifact": {
                            "type": "boolean",
                            "default": False,
                            "description": "Persist the execution result JSON as a run artifact.",
                        },
                        "artifact_filename": {
                            "type": "string",
                            "description": "Optional filename for the result artifact when save_artifact is true.",
                        },
                    },
                    "required": ["code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_execute_tool_batch",
                "description": (
                    "Execute a bounded batch of governed toolkit calls for a durable run. Prefer tool_view_id for "
                    "code-callable/scriptable views. In full_access runs, code-produced calls may target selected "
                    "registered tools by tool_id/function_name without exposing connector credentials to Python. "
                    "Each call is schema validated, approval/resource guarded, and executed by the orchestrator."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "step_id": {"type": "string", "description": "Optional workflow step id for provenance."},
                        "calls": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "call_id": {"type": "string", "description": "Optional stable caller-supplied id."},
                                    "tool_view_id": {"type": "string", "description": "Preferred code-callable tool view id."},
                                    "tool_id": {
                                        "type": "string",
                                        "description": "Registered tool id for full_access mediated tool calls when no tool_view_id is provided.",
                                    },
                                    "function_name": {
                                        "type": "string",
                                        "description": "Optional expected function name. With tool_view_id it must match the view's underlying tool; without tool_view_id it may identify a registered tool in full_access.",
                                    },
                                    "arguments": {"type": "object", "description": "Arguments narrowed to the selected tool view."},
                                },
                                "required": ["arguments"],
                            },
                        },
                        "continue_on_error": {"type": "boolean", "default": True},
                        "max_calls": {"type": "integer", "default": 20},
                        "max_result_chars": {"type": "integer", "default": 8000},
                        "working_memory_key": {
                            "type": "string",
                            "description": "Optional run working-memory key where the batch result envelope is stored.",
                        },
                    },
                    "required": ["run_id", "calls"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_add_artifact_version",
                "description": "Add a new version to an existing run artifact and optionally make it the current version.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {"type": "string"},
                        "step_id": {"type": "string"},
                        "job_id": {"type": "string"},
                        "source_addon_id": {"type": "string"},
                        "uri": {"type": "string"},
                        "path": {"type": "string"},
                        "remote_reference": {"type": "string"},
                        "mime_type": {"type": "string"},
                        "content_hash": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "label": {"type": "string"},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                        "change_summary": {"type": "string"},
                        "make_current": {"type": "boolean", "default": True},
                        "reset_review_status": {"type": "boolean", "default": True},
                    },
                    "required": ["artifact_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_update_artifact_review",
                "description": "Update an artifact's review status after human/model review of the current version.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {"type": "string"},
                        "review_status": {"type": "string", "enum": ["draft", "pending_review", "approved", "rejected", "changes_requested", "archived"]},
                        "review_note": {"type": "string"},
                    },
                    "required": ["artifact_id", "review_status"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "runs_add_evidence",
                "description": "Record evidence derived from a run source, including claims, quotes, citations, scores, and artifact usage.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "source_id": {"type": "string"},
                        "step_id": {"type": "string"},
                        "artifact_id": {"type": "string"},
                        "claim": {"type": "string"},
                        "quote": {"type": "string"},
                        "citation": {"type": "object"},
                        "relevance_score": {"type": "number"},
                        "novelty_score": {"type": "number"},
                        "used_in_artifact": {"type": "boolean", "default": False},
                        "metadata": {"type": "object"},
                    },
                    "required": ["run_id", "source_id"],
                },
            },
        },
    ]


def format_run_protocol_for_prompt() -> str:
    return (
        "Run protocol: Simple chat and direct selected-capability chat do not require durable runs. "
        "For complex, multi-step tasks, prefer the one-call compiler when the task shape is understood; "
        "use lower-level construction only when the plan genuinely needs incremental repair or elaboration:\n"
        "1. Discover capabilities/guidance using the available capabilities tools.\n"
        "2. If you already have enough understanding, call `runs_compile` once with title, objective, resources/capability scope, model/concurrency preferences, validation/retry policy, preview_mode, and compile_mode. For bounded corpus work, put source descriptors in resources or delegation.items; the runtime fingerprints, deduplicates, context-splits, and materializes resumable map/reducer steps without the chief reading the corpus. It returns draft, preview_required, or started. In explicit compiled steps, use generic `step_type` labels such as search, read, extract, classify, synthesize, or publish; do not invent pseudo function names like `source_search` or `drive_read`. Exact callable names must come from grounded tool ids/function names and belong in `tool_ids`, tool-batch calls, or instructions with provenance.\n"
        "3. Low-level construction tools are intentionally hidden in ordinary turns. Use `runs_create`, `runs_update_understanding`, `runs_update_plan`, and `runs_add_step` only when they are exposed for incremental inspection, editing, or repair. Include a concise human-readable `title` so dashboards do not show only a UUID.\n"
        "4. When adding steps manually, use `depends_on_step_ids` for DAG fan-in/fan-out; it accepts prior step IDs or prior `step_key` values. Use `skill_ids` for reusable procedures, and `output_schema` when downstream steps require exact JSON.\n"
        "5. For long, costly, parallel, or side-effecting work, use `runs_create_checkpoint` for progress counts, stall timers, quality checks, or pause-before-side-effect gates. Inspect due checkpoints with `runs_get` and close them with `runs_update_checkpoint` after review.\n"
        "6. Start the run when the planned steps are ready. Do not mark steps `running` manually; the orchestrator dispatches planned steps.\n"
        "7. Use `runs_pause`/`runs_stop` to freeze dispatch and `runs_resume` to continue from paused state when you need deterministic operator control.\n"
        "8. After a broad run starts, prefer `runs_observe` for compact progress, cost, failures, samples, checkpoints, and artifacts. Use `runs_get`, `runs_list_steps`, `runs_list_artifacts`, `runs_read_shared_memory`, and `runs_read_artifact` only when deeper inspection is needed.\n"
        "9. Record any files or links using `runs_add_source`, `runs_add_evidence`, or `runs_add_artifact` as appropriate during your steps. Use `runs_create_text_artifact` when you need to hand a markdown/text report or draft directly to the user as a durable downloadable artifact.\n"
        "10. For one-shot markdown/text deliverables where full planning is unnecessary, prefer `runs_quick_text_artifact`.\n"
        "11. For deterministic JSON/text transforms, dedupe, batching, validation, or reduction inside a run, use `runs_execute_python`. It is a bounded scratch Python runtime, not a connector SDK and not an OS sandbox; scripts cannot call toolkit tools directly.\n"
        "12. If code or planning produces a normalized list of connector/tool calls, use `runs_execute_tool_batch`. Prefer code-callable tool views; in full_access runs it may target selected registered tools directly. The orchestrator validates and guards each call; Python still never receives connector credentials.\n"
        "13. Use `documents_inspect`, `documents_preview_pages`, `documents_extract_pages`, and `documents_split_pages` for selected-page document work. Prefer direct extraction; use the advanced Document Processor provider only for OCR, layout, or conversion.\n"
        "14. Use `runs_code_session_create` plus `runs_code_session_execute` when deterministic work needs maintained data/document dependencies, resumable state, artifacts, selected host tools, or bounded agent mapping. Import `runtime` from `iarf_runtime`; never attempt direct credentials, network, subprocesses, or host paths. Extra dependencies require an explicit approval-gated install call.\n"
        "15. For every-item workflows, create a row-backed run collection with `runs_create_collection`, ingest provider payloads through the matching adapter with `runs_upsert_collection_items`, plan worker batches with `runs_partition_collection_items`, atomically lease work with `runs_claim_collection_items`, and pass its current `lease_token` to `runs_annotate_collection_items`. Stale or expired worker results are rejected.\n"
        "16. For software-project work, open an explicit root with `runs_project_workspace_create`, inspect before editing, use hash-checked `runs_project_workspace_edit`, and run argv-based tests/builds with `runs_project_workspace_execute`. Poll or stop background commands with `runs_project_workspace_process` and close the workspace when finished. Network, dependency-changing, and destructive intent must be declared; full access may suppress prompts but never root, credential, event, output, or cleanup controls.\n"
        "`assigned_model_role` accepts model tiers (`fast`, `medium`, `powerful`), direct model ids, or exact AgentProfile ids discovered through capabilities.\n"
        "IMPORTANT: Do not call the same runs_* tool consecutively without progress. Do not get stuck in a planning loop. Make actual tool calls between step updates."
    )


class RunOrchestrator:
    """Small service layer for model-facing durable run operations."""

    _TOOL_HANDLERS = {
        "runs_list": "list_runs",
        "runs_get": "get_run",
        "runs_observe": "observe_run",
        "runs_list_steps": "list_steps",
        "runs_list_artifacts": "list_artifacts",
        "runs_read_artifact": "read_artifact",
        "runs_read_shared_memory": "read_working_memory",
        "runs_create_collection": "create_collection",
        "runs_get_collection": "get_collection",
        "runs_upsert_collection_items": "upsert_collection_items",
        "runs_partition_collection_items": "partition_collection_items",
        "runs_claim_collection_items": "claim_collection_items",
        "runs_annotate_collection_items": "annotate_collection_items",
        "runs_create": "create_run",
        "runs_update_understanding": "update_understanding",
        "runs_update_plan": "update_plan",
        "runs_create_checkpoint": "set_checkpoint",
        "runs_set_checkpoint": "set_checkpoint",
        "runs_update_checkpoint": "update_checkpoint",
        "runs_start": "start_run",
        "runs_pause": "pause_run",
        "runs_stop": "pause_run",
        "runs_resume": "resume_run",
        "runs_add_step": "add_step",
        "runs_update_step": "update_step",
        "runs_add_source": "add_source",
        "runs_add_evidence": "add_evidence",
        "runs_add_artifact": "add_artifact",
        "runs_create_text_artifact": "create_text_artifact",
        "runs_quick_text_artifact": "quick_create_text_artifact",
        "runs_compile": "compile_run",
        "documents_inspect": "inspect_document",
        "documents_preview_pages": "preview_document_pages",
        "documents_extract_pages": "extract_document_pages",
        "documents_split_pages": "split_document_pages",
        "runs_code_session_create": "create_code_session",
        "runs_code_session_get": "get_code_session",
        "runs_code_session_execute": "execute_code_session",
        "runs_code_session_install_dependencies": "install_code_session_dependencies",
        "runs_project_workspace_create": "create_project_workspace",
        "runs_project_workspace_get": "get_project_workspace",
        "runs_project_workspace_inspect": "inspect_project_workspace",
        "runs_project_workspace_edit": "edit_project_workspace",
        "runs_project_workspace_execute": "execute_project_workspace",
        "runs_project_workspace_process": "control_project_workspace_process",
        "runs_project_workspace_close": "close_project_workspace",
        "runs_execute_python": "execute_python",
        "runs_execute_tool_batch": "execute_tool_batch",
        "runs_add_artifact_version": "add_artifact_version",
        "runs_update_artifact_review": "update_artifact_review",
    }

    def __init__(
        self,
        *,
        user_id: int | None,
        is_admin: bool = False,
        addon_manager: Any = None,
        tool_config: dict[str, Any] | None = None,
        chat_session_id: str | None = None,
        enforced_run_id: str | None = None,
        enforced_step_id: str | None = None,
    ):
        self.user_id = int(user_id) if user_id is not None else None
        self.is_admin = bool(is_admin)
        self.addon_manager = addon_manager
        self.tool_config = tool_config if isinstance(tool_config, dict) else {}
        self.chat_session_id = str(chat_session_id) if chat_session_id else None
        self.enforced_run_id = str(enforced_run_id) if enforced_run_id else None
        self.enforced_step_id = str(enforced_step_id) if enforced_step_id else None

    def execute_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        if self.enforced_run_id and name in {"runs_create", "runs_compile"}:
            raise PermissionError(f"{name} is not allowed inside a workflow-step context.")
        handler_name = self._TOOL_HANDLERS.get(str(name))
        if not handler_name:
            raise KeyError(f"Unknown run tool '{name}'.")
        result = getattr(self, handler_name)(arguments)
        return self._sanitize_project_workspace_payload(result)

    def _sanitize_project_workspace_payload(self, value: Any) -> Any:
        """Remove configured host paths from every public run-tool projection."""
        if isinstance(value, list):
            return [self._sanitize_project_workspace_payload(item) for item in value]
        if isinstance(value, tuple):
            return [self._sanitize_project_workspace_payload(item) for item in value]
        if not isinstance(value, dict):
            return value

        configured_roots = (
            current_app.config.get("PROJECT_WORKSPACE_ROOTS", [])
            if has_app_context()
            else []
        )
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key != "workspace_root":
                sanitized[key] = self._sanitize_project_workspace_payload(item)
                continue
            reference = public_workspace_reference_for_path(
                configured_roots,
                item,
                user_id=self.user_id,
                is_admin=self.is_admin,
            )
            if reference is None:
                sanitized["workspace_root_redacted"] = True
                continue
            sanitized["workspace_root_id"] = reference["id"]
            sanitized["workspace_root_label"] = reference["label"]
            sanitized["workspace_root_access"] = reference["access"]
            sanitized["workspace_path"] = reference["relative_path"]
        return sanitized

    def list_runs(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = WorkflowRun.query
        if self.user_id is not None and not self.is_admin:
            query = query.filter(WorkflowRun.created_by_user_id == self.user_id)
        if self.enforced_run_id:
            query = query.filter(WorkflowRun.id == self.enforced_run_id)

        scope = payload.get("scope") or "current_session"
        chat_session_id = payload.get("chat_session_id")
        if chat_session_id:
            chat_session_id = self._validated_chat_session_id(chat_session_id)
        elif scope != "all_visible" and self.chat_session_id:
            chat_session_id = self._validated_chat_session_id(self.chat_session_id)
        if chat_session_id:
            query = query.filter(WorkflowRun.chat_session_id == chat_session_id)

        status = payload.get("status")
        if status:
            query = query.filter(WorkflowRun.status == str(status).lower())
        kind = payload.get("kind")
        if kind:
            query = query.filter(WorkflowRun.kind == str(kind).lower())

        limit = max(1, min(int(payload.get("limit") or 20), 100))
        rows = query.order_by(WorkflowRun.updated_at.desc()).limit(limit).all()
        checkpoint_runs = [
            row
            for row in rows
            if isinstance(row.metadata_json, dict)
            and isinstance(row.metadata_json.get("runtime_checkpoints"), list)
            and row.metadata_json["runtime_checkpoints"]
        ]
        if checkpoint_runs:
            evaluate_runtime_checkpoints(workflow_runs=checkpoint_runs)
        projection = load_run_projections(rows)
        return {
            "runs": [serialize_workflow_run(row, projection) for row in rows],
            "count": len(rows),
        }

    def get_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        evaluate_runtime_checkpoints(run.id)
        run = db.session.get(WorkflowRun, run.id)
        projection = load_run_projection(run)
        steps = list(projection.steps_for(run.id))
        result: dict[str, Any] = {"run": serialize_workflow_run(run, projection)}
        if payload.get("include_steps", True):
            result["steps"] = [step.to_dict() for step in steps]
        if payload.get("include_artifacts", True):
            result["artifacts"] = [artifact.to_dict() for artifact in self._artifacts_for_run(run.id)]
        if payload.get("include_sources"):
            result["sources"] = [source.to_dict() for source in RuntimeSourceRecord.query.filter_by(workflow_run_id=run.id).order_by(RuntimeSourceRecord.created_at.asc()).all()]
        if payload.get("include_evidence"):
            from ..models import RuntimeEvidenceRecord

            result["evidence"] = [evidence.to_dict() for evidence in RuntimeEvidenceRecord.query.filter_by(workflow_run_id=run.id).order_by(RuntimeEvidenceRecord.created_at.asc()).all()]
        if payload.get("include_working_memory", True):
            result["working_memory"] = self._run_working_memory(run)
        return result

    def observe_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        evaluate_runtime_checkpoints(run.id)
        run = db.session.get(WorkflowRun, run.id)
        steps = self._steps_for_run(run.id)
        self._sync_delegation_collection_statuses(run, steps)
        run = db.session.get(WorkflowRun, run.id)
        sample_limit = _clamped_int(payload.get("sample_limit"), default=3, minimum=0, maximum=10)

        status_counts: dict[str, int] = {}
        for step in steps:
            status = str(step.status or "unknown").lower()
            status_counts[status] = status_counts.get(status, 0) + 1
        terminal_statuses = {"completed", "failed", "cancelled", "skipped"}
        finished_count = sum(count for status, count in status_counts.items() if status in terminal_statuses)
        total_count = len(steps)
        progress_percent = round((finished_count / total_count) * 100, 1) if total_count else 100.0

        costs = LLMCost.query.filter_by(workflow_run_id=run.id).all()
        models: dict[str, dict[str, Any]] = {}
        for row in costs:
            model = str(row.model or "unknown")
            summary = models.setdefault(model, {"calls": 0, "cost": 0.0, "tokens": 0})
            summary["calls"] += 1
            summary["cost"] += float(row.cost or 0.0)
            summary["tokens"] += int(row.total_tokens or 0)
        for summary in models.values():
            summary["cost"] = round(float(summary["cost"]), 8)

        failed_steps = [step for step in steps if str(step.status or "").lower() == "failed"]
        completed_steps = [step for step in steps if str(step.status or "").lower() == "completed"]

        def step_sample(step: WorkflowStep, *, include_output: bool) -> dict[str, Any]:
            metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
            delegation = metadata.get("delegation_item") if isinstance(metadata.get("delegation_item"), dict) else {}
            row = {
                "step_id": step.id,
                "step_key": step.step_key,
                "title": step.display_title(),
                "status": step.status,
                "item_id": delegation.get("item_id"),
                "parent_item_id": delegation.get("parent_item_id"),
                "chunk_index": delegation.get("chunk_index"),
                "attempts": (
                    (metadata.get("retry_state") or {}).get("attempts")
                    if isinstance(metadata.get("retry_state"), dict)
                    else 0
                ),
            }
            if step.error:
                row["error"] = str(step.error)[:1200]
            if include_output:
                row["result"] = _bounded_json_value(step.output_payload or {}, 2400)
            return row

        run_payload = run.to_dict()
        observation = {
            "run": {
                "id": run.id,
                "title": run_payload.get("display_title"),
                "status": run.status,
                "updated_at": run_payload.get("updated_at"),
                "completion_outcome": (run.metadata_json or {}).get("completion_outcome"),
            },
            "progress": {
                "total_steps": total_count,
                "finished_steps": finished_count,
                "percent": progress_percent,
                "status_counts": status_counts,
                "max_workers": self._compiled_max_workers(run),
            },
            "cost": {
                "calls": len(costs),
                "cost": round(sum(float(row.cost or 0.0) for row in costs), 8),
                "prompt_tokens": sum(int(row.prompt_tokens or 0) for row in costs),
                "completion_tokens": sum(int(row.completion_tokens or 0) for row in costs),
                "total_tokens": sum(int(row.total_tokens or 0) for row in costs),
                "models": models,
            },
            "failures": {
                "count": len(failed_steps),
                "items": [step_sample(step, include_output=False) for step in failed_steps[:sample_limit]],
            },
            "sampled_results": [
                step_sample(step, include_output=True)
                for step in completed_steps[-sample_limit:]
            ] if sample_limit else [],
            "checkpoints": {
                "pending": run_payload.get("pending_runtime_checkpoint_count") or 0,
                "due": run_payload.get("due_runtime_checkpoint_count") or 0,
                "latest_due": run_payload.get("latest_due_runtime_checkpoint"),
            },
            "collections": run_payload.get("runtime_collections") or [],
        }
        if payload.get("include_artifacts", True) is not False:
            artifacts = self._artifacts_for_run(run.id)
            observation["artifacts"] = [
                {
                    key: artifact.to_dict().get(key)
                    for key in (
                        "id",
                        "label",
                        "type",
                        "mime_type",
                        "size_bytes",
                        "review_status",
                        "download_url",
                        "created_at",
                    )
                }
                for artifact in artifacts[-20:]
            ]
        return observation

    @staticmethod
    def _compiled_max_workers(run: WorkflowRun) -> int:
        metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
        compiled = metadata.get("compiled_run") if isinstance(metadata.get("compiled_run"), dict) else {}
        concurrency = compiled.get("concurrency") if isinstance(compiled.get("concurrency"), dict) else {}
        return _clamped_int(
            concurrency.get("max_workers") or metadata.get("max_concurrency"),
            default=4,
            minimum=1,
            maximum=32,
        )

    def _sync_delegation_collection_statuses(
        self,
        run: WorkflowRun,
        steps: list[WorkflowStep],
    ) -> None:
        self._collection_service(run).sync_delegation_steps(steps)

    def list_steps(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        rows = self._steps_for_run(run.id)
        status = payload.get("status")
        if status:
            rows = [row for row in rows if row.status == str(status).lower()]
        return {"steps": [row.to_dict() for row in rows], "run": run.to_dict(), "count": len(rows)}

    def list_artifacts(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        rows = self._artifacts_for_run(run.id)
        review_status = payload.get("review_status") or payload.get("status")
        if review_status:
            rows = [row for row in rows if row.review_status == str(review_status).lower()]
        return {"artifacts": [row.to_dict() for row in rows], "run": run.to_dict(), "count": len(rows)}

    def read_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        artifact_id = payload.get("artifact_id")
        if artifact_id:
            artifact = self._require_artifact(artifact_id)
        else:
            run = self._require_run(payload.get("run_id"))
            artifacts = self._artifacts_for_run(run.id)
            if not artifacts:
                raise KeyError(f"No artifacts found for workflow run '{run.id}'.")
            artifact = artifacts[-1]
        content = read_text_artifact_content(
            artifact,
            instance_path=current_app.instance_path,
            max_chars=int(payload.get("max_chars") or 20000),
        )
        return {"artifact": artifact.to_dict(), "content": content}

    def read_working_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        memory = self._run_working_memory(run)
        key = payload.get("key")
        if key:
            return {"run": run.to_dict(), "key": str(key), "value": memory.get(str(key))}
        return {"run": run.to_dict(), "working_memory": memory}

    def create_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        record = self._collection_service(run).create(payload)
        self._record_collection_event(
            run,
            "collection.created",
            record.collection_key,
            {
                "adapter_id": record.adapter_id,
                "kind": record.kind,
                "storage": "rows_v1",
            },
        )
        return {
            "run": db.session.get(WorkflowRun, run.id).to_dict(),
            "collection": record.to_dict(include_items=False),
            "available_adapters": RuntimeCollectionService.adapter_cards(),
        }

    def get_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        include_items = payload.get("include_items", True) is not False
        status = str(payload.get("status") or "").strip().lower()
        limit = _clamped_int(payload.get("limit"), default=100, minimum=1, maximum=1000)
        return {
            "run": run.to_dict(),
            "collection": self._collection_service(run).get(
                payload.get("collection_id"),
                include_items=include_items,
                status=status or None,
                limit=limit,
            ),
        }

    def upsert_collection_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        result = self._collection_service(run).upsert(
            payload.get("collection_id"),
            payload.get("items"),
            adapter_id=payload.get("adapter_id") or payload.get("adapter"),
        )
        self._record_collection_event(
            run,
            "collection.items.upserted",
            result["collection"]["id"],
            {
                "adapter_id": result["adapter_id"],
                "created_count": result["created_count"],
                "updated_count": result["updated_count"],
            },
        )
        return {"run": db.session.get(WorkflowRun, run.id).to_dict(), **result}

    def partition_collection_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        statuses = _string_values(payload.get("statuses"))
        if not statuses and payload.get("status"):
            statuses = [str(payload.get("status"))]
        max_items = _clamped_int(payload.get("max_items_per_partition"), default=10, minimum=1, maximum=500)
        max_input_tokens = _clamped_int(payload.get("max_input_tokens"), default=30000, minimum=1000, maximum=200000)
        reserved_output_tokens = _clamped_int(payload.get("reserved_output_tokens"), default=4000, minimum=0, maximum=100000)
        chars_per_token = _clamped_float(payload.get("chars_per_token"), default=4.0, minimum=1.0, maximum=12.0)
        result = self._collection_service(run).partition(
            payload.get("collection_id"),
            statuses=statuses or ["pending"],
            partition_id=payload.get("partition_id"),
            max_items_per_partition=max_items,
            max_input_tokens=max_input_tokens,
            reserved_output_tokens=reserved_output_tokens,
            chars_per_token=chars_per_token,
            include_annotations=payload.get("include_annotations") is True,
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
        self._record_collection_event(
            run,
            "collection.partition.created",
            result["collection"]["id"],
            {
                "partition_id": result["partition"]["id"],
                "item_count": result["partition"]["item_count"],
                "partition_count": result["partition"]["partition_count"],
            },
        )
        return {"run": db.session.get(WorkflowRun, run.id).to_dict(), **result}

    def claim_collection_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        limit = _clamped_int(payload.get("limit"), default=10, minimum=1, maximum=200)
        claimant = str(payload.get("claimant") or self.enforced_step_id or self.user_id or "worker").strip()
        lease_seconds = _clamped_int(payload.get("lease_seconds"), default=900, minimum=30, maximum=86400)
        result = self._collection_service(run).claim(
            payload.get("collection_id"),
            limit=limit,
            claimant=claimant,
            statuses=payload.get("statuses"),
            lease_seconds=lease_seconds,
        )
        self._record_collection_event(
            run,
            "collection.items.leased",
            result["collection"]["id"],
            {
                "claimant": result["claimant"],
                "claimed_count": result["claimed_count"],
                "lease_expires_at": result["lease_expires_at"],
            },
        )
        return {"run": db.session.get(WorkflowRun, run.id).to_dict(), **result}

    def annotate_collection_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        result = self._collection_service(run).annotate(
            payload.get("collection_id"),
            payload.get("annotations"),
            lease_token=payload.get("lease_token") or payload.get("claim_id"),
            require_lease=payload.get("require_lease", True) is not False,
        )
        self._record_collection_event(
            run,
            (
                "collection.annotation.rejected"
                if result["rejected_count"]
                else "collection.items.annotated"
            ),
            result["collection"]["id"],
            {
                "updated_count": result["updated_count"],
                "rejected_count": result["rejected_count"],
                "missing_count": len(result["missing"]),
            },
            status="warning" if result["rejected_count"] else "completed",
        )
        return {"run": db.session.get(WorkflowRun, run.id).to_dict(), **result}

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = payload.get("workflow_id")
        template_id = payload.get("template_id")
        if workflow_id and template_id:
            raise ValueError("Provide either workflow_id or template_id, not both.")
        kind = payload.get("kind") or ("template" if (workflow_id or template_id) else "ad_hoc")
        workflow = None
        template_record = None
        compiled_template = None

        if workflow_id and self.addon_manager:
            workflow = self.addon_manager.get_workflow_record(workflow_id)
            if not workflow:
                raise KeyError(f"Workflow '{workflow_id}' not found.")
        if template_id:
            template_record = self._require_template(template_id)
            if not template_record.is_active:
                raise ValueError("Workflow template is inactive.")

        log_source_ids = payload.get("log_source_ids")
        if workflow and not log_source_ids:
            log_source_ids = [link.get("id") for link in workflow.log_links if isinstance(link, dict) and link.get("id")]

        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if "execution_scope" not in metadata and (payload.get("runtime_profile_ids") or payload.get("model_profile_ids")):
            metadata = {
                **metadata,
                "execution_scope": ExecutionScope(
                    runtime_profile_ids=payload.get("runtime_profile_ids"),
                    model_profile_ids=payload.get("model_profile_ids"),
                ).to_dict(),
            }
        if isinstance(payload.get("runtime_policy"), dict) and "runtime_policy" not in metadata:
            metadata = {**metadata, "runtime_policy": payload["runtime_policy"]}
        if payload.get("max_tool_calls") is not None and "max_tool_calls" not in metadata:
            metadata = {**metadata, "max_tool_calls": payload.get("max_tool_calls")}
        plan_payload = payload.get("plan")
        if template_record:
            storage = read_template_storage(template_record.definition_json)
            definition = active_template_definition(storage)
            compiled_template = compile_template_definition(definition, fallback_name=template_record.title)
            plan_payload = {**compiled_template.plan, **(plan_payload if isinstance(plan_payload, dict) else {})}
            metadata = {
                **metadata,
                "template_runtime": {
                    "type": "user_template",
                    "template_id": template_record.id,
                    "active_version": int(storage["active_version"]),
                    "step_count": len(compiled_template.steps),
                    "definition_version": int(compiled_template.definition.get("version") or 1),
                },
            }
        elif workflow:
            definition = executable_definition_from_addon_workflow(workflow)
            if definition:
                compiled_template = compile_template_definition(definition, fallback_name=workflow.name)
                plan_payload = {**compiled_template.plan, **(plan_payload if isinstance(plan_payload, dict) else {})}
                metadata = {
                    **metadata,
                    "template_runtime": {
                        "type": "addon_workflow",
                        "workflow_id": workflow.id,
                        "step_count": len(compiled_template.steps),
                        "definition_version": int(compiled_template.definition.get("version") or 1),
                    },
                }

        run_workflow_id = workflow_id
        if template_record and not run_workflow_id:
            run_workflow_id = f"user_template:{template_record.id}"

        record = create_workflow_run(
            title=payload.get("title") or payload.get("name"),
            kind=kind,
            workflow_id=run_workflow_id,
            source_addon_id=payload.get("source_addon_id") or (workflow.addon_id if workflow else ("user_template" if template_record else None)),
            status=payload.get("status") or "draft",
            input_payload=payload.get("input") or payload.get("input_payload"),
            understanding=payload.get("understanding"),
            plan=plan_payload,
            state=payload.get("state"),
            current_step=payload.get("current_step"),
            created_by_user_id=self.user_id,
            chat_session_id=self._validated_chat_session_id(payload.get("chat_session_id") or self.chat_session_id),
            approval_ids=payload.get("approval_ids"),
            resource_reference_ids=payload.get("resource_reference_ids"),
            job_ids=payload.get("job_ids"),
            step_ids=payload.get("step_ids"),
            source_ids=payload.get("source_ids"),
            evidence_ids=payload.get("evidence_ids"),
            artifact_ids=payload.get("artifact_ids"),
            log_source_ids=log_source_ids,
            resume_url=payload.get("resume_url"),
            cancel_url=payload.get("cancel_url"),
            metadata=metadata,
        )
        if compiled_template:
            self._materialize_compiled_template_steps(record, compiled_template)
        if payload.get("start"):
            if record.status != "running":
                record = set_workflow_run_status(record.id, "running")
            self._trigger_orchestrator()
            record = db.session.get(WorkflowRun, record.id)
        return {"run": record.to_dict()}

    def update_understanding(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        record = update_workflow_run_planning(
            run.id,
            understanding=payload.get("understanding"),
            status=payload.get("status"),
            merge=not bool(payload.get("replace")),
        )
        return {"run": record.to_dict()}

    def update_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        record = update_workflow_run_planning(
            run.id,
            title=payload.get("title") or payload.get("name"),
            plan=payload.get("plan"),
            status=payload.get("status"),
            current_step=payload.get("current_step"),
            merge=not bool(payload.get("replace")),
        )
        return {"run": record.to_dict()}

    def set_checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        checkpoint = create_runtime_checkpoint(
            workflow_run_id=run.id,
            title=payload.get("title"),
            kind=payload.get("kind"),
            action=payload.get("action"),
            reason=payload.get("reason"),
            instruction=payload.get("instruction"),
            condition=payload.get("condition") if isinstance(payload.get("condition"), dict) else None,
            due_at=payload.get("due_at"),
            due_in_seconds=payload.get("due_in_seconds"),
            stall_after_seconds=payload.get("stall_after_seconds"),
            after_completed_steps=payload.get("after_completed_steps"),
            target_completed_steps=payload.get("target_completed_steps"),
            created_by_user_id=self.user_id,
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )
        refreshed_run = db.session.get(WorkflowRun, run.id)
        return {"checkpoint": self._checkpoint_from_run(refreshed_run, checkpoint["id"]), "run": refreshed_run.to_dict()}

    def update_checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        checkpoint = update_runtime_checkpoint(
            run.id,
            str(payload.get("checkpoint_id") or ""),
            status=payload.get("status") or "resolved",
            note=payload.get("note"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )
        refreshed_run = db.session.get(WorkflowRun, run.id)
        return {"checkpoint": self._checkpoint_from_run(refreshed_run, checkpoint["id"]), "run": refreshed_run.to_dict()}

    def start_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        if run.status in {"completed", "failed", "cancelled"}:
            raise ValueError(f"Workflow run is already {run.status}.")
        if run.status == "paused":
            record, resume_summary = resume_workflow_run(run.id)
            self._trigger_orchestrator()
            return {"run": db.session.get(WorkflowRun, record.id).to_dict(), "resume_summary": resume_summary}
        record = set_workflow_run_status(run.id, "running")
        self._trigger_orchestrator()
        return {"run": db.session.get(WorkflowRun, record.id).to_dict()}

    def pause_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        if run.status in {"completed", "failed", "cancelled"}:
            raise ValueError(f"Workflow run is already {run.status}.")
        if run.status == "paused":
            return {"run": run.to_dict(), "pause_summary": {"paused_steps": 0, "running_steps": 0, "waiting_steps": 0}}
        record, pause_summary = pause_workflow_run(run.id)
        return {"run": db.session.get(WorkflowRun, record.id).to_dict(), "pause_summary": pause_summary}

    def resume_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        if run.status in {"completed", "failed", "cancelled"}:
            raise ValueError(f"Workflow run is already {run.status}.")
        if run.status != "paused":
            raise ValueError(f"Workflow run is {run.status}. Only paused runs can be resumed.")
        record, resume_summary = resume_workflow_run(run.id)
        self._trigger_orchestrator()
        return {"run": db.session.get(WorkflowRun, record.id).to_dict(), "resume_summary": resume_summary}

    def add_step(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        step_type = payload.get("step_type") or payload.get("type")
        if not step_type:
            raise ValueError("step_type is required.")
        completion_requirements = payload.get("completion_requirements")
        if completion_requirements is not None and not isinstance(completion_requirements, dict):
            raise ValueError("completion_requirements must be an object when provided.")
        retry_policy = payload.get("retry_policy")
        if retry_policy is not None and not isinstance(retry_policy, dict):
            raise ValueError("retry_policy must be an object when provided.")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if completion_requirements is not None:
            metadata = {**metadata, "completion_requirements": completion_requirements}
        if retry_policy is not None:
            metadata = {**metadata, "retry_policy": retry_policy}
        execution_scope = ExecutionScope.from_dict((run.metadata_json or {}).get("execution_scope"))
        if "execution_scope" not in metadata:
            metadata = {**metadata, "execution_scope": execution_scope.to_dict()}
        run_metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
        if "runtime_policy" not in metadata and isinstance(run_metadata.get("runtime_policy"), dict):
            metadata = {**metadata, "runtime_policy": run_metadata["runtime_policy"]}
        if "max_tool_calls" not in metadata and run_metadata.get("max_tool_calls") is not None:
            metadata = {**metadata, "max_tool_calls": run_metadata.get("max_tool_calls")}
        scope_check = execution_scope.check_child_request(
            capability_ids=payload.get("capability_ids"),
            skill_ids=payload.get("skill_ids"),
            tool_ids=payload.get("tool_ids"),
            tool_view_ids=payload.get("tool_view_ids"),
            resource_reference_ids=payload.get("resource_reference_ids"),
        )
        if not scope_check.allowed:
            error = scope_check.to_error()
            error["allowed_capability_ids"] = list(execution_scope.capability_ids)
            error["allowed_skill_ids"] = list(execution_scope.skill_ids)
            error["allowed_tool_ids"] = list(execution_scope.tool_ids)
            error["allowed_tool_view_ids"] = list(execution_scope.tool_view_ids)
            error["allowed_resource_reference_ids"] = list(execution_scope.resource_reference_ids)
            raise PermissionError(json.dumps(error, sort_keys=True))

        step = create_workflow_step(
            workflow_run_id=run.id,
            step_type=step_type,
            title=payload.get("title"),
            step_key=payload.get("step_key") or payload.get("key"),
            status=payload.get("status") or "planned",
            sequence=payload.get("sequence"),
            parent_step_id=payload.get("parent_step_id"),
            depends_on_step_ids=payload.get("depends_on_step_ids"),
            assigned_model_role=payload.get("assigned_model_role") or payload.get("model_role"),
            created_by_user_id=self.user_id,
            instructions=payload.get("instructions"),
            capability_ids=scope_check.capability_ids,
            skill_ids=scope_check.skill_ids,
            tool_ids=scope_check.tool_ids,
            tool_view_ids=scope_check.tool_view_ids,
            provider_requirements=payload.get("provider_requirements"),
            resource_reference_ids=scope_check.resource_reference_ids,
            approval_ids=payload.get("approval_ids"),
            job_ids=payload.get("job_ids"),
            artifact_ids=payload.get("artifact_ids"),
            source_ids=payload.get("source_ids"),
            evidence_ids=payload.get("evidence_ids"),
            input_schema=payload.get("input_schema"),
            output_schema=payload.get("output_schema"),
            input_payload=payload.get("input"),
            output_payload=payload.get("output"),
            error=payload.get("error"),
            metadata=metadata,
            log_source_ids=payload.get("log_source_ids"),
            make_current=bool(payload.get("make_current")),
        )
        return {"step": step.to_dict(), "run": db.session.get(WorkflowRun, run.id).to_dict()}

    def update_step(self, payload: dict[str, Any]) -> dict[str, Any]:
        step = self._require_step(payload.get("step_id"))
        status = payload.get("status") or step.status
        updated = set_workflow_step_status(
            step.id,
            status,
            output_payload=payload.get("output"),
            error=payload.get("error"),
            make_current=bool(payload.get("make_current")),
        )
        return {"step": updated.to_dict(), "run": db.session.get(WorkflowRun, updated.workflow_run_id).to_dict()}

    def add_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        source_type = payload.get("source_type") or payload.get("type")
        uri = payload.get("uri") or payload.get("resource_uri") or payload.get("url")
        if not source_type or not uri:
            raise ValueError("source_type and uri are required.")

        source = SourceLedger.record_source(
            workflow_run_id=run.id,
            source_type=source_type,
            uri=uri,
            step_id=payload.get("step_id"),
            provider=payload.get("provider"),
            external_id=payload.get("external_id"),
            title=payload.get("title"),
            summary=payload.get("summary"),
            content_hash=payload.get("content_hash"),
            access_scope=payload.get("access_scope") or "private",
            created_by_user_id=self.user_id,
            metadata=payload.get("metadata"),
        )
        return {"source": source.to_dict(), "run": db.session.get(WorkflowRun, run.id).to_dict()}

    def add_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        artifact = ArtifactStore.create_artifact(
            artifact_type=payload.get("artifact_type") or payload.get("type") or "file",
            job_id=payload.get("job_id"),
            workflow_run_id=run.id,
            step_id=payload.get("step_id"),
            source_addon_id=payload.get("source_addon_id") or run.source_addon_id,
            created_by_user_id=self.user_id,
            uri=payload.get("uri"),
            path=payload.get("path"),
            remote_reference=payload.get("remote_reference"),
            mime_type=payload.get("mime_type"),
            content_hash=payload.get("content_hash"),
            size_bytes=payload.get("size_bytes"),
            label=payload.get("label"),
            source_ids=payload.get("source_ids"),
            evidence_ids=payload.get("evidence_ids"),
            metadata=payload.get("metadata"),
            change_summary=payload.get("change_summary"),
            review_status=payload.get("review_status") or "draft",
            retention_policy=payload.get("retention_policy") or "default",
            download_permission=payload.get("download_permission") or "owner",
        )
        return {"artifact": artifact.to_dict(), "run": db.session.get(WorkflowRun, run.id).to_dict()}

    def create_text_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        content = payload.get("content")
        if content is None:
            raise ValueError("content is required.")

        filename = _safe_artifact_filename(
            payload.get("filename")
            or payload.get("label")
            or f"{run.title or fallback_run_title(run) or 'artifact'}.md"
        )
        if not Path(filename).suffix:
            filename = f"{filename}.md"

        artifacts_dir = Path(current_app.instance_path) / "artifacts" / "workflow_runs" / run.id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_artifact_path(artifacts_dir / filename)
        target.write_text(str(content), encoding="utf-8")

        artifact = create_local_file_artifact(
            instance_path=current_app.instance_path,
            file_path=target,
            workflow_run_id=run.id,
            step_id=payload.get("step_id"),
            source_addon_id=payload.get("source_addon_id") or run.source_addon_id,
            created_by_user_id=self.user_id,
            label=payload.get("label") or target.stem.replace("_", " ").replace("-", " ").strip() or target.name,
            metadata=payload.get("metadata"),
            change_summary=payload.get("change_summary") or "Created text artifact from workflow output.",
            review_status=payload.get("review_status") or "draft",
            download_permission=payload.get("download_permission") or "owner",
        )
        refreshed_run = self._maybe_complete_ad_hoc_run(
            run.id,
            auto_complete=payload.get("auto_complete_run") is not False,
        )
        return self._artifact_tool_response(run=refreshed_run, artifact=artifact)

    def quick_create_text_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("content") is None:
            raise ValueError("content is required.")
        if self.enforced_run_id and not payload.get("run_id"):
            payload = {**payload, "run_id": self.enforced_run_id}
        run_id = payload.get("run_id")
        if run_id:
            run = self._require_run(run_id)
        else:
            run = create_workflow_run(
                title=payload.get("title") or payload.get("label") or payload.get("filename"),
                kind="ad_hoc",
                source_addon_id=payload.get("source_addon_id"),
                status="draft",
                input_payload=payload.get("input"),
                created_by_user_id=self.user_id,
                chat_session_id=self._validated_chat_session_id(payload.get("chat_session_id") or self.chat_session_id),
                metadata=payload.get("run_metadata") if isinstance(payload.get("run_metadata"), dict) else None,
            )
        artifact_payload = {
            "run_id": run.id,
            "step_id": payload.get("step_id"),
            "content": payload.get("content"),
            "filename": payload.get("filename"),
            "label": payload.get("label"),
            "metadata": payload.get("metadata"),
            "change_summary": payload.get("change_summary"),
            "review_status": payload.get("review_status"),
            "download_permission": payload.get("download_permission"),
            "auto_complete_run": payload.get("auto_complete_run", True),
        }
        return self.create_text_artifact(artifact_payload)

    def compile_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .runs.compiler import RunCompiler

        return RunCompiler(self).compile(payload)

    def _compiled_step_list(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw_steps = payload.get("steps")
        if raw_steps is None and isinstance(payload.get("plan"), dict):
            raw_steps = payload["plan"].get("steps")
        if not isinstance(raw_steps, list):
            return []
        return [dict(item) for item in raw_steps if isinstance(item, dict)]

    def _compiled_delegation_plan(
        self,
        payload: dict[str, Any],
        *,
        delegation: dict[str, Any],
        model_preferences: dict[str, Any],
        concurrency: dict[str, Any],
    ) -> dict[str, Any]:
        requested_strategy = str(
            payload.get("execution_strategy")
            or payload.get("strategy")
            or ""
        ).strip().lower()
        if not delegation and requested_strategy not in {"bounded_map", "delegate", "exploratory_workers"}:
            return {}

        objective = str(
            delegation.get("objective")
            or payload.get("objective")
            or payload.get("goal")
            or payload.get("user_request_summary")
            or ""
        ).strip()
        if not objective:
            raise ValueError("delegation.objective or objective is required for compiled delegation.")

        raw_items = _compiled_delegation_items(payload, delegation)
        exploratory = (
            str(delegation.get("strategy") or requested_strategy).lower()
            in {"exploratory", "exploratory_workers"}
        )
        if not raw_items and exploratory:
            queries = _string_values(
                delegation.get("exploratory_queries")
                or delegation.get("queries")
            )
            if not queries:
                worker_count = _clamped_int(
                    delegation.get("worker_count"),
                    default=concurrency["max_workers"],
                    minimum=1,
                    maximum=concurrency["max_workers"],
                )
                queries = [
                    f"{objective} (independent exploration angle {index})"
                    for index in range(1, worker_count + 1)
                ]
            raw_items = [
                {
                    "id": f"explore_{index:04d}",
                    "title": query,
                    "metadata": {
                        "exploratory_query": query,
                        "strategy": "direct_worker_search",
                    },
                }
                for index, query in enumerate(queries, start=1)
            ]
        if not raw_items:
            raise ValueError(
                "Compiled delegation requires delegation.items, resources, or exploratory_queries."
            )

        max_input_tokens = _clamped_int(
            delegation.get("max_input_tokens"),
            default=30000,
            minimum=1000,
            maximum=200000,
        )
        reserved_output_tokens = _clamped_int(
            delegation.get("reserved_output_tokens"),
            default=4000,
            minimum=0,
            # Preserve a useful input window even when the caller supplies an
            # output reserve larger than (or nearly equal to) the total budget.
            maximum=max(0, max_input_tokens - 256),
        )
        chars_per_token = _clamped_float(
            delegation.get("chars_per_token"),
            default=4.0,
            minimum=1.0,
            maximum=12.0,
        )
        work_items, item_stats = _prepare_compiled_delegation_items(
            raw_items,
            max_input_tokens=max_input_tokens,
            reserved_output_tokens=reserved_output_tokens,
            chars_per_token=chars_per_token,
        )
        collection_id = _safe_collection_id(
            delegation.get("collection_id")
            or f"delegation-{hashlib.sha1(objective.encode('utf-8'), usedforsecurity=False).hexdigest()[:12]}"
        )
        output_schema = _normalize_compiled_json_schema(
            delegation.get("expected_schema")
            if isinstance(delegation.get("expected_schema"), dict)
            else delegation.get("output_schema")
            if isinstance(delegation.get("output_schema"), dict)
            else (payload.get("validation") or {}).get("output_schema")
            if isinstance(payload.get("validation"), dict)
            and isinstance((payload.get("validation") or {}).get("output_schema"), dict)
            else {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "status": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary"],
            }
        )
        retry_policy = (
            delegation.get("retry_policy")
            if isinstance(delegation.get("retry_policy"), dict)
            else payload.get("retry_policy")
            if isinstance(payload.get("retry_policy"), dict)
            else {
                "enabled": True,
                "max_attempts": 2,
                "backoff_seconds": 0,
                "retry_on": "all",
            }
        )
        worker_model = str(
            delegation.get("model")
            or delegation.get("model_role")
            or model_preferences.get("worker_model")
            or "fast"
        ).strip()
        max_tool_calls = clamp_tool_call_iterations(
            delegation.get("max_tool_calls")
            or (delegation.get("budget") or {}).get("max_tool_calls")
            if isinstance(delegation.get("budget"), dict)
            else delegation.get("max_tool_calls"),
            default=8,
        )
        continue_on_failure = delegation.get("continue_on_item_failure", True) is not False
        capability_ids = _string_values(delegation.get("capability_ids"))
        tool_ids = _string_values(delegation.get("tool_ids"))
        tool_view_ids = _string_values(delegation.get("tool_view_ids"))
        skill_ids = _string_values(delegation.get("skill_ids"))
        common_resource_refs = _string_values(delegation.get("resource_reference_ids"))

        worker_steps: list[dict[str, Any]] = []
        for index, item in enumerate(work_items, start=1):
            item_id = str(item.get("id") or f"item_{index:04d}")
            step_key = _safe_step_key(f"map_{index:04d}_{item_id}")
            item_resource_refs = _string_values([
                *common_resource_refs,
                *_string_values(item.get("resource_reference_ids")),
                *_string_values((item.get("metadata") or {}).get("resource_reference_ids")),
            ])
            worker_steps.append({
                "step_key": step_key,
                "step_type": "delegated_map_item",
                "title": str(item.get("title") or f"Process item {index}")[:255],
                "assigned_model_role": worker_model,
                "instructions": _compiled_delegation_worker_instructions(
                    objective=objective,
                    item=item,
                    exploratory=exploratory,
                    output_schema=output_schema,
                ),
                "capability_ids": capability_ids,
                "skill_ids": skill_ids,
                "tool_ids": tool_ids,
                "tool_view_ids": tool_view_ids,
                "resource_reference_ids": item_resource_refs,
                "input": {
                    "objective": objective,
                    "item": _delegation_worker_item_payload(item),
                    "tool_scope": {
                        "capability_ids": capability_ids,
                        "tool_ids": tool_ids,
                        "tool_view_ids": tool_view_ids,
                    },
                    "budget": {
                        "max_input_tokens": max_input_tokens,
                        "reserved_output_tokens": reserved_output_tokens,
                        "max_tool_calls": max_tool_calls,
                    },
                },
                "output_schema": output_schema,
                "retry_policy": retry_policy,
                "metadata": {
                    "max_tool_calls": max_tool_calls,
                    "parallel_tool_calls": concurrency["parallel_tool_calls"],
                    "continue_on_failure": continue_on_failure,
                    "delegation_item": {
                        "collection_id": collection_id,
                        "item_id": item_id,
                        "parent_item_id": item.get("parent_item_id") or item_id,
                        "fingerprint": item.get("fingerprint"),
                        "chunk_index": item.get("chunk_index"),
                        "chunk_count": item.get("chunk_count"),
                    },
                    "validation": {
                        "structured_output": True,
                        "targeted_repair": True,
                    },
                },
            })

        reduction_raw = delegation.get("reduction")
        if isinstance(reduction_raw, str):
            reduction = {"mode": reduction_raw}
        elif isinstance(reduction_raw, dict):
            reduction = dict(reduction_raw)
        else:
            reduction = {}
        reduction_mode = str(reduction.get("mode") or "none").strip().lower()
        reducer_step = None
        if reduction_mode not in {"", "none", "off", "false"}:
            reducer_schema = _normalize_compiled_json_schema(
                reduction.get("output_schema")
                if isinstance(reduction.get("output_schema"), dict)
                else {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "item_count": {"type": "integer"},
                        "failure_count": {"type": "integer"},
                        "artifact_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["summary"],
                }
            )
            completion_requirements = None
            reduction_instruction = (
                "Reduce the structured dependency findings into a concise final synthesis. "
                "Dependency payloads may be artifact-backed references; read only targeted slices. "
                "Report item failures explicitly and do not request the raw corpus from the chief."
            )
            if reduction_mode == "artifact":
                reduction_instruction += (
                    " Create the user-visible report with runs_create_text_artifact and return its artifact id."
                )
                completion_requirements = {"requires_artifact": True}
            elif reduction_mode == "code":
                reduction_instruction += (
                    " Prefer deterministic reduction through runs_execute_python when python.scratch is in scope; "
                    "otherwise reduce the structured findings directly and state the limitation."
                )
            reducer_step = {
                "step_key": _safe_step_key(reduction.get("step_key") or "reduce_delegation_results"),
                "step_type": "delegated_reduce",
                "title": str(reduction.get("title") or "Reduce delegated findings")[:255],
                "assigned_model_role": str(
                    reduction.get("model")
                    or reduction.get("model_role")
                    or model_preferences.get("reducer_model")
                    or worker_model
                ),
                "instructions": str(reduction.get("instructions") or reduction_instruction),
                "depends_on_step_ids": [step["step_key"] for step in worker_steps],
                "capability_ids": _string_values(reduction.get("capability_ids") or capability_ids),
                "tool_ids": _string_values(reduction.get("tool_ids") or tool_ids),
                "tool_view_ids": _string_values(reduction.get("tool_view_ids") or tool_view_ids),
                "resource_reference_ids": common_resource_refs,
                "output_schema": reducer_schema,
                "completion_requirements": completion_requirements,
                "retry_policy": retry_policy,
                "metadata": {
                    "max_tool_calls": clamp_tool_call_iterations(
                        reduction.get("max_tool_calls"),
                        default=max_tool_calls,
                    ),
                    "parallel_tool_calls": concurrency["parallel_tool_calls"],
                    "allow_failed_dependencies": True,
                    "delegation_reducer": {
                        "collection_id": collection_id,
                        "mode": reduction_mode,
                        "worker_step_count": len(worker_steps),
                    },
                },
            }

        steps = [*worker_steps, *([reducer_step] if reducer_step else [])]
        summary = {
            "collection_id": collection_id,
            "objective": objective,
            "strategy": "exploratory_workers" if exploratory else "bounded_map",
            "model": worker_model,
            "max_workers": concurrency["max_workers"],
            "parallel_tool_calls": concurrency["parallel_tool_calls"],
            "max_input_tokens": max_input_tokens,
            "reserved_output_tokens": reserved_output_tokens,
            "max_items_per_worker": 1,
            "max_tool_calls": max_tool_calls,
            "source_item_count": item_stats["source_item_count"],
            "deduplicated_item_count": item_stats["deduplicated_item_count"],
            "duplicate_count": item_stats["duplicate_count"],
            "work_item_count": len(work_items),
            "split_parent_count": item_stats["split_parent_count"],
            "chunk_count": item_stats["chunk_count"],
            "unresolved_oversized_count": item_stats["unresolved_oversized_count"],
            "continue_on_item_failure": continue_on_failure,
            "reduction_mode": reduction_mode or "none",
            "reducer_step_key": reducer_step.get("step_key") if reducer_step else None,
            "worker_step_keys": [step["step_key"] for step in worker_steps],
        }
        return {
            "steps": steps,
            "items": work_items,
            "collection": {
                "collection_id": collection_id,
                "title": str(delegation.get("collection_title") or f"Delegated work: {objective}")[:255],
                "kind": str(delegation.get("collection_kind") or "delegated_items")[:100],
                "description": objective,
                "identity_keys": ["fingerprint", "identity", "source_id", "canonical_uri"],
                "schema": output_schema,
                "metadata": {
                    "compiled": True,
                    "strategy": summary["strategy"],
                    "model": worker_model,
                },
            },
            "summary": summary,
        }

    @staticmethod
    def _compiled_step_preview(step: dict[str, Any]) -> dict[str, Any]:
        metadata = step.get("metadata") if isinstance(step.get("metadata"), dict) else {}
        delegation = metadata.get("delegation_item") if isinstance(metadata.get("delegation_item"), dict) else {}
        return {
            "id": step.get("id"),
            "step_key": step.get("step_key"),
            "type": step.get("type"),
            "title": step.get("display_title") or step.get("title"),
            "status": step.get("status"),
            "assigned_model_role": step.get("assigned_model_role"),
            "depends_on_step_ids": step.get("depends_on_step_ids") or [],
            "item_id": delegation.get("item_id"),
            "parent_item_id": delegation.get("parent_item_id"),
            "chunk_index": delegation.get("chunk_index"),
            "chunk_count": delegation.get("chunk_count"),
        }

    def _compiled_plan_payload(
        self,
        payload: dict[str, Any],
        *,
        steps: list[dict[str, Any]],
        checkpoints: list[dict[str, Any]],
    ) -> dict[str, Any]:
        plan = dict(payload.get("plan") if isinstance(payload.get("plan"), dict) else {})
        if steps:
            step_summaries = [
                {
                    "key": step.get("step_key") or step.get("key"),
                    "title": step.get("title"),
                    "type": step.get("step_type") or step.get("type"),
                    "depends_on": step.get("depends_on_step_ids") or step.get("depends_on") or [],
                }
                for step in steps
            ]
            if not isinstance(plan.get("steps"), list):
                plan["steps"] = step_summaries
            else:
                existing_keys = {
                    str(item.get("key") or item.get("step_key") or "")
                    for item in plan["steps"]
                    if isinstance(item, dict)
                }
                plan["steps"] = [
                    *plan["steps"],
                    *[
                        item
                        for item in step_summaries
                        if str(item.get("key") or "") not in existing_keys
                    ],
                ]
        if checkpoints and "checkpoints" not in plan:
            plan["checkpoints"] = [
                {
                    "title": checkpoint.get("title"),
                    "kind": checkpoint.get("kind"),
                    "reason": checkpoint.get("reason"),
                }
                for checkpoint in checkpoints
            ]
        return plan

    def _compiled_step_arguments(self, step_payload: dict[str, Any], *, run_id: str, sequence: int) -> dict[str, Any]:
        step_args = dict(step_payload)
        step_args["run_id"] = run_id
        step_args.setdefault("sequence", sequence)
        if "step_type" not in step_args and "type" in step_args:
            step_args["step_type"] = step_args["type"]
        if "step_key" not in step_args and "key" in step_args:
            step_args["step_key"] = step_args["key"]
        if "depends_on_step_ids" not in step_args and "depends_on" in step_args:
            step_args["depends_on_step_ids"] = step_args["depends_on"]
        step_args.setdefault("status", "planned")
        return step_args

    def _validate_compiled_steps_against_scope(self, steps: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
        execution_scope = ExecutionScope.from_dict(metadata.get("execution_scope") if isinstance(metadata, dict) else None)
        if execution_scope.is_autonomous:
            return
        for index, step in enumerate(steps, start=1):
            check = execution_scope.check_child_request(
                capability_ids=step.get("capability_ids"),
                skill_ids=step.get("skill_ids"),
                tool_ids=step.get("tool_ids"),
                tool_view_ids=step.get("tool_view_ids"),
                resource_reference_ids=step.get("resource_reference_ids"),
            )
            if check.allowed:
                continue
            error = check.to_error()
            error["step_index"] = index
            error["step_key"] = step.get("step_key") or step.get("key")
            error["step_title"] = step.get("title")
            error["allowed_capability_ids"] = list(execution_scope.capability_ids)
            error["allowed_skill_ids"] = list(execution_scope.skill_ids)
            error["allowed_tool_ids"] = list(execution_scope.tool_ids)
            error["allowed_tool_view_ids"] = list(execution_scope.tool_view_ids)
            error["allowed_resource_reference_ids"] = list(execution_scope.resource_reference_ids)
            raise PermissionError(json.dumps(error, sort_keys=True))

    def _compiled_run_complexity(
        self,
        *,
        steps: list[dict[str, Any]],
        checkpoints: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        strategy: str,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        reasons = []
        if len(steps) > 3:
            reasons.append("many_steps")
        if len(sources) > 5:
            reasons.append("broad_source_access")
        if strategy in {
            "bounded_map",
            "delegate",
            "exploratory_workers",
            "collection_processing",
            "code_in_loop",
            "code_mediated",
        }:
            reasons.append(strategy)
        if checkpoints:
            reasons.append("checkpoints")
        if any(self._compiled_step_has_write_risk(step) for step in steps):
            reasons.append("side_effect_or_write_risk")
        if str(policy.get("cost") or policy.get("cost_class") or "").lower() in {"high", "expensive"}:
            reasons.append("high_cost")
        return {
            "is_complex": bool(reasons),
            "reasons": reasons,
            "step_count": len(steps),
            "source_count": len(sources),
            "checkpoint_count": len(checkpoints),
        }

    def _compiled_step_has_write_risk(self, step: dict[str, Any]) -> bool:
        text = " ".join([
            str(step.get("risk_class") or ""),
            str(step.get("operation") or ""),
            str(step.get("title") or ""),
            str(step.get("instructions") or ""),
            " ".join(str(item) for item in (step.get("side_effects") or [])),
        ]).lower()
        return any(token in text for token in ("write", "update", "delete", "publish", "send", "side_effect", "external_write"))

    def _compiled_run_requires_preview(
        self,
        *,
        complexity: dict[str, Any],
        start_preference: str,
        preview_preference: str,
    ) -> bool:
        if preview_preference == "always" or start_preference == "preview_then_start":
            return True
        if preview_preference == "never":
            return False
        if start_preference == "draft_only":
            return False
        if start_preference == "start_now":
            return preview_preference == "auto" and bool(complexity.get("is_complex"))
        if preview_preference == "never_if_simple" and not complexity.get("is_complex"):
            return False
        return bool(complexity.get("is_complex"))

    def inspect_document(self, payload: dict[str, Any]) -> dict[str, Any]:
        service, run, _step = self._document_service(payload)
        result = service.inspect(payload.get("file_ref"))
        self._record_document_operation(run, "inspect", result)
        return result

    def preview_document_pages(self, payload: dict[str, Any]) -> dict[str, Any]:
        service, run, _step = self._document_service(payload)
        result = service.preview_pages(
            payload.get("file_ref"),
            pages=payload.get("pages"),
            dpi=_clamped_int(payload.get("dpi"), default=120, minimum=72, maximum=220),
        )
        self._record_document_operation(run, "preview_pages", result)
        return result

    def extract_document_pages(self, payload: dict[str, Any]) -> dict[str, Any]:
        service, run, _step = self._document_service(payload)
        result = service.extract(
            payload.get("file_ref"),
            pages=payload.get("pages"),
            output_format=payload.get("output_format") or "markdown",
            engine=payload.get("engine") or "direct",
            force_ocr=bool(payload.get("force_ocr")),
        )
        self._record_document_operation(run, "extract_pages", result)
        return result

    def split_document_pages(self, payload: dict[str, Any]) -> dict[str, Any]:
        service, run, _step = self._document_service(payload)
        result = service.split_pages(
            payload.get("file_ref"),
            pages=payload.get("pages"),
            mode=payload.get("mode") or "selection",
        )
        self._record_document_operation(run, "split_pages", result)
        return result

    def _document_service(
        self,
        payload: dict[str, Any],
    ) -> tuple[DocumentService, WorkflowRun | None, WorkflowStep | None]:
        run = self._require_run(payload.get("run_id")) if payload.get("run_id") else None
        step = self._require_step(payload.get("step_id")) if payload.get("step_id") else None
        if step and run and step.workflow_run_id != run.id:
            raise ValueError("step_id does not belong to this run.")
        if step and run is None:
            run = self._require_run(step.workflow_run_id)
        if self.enforced_run_id and run is None:
            run = self._require_run(self.enforced_run_id)
        if self.enforced_step_id and step is None:
            step = self._require_step(self.enforced_step_id)

        advanced_extractor = None
        tool = None
        if self.addon_manager and hasattr(self.addon_manager, "get_tool_record"):
            tool = self.addon_manager.get_tool_record("doc_processor.parse_document")
        if tool is not None:
            runtime_policy = (
                (run.metadata_json or {}).get("runtime_policy")
                if run and isinstance(run.metadata_json, dict)
                else None
            )

            def advanced_extractor(
                *,
                path: Path,
                page_range: str,
                engine: str,
                force_ocr: bool,
            ) -> dict[str, Any]:
                function_name = _tool_function_name_from_record(tool)
                requested_engine = str(engine or "").lower()
                arguments = {
                    "filepath": str(path),
                    "engine": "auto",
                    "force_ocr": bool(force_ocr or requested_engine == "ocr"),
                }
                if page_range:
                    arguments["page_range"] = page_range
                content = self.addon_manager.execute_capability(
                    function_name,
                    arguments,
                    self.tool_config,
                    requester_user_id=self.user_id,
                    runtime_policy=runtime_policy if isinstance(runtime_policy, dict) else None,
                    audit_log_source_id="runs.document_service",
                    log_source_ids=("runs.document_service",),
                    workflow_run_id=run.id if run else None,
                )
                if isinstance(content, dict) and content.get("status") in {
                    "access_denied",
                    "addon_inactive",
                    "invalid_tool_call",
                    "scope_denied",
                    "tool_execution_failed",
                    "waiting_for_approval",
                }:
                    raise PermissionError(json.dumps(content, sort_keys=True))
                if inspect.isgenerator(content):
                    content = next(
                        (
                            item
                            for item in content
                            if isinstance(item, dict) and item.get("type") in {"finish", "error"}
                        ),
                        "",
                    )
                return {
                    "content": content,
                    "provider": "doc_processor.parse_document",
                }

        return (
            DocumentService(
                instance_path=current_app.instance_path,
                app_root_path=current_app.root_path,
                owner_user_id=self.user_id,
                is_admin=self.is_admin,
                workflow_run_id=run.id if run else None,
                workflow_step_id=step.id if step else None,
                advanced_extractor=advanced_extractor,
            ),
            run,
            step,
        )

    def _record_document_operation(
        self,
        run: WorkflowRun | None,
        operation: str,
        result: dict[str, Any],
    ) -> None:
        if run is None:
            return
        metadata = dict(run.metadata_json) if isinstance(run.metadata_json, dict) else {}
        operations = (
            metadata.get("document_operations")
            if isinstance(metadata.get("document_operations"), list)
            else []
        )
        resource = result.get("resource") if isinstance(result.get("resource"), dict) else {}
        metadata["document_operations"] = [
            *operations,
            {
                "operation": operation,
                "status": result.get("status"),
                "resource_reference": resource.get("reference"),
                "selected_pages": result.get("selected_pages") or [],
                "artifact_ids": result.get("artifact_ids") or [],
                "provider": result.get("provider"),
                "at": _utc_iso(),
            },
        ][-100:]
        run.metadata_json = metadata
        db.session.commit()

    def create_code_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        service, run, _step = self._code_session_service(
            payload,
            create_run_if_missing=True,
        )
        self._require_runtime_profile(run, "python.session")
        session_payload = service.create(
            resources=payload.get("resources"),
            dependencies=payload.get("dependencies"),
            label=payload.get("label"),
        )
        return {
            "run": db.session.get(WorkflowRun, run.id).to_dict(),
            "session": session_payload,
        }

    def get_code_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        service, run, _step = self._code_session_service(payload)
        return {
            "run": run.to_dict(),
            "session": service.get(payload.get("session_id")),
        }

    def execute_code_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        service, run, _step = self._code_session_service(payload)
        self._require_runtime_profile(run, "python.session")
        result = service.execute(
            payload.get("session_id"),
            code=payload.get("code"),
            input_value=payload.get("input"),
            timeout_seconds=_clamped_int(
                payload.get("timeout_seconds"),
                default=60,
                minimum=1,
                maximum=300,
            ),
            max_output_chars=_clamped_int(
                payload.get("max_output_chars"),
                default=50_000,
                minimum=1_000,
                maximum=500_000,
            ),
            idempotency_key=payload.get("idempotency_key"),
        )
        result["run"] = db.session.get(WorkflowRun, run.id).to_dict()
        return result

    def install_code_session_dependencies(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        service, run, _step = self._code_session_service(payload)
        self._require_runtime_profile(run, "python.session")
        runtime_policy = (
            (run.metadata_json or {}).get("runtime_policy")
            if isinstance(run.metadata_json, dict)
            else None
        )
        install_tool = SimpleNamespace(
            id="runtime.python.install_dependencies",
            legacy_name="runs_code_session_install_dependencies",
            side_effects=["network", "dependency_change"],
            approval_required=True,
            resource_requirements=[],
            source="runtime",
        )
        guard = guard_tool_execution(
            install_tool,
            arguments={
                "session_id": payload.get("session_id"),
                "mechanism": "pixi",
            },
            requester_user_id=self.user_id,
            approval_id=payload.get("_approval_id"),
            workflow_run_id=run.id,
            audit_log_source_id="runs.code_session.dependencies",
            log_source_ids=["runs.code_session.dependencies"],
            runtime_policy=runtime_policy if isinstance(runtime_policy, dict) else None,
        )
        if not guard.allowed:
            if guard.approval_id and guard.approval_id not in (run.approval_ids or []):
                run.approval_ids = [*(run.approval_ids or []), guard.approval_id]
                db.session.commit()
            return {
                "run": db.session.get(WorkflowRun, run.id).to_dict(),
                "session": service.get(payload.get("session_id")),
                "dependency_install": guard.to_tool_response(),
            }
        result = service.install_dependencies(
            payload.get("session_id"),
            timeout_seconds=_clamped_int(
                payload.get("timeout_seconds"),
                default=600,
                minimum=30,
                maximum=1200,
            ),
        )
        result["run"] = db.session.get(WorkflowRun, run.id).to_dict()
        return result

    def _code_session_service(
        self,
        payload: dict[str, Any],
        *,
        create_run_if_missing: bool = False,
    ) -> tuple[CodeSessionService, WorkflowRun, WorkflowStep | None]:
        run_id = payload.get("run_id") or self.enforced_run_id
        if run_id:
            run = self._require_run(run_id)
        elif create_run_if_missing:
            metadata = (
                payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {}
            )
            run = create_workflow_run(
                title=payload.get("label") or "Governed Python session",
                kind="ad_hoc",
                status="draft",
                input_payload={
                    "purpose": "governed_code_session",
                    "resource_count": len(payload.get("resources") or []),
                },
                created_by_user_id=self.user_id,
                chat_session_id=self._validated_chat_session_id(self.chat_session_id),
                metadata=metadata,
            )
        else:
            raise ValueError("run_id is required.")

        step_id = payload.get("step_id") or self.enforced_step_id
        step = self._require_step(step_id) if step_id else None
        if step and step.workflow_run_id != run.id:
            raise ValueError("step_id does not belong to this run.")
        execution_scope = ExecutionScope.from_dict(
            (run.metadata_json or {}).get("execution_scope")
        )

        document_service = DocumentService(
            instance_path=current_app.instance_path,
            app_root_path=current_app.root_path,
            owner_user_id=self.user_id,
            is_admin=self.is_admin,
            workflow_run_id=run.id,
            workflow_step_id=step.id if step else None,
        )

        def resolve_resource(reference: Any) -> dict[str, Any]:
            resource = document_service.resolve(reference)
            if resource.resource_reference_id:
                scope_check = execution_scope.check_child_request(
                    resource_reference_ids=[resource.resource_reference_id],
                )
                if not scope_check.allowed:
                    error = scope_check.to_error()
                    error["allowed_resource_reference_ids"] = list(
                        execution_scope.resource_reference_ids
                    )
                    raise PermissionError(json.dumps(error, sort_keys=True))
            return {
                **resource.to_dict(),
                "path": resource.path,
            }

        def execute_tools(
            calls: list[dict[str, Any]],
            continue_on_error: bool,
        ) -> dict[str, Any]:
            batch_result = self.execute_tool_batch({
                "run_id": run.id,
                "step_id": step.id if step else None,
                "calls": calls,
                "continue_on_error": continue_on_error,
                "max_calls": min(50, len(calls)),
                "max_result_chars": 100_000,
            })
            return batch_result["batch"]

        def map_agents(arguments: dict[str, Any], action_id: str) -> dict[str, Any]:
            return self._map_code_session_agents(
                run,
                step,
                arguments,
                action_id=action_id,
            )

        def register_artifact(
            *,
            path: Path,
            label: str,
            metadata: dict[str, Any],
            review_status: str,
            action_id: str,
        ) -> dict[str, Any]:
            for artifact in RuntimeArtifact.query.filter_by(
                workflow_run_id=run.id
            ).order_by(RuntimeArtifact.created_at.desc()).limit(500).all():
                artifact_metadata = (
                    artifact.metadata_json
                    if isinstance(artifact.metadata_json, dict)
                    else {}
                )
                if artifact_metadata.get("code_session_action_id") == action_id:
                    return artifact.to_dict()
            artifact = create_local_file_artifact(
                instance_path=current_app.instance_path,
                file_path=path,
                workflow_run_id=run.id,
                step_id=step.id if step else None,
                source_addon_id=run.source_addon_id,
                created_by_user_id=self.user_id,
                label=label,
                metadata={
                    **(metadata if isinstance(metadata, dict) else {}),
                    "code_session_action_id": action_id,
                },
                change_summary="Created by a governed Python code session.",
                review_status=review_status,
                retention_policy="code_session",
                download_permission="owner",
            )
            return artifact.to_dict()

        def emit_event(kind: str, event_payload: dict[str, Any]) -> None:
            status = str(event_payload.get("status") or "completed")
            packet = make_agent_event(
                kind=kind,
                phase="code",
                status=status,
                title=kind.replace(".", " ").replace("_", " ").title(),
                summary=str(
                    event_payload.get("error")
                    or event_payload.get("message")
                    or status
                )[:500],
                subject_type="code_session",
                subject_id=event_payload.get("session_id"),
                payload={
                    **event_payload,
                    "runtime_context": {
                        "origin": "code",
                        "runtime_profile_id": "python.session",
                        "workflow_run_id": run.id,
                        "workflow_step_id": step.id if step else None,
                    },
                },
            )
            append_agent_event_to_metadata_attr(run, packet, limit=300)
            if step is not None:
                append_agent_event_to_metadata_attr(step, packet, limit=300)
            db.session.commit()

        return (
            CodeSessionService(
                instance_path=current_app.instance_path,
                owner_user_id=self.user_id,
                run_id=run.id,
                step_id=step.id if step else None,
                resource_resolver=resolve_resource,
                tool_executor=execute_tools,
                agent_mapper=map_agents,
                artifact_registrar=register_artifact,
                event_emitter=emit_event,
                python_executable=sys.executable,
            ),
            run,
            step,
        )

    def create_project_workspace(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        selection = self._project_workspace_selection(payload)
        access = resolve_project_workspace_access(selection, payload.get("access"))
        service, run, _step = self._project_workspace_service(
            payload,
            create_run_if_missing=True,
            selection=selection,
        )
        self._require_runtime_profile(run, PROJECT_WORKSPACE_PROFILE_ID)
        effects = ["workspace_read", "process"]
        if access == "read_write":
            effects.append("workspace_write")
        guard = self._guard_project_workspace_action(
            run,
            payload,
            tool_id="runtime.project_workspace.open",
            legacy_name="runs_project_workspace_create",
            effects=effects,
            arguments={
                "workspace_root_id": selection.root.id,
                "workspace_path": selection.relative_path,
                "access": access,
            },
        )
        if not guard.allowed:
            return {
                "run": db.session.get(WorkflowRun, run.id).to_dict(),
                "workspace_access": guard.to_tool_response(),
            }
        workspace = service.create(
            workspace_root=selection.path,
            label=payload.get("label"),
            access=access,
        )
        return {
            "run": db.session.get(WorkflowRun, run.id).to_dict(),
            "workspace": workspace,
        }

    def get_project_workspace(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        service, run, _step = self._project_workspace_service(payload)
        self._require_runtime_profile(run, PROJECT_WORKSPACE_PROFILE_ID)
        return {
            "run": db.session.get(WorkflowRun, run.id).to_dict(),
            "workspace": service.get(payload.get("workspace_id")),
        }

    def inspect_project_workspace(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        service, run, _step = self._project_workspace_service(payload)
        self._require_runtime_profile(run, PROJECT_WORKSPACE_PROFILE_ID)
        result = service.inspect(
            payload.get("workspace_id"),
            operation=payload.get("operation"),
            path=payload.get("path"),
            pattern=payload.get("pattern") or "*",
            max_entries=_clamped_int(
                payload.get("max_entries"),
                default=200,
                minimum=1,
                maximum=2_000,
            ),
            max_depth=_clamped_int(
                payload.get("max_depth"),
                default=4,
                minimum=0,
                maximum=20,
            ),
            max_chars=_clamped_int(
                payload.get("max_chars"),
                default=100_000,
                minimum=1_000,
                maximum=500_000,
            ),
            include_hidden=bool(payload.get("include_hidden")),
        )
        result["run"] = db.session.get(WorkflowRun, run.id).to_dict()
        return result

    def edit_project_workspace(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        service, run, _step = self._project_workspace_service(
            payload,
            require_write_access=True,
        )
        self._require_runtime_profile(run, PROJECT_WORKSPACE_PROFILE_ID)
        content = payload.get("content")
        replacements = payload.get("replacements")
        guard = self._guard_project_workspace_action(
            run,
            payload,
            tool_id="runtime.project_workspace.edit",
            legacy_name="runs_project_workspace_edit",
            effects=["workspace_write"],
            arguments={
                "workspace_id": payload.get("workspace_id"),
                "path": payload.get("path"),
                "create": bool(payload.get("create")),
                "expected_sha256": payload.get("expected_sha256"),
                "content_chars": (
                    len(str(content)) if content is not None else None
                ),
                "replacement_count": (
                    len(replacements) if isinstance(replacements, list) else 0
                ),
            },
        )
        if not guard.allowed:
            return {
                "run": db.session.get(WorkflowRun, run.id).to_dict(),
                "workspace": service.get(payload.get("workspace_id")),
                "edit": guard.to_tool_response(),
            }
        result = service.edit(
            payload.get("workspace_id"),
            path=payload.get("path"),
            content=content,
            replacements=replacements,
            expected_sha256=payload.get("expected_sha256"),
            create=bool(payload.get("create")),
        )
        result["run"] = db.session.get(WorkflowRun, run.id).to_dict()
        return result

    def execute_project_workspace(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        service, run, _step = self._project_workspace_service(
            payload,
            require_write_access=True,
        )
        self._require_runtime_profile(run, PROJECT_WORKSPACE_PROFILE_ID)
        argv = payload.get("argv") if isinstance(payload.get("argv"), list) else []
        classification = ProjectWorkspaceService.classify_command(
            [str(value) for value in argv]
        )
        required_flags = []
        if classification["network"] and not bool(payload.get("allow_network")):
            required_flags.append("allow_network")
        if classification["dependency_change"] and not bool(
            payload.get("allow_dependency_changes")
        ):
            required_flags.append("allow_dependency_changes")
        if classification["destructive"] and not bool(
            payload.get("allow_destructive")
        ):
            required_flags.append("allow_destructive")
        if required_flags:
            raise PermissionError(
                "Command intent requires explicit declaration(s): "
                + ", ".join(required_flags)
                + "."
            )
        effects = ["process", "workspace_read", "workspace_write"]
        if classification["network"] or bool(payload.get("allow_network")):
            effects.append("network")
        if classification["dependency_change"] or bool(
            payload.get("allow_dependency_changes")
        ):
            effects.append("dependency_change")
        if classification["destructive"] or bool(payload.get("allow_destructive")):
            effects.append("destructive")
        guard = self._guard_project_workspace_action(
            run,
            payload,
            tool_id="runtime.project_workspace.execute",
            legacy_name="runs_project_workspace_execute",
            effects=effects,
            arguments={
                "workspace_id": payload.get("workspace_id"),
                "argv": argv,
                "cwd": payload.get("cwd") or "",
                "timeout_seconds": payload.get("timeout_seconds") or 300,
                "background": bool(payload.get("background")),
                "allow_network": bool(payload.get("allow_network")),
                "allow_dependency_changes": bool(
                    payload.get("allow_dependency_changes")
                ),
                "allow_destructive": bool(payload.get("allow_destructive")),
                "classification": classification,
            },
        )
        if not guard.allowed:
            return {
                "run": db.session.get(WorkflowRun, run.id).to_dict(),
                "workspace": service.get(payload.get("workspace_id")),
                "process": guard.to_tool_response(),
            }
        result = service.execute(
            payload.get("workspace_id"),
            argv=argv,
            cwd=payload.get("cwd"),
            environment=payload.get("environment"),
            timeout_seconds=_clamped_int(
                payload.get("timeout_seconds"),
                default=300,
                minimum=1,
                maximum=3_600,
            ),
            background=bool(payload.get("background")),
            max_output_chars=_clamped_int(
                payload.get("max_output_chars"),
                default=20_000,
                minimum=1_000,
                maximum=200_000,
            ),
            allow_network=bool(payload.get("allow_network")),
            allow_dependency_changes=bool(
                payload.get("allow_dependency_changes")
            ),
            allow_destructive=bool(payload.get("allow_destructive")),
        )
        result["run"] = db.session.get(WorkflowRun, run.id).to_dict()
        return result

    def control_project_workspace_process(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        service, run, _step = self._project_workspace_service(payload)
        self._require_runtime_profile(run, PROJECT_WORKSPACE_PROFILE_ID)
        result = service.process(
            payload.get("workspace_id"),
            operation=payload.get("operation") or "get",
            process_id=payload.get("process_id"),
        )
        result["run"] = db.session.get(WorkflowRun, run.id).to_dict()
        return result

    def close_project_workspace(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        service, run, _step = self._project_workspace_service(payload)
        self._require_runtime_profile(run, PROJECT_WORKSPACE_PROFILE_ID)
        return {
            "run": db.session.get(WorkflowRun, run.id).to_dict(),
            "workspace": service.close(payload.get("workspace_id")),
        }

    def _guard_project_workspace_action(
        self,
        run: WorkflowRun,
        payload: dict[str, Any],
        *,
        tool_id: str,
        legacy_name: str,
        effects: list[str],
        arguments: dict[str, Any],
    ):
        runtime_policy = (
            (run.metadata_json or {}).get("runtime_policy")
            if isinstance(run.metadata_json, dict)
            else None
        )
        guard = guard_tool_execution(
            SimpleNamespace(
                id=tool_id,
                legacy_name=legacy_name,
                side_effects=effects,
                approval_required=True,
                resource_requirements=[],
                source="runtime",
            ),
            arguments=arguments,
            requester_user_id=self.user_id,
            approval_id=payload.get("_approval_id"),
            workflow_run_id=run.id,
            audit_log_source_id="runs.project_workspace",
            log_source_ids=["runs.project_workspace"],
            runtime_policy=(
                runtime_policy if isinstance(runtime_policy, dict) else None
            ),
        )
        if (
            not guard.allowed
            and guard.approval_id
            and guard.approval_id not in (run.approval_ids or [])
        ):
            run.approval_ids = [*(run.approval_ids or []), guard.approval_id]
            db.session.commit()
        return guard

    def _project_workspace_service(
        self,
        payload: dict[str, Any],
        *,
        create_run_if_missing: bool = False,
        require_write_access: bool = False,
        selection: ProjectWorkspaceSelection | None = None,
    ) -> tuple[ProjectWorkspaceService, WorkflowRun, WorkflowStep | None]:
        run_id = payload.get("run_id") or self.enforced_run_id
        if run_id:
            run = self._require_run(run_id)
        elif create_run_if_missing:
            if selection is None:
                raise ValueError("A project workspace root selection is required.")
            metadata = (
                payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {}
            )
            run = create_workflow_run(
                title=payload.get("label") or "Project workspace",
                kind="ad_hoc",
                status="draft",
                input_payload={
                    "purpose": "project_workspace_execution",
                    "workspace_root_id": selection.root.id,
                    "workspace_path": selection.relative_path,
                },
                created_by_user_id=self.user_id,
                chat_session_id=self._validated_chat_session_id(
                    self.chat_session_id
                ),
                metadata=metadata,
            )
        else:
            raise ValueError("run_id is required.")

        step_id = payload.get("step_id") or self.enforced_step_id
        step = self._require_step(step_id) if step_id else None
        if step and step.workflow_run_id != run.id:
            raise ValueError("step_id does not belong to this run.")

        configured_roots = current_app.config.get("PROJECT_WORKSPACE_ROOTS", [])
        authorized_roots = authorized_project_workspace_roots(
            configured_roots,
            user_id=self.user_id,
            is_admin=self.is_admin,
        )
        allowed_roots = [
            root.path
            for root in authorized_roots
            if not require_write_access or root.access == "read_write"
        ]
        configured_executables = current_app.config.get(
            "PROJECT_WORKSPACE_EXECUTABLES"
        )
        if isinstance(configured_executables, str):
            executable_allowlist = [
                value.strip()
                for value in configured_executables.split(",")
                if value.strip()
            ]
        else:
            executable_allowlist = configured_executables

        def register_artifact(
            *,
            path: Path,
            label: str,
            metadata: dict[str, Any],
            review_status: str,
            action_id: str,
        ) -> dict[str, Any]:
            for artifact in RuntimeArtifact.query.filter_by(
                workflow_run_id=run.id
            ).order_by(RuntimeArtifact.created_at.desc()).limit(500).all():
                artifact_metadata = (
                    artifact.metadata_json
                    if isinstance(artifact.metadata_json, dict)
                    else {}
                )
                if (
                    artifact_metadata.get("project_workspace_action_id")
                    == action_id
                ):
                    return artifact.to_dict()
            artifact = create_local_file_artifact(
                instance_path=current_app.instance_path,
                file_path=path,
                workflow_run_id=run.id,
                step_id=step.id if step else None,
                source_addon_id=run.source_addon_id,
                created_by_user_id=self.user_id,
                label=label,
                metadata={
                    **(metadata if isinstance(metadata, dict) else {}),
                    "project_workspace_action_id": action_id,
                },
                change_summary="Created by an observable project workspace action.",
                review_status=review_status,
                retention_policy="project_workspace",
                download_permission="owner",
            )
            return artifact.to_dict()

        def emit_event(kind: str, event_payload: dict[str, Any]) -> None:
            public_event_payload = self._sanitize_project_workspace_payload(
                event_payload
            )
            status = str(event_payload.get("status") or "completed")
            packet = make_agent_event(
                kind=kind,
                phase="code",
                status=status,
                title=kind.replace(".", " ").replace("_", " ").title(),
                summary=str(
                    public_event_payload.get("error")
                    or public_event_payload.get("message")
                    or status
                )[:500],
                subject_type="project_workspace",
                subject_id=public_event_payload.get("workspace_id"),
                payload={
                    **public_event_payload,
                    "runtime_context": {
                        "origin": "project",
                        "runtime_profile_id": PROJECT_WORKSPACE_PROFILE_ID,
                        "workflow_run_id": run.id,
                        "workflow_step_id": step.id if step else None,
                    },
                },
            )
            append_agent_event_to_metadata_attr(run, packet, limit=300)
            if step is not None:
                append_agent_event_to_metadata_attr(step, packet, limit=300)
            db.session.commit()

        return (
            ProjectWorkspaceService(
                instance_path=current_app.instance_path,
                owner_user_id=self.user_id,
                run_id=run.id,
                step_id=step.id if step else None,
                allowed_roots=allowed_roots,
                artifact_registrar=register_artifact,
                event_emitter=emit_event,
                python_executable=sys.executable,
                executable_allowlist=executable_allowlist,
            ),
            run,
            step,
        )

    def _project_workspace_selection(
        self,
        payload: dict[str, Any],
    ) -> ProjectWorkspaceSelection:
        return resolve_project_workspace_selection(
            current_app.config.get("PROJECT_WORKSPACE_ROOTS", []),
            user_id=self.user_id,
            is_admin=self.is_admin,
            root_id=payload.get("workspace_root_id"),
            relative_path=payload.get("workspace_path"),
            legacy_absolute_path=payload.get("workspace_root"),
        )

    def _map_code_session_agents(
        self,
        run: WorkflowRun,
        parent_step: WorkflowStep | None,
        payload: dict[str, Any],
        *,
        action_id: str,
    ) -> dict[str, Any]:
        existing = [
            step
            for step in WorkflowStep.query.filter_by(workflow_run_id=run.id).all()
            if (
                isinstance(step.metadata_json, dict)
                and (step.metadata_json.get("code_session_map") or {}).get("action_id")
                == action_id
            )
        ]
        if existing:
            return {
                "status": "scheduled",
                "idempotent_replay": True,
                "action_id": action_id,
                "concurrency": (
                    (existing[0].metadata_json.get("code_session_map") or {}).get(
                        "concurrency"
                    )
                ),
                "steps": [step.to_dict() for step in existing],
            }

        items = payload.get("items")
        instructions = str(payload.get("instructions") or "").strip()
        if not isinstance(items, list) or not items:
            raise ValueError("agents.map requires a non-empty items list.")
        if len(items) > 100:
            raise ValueError("agents.map supports at most 100 items.")
        if len(json.dumps(items, ensure_ascii=False, default=str)) > 2_000_000:
            raise ValueError("agents.map items exceed the 2 MB request limit.")
        if not instructions:
            raise ValueError("agents.map instructions are required.")
        concurrency = _clamped_int(
            payload.get("concurrency"),
            default=4,
            minimum=1,
            maximum=8,
        )
        run_metadata = (
            dict(run.metadata_json)
            if isinstance(run.metadata_json, dict)
            else {}
        )
        try:
            existing_limit = int(run_metadata.get("max_concurrency"))
        except (TypeError, ValueError):
            existing_limit = concurrency
        run_metadata["max_concurrency"] = min(
            max(1, existing_limit),
            concurrency,
        )
        map_records = (
            run_metadata.get("code_session_maps")
            if isinstance(run_metadata.get("code_session_maps"), list)
            else []
        )
        run_metadata["code_session_maps"] = [
            *map_records,
            {
                "action_id": action_id,
                "item_count": len(items),
                "concurrency": concurrency,
                "model_role": str(payload.get("model_role") or "fast")[:100],
                "created_at": _utc_iso(),
            },
        ][-100:]
        run.metadata_json = run_metadata
        db.session.commit()
        model_role = str(payload.get("model_role") or "fast")[:100]
        base_title = str(payload.get("title") or "Mapped code-session work")[:180]
        output_schema = (
            payload.get("output_schema")
            if isinstance(payload.get("output_schema"), dict)
            else None
        )
        current_max_sequence = max(
            (
                int(step.sequence)
                for step in WorkflowStep.query.filter_by(workflow_run_id=run.id).all()
                if step.sequence is not None
            ),
            default=0,
        )
        created = []
        for index, item in enumerate(items, start=1):
            item_text = json.dumps(item, ensure_ascii=False, default=str)
            response = self.add_step({
                "run_id": run.id,
                "step_type": "agent",
                "step_key": f"code-map-{action_id[:12]}-{index:04d}",
                "title": f"{base_title} {index}/{len(items)}",
                "status": "planned",
                "sequence": current_max_sequence + index,
                "depends_on_step_ids": [parent_step.id] if parent_step else [],
                "assigned_model_role": model_role,
                "instructions": (
                    f"{instructions}\n\n"
                    "Process exactly this mapped item and return only the requested result.\n"
                    f"Mapped item:\n{item_text}"
                ),
                "input": {
                    "item": item,
                    "index": index - 1,
                    "item_count": len(items),
                    "code_session_action_id": action_id,
                },
                "output_schema": output_schema,
                "metadata": {
                    "code_session_map": {
                        "action_id": action_id,
                        "index": index - 1,
                        "item_count": len(items),
                        "concurrency": concurrency,
                    },
                    "scheduler": {
                        "concurrency_group": f"code-session-map:{action_id}",
                        "concurrency_limit": concurrency,
                    },
                },
            })
            created.append(response["step"])
        if run.status == "draft":
            set_workflow_run_status(run.id, "running")
        self._trigger_orchestrator()
        return {
            "status": "scheduled",
            "idempotent_replay": False,
            "action_id": action_id,
            "concurrency": concurrency,
            "step_count": len(created),
            "steps": created,
        }

    def _compiled_parallel_groups(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        groups = plan.get("parallel_groups") if isinstance(plan, dict) else None
        return [dict(item) for item in groups if isinstance(item, dict)] if isinstance(groups, list) else []

    def execute_python(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload.get("run_id") or self.enforced_run_id
        auto_created_run = not bool(run_id)

        code = str(payload.get("code") or payload.get("script") or "")
        if not code.strip():
            raise ValueError("code is required.")
        if len(code) > 100_000:
            raise ValueError("code is too large for the scratch runtime.")

        if run_id:
            run = self._require_run(run_id)
        else:
            metadata = (
                payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {}
            )
            run = create_workflow_run(
                title=payload.get("label") or "Scratch Python execution",
                kind="ad_hoc",
                status="draft",
                input_payload={"purpose": "python_scratch"},
                created_by_user_id=self.user_id,
                chat_session_id=self._validated_chat_session_id(
                    self.chat_session_id
                ),
                metadata=metadata,
            )
        self._require_runtime_profile(run, "python.scratch")
        step = self._require_step(payload.get("step_id")) if payload.get("step_id") else None
        if step and step.workflow_run_id != run.id:
            raise ValueError("step_id does not belong to this run.")

        timeout_seconds = _clamped_int(payload.get("timeout_seconds"), default=10, minimum=1, maximum=60)
        max_output_chars = _clamped_int(payload.get("max_output_chars"), default=20_000, minimum=1_000, maximum=200_000)
        execution_id = str(uuid.uuid4())
        execution_dir = Path(current_app.instance_path) / "artifacts" / "workflow_runs" / run.id / "code_sessions" / execution_id
        execution_dir.mkdir(parents=True, exist_ok=True)
        script_path = execution_dir / "script.py"
        result_path = execution_dir / "result.json"
        script_path.write_text(code, encoding="utf-8")

        stdin_envelope = {
            "run_id": run.id,
            "step_id": step.id if step else None,
            "input": payload.get("input"),
            "working_memory": self._run_working_memory(run) if payload.get("include_working_memory", True) is not False else {},
            "runtime": {
                "profile_id": "python.scratch",
                "execution_id": execution_id,
                "cwd": str(execution_dir),
            },
        }
        stdin_text = json.dumps(stdin_envelope, ensure_ascii=False, default=str)
        env = _scratch_python_env()
        started_at = utc_iso()
        timed_out = False
        stdout_raw = ""
        stderr_raw = ""
        returncode = None
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", str(script_path)],
                cwd=str(execution_dir),
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                env=env,
                check=False,
            )
            stdout_raw = completed.stdout or ""
            stderr_raw = completed.stderr or ""
            returncode = int(completed.returncode)
            status = "completed" if returncode == 0 else "failed"
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout_raw = _coerce_process_text(exc.stdout)
            stderr_raw = _coerce_process_text(exc.stderr)
            status = "timeout"

        stdout, stdout_truncated = _truncate_text_with_flag(stdout_raw, max_output_chars)
        stderr, stderr_truncated = _truncate_text_with_flag(stderr_raw, max_output_chars)
        result: dict[str, Any] = {
            "id": execution_id,
            "runtime_profile_id": "python.scratch",
            "status": status,
            "returncode": returncode,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "started_at": started_at,
            "finished_at": utc_iso(),
            "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "script_path": str(script_path),
            "cwd": str(execution_dir),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "limitations": [
                "scratch Python only",
                "no connector credentials or toolkit SDK",
                "sanitized environment",
                "not an OS sandbox",
            ],
        }

        if payload.get("parse_stdout_json"):
            try:
                result["parsed_stdout"] = json.loads(stdout.strip() or "null")
            except json.JSONDecodeError as exc:
                result["parsed_stdout_error"] = str(exc)

        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

        working_memory_key = str(payload.get("working_memory_key") or "").strip()
        if working_memory_key:
            state = dict(run.state_json) if isinstance(run.state_json, dict) else {}
            memory = dict(state.get("working_memory")) if isinstance(state.get("working_memory"), dict) else {}
            memory[working_memory_key] = result
            run.state_json = {**state, "working_memory": memory}

        metadata = dict(run.metadata_json) if isinstance(run.metadata_json, dict) else {}
        executions = metadata.get("code_executions") if isinstance(metadata.get("code_executions"), list) else []
        metadata["code_executions"] = [
            *executions,
            {
                "id": execution_id,
                "runtime_profile_id": "python.scratch",
                "status": status,
                "returncode": returncode,
                "timed_out": timed_out,
                "started_at": started_at,
                "finished_at": result["finished_at"],
                "code_sha256": result["code_sha256"],
            },
        ][-50:]
        run.metadata_json = metadata
        db.session.commit()

        if payload.get("save_artifact"):
            artifact_filename = _safe_artifact_filename(
                payload.get("artifact_filename") or f"python-execution-{execution_id[:8]}.json"
            )
            artifact_path = _unique_artifact_path(result_path.with_name(artifact_filename))
            if artifact_path != result_path:
                artifact_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            artifact = create_local_file_artifact(
                instance_path=current_app.instance_path,
                file_path=artifact_path,
                workflow_run_id=run.id,
                step_id=step.id if step else None,
                source_addon_id=run.source_addon_id,
                created_by_user_id=self.user_id,
                label=payload.get("artifact_label") or "Python execution result",
                metadata={
                    "runtime_profile_id": "python.scratch",
                    "execution_id": execution_id,
                    "status": status,
                    "returncode": returncode,
                },
                change_summary="Created scratch Python execution result.",
                review_status="draft",
                download_permission="owner",
            )
            result["artifact"] = artifact.to_dict()

        if auto_created_run:
            # The implicit run represents this single execution. Leaving it in
            # draft makes a completed scratch calculation look like abandoned
            # runtime work and gradually clutters the run list.
            set_workflow_run_status(
                run.id,
                "completed" if status == "completed" else "failed",
            )

        return {"run": db.session.get(WorkflowRun, run.id).to_dict(), "execution": result}

    def execute_tool_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        step = self._require_step(payload.get("step_id")) if payload.get("step_id") else None
        if step and step.workflow_run_id != run.id:
            raise ValueError("step_id does not belong to this run.")
        if not self.addon_manager:
            raise ValueError("addon_manager is required for runs_execute_tool_batch.")

        raw_calls = payload.get("calls")
        if not isinstance(raw_calls, list) or not raw_calls:
            raise ValueError("calls must be a nonempty array.")
        max_calls = _clamped_int(payload.get("max_calls"), default=20, minimum=1, maximum=50)
        max_result_chars = _clamped_int(payload.get("max_result_chars"), default=8000, minimum=1000, maximum=100000)
        continue_on_error = payload.get("continue_on_error", True) is not False
        batch_id = str(uuid.uuid4())
        started_at = utc_iso()
        runtime_policy = (run.metadata_json or {}).get("runtime_policy") if isinstance(run.metadata_json, dict) else None
        selected_calls = [
            call if isinstance(call, dict) else {}
            for call in raw_calls[:max_calls]
        ]
        event_packets: list[dict[str, Any]] = []
        tool_calls = [
            {
                "id": str(call.get("call_id") or f"call_{index + 1}"),
                "type": "function",
                "function": {
                    "name": self._code_call_tool_name(call),
                    "arguments": json.dumps(
                        call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            }
            for index, call in enumerate(selected_calls)
        ]

        active_call_index = 0

        def dispatch_code_tool(_name: str, _arguments: dict[str, Any], _prepared: Any):
            call = selected_calls[active_call_index]
            result = self._execute_code_callable_tool_view_call(
                run,
                call,
                index=active_call_index,
                step=step,
                runtime_policy=runtime_policy if isinstance(runtime_policy, dict) else None,
                max_result_chars=max_result_chars,
            )
            status_value = str(result.get("status") or "").lower()
            if status_value in {
                "access_denied",
                "blocked",
                "scope_denied",
                "waiting_for_approval",
            }:
                action = "blocked"
            elif status_value in {
                "failed",
                "invalid_request",
                "invalid_tool_call",
                "tool_execution_failed",
            }:
                action = "error"
            else:
                action = "tool"
            return {"action": action, "result": result}

        executor = ToolExecutor(
            dispatcher=dispatch_code_tool,
            event_sink=AgentEventSink(
                persist=event_packets.append,
                runtime_context={
                    "origin": "code",
                    "runtime_profile_id": "code_callable_tool_batch",
                    "workflow_run_id": run.id,
                    "workflow_step_id": step.id if step else None,
                    "batch_id": batch_id,
                },
            ),
            authority=ToolAuthorityContext(
                requester_user_id=self.user_id,
                runtime_policy=runtime_policy if isinstance(runtime_policy, dict) else {},
                audit_log_source_id="runs.code_tool_batch",
                log_source_ids=("runs.code_tool_batch",),
                workflow_run_id=run.id,
                workflow_step_id=step.id if step else None,
            ),
            inline_execution=True,
            inline_result_limit_chars=max_result_chars,
            preview_chars=min(2400, max_result_chars),
        )

        call_results: list[dict[str, Any]] = []
        for index, tool_call in enumerate(tool_calls):
            active_call_index = index
            executed = executor.execute_call(tool_call)
            result = (
                dict(executed.result)
                if isinstance(executed.result, dict)
                else {
                    "status": "tool_execution_failed",
                    "error": str(executed.result),
                }
            )
            result["result_envelope"] = executed.result_envelope or {}
            call_results.append(result)
            if result.get("status") != "completed" and not continue_on_error:
                break

        omitted_call_count = max(0, len(raw_calls) - len(call_results))
        status = "completed" if call_results and all(item.get("status") == "completed" for item in call_results) else "partial"
        if call_results and call_results[0].get("status") != "completed" and not continue_on_error:
            status = "failed"

        envelope = {
            "id": batch_id,
            "runtime_profile_id": "code_callable_tool_batch",
            "status": status,
            "started_at": started_at,
            "finished_at": utc_iso(),
            "call_count": len(call_results),
            "omitted_call_count": omitted_call_count,
            "continue_on_error": continue_on_error,
            "calls": call_results,
            "limitations": [
                "tool_view_id calls require code-callable tool views",
                "registered tool calls without tool_view_id require full_access runtime policy",
                "orchestrator executes calls; Python never receives connector credentials",
                "normal approval and resource guards still apply",
            ],
        }

        working_memory_key = str(payload.get("working_memory_key") or "").strip()
        if working_memory_key:
            state = dict(run.state_json) if isinstance(run.state_json, dict) else {}
            memory = dict(state.get("working_memory")) if isinstance(state.get("working_memory"), dict) else {}
            memory[working_memory_key] = envelope
            run.state_json = {**state, "working_memory": memory}

        metadata = dict(run.metadata_json) if isinstance(run.metadata_json, dict) else {}
        batches = metadata.get("code_tool_batches") if isinstance(metadata.get("code_tool_batches"), list) else []
        metadata["code_tool_batches"] = [
            *batches,
            {
                "id": batch_id,
                "runtime_profile_id": "code_callable_tool_batch",
                "status": status,
                "started_at": started_at,
                "finished_at": envelope["finished_at"],
                "call_count": len(call_results),
                "omitted_call_count": omitted_call_count,
                "tool_view_ids": list(dict.fromkeys(
                    item.get("tool_view_id")
                    for item in call_results
                    if item.get("tool_view_id")
                )),
                "tool_ids": list(dict.fromkeys(
                    item.get("tool_id")
                    for item in call_results
                    if item.get("tool_id")
                )),
            },
        ][-50:]
        run.metadata_json = metadata
        for event_packet in event_packets:
            append_agent_event_to_metadata_attr(run, event_packet)
            if step is not None:
                append_agent_event_to_metadata_attr(step, event_packet)
        db.session.commit()

        return {"run": db.session.get(WorkflowRun, run.id).to_dict(), "batch": envelope}

    def _code_call_tool_name(self, call: dict[str, Any]) -> str:
        tool_view_id = str(call.get("tool_view_id") or "").strip()
        if tool_view_id and self.addon_manager:
            tools_registry = getattr(getattr(self.addon_manager, "registries", None), "tools", None)
            view = getattr(tools_registry, "tool_views", {}).get(tool_view_id) if tools_registry else None
            if view and hasattr(self.addon_manager, "get_tool_record"):
                tool = self.addon_manager.get_tool_record(view.tool_id)
                if tool:
                    return _tool_function_name_from_record(tool)
        target = str(
            call.get("tool_id")
            or call.get("function_name")
            or call.get("tool_name")
            or "unresolved_code_tool"
        ).strip()
        if self.addon_manager and hasattr(self.addon_manager, "get_tool_record"):
            tool = self.addon_manager.get_tool_record(target)
            if tool:
                return _tool_function_name_from_record(tool)
        return re.sub(r"[^a-zA-Z0-9_.-]+", "_", target) or "unresolved_code_tool"

    def _execute_code_callable_tool_view_call(
        self,
        run: WorkflowRun,
        call: dict[str, Any],
        *,
        index: int,
        step: WorkflowStep | None,
        runtime_policy: dict[str, Any] | None,
        max_result_chars: int,
    ) -> dict[str, Any]:
        call_id = str(call.get("call_id") or f"call_{index + 1}")
        tool_view_id = str(call.get("tool_view_id") or "").strip()
        base_result: dict[str, Any] = {
            "call_id": call_id,
            "index": index,
            "tool_view_id": tool_view_id,
        }
        if not tool_view_id:
            return self._execute_full_access_registered_tool_call(
                run,
                call,
                index=index,
                step=step,
                runtime_policy=runtime_policy,
                max_result_chars=max_result_chars,
            )

        tools_registry = getattr(getattr(self.addon_manager, "registries", None), "tools", None)
        tool_views = getattr(tools_registry, "tool_views", {}) if tools_registry else {}
        view = tool_views.get(tool_view_id)
        if not view:
            return {**base_result, "status": "invalid_request", "error": f"Tool view '{tool_view_id}' is not registered."}
        if not getattr(view, "code_callable", False):
            return {**base_result, "status": "access_denied", "error": f"Tool view '{tool_view_id}' is not code-callable."}
        scope_check = ExecutionScope.from_dict((run.metadata_json or {}).get("execution_scope")).check_child_request(
            tool_view_ids=[tool_view_id],
        )
        if not scope_check.allowed:
            error = scope_check.to_error()
            error["allowed_tool_view_ids"] = list(ExecutionScope.from_dict((run.metadata_json or {}).get("execution_scope")).tool_view_ids)
            return {**base_result, **error}

        tool = self.addon_manager.get_tool_record(view.tool_id) if hasattr(self.addon_manager, "get_tool_record") else None
        if not tool:
            return {**base_result, "status": "invalid_request", "error": f"Tool for view '{tool_view_id}' is not registered."}
        function_name = _tool_function_name_from_record(tool)
        expected_name = str(call.get("function_name") or call.get("tool_name") or "").strip()
        if expected_name and expected_name not in {function_name, getattr(tool, "id", None), getattr(tool, "legacy_name", None)}:
            return {
                **base_result,
                "function_name": function_name,
                "status": "invalid_request",
                "error": f"Requested function_name '{expected_name}' does not match tool view '{tool_view_id}'.",
            }

        raw_args = dict(call.get("arguments") or {})
        approval_id = raw_args.pop("_approval_id", None)
        resource_reference_ids = raw_args.pop("_resource_reference_ids", None)
        tool_result = self.addon_manager.execute_capability(
            function_name,
            raw_args,
            self.tool_config,
            requester_user_id=self.user_id,
            runtime_policy=runtime_policy,
            approval_id=approval_id,
            resource_reference_ids=resource_reference_ids,
            allowed_parameters=list(getattr(view, "allowed_parameters", []) or []),
            audit_log_source_id="runs.code_tool_batch",
            log_source_ids=("runs.code_tool_batch",),
            workflow_run_id=run.id,
            workflow_step_id=step.id if step else None,
        )
        if isinstance(tool_result, dict) and tool_result.get("status") in {
            "access_denied",
            "addon_inactive",
            "blocked",
            "invalid_tool_call",
            "scope_denied",
            "tool_execution_failed",
            "waiting_for_approval",
        }:
            return {
                **base_result,
                "function_name": function_name,
                "tool_id": getattr(tool, "id", None),
                "status": tool_result.get("status"),
                "error": tool_result.get("error") or tool_result.get("message"),
                "approval_id": tool_result.get("approval_id"),
                "resource_reference_ids": tool_result.get("resource_reference_ids") or [],
            }
        if inspect.isgenerator(tool_result):
            tool_result = next(
                (
                    item
                    for item in tool_result
                    if isinstance(item, dict) and item.get("type") in {"finish", "error"}
                ),
                {"status": "success", "message": "Streaming operation completed."},
            )

        return {
            **base_result,
            "function_name": function_name,
            "tool_id": getattr(tool, "id", None),
            "status": "completed",
            "arguments": raw_args,
            "result": _bounded_json_value(tool_result, max_result_chars),
        }

    def _execute_full_access_registered_tool_call(
        self,
        run: WorkflowRun,
        call: dict[str, Any],
        *,
        index: int,
        step: WorkflowStep | None,
        runtime_policy: dict[str, Any] | None,
        max_result_chars: int,
    ) -> dict[str, Any]:
        call_id = str(call.get("call_id") or f"call_{index + 1}")
        target = str(call.get("tool_id") or call.get("function_name") or call.get("tool_name") or "").strip()
        base_result: dict[str, Any] = {
            "call_id": call_id,
            "index": index,
            "tool_id": target,
        }
        if not target:
            return {
                **base_result,
                "status": "invalid_request",
                "error": "Either tool_view_id or tool_id/function_name is required.",
            }
        if not _runtime_policy_is_full_access(runtime_policy):
            return {
                **base_result,
                "status": "access_denied",
                "error": "Registered tool batch calls without a code-callable tool_view_id require full_access runtime policy.",
            }

        tool = self.addon_manager.get_tool_record(target) if hasattr(self.addon_manager, "get_tool_record") else None
        if not tool:
            return {**base_result, "status": "invalid_request", "error": f"Tool '{target}' is not registered."}

        function_name = _tool_function_name_from_record(tool)
        expected_name = str(call.get("function_name") or call.get("tool_name") or "").strip()
        if call.get("tool_id") and expected_name and expected_name not in {function_name, getattr(tool, "id", None), getattr(tool, "legacy_name", None)}:
            return {
                **base_result,
                "tool_id": getattr(tool, "id", None),
                "function_name": function_name,
                "status": "invalid_request",
                "error": f"Requested function_name '{expected_name}' does not match tool '{getattr(tool, 'id', target)}'.",
            }

        scope = ExecutionScope.from_dict((run.metadata_json or {}).get("execution_scope"))
        scope_check = scope.check_child_request(tool_ids=[getattr(tool, "id", target)])
        if not scope_check.allowed:
            error = scope_check.to_error()
            error["allowed_tool_ids"] = list(scope.tool_ids)
            return {**base_result, **error}

        raw_args = dict(call.get("arguments") or {})
        approval_id = raw_args.pop("_approval_id", None)
        resource_reference_ids = raw_args.pop("_resource_reference_ids", None)
        tool_result = self.addon_manager.execute_capability(
            function_name,
            raw_args,
            self.tool_config,
            requester_user_id=self.user_id,
            runtime_policy=runtime_policy,
            approval_id=approval_id,
            resource_reference_ids=resource_reference_ids,
            audit_log_source_id="runs.code_tool_batch",
            log_source_ids=("runs.code_tool_batch",),
            workflow_run_id=run.id,
            workflow_step_id=step.id if step else None,
        )
        if isinstance(tool_result, dict) and tool_result.get("status") in {
            "access_denied",
            "addon_inactive",
            "blocked",
            "invalid_tool_call",
            "scope_denied",
            "tool_execution_failed",
            "waiting_for_approval",
        }:
            return {
                **base_result,
                "function_name": function_name,
                "tool_id": getattr(tool, "id", None),
                "status": tool_result.get("status"),
                "error": tool_result.get("error") or tool_result.get("message"),
                "approval_id": tool_result.get("approval_id"),
                "resource_reference_ids": tool_result.get("resource_reference_ids") or [],
            }
        if inspect.isgenerator(tool_result):
            tool_result = next(
                (
                    item
                    for item in tool_result
                    if isinstance(item, dict) and item.get("type") in {"finish", "error"}
                ),
                {"status": "success", "message": "Streaming operation completed."},
            )

        return {
            **base_result,
            "function_name": function_name,
            "tool_id": getattr(tool, "id", None),
            "status": "completed",
            "arguments": raw_args,
            "result": _bounded_json_value(tool_result, max_result_chars),
            "access_path": "full_access_registered_tool",
        }

    def add_artifact_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        artifact = self._require_artifact(payload.get("artifact_id"))
        version = ArtifactStore.create_artifact_version(
            artifact.id,
            job_id=payload.get("job_id"),
            workflow_run_id=artifact.workflow_run_id,
            step_id=payload.get("step_id"),
            source_addon_id=payload.get("source_addon_id"),
            created_by_user_id=self.user_id,
            artifact_type=payload.get("artifact_type") or payload.get("type"),
            uri=payload.get("uri"),
            path=payload.get("path"),
            remote_reference=payload.get("remote_reference"),
            mime_type=payload.get("mime_type"),
            content_hash=payload.get("content_hash"),
            size_bytes=payload.get("size_bytes"),
            label=payload.get("label"),
            source_ids=payload.get("source_ids"),
            evidence_ids=payload.get("evidence_ids"),
            metadata=payload.get("metadata"),
            change_summary=payload.get("change_summary"),
            make_current=payload.get("make_current") is not False,
            reset_review_status=payload.get("reset_review_status") is not False,
        )
        updated_artifact = db.session.get(RuntimeArtifact, artifact.id)
        return {"version": version.to_dict(), "artifact": updated_artifact.to_dict()}

    def update_artifact_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        artifact = self._require_artifact(payload.get("artifact_id"))
        review_status = payload.get("review_status") or payload.get("status")
        if not review_status:
            raise ValueError("review_status is required.")
        updated = ArtifactStore.update_artifact_review_status(
            artifact.id,
            review_status,
            reviewed_by_user_id=self.user_id,
            review_note=payload.get("review_note") or payload.get("note"),
        )
        return {"artifact": updated.to_dict()}

    def add_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self._require_run(payload.get("run_id"))
        source = self._require_source(payload.get("source_id"), run.id)
        evidence = SourceLedger.record_evidence(
            workflow_run_id=run.id,
            source_id=source.id,
            step_id=payload.get("step_id"),
            artifact_id=payload.get("artifact_id"),
            claim=payload.get("claim"),
            quote=payload.get("quote"),
            citation=payload.get("citation"),
            relevance_score=payload.get("relevance_score"),
            novelty_score=payload.get("novelty_score"),
            used_in_artifact=bool(payload.get("used_in_artifact")),
            created_by_user_id=self.user_id,
            metadata=payload.get("metadata"),
        )
        return {"evidence": evidence.to_dict(), "run": db.session.get(WorkflowRun, run.id).to_dict()}

    def _checkpoint_from_run(self, run: WorkflowRun, checkpoint_id: str) -> dict[str, Any]:
        for checkpoint in run.to_dict().get("runtime_checkpoints") or []:
            if str(checkpoint.get("id")) == str(checkpoint_id):
                return checkpoint
        return {"id": checkpoint_id, "status": "unknown"}

    def _steps_for_run(self, run_id: str) -> list[WorkflowStep]:
        return WorkflowStep.query.filter_by(workflow_run_id=run_id).order_by(WorkflowStep.created_at.asc()).all()

    def _artifacts_for_run(self, run_id: str) -> list[RuntimeArtifact]:
        return RuntimeArtifact.query.filter_by(workflow_run_id=run_id).order_by(RuntimeArtifact.created_at.asc()).all()

    def _artifact_tool_response(self, *, run: WorkflowRun, artifact: RuntimeArtifact) -> dict[str, Any]:
        artifact_payload = artifact.to_dict()
        run_payload = run.to_dict()
        artifact_id = str(artifact_payload.get("id") or "").strip()
        result = {"artifact": artifact_payload, "run": run_payload}
        if artifact_id:
            result["open_canvas_artifact_id"] = artifact_id
            result["_ui_render"] = self._artifact_ui_render(artifact_payload)
        return result

    def _artifact_ui_render(self, artifact_payload: dict[str, Any]) -> str:
        artifact_id = str(artifact_payload.get("id") or "").strip()
        label = str(artifact_payload.get("label") or "Artifact").strip() or "Artifact"
        open_href = f"#artifact-{artifact_id}" if artifact_id else "#"
        download_url = str(artifact_payload.get("download_url") or "").strip()
        parts = [
            "<div>",
            f"<p><strong>{label}</strong> is ready.</p>",
            f"<p><a class=\"download-link\" href=\"{open_href}\" title=\"Open artifact in canvas\">Open In Canvas</a></p>",
        ]
        if download_url:
            parts.append(
                f"<p><a class=\"download-link\" href=\"{download_url}\" target=\"_blank\" title=\"Download artifact\">Download</a></p>"
            )
        parts.append("</div>")
        return "".join(parts)

    def _maybe_complete_ad_hoc_run(self, run_id: str, *, auto_complete: bool = True) -> WorkflowRun:
        run = db.session.get(WorkflowRun, run_id)
        if not run or not auto_complete:
            return run
        if run.kind != "ad_hoc":
            return run
        if run.status in {"completed", "failed", "cancelled"}:
            return run

        steps = self._steps_for_run(run.id)
        if not steps:
            return set_workflow_run_status(run.id, "completed")

        statuses = {step.status for step in steps}
        if "failed" in statuses:
            return run
        if statuses.intersection({"planned", "ready", "queued", "running", "waiting_for_approval", "waiting_for_review", "paused"}):
            return run
        if statuses and statuses.issubset({"completed", "skipped", "cancelled"}):
            return set_workflow_run_status(run.id, "completed")
        return run

    def _run_working_memory(self, run: WorkflowRun) -> dict[str, Any]:
        state = run.state_json if isinstance(run.state_json, dict) else {}
        memory = state.get("working_memory") if isinstance(state, dict) else {}
        return memory if isinstance(memory, dict) else {}

    def _collection_service(self, run: WorkflowRun) -> RuntimeCollectionService:
        return RuntimeCollectionService(
            run=run,
            owner_user_id=self.user_id,
            workflow_step_id=self.enforced_step_id,
        )

    def _record_collection_event(
        self,
        run: WorkflowRun,
        kind: str,
        collection_id: str,
        payload: dict[str, Any],
        *,
        status: str = "completed",
    ) -> None:
        packet = make_agent_event(
            kind=kind,
            phase="run",
            status=status,
            title=kind.replace(".", " ").replace("_", " ").title(),
            summary=str(
                payload.get("error")
                or payload.get("message")
                or status
            )[:500],
            subject_type="runtime_collection",
            subject_id=collection_id,
            payload={
                **payload,
                "workflow_run_id": run.id,
                "workflow_step_id": self.enforced_step_id,
                "storage": "rows_v1",
            },
        )
        append_agent_event_to_metadata_attr(run, packet, limit=300)
        if self.enforced_step_id:
            step = db.session.get(WorkflowStep, self.enforced_step_id)
            if step and step.workflow_run_id == run.id:
                append_agent_event_to_metadata_attr(step, packet, limit=300)
        db.session.commit()

    def _require_run(self, run_id: Any) -> WorkflowRun:
        if not run_id:
            raise ValueError("run_id is required.")
        if self.enforced_run_id and str(run_id) != self.enforced_run_id:
            raise PermissionError("Run access is restricted to the current workflow-step context.")
        record = db.session.get(WorkflowRun, str(run_id))
        if not record:
            raise KeyError(f"Workflow run '{run_id}' not found.")
        if self.user_id is not None and not visible_to_user(record, self.user_id, self.is_admin):
            raise PermissionError("Access denied.")
        return record

    def _require_runtime_profile(self, run: WorkflowRun, runtime_profile_id: str) -> None:
        execution_scope = ExecutionScope.from_dict((run.metadata_json or {}).get("execution_scope"))
        if execution_scope.is_autonomous:
            return
        if runtime_profile_id in set(execution_scope.runtime_profile_ids):
            return
        raise PermissionError(json.dumps({
            "status": "blocked",
            "error": "Execution scope does not allow the requested runtime profile.",
            "blocked_runtime_profile_ids": [runtime_profile_id],
            "allowed_runtime_profile_ids": list(execution_scope.runtime_profile_ids),
        }, sort_keys=True))

    def _trigger_orchestrator(self) -> None:
        if not has_app_context():
            return
        try:
            from iarf_assistant.core.workflow_executor import (
                poll_workflow_orchestrator_once,
            )

            poll_workflow_orchestrator_once(current_app._get_current_object())
        except Exception:
            current_app.logger.exception("Failed to trigger workflow orchestrator.")

    def _require_step(self, step_id: Any) -> WorkflowStep:
        if not step_id:
            raise ValueError("step_id is required.")
        if self.enforced_step_id and str(step_id) != self.enforced_step_id:
            raise PermissionError("Step access is restricted to the current workflow-step context.")
        record = db.session.get(WorkflowStep, str(step_id))
        if not record:
            raise KeyError(f"Workflow step '{step_id}' not found.")
        if self.enforced_run_id and str(record.workflow_run_id) != self.enforced_run_id:
            raise PermissionError("Step access is restricted to the current workflow-step run.")
        self._require_run(record.workflow_run_id)
        return record

    def _require_source(self, source_id: Any, run_id: str) -> RuntimeSourceRecord:
        if not source_id:
            raise ValueError("source_id is required.")
        record = db.session.get(RuntimeSourceRecord, str(source_id))
        if not record:
            raise KeyError(f"Runtime source '{source_id}' not found.")
        if record.workflow_run_id != run_id:
            raise ValueError("source_id does not belong to this run.")
        return record

    def _require_template(self, template_id: Any) -> WorkflowTemplate:
        if not template_id:
            raise ValueError("template_id is required.")
        record = db.session.get(WorkflowTemplate, str(template_id))
        if not record:
            raise KeyError(f"Workflow template '{template_id}' not found.")
        if self.user_id is None or self.is_admin:
            return record
        owner_id = int(record.owner_id) if record.owner_id is not None else None
        user_id = int(self.user_id)
        if owner_id == user_id:
            return record
        if owner_id is None and bool(record.is_active):
            return record
        raise PermissionError("Access denied.")

    def _materialize_compiled_template_steps(self, run: WorkflowRun, compiled_template) -> None:
        execution_scope = ExecutionScope.from_dict((run.metadata_json or {}).get("execution_scope"))
        run_metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
        for index, step in enumerate(compiled_template.steps, start=1):
            metadata = step.get("metadata") if isinstance(step.get("metadata"), dict) else {}
            if "execution_scope" not in metadata:
                metadata = {**metadata, "execution_scope": execution_scope.to_dict()}
            if "runtime_policy" not in metadata and isinstance(run_metadata.get("runtime_policy"), dict):
                metadata = {**metadata, "runtime_policy": run_metadata["runtime_policy"]}
            if "max_tool_calls" not in metadata and run_metadata.get("max_tool_calls") is not None:
                metadata = {**metadata, "max_tool_calls": run_metadata.get("max_tool_calls")}
            scope_check = execution_scope.check_child_request(
                capability_ids=step.get("capability_ids"),
                skill_ids=step.get("skill_ids"),
                tool_ids=step.get("tool_ids"),
                tool_view_ids=step.get("tool_view_ids"),
                resource_reference_ids=step.get("resource_reference_ids"),
            )
            if not scope_check.allowed:
                error = scope_check.to_error()
                error["step_key"] = step.get("key")
                error["step_title"] = step.get("title")
                raise PermissionError(json.dumps(error, sort_keys=True))
            create_workflow_step(
                workflow_run_id=run.id,
                step_type=step["type"],
                title=step["title"],
                step_key=step["key"],
                status="planned",
                sequence=step.get("sequence") if step.get("sequence") is not None else index,
                depends_on_step_ids=step.get("depends_on"),
                assigned_model_role=step.get("assigned_model_role"),
                created_by_user_id=self.user_id,
                instructions=step.get("instructions"),
                capability_ids=scope_check.capability_ids,
                skill_ids=scope_check.skill_ids,
                tool_ids=scope_check.tool_ids,
                tool_view_ids=scope_check.tool_view_ids,
                provider_requirements=step.get("provider_requirements"),
                resource_reference_ids=scope_check.resource_reference_ids,
                input_schema=step.get("input_schema"),
                output_schema=step.get("output_schema"),
                input_payload=step.get("input"),
                metadata=metadata,
                commit=False,
            )
        db.session.commit()

    def _require_artifact(self, artifact_id: Any) -> RuntimeArtifact:
        if not artifact_id:
            raise ValueError("artifact_id is required.")
        record = db.session.get(RuntimeArtifact, str(artifact_id))
        if not record:
            raise KeyError(f"Runtime artifact '{artifact_id}' not found.")
        if self.enforced_run_id and str(record.workflow_run_id) != self.enforced_run_id:
            raise PermissionError("Artifact access is restricted to the current workflow-step run.")
        if self.user_id is not None and not visible_to_user(record, self.user_id, self.is_admin):
            raise PermissionError("Access denied.")
        return record

    def _validated_chat_session_id(self, chat_session_id: Any) -> str | None:
        if not chat_session_id:
            return None
        record = db.session.get(ChatSession, str(chat_session_id))
        if not record:
            raise KeyError(f"Chat session '{chat_session_id}' not found.")
        if self.user_id is not None and int(record.user_id) != int(self.user_id) and not self.is_admin:
            raise PermissionError("Access denied.")
        return record.id


def execute_run_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    user_id: int | None,
    is_admin: bool = False,
    addon_manager: Any = None,
    tool_config: dict[str, Any] | None = None,
    chat_session_id: str | None = None,
    enforced_run_id: str | None = None,
    enforced_step_id: str | None = None,
) -> dict[str, Any]:
    try:
        return RunOrchestrator(
            user_id=user_id,
            is_admin=is_admin,
            addon_manager=addon_manager,
            tool_config=tool_config,
            chat_session_id=chat_session_id,
            enforced_run_id=enforced_run_id,
            enforced_step_id=enforced_step_id,
        ).execute_tool(name, arguments)
    except KeyError as exc:
        db.session.rollback()
        return {"error": str(exc), "status": "not_found"}
    except PermissionError as exc:
        db.session.rollback()
        return {"error": str(exc), "status": "access_denied"}
    except ValueError as exc:
        db.session.rollback()
        return {"error": str(exc), "status": "invalid_request"}


def _clamped_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _clamped_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _coerce_process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _truncate_text_with_flag(value: Any, limit: int) -> tuple[str, bool]:
    text = _coerce_process_text(value)
    limit = max(1, int(limit or 1))
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _bounded_json_value(value: Any, max_chars: int) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = json.dumps(str(value), ensure_ascii=False)
    if len(text) <= max_chars:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return {
        "truncated": True,
        "preview": text[:max_chars],
        "original_chars": len(text),
    }


def _tool_function_name_from_record(tool: Any) -> str:
    function = (getattr(tool, "function_schema", {}) or {}).get("function") or {}
    if function.get("name"):
        return function["name"]
    legacy_name = getattr(tool, "legacy_name", None)
    if legacy_name:
        return legacy_name
    return str(getattr(tool, "id", "")).replace(".", "_")


def _runtime_policy_is_full_access(runtime_policy: dict[str, Any] | None) -> bool:
    if not isinstance(runtime_policy, dict):
        return False
    return normalize_policy_profile(
        runtime_policy.get("profile")
        or runtime_policy.get("approval_profile")
        or runtime_policy.get("mode")
        or runtime_policy.get("policy_profile"),
    ) == FULL_ACCESS


def _utc_iso() -> str:
    return utc_iso()


def _string_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if isinstance(value, (tuple, set)):
        value = list(value)
    if not isinstance(value, list):
        value = [value]
    result = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _safe_int_value(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_compiled_json_schema(value: dict[str, Any]) -> dict[str, Any]:
    def normalize(node: Any) -> Any:
        if isinstance(node, list):
            return [normalize(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {
            key: normalize(item)
            for key, item in node.items()
        }
        schema_type = result.get("type")
        if isinstance(schema_type, str):
            result["type"] = schema_type.strip().lower()
        elif isinstance(schema_type, list):
            result["type"] = [
                item.strip().lower() if isinstance(item, str) else item
                for item in schema_type
            ]
        return result

    return normalize(value)


def _safe_step_key(value: Any) -> str:
    text = str(value or f"step_{uuid.uuid4().hex[:12]}").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text).strip("_")
    return text[:180] or f"step_{uuid.uuid4().hex[:12]}"


def _compiled_delegation_items(
    payload: dict[str, Any],
    delegation: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_items: list[Any] = []
    delegation_items = delegation.get("items")
    if isinstance(delegation_items, list):
        raw_items.extend(delegation_items)

    resources = payload.get("resources")
    if isinstance(resources, list):
        for resource_index, resource in enumerate(resources, start=1):
            if isinstance(resource, str):
                raw_items.append({
                    "id": f"resource_{resource_index:04d}",
                    "uri": resource,
                    "title": resource,
                })
                continue
            if not isinstance(resource, dict):
                continue
            nested_items = resource.get("items")
            if not isinstance(nested_items, list):
                raw_items.append(dict(resource))
                continue
            resource_metadata = {
                key: value
                for key, value in resource.items()
                if key not in {"items", "text", "content", "body"}
            }
            inherited_refs = _string_values(
                resource.get("resource_reference_ids")
                or resource.get("resource_reference_id")
            )
            for item_index, item in enumerate(nested_items, start=1):
                if isinstance(item, str):
                    item_payload: dict[str, Any] = {
                        "id": f"resource_{resource_index:04d}_item_{item_index:04d}",
                        "text": item,
                    }
                elif isinstance(item, dict):
                    item_payload = dict(item)
                else:
                    continue
                metadata = (
                    dict(item_payload.get("metadata"))
                    if isinstance(item_payload.get("metadata"), dict)
                    else {}
                )
                metadata.setdefault("parent_resource", resource_metadata)
                item_payload["metadata"] = metadata
                item_payload["resource_reference_ids"] = _string_values([
                    *inherited_refs,
                    *_string_values(item_payload.get("resource_reference_ids")),
                    *(
                        [item_payload.get("resource_reference_id")]
                        if item_payload.get("resource_reference_id")
                        else []
                    ),
                ])
                for key in ("provider", "source_type"):
                    if resource.get(key) and not item_payload.get(key):
                        item_payload[key] = resource[key]
                raw_items.append(item_payload)

    normalized_raw: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, str):
            normalized_raw.append({
                "id": f"item_{index:04d}",
                "text": item,
            })
        elif isinstance(item, dict):
            normalized_raw.append(dict(item))
    return normalized_raw


def _prepare_compiled_delegation_items(
    raw_items: list[dict[str, Any]],
    *,
    max_input_tokens: int,
    reserved_output_tokens: int,
    chars_per_token: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    now = _utc_iso()
    deduplicated: list[dict[str, Any]] = []
    seen_identities: set[str] = set()
    for raw_item in raw_items:
        normalized = _normalize_collection_item(raw_item, now=now)
        identity = str(normalized.get("identity") or normalized.get("fingerprint") or "")
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        deduplicated.append(normalized)

    usable_tokens = max(1, int(max_input_tokens) - int(reserved_output_tokens))
    max_chars = max(1, int(usable_tokens * float(chars_per_token)))
    work_items: list[dict[str, Any]] = []
    split_parent_count = 0
    chunk_count = 0
    unresolved_oversized_count = 0
    for parent in deduplicated:
        text = _collection_item_inline_text(parent)
        estimated_tokens = _estimate_collection_item_tokens(
            parent,
            chars_per_token=chars_per_token,
            include_annotations=False,
        )
        chunks = _split_text_for_budget(text, max_chars=max_chars) if text and len(text) > max_chars else [text]
        if len(chunks) <= 1:
            item = {
                **parent,
                "parent_item_id": parent["id"],
                "chunk_index": 1,
                "chunk_count": 1,
                "estimated_tokens": estimated_tokens,
            }
            if estimated_tokens > usable_tokens and not text:
                metadata = dict(item.get("metadata") or {})
                metadata["oversized_unresolved"] = True
                metadata["usable_input_tokens"] = usable_tokens
                item["metadata"] = metadata
                unresolved_oversized_count += 1
            work_items.append(item)
            continue

        split_parent_count += 1
        chunk_count += len(chunks)
        parent_id = str(parent["id"])
        for index, chunk_text in enumerate(chunks, start=1):
            chunk_identity = f"{parent.get('identity')}#chunk:{index:04d}/{len(chunks):04d}"
            chunk_id = f"{parent_id}_chunk_{index:04d}"
            content_ref = dict(parent.get("content_ref") or {})
            content_ref.update({
                "text": chunk_text,
                "parent_item_id": parent_id,
                "parent_fingerprint": parent.get("fingerprint"),
                "chunk_index": index,
                "chunk_count": len(chunks),
            })
            metadata = dict(parent.get("metadata") or {})
            metadata.update({
                "parent_item_id": parent_id,
                "parent_fingerprint": parent.get("fingerprint"),
                "chunk_index": index,
                "chunk_count": len(chunks),
            })
            work_items.append({
                **parent,
                "id": chunk_id,
                "identity": chunk_identity,
                "fingerprint": hashlib.sha256(
                    f"{parent.get('fingerprint')}:{index}:{chunk_text}".encode("utf-8")
                ).hexdigest(),
                "parent_item_id": parent_id,
                "chunk_index": index,
                "chunk_count": len(chunks),
                "title": (
                    f"{parent.get('title') or parent_id} (part {index}/{len(chunks)})"
                )[:255],
                "content_ref": content_ref,
                "metadata": metadata,
                "estimated_tokens": max(
                    1,
                    int((len(chunk_text) + chars_per_token - 1) / chars_per_token),
                ),
            })

    return work_items, {
        "source_item_count": len(raw_items),
        "deduplicated_item_count": len(deduplicated),
        "duplicate_count": max(0, len(raw_items) - len(deduplicated)),
        "split_parent_count": split_parent_count,
        "chunk_count": chunk_count,
        "unresolved_oversized_count": unresolved_oversized_count,
    }


def _collection_item_inline_text(item: dict[str, Any]) -> str:
    content_ref = item.get("content_ref") if isinstance(item.get("content_ref"), dict) else {}
    for key in ("text", "content", "body"):
        value = content_ref.get(key)
        if isinstance(value, str) and value:
            return value
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    for key in ("text", "content", "body"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _split_text_for_budget(text: str, *, max_chars: int) -> list[str]:
    source = str(text or "")
    if not source:
        return [""]
    limit = max(1, int(max_chars or 1))
    chunks: list[str] = []
    offset = 0
    while offset < len(source):
        upper = min(len(source), offset + limit)
        if upper < len(source):
            minimum_break = offset + max(1, int(limit * 0.6))
            newline = source.rfind("\n", minimum_break, upper)
            whitespace = source.rfind(" ", minimum_break, upper)
            boundary = max(newline, whitespace)
            if boundary > offset:
                upper = boundary + 1
        chunk = source[offset:upper]
        if not chunk:
            upper = min(len(source), offset + limit)
            chunk = source[offset:upper]
        chunks.append(chunk)
        offset = upper
    return chunks


def _delegation_worker_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "id",
            "identity",
            "fingerprint",
            "parent_item_id",
            "chunk_index",
            "chunk_count",
            "source_id",
            "provider",
            "external_id",
            "uri",
            "canonical_uri",
            "title",
            "content_hash",
            "content_ref",
            "resource_reference_ids",
            "estimated_tokens",
            "metadata",
        )
        if item.get(key) not in (None, "", [], {})
    }


def _compiled_delegation_worker_instructions(
    *,
    objective: str,
    item: dict[str, Any],
    exploratory: bool,
    output_schema: dict[str, Any],
) -> str:
    mode_instruction = (
        "This is an exploratory worker. Search directly with the selected read-only toolkit; "
        "do not wait for a central ingestion step."
        if exploratory
        else (
            "Process only the packaged item/chunk in the step input. Use its stable content/resource "
            "references and selected toolkit directly; do not ask the chief to read or resend the corpus."
        )
    )
    schema_text = json.dumps(output_schema, ensure_ascii=True, sort_keys=True)
    if len(schema_text) > 5000:
        schema_text = schema_text[:5000] + "... [schema truncated in instructions]"
    return (
        f"Objective: {objective}\n\n"
        f"{mode_instruction}\n"
        "Preserve the item id, parent/source identity, and evidence refs in your result. "
        "If the item cannot be processed, return a structured failure explanation rather than silently "
        "substituting another item. Tool calls that are independent and read-only may be issued together.\n\n"
        "Return only JSON matching this output schema:\n"
        f"{schema_text}"
    )


def _safe_collection_id(value: Any) -> str:
    text = str(value or f"collection-{uuid.uuid4()}").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text).strip("_")
    return text[:96] or f"collection-{uuid.uuid4()}"


def _collection_item_identity(raw_item: dict[str, Any]) -> str:
    if raw_item.get("identity"):
        return f"identity:{str(raw_item['identity']).strip()}"
    provider = str(raw_item.get("provider") or "").strip()
    external_id = str(raw_item.get("external_id") or "").strip()
    if provider and external_id:
        return f"provider_external:{provider}:{external_id}"
    source_id = str(raw_item.get("source_id") or "").strip()
    if provider and source_id:
        return f"provider_source:{provider}:{source_id}"
    for key in (
        "canonical_uri",
        "canonical_url",
        "path",
        "file_path",
        "uri",
        "content_hash",
        "source_id",
        "fingerprint",
        "id",
    ):
        value = raw_item.get(key)
        if value:
            return f"{key}:{str(value).strip()}"
    digest = hashlib.sha1(
        json.dumps(raw_item, sort_keys=True, default=str).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:16]
    return f"hash:{digest}"


def _collection_item_id(raw_item: dict[str, Any], identity: str) -> str:
    if raw_item.get("id"):
        return str(raw_item["id"]).strip()
    digest = hashlib.sha1(
        identity.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:16]
    return f"item_{digest}"


def _normalize_collection_item(raw_item: dict[str, Any], *, now: str) -> dict[str, Any]:
    identity = _collection_item_identity(raw_item)
    item_id = _collection_item_id(raw_item, identity)
    metadata = dict(raw_item.get("metadata")) if isinstance(raw_item.get("metadata"), dict) else {}
    content_ref = dict(raw_item.get("content_ref")) if isinstance(raw_item.get("content_ref"), dict) else {}
    for key in ("text", "content", "body", "excerpt", "summary"):
        value = raw_item.get(key)
        if isinstance(value, str) and value and "text" not in content_ref:
            content_ref["text"] = value
            content_ref["inline_field"] = key
            break
    resource_reference_ids = _string_values(
        raw_item.get("resource_reference_ids")
        or raw_item.get("resource_reference_id")
        or metadata.get("resource_reference_ids")
    )
    if resource_reference_ids:
        metadata["resource_reference_ids"] = resource_reference_ids
    fingerprint = str(raw_item.get("fingerprint") or "").strip()
    if not fingerprint:
        fingerprint_payload = {
            "identity": identity,
            "content_hash": raw_item.get("content_hash"),
            "text": content_ref.get("text"),
            "uri": raw_item.get("canonical_uri") or raw_item.get("uri"),
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
    return {
        "id": item_id,
        "identity": identity,
        "fingerprint": fingerprint,
        "parent_item_id": str(raw_item.get("parent_item_id") or item_id),
        "chunk_index": _safe_int_value(raw_item.get("chunk_index")),
        "chunk_count": _safe_int_value(raw_item.get("chunk_count")),
        "source_id": str(raw_item.get("source_id") or "").strip() or None,
        "provider": str(raw_item.get("provider") or "").strip() or None,
        "external_id": str(raw_item.get("external_id") or "").strip() or None,
        "uri": str(raw_item.get("uri") or "").strip() or None,
        "canonical_uri": str(raw_item.get("canonical_uri") or "").strip() or None,
        "title": str(raw_item.get("title") or "").strip() or None,
        "content_hash": str(raw_item.get("content_hash") or "").strip() or None,
        "content_ref": content_ref,
        "resource_reference_ids": resource_reference_ids,
        "estimated_tokens": _safe_int_value(raw_item.get("estimated_tokens")),
        "metadata": metadata,
        "status": str(raw_item.get("status") or "pending").lower(),
        "annotations": raw_item.get("annotations") if isinstance(raw_item.get("annotations"), list) else [],
        "claim_count": int(raw_item.get("claim_count") or 0),
        "retry_count": int(raw_item.get("retry_count") or 0),
        "created_at": str(raw_item.get("created_at") or now),
        "updated_at": now,
    }


def _first_positive_int(*values: Any) -> int | None:
    for value in values:
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _estimate_collection_item_tokens(
    item: dict[str, Any],
    *,
    chars_per_token: float,
    include_annotations: bool,
) -> int:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    content_ref = item.get("content_ref") if isinstance(item.get("content_ref"), dict) else {}
    explicit_tokens = _first_positive_int(
        item.get("estimated_tokens"),
        item.get("token_count"),
        item.get("input_tokens"),
        metadata.get("estimated_tokens"),
        metadata.get("token_count"),
        metadata.get("input_tokens"),
        content_ref.get("estimated_tokens"),
        content_ref.get("token_count"),
        content_ref.get("input_tokens"),
    )
    if explicit_tokens:
        return explicit_tokens

    explicit_chars = _first_positive_int(
        item.get("estimated_chars"),
        item.get("char_count"),
        item.get("size_chars"),
        metadata.get("estimated_chars"),
        metadata.get("char_count"),
        metadata.get("size_chars"),
        content_ref.get("estimated_chars"),
        content_ref.get("char_count"),
        content_ref.get("size_chars"),
    )
    if explicit_chars:
        return max(1, int((explicit_chars + chars_per_token - 1) / chars_per_token))

    text_parts: list[str] = []
    for key in ("title", "uri", "canonical_uri", "source_id", "provider", "external_id", "content_hash"):
        value = item.get(key)
        if value:
            text_parts.append(str(value))
    for key in ("text", "text_preview", "content_preview", "excerpt", "summary", "description", "body"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            text_parts.append(value)
    for key in ("text", "preview", "path", "artifact_id", "locator", "uri"):
        value = content_ref.get(key)
        if isinstance(value, str) and value:
            text_parts.append(value)
    if include_annotations:
        annotations = item.get("annotations") if isinstance(item.get("annotations"), list) else []
        if annotations:
            text_parts.append(json.dumps(annotations, ensure_ascii=False, default=str))
    char_count = sum(len(part) for part in text_parts)
    return max(1, int((char_count + chars_per_token - 1) / chars_per_token))


def _scratch_python_env() -> dict[str, str]:
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    set_product_environment(
        env,
        "CODE_RUNTIME",
        "python.scratch",
        include_legacy=True,
    )
    for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _safe_artifact_filename(raw_name: Any) -> str:
    name = " ".join(str(raw_name or "artifact.md").split())
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name[:120] or "artifact.md"


def _unique_artifact_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for index in range(2, 1000):
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise ValueError("Could not allocate a unique artifact filename.")
