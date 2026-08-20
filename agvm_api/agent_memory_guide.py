from __future__ import annotations

from typing import Any


AGVM_USAGE_GUIDE_SCHEMA_VERSION = "agvm.mcp_usage_guide.v1"


def build_agvm_usage_guide() -> dict[str, Any]:
    recommended_flow = [
        "get_agvm_usage_guide",
        "list_brains",
        "ensure_brain",
        "retrieve_context",
    ]
    query_recipes = {
        "normal_recall": {
            "tool": "retrieve_context",
            "arguments": {
                "query_text": "State the concrete user need, including names, project, decision/action needed and relevant time window.",
                "retrieval_mode": "balanced",
                "context_package_mode": "mcp_operational",
                "include_answer_demo": False,
            },
        },
        "fast_recall": {
            "tool": "retrieve_context",
            "arguments": {
                "query_text": "Ask the smallest useful memory question.",
                "retrieval_mode": "flash",
                "include_answer_demo": False,
            },
        },
        "broad_synthesis": {
            "tool": "retrieve_context",
            "arguments": {
                "query_text": "Describe the broad theme and the synthesis decision the user needs.",
                "retrieval_mode": "heavy",
                "context_package_mode": "broad_dossier",
                "include_answer_demo": False,
            },
        },
        "evidence_sensitive": {
            "tool": "retrieve_source_trace",
            "arguments": {
                "query_text": "Ask the claim or decision that needs provenance.",
                "retrieval_mode": "forensic",
                "document_text_policy": "refs_only",
                "include_answer_demo": False,
            },
        },
        "known_document": {
            "tool": "retrieve_document",
            "arguments": {
                "query_text": "Ask what should be hydrated or checked in the document.",
                "document_id": "Use when a previous document_ref exposed an exact id.",
                "document_hint": "Use title, filename, URL, source label or topic when id is unknown.",
                "document_text_policy": "refs_only first; use top_raw/all_raw only when raw text is necessary.",
            },
        },
    }
    policy = {
        "brain_selection": {
            "default": "Use an explicit brain_id for durable sessions.",
            "safe_onboarding": "Call ensure_brain with activation_policy=return_only unless the user asks to change active/default brain.",
            "shared_state_warning": "select_brain changes shared local active/default state visible to UI and other clients.",
        },
        "retrieval": {
            "source_of_truth": "Read the default output package first; answer_demo is secondary and never authoritative.",
            "query_text": "Natural-language need, not keyword-only search.",
            "thread_id": "Use for follow-up turns that should share continuity.",
            "search_id": "Use returned search_id with inspection tools instead of repeating uncertain searches.",
        },
        "mutation": {
            "preview_first": "Use preview tools before apply tools.",
            "apply_requires_user_approval": True,
            "registry_write": "Brain creation/selection is separate from memory mutation but still changes registry state.",
        },
        "documents": {
            "first_pass": "Use refs_only and document refs before raw text hydration.",
            "raw_text": "Use top_raw/all_raw only when the task needs source text, quotes or exact wording.",
        },
    }
    tool_map = {
        "guide": ["get_agvm_usage_guide"],
        "brain_registry": ["list_brains", "active_brain", "ensure_brain", "create_brain", "select_brain"],
        "retrieval": [
            "retrieve_context",
            "retrieve_document",
            "retrieve_document_workspace",
            "retrieve_project_workspace",
            "retrieve_path_corridor",
            "retrieve_source_trace",
        ],
        "inspection": ["inspect_context_package", "inspect_route", "inspect_path_corridor", "inspect_memory_object"],
        "memory_growth": ["grow_source_preview", "grow_source_status", "write_memory_preview", "ask_memory_clarification"],
        "apply_gated": ["grow_source_apply", "write_memory_commit", "grow_apply", "sleep_apply", "evolve_apply", "matrix_calibration_apply"],
    }
    markdown_guide = """# AGVM MCP Usage Guide

Use AGVM as an external persistent memory engine. Do not treat it as model-internal memory.

Recommended first flow:
1. Call `get_agvm_usage_guide`.
2. Call `list_brains`.
3. Call `ensure_brain` for the user/project/task with `activation_policy=return_only`.
4. Pass the resolved `brain_id` to `retrieve_context` and follow-up tools.

Query rules:
- Write `query_text` as the concrete user need, not keywords only.
- Include names, project labels, time window, decision/action needed and known source hints.
- Use `retrieval_mode=flash` for quick recall, `balanced` for normal work, `heavy` for broad synthesis and `forensic` for evidence-sensitive checks.
- Use `document_hint` for title, filename, URL, source label or topic when `document_id` is unknown.
- Start document retrieval with `document_text_policy=refs_only`; use `top_raw` or `all_raw` only when raw text is required.
- Use returned `search_id` with inspection tools before repeating uncertain searches.

Safety rules:
- The default output package is the source of truth.
- `answer_demo` is optional and secondary.
- Preview memory changes before apply.
- Do not call apply/destructive tools without explicit user approval.
- Do not silently change shared active/default brain.
"""
    return {
        "schema_version": AGVM_USAGE_GUIDE_SCHEMA_VERSION,
        "guide_name": "AGVM MCP Agent Memory Usage Guide",
        "markdown_guide": markdown_guide,
        "policy": policy,
        "recommended_flow": recommended_flow,
        "query_recipes": query_recipes,
        "tool_map": tool_map,
        "first_call": {
            "tool": "get_agvm_usage_guide",
            "requires_brain_id": False,
            "next": recommended_flow[1:],
        },
    }
