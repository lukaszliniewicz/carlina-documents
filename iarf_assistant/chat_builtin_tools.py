import base64
import html
import json
import mimetypes
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy import update as sa_update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.attributes import flag_modified

from carlina.core.runtime_policy import (
    MAX_TOOL_CALL_ITERATIONS,
    clamp_tool_call_iterations,
)
from carlina.core.utc import utc_iso
from iarf_assistant.chat_constants import SOURCE_COVERAGE_LEDGER_ROLE
from iarf_assistant.chat_execution_helpers import get_memory_lock
from iarf_assistant.core.execution_scope import ExecutionScope, string_list
from iarf_assistant.core.memory_defaults import (
    DEFAULT_MEMORY_CATEGORY_DEFINITIONS as DEFAULT_MEMORY_CATEGORY_DEFINITIONS,
    MEMORY_RECALL_INCLUDE_TIER2_DEFAULT as MEMORY_RECALL_INCLUDE_TIER2_DEFAULT,
    MEMORY_TIER1_LIMIT_ORG as MEMORY_TIER1_LIMIT_ORG,
    MEMORY_TIER1_LIMIT_USER as MEMORY_TIER1_LIMIT_USER,
    get_default_memory_category_definitions as get_default_memory_category_definitions,
)
from iarf_assistant.core.runtime_state import create_scope_expansion_request
from iarf_assistant.core.upload_paths import (
    is_upload_reference,
    resolve_upload_reference,
)
from iarf_assistant.database_utils import commit_with_retry
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    ChatMessage,
    ChatSession,
    DataContextCollection,
    OrgMemory,
    Setting,
    User,
    UserAlert,
    UserMemory,
)

SEND_USER_ALERT_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "send_user_alert",
        "description": "Send an actionable alert to the user's Notification Hub. Use this when a background process or sub-agent requires human attention asynchronously.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "A short, descriptive title for the alert."},
                "message": {"type": "string", "description": "The detailed message or reason for the alert."},
                "action_url": {"type": "string", "description": "Optional relative URL for the user to click (e.g., /approvals or /workflow-runs)."},
            },
            "required": ["title", "message"],
        },
    },
}


REQUEST_USER_CHOICE_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "request_user_choice",
        "description": (
            "Pause the current turn for one short, persistent in-chat interaction. Use this only when "
            "progress truly requires a user choice, review decision, or short structured value."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "interaction_type": {
                    "type": "string",
                    "enum": ["choice_question", "review_decision", "structured_input"],
                    "description": "The compact interaction shape. Defaults to choice_question.",
                },
                "question": {
                    "type": "string",
                    "description": "The exact short question to show the user.",
                },
                "choices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "Short button label."},
                            "value": {"type": "string", "description": "Stable answer value sent back to the chat."},
                            "description": {"type": "string", "description": "Optional one-line explanation."},
                        },
                        "required": ["label"],
                    },
                    "description": (
                        "Two to six mutually exclusive choices. Required for choice_question; "
                        "review_decision defaults to Approve, Request changes, and Reject."
                    ),
                },
                "reason": {"type": "string", "description": "Brief reason this question is needed."},
                "allow_free_text": {"type": "boolean", "description": "Whether the user may answer manually."},
                "input_label": {"type": "string", "description": "Short label for structured_input."},
                "input_placeholder": {"type": "string", "description": "Optional input hint."},
                "input_type": {
                    "type": "string",
                    "enum": ["text", "number", "date", "email"],
                    "description": "Browser input type for structured_input.",
                },
                "required": {"type": "boolean", "description": "Whether structured_input requires a value."},
                "preview": {
                    "type": "object",
                    "description": "Optional compact preview metadata for a review decision.",
                    "additionalProperties": True,
                },
            },
            "required": ["question"],
        },
    },
}


REQUEST_SELF_NUDGE_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "request_self_nudge",
        "description": (
            "Schedule a durable chat/session self-nudge that creates a time checkpoint and re-enters "
            "the assistant worker loop when it becomes due. "
            "Use this for timers like 'nudge me in 60 seconds to inspect worker progress' or "
            "'check quality after a short wait'. The timer and eventual review are visible in the run; "
            "this is not a user approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why this nudge is useful.",
                },
                "delay_seconds": {
                    "type": "integer",
                    "description": "How many seconds from now the nudge should become due. Preferred for relative timers.",
                },
                "due_at": {
                    "type": "string",
                    "description": "Optional ISO-8601 due time. Use only when the user or task requires an absolute time.",
                },
                "state_request": {
                    "type": "string",
                    "description": "What compact state the assistant should inspect when the nudge fires.",
                },
                "checkpoint_type": {
                    "type": "string",
                    "description": "Optional label such as progress_review, quality_sample, stall_check, budget_check, or custom.",
                },
            },
            "required": ["reason"],
        },
    },
}


RAG_SEARCH_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "rag_search",
        "description": (
            "Legacy vector-only private knowledge search. Prefer "
            "knowledge_query, which supports exact filters, lexical search, "
            "full-source reading, coverage, and versioned semantic indexes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {"type": "integer"},
                "collection_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["query"],
        },
    },
}

KNOWLEDGE_QUERY_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "knowledge_query",
        "description": (
            "Query canonical private knowledge with explicit strategy and "
            "stable provenance. Use structured for dates/senders/labels, "
            "lexical/BM25 for quoted exact phrases, semantic for ordinary "
            "paraphrase/conceptual questions, and intent-weighted hybrid for "
            "identifiers, corrections, supersession, or chronology. Use full "
            "to read explicit source_ids completely, coverage for exhaustive "
            "source processing, or programmatic for deterministic counts. "
            "auto applies these rules and reports any semantic-to-lexical "
            "fallback. Request only useful fields; full content is allowed "
            "when the task needs it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search text. May be empty for purely structured or "
                        "programmatic queries."
                    ),
                },
                "strategy": {
                    "type": "string",
                    "enum": [
                        "auto",
                        "structured",
                        "lexical",
                        "semantic",
                        "hybrid",
                        "full",
                        "coverage",
                        "programmatic",
                    ],
                    "description": (
                        "Retrieval method. auto uses deterministic routing; "
                        "choose explicitly when the intent is known."
                    ),
                },
                "collection_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional collection scope.",
                },
                "source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Stable canonical source IDs. Required for full; "
                        "exactly one is required for coverage."
                    ),
                },
                "filters": {
                    "type": "object",
                    "properties": {
                        "date_from": {
                            "type": "string",
                            "description": "Inclusive ISO-8601 lower bound.",
                        },
                        "date_to": {
                            "type": "string",
                            "description": "Inclusive ISO-8601 upper bound.",
                        },
                        "after": {"type": "string", "description": "ISO-8601 lower bound."},
                        "before": {"type": "string", "description": "ISO-8601 upper bound."},
                        "sender": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "recipient": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "subject": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "label": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "state": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "source_type": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "external_id": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "unit_type": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "additionalProperties": False,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum result units or full sources.",
                },
                "include_content": {
                    "type": "boolean",
                    "description": (
                        "Include complete matching unit content rather than "
                        "snippets. This does not truncate canonical sources."
                    ),
                },
                "snippet_chars": {
                    "type": "integer",
                    "minimum": 80,
                    "maximum": 20000,
                },
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "rank",
                            "score",
                            "component_scores",
                            "matched_fields",
                            "source_id",
                            "source_type",
                            "external_id",
                            "title",
                            "source_version_id",
                            "version_number",
                            "source_content_hash",
                            "unit_id",
                            "unit_external_id",
                            "unit_ordinal",
                            "unit_type",
                            "unit_anchor",
                            "unit_content_hash",
                            "occurred_at",
                            "metadata",
                            "snippet",
                            "content",
                        ],
                    },
                    "description": (
                        "Optional unit-result field projection. Stable source, "
                        "version, unit, and content-hash provenance is retained."
                    ),
                },
                "order_by": {
                    "type": "string",
                    "enum": [
                        "relevance",
                        "occurred_at_asc",
                        "occurred_at_desc",
                        "source_order",
                    ],
                },
                "program": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["count", "group_count"],
                        },
                        "field": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    },
}

LIST_KNOWLEDGE_COLLECTIONS_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "list_knowledge_collections",
        "description": (
            "List canonical private knowledge collections, saved views, item "
            "counts, and active index generations."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

SESSION_MEMORY_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "update_session_memory",
        "description": "Manage a persistent JSON Key-Value store for your task state, facts, or intermediate summaries across turns.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["set", "delete", "clear"], "description": "The operation to perform."},
                "key": {"type": "string", "description": "The dictionary key to set or delete (e.g., 'US_News', 'Draft_v1')."},
                "value": {"type": "string", "description": "The string value or JSON string to store (ignored for delete/clear)."},
            },
            "required": ["action"],
        },
    },
}

# Backward-compatible export name used across existing modules/tests.
WORKING_MEMORY_TOOL_DEF = SESSION_MEMORY_TOOL_DEF

MEMORY_CATEGORIES = [entry["key"] for entry in DEFAULT_MEMORY_CATEGORY_DEFINITIONS]
MEMORY_CATEGORY_LABELS = {entry["key"]: entry["label"] for entry in DEFAULT_MEMORY_CATEGORY_DEFINITIONS}
MEMORY_TIERS = ["tier1", "tier2"]
MEMORY_IMPORTANCE_LEVELS = ["low", "normal", "high"]
DEFAULT_MEMORY_CATEGORY = "other"
DEFAULT_MEMORY_TIER = "tier1"
DEFAULT_MEMORY_IMPORTANCE = "normal"

_CATEGORY_ALIASES = {
    "likes": "likes_dislikes",
    "dislikes": "likes_dislikes",
    "hobbies": "pastimes",
    "hobby": "pastimes",
    "health": "health_accessibility",
    "accessibility": "health_accessibility",
    "work": "professional",
    "job": "professional",
    "career": "professional",
    "org": "organization_policy",
    "organization": "organization_policy",
    "policy": "organization_policy",
    "policies": "organization_policy",
}

VIEW_ATTACHMENT_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "view_attachment",
        "description": "View the visual content of an attached image or PDF file.",
        "parameters": {
            "type": "object",
            "properties": {"filepath": {"type": "string"}},
            "required": ["filepath"],
        },
    },
}

MEMORIZE_FACT_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "memorize_fact",
        "description": (
            "Store a long-term fact or preference for the organization or user. "
            "Include category and tier when possible. Store only stable, useful facts; avoid transient conversation details."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["user", "org"], "description": "Scope of the memory. 'user' for personal preferences, 'org' for global/company facts."},
                "fact": {"type": "string", "description": "The explicit fact to memorize."},
                "category": {"type": "string", "description": "Optional category label used for retrieval and summarization. Snake_case recommended."},
                "tier": {"type": "string", "enum": MEMORY_TIERS, "description": "Optional tier. tier1 is hot memory; tier2 is archive/search-only memory."},
                "importance": {"type": "string", "enum": MEMORY_IMPORTANCE_LEVELS, "description": "Optional importance level. High-importance facts are less likely to be auto-demoted."},
            },
            "required": ["scope", "fact"],
        },
    },
}

RECALL_FACT_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "recall_fact",
        "description": "Search long-term memory for previously stored facts or preferences, with optional category and tier filters.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query or keyword."},
                "scope": {"type": "string", "enum": ["user", "org", "both"], "description": "Optional scope filter. Defaults to both."},
                "category": {"type": "string", "description": "Optional category filter."},
                "include_tier2": {"type": "boolean", "description": "Include tier2 archive memories in search results. Defaults to false."},
                "max_results": {"type": "integer", "description": "Optional max results (default 12, min 1, max 100)."},
            },
            "required": ["query"],
        },
    },
}


MANAGE_LONG_TERM_MEMORY_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "manage_long_term_memory",
        "description": (
            "Manage long-term user/org memory records. Supports listing, updating, deleting, deduplicating, "
            "category summaries, and tier policy rebalancing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "update", "delete", "dedupe", "summarize_categories", "retier"]},
                "scope": {"type": "string", "enum": ["user", "org"]},
                "memory_id": {"type": "integer", "description": "Required for update/delete."},
                "fact": {"type": "string", "description": "Required for update."},
                "query": {"type": "string", "description": "Optional filter query for list."},
                "limit": {"type": "integer", "description": "Optional list limit (default 50, max 200)."},
                "category": {"type": "string", "description": "Optional category filter for list/update."},
                "tier": {"type": "string", "enum": MEMORY_TIERS, "description": "Optional tier filter for list/update."},
                "include_tier2": {"type": "boolean", "description": "Include tier2 rows when listing without tier filter. Defaults to true."},
                "importance": {"type": "string", "enum": MEMORY_IMPORTANCE_LEVELS, "description": "Optional importance update for memory rows."},
                "tier_locked": {"type": "boolean", "description": "Optional lock that prevents auto-tiering from changing this row's tier."},
            },
            "required": ["action", "scope"],
        },
    },
}


DELEGATE_TASK_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Run a short-lived inline sub-agent without creating a durable workflow run. "
            "Use this only for self-contained, token-heavy reading, summarizing, extraction, or analysis "
            "that a fast/medium model can handle. Do not use it for external writes/deletes, approval-gated "
            "actions, long-running work, multi-step orchestration, user-visible artifacts, or anything that "
            "needs provenance/resumability. Ask the delegate to return structured source coverage whenever "
            "it inspects external sources, documents, emails, pages, files, posts, or tool results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Precise task for the delegated sub-agent."},
                "capability_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional capability IDs whose read-only tools should be exposed to the sub-agent.",
                },
                "model_role": {
                    "type": "string",
                    "enum": ["fast", "medium"],
                    "default": "fast",
                    "description": "Use fast by default; use medium for harder but still bounded subtasks.",
                },
                "max_tool_calls": {
                    "type": "integer",
                    "default": 8,
                    "description": "Safety cap for delegated tool calls. Keep this small.",
                },
                "parallel_tool_calls": {
                    "type": "integer",
                    "default": 4,
                    "description": "Maximum independent read-only tool calls to execute concurrently per worker turn.",
                },
                "coverage_purpose": {
                    "type": "string",
                    "description": (
                        "Optional short purpose label for the source coverage ledger, e.g. "
                        "'research X initial web scan' or 'email triage for contract issue'."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
}


REQUEST_SCOPE_EXPANSION_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "request_scope_expansion",
        "description": (
            "In Scoped Tools mode, ask the user to approve additional capabilities, skills, workflows, "
            "individual tools, narrowed tool views, resource grants, model profiles, or runtime profiles. "
            "Use this instead of trying to discover or use unselected capabilities."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Concrete reason the current selected scope is insufficient.",
                },
                "capability_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Known missing capability ids or common toolkit aliases, e.g. "
                        "filesystem, filesystem.toolkit, gmail, email.toolkit, google_drive, googledrive.toolkit."
                    ),
                },
                "skill_ids": {"type": "array", "items": {"type": "string"}},
                "workflow_ids": {"type": "array", "items": {"type": "string"}},
                "tool_ids": {"type": "array", "items": {"type": "string"}},
                "tool_view_ids": {"type": "array", "items": {"type": "string"}},
                "resource_reference_ids": {"type": "array", "items": {"type": "string"}},
                "runtime_profile_ids": {"type": "array", "items": {"type": "string"}},
                "model_profile_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["reason"],
        },
    },
}

COMMON_SCOPE_CAPABILITY_ALIASES = (
    "filesystem",
    "local files",
    "local folder",
    "local storage",
    "email",
    "gmail",
    "mailbox",
    "google drive",
    "google_drive",
    "gdrive",
    "googledrive",
    "drive",
    "wordpress",
    "wp",
    "web",
    "scraping",
    "documents",
    "document processor",
    "doc_processor",
    "images",
    "openverse",
)


REQUEST_TOOL_BUDGET_EXTENSION_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "request_tool_budget_extension",
        "description": (
            "Request a small bounded extension to the current chat turn's tool-call budget. "
            "Use this only after a tool-budget warning when more tool calls are essential and returning "
            "a final answer now would be materially incomplete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Concise reason the remaining tool turns are not enough.",
                },
                "requested_iterations": {
                    "type": "integer",
                    "default": 2,
                    "description": "Requested extra tool-call iterations. Keep this small.",
                },
                "next_actions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The specific remaining tool actions the extension will be used for.",
                },
            },
            "required": ["reason"],
        },
    },
}


SOURCE_COVERAGE_MEMORY_KEY = "_source_coverage"
SOURCE_COVERAGE_MAX_ITEMS = 400
SOURCE_COVERAGE_STRING_LIMIT = 1000
SOURCE_COVERAGE_SUMMARY_LIMIT = 1800
SOURCE_COVERAGE_STATUSES = {
    "discovered",
    "queued",
    "claimed",
    "inspected",
    "skipped",
    "failed",
}
AUTO_SOURCE_COVERAGE_TOOL_PREFIXES = (
    "email_",
    "googledrive_",
    "web_search_",
    "scraping_",
    "filesystem_",
    "wordpress_",
)
AUTO_SOURCE_COVERAGE_SKIP_TOOLS = {
    "source_coverage_upsert",
    "source_coverage_get",
    "context_read_result",
    "context_search_result",
    "context_manage_results",
    "update_session_memory",
    "memorize_fact",
    "recall_fact",
    "manage_long_term_memory",
}


SOURCE_COVERAGE_UPSERT_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "source_coverage_upsert",
        "description": (
            "Record or update chat-scoped source/item coverage. Use this after searches, reads, "
            "delegation, code batches, or worker results so the chief can avoid duplicate source "
            "inspection and preserve what was covered without keeping large payloads in context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Coverage records to upsert. Keep each summary concise.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_key": {"type": "string", "description": "Optional stable dedupe key. If omitted, the runtime derives one."},
                            "source_type": {"type": "string", "description": "web, email, gdrive, local_file, wp_post, document_page, image, tool_result, other, etc."},
                            "source_id": {"type": "string", "description": "Provider id, URL, file path, post id, message id, page id, or other stable id."},
                            "title": {"type": "string"},
                            "canonical_url": {"type": "string"},
                            "context_ref_id": {"type": "string", "description": "Retained tool-result ref if this coverage came from a compacted result."},
                            "status": {"type": "string", "enum": sorted(SOURCE_COVERAGE_STATUSES)},
                            "purpose": {"type": "string", "description": "Why this source was or will be inspected."},
                            "inspected_by": {"type": "string", "description": "chief, delegate, worker, code, tool, or a short worker id."},
                            "summary": {"type": "string", "description": "Concise finding, reason skipped, or work note."},
                            "confidence": {"type": "string", "description": "Optional confidence label or score."},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                            "metadata": {"type": "object", "description": "Small source-specific metadata only."},
                        },
                    },
                },
                "default_status": {"type": "string", "enum": sorted(SOURCE_COVERAGE_STATUSES), "description": "Status to use when an item omits status."},
                "reason": {"type": "string", "description": "Short reason for this coverage update."},
            },
            "required": ["items"],
        },
    },
}


SOURCE_COVERAGE_GET_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "source_coverage_get",
        "description": (
            "Query the chat-scoped source/item coverage ledger. Use before broad reading, delegation, "
            "or synthesis to see what has already been discovered, queued, inspected, skipped, or failed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source_keys": {"type": "array", "items": {"type": "string"}, "description": "Optional exact coverage keys to fetch."},
                "source_types": {"type": "array", "items": {"type": "string"}},
                "statuses": {"type": "array", "items": {"type": "string", "enum": sorted(SOURCE_COVERAGE_STATUSES)}},
                "purpose": {"type": "string", "description": "Optional purpose substring filter."},
                "query": {"type": "string", "description": "Optional case-insensitive text search across title, id, URL, and summary."},
                "limit": {"type": "integer", "default": 50, "description": "Maximum records to return. Clamped by the runtime."},
            },
        },
    },
}


CONTEXT_READ_RESULT_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "context_read_result",
        "description": (
            "Read a bounded, integrity-checked exact slice of durable context. "
            "Prefer the stable context_item_id emitted by archived/reference "
            "envelopes; legacy context_ref_id remains supported."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "context_item_id": {
                    "type": "string",
                    "description": (
                        "Stable Context Fabric item ID. This can dereference "
                        "active, archived, excluded, reference, or overflow items."
                    ),
                },
                "context_ref_id": {
                    "type": "string",
                    "description": (
                        "Legacy retained-result ref accepted when no "
                        "context_item_id is available."
                    ),
                },
                "start_offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Character offset to start reading from.",
                },
                "max_chars": {
                    "type": "integer",
                    "default": 8000,
                    "description": "Maximum characters to return. Clamped by the runtime; request larger slices only when needed.",
                },
            },
        },
    },
}


CONTEXT_SEARCH_RESULT_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "context_search_result",
        "description": (
            "Literal-search durable context and return bounded snippets, stable "
            "context/evidence IDs, lifecycle versions, and offsets. Supply "
            "context_item_ids or legacy refs to constrain the search; omit both "
            "to search the archived context for this chat."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "context_item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional stable context items to search, including "
                        "active or archived items."
                    ),
                },
                "context_item_id": {
                    "type": "string",
                    "description": "Single context-item alias.",
                },
                "context_ref_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Retained result refs to search. Preferred for one or more refs.",
                },
                "context_ref_id": {
                    "type": "string",
                    "description": "Single-ref alias accepted for one retained result.",
                },
                "query": {
                    "type": "string",
                    "description": "Literal text to search for. Keep it specific.",
                },
                "max_matches": {
                    "type": "integer",
                    "default": 10,
                    "description": "Maximum matches per retained result. Clamped by the runtime.",
                },
                "snippet_chars": {
                    "type": "integer",
                    "default": 320,
                    "description": "Approximate characters of surrounding context for each match.",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Applies to legacy retained-result refs. Durable archive "
                        "search is case-insensitive."
                    ),
                },
                "lifecycle_states": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["active", "archived", "excluded"],
                    },
                    "description": (
                        "Optional lifecycle scope. Defaults to archived when "
                        "searching without explicit IDs."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}


CONTEXT_MANAGE_RESULTS_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "context_manage_results",
        "description": (
            "Apply an optimistic, audited lifecycle/materialization transition "
            "to durable context. Raw evidence remains retained. Prefer stable "
            "context_item_ids and expected versions; legacy result refs and "
            "retention_state remain supported. In Manual mode, lossy projections "
            "require explicit user authorization."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "context_item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Stable Context Fabric item IDs to update.",
                },
                "context_item_id": {
                    "type": "string",
                    "description": "Single context-item alias.",
                },
                "context_ref_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Context result refs to update. Preferred for one or more refs.",
                },
                "context_ref_id": {
                    "type": "string",
                    "description": "Single-ref alias accepted for models that choose singular argument names.",
                },
                "retention_state": {
                    "type": "string",
                    "enum": [
                        "inline_keep",
                        "inline_this_turn",
                        "preview_ref",
                        "summary_ref",
                        "archive_only",
                        "pin_for_chief",
                    ],
                    "description": (
                        "Legacy compatibility field. Prefer action for new "
                        "Context Fabric items."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": [
                        "keep",
                        "restore",
                        "archive",
                        "exclude",
                        "pin",
                        "unpin",
                        "materialize_full",
                        "materialize_reference",
                        "materialize_overflow",
                        "summarize",
                        "excerpt",
                    ],
                    "description": (
                        "Versioned lifecycle/materialization action. Restore "
                        "reactivates as a reference; use materialize_full when "
                        "the exact body should enter the next provider payload."
                    ),
                },
                "expected_version": {
                    "type": "integer",
                    "description": (
                        "Expected lifecycle version for a single/all selected "
                        "items. A mismatch is returned as a conflict."
                    ),
                },
                "expected_versions": {
                    "type": "object",
                    "description": (
                        "Optional map from context_item_id to expected version."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "Optional compact summary to keep in model context when using summary_ref.",
                },
                "key_facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional durable facts or claims extracted from the result.",
                },
                "excerpt": {
                    "type": "string",
                    "description": "Durable exact excerpt for action=excerpt.",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason for the retention change.",
                },
            },
        },
    },
}


def handle_request_tool_budget_extension(
    arguments: dict[str, Any] | None,
    *,
    current_max_tool_calls: int,
    max_extension: int = 4,
    already_used: bool = False,
) -> dict[str, Any]:
    current_max_tool_calls = clamp_tool_call_iterations(current_max_tool_calls)
    if already_used:
        return {
            "status": "blocked",
            "policy": "single_chat_tool_budget_extension",
            "error": "A tool-budget extension has already been granted for this chat turn.",
            "current_max_tool_calls": current_max_tool_calls,
        }
    try:
        requested = int((arguments or {}).get("requested_iterations") or 2)
    except (TypeError, ValueError):
        requested = 2
    requested = max(1, requested)
    try:
        max_extension = max(0, int(max_extension))
    except (TypeError, ValueError):
        max_extension = 4
    if max_extension < 1:
        return {
            "status": "blocked",
            "policy": "tool_budget_extension_disabled",
            "error": "Tool-budget extensions are disabled by policy.",
            "requested_iterations": requested,
            "current_max_tool_calls": current_max_tool_calls,
        }
    remaining_headroom = MAX_TOOL_CALL_ITERATIONS - current_max_tool_calls
    if remaining_headroom < 1:
        return {
            "status": "blocked",
            "policy": "tool_budget_ceiling_reached",
            "error": "The server tool-call ceiling has already been reached.",
            "requested_iterations": requested,
            "current_max_tool_calls": current_max_tool_calls,
            "maximum_tool_calls": MAX_TOOL_CALL_ITERATIONS,
        }
    reason = str((arguments or {}).get("reason") or "").strip()
    if not reason:
        return {
            "status": "invalid_request",
            "error": "A concise reason is required to extend the tool-call budget.",
            "requested_iterations": requested,
            "current_max_tool_calls": current_max_tool_calls,
        }
    added = min(requested, max_extension, remaining_headroom)
    return {
        "status": "approved",
        "policy": "bounded_chat_tool_budget_extension",
        "requested_iterations": requested,
        "added_tool_call_iterations": added,
        "current_max_tool_calls": current_max_tool_calls,
        "new_max_tool_calls": current_max_tool_calls + added,
        "reason": reason,
        "next_actions": string_list((arguments or {}).get("next_actions")),
        "message": f"Tool-call budget extended by {added} iteration(s). Use them only for the stated remaining actions.",
    }


def _scope_expansion_payload(arguments: dict[str, Any] | None) -> dict[str, list[str]]:
    args = arguments if isinstance(arguments, dict) else {}
    return {
        "capability_ids": string_list(args.get("capability_ids") or args.get("capabilities")),
        "skill_ids": string_list(args.get("skill_ids") or args.get("skills")),
        "workflow_ids": string_list(args.get("workflow_ids") or args.get("workflows")),
        "tool_ids": string_list(args.get("tool_ids") or args.get("tools")),
        "tool_view_ids": string_list(args.get("tool_view_ids") or args.get("tool_views")),
        "resource_reference_ids": string_list(args.get("resource_reference_ids") or args.get("resources")),
        "runtime_profile_ids": string_list(args.get("runtime_profile_ids")),
        "model_profile_ids": string_list(args.get("model_profile_ids")),
    }


def _append_unique_scope_value(target: dict[str, list[str]], key: str, value: str) -> None:
    if not value:
        return
    bucket = target.setdefault(key, [])
    if value not in bucket:
        bucket.append(value)


def _compact_scope_normalization(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in report.items()
        if value
    }


def _infer_scope_capability_aliases_from_text(text: str | None) -> list[str]:
    normalized = " ".join(str(text or "").lower().replace("_", " ").replace("-", " ").split())
    if not normalized:
        return []
    inferred = []
    for alias in COMMON_SCOPE_CAPABILITY_ALIASES:
        alias_text = " ".join(str(alias).lower().replace("_", " ").replace("-", " ").split())
        if not alias_text:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(alias_text)}(?![a-z0-9])", normalized):
            inferred.append(alias)
    return string_list(inferred)


def normalize_scope_expansion_payload(
    requested_scope: dict[str, list[str]],
    current_scope: Any,
    *,
    capability_id_normalizer=None,
    inference_text: str | None = None,
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    scope = ExecutionScope.from_dict(current_scope)
    current = scope.to_dict()
    requested_scope = {
        key: string_list(value)
        for key, value in (requested_scope or {}).items()
    }
    normalized: dict[str, list[str]] = {key: [] for key in requested_scope.keys()}
    report: dict[str, Any] = {
        "canonicalized_scope_ids": {},
        "unresolved_scope_ids": {},
        "already_in_scope": {},
    }
    if not any(requested_scope.values()) and capability_id_normalizer is not None:
        inferred_aliases = _infer_scope_capability_aliases_from_text(inference_text)
        if inferred_aliases:
            requested_scope.setdefault("capability_ids", [])
            requested_scope["capability_ids"] = inferred_aliases
            normalized.setdefault("capability_ids", [])
            report["inferred_scope_ids"] = {"capability_ids": list(inferred_aliases)}

    for key, values in requested_scope.items():
        for raw_value in string_list(values):
            if raw_value in set(current.get(key) or []):
                report["already_in_scope"].setdefault(key, []).append(raw_value)
                continue
            resolved_values = [raw_value]
            if key == "capability_ids" and capability_id_normalizer is not None:
                try:
                    resolved_values = string_list(capability_id_normalizer([raw_value]))
                except Exception:
                    resolved_values = []
                if not resolved_values:
                    report["unresolved_scope_ids"].setdefault(key, []).append(raw_value)
                    continue
                if resolved_values != [raw_value]:
                    report["canonicalized_scope_ids"].setdefault(key, []).append({
                        "requested": raw_value,
                        "canonical": resolved_values,
                    })

            for resolved_value in resolved_values:
                if resolved_value in set(current.get(key) or []):
                    report["already_in_scope"].setdefault(key, []).append(resolved_value)
                    continue
                _append_unique_scope_value(normalized, key, resolved_value)

    return normalized, _compact_scope_normalization(report)


def handle_request_scope_expansion(
    arguments: dict[str, Any] | None,
    *,
    requester_user_id: int | None,
    current_scope: Any,
    chat_session_id: str | None = None,
    workflow_run_id: str | None = None,
    capability_id_normalizer=None,
) -> dict[str, Any]:
    scope = ExecutionScope.from_dict(current_scope)
    if scope.is_autonomous:
        return {
            "status": "not_needed",
            "message": "Autonomous discovery is enabled; request or load needed capabilities directly instead of asking for scope expansion.",
            "current_scope": scope.to_dict(),
        }
    if not scope.allow_scope_expansion_request:
        return {
            "status": "blocked",
            "error": "Scope expansion requests are disabled for this turn.",
            "current_scope": scope.to_dict(),
        }

    requested_scope_original = _scope_expansion_payload(arguments)
    requested_scope, scope_normalization = normalize_scope_expansion_payload(
        requested_scope_original,
        scope,
        capability_id_normalizer=capability_id_normalizer,
        inference_text=str((arguments or {}).get("reason") or ""),
    )
    if not any(requested_scope.values()):
        status = "not_needed" if scope_normalization.get("already_in_scope") else "invalid_request"
        return {
            "status": status,
            "error": (
                "The requested scope expansion did not include any recognized new scope ids."
                if status == "invalid_request"
                else None
            ),
            "message": (
                "All requested scope ids are already available in this Scoped Tools turn."
                if status == "not_needed"
                else "Ask for exact capability ids from the user or switch to Autonomous mode for discovery."
            ),
            "requested_scope": requested_scope,
            "requested_scope_original": requested_scope_original,
            "scope_normalization": scope_normalization,
            "current_scope": scope.to_dict(),
        }

    reason = str((arguments or {}).get("reason") or "").strip()
    approval = create_scope_expansion_request(
        requester_user_id=requester_user_id,
        current_scope=scope,
        requested_scope=requested_scope,
        requested_scope_original=requested_scope_original,
        scope_normalization=scope_normalization,
        reason=reason,
        chat_session_id=chat_session_id,
        workflow_run_id=workflow_run_id,
        log_source_ids=["chat.scope"],
    )
    escaped_reason = html.escape(reason or "No reason provided.")
    return {
        "status": "waiting_for_approval",
        "approval_id": approval.id,
        "subject_type": "scope_expansion",
        "approval_url": "/approvals",
        "requested_scope": requested_scope,
        "requested_scope_original": requested_scope_original,
        "scope_normalization": scope_normalization,
        "current_scope": scope.to_dict(),
        "reason": reason,
        "message": (
            "Scope expansion request created. The user can approve or reject it in chat, then continue "
            "with the expanded Scoped Tools selection."
        ),
        "_ui_render": (
            "<div class=\"runtime-approval-card\">"
            "<strong>Scope expansion requested</strong>"
            f"<p>{escaped_reason}</p>"
            f"<span>Approval {html.escape(approval.id)}</span>"
            "</div>"
        ),
    }


def _choice_text(value, limit=160):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def handle_request_user_choice(arguments: dict | None) -> dict:
    args = arguments if isinstance(arguments, dict) else {}
    question = _choice_text(args.get("question"), 500)
    if not question:
        return {"status": "error", "error": "question is required."}

    interaction_type = str(args.get("interaction_type") or "choice_question").strip().lower().replace("-", "_")
    interaction_type = {
        "choice": "choice_question",
        "review": "review_decision",
        "preview": "review_decision",
        "text": "structured_input",
        "input": "structured_input",
    }.get(interaction_type, interaction_type)
    if interaction_type not in {"choice_question", "review_decision", "structured_input"}:
        return {"status": "error", "error": f"Unsupported interaction_type: {interaction_type}"}

    raw_choices = args.get("choices") or []
    if interaction_type == "review_decision" and not raw_choices:
        raw_choices = [
            {"label": "Approve", "value": "approved"},
            {"label": "Request changes", "value": "changes_requested"},
            {"label": "Reject", "value": "rejected"},
        ]

    choices = []
    for index, raw_choice in enumerate(raw_choices, start=1):
        if isinstance(raw_choice, str):
            label = _choice_text(raw_choice, 80)
            value = label
            description = ""
        elif isinstance(raw_choice, dict):
            label = _choice_text(raw_choice.get("label"), 80)
            value = _choice_text(raw_choice.get("value") or label, 120)
            description = _choice_text(raw_choice.get("description"), 180)
        else:
            continue
        if not label:
            continue
        choices.append({
            "id": f"choice_{index}",
            "label": label,
            "value": value,
            "description": description,
        })
        if len(choices) >= 6:
            break

    if interaction_type != "structured_input" and len(choices) < 2:
        return {"status": "error", "error": "At least two choices are required."}

    input_type = str(args.get("input_type") or "text").strip().lower()
    if input_type not in {"text", "number", "date", "email"}:
        input_type = "text"
    id_prefix = "user_choice" if interaction_type == "choice_question" else "user_interaction"
    question_id = f"{id_prefix}_{uuid.uuid4().hex[:12]}"
    reason = _choice_text(args.get("reason"), 240)
    return {
        "status": "waiting_for_user",
        "interaction_type": interaction_type,
        "question_id": question_id,
        "question": question,
        "choices": choices,
        "reason": reason,
        "allow_free_text": bool(args.get("allow_free_text")) or interaction_type == "structured_input",
        "input_label": _choice_text(args.get("input_label"), 80) or "Answer",
        "input_placeholder": _choice_text(args.get("input_placeholder"), 160),
        "input_type": input_type,
        "required": args.get("required", True) is not False,
        "preview": args.get("preview") if isinstance(args.get("preview"), dict) else {},
        "message": "Waiting for the user to choose an answer in chat.",
    }


def _parse_self_nudge_due_at(args: dict, now=None) -> tuple[datetime | None, int | None, str | None]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    due_at_raw = str(args.get("due_at") or "").strip()
    if due_at_raw:
        try:
            due_at = datetime.fromisoformat(due_at_raw.replace("Z", "+00:00"))
        except ValueError:
            return None, None, "due_at must be ISO-8601."
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        else:
            due_at = due_at.astimezone(timezone.utc)
        return due_at, max(0, int((due_at - current).total_seconds())), None

    try:
        delay_seconds = int(args.get("delay_seconds") or 60)
    except (TypeError, ValueError):
        delay_seconds = 60
    delay_seconds = max(1, min(delay_seconds, 365 * 24 * 60 * 60))
    return current + timedelta(seconds=delay_seconds), delay_seconds, None


def handle_request_self_nudge(
    arguments: dict | None,
    *,
    chat_session_id: str | None = None,
    requester_user_id: int | None = None,
    execution_scope: dict | None = None,
    runtime_policy: dict | None = None,
    now=None,
) -> dict:
    args = arguments if isinstance(arguments, dict) else {}
    reason = _choice_text(args.get("reason"), 500)
    if not reason:
        return {"status": "error", "error": "reason is required."}

    due_at, delay_seconds, error = _parse_self_nudge_due_at(args, now=now)
    if error:
        return {"status": "error", "error": error}
    if due_at is None or delay_seconds is None:
        return {"status": "error", "error": "Unable to schedule self-nudge."}

    created_at = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)

    monitor_id = f"self_nudge_{uuid.uuid4().hex[:12]}"
    payload = {
        "schema_version": 2,
        "monitor_id": monitor_id,
        "monitor_type": "self_nudge",
        "status": "pending",
        "chat_session_id": chat_session_id,
        "requester_user_id": requester_user_id,
        "due_at": due_at.isoformat(),
        "due_in_seconds": delay_seconds,
        "reason": reason,
        "state_request": _choice_text(args.get("state_request"), 500),
        "checkpoint_type": _choice_text(args.get("checkpoint_type") or "progress_review", 80),
        "created_at": created_at.isoformat(),
    }

    if not chat_session_id:
        return {
            "status": "error",
            "error": "chat_session_id is required for a functioning self-nudge.",
        }

    try:
        from iarf_assistant.core.runtime_state import (
            create_runtime_checkpoint,
            create_workflow_run,
        )

        run_metadata = {
            "chat_self_nudge": {
                "monitor_id": monitor_id,
                "reason": reason,
                "state_request": payload["state_request"],
                "checkpoint_type": payload["checkpoint_type"],
            },
            "max_concurrency": 1,
        }
        if isinstance(execution_scope, dict):
            run_metadata["execution_scope"] = execution_scope
        if isinstance(runtime_policy, dict):
            run_metadata["runtime_policy"] = runtime_policy
            if runtime_policy.get("max_tool_calls") is not None:
                run_metadata["max_tool_calls"] = runtime_policy.get("max_tool_calls")

        wake_run = create_workflow_run(
            title=f"Self-nudge: {reason[:180]}",
            kind="ad_hoc",
            status="paused",
            input_payload={
                "monitor_id": monitor_id,
                "reason": reason,
                "state_request": payload["state_request"],
            },
            understanding={
                "goal": "Re-enter the agent loop at the requested time and inspect compact current state.",
            },
            plan={
                "mechanism": "time_self_nudge",
                "due_at": due_at.isoformat(),
                "action": "create_review_step",
            },
            created_by_user_id=requester_user_id,
            chat_session_id=chat_session_id,
            metadata=run_metadata,
            commit=False,
        )
        db.session.flush()
        checkpoint = create_runtime_checkpoint(
            workflow_run_id=wake_run.id,
            title="Time-based self-nudge",
            kind="time_self_nudge",
            action="create_review_step",
            reason=reason,
            instruction=(
                f"{payload['state_request'] or 'Inspect compact run/chat state and report whether follow-up is needed.'} "
                "When the check is complete, call send_user_alert with a concise result and the run link."
            ),
            due_at=due_at.isoformat(),
            created_by_user_id=requester_user_id,
            metadata={
                "monitor_id": monitor_id,
                "chat_session_id": chat_session_id,
                "checkpoint_type": payload["checkpoint_type"],
            },
            commit=False,
        )
        payload["workflow_run_id"] = wake_run.id
        payload["checkpoint_id"] = checkpoint["id"]
        db.session.add(ChatMessage(
            session_id=chat_session_id,
            message={"role": "runtime_monitor", "monitor": payload},
        ))
        commit_with_retry(db.session)
    except Exception as exc:
        db.session.rollback()
        return {"status": "error", "error": f"Database error scheduling self-nudge: {exc}"}

    return {
        **payload,
        "status": "scheduled",
        "monitor_status": payload["status"],
        "interaction_type": "self_nudge",
        "message": (
            "Self-nudge scheduled as a durable paused run. The workflow poller will create and dispatch "
            "a review worker when the timer becomes due."
        ),
    }



def handle_rag_search(rag_service, settings, query, target_uid, local_rag_ids, agent_num_results=None, agent_collection_ids=None):
    if agent_collection_ids:
        collection_ids = agent_collection_ids
    elif local_rag_ids and "all" not in local_rag_ids:
        collection_ids = local_rag_ids
    else:
        collection_ids = [
            collection.id
            for collection in DataContextCollection.query.filter_by(user_id=target_uid, collection_mode='vector').all()
        ]
    if not collection_ids:
        return {"error": "No collections found."}
    top_k = max(1, min(20, int(agent_num_results))) if agent_num_results is not None else int(settings.get('rag_top_k', 5))
    chunks = rag_service.search(query_text=query, collection_ids=collection_ids, top_k=top_k, user_id=target_uid)
    return "\n\n---\n\n".join(chunks) if chunks else "No relevant information found."


def _knowledge_collection_scope(
    *,
    target_uid: int,
    local_collection_ids,
    requested_collection_ids,
    source_ids,
):
    owned_ids = {
        collection.id
        for collection in DataContextCollection.query.filter_by(
            user_id=target_uid,
        ).all()
    }
    raw_local = list(local_collection_ids or [])
    if raw_local and "all" not in {
        str(item).strip().lower()
        for item in raw_local
    }:
        try:
            allowed_ids = {int(item) for item in raw_local}
        except (TypeError, ValueError):
            return None, {
                "status": "error",
                "error": "The selected knowledge collection scope is invalid.",
            }
        allowed_ids &= owned_ids
    else:
        allowed_ids = owned_ids

    if requested_collection_ids:
        try:
            requested_ids = {
                int(item)
                for item in requested_collection_ids
            }
        except (TypeError, ValueError):
            return None, {
                "status": "error",
                "error": "collection_ids must contain integers.",
            }
        disallowed = sorted(requested_ids - allowed_ids)
        if disallowed:
            return None, {
                "status": "error",
                "error": "One or more requested collections are outside the current scope.",
                "disallowed_collection_ids": disallowed,
            }
        return sorted(requested_ids), None

    if raw_local and "all" not in {
        str(item).strip().lower()
        for item in raw_local
    }:
        return sorted(allowed_ids), None
    # Explicit stable source IDs may address an owned source that is not in a
    # collection. Do not introduce an accidental collection-membership filter.
    if source_ids:
        return None, None
    return sorted(allowed_ids), None


def handle_knowledge_query(
    rag_service,
    tool_args,
    target_uid,
    local_collection_ids=None,
):
    args = dict(tool_args or {})
    source_ids = args.get("source_ids") or []
    collection_ids, scope_error = _knowledge_collection_scope(
        target_uid=target_uid,
        local_collection_ids=local_collection_ids,
        requested_collection_ids=args.get("collection_ids"),
        source_ids=source_ids,
    )
    if scope_error:
        return scope_error
    try:
        result = rag_service.query_knowledge(
            user_id=target_uid,
            query=args.get("query") or "",
            strategy=args.get("strategy") or "auto",
            collection_ids=collection_ids,
            source_ids=source_ids,
            filters=args.get("filters") or {},
            limit=args.get("limit") or 10,
            include_content=bool(args.get("include_content", False)),
            snippet_chars=args.get("snippet_chars") or 1200,
            fields=args.get("fields"),
            order_by=args.get("order_by") or "relevance",
            program=args.get("program"),
        )
    except Exception as exc:
        from iarf_assistant.core.knowledge_fabric import KnowledgeFabricError

        if not isinstance(exc, KnowledgeFabricError):
            raise
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return {"status": "ok", **result}


def handle_list_knowledge_collections(target_uid):
    from iarf_assistant.models import (
        KnowledgeIndexGeneration,
        KnowledgeSavedView,
        KnowledgeSource,
    )

    collections = (
        DataContextCollection.query
        .filter_by(user_id=target_uid)
        .order_by(DataContextCollection.name.asc())
        .all()
    )
    collection_payloads = []
    for collection in collections:
        payload = collection.to_dict()
        payload["saved_views"] = [
            {
                "id": view.id,
                "name": view.name,
                "provider": view.provider,
                "query": view.query_json or {},
            }
            for view in KnowledgeSavedView.query.filter_by(
                user_id=target_uid,
                collection_id=collection.id,
            ).order_by(KnowledgeSavedView.name.asc()).all()
        ]
        payload["active_index_generations"] = [
            {
                "id": generation.id,
                "projection_type": generation.projection_type,
                "entry_count": generation.entry_count,
                "signature_hash": generation.signature_hash,
                "embedding_model": (
                    generation.signature_json or {}
                ).get("embedding_model"),
                "dimensions": (
                    generation.signature_json or {}
                ).get("dimensions"),
            }
            for generation in KnowledgeIndexGeneration.query.filter_by(
                user_id=target_uid,
                collection_id=collection.id,
                status="active",
            ).all()
        ]
        collection_payloads.append(payload)
    return {
        "status": "ok",
        "collections": collection_payloads,
        "collection_count": len(collection_payloads),
        "canonical_source_count": KnowledgeSource.query.filter_by(
            user_id=target_uid,
        ).count(),
    }


def _normalize_fact_text(text: Any) -> str:
    return " ".join(str(text or "").split()).strip().lower()


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def is_missing_memory_tables_error(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return "no such table: user_memory" in message or "no such table: org_memory" in message


def ensure_memory_tables_exist() -> None:
    bind = db.session.get_bind()
    UserMemory.__table__.create(bind=bind, checkfirst=True)
    OrgMemory.__table__.create(bind=bind, checkfirst=True)


def _slugify_memory_category_key(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = raw.replace("-", "_").replace(" ", "_")
    raw = re.sub(r"[^a-z0-9_]+", "", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    if not raw:
        return ""
    if raw[0].isdigit():
        raw = f"cat_{raw}"
    return raw[:40]


def _default_memory_category_label(key: str) -> str:
    if key in MEMORY_CATEGORY_LABELS:
        return MEMORY_CATEGORY_LABELS[key]
    return key.replace("_", " ").strip().title()[:60] or "Category"


def sanitize_memory_category_definitions(raw_definitions: Any) -> list[dict[str, str]]:
    if not isinstance(raw_definitions, list):
        return get_default_memory_category_definitions()

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw_definitions:
        if isinstance(entry, dict):
            key = _slugify_memory_category_key(entry.get("key"))
            label_raw = entry.get("label")
        elif isinstance(entry, str):
            key = _slugify_memory_category_key(entry)
            label_raw = entry
        else:
            continue

        if not key or key in seen:
            continue

        label = str(label_raw or "").strip()[:60] or _default_memory_category_label(key)
        normalized.append({"key": key, "label": label})
        seen.add(key)

        if len(normalized) >= 30:
            break

    if not normalized:
        normalized = get_default_memory_category_definitions()
        seen = {entry["key"] for entry in normalized}

    if DEFAULT_MEMORY_CATEGORY not in seen:
        normalized.append(
            {
                "key": DEFAULT_MEMORY_CATEGORY,
                "label": _default_memory_category_label(DEFAULT_MEMORY_CATEGORY),
            }
        )
    return normalized


def _normalize_category(value: Any) -> str:
    if value is None:
        return DEFAULT_MEMORY_CATEGORY
    raw = str(value).strip().lower()
    if not raw:
        return DEFAULT_MEMORY_CATEGORY
    canonical = _CATEGORY_ALIASES.get(raw, raw)
    canonical_slug = _slugify_memory_category_key(canonical)
    return canonical_slug or DEFAULT_MEMORY_CATEGORY


def _normalize_tier(value: Any) -> str:
    if value is None:
        return DEFAULT_MEMORY_TIER
    raw = str(value).strip().lower()
    if not raw:
        return DEFAULT_MEMORY_TIER
    return raw if raw in MEMORY_TIERS else DEFAULT_MEMORY_TIER


def _normalize_importance(value: Any) -> str:
    if value is None:
        return DEFAULT_MEMORY_IMPORTANCE
    raw = str(value).strip().lower()
    if not raw:
        return DEFAULT_MEMORY_IMPORTANCE
    return raw if raw in MEMORY_IMPORTANCE_LEVELS else DEFAULT_MEMORY_IMPORTANCE


def _normalize_scope(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"user", "org", "both"}:
        return raw
    return "both"


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed))


def _setting_value(key: str) -> str | None:
    row = db.session.get(Setting, key)
    return row.value if row else None


def _memory_tier1_limit(scope: str) -> int:
    scope_normalized = str(scope or "").strip().lower()
    if scope_normalized == "org":
        raw = _setting_value("memory_tier1_limit_org")
        return _coerce_int(raw, MEMORY_TIER1_LIMIT_ORG, 10, 500)
    raw = _setting_value("memory_tier1_limit_user")
    return _coerce_int(raw, MEMORY_TIER1_LIMIT_USER, 10, 500)


def _memory_recall_include_tier2_default() -> bool:
    raw = _setting_value("memory_recall_include_tier2_default")
    return _parse_bool(raw, default=MEMORY_RECALL_INCLUDE_TIER2_DEFAULT)


def _coerce_memory_metadata(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, dict) else {}


def _extract_category(metadata: dict[str, Any]) -> str:
    return _normalize_category(metadata.get("category"))


def _extract_tier(metadata: dict[str, Any]) -> str:
    return _normalize_tier(metadata.get("tier"))


def _extract_importance(metadata: dict[str, Any]) -> str:
    return _normalize_importance(metadata.get("importance"))


def _extract_tier_locked(metadata: dict[str, Any]) -> bool:
    return _parse_bool(metadata.get("tier_locked"), default=False)


def _memory_timestamp(row: UserMemory | OrgMemory):
    return row.updated_at or row.created_at or datetime.min


def _set_memory_metadata(
    row: UserMemory | OrgMemory,
    *,
    category: Any = None,
    tier: Any = None,
    importance: Any = None,
    tier_locked: Any = None,
    source: Any = None,
) -> dict[str, Any]:
    metadata = _coerce_memory_metadata(row.json_metadata)
    metadata["category"] = _normalize_category(category if category is not None else metadata.get("category"))
    metadata["tier"] = _normalize_tier(tier if tier is not None else metadata.get("tier"))
    metadata["importance"] = _normalize_importance(importance if importance is not None else metadata.get("importance"))
    metadata["tier_locked"] = _extract_tier_locked(metadata) if tier_locked is None else bool(tier_locked)
    if source is not None:
        source_clean = _clean_optional_text(source)
        if source_clean:
            metadata["source"] = source_clean
    row.json_metadata = metadata
    return metadata


def _normalized_metadata_view(row: UserMemory | OrgMemory) -> dict[str, Any]:
    metadata = _coerce_memory_metadata(row.json_metadata)
    return {
        **metadata,
        "category": _extract_category(metadata),
        "tier": _extract_tier(metadata),
        "importance": _extract_importance(metadata),
        "tier_locked": _extract_tier_locked(metadata),
    }


def _apply_memory_tiering_policy(scope: str, target_uid: int) -> dict[str, int]:
    if scope == "user":
        rows = UserMemory.query.filter_by(user_id=target_uid).order_by(UserMemory.updated_at.desc()).all()
        limit = _memory_tier1_limit("user")
    elif scope == "org":
        rows = OrgMemory.query.order_by(OrgMemory.updated_at.desc()).all()
        limit = _memory_tier1_limit("org")
    else:
        return {"tier1_count": 0, "tier2_count": 0, "changed_count": 0}

    changed_count = 0
    locked_tier1_count = 0
    for row in rows:
        metadata = _coerce_memory_metadata(row.json_metadata)
        if _extract_tier_locked(metadata) and _extract_tier(metadata) == "tier1":
            locked_tier1_count += 1

    available_slots = max(0, limit - locked_tier1_count)
    protected_ids = {
        row.id
        for row in rows
        if _extract_importance(_coerce_memory_metadata(row.json_metadata)) == "high"
    }

    for row in rows:
        metadata = _coerce_memory_metadata(row.json_metadata)
        if _extract_tier_locked(metadata):
            if row.json_metadata != metadata:
                row.json_metadata = metadata
            continue

        should_be_tier1 = False
        if row.id in protected_ids:
            should_be_tier1 = True
        elif available_slots > 0:
            should_be_tier1 = True

        desired_tier = "tier1" if should_be_tier1 else "tier2"
        if should_be_tier1:
            available_slots = max(0, available_slots - 1)

        current_tier = _extract_tier(metadata)
        if current_tier != desired_tier:
            metadata["tier"] = desired_tier
            metadata["tier_reason"] = "auto_policy"
            row.json_metadata = metadata
            changed_count += 1
        elif row.json_metadata != metadata:
            row.json_metadata = metadata

    tier1_count = 0
    for row in rows:
        metadata = _coerce_memory_metadata(row.json_metadata)
        if _extract_tier(metadata) == "tier1":
            tier1_count += 1
    return {"tier1_count": tier1_count, "tier2_count": max(0, len(rows) - tier1_count), "changed_count": changed_count}


def _memory_scope_query(scope: str, target_uid: int):
    if scope == 'user':
        return UserMemory.query.filter_by(user_id=target_uid)
    if scope == 'org':
        return OrgMemory.query
    return None


def _can_manage_org_memory(target_uid: int) -> bool:
    user = db.session.get(User, target_uid)
    return bool(user and user.is_admin)


def _serialize_memory_record(scope: str, row: UserMemory | OrgMemory) -> dict[str, Any]:
    metadata = _normalized_metadata_view(row)
    return {
        "id": row.id,
        "scope": scope,
        "user_id": getattr(row, "user_id", None),
        "fact": row.fact,
        "category": _extract_category(metadata),
        "tier": _extract_tier(metadata),
        "importance": _extract_importance(metadata),
        "tier_locked": _extract_tier_locked(metadata),
        "metadata": metadata,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _raw_org_memory_cell(memory_id: int, column_name: str):
    if column_name not in {"fact", "json_metadata"}:
        raise ValueError("Unsupported org memory column.")
    row = db.session.execute(
        text(f"SELECT {column_name} FROM org_memory WHERE id = :id"),
        {"id": int(memory_id)},
    ).first()
    return row[0] if row else None


def _migrate_org_memory_to_system_encryption(target_uid: int) -> dict[str, Any]:
    """
    Best-effort lazy migration for legacy org-memory rows encrypted with user keys.
    Decryptable rows are re-saved using system encryption.
    """
    if not _can_manage_org_memory(target_uid):
        return {"migrated_count": 0, "skipped_ids": []}

    migrated_count = 0
    skipped_ids: list[int] = []
    for row in OrgMemory.query.all():
        raw_fact = _raw_org_memory_cell(row.id, "fact")
        raw_metadata = _raw_org_memory_cell(row.id, "json_metadata")
        legacy_encrypted = (
            (isinstance(raw_fact, str) and raw_fact.startswith("usr:"))
            or (isinstance(raw_metadata, str) and raw_metadata.startswith("usr:"))
        )
        if not legacy_encrypted:
            continue

        # If decryption failed, EncryptedType returns the raw ciphertext string.
        unreadable_fact = isinstance(row.fact, str) and row.fact.startswith("usr:")
        unreadable_metadata = isinstance(row.json_metadata, str) and row.json_metadata.startswith("usr:")
        if unreadable_fact or unreadable_metadata:
            skipped_ids.append(int(row.id))
            continue

        # Force a DB write so values are re-encrypted under the current system strategy.
        db.session.execute(
            sa_update(OrgMemory)
            .where(OrgMemory.id == row.id)
            .values(fact=row.fact, json_metadata=row.json_metadata)
        )
        migrated_count += 1

    if migrated_count:
        commit_with_retry(db.session)
    return {"migrated_count": migrated_count, "skipped_ids": skipped_ids}


def update_working_memory(action, key, value, target_sid):
    if not target_sid:
        return {"status": "error", "message": "No active session."}

    lock = get_memory_lock(target_sid)
    with lock:
        chat_session = db.session.get(ChatSession, target_sid)
        if not chat_session:
            return {"status": "error", "message": "Session not found."}
        try:
            memory_dict = json.loads(chat_session.working_memory) if chat_session.working_memory else {}
            if not isinstance(memory_dict, dict):
                memory_dict = {"_legacy": chat_session.working_memory}
        except json.JSONDecodeError:
            memory_dict = {"_legacy": chat_session.working_memory} if chat_session.working_memory else {}

        if action == "set":
            if not key:
                return {"status": "error", "message": "Error: 'key' is required for 'set'."}
            memory_dict[key] = value
            message = f"Key '{key}' set successfully."
        elif action == "delete":
            if not key:
                return {"status": "error", "message": "Error: 'key' is required for 'delete'."}
            if key in memory_dict:
                del memory_dict[key]
                message = f"Key '{key}' deleted successfully."
            else:
                message = f"Key '{key}' not found."
        elif action == "clear":
            memory_dict = {}
            message = "Memory cleared successfully."
        else:
            return {"status": "error", "message": f"Error: Unknown action '{action}'."}

        try:
            chat_session.working_memory = json.dumps(memory_dict, indent=2, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            return {"status": "error", "message": f"Error serializing memory value: {exc}"}
        try:
            commit_with_retry(db.session)
            return {"status": "ok", "message": message, "working_memory": memory_dict}
        except Exception as exc:
            return {"status": "error", "message": f"Database error updating memory: {exc}"}


def _utc_timestamp():
    return utc_iso()


def _coverage_text(value, limit=SOURCE_COVERAGE_STRING_LIMIT):
    text_value = str(value or "").strip()
    if not text_value:
        return ""
    if len(text_value) <= limit:
        return text_value
    return text_value[: max(0, limit - 16)].rstrip() + "... [truncated]"


def _coverage_status(value, default="discovered"):
    status = str(value or default or "discovered").strip().lower()
    aliases = {
        "read": "inspected",
        "checked": "inspected",
        "covered": "inspected",
        "seen": "inspected",
        "todo": "queued",
        "pending": "queued",
        "error": "failed",
    }
    status = aliases.get(status, status)
    return status if status in SOURCE_COVERAGE_STATUSES else "discovered"


def _coverage_metadata(value):
    if not isinstance(value, dict):
        return {}
    clean = {}
    for key, item in value.items():
        key_text = _coverage_text(key, 80)
        if not key_text:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            clean[key_text] = _coverage_text(item, 300) if isinstance(item, str) else item
        elif isinstance(item, (list, tuple)):
            clean[key_text] = [_coverage_text(entry, 220) for entry in item[:20]]
        elif isinstance(item, dict):
            clean[key_text] = {
                _coverage_text(nested_key, 80): _coverage_text(nested_value, 220)
                for nested_key, nested_value in list(item.items())[:20]
            }
    return clean


def _coverage_stable_key(item):
    raw_key = _coverage_text(item.get("source_key"), 300)
    if raw_key:
        return raw_key
    source_type = _coverage_text(item.get("source_type") or "other", 80).lower() or "other"
    source_id = (
        item.get("source_id")
        or item.get("canonical_url")
        or item.get("url")
        or item.get("path")
        or item.get("context_ref_id")
        or item.get("title")
    )
    source_id = _coverage_text(source_id, 500)
    if not source_id:
        source_id = f"generated:{uuid.uuid4().hex[:12]}"
    return f"{source_type}:{source_id}"


def _coverage_record_from_item(item, *, default_status="discovered", existing=None):
    if not isinstance(item, dict):
        return None
    now = _utc_timestamp()
    existing = existing if isinstance(existing, dict) else {}
    context_refs = string_list(existing.get("context_ref_ids"))
    context_ref = _coverage_text(item.get("context_ref_id"), 300)
    if context_ref and context_ref not in context_refs:
        context_refs.append(context_ref)

    evidence_refs = string_list(existing.get("evidence_refs"))
    for ref in string_list(item.get("evidence_refs")):
        if ref not in evidence_refs:
            evidence_refs.append(ref)

    source_type = _coverage_text(item.get("source_type") or existing.get("source_type") or "other", 80).lower() or "other"
    record = {
        "source_key": _coverage_stable_key(item),
        "source_type": source_type,
        "source_id": _coverage_text(item.get("source_id") or existing.get("source_id"), 500),
        "title": _coverage_text(item.get("title") or existing.get("title"), 300),
        "canonical_url": _coverage_text(item.get("canonical_url") or existing.get("canonical_url"), 700),
        "status": _coverage_status(item.get("status"), default=default_status or existing.get("status") or "discovered"),
        "purpose": _coverage_text(item.get("purpose") or existing.get("purpose"), 300),
        "inspected_by": _coverage_text(item.get("inspected_by") or existing.get("inspected_by"), 120),
        "summary": _coverage_text(item.get("summary") or existing.get("summary"), SOURCE_COVERAGE_SUMMARY_LIMIT),
        "confidence": _coverage_text(item.get("confidence") or existing.get("confidence"), 80),
        "context_ref_ids": context_refs[:20],
        "evidence_refs": evidence_refs[:50],
        "metadata": {**_coverage_metadata(existing.get("metadata")), **_coverage_metadata(item.get("metadata"))},
        "first_seen_at": existing.get("first_seen_at") or now,
        "updated_at": now,
    }
    return {key: value for key, value in record.items() if value not in ("", [], {})}


def _empty_coverage_ledger():
    return {
        "role": SOURCE_COVERAGE_LEDGER_ROLE,
        "schema_version": 1,
        "coverage": {
            "items": [],
            "updated_at": _utc_timestamp(),
        },
    }


def _coverage_ledger_row(target_sid):
    row = (
        ChatMessage.query
        .filter_by(session_id=target_sid)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    for candidate in row:
        message = candidate.message if isinstance(candidate.message, dict) else {}
        if message.get("role") == SOURCE_COVERAGE_LEDGER_ROLE:
            return candidate
    return None


def _coverage_ledger_payload(target_sid, *, create=False):
    row = _coverage_ledger_row(target_sid)
    if row is None:
        if not create:
            return _empty_coverage_ledger(), None
        row = ChatMessage(session_id=target_sid, message=_empty_coverage_ledger())
        db.session.add(row)
        db.session.flush()
    payload = row.message if isinstance(row.message, dict) else _empty_coverage_ledger()
    if payload.get("role") != SOURCE_COVERAGE_LEDGER_ROLE:
        payload = _empty_coverage_ledger()
    coverage = payload.setdefault("coverage", {})
    if not isinstance(coverage.get("items"), list):
        coverage["items"] = []
    return payload, row


def _coverage_counts(items):
    by_status = {}
    by_type = {}
    for item in items:
        status = item.get("status") or "unknown"
        source_type = item.get("source_type") or "other"
        by_status[status] = by_status.get(status, 0) + 1
        by_type[source_type] = by_type.get(source_type, 0) + 1
    return {"total": len(items), "by_status": by_status, "by_source_type": by_type}


def _compact_coverage_item(item):
    return {
        key: value
        for key, value in {
            "source_key": item.get("source_key"),
            "source_type": item.get("source_type"),
            "source_id": item.get("source_id"),
            "title": item.get("title"),
            "canonical_url": item.get("canonical_url"),
            "status": item.get("status"),
            "purpose": item.get("purpose"),
            "inspected_by": item.get("inspected_by"),
            "summary": _coverage_text(item.get("summary"), 700),
            "confidence": item.get("confidence"),
            "context_ref_ids": item.get("context_ref_ids") or [],
            "evidence_refs": item.get("evidence_refs") or [],
            "updated_at": item.get("updated_at"),
        }.items()
        if value not in ("", [], None)
    }


def upsert_source_coverage(arguments, target_sid):
    if not target_sid:
        return {"status": "error", "message": "No active session."}
    items = (arguments or {}).get("items")
    if not isinstance(items, list) or not items:
        return {"status": "error", "message": "items must be a non-empty array."}

    lock = get_memory_lock(f"{target_sid}:source_coverage")
    with lock:
        chat_session = db.session.get(ChatSession, target_sid)
        if not chat_session:
            return {"status": "error", "message": "Session not found."}
        payload, row = _coverage_ledger_payload(target_sid, create=True)
        coverage = payload.setdefault("coverage", {})
        current_items = [
            item for item in coverage.get("items", [])
            if isinstance(item, dict) and item.get("source_key")
        ]
        current_by_key = {item["source_key"]: item for item in current_items}
        default_status = _coverage_status((arguments or {}).get("default_status"), default="discovered")
        changed = []
        for raw_item in items[:100]:
            probe_key = _coverage_stable_key(raw_item) if isinstance(raw_item, dict) else ""
            normalized = _coverage_record_from_item(
                raw_item,
                default_status=default_status,
                existing=current_by_key.get(probe_key),
            )
            if not normalized:
                continue
            current_by_key[normalized["source_key"]] = normalized
            changed.append(_compact_coverage_item(normalized))

        sorted_items = sorted(
            current_by_key.values(),
            key=lambda item: item.get("updated_at") or "",
            reverse=True,
        )[:SOURCE_COVERAGE_MAX_ITEMS]
        coverage["items"] = sorted_items
        coverage["updated_at"] = _utc_timestamp()
        coverage["reason"] = _coverage_text((arguments or {}).get("reason"), 500)
        row.message = payload
        flag_modified(row, "message")
        try:
            db.session.flush()
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            return {"status": "error", "message": f"Database error updating source coverage: {exc}"}

    counts = _coverage_counts(sorted_items)
    return {
        "status": "ok",
        "updated_count": len(changed),
        "ledger_counts": counts,
        "items": changed[:50],
        "message": (
            "Source coverage updated. Query source_coverage_get before duplicate broad reads "
            "or before final synthesis."
        ),
    }


def get_source_coverage(arguments, target_sid):
    if not target_sid:
        return {"status": "error", "message": "No active session."}
    payload, _row = _coverage_ledger_payload(target_sid, create=False)
    items = [
        item for item in payload.get("coverage", {}).get("items", [])
        if isinstance(item, dict)
    ]
    args = arguments if isinstance(arguments, dict) else {}
    source_keys = set(string_list(args.get("source_keys")))
    source_types = {value.lower() for value in string_list(args.get("source_types"))}
    statuses = {_coverage_status(value) for value in string_list(args.get("statuses"))}
    purpose = str(args.get("purpose") or "").strip().lower()
    query = str(args.get("query") or "").strip().lower()
    try:
        limit = max(1, min(int(args.get("limit") or 50), 200))
    except (TypeError, ValueError):
        limit = 50

    def matches(item):
        if source_keys and item.get("source_key") not in source_keys:
            return False
        if source_types and str(item.get("source_type") or "").lower() not in source_types:
            return False
        if statuses and item.get("status") not in statuses:
            return False
        if purpose and purpose not in str(item.get("purpose") or "").lower():
            return False
        if query:
            haystack = " ".join([
                str(item.get("source_key") or ""),
                str(item.get("source_id") or ""),
                str(item.get("title") or ""),
                str(item.get("canonical_url") or ""),
                str(item.get("summary") or ""),
            ]).lower()
            if query not in haystack:
                return False
        return True

    matching = [item for item in items if matches(item)]
    return {
        "status": "ok",
        "ledger_counts": _coverage_counts(items),
        "matched_count": len(matching),
        "items": [_compact_coverage_item(item) for item in matching[:limit]],
        "message": (
            "Use inspected/skipped records to avoid duplicate source work. "
            "Use discovered/queued records to plan remaining batches."
        ),
    }


def extract_delegate_coverage_items(
    delegate_result,
    *,
    default_purpose=None,
    inspected_by="delegate",
    context_ref_id=None,
    max_items=100,
):
    """Extract source coverage records from a delegate result envelope or JSON block."""
    candidates = []
    for envelope in _delegate_coverage_envelopes(delegate_result):
        candidates.extend(_delegate_coverage_items_from_envelope(envelope))
        if len(candidates) >= max_items:
            break

    normalized = []
    for raw_item in candidates[:max_items]:
        record = _delegate_coverage_record_from_item(
            raw_item,
            default_purpose=default_purpose,
            inspected_by=inspected_by,
            context_ref_id=context_ref_id,
        )
        if record:
            normalized.append(record)

    return _dedupe_auto_coverage_records(normalized)


def _delegate_coverage_envelopes(delegate_result):
    envelopes = []
    if isinstance(delegate_result, dict):
        envelopes.append(delegate_result)
        text = delegate_result.get("text") or delegate_result.get("content") or delegate_result.get("answer")
        envelopes.extend(_parse_delegate_coverage_json_from_text(text))
    elif isinstance(delegate_result, list):
        envelopes.append({"coverage_items": delegate_result})
    else:
        envelopes.extend(_parse_delegate_coverage_json_from_text(delegate_result))
    return envelopes


def _delegate_coverage_items_from_envelope(envelope):
    if isinstance(envelope, list):
        return [item for item in envelope if isinstance(item, dict)]
    if not isinstance(envelope, dict):
        return []
    for key in (
        "coverage_items",
        "source_coverage_items",
        "sources",
        "inspected_sources",
        "checked_sources",
    ):
        value = envelope.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    coverage = envelope.get("source_coverage") or envelope.get("coverage")
    if isinstance(coverage, dict):
        for key in ("items", "coverage_items", "sources", "inspected_sources"):
            value = coverage.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    if isinstance(coverage, list):
        return [item for item in coverage if isinstance(item, dict)]
    return []


def _parse_delegate_coverage_json_from_text(text):
    if not isinstance(text, str) or not text.strip():
        return []
    parsed = []
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed.append(json.loads(stripped))
        except (TypeError, ValueError):
            pass
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            parsed.append(json.loads(block))
        except (TypeError, ValueError):
            continue
    return parsed


def _delegate_coverage_record_from_item(
    item,
    *,
    default_purpose=None,
    inspected_by="delegate",
    context_ref_id=None,
):
    if not isinstance(item, dict):
        return None
    canonical_url = (
        item.get("canonical_url")
        or item.get("url")
        or item.get("link")
        or item.get("web_url")
        or item.get("webViewLink")
    )
    source_id = (
        item.get("source_id")
        or item.get("item_id")
        or item.get("ref_id")
        or item.get("source_ref")
        or item.get("thread_id")
        or item.get("message_id")
        or item.get("file_id")
        or item.get("post_id")
        or item.get("page_id")
        or item.get("path")
        or item.get("filepath")
        or canonical_url
    )
    title = item.get("title") or item.get("name") or item.get("subject") or item.get("label")
    if not source_id and not canonical_url and not title:
        return None

    source_type = item.get("source_type") or item.get("type") or item.get("source_class") or "other"
    record = {
        "source_key": item.get("source_key"),
        "source_type": source_type,
        "source_id": source_id,
        "title": title,
        "canonical_url": canonical_url,
        "status": _coverage_status(item.get("status"), default="inspected"),
        "purpose": item.get("purpose") or default_purpose,
        "inspected_by": item.get("inspected_by") or inspected_by,
        "summary": item.get("summary") or item.get("finding") or item.get("notes") or item.get("reason"),
        "confidence": item.get("confidence"),
        "context_ref_id": item.get("context_ref_id") or context_ref_id,
        "evidence_refs": item.get("evidence_refs") or item.get("evidence_ref_ids") or item.get("refs"),
        "metadata": {
            **_coverage_metadata(item.get("metadata")),
            "coverage_source": "delegate_result",
        },
    }
    return {key: value for key, value in record.items() if value not in ("", None, {}, [])}


def extract_source_coverage_items_from_tool_result(
    tool_name,
    result,
    *,
    context_ref_id=None,
    tool_args=None,
):
    """Conservatively infer source coverage records from stable search/read outputs."""
    name = str(tool_name or "")
    if not name or name in AUTO_SOURCE_COVERAGE_SKIP_TOOLS:
        return []
    if not any(name.startswith(prefix) for prefix in AUTO_SOURCE_COVERAGE_TOOL_PREFIXES):
        return []

    records = []
    args = tool_args if isinstance(tool_args, dict) else {}
    status = _auto_coverage_status_for_tool(name)
    source_type_hint = _auto_source_type_for_tool(name, args)
    for item in _auto_coverage_candidate_items(result):
        record = _auto_coverage_record_for_item(
            item,
            tool_name=name,
            source_type_hint=source_type_hint,
            status=status,
            context_ref_id=context_ref_id,
            tool_args=args,
        )
        if record:
            records.append(record)

    if records:
        return _dedupe_auto_coverage_records(records)

    fallback = _auto_coverage_record_from_args(
        tool_name=name,
        tool_args=args,
        source_type_hint=source_type_hint,
        status=status,
        context_ref_id=context_ref_id,
        result=result,
    )
    return [fallback] if fallback else []


def _auto_coverage_status_for_tool(tool_name):
    name = str(tool_name or "")
    if any(token in name for token in ("search", "list", "get_posts", "get_media_items", "get_directory_tree")):
        return "discovered"
    if any(token in name for token in ("read", "get_thread", "get_threads", "get_messages", "get_post", "get_file", "view", "scraping_get")):
        return "inspected"
    return "discovered"


def _auto_source_type_for_tool(tool_name, tool_args=None):
    name = str(tool_name or "")
    args = tool_args if isinstance(tool_args, dict) else {}
    if name.startswith("email_"):
        return "email"
    if name.startswith("googledrive_"):
        return "gdrive"
    if name.startswith("web_search_") or name.startswith("scraping_"):
        return "web"
    if name.startswith("filesystem_"):
        return "local_file"
    if name.startswith("wordpress_"):
        if "media" in name:
            return "wp_media"
        post_type = str(args.get("post_type") or "").strip()
        if post_type == "pages":
            return "wp_page"
        return "wp_post"
    return "other"


def _auto_coverage_candidate_items(result):
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    if result.get("error"):
        return []
    for key in ("items", "results", "files", "threads", "messages", "data"):
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if isinstance(result.get("artifact"), dict):
        return [result["artifact"]]
    return [result]


def _auto_coverage_record_for_item(
    item,
    *,
    tool_name,
    source_type_hint,
    status,
    context_ref_id=None,
    tool_args=None,
):
    if not isinstance(item, dict):
        return None
    source_type = source_type_hint or _auto_source_type_from_item(item)
    source_id = _auto_source_id_for_item(item, source_type=source_type, tool_name=tool_name)
    canonical_url = _auto_url_for_item(item)
    if not source_id and not canonical_url:
        return None
    title = _auto_title_for_item(item)
    summary = _auto_summary_for_item(item, tool_name=tool_name)
    record = {
        "source_type": source_type,
        "source_id": source_id or canonical_url,
        "title": title,
        "canonical_url": canonical_url,
        "status": status,
        "purpose": _auto_purpose_for_tool(tool_name, tool_args),
        "inspected_by": "tool",
        "summary": summary,
        "context_ref_id": context_ref_id,
        "metadata": _auto_metadata_for_item(item, tool_name=tool_name),
    }
    return {key: value for key, value in record.items() if value not in ("", None, {}, [])}


def _auto_coverage_record_from_args(
    *,
    tool_name,
    tool_args,
    source_type_hint,
    status,
    context_ref_id=None,
    result=None,
):
    if isinstance(result, dict) and result.get("error"):
        status = "failed"
    source_type = source_type_hint or _auto_source_type_for_tool(tool_name, tool_args)
    source_id = (
        tool_args.get("thread_id")
        or tool_args.get("message_id")
        or tool_args.get("file_id")
        or tool_args.get("id")
        or tool_args.get("post_id")
        or tool_args.get("url")
        or tool_args.get("filepath")
        or tool_args.get("path")
        or tool_args.get("folder_id")
        or tool_args.get("directory")
    )
    if not source_id:
        ids = tool_args.get("thread_ids") or tool_args.get("message_ids") or tool_args.get("file_ids")
        if isinstance(ids, list) and len(ids) == 1:
            source_id = ids[0]
    if not source_id:
        return None
    source_id = _auto_source_id_with_kind(source_id, tool_args, source_type)
    return {
        "source_type": source_type,
        "source_id": source_id,
        "canonical_url": tool_args.get("url") if source_type == "web" else "",
        "status": status,
        "purpose": _auto_purpose_for_tool(tool_name, tool_args),
        "inspected_by": "tool",
        "summary": f"Auto-recorded from `{tool_name}` arguments.",
        "context_ref_id": context_ref_id,
        "metadata": {"tool_name": tool_name},
    }


def _auto_source_type_from_item(item):
    if item.get("thread_id") or item.get("message_id"):
        return "email"
    if item.get("file_id") or item.get("mimeType") or item.get("mime_type"):
        return "gdrive"
    if item.get("post_id") or (item.get("id") is not None and item.get("link")):
        return "wp_post"
    if _auto_url_for_item(item):
        return "web"
    if item.get("path") or item.get("filepath") or item.get("file_path"):
        return "local_file"
    return "other"


def _auto_source_id_for_item(item, *, source_type, tool_name):
    if source_type == "email":
        if item.get("thread_id") is not None:
            return f"thread:{item.get('thread_id')}"
        if item.get("message_id") is not None:
            return f"message:{item.get('message_id')}"
    if source_type == "gdrive":
        value = item.get("file_id") or item.get("id")
        return str(value) if value is not None else ""
    if source_type in {"wp_post", "wp_page", "wp_media"}:
        value = item.get("post_id") or item.get("id")
        if value is not None:
            return f"{source_type}:{value}"
    if source_type == "local_file":
        value = item.get("path") or item.get("filepath") or item.get("file_path") or item.get("full_path")
        return str(value) if value is not None else ""
    url = _auto_url_for_item(item)
    if url:
        return url
    value = item.get("source_id") or item.get("id")
    return str(value) if value is not None else ""


def _auto_source_id_with_kind(value, tool_args, source_type):
    raw = str(value or "").strip()
    if not raw:
        return ""
    if source_type == "email":
        if tool_args.get("thread_id") or tool_args.get("thread_ids"):
            return f"thread:{raw}"
        if tool_args.get("message_id") or tool_args.get("message_ids"):
            return f"message:{raw}"
    if source_type in {"wp_post", "wp_page", "wp_media"} and not raw.startswith(f"{source_type}:"):
        return f"{source_type}:{raw}"
    return raw


def _auto_url_for_item(item):
    for key in ("canonical_url", "url", "link", "webViewLink", "web_view_link", "source_url"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _auto_title_for_item(item):
    for key in ("title", "name", "subject", "filename"):
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("rendered") or value.get("raw")
        if value:
            return _coverage_text(value, 300)
    return ""


def _auto_summary_for_item(item, *, tool_name):
    parts = []
    for key in ("snippet", "summary", "description", "matched_fields", "matched_message_count", "message_count", "mime_type", "mimeType", "date", "newest_message_at"):
        value = item.get(key)
        if value in (None, "", [], {}):
            continue
        parts.append(f"{key}: {value}")
    if parts:
        return _coverage_text("; ".join(parts), 700)
    return f"Auto-recorded from `{tool_name}` result."


def _auto_purpose_for_tool(tool_name, tool_args=None):
    args = tool_args if isinstance(tool_args, dict) else {}
    query = args.get("query") or args.get("search") or args.get("name_contains") or args.get("full_text_contains")
    if query:
        return _coverage_text(f"{tool_name}: {query}", 300)
    return _coverage_text(tool_name, 300)


def _auto_metadata_for_item(item, *, tool_name):
    metadata = {"tool_name": tool_name}
    for key in (
        "matched_message_ids",
        "matched_fields",
        "participant_count",
        "message_count",
        "mime_type",
        "mimeType",
        "size",
        "truncated",
        "status",
        "body_mode",
        "content_chars",
    ):
        if item.get(key) not in (None, "", [], {}):
            metadata[key] = item.get(key)
    return _coverage_metadata(metadata)


def _dedupe_auto_coverage_records(records):
    by_key = {}
    for record in records:
        key = _coverage_stable_key(record)
        if key and key not in by_key:
            by_key[key] = record
    return list(by_key.values())[:100]


def view_attachment(app_root_path, filepath):
    if filepath is None or str(filepath).strip() == "":
        return {"error": "File path is required."}

    app_root = Path(app_root_path).resolve()
    workspace_root = app_root.parent.resolve()
    raw_path = str(filepath).strip()
    candidate_path = Path(raw_path).expanduser()
    candidates = []
    if is_upload_reference(raw_path):
        try:
            resolved_upload = resolve_upload_reference(raw_path, app_root)
        except (TypeError, ValueError):
            return {"error": "File path is invalid."}
        if resolved_upload is not None:
            candidates.append(resolved_upload)
    if not candidates and candidate_path.is_absolute():
        candidates.append(candidate_path.resolve())
    elif not candidates:
        normalized = raw_path.replace("\\", "/")
        stripped = normalized.lstrip("/")
        stripped_uploads = stripped[8:] if stripped.lower().startswith("uploads/") else stripped
        candidates.extend([
            (app_root / stripped).resolve(),
            (workspace_root / stripped).resolve(),
            (app_root / "uploads" / stripped_uploads).resolve(),
        ])

    local_path = next((item for item in candidates if item.exists() and item.is_file()), None)
    if local_path is None:
        return {"error": "File not found on server."}

    mime_type, _ = mimetypes.guess_type(str(local_path))
    if not mime_type:
        mime_type = 'application/octet-stream'
    try:
        with open(local_path, "rb") as attachment_file:
            encoded = base64.b64encode(attachment_file.read()).decode('utf-8')
        return f"data:{mime_type};base64,{encoded}"
    except Exception as exc:
        return {"error": f"Failed to read file: {str(exc)}"}

def handle_memorize_fact(
    scope,
    fact,
    target_uid,
    category=None,
    tier=None,
    importance=None,
    tier_locked=None,
    source="model",
):
    try:
        ensure_memory_tables_exist()
    except Exception:
        # If self-healing fails, existing logic will surface a concrete DB error.
        pass

    if not fact:
        return "Error: Fact cannot be empty."

    fact_clean = str(fact).strip()
    if not fact_clean:
        return "Error: Fact cannot be empty."

    scope_normalized = str(scope or "").strip().lower()
    category_normalized = _normalize_category(category) if category is not None else None
    tier_normalized = _normalize_tier(tier) if tier is not None else None
    importance_normalized = _normalize_importance(importance) if importance is not None else None

    if scope_normalized == 'user':
        existing = UserMemory.query.filter_by(user_id=target_uid).all()
        normalized = _normalize_fact_text(fact_clean)
        duplicate = next((row for row in existing if _normalize_fact_text(row.fact) == normalized), None)
        if duplicate:
            _set_memory_metadata(
                duplicate,
                category=category_normalized,
                tier=tier_normalized,
                importance=importance_normalized,
                tier_locked=tier_locked,
                source=source,
            )
            try:
                _apply_memory_tiering_policy("user", target_uid)
                commit_with_retry(db.session)
            except Exception as exc:
                return f"Database error updating duplicate memory metadata: {exc}"
            return f"Fact already exists in user scope (id: {duplicate.id}). Metadata refreshed."
        memory = UserMemory(user_id=target_uid, fact=fact_clean)
        db.session.add(memory)
    elif scope_normalized == 'org':
        if not _can_manage_org_memory(target_uid):
            return "Error: Only administrators can memorize organizational facts."
        _migrate_org_memory_to_system_encryption(target_uid)
        existing = OrgMemory.query.all()
        normalized = _normalize_fact_text(fact_clean)
        duplicate = next((row for row in existing if _normalize_fact_text(row.fact) == normalized), None)
        if duplicate:
            _set_memory_metadata(
                duplicate,
                category=category_normalized,
                tier=tier_normalized,
                importance=importance_normalized,
                tier_locked=tier_locked,
                source=source,
            )
            try:
                _apply_memory_tiering_policy("org", target_uid)
                commit_with_retry(db.session)
            except Exception as exc:
                return f"Database error updating duplicate memory metadata: {exc}"
            return f"Fact already exists in org scope (id: {duplicate.id}). Metadata refreshed."
        memory = OrgMemory(fact=fact_clean)
        db.session.add(memory)
    else:
        return "Error: Invalid scope."

    _set_memory_metadata(
        memory,
        category=category_normalized,
        tier=tier_normalized,
        importance=importance_normalized,
        tier_locked=tier_locked,
        source=source,
    )

    try:
        _apply_memory_tiering_policy(scope_normalized, target_uid)
        commit_with_retry(db.session)
        stored_metadata = _normalized_metadata_view(memory)
        return (
            f"Successfully memorized fact in {scope_normalized} scope "
            f"(category={stored_metadata['category']}, tier={stored_metadata['tier']}, importance={stored_metadata['importance']})."
        )
    except Exception as exc:
        return f"Database error memorizing fact: {exc}"


def _memory_match_score(row: UserMemory | OrgMemory, query_lower: str, terms: list[str]) -> int:
    fact_lower = str(row.fact or "").lower()
    score = 0
    if query_lower and query_lower in fact_lower:
        score += 4
    score += sum(1 for term in terms if term in fact_lower)
    metadata = _normalized_metadata_view(row)
    if metadata["tier"] == "tier1":
        score += 1
    if metadata["importance"] == "high":
        score += 1
    return score


def handle_recall_fact(query, target_uid, scope=None, category=None, include_tier2=None, max_results=12):
    try:
        ensure_memory_tables_exist()
    except Exception:
        pass

    query_clean = _clean_optional_text(query)
    if not query_clean:
        return "Error: Query cannot be empty."

    query_lower = query_clean.lower()
    terms = [part for part in query_lower.split() if part]
    if not terms:
        return "Error: Query cannot be empty."

    _migrate_org_memory_to_system_encryption(target_uid)
    scope_filter = _normalize_scope(scope)
    category_filter = _normalize_category(category) if _clean_optional_text(category) is not None else None
    include_tier2_bool = _parse_bool(include_tier2, default=_memory_recall_include_tier2_default())

    try:
        max_results_value = max(1, min(int(max_results or 12), 100))
    except (TypeError, ValueError):
        max_results_value = 12

    candidates: list[tuple[str, UserMemory | OrgMemory]] = []
    if scope_filter in {"both", "user"}:
        for mem in UserMemory.query.filter_by(user_id=target_uid).all():
            candidates.append(("user", mem))
    if scope_filter in {"both", "org"}:
        for mem in OrgMemory.query.all():
            candidates.append(("org", mem))

    scored: list[dict[str, Any]] = []
    for scope_name, row in candidates:
        if not row.fact:
            continue
        metadata = _normalized_metadata_view(row)
        if category_filter and metadata["category"] != category_filter:
            continue
        if not include_tier2_bool and metadata["tier"] == "tier2":
            continue
        score = _memory_match_score(row, query_lower, terms)
        if score <= 0:
            continue
        scored.append(
            {
                "scope": scope_name,
                "row": row,
                "metadata": metadata,
                "score": score,
                "ts": _memory_timestamp(row),
            }
        )

    if not scored:
        return "No relevant facts found."

    scored.sort(key=lambda item: (item["score"], item["ts"]), reverse=True)
    lines = []
    for item in scored[:max_results_value]:
        prefix = "[User Preference]" if item["scope"] == "user" else "[Org Rule/Fact]"
        metadata = item["metadata"]
        lines.append(
            f"{prefix} {item['row'].fact} "
            f"(category={metadata['category']}, tier={metadata['tier']}, importance={metadata['importance']})"
        )
    return "\n".join(lines)


def handle_manage_long_term_memory(
    action,
    scope,
    target_uid,
    memory_id=None,
    fact=None,
    query=None,
    limit=50,
    category=None,
    tier=None,
    include_tier2=True,
    importance=None,
    tier_locked=None,
):
    try:
        ensure_memory_tables_exist()
    except Exception:
        pass

    action = str(action or "").strip().lower()
    scope = str(scope or "").strip().lower()
    query_obj = _memory_scope_query(scope, target_uid)
    if query_obj is None:
        return {"status": "error", "message": "Error: Invalid scope."}

    if scope == "org" and action in {"update", "delete", "dedupe", "retier"} and not _can_manage_org_memory(target_uid):
        return {"status": "error", "message": "Error: Only administrators can modify organizational memory."}
    if scope == "org":
        _migrate_org_memory_to_system_encryption(target_uid)

    category_filter = _normalize_category(category) if _clean_optional_text(category) is not None else None
    tier_filter = _normalize_tier(tier) if _clean_optional_text(tier) is not None else None
    include_tier2_bool = _parse_bool(include_tier2, default=True)

    if action == "list":
        try:
            limit_value = max(1, min(int(limit or 50), 200))
        except (TypeError, ValueError):
            return {"status": "error", "message": "Error: limit must be an integer."}
        rows = query_obj.order_by(UserMemory.updated_at.desc() if scope == "user" else OrgMemory.updated_at.desc()).all()
        if query:
            terms = [part for part in str(query).lower().split() if part]
            if terms:
                rows = [
                    row for row in rows
                    if row.fact and any(term in row.fact.lower() for term in terms)
                ]
        filtered_rows = []
        for row in rows:
            metadata = _normalized_metadata_view(row)
            if category_filter and metadata["category"] != category_filter:
                continue
            if tier_filter and metadata["tier"] != tier_filter:
                continue
            if not tier_filter and not include_tier2_bool and metadata["tier"] == "tier2":
                continue
            filtered_rows.append(row)
        rows = filtered_rows[:limit_value]
        return {
            "status": "ok",
            "scope": scope,
            "count": len(rows),
            "memories": [_serialize_memory_record(scope, row) for row in rows],
        }

    if action == "delete":
        if not memory_id:
            return {"status": "error", "message": "Error: memory_id is required for delete."}
        row = query_obj.filter_by(id=memory_id).first()
        if not row:
            return {"status": "error", "message": f"Error: Memory id {memory_id} not found in {scope} scope."}
        db.session.delete(row)
        try:
            commit_with_retry(db.session)
            return {"status": "ok", "message": f"Deleted {scope} memory id {memory_id}."}
        except Exception as exc:
            return {"status": "error", "message": f"Database error deleting memory: {exc}"}

    if action == "update":
        if not memory_id:
            return {"status": "error", "message": "Error: memory_id is required for update."}
        if fact is None or not str(fact).strip():
            return {"status": "error", "message": "Error: fact is required for update."}
        row = query_obj.filter_by(id=memory_id).first()
        if not row:
            return {"status": "error", "message": f"Error: Memory id {memory_id} not found in {scope} scope."}

        normalized = _normalize_fact_text(fact)
        sibling_rows = query_obj.all()
        duplicate = next(
            (
                item for item in sibling_rows
                if item.id != row.id and _normalize_fact_text(item.fact) == normalized
            ),
            None,
        )
        if duplicate:
            return {
                "status": "error",
                "message": f"Error: Updated fact would duplicate memory id {duplicate.id} in {scope} scope.",
            }

        row.fact = str(fact).strip()
        _set_memory_metadata(
            row,
            category=category if category is not None else category_filter,
            tier=tier if tier is not None else tier_filter,
            importance=importance,
            tier_locked=tier_locked,
        )
        try:
            _apply_memory_tiering_policy(scope, target_uid)
            commit_with_retry(db.session)
            return {"status": "ok", "message": f"Updated {scope} memory id {row.id}.", "memory": _serialize_memory_record(scope, row)}
        except Exception as exc:
            return {"status": "error", "message": f"Database error updating memory: {exc}"}

    if action == "dedupe":
        rows = query_obj.order_by(UserMemory.created_at.asc() if scope == "user" else OrgMemory.created_at.asc()).all()
        kept_by_key = {}
        duplicates = []
        for row in rows:
            key = _normalize_fact_text(row.fact)
            if not key:
                key = f"_empty_{row.id}"
            if key in kept_by_key:
                duplicates.append(row)
            else:
                kept_by_key[key] = row.id

        removed_ids = [row.id for row in duplicates]
        for row in duplicates:
            db.session.delete(row)
        try:
            _apply_memory_tiering_policy(scope, target_uid)
            if duplicates:
                commit_with_retry(db.session)
            return {
                "status": "ok",
                "scope": scope,
                "removed_count": len(removed_ids),
                "removed_ids": removed_ids,
                "kept_count": len(kept_by_key),
            }
        except Exception as exc:
            return {"status": "error", "message": f"Database error deduplicating memory: {exc}"}

    if action == "summarize_categories":
        rows = query_obj.order_by(UserMemory.updated_at.desc() if scope == "user" else OrgMemory.updated_at.desc()).all()
        if query:
            terms = [part for part in str(query).lower().split() if part]
            if terms:
                rows = [
                    row for row in rows
                    if row.fact and any(term in row.fact.lower() for term in terms)
                ]
        summary: dict[str, dict[str, Any]] = {}
        for row in rows:
            metadata = _normalized_metadata_view(row)
            if category_filter and metadata["category"] != category_filter:
                continue
            if tier_filter and metadata["tier"] != tier_filter:
                continue
            if not tier_filter and not include_tier2_bool and metadata["tier"] == "tier2":
                continue
            category_key = metadata["category"]
            bucket = summary.setdefault(
                category_key,
                {"category": category_key, "count": 0, "tier1_count": 0, "tier2_count": 0, "sample_facts": []},
            )
            bucket["count"] += 1
            if metadata["tier"] == "tier1":
                bucket["tier1_count"] += 1
            else:
                bucket["tier2_count"] += 1
            if len(bucket["sample_facts"]) < 3 and row.fact:
                bucket["sample_facts"].append(row.fact)

        buckets = sorted(summary.values(), key=lambda item: (-item["count"], item["category"]))
        return {
            "status": "ok",
            "scope": scope,
            "total_categories": len(buckets),
            "categories": buckets,
        }

    if action == "retier":
        try:
            stats = _apply_memory_tiering_policy(scope, target_uid)
            if stats.get("changed_count", 0):
                commit_with_retry(db.session)
            return {
                "status": "ok",
                "scope": scope,
                "tier1_limit": _memory_tier1_limit(scope),
                **stats,
            }
        except Exception as exc:
            return {"status": "error", "message": f"Database error re-tiering memory: {exc}"}

    return {"status": "error", "message": f"Error: Unknown action '{action}'."}

def handle_send_user_alert(title, message, action_url, target_uid):
    if not title or not message:
        return "Error: Title and message are required."
        
    alert = UserAlert(
        user_id=target_uid,
        title=title,
        message=message,
        action_url=action_url,
    )
    db.session.add(alert)
    
    try:
        commit_with_retry(db.session)
        return "Successfully dispatched user alert."
    except Exception as exc:
        return f"Database error sending alert: {exc}"
