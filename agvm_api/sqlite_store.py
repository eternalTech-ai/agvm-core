# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from collections import defaultdict
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from config import (
    APP_NAME,
    APP_VERSION,
    ATLAS_VERSION,
    COARSE_BUCKET_SIZE,
    FACET_FIELDS,
    FINE_BUCKET_SIZE,
    GRAPH_VERSION,
    ROUTING_FIELDS,
)
from graph_view import build_graph_view
from memory_hygiene import build_hygiene_metadata
from memory_learning import (
    COGNITIVE_JOB_SCHEMA_VERSION,
    MATRIX_REVISION_SCHEMA_VERSION,
    MEMORY_LEARNING_EVENT_SCHEMA_VERSION,
    MEMORY_LEARNING_REQUIRED_TABLES,
    MEMORY_POLICY_REVISION_SCHEMA_VERSION,
    SOURCE_ASSET_SCHEMA_VERSION,
    SOURCE_REFERENCE_SCHEMA_VERSION,
    TOPOLOGY_FIELD_REVISION_SCHEMA_VERSION,
    build_memory_learning_capability_report,
)
from cognitive_jobs import (
    build_cognitive_job_capability_report,
    cognitive_job_learning_event,
    evaluate_cognitive_job_policy,
    normalize_cognitive_job,
)
from projection import color_from_brainhex, normalize_scores, position_to_bucket, position_to_topology_brainhex
from runtime_scope import current_atlas_path, current_brain_id, current_graph_path, current_graph_view_path, current_index_path, current_sqlite_path
from storage import atomic_write_json, empty_atlas, empty_graph, empty_graph_view, empty_index, ensure_data_dir, utc_timestamp
from stream_contract import annotate_stream_event

_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAPPED_RUNTIME_STORE_PATHS: set[str] = set()
RUNTIME_RETENTION_REPORT_SCHEMA_VERSION = "agvm.runtime_retention_report.v1"
_RUNTIME_RETENTION_ACTIVE_STATUSES = {"created", "running"}
_RUNTIME_RETENTION_PINNED_EVENT_TYPES = {
    "answer_final",
    "context_package_materialized",
    "mcp_fast_exact_no_match_boundary_returned",
    "mcp_first_package_terminal_ready",
    "mcp_spatial_background_completed",
    "result_ready",
    "search_failed",
    "search_stopped",
}

GEOMETRY_CALIBRATION_OPERATION_SCHEMA_VERSION = "agvm.geometry_calibration_operation.v1"
GEOMETRY_CALIBRATION_ROLLBACK_SCHEMA_VERSION = "agvm.geometry_calibration_rollback.v1"
_GEOMETRY_NODE_STATE_COLUMNS = (
    "id",
    "x",
    "y",
    "z",
    "coarse_bucket_x",
    "coarse_bucket_y",
    "coarse_bucket_z",
    "coarse_bucket_key",
    "fine_bucket_x",
    "fine_bucket_y",
    "fine_bucket_z",
    "fine_bucket_key",
    "topology_brainhex_json",
    "topology_color_json",
    "matrix_revision_id",
    "topology_revision_id",
    "matrix_calibration_plan_signature",
    "matrix_calibrated_at",
)


class GeometryCalibrationStoreError(ValueError):
    def __init__(self, code: str, *, status_code: int = 409, context: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.context = dict(context or {})


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _search_plan_health_summary(plan_payload: dict[str, Any] | None) -> dict[str, Any]:
    plan = _dict_value(plan_payload)
    runtime = _dict_value(plan.get("planner_runtime")) or _dict_value(plan.get("runtime"))
    semantic = _dict_value(plan.get("semantic_contract_runtime")) or _dict_value(runtime.get("semantic_contract_runtime"))
    truth = _dict_value(plan.get("search_map_2d_truth"))
    return {
        "planner_runtime": {
            "semantic_contract_ai_required": runtime.get("semantic_contract_ai_required"),
            "semantic_contract_material": runtime.get("semantic_contract_material"),
            "semantic_contract_status": runtime.get("semantic_contract_status"),
            "planner_path": runtime.get("planner_path"),
            "planner_kind": runtime.get("planner_kind"),
            "heuristic_provisional": runtime.get("heuristic_provisional"),
        },
        "semantic_contract_runtime": {
            "ai_required": semantic.get("ai_required"),
            "enabled": semantic.get("enabled"),
            "material": semantic.get("material"),
            "status": semantic.get("status"),
        },
        "search_map_2d_truth": {
            "required": truth.get("required"),
            "ready": truth.get("ready"),
            "route_event_count": truth.get("route_event_count"),
            "travel": truth.get("travel"),
            "route_steps": truth.get("route_steps"),
            "pending_path_count": truth.get("pending_path_count"),
            "pending": truth.get("pending"),
            "path_count": truth.get("path_count"),
        },
    }


def _search_result_health_summary(result_payload: dict[str, Any] | None, *, result_json_length: int = 0) -> dict[str, Any]:
    result = _dict_value(result_payload)
    context_package = _dict_value(result.get("context_package"))
    contract = _dict_value(context_package.get("contract"))
    path_truth = _dict_value(contract.get("path_truth")) or _dict_value(context_package.get("path_truth_contract"))
    package_metrics = _dict_value(context_package.get("metrics"))
    path_corridors = _dict_value(result.get("path_corridors"))
    corridor_metrics = _dict_value(path_corridors.get("metrics"))
    runtime = _dict_value(result.get("planner_runtime")) or _dict_value(result.get("runtime"))
    semantic_runtime = _dict_value(result.get("semantic_contract_runtime"))
    ai_spatial_runtime = _dict_value(result.get("ai_spatial_landing_contract_runtime"))
    delivery_contract = _dict_value(result.get("mcp_delivery_contract"))
    delivery_ai = _dict_value(delivery_contract.get("ai"))
    delivery_ai_spatial = _dict_value(delivery_contract.get("ai_spatial_landing_contract"))
    delivery_path_truth = _dict_value(delivery_contract.get("path_truth"))
    document_delivery = _dict_value(result.get("document_delivery_contract"))
    document_refs = [
        _dict_value(ref)
        for ref in list(result.get("document_refs") or result.get("docs") or [])[:12]
        if isinstance(ref, dict)
    ]
    materialization = _dict_value(result.get("context_package_materialization"))
    agent_markdown = str(context_package.get("agent_markdown") or "")
    raw_result_length = int(result_json_length or 0)
    mission_learning = (
        _dict_value(result.get("mission_learning_rollup"))
        or _dict_value(runtime.get("mission_learning_rollup"))
        or _dict_value(context_package.get("mission_learning_rollup"))
    )
    mission_learning_summary = {
        "schema_version": mission_learning.get("schema_version"),
        "status": mission_learning.get("status"),
        "signal_count": mission_learning.get("signal_count"),
        "family_counts": _dict_value(mission_learning.get("family_counts")),
        "reason_counts": _dict_value(mission_learning.get("reason_counts")),
        "recommended_actions": [
            {
                "family": _dict_value(action).get("family"),
                "action": _dict_value(action).get("action"),
                "severity": _dict_value(action).get("severity"),
                "reason_code": _dict_value(action).get("reason_code"),
            }
            for action in list(mission_learning.get("recommended_actions") or [])[:12]
            if isinstance(action, dict)
        ],
        "topology_overlay_summary": {
            "schema_version": _dict_value(mission_learning.get("topology_overlay_summary")).get("schema_version"),
            "review_required": _dict_value(mission_learning.get("topology_overlay_summary")).get("review_required"),
            "density_lobe_count": len(list(_dict_value(mission_learning.get("topology_overlay_summary")).get("density_lobes") or [])),
            "correction_prior_count": len(list(_dict_value(mission_learning.get("topology_overlay_summary")).get("correction_priors") or [])),
            "bridge_candidate_count": len(list(_dict_value(mission_learning.get("topology_overlay_summary")).get("bridge_candidates") or [])),
        },
    }
    return {
        "schema_version": "agvm.search_result_health_summary.v2",
        "status": result.get("status"),
        "answerability_state": result.get("answerability_state"),
        "stop_reason": result.get("stop_reason"),
        "result_json_length": raw_result_length,
        "document_text_policy": result.get("document_text_policy"),
        "document_delivery_contract": {
            "schema_version": document_delivery.get("schema_version"),
            "state": document_delivery.get("state"),
            "document_text_policy": document_delivery.get("document_text_policy"),
            "document_ref_count": document_delivery.get("document_ref_count"),
            "actionable_document_ref_count": document_delivery.get("actionable_document_ref_count"),
            "raw_available_document_ref_count": document_delivery.get("raw_available_document_ref_count"),
            "raw_included_document_count": document_delivery.get("raw_included_document_count"),
            "document_bundle_state": document_delivery.get("document_bundle_state"),
            "document_bundle_document_count": document_delivery.get("document_bundle_document_count"),
            "all_refs_actionable": document_delivery.get("all_refs_actionable"),
        },
        "document_refs": [
            {
                "document_id": ref.get("document_id") or ref.get("anchor_node_id"),
                "raw_text_available": ref.get("raw_text_available"),
                "raw_available": ref.get("raw_available"),
                "raw_availability": {
                    "state": _dict_value(ref.get("raw_availability")).get("state"),
                    "raw_text_available": _dict_value(ref.get("raw_availability")).get("raw_text_available"),
                },
                "retrieve_document_call": {"tool_name": "retrieve_document"}
                if ref.get("retrieve_document_call")
                else {},
            }
            for ref in document_refs
        ],
        "planner_runtime": {
            "semantic_contract_ai_required": runtime.get("semantic_contract_ai_required"),
            "semantic_contract_material": runtime.get("semantic_contract_material"),
            "semantic_contract_status": runtime.get("semantic_contract_status"),
            "planner_path": runtime.get("planner_path"),
            "planner_kind": runtime.get("planner_kind"),
            "heuristic_provisional": runtime.get("heuristic_provisional"),
        },
        "semantic_contract_runtime": {
            "ai_required": semantic_runtime.get("ai_required"),
            "enabled": semantic_runtime.get("enabled"),
            "material": semantic_runtime.get("material"),
            "status": semantic_runtime.get("status"),
        },
        "ai_spatial_landing_contract_runtime": {
            "material": ai_spatial_runtime.get("material"),
            "status": ai_spatial_runtime.get("status"),
            "cache_hit": ai_spatial_runtime.get("cache_hit"),
        },
        "mcp_delivery_contract": {
            "schema_version": delivery_contract.get("schema_version"),
            "client_payload_state": delivery_contract.get("client_payload_state"),
            "completion_state": delivery_contract.get("completion_state"),
            "terminal_for_client": delivery_contract.get("terminal_for_client"),
            "terminal_for_inspection": delivery_contract.get("terminal_for_inspection"),
            "run_finished": delivery_contract.get("run_finished"),
            "path_truth": delivery_path_truth,
            "ai": {
                "required": delivery_ai.get("required"),
                "materialized": delivery_ai.get("materialized"),
                "critical_path_state": delivery_ai.get("critical_path_state"),
                "critical_path_certifiable": delivery_ai.get("critical_path_certifiable"),
                "route_arbitration_certifiable": delivery_ai.get("route_arbitration_certifiable"),
            },
            "ai_spatial_landing_contract": {
                "observed": delivery_ai_spatial.get("observed"),
                "materialized": delivery_ai_spatial.get("materialized"),
                "certifiable": delivery_ai_spatial.get("certifiable"),
                "status": delivery_ai_spatial.get("status"),
                "source": delivery_ai_spatial.get("source"),
            },
        },
        "context_package_materialization": {
            "state": materialization.get("state"),
            "contract_passed": materialization.get("contract_passed"),
            "terminal": materialization.get("terminal"),
            "terminal_for_mcp_client": materialization.get("terminal_for_mcp_client"),
            "background_cap_reason": materialization.get("background_cap_reason"),
            "agent_markdown_chars": materialization.get("agent_markdown_chars"),
        },
        "result_surface_ready_ms": result.get("result_surface_ready_ms"),
        "final_materialization_completed_ms": result.get("final_materialization_completed_ms"),
        "ai_materialization_hard_gate": _dict_value(result.get("ai_materialization_hard_gate")),
        "ai_landing_materialization": _dict_value(result.get("ai_landing_materialization")),
        "mission_learning_rollup": mission_learning_summary,
        "runtime_state_contract": _dict_value(result.get("runtime_state_contract")),
        "context_package": {
            "status": context_package.get("status"),
            "contract": {"path_truth": path_truth},
            "path_truth_contract": path_truth,
            "metrics": {
                "agent_markdown_char_count": len(agent_markdown),
                "hot_item_count": package_metrics.get("hot_item_count"),
                "cold_item_count": package_metrics.get("cold_item_count"),
                "document_ref_count": package_metrics.get("document_ref_count"),
            },
            "agent_markdown_preview": agent_markdown[:1200],
        },
        "path_corridors": {
            "metrics": corridor_metrics,
            "path_count": len(list(path_corridors.get("paths") or path_corridors.get("corridors") or [])),
        },
    }


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_suffix: str) -> bool:
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_suffix}")
        return True
    return False


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _select_column_or_sql(columns: set[str], column: str, fallback_sql: str, *, alias: str | None = None) -> str:
    target = alias or column
    if column in columns:
        return column if target == column else f"{column} AS {target}"
    return f"{fallback_sql} AS {target}"


def _backfill_recent_search_plan_health(conn: sqlite3.Connection, *, limit: int = 200) -> int:
    rows = conn.execute(
        """
        SELECT search_id, plan_json
        FROM search_sessions
        WHERE plan_json IS NOT NULL
          AND (plan_health_json IS NULL OR plan_health_json = '')
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    updated = 0
    for row in rows:
        plan = _json_load(row["plan_json"], {})
        summary = _search_plan_health_summary(plan if isinstance(plan, dict) else {})
        conn.execute(
            """
            UPDATE search_sessions
            SET plan_health_json = ?
            WHERE search_id = ?
            """,
            (_json_dump(summary), str(row["search_id"])),
        )
        updated += 1
    return updated


def _backfill_recent_search_result_health(conn: sqlite3.Connection, *, limit: int = 50) -> int:
    rows = conn.execute(
        """
        SELECT search_id, result_json
        FROM search_sessions
        WHERE result_json IS NOT NULL
          AND (
            result_health_json IS NULL
            OR result_health_json = ''
            OR json_extract(result_health_json, '$.schema_version') != 'agvm.search_result_health_summary.v2'
          )
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    updated = 0
    for row in rows:
        result_json = str(row["result_json"] or "")
        result = _json_load(result_json, {})
        summary = _search_result_health_summary(
            result if isinstance(result, dict) else {},
            result_json_length=len(result_json),
        )
        conn.execute(
            """
            UPDATE search_sessions
            SET result_health_json = ?
            WHERE search_id = ?
            """,
            (_json_dump(summary), str(row["search_id"])),
        )
        updated += 1
    return updated


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * ratio))))
    return float(ordered[index])


def _median(values: list[float]) -> float:
    return _percentile(values, 0.5)


def _nonzero_median(values: list[float]) -> float:
    clean: list[float] = []
    for value in values:
        numeric = _safe_float(value, default=math.nan)
        if math.isfinite(numeric):
            clean.append(numeric)
    if not clean:
        return 0.0
    nonzero = [value for value in clean if value > 0.0]
    return _median(nonzero or clean)


def _octant_key(x: float, y: float, z: float) -> str:
    return f"{'+' if x >= 0 else '-'}{'+' if y >= 0 else '-'}{'+' if z >= 0 else '-'}"


@contextmanager
def connect(
    *,
    timeout_seconds: float = 30.0,
    busy_timeout_ms: int = 30000,
) -> Iterable[sqlite3.Connection]:
    ensure_data_dir()
    sqlite_path = current_sqlite_path()
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sqlite_path, timeout=max(0.001, float(timeout_seconds)))
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={max(1, int(busy_timeout_ms))};")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def connect_readonly(
    *,
    timeout_seconds: float = 2.0,
    busy_timeout_ms: int = 2000,
) -> Iterable[sqlite3.Connection]:
    ensure_data_dir()
    sqlite_path = current_sqlite_path()
    if not sqlite_path.exists():
        with connect(timeout_seconds=timeout_seconds, busy_timeout_ms=busy_timeout_ms) as conn:
            yield conn
        return
    uri = f"file:{sqlite_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=max(0.001, float(timeout_seconds)))
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={max(1, int(busy_timeout_ms))};")
    conn.execute("PRAGMA query_only=ON;")
    try:
        yield conn
    finally:
        conn.close()


def _reconcile_node_hygiene(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT
            n.id,
            n.memory_type,
            n.is_document_anchor,
            n.source_trust,
            n.claim_status,
            t.raw_text,
            t.summary_full,
            t.provenance_json,
            s.derivation_role,
            s.document_role
        FROM nodes_nav n
        LEFT JOIN node_text t ON t.node_id = n.id
        LEFT JOIN node_semantics s ON s.node_id = n.id
        """
    ).fetchall()
    hygiene_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        node_id = str(row["id"])
        raw_text = str(row["raw_text"] or row["summary_full"] or "")
        provenance = _json_load(row["provenance_json"], {})
        input_mode = (
            "document"
            if bool(row["is_document_anchor"]) or str((provenance or {}).get("source_type") or "").strip().lower() == "document"
            else "auto"
        )
        hygiene_by_id[node_id] = build_hygiene_metadata(
            raw_text=raw_text,
            input_mode=input_mode,
            provenance=provenance,
            memory_type=row["memory_type"],
            derivation_role=row["derivation_role"],
            document_role=row["document_role"],
            is_document_anchor=bool(row["is_document_anchor"]),
        )

    edge_rows = conn.execute(
        """
        SELECT source_id, target_id
        FROM graph_edges
        WHERE edge_type = 'derives_from'
        """
    ).fetchall()
    for _ in range(4):
        changed = False
        for edge in edge_rows:
            source = hygiene_by_id.get(str(edge["source_id"]))
            target_id = str(edge["target_id"])
            target = hygiene_by_id.get(target_id)
            if not source or not target:
                continue
            if source.get("source_trust") == "synthetic_test" and target.get("source_trust") != "synthetic_test":
                target.update(
                    {
                        "source_trust": "synthetic_test",
                        "claim_status": "test_artifact",
                        "answer_eligible": False,
                        "profile_eligible": False,
                        "document_eligible": False,
                    }
                )
                changed = True
        if not changed:
            break

    for node_id, hygiene in hygiene_by_id.items():
        conn.execute(
            """
            UPDATE nodes_nav
            SET source_trust = ?,
                claim_status = ?,
                answer_eligible = ?,
                profile_eligible = ?,
                document_eligible = ?
            WHERE id = ?
            """,
            (
                str(hygiene.get("source_trust") or "user_asserted"),
                str(hygiene.get("claim_status") or "fact"),
                1 if hygiene.get("answer_eligible") else 0,
                1 if hygiene.get("profile_eligible") else 0,
                1 if hygiene.get("document_eligible") else 0,
                node_id,
            ),
        )


def bootstrap_runtime_store() -> None:
    ensure_data_dir()
    sqlite_path_key = str(current_sqlite_path().expanduser().resolve())
    with _BOOTSTRAP_LOCK:
        if sqlite_path_key in _BOOTSTRAPPED_RUNTIME_STORE_PATHS:
            ensure_legacy_exports()
            return
        with connect() as conn:
            conn.executescript(
                """
            CREATE TABLE IF NOT EXISTS nodes_nav (
                id TEXT PRIMARY KEY,
                node_kind TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                z REAL NOT NULL,
                coarse_bucket_x INTEGER NOT NULL,
                coarse_bucket_y INTEGER NOT NULL,
                coarse_bucket_z INTEGER NOT NULL,
                coarse_bucket_key TEXT NOT NULL,
                fine_bucket_x INTEGER NOT NULL,
                fine_bucket_y INTEGER NOT NULL,
                fine_bucket_z INTEGER NOT NULL,
                fine_bucket_key TEXT NOT NULL,
                topology_brainhex_json TEXT NOT NULL,
                topology_color_json TEXT NOT NULL,
                routing_scores_json TEXT NOT NULL,
                routing_facets_json TEXT NOT NULL,
                summary_short TEXT NOT NULL,
                guide_area TEXT,
                memory_confidence REAL,
                identity_resolution_confidence REAL,
                evidence_confidence REAL,
                stability_confidence REAL,
                is_document_anchor INTEGER NOT NULL DEFAULT 0,
                is_summary INTEGER NOT NULL DEFAULT 0,
                granularity REAL NOT NULL DEFAULT 0.5,
                novelty REAL NOT NULL DEFAULT 0.5,
                sleep_revision_count INTEGER NOT NULL DEFAULT 0,
                last_sleep_review_at TEXT,
                temporal_role TEXT,
                valid_from TEXT,
                valid_to TEXT,
                observed_at TEXT,
                superseded_by TEXT,
                obsoletes_json TEXT NOT NULL DEFAULT '[]',
                temporal_confidence REAL,
                lifecycle_status TEXT NOT NULL DEFAULT 'active',
                source_trust TEXT NOT NULL DEFAULT 'user_asserted',
                claim_status TEXT NOT NULL DEFAULT 'fact',
                answer_eligible INTEGER NOT NULL DEFAULT 1,
                profile_eligible INTEGER NOT NULL DEFAULT 1,
                document_eligible INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS node_text (
                node_id TEXT PRIMARY KEY,
                raw_text TEXT NOT NULL,
                summary_full TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                source_label TEXT,
                source_type TEXT,
                FOREIGN KEY (node_id) REFERENCES nodes_nav(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS node_semantics (
                node_id TEXT PRIMARY KEY,
                routing_brainhex_json TEXT NOT NULL,
                semantic_color_json TEXT NOT NULL,
                base_position_json TEXT NOT NULL,
                derivation_role TEXT,
                derivation_confidence REAL,
                derived_from_preview_id TEXT,
                document_role TEXT,
                document_anchor_id TEXT,
                document_chunk_index INTEGER,
                source_unit_id TEXT,
                source_unit_title TEXT,
                source_unit_kind TEXT,
                source_unit_role TEXT,
                promotion_role TEXT,
                source_unit_formation_strategy TEXT,
                source_span_start INTEGER,
                source_span_end INTEGER,
                retrieval_affordance_json TEXT NOT NULL DEFAULT '{}',
                retrieval_aliases_json TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY (node_id) REFERENCES nodes_nav(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS links (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                strength REAL NOT NULL,
                reason TEXT NOT NULL,
                kind TEXT,
                stability REAL,
                PRIMARY KEY (source_id, target_id),
                FOREIGN KEY (source_id) REFERENCES nodes_nav(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES nodes_nav(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS highways (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                strength REAL NOT NULL,
                reason TEXT NOT NULL,
                kind TEXT,
                stability REAL,
                PRIMARY KEY (source_id, target_id),
                FOREIGN KEY (source_id) REFERENCES nodes_nav(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES nodes_nav(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS graph_edges (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (source_id, target_id, edge_type),
                FOREIGN KEY (source_id) REFERENCES nodes_nav(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES nodes_nav(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS atlas_cache (
                bucket_key TEXT PRIMARY KEY,
                granularity TEXT NOT NULL,
                centroid_x REAL NOT NULL,
                centroid_y REAL NOT NULL,
                centroid_z REAL NOT NULL,
                node_count INTEGER NOT NULL,
                document_anchor_count INTEGER NOT NULL,
                guide_area_histogram_json TEXT NOT NULL,
                dominant_direction_hint_json TEXT NOT NULL,
                outgoing_highway_gateways_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS identity_nucleus_cache (
                cache_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS search_sessions (
                search_id TEXT PRIMARY KEY,
                thread_id TEXT,
                query_text TEXT NOT NULL,
                response_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                plan_json TEXT,
                plan_health_json TEXT,
                result_json TEXT,
                result_health_json TEXT,
                stop_reason TEXT,
                answerability_state TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS warm_thread_state (
                thread_id TEXT PRIMARY KEY,
                last_search_id TEXT NOT NULL,
                topic_signature_json TEXT NOT NULL,
                warm_packet_json TEXT NOT NULL,
                continuity_state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS hot_working_memory_state (
                brain_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                packet_json TEXT NOT NULL,
                contract_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (brain_id, thread_id)
            );

            CREATE TABLE IF NOT EXISTS search_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (search_id) REFERENCES search_sessions(search_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS correction_history (
                correction_id TEXT PRIMARY KEY,
                search_id TEXT,
                query_text TEXT NOT NULL,
                returned_answer TEXT NOT NULL,
                correction_text TEXT NOT NULL,
                correction_mode TEXT NOT NULL,
                used_evidence_node_ids_json TEXT NOT NULL,
                target_node_ids_json TEXT NOT NULL,
                action_summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS region_summaries (
                region_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS benchmark_runs (
                benchmark_id TEXT PRIMARY KEY,
                phase TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS maintenance_runs (
                maintenance_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                applied INTEGER NOT NULL,
                preview_only INTEGER NOT NULL,
                focus_node_id TEXT,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS heuristic_calibration_store (
                scope_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS heuristic_calibration_events (
                event_id TEXT PRIMARY KEY,
                event_kind TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_ref TEXT,
                scope_key TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                delta_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS landing_correction_events (
                event_id TEXT PRIMARY KEY,
                search_id TEXT NOT NULL,
                brain_id TEXT,
                query_text TEXT,
                retrieval_mode TEXT,
                query_class TEXT,
                goal TEXT,
                answer_field TEXT,
                guide_area TEXT,
                memory_type TEXT,
                radial_band TEXT,
                brain_revision TEXT,
                scope_key TEXT,
                path_id TEXT,
                landing_id TEXT,
                ai_spatial_path_id TEXT,
                ai_landing_region_ref TEXT,
                ai_landing_coordinate_json TEXT NOT NULL,
                snapped_region_ref TEXT,
                snapped_coordinate_json TEXT NOT NULL,
                bucket_key TEXT,
                snap_delta REAL,
                snap_status TEXT,
                snap_source TEXT,
                backend_changed_coordinate INTEGER NOT NULL,
                heuristic_provisional INTEGER NOT NULL,
                destination_reached INTEGER NOT NULL,
                changed_context_package INTEGER NOT NULL,
                successful INTEGER NOT NULL,
                traversed_node_ids_json TEXT NOT NULL,
                promoted_hot_node_ids_json TEXT NOT NULL,
                cold_reservoir_node_ids_json TEXT NOT NULL,
                excluded_node_ids_json TEXT NOT NULL,
                traversed_edge_count INTEGER NOT NULL,
                persistence_state TEXT NOT NULL,
                raw_event_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (search_id) REFERENCES search_sessions(search_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS memory_learning_events (
                event_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                brain_id TEXT NOT NULL,
                thread_id TEXT,
                operation_id TEXT,
                event_kind TEXT NOT NULL,
                event_source TEXT NOT NULL,
                source_unit_id TEXT,
                source_asset_id TEXT,
                preview_id TEXT,
                persisted_node_id TEXT,
                related_node_ids_json TEXT NOT NULL DEFAULT '[]',
                memory_act_type TEXT,
                claim_status TEXT,
                source_trust TEXT,
                confidence REAL,
                duplicate_targets_json TEXT NOT NULL DEFAULT '[]',
                contradiction_targets_json TEXT NOT NULL DEFAULT '[]',
                clarification_questions_json TEXT NOT NULL DEFAULT '[]',
                clarification_answers_json TEXT NOT NULL DEFAULT '{}',
                human_decision TEXT,
                apply_decision TEXT,
                sleep_evolve_priority REAL,
                matrix_hint_json TEXT NOT NULL DEFAULT '{}',
                topology_hint_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_references (
                source_ref_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                brain_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_uri TEXT,
                source_label TEXT,
                content_hash TEXT,
                fetch_snapshot_hash TEXT,
                original_storage_ref TEXT,
                redaction_policy TEXT NOT NULL DEFAULT 'metadata_only',
                source_trust TEXT NOT NULL DEFAULT 'unknown',
                created_at TEXT NOT NULL,
                last_verified_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS source_assets (
                asset_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                source_ref_id TEXT,
                brain_id TEXT NOT NULL,
                asset_kind TEXT NOT NULL,
                content_type TEXT,
                hash TEXT,
                byte_size INTEGER,
                width INTEGER,
                height INTEGER,
                storage_ref TEXT,
                ocr_text TEXT,
                vision_summary TEXT,
                requires_human_confirmation INTEGER NOT NULL DEFAULT 0,
                source_unit_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (source_ref_id) REFERENCES source_references(source_ref_id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS matrix_revisions (
                matrix_revision_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                brain_id TEXT NOT NULL,
                parent_revision_id TEXT,
                base_projection_version TEXT,
                semantic_axis_transform_json TEXT NOT NULL DEFAULT '{}',
                radial_band_transform_json TEXT NOT NULL DEFAULT '{}',
                guide_area_transform_json TEXT NOT NULL DEFAULT '{}',
                quality_before_json TEXT NOT NULL DEFAULT '{}',
                quality_after_json TEXT NOT NULL DEFAULT '{}',
                source_event_ids_json TEXT NOT NULL DEFAULT '[]',
                apply_policy TEXT NOT NULL DEFAULT 'preview_apply_required',
                rollback_payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                activated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS topology_field_revisions (
                topology_revision_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                brain_id TEXT NOT NULL,
                matrix_revision_id TEXT,
                attraction_priors_json TEXT NOT NULL DEFAULT '[]',
                repulsion_priors_json TEXT NOT NULL DEFAULT '[]',
                rotation_hints_json TEXT NOT NULL DEFAULT '[]',
                density_constraints_json TEXT NOT NULL DEFAULT '{}',
                bridge_corridors_json TEXT NOT NULL DEFAULT '[]',
                unstable_regions_json TEXT NOT NULL DEFAULT '[]',
                saturated_regions_json TEXT NOT NULL DEFAULT '[]',
                source_event_ids_json TEXT NOT NULL DEFAULT '[]',
                quality_before_json TEXT NOT NULL DEFAULT '{}',
                quality_after_json TEXT NOT NULL DEFAULT '{}',
                apply_policy TEXT NOT NULL DEFAULT 'preview_apply_required',
                rollback_payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                activated_at TEXT,
                FOREIGN KEY (matrix_revision_id) REFERENCES matrix_revisions(matrix_revision_id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS geometry_calibration_operations (
                operation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                brain_id TEXT NOT NULL,
                plan_signature TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                operation_state TEXT NOT NULL,
                matrix_revision_id TEXT NOT NULL,
                topology_revision_id TEXT NOT NULL,
                apply_event_id TEXT NOT NULL,
                rollback_event_id TEXT,
                rollback_snapshot_json TEXT NOT NULL,
                apply_result_json TEXT NOT NULL,
                rollback_result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                rolled_back_at TEXT,
                UNIQUE (brain_id, plan_signature)
            );

            CREATE TABLE IF NOT EXISTS memory_policy_revisions (
                policy_revision_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                brain_id TEXT NOT NULL,
                parent_policy_revision_id TEXT,
                policy_scope TEXT NOT NULL DEFAULT 'brain',
                ingest_rules_json TEXT NOT NULL DEFAULT '{}',
                retrieval_rules_json TEXT NOT NULL DEFAULT '{}',
                source_rules_json TEXT NOT NULL DEFAULT '{}',
                deduction_rules_json TEXT NOT NULL DEFAULT '{}',
                sleep_rules_json TEXT NOT NULL DEFAULT '{}',
                evolve_rules_json TEXT NOT NULL DEFAULT '{}',
                matrix_rules_json TEXT NOT NULL DEFAULT '{}',
                supporting_event_ids_json TEXT NOT NULL DEFAULT '[]',
                quality_before_json TEXT NOT NULL DEFAULT '{}',
                quality_after_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'candidate',
                apply_policy TEXT NOT NULL DEFAULT 'preview_apply_required',
                rollback_payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                activated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS cognitive_jobs (
                job_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                brain_id TEXT NOT NULL,
                trigger_source TEXT NOT NULL,
                requested_capability TEXT NOT NULL,
                required_plan TEXT NOT NULL,
                module_id TEXT,
                automation_level TEXT NOT NULL,
                mutation_policy TEXT NOT NULL,
                approval_required INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                priority REAL NOT NULL DEFAULT 0,
                idempotency_key TEXT,
                operation_id TEXT,
                parent_job_id TEXT,
                workspace_id TEXT,
                lease_id TEXT,
                lease_owner TEXT,
                lease_expires_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                payload_json TEXT NOT NULL DEFAULT '{}',
                policy_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                blocked_reasons_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                scheduled_for TEXT,
                started_at TEXT,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_nav_coarse
              ON nodes_nav (coarse_bucket_x, coarse_bucket_y, coarse_bucket_z);
            CREATE INDEX IF NOT EXISTS idx_nodes_nav_fine
              ON nodes_nav (fine_bucket_x, fine_bucket_y, fine_bucket_z);
            CREATE INDEX IF NOT EXISTS idx_nodes_nav_type
              ON nodes_nav (memory_type);
            CREATE INDEX IF NOT EXISTS idx_links_source
              ON links (source_id);
            CREATE INDEX IF NOT EXISTS idx_highways_source
              ON highways (source_id);
            CREATE INDEX IF NOT EXISTS idx_highways_target
              ON highways (target_id);
            CREATE INDEX IF NOT EXISTS idx_search_events_search_id_seq
              ON search_events (search_id, seq);
            CREATE INDEX IF NOT EXISTS idx_warm_thread_state_updated_at
              ON warm_thread_state (updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_hot_working_memory_updated_at
              ON hot_working_memory_state (updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_benchmark_runs_created_at
              ON benchmark_runs (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_maintenance_runs_created_at
              ON maintenance_runs (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_maintenance_runs_mode_applied
              ON maintenance_runs (mode, applied, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_heuristic_calibration_store_updated_at
              ON heuristic_calibration_store (updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_heuristic_calibration_events_created_at
              ON heuristic_calibration_events (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_heuristic_calibration_events_scope_key
              ON heuristic_calibration_events (scope_key, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_landing_correction_events_search
              ON landing_correction_events (search_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_landing_correction_events_scope
              ON landing_correction_events (scope_key, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_landing_correction_events_brain
              ON landing_correction_events (brain_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_learning_events_brain_created
              ON memory_learning_events (brain_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_learning_events_operation
              ON memory_learning_events (operation_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_learning_events_kind
              ON memory_learning_events (event_kind, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_learning_events_source_unit
              ON memory_learning_events (source_unit_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_cognitive_jobs_brain_status
              ON cognitive_jobs (brain_id, status, priority DESC, created_at ASC);
            CREATE INDEX IF NOT EXISTS idx_cognitive_jobs_idempotency
              ON cognitive_jobs (brain_id, idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_cognitive_jobs_capability
              ON cognitive_jobs (requested_capability, status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_source_references_brain_created
              ON source_references (brain_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_source_references_hash
              ON source_references (content_hash);
            CREATE INDEX IF NOT EXISTS idx_source_assets_brain_created
              ON source_assets (brain_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_source_assets_source_ref
              ON source_assets (source_ref_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_matrix_revisions_brain_active
              ON matrix_revisions (brain_id, activated_at DESC, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_topology_revisions_brain_active
              ON topology_field_revisions (brain_id, activated_at DESC, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_geometry_calibration_operations_brain_state
              ON geometry_calibration_operations (brain_id, operation_state, operation_id DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_policy_revisions_brain_status
              ON memory_policy_revisions (brain_id, status, activated_at DESC, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_policy_revisions_brain_active
              ON memory_policy_revisions (brain_id, activated_at DESC, created_at DESC);
                """
            )
            _ensure_column(conn, "search_sessions", "thread_id", "TEXT")
            _ensure_column(conn, "search_sessions", "plan_health_json", "TEXT")
            _ensure_column(conn, "search_sessions", "result_health_json", "TEXT")
            _backfill_recent_search_plan_health(conn, limit=200)
            _backfill_recent_search_result_health(conn, limit=50)
            conn.execute(
                """
            CREATE INDEX IF NOT EXISTS idx_search_sessions_thread_status
              ON search_sessions (thread_id, status, updated_at DESC)
            """
            )
            conn.execute(
                """
            CREATE INDEX IF NOT EXISTS idx_search_sessions_updated_at
              ON search_sessions (updated_at DESC)
            """
            )
            conn.execute(
                """
            CREATE INDEX IF NOT EXISTS idx_search_sessions_status_updated_at
              ON search_sessions (status, updated_at DESC)
            """
            )
            _ensure_column(conn, "nodes_nav", "temporal_role", "TEXT")
            _ensure_column(conn, "nodes_nav", "valid_from", "TEXT")
            _ensure_column(conn, "nodes_nav", "valid_to", "TEXT")
            _ensure_column(conn, "nodes_nav", "observed_at", "TEXT")
            _ensure_column(conn, "nodes_nav", "superseded_by", "TEXT")
            _ensure_column(conn, "nodes_nav", "obsoletes_json", "TEXT NOT NULL DEFAULT '[]'")
            _ensure_column(conn, "nodes_nav", "temporal_confidence", "REAL")
            _ensure_column(conn, "nodes_nav", "lifecycle_status", "TEXT NOT NULL DEFAULT 'active'")
            hygiene_columns_added = any(
                [
                    _ensure_column(conn, "nodes_nav", "source_trust", "TEXT NOT NULL DEFAULT 'user_asserted'"),
                    _ensure_column(conn, "nodes_nav", "claim_status", "TEXT NOT NULL DEFAULT 'fact'"),
                    _ensure_column(conn, "nodes_nav", "answer_eligible", "INTEGER NOT NULL DEFAULT 1"),
                    _ensure_column(conn, "nodes_nav", "profile_eligible", "INTEGER NOT NULL DEFAULT 1"),
                    _ensure_column(conn, "nodes_nav", "document_eligible", "INTEGER NOT NULL DEFAULT 1"),
                ]
            )
            _ensure_column(conn, "nodes_nav", "matrix_revision_id", "TEXT")
            _ensure_column(conn, "nodes_nav", "topology_revision_id", "TEXT")
            _ensure_column(conn, "nodes_nav", "matrix_calibration_plan_signature", "TEXT")
            _ensure_column(conn, "nodes_nav", "matrix_calibrated_at", "TEXT")
            _ensure_column(conn, "node_semantics", "document_role", "TEXT")
            _ensure_column(conn, "node_semantics", "document_anchor_id", "TEXT")
            _ensure_column(conn, "node_semantics", "document_chunk_index", "INTEGER")
            _ensure_column(conn, "node_semantics", "source_unit_id", "TEXT")
            _ensure_column(conn, "node_semantics", "source_unit_title", "TEXT")
            _ensure_column(conn, "node_semantics", "source_unit_kind", "TEXT")
            _ensure_column(conn, "node_semantics", "source_unit_role", "TEXT")
            _ensure_column(conn, "node_semantics", "promotion_role", "TEXT")
            _ensure_column(conn, "node_semantics", "source_unit_formation_strategy", "TEXT")
            _ensure_column(conn, "node_semantics", "retrieval_affordance_json", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(conn, "node_semantics", "retrieval_aliases_json", "TEXT NOT NULL DEFAULT '[]'")
            if hygiene_columns_added or str(os.getenv("AGVM_RECONCILE_NODE_HYGIENE_ON_BOOTSTRAP", "")).strip().lower() in {"1", "true", "yes", "on"}:
                _reconcile_node_hygiene(conn)
            conn.commit()
        _BOOTSTRAPPED_RUNTIME_STORE_PATHS.add(sqlite_path_key)
    ensure_legacy_exports()


def ensure_legacy_exports() -> None:
    graph_path = current_graph_path()
    graph_view_path = current_graph_view_path()
    index_path = current_index_path()
    atlas_path = current_atlas_path()
    if not graph_path.exists():
        atomic_write_json(graph_path, empty_graph())
    if not graph_view_path.exists():
        atomic_write_json(graph_view_path, empty_graph_view())
    if not index_path.exists():
        atomic_write_json(index_path, empty_index())
    if not atlas_path.exists():
        atomic_write_json(atlas_path, empty_atlas())


def reset_legacy_exports() -> None:
    atomic_write_json(current_graph_path(), empty_graph())
    atomic_write_json(current_graph_view_path(), empty_graph_view())
    atomic_write_json(current_index_path(), empty_index())
    atomic_write_json(current_atlas_path(), empty_atlas())


def _active_brain_id(brain_id: str | None = None) -> str:
    normalized = str(brain_id or current_brain_id() or "").strip()
    return normalized or "default"


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _memory_learning_event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": str(row["schema_version"]),
        "event_id": str(row["event_id"]),
        "brain_id": str(row["brain_id"]),
        "thread_id": _text_or_none(row["thread_id"]),
        "operation_id": _text_or_none(row["operation_id"]),
        "event_kind": str(row["event_kind"]),
        "event_source": str(row["event_source"]),
        "source_unit_id": _text_or_none(row["source_unit_id"]),
        "source_asset_id": _text_or_none(row["source_asset_id"]),
        "preview_id": _text_or_none(row["preview_id"]),
        "persisted_node_id": _text_or_none(row["persisted_node_id"]),
        "related_node_ids": list(_json_load(row["related_node_ids_json"], [])),
        "memory_act_type": _text_or_none(row["memory_act_type"]),
        "claim_status": _text_or_none(row["claim_status"]),
        "source_trust": _text_or_none(row["source_trust"]),
        "confidence": _float_or_none(row["confidence"]),
        "duplicate_targets": list(_json_load(row["duplicate_targets_json"], [])),
        "contradiction_targets": list(_json_load(row["contradiction_targets_json"], [])),
        "clarification_questions": list(_json_load(row["clarification_questions_json"], [])),
        "clarification_answers": dict(_json_load(row["clarification_answers_json"], {})),
        "human_decision": _text_or_none(row["human_decision"]),
        "apply_decision": _text_or_none(row["apply_decision"]),
        "sleep_evolve_priority": _float_or_none(row["sleep_evolve_priority"]),
        "matrix_hint": dict(_json_load(row["matrix_hint_json"], {})),
        "topology_hint": dict(_json_load(row["topology_hint_json"], {})),
        "payload": dict(_json_load(row["payload_json"], {})),
        "created_at": str(row["created_at"]),
    }


def append_memory_learning_event(event: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload = {**_json_dict(event), **dict(overrides)}
    event_kind = str(payload.get("event_kind") or "").strip()
    if not event_kind:
        raise ValueError("memory_learning_event_kind_required")
    event_id = str(payload.get("event_id") or f"memory_learning_event::{uuid.uuid4()}").strip()
    brain_id = _active_brain_id(_text_or_none(payload.get("brain_id")))
    created_at = str(payload.get("created_at") or utc_timestamp())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO memory_learning_events (
                event_id, schema_version, brain_id, thread_id, operation_id,
                event_kind, event_source, source_unit_id, source_asset_id,
                preview_id, persisted_node_id, related_node_ids_json,
                memory_act_type, claim_status, source_trust, confidence,
                duplicate_targets_json, contradiction_targets_json,
                clarification_questions_json, clarification_answers_json,
                human_decision, apply_decision, sleep_evolve_priority,
                matrix_hint_json, topology_hint_json, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                str(payload.get("schema_version") or MEMORY_LEARNING_EVENT_SCHEMA_VERSION),
                brain_id,
                _text_or_none(payload.get("thread_id")),
                _text_or_none(payload.get("operation_id")),
                event_kind,
                str(payload.get("event_source") or "core"),
                _text_or_none(payload.get("source_unit_id")),
                _text_or_none(payload.get("source_asset_id")),
                _text_or_none(payload.get("preview_id")),
                _text_or_none(payload.get("persisted_node_id")),
                _json_dump(_json_list(payload.get("related_node_ids"))),
                _text_or_none(payload.get("memory_act_type")),
                _text_or_none(payload.get("claim_status")),
                _text_or_none(payload.get("source_trust")),
                _float_or_none(payload.get("confidence")),
                _json_dump(_json_list(payload.get("duplicate_targets"))),
                _json_dump(_json_list(payload.get("contradiction_targets"))),
                _json_dump(_json_list(payload.get("clarification_questions"))),
                _json_dump(_json_dict(payload.get("clarification_answers"))),
                _text_or_none(payload.get("human_decision")),
                _text_or_none(payload.get("apply_decision")),
                _float_or_none(payload.get("sleep_evolve_priority")),
                _json_dump(_json_dict(payload.get("matrix_hint"))),
                _json_dump(_json_dict(payload.get("topology_hint"))),
                _json_dump(_json_dict(payload.get("payload"))),
                created_at,
            ),
        )
        conn.commit()
    fetched = fetch_memory_learning_event(event_id, brain_id=brain_id)
    if fetched is None:
        raise RuntimeError(f"memory_learning_event_not_found_after_insert:{event_id}")
    return fetched


def fetch_memory_learning_event(event_id: str, *, brain_id: str | None = None) -> dict[str, Any] | None:
    normalized_event_id = str(event_id or "").strip()
    if not normalized_event_id:
        return None
    params: list[Any] = [normalized_event_id]
    sql = """
        SELECT *
        FROM memory_learning_events
        WHERE event_id = ?
    """
    if brain_id:
        sql += " AND brain_id = ?"
        params.append(_active_brain_id(brain_id))
    with connect_readonly() as conn:
        row = conn.execute(sql, params).fetchone()
    return _memory_learning_event_from_row(row) if row else None


def fetch_memory_learning_events(
    *,
    brain_id: str | None = None,
    event_kind: str | None = None,
    operation_id: str | None = None,
    source_unit_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if brain_id:
        clauses.append("brain_id = ?")
        params.append(_active_brain_id(brain_id))
    if event_kind:
        clauses.append("event_kind = ?")
        params.append(str(event_kind).strip())
    if operation_id:
        clauses.append("operation_id = ?")
        params.append(str(operation_id).strip())
    if source_unit_id:
        clauses.append("source_unit_id = ?")
        params.append(str(source_unit_id).strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit or 100), 1000)))
    with connect_readonly() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM memory_learning_events
            {where}
            ORDER BY created_at DESC, event_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_memory_learning_event_from_row(row) for row in rows]


def _cognitive_job_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": str(row["schema_version"]),
        "job_id": str(row["job_id"]),
        "brain_id": str(row["brain_id"]),
        "trigger_source": str(row["trigger_source"]),
        "requested_capability": str(row["requested_capability"]),
        "required_plan": str(row["required_plan"]),
        "module_id": _text_or_none(row["module_id"]),
        "automation_level": str(row["automation_level"]),
        "mutation_policy": str(row["mutation_policy"]),
        "approval_required": bool(row["approval_required"]),
        "status": str(row["status"]),
        "priority": _float_or_none(row["priority"]) or 0.0,
        "idempotency_key": _text_or_none(row["idempotency_key"]),
        "operation_id": _text_or_none(row["operation_id"]),
        "parent_job_id": _text_or_none(row["parent_job_id"]),
        "workspace_id": _text_or_none(row["workspace_id"]),
        "lease_id": _text_or_none(row["lease_id"]),
        "lease_owner": _text_or_none(row["lease_owner"]),
        "lease_expires_at": _text_or_none(row["lease_expires_at"]),
        "attempts": _int_or_none(row["attempts"]) or 0,
        "max_attempts": _int_or_none(row["max_attempts"]) or 3,
        "payload": dict(_json_load(row["payload_json"], {})),
        "policy": dict(_json_load(row["policy_json"], {})),
        "result": dict(_json_load(row["result_json"], {})),
        "blocked_reasons": list(_json_load(row["blocked_reasons_json"], [])),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "scheduled_for": _text_or_none(row["scheduled_for"]),
        "started_at": _text_or_none(row["started_at"]),
        "completed_at": _text_or_none(row["completed_at"]),
    }


def fetch_cognitive_job(job_id: str, *, brain_id: str | None = None) -> dict[str, Any] | None:
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return None
    bootstrap_runtime_store()
    params: list[Any] = [normalized_job_id]
    sql = """
        SELECT *
        FROM cognitive_jobs
        WHERE job_id = ?
    """
    if brain_id:
        sql += " AND brain_id = ?"
        params.append(_active_brain_id(brain_id))
    with connect_readonly() as conn:
        row = conn.execute(sql, params).fetchone()
    return _cognitive_job_from_row(row) if row else None


def fetch_cognitive_jobs(
    *,
    brain_id: str | None = None,
    status: str | None = None,
    requested_capability: str | None = None,
    operation_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    bootstrap_runtime_store()
    params: list[Any] = []
    clauses: list[str] = []
    if brain_id:
        clauses.append("brain_id = ?")
        params.append(_active_brain_id(brain_id))
    if status:
        clauses.append("status = ?")
        params.append(str(status).strip())
    if requested_capability:
        clauses.append("requested_capability = ?")
        params.append(str(requested_capability).strip())
    if operation_id:
        clauses.append("operation_id = ?")
        params.append(str(operation_id).strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit or 100), 1000)))
    with connect_readonly() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM cognitive_jobs
            {where}
            ORDER BY priority DESC, created_at ASC, job_id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_cognitive_job_from_row(row) for row in rows]


def schedule_cognitive_job(
    job: dict[str, Any] | None = None,
    *,
    policy_context: dict[str, Any] | None = None,
    record_event: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    bootstrap_runtime_store()
    normalized = normalize_cognitive_job(job, **overrides)
    normalized["brain_id"] = _active_brain_id(_text_or_none(normalized.get("brain_id")))
    decision = evaluate_cognitive_job_policy(normalized, policy_context or {})
    normalized["policy"] = {**dict(normalized.get("policy") or {}), "decision": decision}
    normalized["blocked_reasons"] = list(decision.get("blocked_reasons") or [])
    if not decision.get("allowed"):
        normalized["status"] = "blocked"
    elif normalized.get("status") == "blocked":
        normalized["status"] = "queued"
    now = utc_timestamp()
    normalized["updated_at"] = now
    normalized["created_at"] = str(normalized.get("created_at") or now)

    existing: dict[str, Any] | None = None
    if normalized.get("idempotency_key"):
        with connect_readonly() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM cognitive_jobs
                WHERE brain_id = ? AND idempotency_key = ?
                ORDER BY created_at DESC, job_id DESC
                LIMIT 1
                """,
                (normalized["brain_id"], normalized["idempotency_key"]),
            ).fetchone()
        if row:
            existing = _cognitive_job_from_row(row)

    if existing is not None:
        return {**existing, "idempotency_reused": True}

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO cognitive_jobs (
                job_id, schema_version, brain_id, trigger_source,
                requested_capability, required_plan, module_id, automation_level,
                mutation_policy, approval_required, status, priority,
                idempotency_key, operation_id, parent_job_id, workspace_id,
                lease_id, lease_owner, lease_expires_at, attempts, max_attempts,
                payload_json, policy_json, result_json, blocked_reasons_json,
                created_at, updated_at, scheduled_for, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized["job_id"],
                str(normalized.get("schema_version") or COGNITIVE_JOB_SCHEMA_VERSION),
                normalized["brain_id"],
                normalized["trigger_source"],
                normalized["requested_capability"],
                normalized["required_plan"],
                _text_or_none(normalized.get("module_id")),
                normalized["automation_level"],
                normalized["mutation_policy"],
                1 if normalized.get("approval_required") else 0,
                normalized["status"],
                _float_or_none(normalized.get("priority")) or 0.0,
                _text_or_none(normalized.get("idempotency_key")),
                _text_or_none(normalized.get("operation_id")),
                _text_or_none(normalized.get("parent_job_id")),
                _text_or_none(normalized.get("workspace_id")),
                _text_or_none(normalized.get("lease_id")),
                _text_or_none(normalized.get("lease_owner")),
                _text_or_none(normalized.get("lease_expires_at")),
                _int_or_none(normalized.get("attempts")) or 0,
                _int_or_none(normalized.get("max_attempts")) or 3,
                _json_dump(_json_dict(normalized.get("payload"))),
                _json_dump(_json_dict(normalized.get("policy"))),
                _json_dump(_json_dict(normalized.get("result"))),
                _json_dump(_json_list(normalized.get("blocked_reasons"))),
                normalized["created_at"],
                normalized["updated_at"],
                _text_or_none(normalized.get("scheduled_for")),
                _text_or_none(normalized.get("started_at")),
                _text_or_none(normalized.get("completed_at")),
            ),
        )
        conn.commit()
    fetched = fetch_cognitive_job(str(normalized["job_id"]), brain_id=str(normalized["brain_id"]))
    if fetched is None:
        raise RuntimeError(f"cognitive_job_not_found_after_insert:{normalized['job_id']}")
    if record_event:
        append_memory_learning_event(
            cognitive_job_learning_event(
                fetched,
                event_kind="background_job_scheduled" if fetched["status"] != "blocked" else "background_job_blocked",
                payload={"policy_decision": fetched["policy"].get("decision")},
            )
        )
    return fetched


def lease_next_cognitive_job(
    *,
    worker_id: str,
    brain_id: str | None = None,
    requested_capability: str | None = None,
    lease_seconds: int = 300,
) -> dict[str, Any] | None:
    bootstrap_runtime_store()
    owner = str(worker_id or "").strip()
    if not owner:
        raise ValueError("cognitive_job_worker_id_required")
    now = utc_timestamp()
    lease_id = f"lease::{uuid.uuid4()}"
    lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=max(1, min(int(lease_seconds or 300), 86400)))).isoformat()
    clauses = ["status = 'queued'", "(scheduled_for IS NULL OR scheduled_for <= ?)"]
    params: list[Any] = [now]
    if brain_id:
        clauses.append("brain_id = ?")
        params.append(_active_brain_id(brain_id))
    if requested_capability:
        clauses.append("requested_capability = ?")
        params.append(str(requested_capability).strip())
    where = " AND ".join(clauses)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT job_id
            FROM cognitive_jobs
            WHERE {where}
            ORDER BY priority DESC, created_at ASC, job_id ASC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if not row:
            return None
        job_id = str(row["job_id"])
        conn.execute(
            """
            UPDATE cognitive_jobs
            SET status = 'leased',
                lease_id = ?,
                lease_owner = ?,
                lease_expires_at = ?,
                attempts = attempts + 1,
                started_at = COALESCE(started_at, ?),
                updated_at = ?
            WHERE job_id = ? AND status = 'queued'
            """,
            (lease_id, owner, lease_expires_at, now, now, job_id),
        )
        conn.commit()
    return fetch_cognitive_job(job_id, brain_id=brain_id)


def complete_cognitive_job(
    job_id: str,
    *,
    lease_id: str | None = None,
    result: dict[str, Any] | None = None,
    status: str = "completed",
    record_event: bool = True,
) -> dict[str, Any] | None:
    normalized_status = str(status or "completed").strip()
    if normalized_status not in {"completed", "failed"}:
        raise ValueError("cognitive_job_complete_status_must_be_completed_or_failed")
    now = utc_timestamp()
    params: list[Any] = [
        normalized_status,
        _json_dump(_json_dict(result)),
        now,
        now,
        str(job_id or "").strip(),
    ]
    lease_clause = ""
    if lease_id:
        lease_clause = " AND lease_id = ?"
        params.append(str(lease_id).strip())
    with connect() as conn:
        conn.execute(
            f"""
            UPDATE cognitive_jobs
            SET status = ?,
                result_json = ?,
                completed_at = ?,
                updated_at = ?
            WHERE job_id = ?{lease_clause}
              AND status IN ('leased', 'running', 'queued')
            """,
            params,
        )
        conn.commit()
    fetched = fetch_cognitive_job(str(job_id or ""))
    if fetched and record_event and fetched["status"] == normalized_status:
        append_memory_learning_event(
            cognitive_job_learning_event(
                fetched,
                event_kind="background_job_completed",
                payload={"result_summary": _json_dict(result)},
            )
        )
    return fetched


def cancel_cognitive_job(job_id: str, *, reason: str = "", record_event: bool = True) -> dict[str, Any] | None:
    now = utc_timestamp()
    with connect() as conn:
        conn.execute(
            """
            UPDATE cognitive_jobs
            SET status = 'cancelled',
                result_json = ?,
                completed_at = ?,
                updated_at = ?
            WHERE job_id = ?
              AND status IN ('queued', 'blocked', 'leased', 'running', 'failed')
            """,
            (_json_dump({"reason": str(reason or "cancelled")}), now, now, str(job_id or "").strip()),
        )
        conn.commit()
    fetched = fetch_cognitive_job(str(job_id or ""))
    if fetched and record_event and fetched["status"] == "cancelled":
        append_memory_learning_event(
            cognitive_job_learning_event(
                fetched,
                event_kind="background_job_cancelled",
                payload={"reason": str(reason or "cancelled")},
            )
        )
    return fetched


def cognitive_job_capability_report(*, brain_id: str | None = None) -> dict[str, Any]:
    bootstrap_runtime_store()
    with connect_readonly() as conn:
        row = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'cognitive_jobs'
            """
        ).fetchone()
    return build_cognitive_job_capability_report(
        storage_backend="sqlite",
        table_present=bool(row),
        writable=True,
        brain_id=_active_brain_id(brain_id),
    )


def _source_reference_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": str(row["schema_version"]),
        "source_ref_id": str(row["source_ref_id"]),
        "brain_id": str(row["brain_id"]),
        "source_kind": str(row["source_kind"]),
        "source_uri": _text_or_none(row["source_uri"]),
        "source_label": _text_or_none(row["source_label"]),
        "content_hash": _text_or_none(row["content_hash"]),
        "fetch_snapshot_hash": _text_or_none(row["fetch_snapshot_hash"]),
        "original_storage_ref": _text_or_none(row["original_storage_ref"]),
        "redaction_policy": str(row["redaction_policy"]),
        "source_trust": str(row["source_trust"]),
        "created_at": str(row["created_at"]),
        "last_verified_at": _text_or_none(row["last_verified_at"]),
        "metadata": dict(_json_load(row["metadata_json"], {})),
    }


def upsert_source_reference(source_reference: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload = {**_json_dict(source_reference), **dict(overrides)}
    source_ref_id = str(payload.get("source_ref_id") or f"source_ref::{uuid.uuid4()}").strip()
    brain_id = _active_brain_id(_text_or_none(payload.get("brain_id")))
    created_at = str(payload.get("created_at") or utc_timestamp())
    with connect() as conn:
        existing = conn.execute("SELECT created_at FROM source_references WHERE source_ref_id = ?", (source_ref_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO source_references (
                source_ref_id, schema_version, brain_id, source_kind, source_uri,
                source_label, content_hash, fetch_snapshot_hash, original_storage_ref,
                redaction_policy, source_trust, created_at, last_verified_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_ref_id) DO UPDATE SET
                schema_version=excluded.schema_version,
                brain_id=excluded.brain_id,
                source_kind=excluded.source_kind,
                source_uri=excluded.source_uri,
                source_label=excluded.source_label,
                content_hash=excluded.content_hash,
                fetch_snapshot_hash=excluded.fetch_snapshot_hash,
                original_storage_ref=excluded.original_storage_ref,
                redaction_policy=excluded.redaction_policy,
                source_trust=excluded.source_trust,
                last_verified_at=excluded.last_verified_at,
                metadata_json=excluded.metadata_json
            """,
            (
                source_ref_id,
                str(payload.get("schema_version") or SOURCE_REFERENCE_SCHEMA_VERSION),
                brain_id,
                str(payload.get("source_kind") or "unknown"),
                _text_or_none(payload.get("source_uri")),
                _text_or_none(payload.get("source_label")),
                _text_or_none(payload.get("content_hash")),
                _text_or_none(payload.get("fetch_snapshot_hash")),
                _text_or_none(payload.get("original_storage_ref")),
                str(payload.get("redaction_policy") or "metadata_only"),
                str(payload.get("source_trust") or "unknown"),
                str(existing["created_at"]) if existing else created_at,
                _text_or_none(payload.get("last_verified_at")),
                _json_dump(_json_dict(payload.get("metadata"))),
            ),
        )
        conn.commit()
    fetched = fetch_source_reference(source_ref_id, brain_id=brain_id)
    if fetched is None:
        raise RuntimeError(f"source_reference_not_found_after_upsert:{source_ref_id}")
    return fetched


def fetch_source_reference(source_ref_id: str, *, brain_id: str | None = None) -> dict[str, Any] | None:
    normalized = str(source_ref_id or "").strip()
    if not normalized:
        return None
    params: list[Any] = [normalized]
    sql = "SELECT * FROM source_references WHERE source_ref_id = ?"
    if brain_id:
        sql += " AND brain_id = ?"
        params.append(_active_brain_id(brain_id))
    with connect_readonly() as conn:
        row = conn.execute(sql, params).fetchone()
    return _source_reference_from_row(row) if row else None


def fetch_source_references(*, brain_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if brain_id:
        where = "WHERE brain_id = ?"
        params.append(_active_brain_id(brain_id))
    params.append(max(1, min(int(limit or 100), 1000)))
    with connect_readonly() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM source_references
            {where}
            ORDER BY created_at DESC, source_ref_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_source_reference_from_row(row) for row in rows]


def _source_asset_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": str(row["schema_version"]),
        "asset_id": str(row["asset_id"]),
        "source_ref_id": _text_or_none(row["source_ref_id"]),
        "brain_id": str(row["brain_id"]),
        "asset_kind": str(row["asset_kind"]),
        "content_type": _text_or_none(row["content_type"]),
        "hash": _text_or_none(row["hash"]),
        "byte_size": _int_or_none(row["byte_size"]),
        "width": _int_or_none(row["width"]),
        "height": _int_or_none(row["height"]),
        "storage_ref": _text_or_none(row["storage_ref"]),
        "ocr_text": _text_or_none(row["ocr_text"]),
        "vision_summary": _text_or_none(row["vision_summary"]),
        "requires_human_confirmation": bool(row["requires_human_confirmation"]),
        "source_unit_id": _text_or_none(row["source_unit_id"]),
        "created_at": str(row["created_at"]),
        "metadata": dict(_json_load(row["metadata_json"], {})),
    }


def upsert_source_asset(source_asset: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload = {**_json_dict(source_asset), **dict(overrides)}
    asset_id = str(payload.get("asset_id") or f"source_asset::{uuid.uuid4()}").strip()
    brain_id = _active_brain_id(_text_or_none(payload.get("brain_id")))
    created_at = str(payload.get("created_at") or utc_timestamp())
    with connect() as conn:
        existing = conn.execute("SELECT created_at FROM source_assets WHERE asset_id = ?", (asset_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO source_assets (
                asset_id, schema_version, source_ref_id, brain_id, asset_kind,
                content_type, hash, byte_size, width, height, storage_ref,
                ocr_text, vision_summary, requires_human_confirmation,
                source_unit_id, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                schema_version=excluded.schema_version,
                source_ref_id=excluded.source_ref_id,
                brain_id=excluded.brain_id,
                asset_kind=excluded.asset_kind,
                content_type=excluded.content_type,
                hash=excluded.hash,
                byte_size=excluded.byte_size,
                width=excluded.width,
                height=excluded.height,
                storage_ref=excluded.storage_ref,
                ocr_text=excluded.ocr_text,
                vision_summary=excluded.vision_summary,
                requires_human_confirmation=excluded.requires_human_confirmation,
                source_unit_id=excluded.source_unit_id,
                metadata_json=excluded.metadata_json
            """,
            (
                asset_id,
                str(payload.get("schema_version") or SOURCE_ASSET_SCHEMA_VERSION),
                _text_or_none(payload.get("source_ref_id")),
                brain_id,
                str(payload.get("asset_kind") or "unknown"),
                _text_or_none(payload.get("content_type")),
                _text_or_none(payload.get("hash")),
                _int_or_none(payload.get("byte_size")),
                _int_or_none(payload.get("width")),
                _int_or_none(payload.get("height")),
                _text_or_none(payload.get("storage_ref")),
                _text_or_none(payload.get("ocr_text")),
                _text_or_none(payload.get("vision_summary")),
                1 if payload.get("requires_human_confirmation") else 0,
                _text_or_none(payload.get("source_unit_id")),
                str(existing["created_at"]) if existing else created_at,
                _json_dump(_json_dict(payload.get("metadata"))),
            ),
        )
        conn.commit()
    fetched = fetch_source_asset(asset_id, brain_id=brain_id)
    if fetched is None:
        raise RuntimeError(f"source_asset_not_found_after_upsert:{asset_id}")
    return fetched


def fetch_source_asset(asset_id: str, *, brain_id: str | None = None) -> dict[str, Any] | None:
    normalized = str(asset_id or "").strip()
    if not normalized:
        return None
    params: list[Any] = [normalized]
    sql = "SELECT * FROM source_assets WHERE asset_id = ?"
    if brain_id:
        sql += " AND brain_id = ?"
        params.append(_active_brain_id(brain_id))
    with connect_readonly() as conn:
        row = conn.execute(sql, params).fetchone()
    return _source_asset_from_row(row) if row else None


def fetch_source_assets(*, brain_id: str | None = None, source_ref_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if brain_id:
        clauses.append("brain_id = ?")
        params.append(_active_brain_id(brain_id))
    if source_ref_id:
        clauses.append("source_ref_id = ?")
        params.append(str(source_ref_id).strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit or 100), 1000)))
    with connect_readonly() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM source_assets
            {where}
            ORDER BY created_at DESC, asset_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_source_asset_from_row(row) for row in rows]


def _matrix_revision_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": str(row["schema_version"]),
        "matrix_revision_id": str(row["matrix_revision_id"]),
        "brain_id": str(row["brain_id"]),
        "parent_revision_id": _text_or_none(row["parent_revision_id"]),
        "base_projection_version": _text_or_none(row["base_projection_version"]),
        "semantic_axis_transform": dict(_json_load(row["semantic_axis_transform_json"], {})),
        "radial_band_transform": dict(_json_load(row["radial_band_transform_json"], {})),
        "guide_area_transform": dict(_json_load(row["guide_area_transform_json"], {})),
        "quality_before": dict(_json_load(row["quality_before_json"], {})),
        "quality_after": dict(_json_load(row["quality_after_json"], {})),
        "source_event_ids": list(_json_load(row["source_event_ids_json"], [])),
        "apply_policy": str(row["apply_policy"]),
        "rollback_payload": dict(_json_load(row["rollback_payload_json"], {})),
        "created_at": str(row["created_at"]),
        "activated_at": _text_or_none(row["activated_at"]),
    }


def store_matrix_revision(revision: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload = {**_json_dict(revision), **dict(overrides)}
    matrix_revision_id = str(payload.get("matrix_revision_id") or f"matrix_revision::{uuid.uuid4()}").strip()
    brain_id = _active_brain_id(_text_or_none(payload.get("brain_id")))
    created_at = str(payload.get("created_at") or utc_timestamp())
    with connect() as conn:
        existing = conn.execute("SELECT created_at FROM matrix_revisions WHERE matrix_revision_id = ?", (matrix_revision_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO matrix_revisions (
                matrix_revision_id, schema_version, brain_id, parent_revision_id,
                base_projection_version, semantic_axis_transform_json,
                radial_band_transform_json, guide_area_transform_json,
                quality_before_json, quality_after_json, source_event_ids_json,
                apply_policy, rollback_payload_json, created_at, activated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(matrix_revision_id) DO UPDATE SET
                schema_version=excluded.schema_version,
                brain_id=excluded.brain_id,
                parent_revision_id=excluded.parent_revision_id,
                base_projection_version=excluded.base_projection_version,
                semantic_axis_transform_json=excluded.semantic_axis_transform_json,
                radial_band_transform_json=excluded.radial_band_transform_json,
                guide_area_transform_json=excluded.guide_area_transform_json,
                quality_before_json=excluded.quality_before_json,
                quality_after_json=excluded.quality_after_json,
                source_event_ids_json=excluded.source_event_ids_json,
                apply_policy=excluded.apply_policy,
                rollback_payload_json=excluded.rollback_payload_json,
                activated_at=excluded.activated_at
            """,
            (
                matrix_revision_id,
                str(payload.get("schema_version") or MATRIX_REVISION_SCHEMA_VERSION),
                brain_id,
                _text_or_none(payload.get("parent_revision_id")),
                _text_or_none(payload.get("base_projection_version")),
                _json_dump(_json_dict(payload.get("semantic_axis_transform"))),
                _json_dump(_json_dict(payload.get("radial_band_transform"))),
                _json_dump(_json_dict(payload.get("guide_area_transform"))),
                _json_dump(_json_dict(payload.get("quality_before"))),
                _json_dump(_json_dict(payload.get("quality_after"))),
                _json_dump(_json_list(payload.get("source_event_ids"))),
                str(payload.get("apply_policy") or "preview_apply_required"),
                _json_dump(_json_dict(payload.get("rollback_payload"))),
                str(existing["created_at"]) if existing else created_at,
                _text_or_none(payload.get("activated_at")),
            ),
        )
        conn.commit()
    fetched = fetch_matrix_revision(matrix_revision_id, brain_id=brain_id)
    if fetched is None:
        raise RuntimeError(f"matrix_revision_not_found_after_store:{matrix_revision_id}")
    return fetched


def fetch_matrix_revision(matrix_revision_id: str, *, brain_id: str | None = None) -> dict[str, Any] | None:
    normalized = str(matrix_revision_id or "").strip()
    if not normalized:
        return None
    params: list[Any] = [normalized]
    sql = "SELECT * FROM matrix_revisions WHERE matrix_revision_id = ?"
    if brain_id:
        sql += " AND brain_id = ?"
        params.append(_active_brain_id(brain_id))
    with connect_readonly() as conn:
        row = conn.execute(sql, params).fetchone()
    return _matrix_revision_from_row(row) if row else None


def activate_matrix_revision(matrix_revision_id: str, *, brain_id: str | None = None, activated_at: str | None = None) -> dict[str, Any] | None:
    normalized = str(matrix_revision_id or "").strip()
    if not normalized:
        return None
    timestamp = str(activated_at or utc_timestamp())
    with connect() as conn:
        conn.execute(
            """
            UPDATE matrix_revisions
            SET activated_at = ?
            WHERE matrix_revision_id = ?
              AND brain_id = ?
            """,
            (timestamp, normalized, _active_brain_id(brain_id)),
        )
        conn.commit()
    return fetch_matrix_revision(normalized, brain_id=brain_id)


def fetch_matrix_revisions(*, brain_id: str | None = None, active_only: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if brain_id:
        clauses.append("brain_id = ?")
        params.append(_active_brain_id(brain_id))
    if active_only:
        clauses.append("activated_at IS NOT NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit or 20), 1000)))
    with connect_readonly() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM matrix_revisions
            {where}
            ORDER BY activated_at DESC, created_at DESC, matrix_revision_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_matrix_revision_from_row(row) for row in rows]


def fetch_active_matrix_revision(*, brain_id: str | None = None) -> dict[str, Any] | None:
    revisions = fetch_matrix_revisions(brain_id=brain_id, active_only=True, limit=1)
    return revisions[0] if revisions else None


def _topology_field_revision_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": str(row["schema_version"]),
        "topology_revision_id": str(row["topology_revision_id"]),
        "brain_id": str(row["brain_id"]),
        "matrix_revision_id": _text_or_none(row["matrix_revision_id"]),
        "attraction_priors": list(_json_load(row["attraction_priors_json"], [])),
        "repulsion_priors": list(_json_load(row["repulsion_priors_json"], [])),
        "rotation_hints": list(_json_load(row["rotation_hints_json"], [])),
        "density_constraints": dict(_json_load(row["density_constraints_json"], {})),
        "bridge_corridors": list(_json_load(row["bridge_corridors_json"], [])),
        "unstable_regions": list(_json_load(row["unstable_regions_json"], [])),
        "saturated_regions": list(_json_load(row["saturated_regions_json"], [])),
        "source_event_ids": list(_json_load(row["source_event_ids_json"], [])),
        "quality_before": dict(_json_load(row["quality_before_json"], {})),
        "quality_after": dict(_json_load(row["quality_after_json"], {})),
        "apply_policy": str(row["apply_policy"]),
        "rollback_payload": dict(_json_load(row["rollback_payload_json"], {})),
        "created_at": str(row["created_at"]),
        "activated_at": _text_or_none(row["activated_at"]),
    }


def store_topology_field_revision(revision: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload = {**_json_dict(revision), **dict(overrides)}
    topology_revision_id = str(payload.get("topology_revision_id") or f"topology_revision::{uuid.uuid4()}").strip()
    brain_id = _active_brain_id(_text_or_none(payload.get("brain_id")))
    created_at = str(payload.get("created_at") or utc_timestamp())
    with connect() as conn:
        existing = conn.execute(
            "SELECT created_at FROM topology_field_revisions WHERE topology_revision_id = ?",
            (topology_revision_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO topology_field_revisions (
                topology_revision_id, schema_version, brain_id, matrix_revision_id,
                attraction_priors_json, repulsion_priors_json, rotation_hints_json,
                density_constraints_json, bridge_corridors_json, unstable_regions_json,
                saturated_regions_json, source_event_ids_json, quality_before_json,
                quality_after_json, apply_policy, rollback_payload_json, created_at,
                activated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(topology_revision_id) DO UPDATE SET
                schema_version=excluded.schema_version,
                brain_id=excluded.brain_id,
                matrix_revision_id=excluded.matrix_revision_id,
                attraction_priors_json=excluded.attraction_priors_json,
                repulsion_priors_json=excluded.repulsion_priors_json,
                rotation_hints_json=excluded.rotation_hints_json,
                density_constraints_json=excluded.density_constraints_json,
                bridge_corridors_json=excluded.bridge_corridors_json,
                unstable_regions_json=excluded.unstable_regions_json,
                saturated_regions_json=excluded.saturated_regions_json,
                source_event_ids_json=excluded.source_event_ids_json,
                quality_before_json=excluded.quality_before_json,
                quality_after_json=excluded.quality_after_json,
                apply_policy=excluded.apply_policy,
                rollback_payload_json=excluded.rollback_payload_json,
                activated_at=excluded.activated_at
            """,
            (
                topology_revision_id,
                str(payload.get("schema_version") or TOPOLOGY_FIELD_REVISION_SCHEMA_VERSION),
                brain_id,
                _text_or_none(payload.get("matrix_revision_id")),
                _json_dump(_json_list(payload.get("attraction_priors"))),
                _json_dump(_json_list(payload.get("repulsion_priors"))),
                _json_dump(_json_list(payload.get("rotation_hints"))),
                _json_dump(_json_dict(payload.get("density_constraints"))),
                _json_dump(_json_list(payload.get("bridge_corridors"))),
                _json_dump(_json_list(payload.get("unstable_regions"))),
                _json_dump(_json_list(payload.get("saturated_regions"))),
                _json_dump(_json_list(payload.get("source_event_ids"))),
                _json_dump(_json_dict(payload.get("quality_before"))),
                _json_dump(_json_dict(payload.get("quality_after"))),
                str(payload.get("apply_policy") or "preview_apply_required"),
                _json_dump(_json_dict(payload.get("rollback_payload"))),
                str(existing["created_at"]) if existing else created_at,
                _text_or_none(payload.get("activated_at")),
            ),
        )
        conn.commit()
    fetched = fetch_topology_field_revision(topology_revision_id, brain_id=brain_id)
    if fetched is None:
        raise RuntimeError(f"topology_field_revision_not_found_after_store:{topology_revision_id}")
    return fetched


def fetch_topology_field_revision(topology_revision_id: str, *, brain_id: str | None = None) -> dict[str, Any] | None:
    normalized = str(topology_revision_id or "").strip()
    if not normalized:
        return None
    params: list[Any] = [normalized]
    sql = "SELECT * FROM topology_field_revisions WHERE topology_revision_id = ?"
    if brain_id:
        sql += " AND brain_id = ?"
        params.append(_active_brain_id(brain_id))
    with connect_readonly() as conn:
        row = conn.execute(sql, params).fetchone()
    return _topology_field_revision_from_row(row) if row else None


def activate_topology_field_revision(
    topology_revision_id: str,
    *,
    brain_id: str | None = None,
    activated_at: str | None = None,
) -> dict[str, Any] | None:
    normalized = str(topology_revision_id or "").strip()
    if not normalized:
        return None
    timestamp = str(activated_at or utc_timestamp())
    with connect() as conn:
        conn.execute(
            """
            UPDATE topology_field_revisions
            SET activated_at = ?
            WHERE topology_revision_id = ?
              AND brain_id = ?
            """,
            (timestamp, normalized, _active_brain_id(brain_id)),
        )
        conn.commit()
    return fetch_topology_field_revision(normalized, brain_id=brain_id)


def fetch_topology_field_revisions(*, brain_id: str | None = None, active_only: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if brain_id:
        clauses.append("brain_id = ?")
        params.append(_active_brain_id(brain_id))
    if active_only:
        clauses.append("activated_at IS NOT NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit or 20), 1000)))
    with connect_readonly() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM topology_field_revisions
            {where}
            ORDER BY activated_at DESC, created_at DESC, topology_revision_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_topology_field_revision_from_row(row) for row in rows]


def fetch_active_topology_field_revision(*, brain_id: str | None = None) -> dict[str, Any] | None:
    revisions = fetch_topology_field_revisions(brain_id=brain_id, active_only=True, limit=1)
    return revisions[0] if revisions else None


def _memory_policy_revision_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": str(row["schema_version"]),
        "policy_revision_id": str(row["policy_revision_id"]),
        "brain_id": str(row["brain_id"]),
        "parent_policy_revision_id": _text_or_none(row["parent_policy_revision_id"]),
        "policy_scope": str(row["policy_scope"]),
        "ingest_rules": dict(_json_load(row["ingest_rules_json"], {})),
        "retrieval_rules": dict(_json_load(row["retrieval_rules_json"], {})),
        "source_rules": dict(_json_load(row["source_rules_json"], {})),
        "deduction_rules": dict(_json_load(row["deduction_rules_json"], {})),
        "sleep_rules": dict(_json_load(row["sleep_rules_json"], {})),
        "evolve_rules": dict(_json_load(row["evolve_rules_json"], {})),
        "matrix_rules": dict(_json_load(row["matrix_rules_json"], {})),
        "supporting_event_ids": list(_json_load(row["supporting_event_ids_json"], [])),
        "quality_before": dict(_json_load(row["quality_before_json"], {})),
        "quality_after": dict(_json_load(row["quality_after_json"], {})),
        "status": str(row["status"]),
        "apply_policy": str(row["apply_policy"]),
        "rollback_payload": dict(_json_load(row["rollback_payload_json"], {})),
        "created_at": str(row["created_at"]),
        "activated_at": _text_or_none(row["activated_at"]),
    }


def store_memory_policy_revision(revision: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload = {**_json_dict(revision), **dict(overrides)}
    policy_revision_id = str(payload.get("policy_revision_id") or f"memory_policy_revision::{uuid.uuid4()}").strip()
    brain_id = _active_brain_id(_text_or_none(payload.get("brain_id")))
    created_at = str(payload.get("created_at") or utc_timestamp())
    with connect() as conn:
        existing = conn.execute(
            "SELECT created_at FROM memory_policy_revisions WHERE policy_revision_id = ?",
            (policy_revision_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO memory_policy_revisions (
                policy_revision_id, schema_version, brain_id, parent_policy_revision_id,
                policy_scope, ingest_rules_json, retrieval_rules_json, source_rules_json,
                deduction_rules_json, sleep_rules_json, evolve_rules_json, matrix_rules_json,
                supporting_event_ids_json, quality_before_json, quality_after_json,
                status, apply_policy, rollback_payload_json, created_at, activated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(policy_revision_id) DO UPDATE SET
                schema_version=excluded.schema_version,
                brain_id=excluded.brain_id,
                parent_policy_revision_id=excluded.parent_policy_revision_id,
                policy_scope=excluded.policy_scope,
                ingest_rules_json=excluded.ingest_rules_json,
                retrieval_rules_json=excluded.retrieval_rules_json,
                source_rules_json=excluded.source_rules_json,
                deduction_rules_json=excluded.deduction_rules_json,
                sleep_rules_json=excluded.sleep_rules_json,
                evolve_rules_json=excluded.evolve_rules_json,
                matrix_rules_json=excluded.matrix_rules_json,
                supporting_event_ids_json=excluded.supporting_event_ids_json,
                quality_before_json=excluded.quality_before_json,
                quality_after_json=excluded.quality_after_json,
                status=excluded.status,
                apply_policy=excluded.apply_policy,
                rollback_payload_json=excluded.rollback_payload_json,
                activated_at=excluded.activated_at
            """,
            (
                policy_revision_id,
                str(payload.get("schema_version") or MEMORY_POLICY_REVISION_SCHEMA_VERSION),
                brain_id,
                _text_or_none(payload.get("parent_policy_revision_id")),
                str(payload.get("policy_scope") or "brain"),
                _json_dump(_json_dict(payload.get("ingest_rules"))),
                _json_dump(_json_dict(payload.get("retrieval_rules"))),
                _json_dump(_json_dict(payload.get("source_rules"))),
                _json_dump(_json_dict(payload.get("deduction_rules"))),
                _json_dump(_json_dict(payload.get("sleep_rules"))),
                _json_dump(_json_dict(payload.get("evolve_rules"))),
                _json_dump(_json_dict(payload.get("matrix_rules"))),
                _json_dump(_json_list(payload.get("supporting_event_ids"))),
                _json_dump(_json_dict(payload.get("quality_before"))),
                _json_dump(_json_dict(payload.get("quality_after"))),
                str(payload.get("status") or "candidate"),
                str(payload.get("apply_policy") or "preview_apply_required"),
                _json_dump(_json_dict(payload.get("rollback_payload"))),
                str(existing["created_at"]) if existing else created_at,
                _text_or_none(payload.get("activated_at")),
            ),
        )
        conn.commit()
    fetched = fetch_memory_policy_revision(policy_revision_id, brain_id=brain_id)
    if fetched is None:
        raise RuntimeError(f"memory_policy_revision_not_found_after_store:{policy_revision_id}")
    return fetched


def fetch_memory_policy_revision(policy_revision_id: str, *, brain_id: str | None = None) -> dict[str, Any] | None:
    normalized = str(policy_revision_id or "").strip()
    if not normalized:
        return None
    params: list[Any] = [normalized]
    sql = "SELECT * FROM memory_policy_revisions WHERE policy_revision_id = ?"
    if brain_id:
        sql += " AND brain_id = ?"
        params.append(_active_brain_id(brain_id))
    with connect_readonly() as conn:
        row = conn.execute(sql, params).fetchone()
    return _memory_policy_revision_from_row(row) if row else None


def fetch_memory_policy_revisions(
    *,
    brain_id: str | None = None,
    status: str | None = None,
    active_only: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if brain_id:
        clauses.append("brain_id = ?")
        params.append(_active_brain_id(brain_id))
    if status:
        clauses.append("status = ?")
        params.append(str(status).strip())
    if active_only:
        clauses.append("status = 'active'")
        clauses.append("activated_at IS NOT NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit or 20), 1000)))
    with connect_readonly() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM memory_policy_revisions
            {where}
            ORDER BY activated_at DESC, created_at DESC, policy_revision_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_memory_policy_revision_from_row(row) for row in rows]


def activate_memory_policy_revision(
    policy_revision_id: str,
    *,
    brain_id: str | None = None,
    activated_at: str | None = None,
) -> dict[str, Any] | None:
    normalized = str(policy_revision_id or "").strip()
    if not normalized:
        return None
    active_brain = _active_brain_id(brain_id)
    timestamp = str(activated_at or utc_timestamp())
    with connect() as conn:
        conn.execute(
            """
            UPDATE memory_policy_revisions
            SET status = 'superseded'
            WHERE brain_id = ?
              AND status = 'active'
              AND policy_revision_id <> ?
            """,
            (active_brain, normalized),
        )
        conn.execute(
            """
            UPDATE memory_policy_revisions
            SET status = 'active',
                activated_at = ?
            WHERE policy_revision_id = ?
              AND brain_id = ?
            """,
            (timestamp, normalized, active_brain),
        )
        conn.commit()
    return fetch_memory_policy_revision(normalized, brain_id=active_brain)


def fetch_active_memory_policy_revision(*, brain_id: str | None = None) -> dict[str, Any] | None:
    revisions = fetch_memory_policy_revisions(brain_id=brain_id, active_only=True, limit=1)
    return revisions[0] if revisions else None


def memory_learning_store_capability_report(*, brain_id: str | None = None) -> dict[str, Any]:
    bootstrap_runtime_store()
    with connect_readonly() as conn:
        table_rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        ).fetchall()
        present = {str(row["name"]) for row in table_rows}
        tables: dict[str, dict[str, Any]] = {}
        for table in MEMORY_LEARNING_REQUIRED_TABLES:
            row_count = None
            if table in present:
                row_count = int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
            tables[table] = {"present": table in present, "row_count": row_count}
    return build_memory_learning_capability_report(
        storage_backend="sqlite",
        tables=tables,
        writable=True,
        brain_id=_active_brain_id(brain_id),
    )


def _bucket_at(position: dict[str, float], bucket_size: float) -> dict[str, int | str]:
    return position_to_bucket(position, bucket_size=bucket_size)


def _summary_short(text: str, limit: int = 160) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3].rstrip()}..."


def _upsert_node(conn: sqlite3.Connection, node: dict[str, Any]) -> None:
    final_position = dict(node["final_position"])
    coarse_bucket = _bucket_at(final_position, COARSE_BUCKET_SIZE)
    fine_bucket = _bucket_at(final_position, FINE_BUCKET_SIZE)
    provenance = dict(node.get("provenance") or {})
    conn.execute(
        """
        INSERT INTO nodes_nav (
            id, node_kind, memory_type, x, y, z,
            coarse_bucket_x, coarse_bucket_y, coarse_bucket_z, coarse_bucket_key,
            fine_bucket_x, fine_bucket_y, fine_bucket_z, fine_bucket_key,
            topology_brainhex_json, topology_color_json,
            routing_scores_json, routing_facets_json,
            summary_short, guide_area,
            memory_confidence, identity_resolution_confidence, evidence_confidence, stability_confidence,
            is_document_anchor, is_summary, granularity, novelty,
            sleep_revision_count, last_sleep_review_at,
            temporal_role, valid_from, valid_to, observed_at, superseded_by, obsoletes_json, temporal_confidence, lifecycle_status,
            source_trust, claim_status, answer_eligible, profile_eligible, document_eligible,
            matrix_revision_id, topology_revision_id, matrix_calibration_plan_signature, matrix_calibrated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?,
            ?, ?,
            ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?
        )
        ON CONFLICT(id) DO UPDATE SET
            node_kind=excluded.node_kind,
            memory_type=excluded.memory_type,
            x=excluded.x, y=excluded.y, z=excluded.z,
            coarse_bucket_x=excluded.coarse_bucket_x,
            coarse_bucket_y=excluded.coarse_bucket_y,
            coarse_bucket_z=excluded.coarse_bucket_z,
            coarse_bucket_key=excluded.coarse_bucket_key,
            fine_bucket_x=excluded.fine_bucket_x,
            fine_bucket_y=excluded.fine_bucket_y,
            fine_bucket_z=excluded.fine_bucket_z,
            fine_bucket_key=excluded.fine_bucket_key,
            topology_brainhex_json=excluded.topology_brainhex_json,
            topology_color_json=excluded.topology_color_json,
            routing_scores_json=excluded.routing_scores_json,
            routing_facets_json=excluded.routing_facets_json,
            summary_short=excluded.summary_short,
            guide_area=excluded.guide_area,
            memory_confidence=excluded.memory_confidence,
            identity_resolution_confidence=excluded.identity_resolution_confidence,
            evidence_confidence=excluded.evidence_confidence,
            stability_confidence=excluded.stability_confidence,
            is_document_anchor=excluded.is_document_anchor,
            is_summary=excluded.is_summary,
            granularity=excluded.granularity,
            novelty=excluded.novelty,
            sleep_revision_count=excluded.sleep_revision_count,
            last_sleep_review_at=excluded.last_sleep_review_at,
            temporal_role=excluded.temporal_role,
            valid_from=excluded.valid_from,
            valid_to=excluded.valid_to,
            observed_at=excluded.observed_at,
            superseded_by=excluded.superseded_by,
            obsoletes_json=excluded.obsoletes_json,
            temporal_confidence=excluded.temporal_confidence,
            lifecycle_status=excluded.lifecycle_status,
            source_trust=excluded.source_trust,
            claim_status=excluded.claim_status,
            answer_eligible=excluded.answer_eligible,
            profile_eligible=excluded.profile_eligible,
            document_eligible=excluded.document_eligible,
            matrix_revision_id=excluded.matrix_revision_id,
            topology_revision_id=excluded.topology_revision_id,
            matrix_calibration_plan_signature=excluded.matrix_calibration_plan_signature,
            matrix_calibrated_at=excluded.matrix_calibrated_at
        """,
        (
            str(node["id"]),
            str(node["node_kind"]),
            str(node["memory_type"]),
            float(final_position["x"]),
            float(final_position["y"]),
            float(final_position["z"]),
            int(coarse_bucket["x"]),
            int(coarse_bucket["y"]),
            int(coarse_bucket["z"]),
            str(coarse_bucket["key"]),
            int(fine_bucket["x"]),
            int(fine_bucket["y"]),
            int(fine_bucket["z"]),
            str(fine_bucket["key"]),
            _json_dump(node.get("topology_brainhex") or {}),
            _json_dump(node.get("topology_color") or {}),
            _json_dump(node.get("routing_semantic_scores") or {}),
            _json_dump(node.get("routing_facets") or {}),
            _summary_short(str(node.get("summary") or node.get("raw_text") or "")),
            provenance.get("guide_conceptual_area"),
            node.get("memory_confidence"),
            node.get("identity_resolution_confidence"),
            node.get("evidence_confidence"),
            node.get("stability_confidence"),
            1 if node.get("is_document_anchor") else 0,
            1 if node.get("is_summary") else 0,
            float(node.get("granularity") or 0.5),
            float(node.get("novelty") or 0.5),
            int(node.get("sleep_revision_count") or 0),
            node.get("last_sleep_review_at"),
            node.get("temporal_role"),
            node.get("valid_from"),
            node.get("valid_to"),
            node.get("observed_at"),
            node.get("superseded_by"),
            _json_dump(list(node.get("obsoletes") or [])),
            node.get("temporal_confidence"),
            str(node.get("lifecycle_status") or "active"),
            str(node.get("source_trust") or "user_asserted"),
            str(node.get("claim_status") or "fact"),
            1 if node.get("answer_eligible", True) else 0,
            1 if node.get("profile_eligible", True) else 0,
            1 if node.get("document_eligible", True) else 0,
            node.get("matrix_revision_id"),
            node.get("topology_revision_id"),
            node.get("matrix_calibration_plan_signature"),
            node.get("matrix_calibrated_at"),
        ),
    )
    conn.execute(
        """
        INSERT INTO node_text (
            node_id, raw_text, summary_full, provenance_json, source_label, source_type
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
            raw_text=excluded.raw_text,
            summary_full=excluded.summary_full,
            provenance_json=excluded.provenance_json,
            source_label=excluded.source_label,
            source_type=excluded.source_type
        """,
        (
            str(node["id"]),
            str(node.get("raw_text") or ""),
            str(node.get("summary") or ""),
            _json_dump(provenance),
            provenance.get("source_label"),
            provenance.get("source_type"),
        ),
    )
    conn.execute(
        """
        INSERT INTO node_semantics (
            node_id, routing_brainhex_json, semantic_color_json, base_position_json,
            derivation_role, derivation_confidence, derived_from_preview_id,
            document_role, document_anchor_id, document_chunk_index,
            source_unit_id, source_unit_title, source_unit_kind, source_unit_role,
            promotion_role, source_unit_formation_strategy,
            source_span_start, source_span_end,
            retrieval_affordance_json, retrieval_aliases_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
            routing_brainhex_json=excluded.routing_brainhex_json,
            semantic_color_json=excluded.semantic_color_json,
            base_position_json=excluded.base_position_json,
            derivation_role=excluded.derivation_role,
            derivation_confidence=excluded.derivation_confidence,
            derived_from_preview_id=excluded.derived_from_preview_id,
            document_role=excluded.document_role,
            document_anchor_id=excluded.document_anchor_id,
            document_chunk_index=excluded.document_chunk_index,
            source_unit_id=excluded.source_unit_id,
            source_unit_title=excluded.source_unit_title,
            source_unit_kind=excluded.source_unit_kind,
            source_unit_role=excluded.source_unit_role,
            promotion_role=excluded.promotion_role,
            source_unit_formation_strategy=excluded.source_unit_formation_strategy,
            source_span_start=excluded.source_span_start,
            source_span_end=excluded.source_span_end,
            retrieval_affordance_json=excluded.retrieval_affordance_json,
            retrieval_aliases_json=excluded.retrieval_aliases_json
        """,
        (
            str(node["id"]),
            _json_dump(node.get("routing_brainhex") or {}),
            _json_dump(node.get("semantic_color") or {}),
            _json_dump(node.get("base_position") or {}),
            node.get("derivation_role"),
            node.get("derivation_confidence"),
            node.get("derived_from_preview_id"),
            node.get("document_role"),
            node.get("document_anchor_id"),
            node.get("document_chunk_index"),
            node.get("source_unit_id"),
            node.get("source_unit_title"),
            node.get("source_unit_kind"),
            node.get("source_unit_role"),
            node.get("promotion_role"),
            node.get("source_unit_formation_strategy"),
            node.get("source_span_start"),
            node.get("source_span_end"),
            _json_dump(dict(node.get("retrieval_affordance") or {})),
            _json_dump(list(node.get("retrieval_aliases") or [])),
        ),
    )
def _set_node_relations(
    conn: sqlite3.Connection,
    node: dict[str, Any],
    *,
    valid_target_ids: set[str] | None = None,
) -> None:
    source_id = str(node["id"])
    conn.execute("DELETE FROM links WHERE source_id = ?", (source_id,))
    conn.executemany(
        """
        INSERT INTO links (source_id, target_id, strength, reason, kind, stability)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                source_id,
                str(link["target_node_id"]),
                float(link.get("strength") or 0.0),
                str(link.get("reason") or ""),
                link.get("kind"),
                link.get("stability"),
            )
            for link in list(node.get("links") or [])
            if not valid_target_ids or str(link.get("target_node_id") or "") in valid_target_ids
        ],
    )
    conn.execute("DELETE FROM highways WHERE source_id = ?", (source_id,))
    conn.executemany(
        """
        INSERT INTO highways (source_id, target_id, strength, reason, kind, stability)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                source_id,
                str(link["target_node_id"]),
                float(link.get("strength") or 0.0),
                str(link.get("reason") or ""),
                link.get("kind"),
                link.get("stability"),
            )
            for link in list(node.get("highways") or [])
            if not valid_target_ids or str(link.get("target_node_id") or "") in valid_target_ids
        ],
    )


def _set_graph_edges(conn: sqlite3.Connection, edges: list[dict[str, Any]], *, valid_node_ids: set[str] | None = None) -> None:
    conn.execute("DELETE FROM graph_edges")
    deduped_rows: list[tuple[str, str, str, float, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        source_id = str(edge["source_node_id"])
        target_id = str(edge["target_node_id"])
        if valid_node_ids and (source_id not in valid_node_ids or target_id not in valid_node_ids):
            continue
        row = (
            source_id,
            target_id,
            str(edge["edge_type"]),
            float(edge.get("confidence") or 0.0),
            str(edge.get("reason") or ""),
        )
        signature = row[:3]
        if signature in seen:
            continue
        seen.add(signature)
        deduped_rows.append(row)
    conn.executemany(
        """
        INSERT INTO graph_edges (source_id, target_id, edge_type, confidence, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        deduped_rows,
    )


def _fetch_links_map(conn: sqlite3.Connection, node_ids: list[str], table: str) -> dict[str, list[dict[str, Any]]]:
    if not node_ids:
        return {}
    placeholders = ",".join("?" for _ in node_ids)
    rows = conn.execute(
        f"SELECT source_id, target_id, strength, reason, kind, stability FROM {table} WHERE source_id IN ({placeholders})",
        node_ids,
    ).fetchall()
    payload: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        payload[str(row["source_id"])].append(
            {
                "target_node_id": str(row["target_id"]),
                "strength": float(row["strength"]),
                "reason": str(row["reason"]),
                "kind": row["kind"],
                "stability": row["stability"],
            }
        )
    return payload


def _fetch_edge_rows(conn: sqlite3.Connection, node_ids: list[str] | None = None) -> list[dict[str, Any]]:
    if node_ids:
        placeholders = ",".join("?" for _ in node_ids)
        rows = conn.execute(
            f"""
            SELECT source_id, target_id, edge_type, confidence, reason
            FROM graph_edges
            WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})
            """,
            [*node_ids, *node_ids],
        ).fetchall()
    else:
        rows = conn.execute("SELECT source_id, target_id, edge_type, confidence, reason FROM graph_edges").fetchall()
    return [
        {
            "source_node_id": str(row["source_id"]),
            "target_node_id": str(row["target_id"]),
            "edge_type": str(row["edge_type"]),
            "confidence": float(row["confidence"]),
            "reason": str(row["reason"]),
        }
        for row in rows
    ]


def fetch_graph_edges_for_nodes(node_ids: list[str] | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        return _fetch_edge_rows(conn, node_ids)


def _row_to_node(
    row: sqlite3.Row,
    *,
    raw_text_override: str | None = None,
    summary_override: str | None = None,
) -> dict[str, Any]:
    available = set(row.keys())
    final_position = {"x": float(row["x"]), "y": float(row["y"]), "z": float(row["z"])}
    topology_brainhex = _json_load(row["topology_brainhex_json"], {})
    topology_color = _json_load(row["topology_color_json"], {})
    routing_scores = normalize_scores(_json_load(row["routing_scores_json"], {}), ROUTING_FIELDS)
    routing_facets = normalize_scores(_json_load(row["routing_facets_json"], {}), FACET_FIELDS)
    routing_brainhex = _json_load(row["routing_brainhex_json"], {})
    semantic_color = _json_load(row["semantic_color_json"], {})
    base_position = _json_load(row["base_position_json"], {})
    if not _mapping_has_keys(base_position, ("x", "y", "z")):
        base_position = dict(final_position)
    if not _mapping_has_keys(topology_brainhex, ("theta_bin", "phi_bin", "radius_bin", "code")):
        topology_brainhex = position_to_topology_brainhex(final_position)
    if not _mapping_has_keys(topology_color, ("h", "s", "l", "hex")):
        topology_color = color_from_brainhex(topology_brainhex)
    if not _mapping_has_keys(routing_brainhex, ("theta_bin", "phi_bin", "radius_bin", "code")):
        routing_brainhex = position_to_topology_brainhex(base_position)
    if not _mapping_has_keys(semantic_color, ("h", "s", "l", "hex")):
        semantic_color = color_from_brainhex(routing_brainhex)
    if "provenance_json" in available:
        provenance = _json_load(row["provenance_json"], {})
        provenance.setdefault("mode", "runtime_navigation_store")
    else:
        provenance = {
            "mode": "runtime_navigation_store",
            "source_type": "navigation_store",
            "guide_conceptual_area": row["guide_area"] if "guide_area" in available else None,
        }
    raw_text_source = str(row["raw_text"] or "") if "raw_text" in available else ""
    summary_source = (
        str(row["summary_full"] or "")
        if "summary_full" in available
        else str(row["summary_short"] or "")
    )
    raw_text = raw_text_override if raw_text_override is not None else raw_text_source
    summary = summary_override if summary_override is not None else summary_source
    memory_type = str(row["memory_type"] or "")
    is_document_anchor = bool(row["is_document_anchor"]) or memory_type.strip().lower() == "document_anchor"
    source_span_start = row["source_span_start"]
    source_span_end = row["source_span_end"]
    explicit_document_role = str(row["document_role"] or "").strip() if "document_role" in available and row["document_role"] else ""
    document_role: str | None = explicit_document_role or None
    if not document_role and is_document_anchor:
        document_role = "anchor"
    elif not document_role and memory_type.strip().lower() == "document_summary":
        document_role = "summary"
    elif not document_role and memory_type.strip().lower() == "document_chunk":
        document_role = "chunk"
    elif not document_role and memory_type.strip().lower() == "document_fact":
        document_role = "fact"
    explicit_anchor_id = str(row["document_anchor_id"] or "").strip() if "document_anchor_id" in available and row["document_anchor_id"] else ""
    document_anchor_id = explicit_anchor_id or (str(row["id"]) if is_document_anchor else None)
    return {
        "id": str(row["id"]),
        "node_kind": str(row["node_kind"]),
        "memory_type": memory_type,
        "raw_text": raw_text,
        "summary": summary,
        "routing_semantic_scores": routing_scores,
        "routing_facets": routing_facets,
        "routing_brainhex": routing_brainhex,
        "semantic_color": semantic_color,
        "base_position": base_position,
        "final_position": final_position,
        "topology_brainhex": topology_brainhex,
        "topology_color": topology_color,
        "bucket": {
            "x": int(row["fine_bucket_x"]),
            "y": int(row["fine_bucket_y"]),
            "z": int(row["fine_bucket_z"]),
            "key": str(row["fine_bucket_key"]),
        },
        "is_document_anchor": is_document_anchor,
        "is_summary": bool(row["is_summary"]),
        "granularity": float(row["granularity"]),
        "novelty": float(row["novelty"]),
        "links": [],
        "highways": [],
        "provenance": provenance,
        "debug": None,
        "derivation_role": row["derivation_role"],
        "derivation_confidence": row["derivation_confidence"],
        "derived_from_preview_id": row["derived_from_preview_id"],
        "document_role": document_role,
        "document_anchor_id": document_anchor_id,
        "document_chunk_index": row["document_chunk_index"] if "document_chunk_index" in available else None,
        "source_unit_id": row["source_unit_id"] if "source_unit_id" in available else None,
        "source_unit_title": row["source_unit_title"] if "source_unit_title" in available else None,
        "source_unit_kind": row["source_unit_kind"] if "source_unit_kind" in available else None,
        "source_unit_role": row["source_unit_role"] if "source_unit_role" in available else None,
        "promotion_role": row["promotion_role"] if "promotion_role" in available else None,
        "source_unit_formation_strategy": row["source_unit_formation_strategy"] if "source_unit_formation_strategy" in available else None,
        "source_span_start": source_span_start,
        "source_span_end": source_span_end,
        "memory_confidence": row["memory_confidence"],
        "identity_resolution_confidence": row["identity_resolution_confidence"],
        "evidence_confidence": row["evidence_confidence"],
        "stability_confidence": row["stability_confidence"],
        "sleep_revision_count": int(row["sleep_revision_count"] or 0),
        "last_sleep_review_at": row["last_sleep_review_at"],
        "temporal_role": row["temporal_role"] if "temporal_role" in available else None,
        "valid_from": row["valid_from"] if "valid_from" in available else None,
        "valid_to": row["valid_to"] if "valid_to" in available else None,
        "observed_at": row["observed_at"] if "observed_at" in available else None,
        "superseded_by": row["superseded_by"] if "superseded_by" in available else None,
        "obsoletes": _json_load(row["obsoletes_json"], []) if "obsoletes_json" in available else [],
        "temporal_confidence": row["temporal_confidence"] if "temporal_confidence" in available else None,
        "lifecycle_status": str(row["lifecycle_status"] or "active") if "lifecycle_status" in available else "active",
        "source_trust": str(row["source_trust"] or "user_asserted") if "source_trust" in available else "user_asserted",
        "claim_status": str(row["claim_status"] or "fact") if "claim_status" in available else "fact",
        "answer_eligible": bool(row["answer_eligible"]) if "answer_eligible" in available else True,
        "profile_eligible": bool(row["profile_eligible"]) if "profile_eligible" in available else True,
        "document_eligible": bool(row["document_eligible"]) if "document_eligible" in available else True,
        "retrieval_affordance": _json_load(row["retrieval_affordance_json"], {}) if "retrieval_affordance_json" in available else {},
        "retrieval_aliases": _json_load(row["retrieval_aliases_json"], []) if "retrieval_aliases_json" in available else [],
        "matrix_revision_id": row["matrix_revision_id"] if "matrix_revision_id" in available else None,
        "topology_revision_id": row["topology_revision_id"] if "topology_revision_id" in available else None,
        "matrix_calibration_plan_signature": row["matrix_calibration_plan_signature"] if "matrix_calibration_plan_signature" in available else None,
        "matrix_calibrated_at": row["matrix_calibrated_at"] if "matrix_calibrated_at" in available else None,
    }


def _mapping_has_keys(value: Any, keys: tuple[str, ...]) -> bool:
    return isinstance(value, dict) and all(value.get(key) is not None for key in keys)


def _fetch_node_rows(conn: sqlite3.Connection, *, node_ids: list[str] | None = None) -> list[sqlite3.Row]:
    semantic_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(node_semantics)").fetchall()}

    def semantic_column(name: str) -> str:
        return f"s.{name}" if name in semantic_columns else f"NULL AS {name}"

    sql = """
        SELECT
            n.*,
            t.raw_text,
            t.summary_full,
            t.provenance_json,
            s.routing_brainhex_json,
            s.semantic_color_json,
            s.base_position_json,
            s.derivation_role,
            s.derivation_confidence,
            s.derived_from_preview_id,
            {document_role},
            {document_anchor_id},
            {document_chunk_index},
            {source_unit_id},
            {source_unit_title},
            {source_unit_kind},
            {source_unit_role},
            {promotion_role},
            {source_unit_formation_strategy},
            s.source_span_start,
            s.source_span_end,
            {retrieval_affordance_json},
            {retrieval_aliases_json}
        FROM nodes_nav n
        LEFT JOIN node_text t ON t.node_id = n.id
        LEFT JOIN node_semantics s ON s.node_id = n.id
    """.format(
        document_role=semantic_column("document_role"),
        document_anchor_id=semantic_column("document_anchor_id"),
        document_chunk_index=semantic_column("document_chunk_index"),
        source_unit_id=semantic_column("source_unit_id"),
        source_unit_title=semantic_column("source_unit_title"),
        source_unit_kind=semantic_column("source_unit_kind"),
        source_unit_role=semantic_column("source_unit_role"),
        promotion_role=semantic_column("promotion_role"),
        source_unit_formation_strategy=semantic_column("source_unit_formation_strategy"),
        retrieval_affordance_json=semantic_column("retrieval_affordance_json"),
        retrieval_aliases_json=semantic_column("retrieval_aliases_json"),
    )
    params: list[Any] = []
    if node_ids:
        placeholders = ",".join("?" for _ in node_ids)
        sql += f" WHERE n.id IN ({placeholders})"
        params.extend(node_ids)
    sql += " ORDER BY n.id"
    return conn.execute(sql, params).fetchall()


def fetch_graph_snapshot() -> dict[str, Any]:
    with connect() as conn:
        rows = _fetch_node_rows(conn)
        node_ids = [str(row["id"]) for row in rows]
        links_map = _fetch_links_map(conn, node_ids, "links")
        highways_map = _fetch_links_map(conn, node_ids, "highways")
        nodes = []
        for row in rows:
            node = _row_to_node(row)
            node["links"] = links_map.get(node["id"], [])
            node["highways"] = highways_map.get(node["id"], [])
            nodes.append(node)
        edges = _fetch_edge_rows(conn)
    return {
        "version": GRAPH_VERSION,
        "graph_name": APP_NAME,
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "created_from": "agvm_sqlite_runtime_store",
            "graph_updated_at": utc_timestamp(),
            "node_count": len(nodes),
        },
    }


def fetch_nodes_by_ids(
    node_ids: list[str],
    *,
    include_raw_text: bool = True,
    busy_timeout_ms: int | None = None,
) -> list[dict[str, Any]]:
    if not node_ids:
        return []
    timeout_ms = 30000 if busy_timeout_ms is None else max(1, int(busy_timeout_ms))
    with connect(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
        rows = _fetch_node_rows(conn, node_ids=node_ids)
        links_map = _fetch_links_map(conn, node_ids, "links")
        highways_map = _fetch_links_map(conn, node_ids, "highways")
        node_map = {}
        for row in rows:
            raw_text_override = None if include_raw_text else str(row["summary_short"] or "")
            summary_override = None if include_raw_text else str(row["summary_short"] or "")
            node = _row_to_node(row, raw_text_override=raw_text_override, summary_override=summary_override)
            node["links"] = links_map.get(node["id"], [])
            node["highways"] = highways_map.get(node["id"], [])
            node_map[node["id"]] = node
    return [node_map[node_id] for node_id in node_ids if node_id in node_map]


def fetch_document_child_nodes(
    anchor_ids: list[str],
    *,
    limit_per_anchor: int = 24,
    include_raw_text: bool = True,
) -> list[dict[str, Any]]:
    cleaned_anchor_ids = list(dict.fromkeys(str(item or "").strip() for item in list(anchor_ids or []) if str(item or "").strip()))
    if not cleaned_anchor_ids:
        return []
    with connect() as conn:
        semantic_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(node_semantics)").fetchall()}
        clauses: list[str] = []
        params: list[Any] = []
        placeholders = ",".join("?" for _ in cleaned_anchor_ids)
        if "document_anchor_id" in semantic_columns:
            clauses.append(f"s.document_anchor_id IN ({placeholders})")
            params.extend(cleaned_anchor_ids)
        if "derived_from_preview_id" in semantic_columns:
            clauses.append(f"s.derived_from_preview_id IN ({placeholders})")
            params.extend(cleaned_anchor_ids)
        if not clauses:
            return []
        excluded_placeholders = ",".join("?" for _ in cleaned_anchor_ids)
        rows = conn.execute(
            f"""
            SELECT
                n.id,
                s.document_anchor_id,
                s.derived_from_preview_id,
                s.document_role,
                COALESCE(n.memory_confidence, 0.0) AS memory_confidence,
                COALESCE(n.evidence_confidence, 0.0) AS evidence_confidence
            FROM nodes_nav n
            LEFT JOIN node_semantics s ON s.node_id = n.id
            WHERE ({" OR ".join(clauses)})
              AND n.id NOT IN ({excluded_placeholders})
            ORDER BY
                CASE COALESCE(s.document_role, '')
                    WHEN 'fact' THEN 0
                    WHEN 'summary' THEN 1
                    WHEN 'chunk' THEN 2
                    ELSE 3
                END ASC,
                COALESCE(n.evidence_confidence, 0.0) DESC,
                COALESCE(n.memory_confidence, 0.0) DESC,
                n.id ASC
            LIMIT ?
            """,
            [*params, *cleaned_anchor_ids, max(1, len(cleaned_anchor_ids)) * max(1, int(limit_per_anchor)) * 3],
        ).fetchall()
        selected_ids: list[str] = []
        selected_anchor_by_id: dict[str, str] = {}
        selected_count_by_anchor: dict[str, int] = defaultdict(int)
        for row in rows:
            node_id = str(row["id"] or "").strip()
            anchor_id = str(row["document_anchor_id"] or row["derived_from_preview_id"] or "").strip()
            if not node_id or not anchor_id or anchor_id not in cleaned_anchor_ids:
                continue
            if selected_count_by_anchor[anchor_id] >= max(1, int(limit_per_anchor)):
                continue
            selected_count_by_anchor[anchor_id] += 1
            selected_anchor_by_id[node_id] = anchor_id
            selected_ids.append(node_id)
        if not selected_ids:
            return []
        rows = _fetch_node_rows(conn, node_ids=selected_ids)
        links_map = _fetch_links_map(conn, selected_ids, "links")
        highways_map = _fetch_links_map(conn, selected_ids, "highways")
        node_map = {}
        for row in rows:
            raw_text_override = None if include_raw_text else str(row["summary_short"] or "")
            summary_override = None if include_raw_text else str(row["summary_short"] or "")
            node = _row_to_node(row, raw_text_override=raw_text_override, summary_override=summary_override)
            node["links"] = links_map.get(node["id"], [])
            node["highways"] = highways_map.get(node["id"], [])
            anchor_id = selected_anchor_by_id.get(node["id"])
            if anchor_id and not str(node.get("document_anchor_id") or "").strip():
                node["document_anchor_id"] = anchor_id
            node["_matched_document_anchor_id"] = anchor_id
            node_map[node["id"]] = node
    return [node_map[node_id] for node_id in selected_ids if node_id in node_map]


def fetch_document_source_sibling_nodes(
    source_refs: list[dict[str, Any]],
    *,
    limit_per_source: int = 24,
    include_raw_text: bool = True,
) -> list[dict[str, Any]]:
    cleaned_refs: list[dict[str, str]] = []
    seen_ref_keys: set[tuple[str, str, str]] = set()
    for ref in list(source_refs or []):
        if not isinstance(ref, dict):
            continue
        source_label = str(ref.get("source_label") or "").strip()
        source_type = str(ref.get("source_type") or "").strip()
        anchor_id = str(ref.get("anchor_id") or ref.get("document_anchor_id") or "").strip()
        if not source_label:
            continue
        key = (source_label.lower(), source_type.lower(), anchor_id)
        if key in seen_ref_keys:
            continue
        seen_ref_keys.add(key)
        cleaned_refs.append({"source_label": source_label, "source_type": source_type, "anchor_id": anchor_id})
    if not cleaned_refs:
        return []
    clauses: list[str] = []
    params: list[Any] = []
    for ref in cleaned_refs:
        pattern = f"%{_escape_like_term(ref['source_label'].lower())}%"
        clauses.append("LOWER(COALESCE(t.provenance_json, '')) LIKE ? ESCAPE '\\'")
        params.append(pattern)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                n.*,
                t.raw_text,
                t.summary_full,
                t.provenance_json,
                s.routing_brainhex_json,
                s.semantic_color_json,
                s.base_position_json,
                s.derivation_role,
                s.derivation_confidence,
                s.derived_from_preview_id,
                s.document_role,
                s.document_anchor_id,
                s.document_chunk_index,
                s.source_span_start,
                s.source_span_end
            FROM nodes_nav n
            LEFT JOIN node_text t ON t.node_id = n.id
            LEFT JOIN node_semantics s ON s.node_id = n.id
            WHERE {" OR ".join(clauses)}
            ORDER BY
                CASE COALESCE(s.document_role, '')
                    WHEN 'fact' THEN 0
                    WHEN 'summary' THEN 1
                    WHEN 'chunk' THEN 2
                    WHEN 'anchor' THEN 3
                    ELSE 4
                END ASC,
                COALESCE(n.evidence_confidence, 0.0) DESC,
                COALESCE(n.memory_confidence, 0.0) DESC,
                n.id ASC
            LIMIT ?
            """,
            [*params, max(1, len(cleaned_refs)) * max(1, int(limit_per_source)) * 4],
        ).fetchall()
        node_ids = [str(row["id"]) for row in rows]
        links_map = _fetch_links_map(conn, node_ids, "links")
        highways_map = _fetch_links_map(conn, node_ids, "highways")
    selected: list[dict[str, Any]] = []
    selected_count_by_ref: dict[tuple[str, str, str], int] = defaultdict(int)
    seen_node_ids: set[str] = set()
    for row in rows:
        provenance = _json_load(row["provenance_json"], {})
        row_label = str(provenance.get("source_label") or "").strip().lower()
        row_type = str(provenance.get("source_type") or "").strip().lower()
        if not row_label:
            continue
        matched_ref: dict[str, str] | None = None
        for ref in cleaned_refs:
            ref_label = ref["source_label"].lower()
            ref_type = ref["source_type"].lower()
            if row_label != ref_label:
                continue
            if ref_type and row_type and row_type != ref_type:
                continue
            matched_ref = ref
            break
        if matched_ref is None:
            continue
        node_id = str(row["id"] or "").strip()
        if not node_id or node_id in seen_node_ids:
            continue
        ref_key = (matched_ref["source_label"].lower(), matched_ref["source_type"].lower(), matched_ref["anchor_id"])
        if selected_count_by_ref[ref_key] >= max(1, int(limit_per_source)):
            continue
        raw_text_override = None if include_raw_text else str(row["summary_short"] or "")
        summary_override = None if include_raw_text else str(row["summary_short"] or "")
        node = _row_to_node(row, raw_text_override=raw_text_override, summary_override=summary_override)
        node["links"] = links_map.get(node["id"], [])
        node["highways"] = highways_map.get(node["id"], [])
        anchor_id = matched_ref.get("anchor_id") or str(node.get("document_anchor_id") or "").strip()
        if anchor_id and not str(node.get("document_anchor_id") or "").strip():
            node["document_anchor_id"] = anchor_id
        node["_matched_document_anchor_id"] = anchor_id
        selected.append(node)
        seen_node_ids.add(node_id)
        selected_count_by_ref[ref_key] += 1
    return selected


def fetch_document_anchor_ids_by_source_refs(source_refs: list[dict[str, Any]]) -> dict[str, str]:
    cleaned_refs: list[dict[str, str]] = []
    seen_ref_keys: set[tuple[str, str]] = set()
    for ref in list(source_refs or []):
        if not isinstance(ref, dict):
            continue
        source_label = str(ref.get("source_label") or "").strip()
        source_type = str(ref.get("source_type") or "").strip()
        if not source_label:
            continue
        key = (source_label.lower(), source_type.lower())
        if key in seen_ref_keys:
            continue
        seen_ref_keys.add(key)
        cleaned_refs.append({"source_label": source_label, "source_type": source_type})
    if not cleaned_refs:
        return {}
    clauses: list[str] = []
    params: list[Any] = []
    for ref in cleaned_refs:
        pattern = f"%{_escape_like_term(ref['source_label'].lower())}%"
        clauses.append("LOWER(COALESCE(t.provenance_json, '')) LIKE ? ESCAPE '\\'")
        params.append(pattern)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                n.id,
                n.is_document_anchor,
                n.memory_type,
                t.provenance_json,
                s.document_role,
                s.document_anchor_id
            FROM nodes_nav n
            LEFT JOIN node_text t ON t.node_id = n.id
            LEFT JOIN node_semantics s ON s.node_id = n.id
            WHERE ({" OR ".join(clauses)})
              AND (
                    COALESCE(s.document_role, '') = 'anchor'
                 OR COALESCE(n.is_document_anchor, 0) = 1
                 OR LOWER(COALESCE(n.memory_type, '')) = 'document_anchor'
              )
            ORDER BY
                COALESCE(n.evidence_confidence, 0.0) DESC,
                COALESCE(n.memory_confidence, 0.0) DESC,
                n.id ASC
            LIMIT ?
            """,
            [*params, max(1, len(cleaned_refs)) * 4],
        ).fetchall()
    anchors: dict[str, str] = {}
    for row in rows:
        provenance = _json_load(row["provenance_json"], {})
        row_label = str(provenance.get("source_label") or "").strip().lower()
        row_type = str(provenance.get("source_type") or "").strip().lower()
        if not row_label:
            continue
        anchor_id = str(row["id"] or row["document_anchor_id"] or "").strip()
        if not anchor_id:
            continue
        for ref in cleaned_refs:
            ref_label = ref["source_label"].lower()
            ref_type = ref["source_type"].lower()
            if row_label != ref_label:
                continue
            if ref_type and row_type and row_type != ref_type:
                continue
            key = f"{ref_type}::{ref_label}"
            anchors.setdefault(key, anchor_id)
    return anchors


def _escape_like_term(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_DOCUMENT_CLAIM_RANK_LOW_SIGNAL_TERMS = {
    "about",
    "after",
    "also",
    "are",
    "associated",
    "can",
    "could",
    "due",
    "effect",
    "effects",
    "easily",
    "forms",
    "important",
    "increased",
    "increases",
    "involved",
    "provided",
    "related",
    "show",
    "shows",
    "they",
}

_DOCUMENT_CLAIM_RANK_STOP_TERMS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "be",
    "by",
    "due",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}


def _claim_rank_tokenize(value: Any) -> list[str]:
    return [match.group(0).lower() for match in re.finditer(r"[A-Za-z0-9_]+", str(value or ""))]


def _claim_rank_terms(terms: list[str]) -> list[str]:
    cleaned: list[str] = []
    for term in list(terms or []):
        normalized = str(term or "").strip().lower()
        if not normalized:
            continue
        if len(normalized) < 2 or normalized in _DOCUMENT_CLAIM_RANK_STOP_TERMS:
            continue
        if normalized not in cleaned:
            cleaned.append(normalized)
    term_set = set(cleaned)
    bridge_terms: list[str] = []
    if "weight" in term_set:
        if "low" in term_set or "under" in term_set:
            bridge_terms.append("underweight")
        if "high" in term_set or "over" in term_set:
            bridge_terms.append("overweight")
    if "blood" in term_set and ("level" in term_set or "levels" in term_set):
        bridge_terms.extend(["serum", "plasma"])
    for term in bridge_terms:
        if term and term not in cleaned:
            cleaned.append(term)
    return cleaned[:48]


def _claim_rank_term_variants(term: str) -> list[str]:
    normalized = str(term or "").strip().lower()
    if normalized == "blood":
        return ["blood", "serum", "plasma"]
    if normalized == "plasma":
        return ["plasma", "blood", "serum"]
    if normalized == "serum":
        return ["serum", "blood", "plasma"]
    if normalized == "weight":
        return ["weight", "birthweight"]
    variants = [normalized] if normalized else []
    if normalized.endswith("ies") and len(normalized) > 4:
        variants.append(f"{normalized[:-3]}y")
    if normalized.endswith("ves") and len(normalized) > 4:
        variants.append(f"{normalized[:-3]}f")
    if normalized.endswith("es") and len(normalized) > 4:
        variants.append(normalized[:-2])
    if normalized.endswith("s") and len(normalized) > 3:
        variants.append(normalized[:-1])
    return list(dict.fromkeys(item for item in variants if item))


def _claim_rank_fetch_terms(terms: list[str]) -> list[str]:
    high_signal = [
        term
        for term in _claim_rank_terms(terms)
        if term not in _DOCUMENT_CLAIM_RANK_LOW_SIGNAL_TERMS and not term.isdigit()
    ]
    source = high_signal or _claim_rank_terms(terms)
    variants = [
        variant
        for term in source
        if not term.isdigit()
        for variant in _claim_rank_term_variants(term)
    ]
    return list(dict.fromkeys(variant for variant in variants if len(variant) >= 2))[:64]


def _claim_rank_term_weight(term: str) -> float:
    normalized = str(term or "").strip().lower()
    if not normalized:
        return 0.0
    if normalized in _DOCUMENT_CLAIM_RANK_LOW_SIGNAL_TERMS:
        return 0.25
    if normalized.isdigit():
        return 0.45 if normalized == "000" else 0.22 if len(normalized) <= 2 else 0.32
    if len(normalized) <= 3:
        return 0.65
    if any(char.isdigit() for char in normalized):
        return 1.25
    if normalized.isupper():
        return 1.2
    return 1.0


def _claim_rank_term_frequency(term: str, token_counts: Counter[str], folded_text: str) -> int:
    count = 0
    variants = _claim_rank_term_variants(term)
    for variant in variants:
        count += int(token_counts.get(variant, 0))
    if count:
        return count
    if len(term) >= 5 and term not in {"weight"}:
        for variant in variants:
            for token, token_count in token_counts.items():
                if len(token) > len(variant) and variant in token and len(token) <= len(variant) + 16:
                    return int(token_count)
    # Scientific claims often contain compact surface forms while documents split
    # them into biomedical symbols/words. Credit two contained rare tokens as a
    # soft compound match without making this a SciFact-specific rule.
    if len(term) >= 8:
        token_hits = 0
        for token in token_counts:
            if len(token) >= 4 and token in term:
                token_hits += 1
            if token_hits >= 2:
                return 1
    return 1 if any(re.search(rf"\b{re.escape(variant)}\b", folded_text) for variant in variants) else 0


def fetch_document_nodes_by_claim_terms(
    terms: list[str],
    *,
    limit: int = 24,
    include_raw_text: bool = False,
    busy_timeout_ms: int | None = None,
    max_scan_rows: int = 50000,
) -> list[dict[str, Any]]:
    """Return document anchors ranked by a bounded BM25/IDF claim scorer.

    This is intentionally separate from generic memory text lookup: it is used
    only by document-evidence flows where exact primary-document recall matters.
    """

    cleaned_terms = _claim_rank_terms(terms)
    fetch_terms = _claim_rank_fetch_terms(cleaned_terms)
    if not cleaned_terms or not fetch_terms:
        return []
    clauses: list[str] = []
    params: list[Any] = []
    for term in fetch_terms:
        pattern = f"%{_escape_like_term(term)}%"
        clauses.append(
            "("
            "t.raw_text LIKE ? ESCAPE '\\' OR "
            "t.summary_full LIKE ? ESCAPE '\\' OR "
            "n.summary_short LIKE ? ESCAPE '\\' OR "
            "t.provenance_json LIKE ? ESCAPE '\\' OR "
            "s.source_unit_title LIKE ? ESCAPE '\\' OR "
            "s.retrieval_affordance_json LIKE ? ESCAPE '\\' OR "
            "s.retrieval_aliases_json LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([pattern, pattern, pattern, pattern, pattern, pattern, pattern])
    document_anchor_filter = (
        "("
        "COALESCE(s.document_role, '') = 'anchor' OR "
        "COALESCE(n.is_document_anchor, 0) = 1 OR "
        "LOWER(COALESCE(n.memory_type, '')) = 'document_anchor'"
        ")"
    )
    sql = f"""
        SELECT
            n.*,
            t.raw_text,
            t.summary_full,
            t.provenance_json,
            s.routing_brainhex_json,
            s.semantic_color_json,
            s.base_position_json,
            s.derivation_role,
            s.derivation_confidence,
            s.derived_from_preview_id,
            s.document_role,
            s.document_anchor_id,
            s.document_chunk_index,
            s.source_unit_id,
            s.source_unit_title,
            s.source_unit_kind,
            s.source_unit_role,
            s.promotion_role,
            s.source_unit_formation_strategy,
            s.source_span_start,
            s.source_span_end,
            s.retrieval_affordance_json,
            s.retrieval_aliases_json
        FROM nodes_nav n
        LEFT JOIN node_text t ON t.node_id = n.id
        LEFT JOIN node_semantics s ON s.node_id = n.id
        WHERE {document_anchor_filter}
          AND ({" OR ".join(clauses)})
        LIMIT ?
    """
    timeout_ms = 30000 if busy_timeout_ms is None else max(1, int(busy_timeout_ms))
    scan_limit = max(int(limit or 24), min(max(1000, int(max_scan_rows or 50000)), 50000))
    with connect(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
        rows = conn.execute(sql, [*params, scan_limit]).fetchall()
        node_ids = [str(row["id"]) for row in rows]
        links_map = _fetch_links_map(conn, node_ids, "links")
        highways_map = _fetch_links_map(conn, node_ids, "highways")

    if not rows:
        return []

    row_texts: list[tuple[sqlite3.Row, str, str, Counter[str], int]] = []
    document_frequency: Counter[str] = Counter()
    total_len = 0
    for row in rows:
        provenance = _json_load(row["provenance_json"], {})
        title_source = " ".join(
            str(value or "")
            for value in (
                row["summary_short"],
                provenance.get("source_label"),
                row["source_unit_title"],
                row["source_unit_kind"],
            )
        )
        full_text = " ".join(
            str(value or "")
            for value in (
                title_source,
                row["summary_full"],
                row["raw_text"],
                row["retrieval_affordance_json"],
                row["retrieval_aliases_json"],
            )
        ).lower()
        tokens = _claim_rank_tokenize(full_text)
        counts = Counter(tokens)
        doc_len = max(1, len(tokens))
        total_len += doc_len
        folded_text = " ".join(tokens)
        for term in cleaned_terms:
            if _claim_rank_term_frequency(term, counts, folded_text) > 0:
                document_frequency[term] += 1
        row_texts.append((row, title_source.lower(), folded_text, counts, doc_len))

    candidate_count = len(row_texts)
    avg_doc_len = total_len / max(1, candidate_count)
    query_vector = {
        term: _claim_rank_term_weight(term)
        for term in cleaned_terms
        if _claim_rank_term_weight(term) > 0.0
    }
    query_norm = math.sqrt(sum(weight * weight for weight in query_vector.values())) or 1.0
    k1 = 1.45
    b = 0.72
    scored: list[tuple[float, str, dict[str, Any]]] = []
    max_raw_score = 0.0
    raw_rows: list[tuple[float, str, dict[str, Any], list[str], list[str], dict[str, float], float]] = []
    for row, title_source, folded_text, counts, doc_len in row_texts:
        bm25 = 0.0
        title_score = 0.0
        matched: list[str] = []
        missing: list[str] = []
        idf_by_term: dict[str, float] = {}
        for term in cleaned_terms:
            weight = _claim_rank_term_weight(term)
            if weight <= 0.0:
                continue
            tf = _claim_rank_term_frequency(term, counts, folded_text)
            df = max(1, int(document_frequency.get(term) or 0))
            idf = math.log(1.0 + (candidate_count - df + 0.5) / (df + 0.5))
            idf_by_term[term] = round(idf, 6)
            if tf <= 0:
                missing.append(term)
                continue
            matched.append(term)
            numerator = tf * (k1 + 1.0)
            denominator = tf + k1 * (1.0 - b + b * (doc_len / max(avg_doc_len, 1.0)))
            bm25 += weight * idf * numerator / max(denominator, 0.0001)
            if _claim_rank_term_frequency(term, Counter(_claim_rank_tokenize(title_source)), title_source) > 0:
                title_score += min(0.35, 0.08 * weight + 0.03 * idf)
        if not matched:
            continue
        coverage = sum(_claim_rank_term_weight(term) for term in matched) / max(
            0.0001,
            sum(_claim_rank_term_weight(term) for term in cleaned_terms),
        )
        vector_dot = 0.0
        for term, weight in query_vector.items():
            vector_dot += weight * float(_claim_rank_term_frequency(term, counts, folded_text))
        doc_norm = math.sqrt(sum(float(count) * float(count) for count in counts.values())) or 1.0
        vector_signal = vector_dot / max(0.0001, query_norm * doc_norm)
        phrase = " ".join(term for term in cleaned_terms if term not in _DOCUMENT_CLAIM_RANK_LOW_SIGNAL_TERMS)[:160]
        phrase_bonus = 0.22 if phrase and phrase in folded_text else 0.0
        raw_available_bonus = 0.06 if str(row["raw_text"] or "").strip() else 0.0
        confidence_bonus = 0.03 * max(float(row["memory_confidence"] or 0.0), float(row["evidence_confidence"] or 0.0))
        raw_score = (
            bm25
            + title_score
            + phrase_bonus
            + raw_available_bonus
            + confidence_bonus
            + 0.35 * coverage
            + 1.35 * vector_signal
        )
        max_raw_score = max(max_raw_score, raw_score)
        raw_text_override = None if include_raw_text else str(row["summary_short"] or "")
        summary_override = None if include_raw_text else str(row["summary_short"] or "")
        node = _row_to_node(row, raw_text_override=raw_text_override, summary_override=summary_override)
        node["links"] = links_map.get(node["id"], [])
        node["highways"] = highways_map.get(node["id"], [])
        raw_rows.append((raw_score, str(row["id"]), node, matched, missing, idf_by_term, vector_signal))

    if not raw_rows:
        return []
    for rank, (raw_score, node_id, node, matched, missing, idf_by_term, vector_signal) in enumerate(
        sorted(raw_rows, key=lambda item: (-item[0], item[1])),
        start=1,
    ):
        normalized_score = raw_score / max(max_raw_score, 0.0001)
        node["document_claim_rank"] = {
            "schema_version": "agvm.document_claim_rank.v1",
            "rank": rank,
            "score": round(max(0.0, min(1.0, normalized_score)), 6),
            "raw_score": round(raw_score, 6),
            "matched_terms": matched[:24],
            "missing_terms": missing[:24],
            "query_terms": cleaned_terms[:48],
            "idf_by_term": idf_by_term,
            "vector_signal": round(vector_signal, 6),
            "candidate_count": candidate_count,
            "algorithm": "bounded_bm25_idf_document_anchor_claim_rank",
        }
        scored.append((-normalized_score, node_id, node))
    return [item[2] for item in scored[: max(1, int(limit or 24))]]


def fetch_nodes_by_text_terms(
    terms: list[str],
    *,
    limit: int = 24,
    include_raw_text: bool = False,
    busy_timeout_ms: int | None = None,
) -> list[dict[str, Any]]:
    cleaned_terms = [str(term or "").strip() for term in list(terms or []) if str(term or "").strip()]
    if not cleaned_terms:
        return []
    clauses: list[str] = []
    params: list[Any] = []
    for term in cleaned_terms:
        pattern = f"%{_escape_like_term(term)}%"
        clauses.append("(t.raw_text LIKE ? ESCAPE '\\' OR t.summary_full LIKE ? ESCAPE '\\' OR n.summary_short LIKE ? ESCAPE '\\')")
        params.extend([pattern, pattern, pattern])
    sql = f"""
        SELECT
            n.*,
            t.raw_text,
            t.summary_full,
            t.provenance_json,
            s.routing_brainhex_json,
            s.semantic_color_json,
            s.base_position_json,
            s.derivation_role,
            s.derivation_confidence,
            s.derived_from_preview_id,
            s.document_role,
            s.document_anchor_id,
            s.document_chunk_index,
            s.source_unit_id,
            s.source_unit_title,
            s.source_unit_kind,
            s.source_unit_role,
            s.promotion_role,
            s.source_unit_formation_strategy,
            s.source_span_start,
            s.source_span_end,
            s.retrieval_affordance_json,
            s.retrieval_aliases_json
        FROM nodes_nav n
        LEFT JOIN node_text t ON t.node_id = n.id
        LEFT JOIN node_semantics s ON s.node_id = n.id
        WHERE {" OR ".join(clauses)}
        LIMIT ?
    """
    # The SQL filter is intentionally broad (OR over source text, summary and label);
    # ranking happens in Python after hit counts and confidence are available. Keep
    # the prefetch window wide enough for larger local brains so high-signal nodes
    # are not lost before ranking.
    params.append(min(max(int(limit) * 12, 240), 3000))
    timeout_ms = 30000 if busy_timeout_ms is None else max(1, int(busy_timeout_ms))
    with connect(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
        rows = conn.execute(sql, params).fetchall()
        node_ids = [str(row["id"]) for row in rows]
        links_map = _fetch_links_map(conn, node_ids, "links")
        highways_map = _fetch_links_map(conn, node_ids, "highways")
    ranked: list[tuple[int, float, str, dict[str, Any]]] = []
    folded_terms = [term.lower() for term in cleaned_terms]
    for row in rows:
        raw_text_override = None if include_raw_text else str(row["summary_short"] or "")
        summary_override = None if include_raw_text else str(row["summary_short"] or "")
        node = _row_to_node(row, raw_text_override=raw_text_override, summary_override=summary_override)
        node["links"] = links_map.get(node["id"], [])
        node["highways"] = highways_map.get(node["id"], [])
        haystack = f"{row['raw_text'] or ''} {row['summary_full'] or ''} {row['summary_short'] or ''}".lower()
        hit_count = sum(1 for term in folded_terms if term in haystack)
        confidence = max(float(row["memory_confidence"] or 0.0), float(row["evidence_confidence"] or 0.0))
        ranked.append((-hit_count, -confidence, str(row["id"]), node))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in ranked[: int(limit)]]


def fetch_nodes_by_ids_summary(
    node_ids: list[str],
    *,
    busy_timeout_ms: int | None = None,
) -> list[dict[str, Any]]:
    cleaned_ids = list(dict.fromkeys(str(node_id or "").strip() for node_id in list(node_ids or []) if str(node_id or "").strip()))
    if not cleaned_ids:
        return []
    timeout_ms = 30000 if busy_timeout_ms is None else max(1, int(busy_timeout_ms))
    placeholders = ",".join("?" for _ in cleaned_ids)
    sql = f"""
        SELECT
            n.*,
            '' AS raw_text,
            n.summary_short AS summary_full,
            s.routing_brainhex_json,
            s.semantic_color_json,
            s.base_position_json,
            s.derivation_role,
            s.derivation_confidence,
            s.derived_from_preview_id,
            s.document_role,
            s.document_anchor_id,
            s.document_chunk_index,
            s.source_unit_id,
            s.source_unit_title,
            s.source_unit_kind,
            s.source_unit_role,
            s.promotion_role,
            s.source_unit_formation_strategy,
            s.source_span_start,
            s.source_span_end,
            s.retrieval_affordance_json,
            s.retrieval_aliases_json
        FROM nodes_nav n
        LEFT JOIN node_semantics s ON s.node_id = n.id
        WHERE n.id IN ({placeholders})
    """
    with connect(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
        rows = conn.execute(sql, cleaned_ids).fetchall()
        node_ids = [str(row["id"]) for row in rows]
        links_map = _fetch_links_map(conn, node_ids, "links")
        highways_map = _fetch_links_map(conn, node_ids, "highways")
    node_map: dict[str, dict[str, Any]] = {}
    for row in rows:
        summary_short = str(row["summary_short"] or "")
        node = _row_to_node(row, raw_text_override=summary_short, summary_override=summary_short)
        node["links"] = links_map.get(node["id"], [])
        node["highways"] = highways_map.get(node["id"], [])
        node_map[node["id"]] = node
    return [node_map[node_id] for node_id in cleaned_ids if node_id in node_map]


def fetch_nodes_by_summary_terms(
    terms: list[str],
    *,
    limit: int = 24,
    busy_timeout_ms: int | None = None,
) -> list[dict[str, Any]]:
    cleaned_terms = list(dict.fromkeys(str(term or "").strip() for term in list(terms or []) if str(term or "").strip()))[:48]
    if not cleaned_terms:
        return []
    timeout_ms = 30000 if busy_timeout_ms is None else max(1, int(busy_timeout_ms))
    clauses: list[str] = []
    params: list[Any] = []
    for term in cleaned_terms:
        pattern = f"%{_escape_like_term(term.lower())}%"
        clauses.append(
            "("
            "LOWER(COALESCE(n.summary_short, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(n.memory_type, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(n.guide_area, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(s.source_unit_title, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(s.retrieval_affordance_json, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(s.retrieval_aliases_json, '')) LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([pattern, pattern, pattern, pattern, pattern, pattern])
    prefetch_limit = min(max(int(limit) * 24, 240), 1800)
    sql = """
        SELECT
            n.*,
            '' AS raw_text,
            n.summary_short AS summary_full,
            s.routing_brainhex_json,
            s.semantic_color_json,
            s.base_position_json,
            s.derivation_role,
            s.derivation_confidence,
            s.derived_from_preview_id,
            s.document_role,
            s.document_anchor_id,
            s.document_chunk_index,
            s.source_unit_id,
            s.source_unit_title,
            s.source_unit_kind,
            s.source_unit_role,
            s.promotion_role,
            s.source_unit_formation_strategy,
            s.source_span_start,
            s.source_span_end,
            s.retrieval_affordance_json,
            s.retrieval_aliases_json
        FROM nodes_nav n
        LEFT JOIN node_semantics s ON s.node_id = n.id
        WHERE COALESCE(n.lifecycle_status, 'active') = 'active'
          AND (
    """
    sql += " OR ".join(clauses)
    sql += """
          )
        LIMIT ?
    """
    with connect(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
        rows = conn.execute(sql, [*params, prefetch_limit]).fetchall()
    ranked: list[tuple[int, float, str, sqlite3.Row]] = []
    folded_terms = [term.lower() for term in cleaned_terms]
    for row in rows:
        summary_short = str(row["summary_short"] or "")
        haystack = (
            f"{summary_short} {row['memory_type'] or ''} {row['guide_area'] or ''} "
            f"{row['source_unit_title'] or ''} {row['retrieval_affordance_json'] or ''} {row['retrieval_aliases_json'] or ''}"
        ).lower()
        hit_count = sum(1 for term in folded_terms if term in haystack)
        if hit_count <= 0:
            continue
        confidence = max(float(row["memory_confidence"] or 0.0), float(row["evidence_confidence"] or 0.0))
        ranked.append((-hit_count, -confidence, str(row["id"]), row))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    selected_rows = [item[3] for item in ranked[: int(limit)]]
    selected_ids = [str(row["id"]) for row in selected_rows]
    with connect(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
        links_map = _fetch_links_map(conn, selected_ids, "links")
        highways_map = _fetch_links_map(conn, selected_ids, "highways")
    nodes: list[dict[str, Any]] = []
    for row in selected_rows:
        summary_short = str(row["summary_short"] or "")
        node = _row_to_node(row, raw_text_override=summary_short, summary_override=summary_short)
        node["links"] = links_map.get(node["id"], [])
        node["highways"] = highways_map.get(node["id"], [])
        nodes.append(node)
    return nodes


def fetch_nav_node(node_id: str) -> dict[str, Any] | None:
    nodes = fetch_nodes_by_ids([node_id], include_raw_text=False)
    return nodes[0] if nodes else None


def replace_runtime_graph(graph: dict[str, Any]) -> dict[str, Any]:
    bootstrap_runtime_store()
    with connect() as conn:
        nodes = list(graph.get("nodes") or [])
        valid_node_ids = {str(node.get("id") or "") for node in nodes}
        conn.execute("DELETE FROM graph_edges")
        conn.execute("DELETE FROM links")
        conn.execute("DELETE FROM highways")
        conn.execute("DELETE FROM node_semantics")
        conn.execute("DELETE FROM node_text")
        conn.execute("DELETE FROM nodes_nav")
        conn.execute("DELETE FROM atlas_cache")
        conn.execute("DELETE FROM identity_nucleus_cache")
        for node in nodes:
            _upsert_node(conn, node)
        for node in nodes:
            _set_node_relations(conn, node, valid_target_ids=valid_node_ids)
        _set_graph_edges(conn, list(graph.get("edges") or []), valid_node_ids=valid_node_ids)
        conn.commit()
    rebuild_identity_nucleus_cache()
    atlas = rebuild_atlas_cache()
    canonical_graph = fetch_graph_snapshot()
    write_legacy_exports(canonical_graph, atlas)
    return canonical_graph


def _clean_position_updates(updates: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in list(updates or []):
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "").strip()
        position = _dict_value(item.get("to_position"))
        if not node_id or not {"x", "y", "z"}.issubset(position):
            continue
        try:
            cleaned.append(
                {
                    "node_id": node_id,
                    "to_position": {
                        "x": float(position["x"]),
                        "y": float(position["y"]),
                        "z": float(position["z"]),
                    },
                    "reason_codes": list(item.get("reason_codes") or []),
                    "proposal_code": str(item.get("proposal_code") or ""),
                }
            )
        except (TypeError, ValueError):
            continue
    return cleaned


def apply_node_position_updates(updates: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply reviewed coordinate updates without touching text, links or semantic eligibility."""
    bootstrap_runtime_store()
    cleaned = _clean_position_updates(updates)
    if not cleaned:
        return {
            "schema_version": "agvm.matrix_position_apply_result.v1",
            "requested_update_count": 0,
            "applied_update_count": 0,
            "missing_node_ids": [],
            "updated_node_ids": [],
        }
    updated_node_ids: list[str] = []
    missing_node_ids: list[str] = []
    with connect() as conn:
        for item in cleaned:
            node_id = str(item["node_id"])
            row = conn.execute("SELECT id FROM nodes_nav WHERE id = ?", (node_id,)).fetchone()
            if not row:
                missing_node_ids.append(node_id)
                continue
            position = dict(item["to_position"])
            coarse_bucket = _bucket_at(position, COARSE_BUCKET_SIZE)
            fine_bucket = _bucket_at(position, FINE_BUCKET_SIZE)
            topology_brainhex = position_to_topology_brainhex(position)
            topology_color = color_from_brainhex(topology_brainhex)
            conn.execute(
                """
                UPDATE nodes_nav
                SET x = ?,
                    y = ?,
                    z = ?,
                    coarse_bucket_x = ?,
                    coarse_bucket_y = ?,
                    coarse_bucket_z = ?,
                    coarse_bucket_key = ?,
                    fine_bucket_x = ?,
                    fine_bucket_y = ?,
                    fine_bucket_z = ?,
                    fine_bucket_key = ?,
                    topology_brainhex_json = ?,
                    topology_color_json = ?
                WHERE id = ?
                """,
                (
                    float(position["x"]),
                    float(position["y"]),
                    float(position["z"]),
                    int(coarse_bucket["x"]),
                    int(coarse_bucket["y"]),
                    int(coarse_bucket["z"]),
                    str(coarse_bucket["key"]),
                    int(fine_bucket["x"]),
                    int(fine_bucket["y"]),
                    int(fine_bucket["z"]),
                    str(fine_bucket["key"]),
                    _json_dump(topology_brainhex),
                    _json_dump(topology_color),
                    node_id,
                ),
            )
            updated_node_ids.append(node_id)
        conn.commit()
    atlas = rebuild_atlas_cache()
    graph = fetch_graph_snapshot()
    write_legacy_exports(graph, atlas)
    return {
        "schema_version": "agvm.matrix_position_apply_result.v1",
        "requested_update_count": len(cleaned),
        "applied_update_count": len(updated_node_ids),
        "missing_node_ids": missing_node_ids,
        "updated_node_ids": updated_node_ids,
        "atlas_bucket_count": int(atlas.get("bucket_count") or 0),
        "graph_view_refreshed": True,
        "touched_fields": [
            "nodes_nav.x",
            "nodes_nav.y",
            "nodes_nav.z",
            "nodes_nav.coarse_bucket_*",
            "nodes_nav.fine_bucket_*",
            "nodes_nav.topology_brainhex_json",
            "nodes_nav.topology_color_json",
        ],
        "untouched_surfaces": [
            "node_text",
            "node_semantics.base_position_json",
            "links",
            "highways",
            "graph_edges",
            "answer_eligible",
            "profile_eligible",
            "document_eligible",
        ],
    }



# Paid Geometry Calibration apply and rollback persistence is not part of Public Core.

def write_legacy_exports(graph: dict[str, Any], atlas_payload: dict[str, Any]) -> None:
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    atomic_write_json(
        current_graph_path(),
        {
            "version": graph.get("version", GRAPH_VERSION),
            "graph_name": graph.get("graph_name", APP_NAME),
            "nodes": nodes,
            "edges": edges,
            "meta": {
                **dict(graph.get("meta") or {}),
                "runtime_source": "sqlite",
                "canonical_storage": "sqlite",
                "export_note": "Legacy JSON export is synchronized from the SQLite runtime store for local compatibility.",
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        },
    )
    atomic_write_json(
        current_index_path(),
        {
            **empty_index(),
            "updated_at": utc_timestamp(),
            "runtime_source": "sqlite",
            "canonical_storage": "sqlite",
            "note": "Legacy index export intentionally empty; runtime uses SQLite navigation store.",
        },
    )
    atomic_write_json(current_atlas_path(), atlas_payload)
    atomic_write_json(current_graph_view_path(), build_graph_view(graph, max_nodes=min(1600, max(100, len(nodes)))))


def _sample_node_ids_by_bucket(rows: list[sqlite3.Row], max_nodes: int) -> list[str]:
    if len(rows) <= max_nodes:
        return [str(row["id"]) for row in rows]
    buckets: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        buckets[str(row["coarse_bucket_key"])].append(row)
    for bucket_rows in buckets.values():
        bucket_rows.sort(
            key=lambda item: (
                -float(item["memory_confidence"] or 0.0),
                -float(item["stability_confidence"] or 0.0),
                str(item["id"]),
            )
        )
    selected: list[str] = []
    bucket_lists = list(buckets.values())
    round_index = 0
    while len(selected) < max_nodes:
        progressed = False
        for bucket_rows in bucket_lists:
            if round_index < len(bucket_rows):
                node_id = str(bucket_rows[round_index]["id"])
                if node_id not in selected:
                    selected.append(node_id)
                    progressed = True
                    if len(selected) >= max_nodes:
                        break
        if not progressed:
            break
        round_index += 1
    return selected


def fetch_graph_view(
    *,
    max_nodes: int = 1600,
    memory_type: str | None = None,
    guide_area: str | None = None,
    bucket_window: dict[str, int] | None = None,
    confidence_floor: float | None = None,
) -> dict[str, Any]:
    filters = []
    params: list[Any] = []
    if memory_type:
        filters.append("memory_type = ?")
        params.append(memory_type)
    if guide_area:
        filters.append("guide_area = ?")
        params.append(guide_area)
    if confidence_floor is not None:
        filters.append("COALESCE(memory_confidence, 0.0) >= ?")
        params.append(float(confidence_floor))
    if bucket_window:
        filters.extend(
            [
                "coarse_bucket_x BETWEEN ? AND ?",
                "coarse_bucket_y BETWEEN ? AND ?",
                "coarse_bucket_z BETWEEN ? AND ?",
            ]
        )
        params.extend(
            [
                int(bucket_window["min_bx"]),
                int(bucket_window["max_bx"]),
                int(bucket_window["min_by"]),
                int(bucket_window["max_by"]),
                int(bucket_window["min_bz"]),
                int(bucket_window["max_bz"]),
            ]
        )
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, coarse_bucket_key, memory_confidence, stability_confidence
            FROM nodes_nav
            {where_clause}
            ORDER BY COALESCE(memory_confidence, 0.0) DESC, id ASC
            """,
            params,
        ).fetchall()
        total_nodes = len(rows)
        selected_ids = _sample_node_ids_by_bucket(rows, max_nodes=max_nodes)
        nodes = fetch_nodes_by_ids(selected_ids, include_raw_text=False)
        visible = set(selected_ids)
        edges = _fetch_edge_rows(conn, selected_ids)
        edges = [edge for edge in edges if edge["source_node_id"] in visible and edge["target_node_id"] in visible]
    return {
        "version": GRAPH_VERSION,
        "graph_name": APP_NAME,
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "created_from": "agvm_sqlite_runtime_store",
            "graph_updated_at": utc_timestamp(),
            "view_mode": "render",
            "sampled": total_nodes > len(nodes),
            "total_node_count": total_nodes,
            "total_edge_count": len(edges),
            "sampled_node_count": len(nodes),
            "sampled_edge_count": len(edges),
        },
    }


def _coarse_bucket_key(row: sqlite3.Row) -> str:
    return f"{int(row['coarse_bucket_x'])}:{int(row['coarse_bucket_y'])}:{int(row['coarse_bucket_z'])}"


def rebuild_identity_nucleus_cache() -> dict[str, Any]:
    from derivation import build_identity_nucleus

    payload = build_identity_nucleus(fetch_graph_snapshot())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO identity_nucleus_cache (cache_key, payload_json, updated_at)
            VALUES ('main', ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """,
            (_json_dump(payload), utc_timestamp()),
        )
        conn.commit()
    return payload


def _identity_nucleus_cache_compatible(payload: dict[str, Any]) -> bool:
    if not payload:
        return False
    required_keys = {
        "core_name",
        "primary_self_node_id",
        "self_name_candidates",
        "aliases",
        "partner_candidates",
        "mentor_candidates",
        "sibling_candidates",
        "role_candidates",
        "employer_candidates",
        "project_candidates",
        "self_support_node_ids",
        "partner_support_node_ids",
        "mentor_support_node_ids",
        "sibling_support_node_ids",
        "role_support_node_ids",
        "project_support_node_ids",
        "employer_support_node_ids",
        "core_nodes",
    }
    if not required_keys.issubset(set(payload.keys())):
        return False
    for list_key in (
        "self_name_candidates",
        "aliases",
        "partner_candidates",
        "mentor_candidates",
        "sibling_candidates",
        "role_candidates",
        "employer_candidates",
        "project_candidates",
        "self_support_node_ids",
        "partner_support_node_ids",
        "mentor_support_node_ids",
        "sibling_support_node_ids",
        "role_support_node_ids",
        "project_support_node_ids",
        "employer_support_node_ids",
        "core_nodes",
    ):
        if not isinstance(payload.get(list_key), list):
            return False
    if not payload.get("core_name") and not list(payload.get("self_name_candidates") or []):
        return False
    return True


def fetch_identity_nucleus() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT payload_json FROM identity_nucleus_cache WHERE cache_key = 'main'").fetchone()
    if row:
        payload = _json_load(row["payload_json"], {})
        if _identity_nucleus_cache_compatible(payload):
            return payload
    return rebuild_identity_nucleus_cache()


def rebuild_atlas_cache() -> dict[str, Any]:
    with connect() as conn:
        nav_rows = conn.execute(
            """
            SELECT id, x, y, z, coarse_bucket_key, guide_area, is_document_anchor
            FROM nodes_nav
            """
        ).fetchall()
        highway_rows = conn.execute("SELECT source_id, target_id, strength FROM highways").fetchall()
        nodes_by_bucket: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in nav_rows:
            nodes_by_bucket[str(row["coarse_bucket_key"])].append(row)

        bucket_for_node = {str(row["id"]): str(row["coarse_bucket_key"]) for row in nav_rows}
        gateways_by_bucket: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for highway in highway_rows:
            source_bucket = bucket_for_node.get(str(highway["source_id"]))
            if not source_bucket:
                continue
            gateways_by_bucket[source_bucket][str(highway["target_id"])] += float(highway["strength"] or 0.0)

        conn.execute("DELETE FROM atlas_cache")
        buckets_payload: list[dict[str, Any]] = []
        for bucket_key, bucket_rows in sorted(nodes_by_bucket.items()):
            centroid = {
                "x": sum(float(row["x"]) for row in bucket_rows) / len(bucket_rows),
                "y": sum(float(row["y"]) for row in bucket_rows) / len(bucket_rows),
                "z": sum(float(row["z"]) for row in bucket_rows) / len(bucket_rows),
            }
            guide_histogram: dict[str, int] = defaultdict(int)
            for row in bucket_rows:
                guide_area = str(row["guide_area"] or "")
                if guide_area:
                    guide_histogram[guide_area] += 1
            dominant_direction_hint = {"centroid_norm": math.sqrt(centroid["x"] ** 2 + centroid["y"] ** 2 + centroid["z"] ** 2)}
            gateways = [
                {"target_node_id": target_id, "strength": round(min(1.0, weight), 4), "reason": "atlas_gateway"}
                for target_id, weight in sorted(gateways_by_bucket[bucket_key].items(), key=lambda item: (-item[1], item[0]))[:6]
            ]
            payload = {
                "bucket_key": bucket_key,
                "centroid": centroid,
                "node_count": len(bucket_rows),
                "document_anchor_count": sum(1 for row in bucket_rows if bool(row["is_document_anchor"])),
                "guide_area_histogram": dict(guide_histogram),
                "dominant_direction_hint": dominant_direction_hint,
                "outgoing_highway_gateways": gateways,
            }
            buckets_payload.append(payload)
            conn.execute(
                """
                INSERT INTO atlas_cache (
                    bucket_key, granularity, centroid_x, centroid_y, centroid_z,
                    node_count, document_anchor_count,
                    guide_area_histogram_json, dominant_direction_hint_json, outgoing_highway_gateways_json
                ) VALUES (?, 'coarse', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bucket_key,
                    float(centroid["x"]),
                    float(centroid["y"]),
                    float(centroid["z"]),
                    int(payload["node_count"]),
                    int(payload["document_anchor_count"]),
                    _json_dump(payload["guide_area_histogram"]),
                    _json_dump(dominant_direction_hint),
                    _json_dump(gateways),
                ),
            )
        conn.commit()
    atlas_payload = {
        "version": ATLAS_VERSION,
        "bucket_size": COARSE_BUCKET_SIZE,
        "generated_at": utc_timestamp(),
        "node_count": len(nav_rows),
        "bucket_count": len(buckets_payload),
        "buckets": buckets_payload,
    }
    atomic_write_json(current_atlas_path(), atlas_payload)
    return atlas_payload


def fetch_atlas() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT bucket_key, centroid_x, centroid_y, centroid_z, node_count, document_anchor_count,
                   guide_area_histogram_json, dominant_direction_hint_json, outgoing_highway_gateways_json
            FROM atlas_cache
            ORDER BY bucket_key
            """
        ).fetchall()
        node_count = conn.execute("SELECT COUNT(*) FROM nodes_nav").fetchone()[0]
    if not rows and node_count:
        return rebuild_atlas_cache()
    return {
        "version": ATLAS_VERSION,
        "bucket_size": COARSE_BUCKET_SIZE,
        "generated_at": utc_timestamp(),
        "node_count": int(node_count),
        "bucket_count": len(rows),
        "buckets": [
            {
                "bucket_key": str(row["bucket_key"]),
                "centroid": {"x": float(row["centroid_x"]), "y": float(row["centroid_y"]), "z": float(row["centroid_z"])},
                "node_count": int(row["node_count"]),
                "document_anchor_count": int(row["document_anchor_count"]),
                "guide_area_histogram": _json_load(row["guide_area_histogram_json"], {}),
                "dominant_direction_hint": _json_load(row["dominant_direction_hint_json"], {}),
                "outgoing_highway_gateways": _json_load(row["outgoing_highway_gateways_json"], []),
            }
            for row in rows
        ],
    }


def reset_runtime_store() -> tuple[dict[str, Any], dict[str, Any]]:
    bootstrap_runtime_store()
    with connect() as conn:
        conn.execute("DELETE FROM graph_edges")
        conn.execute("DELETE FROM links")
        conn.execute("DELETE FROM highways")
        conn.execute("DELETE FROM node_semantics")
        conn.execute("DELETE FROM node_text")
        conn.execute("DELETE FROM nodes_nav")
        conn.execute("DELETE FROM atlas_cache")
        conn.execute("DELETE FROM identity_nucleus_cache")
        conn.commit()
    reset_legacy_exports()
    return fetch_graph_snapshot(), fetch_atlas()


def compute_bucket_window(center: dict[str, float], radius: float, *, bucket_size: float) -> dict[str, int]:
    bx = math.floor(float(center["x"]) / bucket_size)
    by = math.floor(float(center["y"]) / bucket_size)
    bz = math.floor(float(center["z"]) / bucket_size)
    d = math.ceil(float(radius) / bucket_size)
    return {
        "min_bx": bx - d,
        "max_bx": bx + d,
        "min_by": by - d,
        "max_by": by + d,
        "min_bz": bz - d,
        "max_bz": bz + d,
    }


def query_nearby_navigation(center: dict[str, float], *, radius: float, limit: int = 24) -> list[dict[str, Any]]:
    window = compute_bucket_window(center, radius, bucket_size=FINE_BUCKET_SIZE)
    qx = float(center["x"])
    qy = float(center["y"])
    qz = float(center["z"])
    r2 = float(radius) ** 2
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                n.*,
                s.routing_brainhex_json,
                s.semantic_color_json,
                s.base_position_json,
                s.derivation_role,
                s.derivation_confidence,
                s.derived_from_preview_id,
                s.source_span_start,
                s.source_span_end
            FROM nodes_nav n
            LEFT JOIN node_semantics s ON s.node_id = n.id
            WHERE n.fine_bucket_x BETWEEN ? AND ?
              AND n.fine_bucket_y BETWEEN ? AND ?
              AND n.fine_bucket_z BETWEEN ? AND ?
              AND ((n.x - ?) * (n.x - ?) + (n.y - ?) * (n.y - ?) + (n.z - ?) * (n.z - ?)) <= ?
            ORDER BY ((n.x - ?) * (n.x - ?) + (n.y - ?) * (n.y - ?) + (n.z - ?) * (n.z - ?)) ASC, n.id ASC
            LIMIT ?
            """,
            (
                window["min_bx"],
                window["max_bx"],
                window["min_by"],
                window["max_by"],
                window["min_bz"],
                window["max_bz"],
                qx,
                qx,
                qy,
                qy,
                qz,
                qz,
                r2,
                qx,
                qx,
                qy,
                qy,
                qz,
                qz,
                int(limit),
            ),
        ).fetchall()
        node_ids = [str(row["id"]) for row in rows]
        links_map = _fetch_links_map(conn, node_ids, "links")
        highways_map = _fetch_links_map(conn, node_ids, "highways")
    nodes = []
    for row in rows:
        summary_short = str(row["summary_short"] or "")
        node = _row_to_node(row, raw_text_override=summary_short, summary_override=summary_short)
        node["links"] = links_map.get(node["id"], [])
        node["highways"] = highways_map.get(node["id"], [])
        nodes.append(node)
    return nodes


def fetch_nav_by_coarse_bucket_keys(bucket_keys: list[str], *, limit: int = 32, document_anchor_only: bool = False) -> list[dict[str, Any]]:
    if not bucket_keys:
        return []
    placeholders = ",".join("?" for _ in bucket_keys)
    sql = f"""
        SELECT
            n.*,
            s.routing_brainhex_json,
            s.semantic_color_json,
            s.base_position_json,
            s.derivation_role,
            s.derivation_confidence,
            s.derived_from_preview_id,
            s.source_span_start,
            s.source_span_end
        FROM nodes_nav n
        LEFT JOIN node_semantics s ON s.node_id = n.id
        WHERE n.coarse_bucket_key IN ({placeholders})
    """
    params: list[Any] = list(bucket_keys)
    if document_anchor_only:
        sql += " AND n.is_document_anchor = 1"
    sql += " ORDER BY COALESCE(n.memory_confidence, 0.0) DESC, n.id ASC LIMIT ?"
    params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        node_ids = [str(row["id"]) for row in rows]
        links_map = _fetch_links_map(conn, node_ids, "links")
        highways_map = _fetch_links_map(conn, node_ids, "highways")
    nodes = []
    for row in rows:
        summary_short = str(row["summary_short"] or "")
        node = _row_to_node(row, raw_text_override=summary_short, summary_override=summary_short)
        node["links"] = links_map.get(node["id"], [])
        node["highways"] = highways_map.get(node["id"], [])
        nodes.append(node)
    return nodes


def fetch_cluster_runtime(node_id: str, *, max_candidates: int = 24, radius: float = 0.32) -> dict[str, Any] | None:
    focus = fetch_nav_node(node_id)
    if not focus:
        return None
    nearby_nodes = [node for node in query_nearby_navigation(dict(focus["final_position"]), radius=radius, limit=max_candidates + 1) if node["id"] != node_id]
    candidate_ids = [node["id"] for node in nearby_nodes[:max_candidates]]
    link_ids = [str(link["target_node_id"]) for link in list(focus.get("links") or [])]
    highway_ids = [str(link["target_node_id"]) for link in list(focus.get("highways") or [])]
    document_anchor_candidate_ids = [node["id"] for node in nearby_nodes if node.get("is_document_anchor")]
    cluster_node_ids = list(dict.fromkeys([node_id, *candidate_ids, *link_ids, *highway_ids]))
    derivation_edges = []
    for edge in fetch_graph_edges_for_nodes(cluster_node_ids):
        if edge["source_node_id"] == node_id or edge["target_node_id"] == node_id:
            derivation_edges.append(
                {
                    "source": edge["source_node_id"],
                    "target": edge["target_node_id"],
                    "kind": "derivation",
                    "strength": float(edge.get("confidence") or 0.0),
                    "sources": [str(edge.get("edge_type") or "derivation")],
                }
            )
    debug_edges: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        debug_edges.append(
            {
                "source": node_id,
                "target": candidate_id,
                "kind": "candidate",
                "sources": ["nearby_radius"],
            }
        )
    for link in list(focus.get("links") or []):
        debug_edges.append(
            {
                "source": node_id,
                "target": str(link["target_node_id"]),
                "kind": "link",
                "strength": float(link.get("strength") or 0.0),
                "sources": [],
            }
        )
    for highway in list(focus.get("highways") or []):
        debug_edges.append(
            {
                "source": node_id,
                "target": str(highway["target_node_id"]),
                "kind": "highway",
                "strength": float(highway.get("strength") or 0.0),
                "sources": [],
            }
        )
    debug_edges.extend(derivation_edges)
    return {
        "focus_node_id": str(node_id),
        "cluster_node_ids": cluster_node_ids,
        "candidate_ids": candidate_ids,
        "origin_node_id": None,
        "bucket_key": str((focus.get("bucket") or {}).get("key") or ""),
        "candidate_sources": {candidate_id: ["nearby_radius"] for candidate_id in candidate_ids},
        "document_anchor_candidate_ids": list(dict.fromkeys(document_anchor_candidate_ids)),
        "highway_expansion_ids": list(dict.fromkeys(highway_ids)),
        "debug_edges": debug_edges,
    }


def create_search_session(search_id: str, request_payload: dict[str, Any]) -> None:
    timestamp = utc_timestamp()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO search_sessions (
                search_id, thread_id, query_text, response_mode, status,
                request_json, plan_json, result_json, stop_reason, answerability_state,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'created', ?, NULL, NULL, NULL, NULL, ?, ?)
            ON CONFLICT(search_id) DO UPDATE SET
                thread_id=excluded.thread_id,
                query_text=excluded.query_text,
                response_mode=excluded.response_mode,
                status='created',
                request_json=excluded.request_json,
                plan_json=NULL,
                plan_health_json=NULL,
                result_json=NULL,
                result_health_json=NULL,
                stop_reason=NULL,
                answerability_state=NULL,
                updated_at=excluded.updated_at
            """,
            (
                str(search_id),
                str(request_payload.get("thread_id") or "") or None,
                str(request_payload.get("query_text") or ""),
                str(request_payload.get("response_mode") or "both"),
                _json_dump(request_payload),
                timestamp,
                timestamp,
            ),
        )
        conn.commit()


def save_search_plan(search_id: str, plan_payload: dict[str, Any]) -> None:
    plan_health_json = _json_dump(_search_plan_health_summary(plan_payload))
    with connect() as conn:
        conn.execute(
            """
            UPDATE search_sessions
            SET plan_json = ?, plan_health_json = ?, updated_at = ?
            WHERE search_id = ?
            """,
            (_json_dump(plan_payload), plan_health_json, utc_timestamp(), str(search_id)),
        )
        conn.commit()


def mark_search_running(search_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE search_sessions SET status = 'running', updated_at = ? WHERE search_id = ?",
            (utc_timestamp(), str(search_id)),
        )
        conn.commit()


def append_search_event(search_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    created_at = utc_timestamp()
    preview_event = annotate_stream_event(
        {
            "seq": 0,
            "event_type": str(event_type),
            "payload": dict(payload or {}),
            "created_at": created_at,
        }
    )
    annotated_payload = dict(preview_event.get("payload") or payload or {})
    with connect() as conn:
        session_exists = conn.execute(
            "SELECT 1 FROM search_sessions WHERE search_id = ? LIMIT 1",
            (str(search_id),),
        ).fetchone()
        if not session_exists:
            return annotate_stream_event(
                {
                    "seq": 0,
                    "event_type": str(event_type),
                    "payload": {
                        **annotated_payload,
                        "event_store_status": "skipped_missing_search_session",
                        "event_store_reason": "search_session_not_found",
                    },
                    "created_at": created_at,
                }
            )
        try:
            cursor = conn.execute(
                """
                INSERT INTO search_events (search_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(search_id), str(event_type), _json_dump(annotated_payload), created_at),
            )
            conn.execute("UPDATE search_sessions SET updated_at = ? WHERE search_id = ?", (created_at, str(search_id)))
            conn.commit()
            seq = int(cursor.lastrowid or 0)
        except sqlite3.IntegrityError:
            return annotate_stream_event(
                {
                    "seq": 0,
                    "event_type": str(event_type),
                    "payload": {
                        **annotated_payload,
                        "event_store_status": "skipped_missing_search_session",
                        "event_store_reason": "search_session_deleted_during_insert",
                    },
                    "created_at": created_at,
                }
            )
    return annotate_stream_event(
        {
            "seq": seq,
            "event_type": str(event_type),
            "payload": annotated_payload,
            "created_at": created_at,
        }
    )


def fetch_search_events(
    search_id: str,
    *,
    after_seq: int = 0,
    limit: int = 200,
    busy_timeout_ms: int | None = None,
) -> list[dict[str, Any]]:
    timeout_ms = 30000 if busy_timeout_ms is None else max(1, int(busy_timeout_ms))
    with connect(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
        rows = conn.execute(
            """
            SELECT seq, event_type, payload_json, created_at
            FROM search_events
            WHERE search_id = ? AND seq > ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (str(search_id), int(after_seq), int(limit)),
        ).fetchall()
    return [
        annotate_stream_event(
            {
                "seq": int(row["seq"]),
                "event_type": str(row["event_type"]),
                "payload": _json_load(row["payload_json"], {}),
                "created_at": str(row["created_at"]),
            }
        )
        for row in rows
    ]


def fetch_search_events_by_type(
    search_id: str,
    event_types: list[str] | tuple[str, ...] | set[str],
    *,
    after_seq: int = 0,
    limit: int = 200,
    busy_timeout_ms: int | None = None,
) -> list[dict[str, Any]]:
    types = [str(item).strip() for item in list(event_types or []) if str(item).strip()]
    if not types:
        return []
    timeout_ms = 30000 if busy_timeout_ms is None else max(1, int(busy_timeout_ms))
    placeholders = ",".join("?" for _ in types)
    with connect(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
        rows = conn.execute(
            f"""
            SELECT seq, event_type, payload_json, created_at
            FROM search_events
            WHERE search_id = ? AND seq > ? AND event_type IN ({placeholders})
            ORDER BY seq ASC
            LIMIT ?
            """,
            (str(search_id), int(after_seq), *types, int(limit)),
        ).fetchall()
    return [
        annotate_stream_event(
            {
                "seq": int(row["seq"]),
                "event_type": str(row["event_type"]),
                "payload": _json_load(row["payload_json"], {}),
                "created_at": str(row["created_at"]),
            }
        )
        for row in rows
    ]


def finalize_search_session(search_id: str, result_payload: dict[str, Any]) -> None:
    result_json = _json_dump(result_payload)
    result_health_json = _json_dump(_search_result_health_summary(result_payload, result_json_length=len(result_json)))
    with connect() as conn:
        conn.execute(
            """
            UPDATE search_sessions
            SET status = 'completed',
                result_json = ?,
                result_health_json = ?,
                stop_reason = ?,
                answerability_state = ?,
                updated_at = ?
            WHERE search_id = ?
            """,
            (
                result_json,
                result_health_json,
                str(result_payload.get("stop_reason") or ""),
                str(result_payload.get("answerability_state") or ""),
                utc_timestamp(),
                str(search_id),
            ),
        )
        conn.commit()


def save_search_result_snapshot(search_id: str, result_payload: dict[str, Any]) -> None:
    result_json = _json_dump(result_payload)
    result_health_json = _json_dump(_search_result_health_summary(result_payload, result_json_length=len(result_json)))
    with connect() as conn:
        conn.execute(
            """
            UPDATE search_sessions
            SET result_json = ?,
                result_health_json = ?,
                stop_reason = ?,
                answerability_state = ?,
                updated_at = ?
            WHERE search_id = ? AND status NOT IN ('completed', 'failed')
            """,
            (
                result_json,
                result_health_json,
                str(result_payload.get("stop_reason") or ""),
                str(result_payload.get("answerability_state") or ""),
                utc_timestamp(),
                str(search_id),
            ),
        )
        conn.commit()


def fail_search_session(search_id: str, error_message: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE search_sessions
            SET status = 'failed',
                stop_reason = ?,
                result_health_json = NULL,
                updated_at = ?
            WHERE search_id = ?
            """,
            (str(error_message), utc_timestamp(), str(search_id)),
        )
        conn.commit()


def fetch_search_session(
    search_id: str,
    *,
    busy_timeout_ms: int | None = None,
    return_on_busy: bool = False,
    include_result: bool = True,
) -> dict[str, Any] | None:
    timeout_ms = 30000 if busy_timeout_ms is None else max(1, int(busy_timeout_ms))
    result_select = "result_json" if include_result else "result_health_json AS result_json"
    result_length_select = "length(result_json)" if include_result else "COALESCE(json_extract(result_health_json, '$.result_json_length'), 0)"
    try:
        with connect(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
            row = conn.execute(
                f"""
                SELECT search_id, thread_id, query_text, response_mode, status, request_json, plan_json,
                       {result_select}, {result_length_select} AS result_json_length,
                       stop_reason, answerability_state, created_at, updated_at
                FROM search_sessions
                WHERE search_id = ?
                """,
                (str(search_id),),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if return_on_busy and "locked" in str(exc).lower():
            return None
        raise
    if not row:
        return None
    return _search_session_from_row(row)


def _search_session_health_plan_projection_sql() -> str:
    return """
        json_object(
            'planner_runtime', json_object(
                'semantic_contract_ai_required',
                    COALESCE(
                        json_extract(plan_json, '$.planner_runtime.semantic_contract_ai_required'),
                        json_extract(plan_json, '$.runtime.semantic_contract_ai_required')
                    ),
                'semantic_contract_material',
                    COALESCE(
                        json_extract(plan_json, '$.planner_runtime.semantic_contract_material'),
                        json_extract(plan_json, '$.runtime.semantic_contract_material')
                    ),
                'semantic_contract_status',
                    COALESCE(
                        json_extract(plan_json, '$.planner_runtime.semantic_contract_status'),
                        json_extract(plan_json, '$.runtime.semantic_contract_status')
                    ),
                'planner_path',
                    COALESCE(
                        json_extract(plan_json, '$.planner_runtime.planner_path'),
                        json_extract(plan_json, '$.runtime.planner_path')
                    ),
                'planner_kind',
                    COALESCE(
                        json_extract(plan_json, '$.planner_runtime.planner_kind'),
                        json_extract(plan_json, '$.runtime.planner_kind')
                    ),
                'heuristic_provisional',
                    COALESCE(
                        json_extract(plan_json, '$.planner_runtime.heuristic_provisional'),
                        json_extract(plan_json, '$.runtime.heuristic_provisional')
                    )
            ),
            'semantic_contract_runtime', json_object(
                'ai_required',
                    COALESCE(
                        json_extract(plan_json, '$.semantic_contract_runtime.ai_required'),
                        json_extract(plan_json, '$.planner_runtime.semantic_contract_runtime.ai_required')
                    ),
                'enabled',
                    COALESCE(
                        json_extract(plan_json, '$.semantic_contract_runtime.enabled'),
                        json_extract(plan_json, '$.planner_runtime.semantic_contract_runtime.enabled')
                    ),
                'material',
                    COALESCE(
                        json_extract(plan_json, '$.semantic_contract_runtime.material'),
                        json_extract(plan_json, '$.planner_runtime.semantic_contract_runtime.material')
                    ),
                'status',
                    COALESCE(
                        json_extract(plan_json, '$.semantic_contract_runtime.status'),
                        json_extract(plan_json, '$.planner_runtime.semantic_contract_runtime.status')
                    )
            ),
            'search_map_2d_truth', json_object(
                'required', json_extract(plan_json, '$.search_map_2d_truth.required'),
                'ready', json_extract(plan_json, '$.search_map_2d_truth.ready'),
                'route_event_count', json_extract(plan_json, '$.search_map_2d_truth.route_event_count'),
                'travel', json_extract(plan_json, '$.search_map_2d_truth.travel'),
                'route_steps', json_extract(plan_json, '$.search_map_2d_truth.route_steps'),
                'pending_path_count', json_extract(plan_json, '$.search_map_2d_truth.pending_path_count'),
                'pending', json_extract(plan_json, '$.search_map_2d_truth.pending'),
                'path_count', json_extract(plan_json, '$.search_map_2d_truth.path_count')
            )
        )
    """


def _search_session_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "search_id": str(row["search_id"]),
        "thread_id": str(row["thread_id"]) if row["thread_id"] else None,
        "query_text": str(row["query_text"]),
        "response_mode": str(row["response_mode"]),
        "status": str(row["status"]),
        "request": _json_load(row["request_json"], {}),
        "plan": _json_load(row["plan_json"], {}) if row["plan_json"] else None,
        "result": _json_load(row["result_json"], {}) if row["result_json"] else None,
        "result_json_length": int(row["result_json_length"] or 0),
        "stop_reason": row["stop_reason"],
        "answerability_state": row["answerability_state"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def fetch_recent_search_sessions(
    *,
    limit: int = 20,
    include_result: bool = True,
    read_only: bool = False,
    busy_timeout_ms: int | None = None,
) -> list[dict[str, Any]]:
    timeout_ms = 2000 if read_only else 30000
    if busy_timeout_ms is not None:
        timeout_ms = max(1, int(busy_timeout_ms))
    connection_factory = connect_readonly if read_only else connect
    with connection_factory(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
        columns = _table_columns(conn, "search_sessions")
        if not columns:
            return []
        plan_projection = _search_session_health_plan_projection_sql() if "plan_json" in columns else "NULL"
        if include_result:
            plan_select = "plan_json" if "plan_json" in columns else "NULL"
            result_select = _select_column_or_sql(columns, "result_json", "NULL", alias="result_json")
            result_length_select = "length(result_json)" if "result_json" in columns else "0"
        else:
            if "plan_health_json" in columns:
                plan_select = f"COALESCE(plan_health_json, {plan_projection})"
            else:
                plan_select = plan_projection
            result_select = (
                "result_health_json AS result_json"
                if "result_health_json" in columns
                else "NULL AS result_json"
            )
            if "result_health_json" in columns:
                result_length_select = "COALESCE(json_extract(result_health_json, '$.result_json_length'), 0)"
            elif "result_json" in columns:
                result_length_select = "COALESCE(length(result_json), 0)"
            else:
                result_length_select = "0"
        order_column = "updated_at" if "updated_at" in columns else "created_at" if "created_at" in columns else "search_id"
        rows = conn.execute(
            f"""
            SELECT
                   {_select_column_or_sql(columns, "search_id", "''", alias="search_id")},
                   {_select_column_or_sql(columns, "thread_id", "NULL", alias="thread_id")},
                   {_select_column_or_sql(columns, "query_text", "''", alias="query_text")},
                   {_select_column_or_sql(columns, "response_mode", "''", alias="response_mode")},
                   {_select_column_or_sql(columns, "status", "'unknown'", alias="status")},
                   {_select_column_or_sql(columns, "request_json", "'{{}}'", alias="request_json")},
                   {plan_select} AS plan_json,
                   {result_select}, {result_length_select} AS result_json_length,
                   {_select_column_or_sql(columns, "stop_reason", "NULL", alias="stop_reason")},
                   {_select_column_or_sql(columns, "answerability_state", "NULL", alias="answerability_state")},
                   {_select_column_or_sql(columns, "created_at", "''", alias="created_at")},
                   {_select_column_or_sql(columns, "updated_at", "''", alias="updated_at")}
            FROM search_sessions
            ORDER BY {order_column} DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [_search_session_from_row(row) for row in rows]


def _runtime_retention_int(value: int | str | None, *, default: int, minimum: int, maximum: int) -> int:
    try:
        resolved = int(value if value is not None else default)
    except (TypeError, ValueError):
        resolved = default
    return max(minimum, min(maximum, resolved))


def _runtime_retention_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (str(table),),
    ).fetchone()
    return bool(row)


def _runtime_retention_file_sizes() -> dict[str, int]:
    sqlite_path = current_sqlite_path()
    wal_path = sqlite_path.with_name(f"{sqlite_path.name}-wal")
    shm_path = sqlite_path.with_name(f"{sqlite_path.name}-shm")
    return {
        "sqlite_size_bytes": sqlite_path.stat().st_size if sqlite_path.exists() else 0,
        "wal_size_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "shm_size_bytes": shm_path.stat().st_size if shm_path.exists() else 0,
    }


def _runtime_retention_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("search_sessions", "search_events", "landing_correction_events"):
        if _runtime_retention_table_exists(conn, table):
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
        else:
            counts[table] = 0
    counts.update(_runtime_retention_file_sizes())
    return counts


def _runtime_retention_chunk(values: list[Any], *, size: int = 500) -> Iterable[list[Any]]:
    for index in range(0, len(values), max(1, int(size))):
        yield values[index : index + max(1, int(size))]


def _runtime_retention_search_ids(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[str]:
    return [str(row["search_id"]) for row in conn.execute(sql, params).fetchall() if str(row["search_id"] or "").strip()]


def _runtime_retention_count_rows_for_search_ids(
    conn: sqlite3.Connection,
    table: str,
    search_ids: list[str],
) -> int:
    if not search_ids or not _runtime_retention_table_exists(conn, table):
        return 0
    total = 0
    for chunk in _runtime_retention_chunk(search_ids):
        placeholders = ",".join("?" for _ in chunk)
        total += int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE search_id IN ({placeholders})", tuple(chunk)).fetchone()[0] or 0)
    return total


def _runtime_retention_delete_rows_for_search_ids(
    conn: sqlite3.Connection,
    table: str,
    search_ids: list[str],
    *,
    chunk_size: int = 100,
    checkpoint_wal: bool = False,
) -> int:
    if not search_ids or not _runtime_retention_table_exists(conn, table):
        return 0
    total = 0
    for index, chunk in enumerate(_runtime_retention_chunk(search_ids, size=chunk_size), start=1):
        placeholders = ",".join("?" for _ in chunk)
        cursor = conn.execute(f"DELETE FROM {table} WHERE search_id IN ({placeholders})", tuple(chunk))
        total += int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0)
        conn.commit()
        if checkpoint_wal and index % 8 == 0:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.OperationalError:
                pass
    return total


def _runtime_retention_delete_event_seqs(
    conn: sqlite3.Connection,
    seqs: list[int],
    *,
    chunk_size: int = 800,
    checkpoint_wal: bool = False,
) -> int:
    if not seqs or not _runtime_retention_table_exists(conn, "search_events"):
        return 0
    total = 0
    for index, chunk in enumerate(_runtime_retention_chunk([int(seq) for seq in seqs], size=chunk_size), start=1):
        placeholders = ",".join("?" for _ in chunk)
        cursor = conn.execute(f"DELETE FROM search_events WHERE seq IN ({placeholders})", tuple(chunk))
        total += int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0)
        conn.commit()
        if checkpoint_wal and index % 8 == 0:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.OperationalError:
                pass
    return total


def _runtime_retention_build_plan(
    conn: sqlite3.Connection,
    *,
    keep_recent_sessions: int,
    keep_failed_sessions: int,
    max_events_per_kept_session: int,
    active_session_grace_minutes: int,
) -> dict[str, Any]:
    session_columns = _table_columns(conn, "search_sessions") if _runtime_retention_table_exists(conn, "search_sessions") else set()
    if not session_columns:
        return {
            "active_session_ids": [],
            "recent_session_ids": [],
            "failed_session_ids": [],
            "preserved_session_ids": [],
            "deleted_session_ids": [],
            "capped_event_seqs": [],
            "event_cap_samples": [],
        }
    order_column = "updated_at" if "updated_at" in session_columns else "created_at" if "created_at" in session_columns else "search_id"
    all_rows = conn.execute(
        f"""
        SELECT search_id, status
        FROM search_sessions
        ORDER BY {order_column} DESC
        """
    ).fetchall()
    status_by_search_id = {str(row["search_id"]): str(row["status"] or "") for row in all_rows}
    all_session_ids = [str(row["search_id"]) for row in all_rows]
    active_status_ids = [
        search_id
        for search_id, status in status_by_search_id.items()
        if status in _RUNTIME_RETENTION_ACTIVE_STATUSES
    ]
    if "updated_at" in session_columns:
        active_ids = _runtime_retention_search_ids(
            conn,
            """
            SELECT search_id
            FROM search_sessions
            WHERE status IN ('created', 'running')
              AND julianday(updated_at) >= julianday('now') - (? / 1440.0)
            """,
            (int(active_session_grace_minutes),),
        )
    else:
        active_ids = list(active_status_ids)
    stale_active_ids = [search_id for search_id in active_status_ids if search_id not in set(active_ids)]
    recent_ids = _runtime_retention_search_ids(
        conn,
        f"""
        SELECT search_id
        FROM search_sessions
        ORDER BY {order_column} DESC
        LIMIT ?
        """,
        (int(keep_recent_sessions),),
    )
    failed_ids = _runtime_retention_search_ids(
        conn,
        f"""
        SELECT search_id
        FROM search_sessions
        WHERE status = 'failed'
        ORDER BY {order_column} DESC
        LIMIT ?
        """,
        (int(keep_failed_sessions),),
    )
    preserved_set = set(active_ids) | set(recent_ids) | set(failed_ids)
    deleted_session_ids = [search_id for search_id in all_session_ids if search_id not in preserved_set]

    capped_event_seqs: list[int] = []
    event_cap_samples: list[dict[str, Any]] = []
    if _runtime_retention_table_exists(conn, "search_events") and max_events_per_kept_session > 0:
        for search_id in sorted(preserved_set):
            if search_id in set(active_ids):
                continue
            rows = conn.execute(
                """
                SELECT seq, event_type
                FROM search_events
                WHERE search_id = ?
                ORDER BY seq DESC
                """,
                (search_id,),
            ).fetchall()
            if len(rows) <= max_events_per_kept_session:
                continue
            keep_seqs = {int(row["seq"]) for row in rows[:max_events_per_kept_session]}
            for row in rows:
                if str(row["event_type"] or "") in _RUNTIME_RETENTION_PINNED_EVENT_TYPES:
                    keep_seqs.add(int(row["seq"]))
            delete_seqs = [int(row["seq"]) for row in rows if int(row["seq"]) not in keep_seqs]
            capped_event_seqs.extend(delete_seqs)
            if len(event_cap_samples) < 8:
                event_cap_samples.append(
                    {
                        "search_id": search_id,
                        "status": status_by_search_id.get(search_id),
                        "event_count_before": len(rows),
                        "event_delete_count": len(delete_seqs),
                        "event_count_after": len(rows) - len(delete_seqs),
                    }
                )

    return {
        "active_session_ids": active_ids,
        "stale_active_session_ids": stale_active_ids,
        "recent_session_ids": recent_ids,
        "failed_session_ids": failed_ids,
        "preserved_session_ids": sorted(preserved_set),
        "deleted_session_ids": deleted_session_ids,
        "capped_event_seqs": capped_event_seqs,
        "event_cap_samples": event_cap_samples,
    }


def _runtime_retention_report(
    *,
    apply: bool,
    keep_recent_sessions: int = 30,
    keep_failed_sessions: int = 10,
    max_events_per_kept_session: int = 220,
    active_session_grace_minutes: int = 360,
    checkpoint_wal: bool = False,
    vacuum: bool = False,
    busy_timeout_ms: int = 2000,
) -> dict[str, Any]:
    keep_recent = _runtime_retention_int(keep_recent_sessions, default=30, minimum=1, maximum=1000)
    keep_failed = _runtime_retention_int(keep_failed_sessions, default=10, minimum=0, maximum=200)
    event_cap = _runtime_retention_int(max_events_per_kept_session, default=220, minimum=1, maximum=5000)
    active_grace = _runtime_retention_int(active_session_grace_minutes, default=360, minimum=1, maximum=10080)
    timeout_ms = _runtime_retention_int(busy_timeout_ms, default=2000, minimum=50, maximum=60000)
    with connect(timeout_seconds=timeout_ms / 1000.0, busy_timeout_ms=timeout_ms) as conn:
        before = _runtime_retention_counts(conn)
        plan = _runtime_retention_build_plan(
            conn,
            keep_recent_sessions=keep_recent,
            keep_failed_sessions=keep_failed,
            max_events_per_kept_session=event_cap,
            active_session_grace_minutes=active_grace,
        )
        deleted_session_ids = list(plan.get("deleted_session_ids") or [])
        capped_event_seqs = [int(seq) for seq in list(plan.get("capped_event_seqs") or [])]
        cascade_event_count = _runtime_retention_count_rows_for_search_ids(conn, "search_events", deleted_session_ids)
        cascade_landing_count = _runtime_retention_count_rows_for_search_ids(conn, "landing_correction_events", deleted_session_ids)
        applied = {
            "search_sessions_deleted": 0,
            "search_events_deleted_by_session_cascade": 0,
            "search_events_deleted_by_event_cap": 0,
            "landing_correction_events_deleted_by_session_cascade": 0,
            "wal_checkpoint": None,
            "vacuum": None,
        }
        if apply:
            applied["search_events_deleted_by_event_cap"] = _runtime_retention_delete_event_seqs(
                conn,
                capped_event_seqs,
                checkpoint_wal=checkpoint_wal,
            )
            applied["search_events_deleted_by_session_cascade"] = _runtime_retention_delete_rows_for_search_ids(
                conn,
                "search_events",
                deleted_session_ids,
                checkpoint_wal=checkpoint_wal,
            )
            applied["landing_correction_events_deleted_by_session_cascade"] = _runtime_retention_delete_rows_for_search_ids(
                conn,
                "landing_correction_events",
                deleted_session_ids,
                checkpoint_wal=checkpoint_wal,
            )
            applied["search_sessions_deleted"] = _runtime_retention_delete_rows_for_search_ids(
                conn,
                "search_sessions",
                deleted_session_ids,
                checkpoint_wal=checkpoint_wal,
            )
            conn.commit()
            if checkpoint_wal:
                try:
                    checkpoint_row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    applied["wal_checkpoint"] = list(checkpoint_row) if checkpoint_row is not None else []
                except sqlite3.OperationalError as exc:
                    applied["wal_checkpoint"] = {"status": "skipped", "reason": str(exc)}
            if vacuum:
                try:
                    conn.execute("VACUUM")
                    applied["vacuum"] = {"status": "completed"}
                except sqlite3.OperationalError as exc:
                    applied["vacuum"] = {"status": "skipped", "reason": str(exc)}
        after = _runtime_retention_counts(conn)

    return {
        "schema_version": RUNTIME_RETENTION_REPORT_SCHEMA_VERSION,
        "generated_at": utc_timestamp(),
        "mode": "apply" if apply else "preview",
        "applied": bool(apply),
        "policy": {
            "keep_recent_sessions": keep_recent,
            "keep_failed_sessions": keep_failed,
            "max_events_per_kept_session": event_cap,
            "active_session_grace_minutes": active_grace,
            "checkpoint_wal": bool(checkpoint_wal),
            "vacuum": bool(vacuum),
            "busy_timeout_ms": timeout_ms,
            "apply_batching": {
                "search_id_chunk_size": 100,
                "event_seq_chunk_size": 800,
                "checkpoint_every_chunks": 8 if checkpoint_wal else 0,
            },
        },
        "before": before,
        "after": after,
        "planned": {
            "preserved_session_count": len(list(plan.get("preserved_session_ids") or [])),
            "active_session_count": len(list(plan.get("active_session_ids") or [])),
            "stale_active_session_count": len(list(plan.get("stale_active_session_ids") or [])),
            "recent_session_count": len(list(plan.get("recent_session_ids") or [])),
            "failed_session_count": len(list(plan.get("failed_session_ids") or [])),
            "deleted_session_count": len(deleted_session_ids),
            "deleted_search_event_count": int(cascade_event_count) + len(capped_event_seqs),
            "deleted_search_events_by_session_cascade": int(cascade_event_count),
            "deleted_search_events_by_event_cap": len(capped_event_seqs),
            "deleted_landing_correction_event_count": int(cascade_landing_count),
            "preserved_session_sample": list(plan.get("preserved_session_ids") or [])[:12],
            "stale_active_session_sample": list(plan.get("stale_active_session_ids") or [])[:12],
            "deleted_session_sample": deleted_session_ids[:12],
            "event_cap_samples": list(plan.get("event_cap_samples") or []),
        },
        "applied_counts": applied,
        "safety_contract": {
            "schema_version": "agvm.runtime_retention_safety.v1",
            "knowledge_graph_tables_mutated": False,
            "mutated_tables": ["search_sessions", "search_events", "landing_correction_events"] if apply else [],
            "active_sessions_preserved": True,
            "recent_sessions_preserved": True,
            "failed_session_tail_preserved": keep_failed > 0,
            "retention_scope": "runtime_search_audit_only",
            "raw_document_nodes_preserved": True,
            "semantic_graph_preserved": True,
        },
    }


def preview_runtime_retention_policy(
    *,
    keep_recent_sessions: int = 30,
    keep_failed_sessions: int = 10,
    max_events_per_kept_session: int = 220,
    active_session_grace_minutes: int = 360,
    busy_timeout_ms: int = 2000,
) -> dict[str, Any]:
    return _runtime_retention_report(
        apply=False,
        keep_recent_sessions=keep_recent_sessions,
        keep_failed_sessions=keep_failed_sessions,
        max_events_per_kept_session=max_events_per_kept_session,
        active_session_grace_minutes=active_session_grace_minutes,
        busy_timeout_ms=busy_timeout_ms,
    )


def apply_runtime_retention_policy(
    *,
    keep_recent_sessions: int = 30,
    keep_failed_sessions: int = 10,
    max_events_per_kept_session: int = 220,
    active_session_grace_minutes: int = 360,
    checkpoint_wal: bool = True,
    vacuum: bool = False,
    busy_timeout_ms: int = 2000,
) -> dict[str, Any]:
    return _runtime_retention_report(
        apply=True,
        keep_recent_sessions=keep_recent_sessions,
        keep_failed_sessions=keep_failed_sessions,
        max_events_per_kept_session=max_events_per_kept_session,
        active_session_grace_minutes=active_session_grace_minutes,
        checkpoint_wal=checkpoint_wal,
        vacuum=vacuum,
        busy_timeout_ms=busy_timeout_ms,
    )


def fetch_active_search_session_by_thread(thread_id: str, *, exclude_search_id: str | None = None) -> dict[str, Any] | None:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return None
    sql = """
        SELECT search_id
        FROM search_sessions
        WHERE thread_id = ? AND status = 'running'
    """
    params: list[Any] = [normalized_thread_id]
    if exclude_search_id:
        sql += " AND search_id != ?"
        params.append(str(exclude_search_id))
    sql += " ORDER BY updated_at DESC LIMIT 1"
    try:
        with connect(timeout_seconds=0.08, busy_timeout_ms=80) as conn:
            row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            return None
        raise
    if not row:
        return None
    search_id = str(row["search_id"] or "").strip()
    if not search_id:
        return None
    return {"search_id": search_id, "thread_id": normalized_thread_id, "status": "running"}


def save_warm_thread_state(
    thread_id: str,
    *,
    last_search_id: str,
    topic_signature: dict[str, Any],
    warm_packet: dict[str, Any],
    continuity_state: str,
) -> None:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return
    timestamp = utc_timestamp()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO warm_thread_state (
                thread_id, last_search_id, topic_signature_json, warm_packet_json, continuity_state, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                last_search_id=excluded.last_search_id,
                topic_signature_json=excluded.topic_signature_json,
                warm_packet_json=excluded.warm_packet_json,
                continuity_state=excluded.continuity_state,
                updated_at=excluded.updated_at
            """,
            (
                normalized_thread_id,
                str(last_search_id),
                _json_dump(topic_signature),
                _json_dump(warm_packet),
                str(continuity_state or "low_continuity"),
                timestamp,
            ),
        )
        conn.commit()


def fetch_warm_thread_state(thread_id: str) -> dict[str, Any] | None:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT thread_id, last_search_id, topic_signature_json, warm_packet_json, continuity_state, updated_at
            FROM warm_thread_state
            WHERE thread_id = ?
            """,
            (normalized_thread_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "thread_id": str(row["thread_id"]),
        "last_search_id": str(row["last_search_id"]),
        "topic_signature": _json_load(row["topic_signature_json"], {}),
        "warm_packet": _json_load(row["warm_packet_json"], {}),
        "continuity_state": str(row["continuity_state"] or "low_continuity"),
        "updated_at": str(row["updated_at"]),
    }


def clear_warm_thread_state(thread_id: str) -> None:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return
    with connect() as conn:
        conn.execute("DELETE FROM warm_thread_state WHERE thread_id = ?", (normalized_thread_id,))
        conn.commit()


def save_hot_working_memory_state(
    brain_id: str,
    thread_id: str,
    *,
    packet: dict[str, Any],
    contract: dict[str, Any],
) -> None:
    normalized_brain_id = str(brain_id or "").strip() or "default"
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return
    timestamp = utc_timestamp()
    try:
        with connect() as conn:
            existing = conn.execute(
                """
                SELECT created_at
                FROM hot_working_memory_state
                WHERE brain_id = ? AND thread_id = ?
                """,
                (normalized_brain_id, normalized_thread_id),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO hot_working_memory_state (
                    brain_id, thread_id, packet_json, contract_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(brain_id, thread_id) DO UPDATE SET
                    packet_json=excluded.packet_json,
                    contract_json=excluded.contract_json,
                    updated_at=excluded.updated_at
                """,
                (
                    normalized_brain_id,
                    normalized_thread_id,
                    _json_dump(packet),
                    _json_dump(contract),
                    str(existing["created_at"]) if existing else timestamp,
                    timestamp,
                ),
            )
            conn.commit()
    except sqlite3.OperationalError as exc:
        if "hot_working_memory_state" not in str(exc) or "no such table" not in str(exc):
            raise
        bootstrap_runtime_store()
        save_hot_working_memory_state(normalized_brain_id, normalized_thread_id, packet=packet, contract=contract)


def fetch_hot_working_memory_state(brain_id: str, thread_id: str) -> dict[str, Any] | None:
    normalized_brain_id = str(brain_id or "").strip() or "default"
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return None
    try:
        with connect() as conn:
            row = conn.execute(
                """
                SELECT brain_id, thread_id, packet_json, contract_json, created_at, updated_at
                FROM hot_working_memory_state
                WHERE brain_id = ? AND thread_id = ?
                """,
                (normalized_brain_id, normalized_thread_id),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "hot_working_memory_state" not in str(exc) or "no such table" not in str(exc):
            raise
        bootstrap_runtime_store()
        return fetch_hot_working_memory_state(normalized_brain_id, normalized_thread_id)
    if not row:
        return None
    return {
        "brain_id": str(row["brain_id"]),
        "thread_id": str(row["thread_id"]),
        "packet": _json_load(row["packet_json"], {}),
        "contract": _json_load(row["contract_json"], {}),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def clear_hot_working_memory_state(brain_id: str, thread_id: str) -> None:
    normalized_brain_id = str(brain_id or "").strip() or "default"
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return
    try:
        with connect() as conn:
            conn.execute(
                "DELETE FROM hot_working_memory_state WHERE brain_id = ? AND thread_id = ?",
                (normalized_brain_id, normalized_thread_id),
            )
            conn.commit()
    except sqlite3.OperationalError as exc:
        if "hot_working_memory_state" not in str(exc) or "no such table" not in str(exc):
            raise
        bootstrap_runtime_store()


def fetch_correction_history(*, search_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = """
        SELECT correction_id, search_id, query_text, returned_answer, correction_text,
               correction_mode, used_evidence_node_ids_json, target_node_ids_json,
               action_summary_json, created_at
        FROM correction_history
    """
    params: list[Any] = []
    if search_id:
        sql += " WHERE search_id = ?"
        params.append(str(search_id))
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "correction_id": str(row["correction_id"]),
            "search_id": str(row["search_id"]) if row["search_id"] else None,
            "query_text": str(row["query_text"]),
            "returned_answer": str(row["returned_answer"]),
            "correction_text": str(row["correction_text"]),
            "correction_mode": str(row["correction_mode"]),
            "used_evidence_node_ids": list(_json_load(row["used_evidence_node_ids_json"], [])),
            "target_node_ids": list(_json_load(row["target_node_ids_json"], [])),
            "action_summary": dict(_json_load(row["action_summary_json"], {})),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def store_correction_history(
    *,
    correction_id: str,
    search_id: str | None,
    query_text: str,
    returned_answer: str,
    correction_text: str,
    correction_mode: str,
    used_evidence_node_ids: list[str],
    target_node_ids: list[str],
    action_summary: dict[str, Any],
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO correction_history (
                correction_id, search_id, query_text, returned_answer, correction_text,
                correction_mode, used_evidence_node_ids_json, target_node_ids_json,
                action_summary_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(correction_id),
                str(search_id) if search_id else None,
                str(query_text),
                str(returned_answer),
                str(correction_text),
                str(correction_mode),
                _json_dump(list(used_evidence_node_ids or [])),
                _json_dump(list(target_node_ids or [])),
                _json_dump(action_summary),
                utc_timestamp(),
            ),
        )
        conn.commit()


def fetch_search_trace(search_id: str) -> dict[str, Any] | None:
    session = fetch_search_session(search_id)
    if not session:
        return None
    result = dict(session.get("result") or {})
    plan = dict(session.get("plan") or {})
    trace_timing = {
        **dict(result.get("timing") or {}),
        "plan_ms": dict(plan.get("planner_runtime") or {}).get("plan_ms", dict(result.get("timing") or {}).get("plan_ms")),
    }
    probes = list(plan.get("probes") or [])
    branches = list(result.get("branches") or plan.get("branches") or [])
    landing_metadata = [
        dict(record)
        for record in list(result.get("landing_metadata") or plan.get("landing_metadata") or [])
        if isinstance(record, dict)
    ]
    if not landing_metadata:
        branch_by_probe_id: dict[str, dict[str, Any]] = {}
        for branch in branches:
            for probe_id in list(branch.get("probe_ids") or []):
                normalized_probe_id = str(probe_id or "").strip()
                if normalized_probe_id and normalized_probe_id not in branch_by_probe_id:
                    branch_by_probe_id[normalized_probe_id] = dict(branch)
        landing_metadata = []
        for probe in probes:
            probe_id = str(probe.get("probe_id") or "")
            branch = branch_by_probe_id.get(probe_id) or {}
            active_destination = dict(branch.get("active_destination") or {}) if isinstance(branch.get("active_destination"), dict) else {}
            destination_queue = [dict(item) for item in list(branch.get("destination_queue") or probe.get("destination_queue") or []) if isinstance(item, dict)]
            landing_metadata.append(
                {
                    "landing_id": str(branch.get("branch_id") or probe_id),
                    "probe_id": probe_id,
                    "branch_id": str(branch.get("branch_id") or ""),
                    "family_branch_id": str(branch.get("family_branch_id") or probe.get("family_branch_id") or ""),
                    "worker_id": str(branch.get("worker_id") or branch.get("branch_id") or ""),
                    "planner_family": str(branch.get("planner_family") or probe.get("planner_family") or ""),
                    "origin_families": list(branch.get("origin_families") or probe.get("origin_families") or []),
                    "dual_origin": bool(branch.get("dual_origin") or probe.get("dual_origin")),
                    "label": str(probe.get("label") or ""),
                    "query_text": str(probe.get("query_text") or ""),
                    "goal": str(probe.get("goal") or branch.get("goal") or ""),
                    "query_class": str(probe.get("query_class") or ""),
                    "expected_guide_area": probe.get("expected_guide_area"),
                    "expected_memory_type": probe.get("expected_memory_type"),
                    "expected_radial_band": probe.get("radial_expectation"),
                    "search_radius": probe.get("search_radius") or branch.get("search_radius"),
                    "target_bucket_keys": list(probe.get("target_bucket_keys") or active_destination.get("target_bucket_keys") or []),
                    "crowding_penalty": probe.get("crowding_penalty"),
                    "landing_position": dict(probe.get("landing_position") or {}),
                    "active_destination": active_destination or None,
                    "destination_queue": destination_queue[:6],
                    "status": branch.get("status"),
                    "stop_reason": branch.get("stop_reason"),
                    "route_state": branch.get("route_state") or branch.get("lifecycle_stage"),
                }
            )
    worker_stop_reasons = {
        str(branch.get("branch_id") or ""): str(branch.get("stop_reason") or branch.get("status") or "")
        for branch in branches
        if str(branch.get("branch_id") or "")
    }
    return {
        "search_id": str(search_id),
        "session": session,
        "events": fetch_search_events(search_id, after_seq=0, limit=1000),
        "corrections": fetch_correction_history(search_id=search_id, limit=100),
        "result": result,
        "timing": trace_timing,
        "planner_metadata": dict(plan.get("planner_runtime") or {}),
        "landing_metadata": landing_metadata,
        "context_waves": [dict(wave) for wave in list(result.get("context_waves") or []) if isinstance(wave, dict)],
        "worker_stop_reasons": worker_stop_reasons,
        "follow_up_candidates": list(result.get("follow_up_candidates") or []),
        "blackboard": dict(result.get("shared_evidence") or {}),
    }


def rebuild_region_summaries() -> list[dict[str, Any]]:
    atlas_payload = fetch_atlas()
    with connect() as conn:
        event_rows = conn.execute(
            """
            SELECT search_id, event_type, payload_json
            FROM search_events
            WHERE event_type IN ('step_complete', 'search_stopped')
            ORDER BY seq ASC
            """
        ).fetchall()
        session_rows = conn.execute(
            """
            SELECT search_id, query_text, request_json, plan_json, result_json
            FROM search_sessions
            """
        ).fetchall()
        conn.execute("DELETE FROM region_summaries")
        summaries: list[dict[str, Any]] = []
        session_meta: dict[str, dict[str, Any]] = {}
        for row in session_rows:
            request_payload = _json_load(row["request_json"], {})
            plan_payload = _json_load(row["plan_json"], {})
            result_payload = _json_load(row["result_json"], {})
            probes = list(plan_payload.get("probes") or [])
            query_class = next(
                (
                    str(probe.get("query_class") or "").strip()
                    for probe in probes
                    if str(probe.get("query_class") or "").strip()
                ),
                "",
            ) or str(plan_payload.get("query_class") or "").strip()
            retrieval_mode = (
                str(result_payload.get("retrieval_mode") or "").strip()
                or str(plan_payload.get("retrieval_mode") or "").strip()
                or str(request_payload.get("retrieval_mode") or "").strip()
            )
            session_meta[str(row["search_id"])] = {
                "query_text": str(row["query_text"] or ""),
                "query_class": query_class or "unknown",
                "retrieval_mode": retrieval_mode or "balanced",
            }
        region_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "trace_hits": 0,
                "success_hits": 0,
                "failure_hits": 0,
                "bridge_hits": 0,
                "stop_reasons": defaultdict(int),
                "question_classes": defaultdict(int),
                "retrieval_modes": defaultdict(int),
                "query_examples": [],
                "candidate_node_hits": defaultdict(int),
            }
        )
        search_regions: dict[str, set[str]] = defaultdict(set)
        for row in event_rows:
            search_id = str(row["search_id"] or "")
            meta = dict(session_meta.get(search_id) or {})
            event_type = str(row["event_type"] or "")
            payload = _json_load(row["payload_json"], {})
            if event_type == "step_complete":
                region_id = str(payload.get("bucket_key") or "").strip()
                if not region_id:
                    continue
                stats = region_stats[region_id]
                search_regions[search_id].add(region_id)
                stats["trace_hits"] += 1
                matches = list(payload.get("matches") or [])
                if matches:
                    stats["success_hits"] += 1
                else:
                    stats["failure_hits"] += 1
                candidate_sources = dict(payload.get("candidate_sources") or {})
                if list(payload.get("followed_highway_targets") or []) or any(
                    "highway" in str(source or "")
                    for sources in candidate_sources.values()
                    for source in list(sources or [])
                ):
                    stats["bridge_hits"] += 1
                stats["question_classes"][str(meta.get("query_class") or "unknown")] += 1
                stats["retrieval_modes"][str(meta.get("retrieval_mode") or "balanced")] += 1
                query_text = str(meta.get("query_text") or "").strip()
                if query_text and query_text not in stats["query_examples"] and len(stats["query_examples"]) < 4:
                    stats["query_examples"].append(query_text)
                for candidate_id in list(payload.get("candidate_ids") or []):
                    node_id = str(candidate_id or "").strip()
                    if node_id:
                        stats["candidate_node_hits"][node_id] += 1
            elif event_type == "search_stopped":
                stop_reason = str(payload.get("stop_reason") or "").strip()
                if not stop_reason:
                    continue
                for region_id in sorted(search_regions.get(search_id) or set()):
                    region_stats[region_id]["stop_reasons"][stop_reason] += 1
        for bucket in list(atlas_payload.get("buckets") or []):
            region_id = str(bucket.get("bucket_key") or "")
            nearby_nodes = fetch_nav_by_coarse_bucket_keys([region_id], limit=18)
            stats = dict(region_stats.get(region_id) or {})
            question_classes = dict(stats.get("question_classes") or {})
            retrieval_modes = dict(stats.get("retrieval_modes") or {})
            trace_hits = int(stats.get("trace_hits") or 0)
            success_hits = int(stats.get("success_hits") or 0)
            failure_hits = int(stats.get("failure_hits") or 0)
            bridge_hits = int(stats.get("bridge_hits") or 0)
            common_question_classes = [
                key
                for key, _value in sorted(question_classes.items(), key=lambda item: (-int(item[1]), str(item[0])))[:4]
                if str(key or "")
            ]
            common_retrieval_modes = [
                key
                for key, _value in sorted(retrieval_modes.items(), key=lambda item: (-int(item[1]), str(item[0])))[:3]
                if str(key or "")
            ]
            candidate_node_hits = dict(stats.get("candidate_node_hits") or {})
            dominant_concepts = list(
                dict.fromkeys(
                    str(node.get("summary") or "").strip()
                    for node in nearby_nodes[:6]
                    if str(node.get("summary") or "").strip()
                )
            )
            dominant_memory_types = list(
                dict.fromkeys(str(node.get("memory_type") or "") for node in nearby_nodes if str(node.get("memory_type") or ""))
            )[:6]
            identity_hints = list(
                dict.fromkeys(
                    str(node.get("summary") or "")
                    for node in nearby_nodes
                    if str((node.get("provenance") or {}).get("guide_conceptual_area") or "") == "Identity"
                )
            )[:4]
            project_hints = list(
                dict.fromkeys(
                    str(node.get("summary") or "")
                    for node in nearby_nodes
                    if str((node.get("provenance") or {}).get("guide_conceptual_area") or "") == "Projects"
                )
            )[:4]
            place_hints = list(
                dict.fromkeys(
                    str(node.get("summary") or "")
                    for node in nearby_nodes
                    if str(node.get("memory_type") or "") in {"place", "relational", "identity"}
                )
            )[:4]
            node_count = int(bucket.get("node_count") or 0)
            if node_count >= 40:
                crowding_severity = "high"
            elif node_count >= 20:
                crowding_severity = "medium"
            else:
                crowding_severity = "low"
            next_useful_entry_points = [
                {
                    "node_id": str(node.get("id") or ""),
                    "summary": str(node.get("summary") or ""),
                }
                for node in sorted(
                    nearby_nodes,
                    key=lambda node: (
                        -int(candidate_node_hits.get(str(node.get("id") or ""), 0)),
                        -float(node.get("memory_confidence") or 0.0),
                    ),
                )[:4]
                if str(node.get("id") or "")
            ]
            summary = {
                "region_id": region_id,
                "centroid": dict(bucket.get("centroid") or {}),
                "node_count": node_count,
                "dominant_concepts": dominant_concepts,
                "dominant_memory_types": dominant_memory_types,
                "identity_hints": identity_hints,
                "project_hints": project_hints,
                "place_hints": place_hints,
                "common_outbound_highways": list(bucket.get("outgoing_highway_gateways") or [])[:6],
                "common_question_classes": common_question_classes,
                "common_retrieval_modes": common_retrieval_modes,
                "query_examples": list(stats.get("query_examples") or []),
                "next_useful_entry_points": next_useful_entry_points,
                "density": {
                    "node_count": node_count,
                    "document_anchor_count": int(bucket.get("document_anchor_count") or 0),
                },
                "crowding_severity": crowding_severity,
                "instability_flags": ["crowded_region"] if node_count >= 20 else [],
                "retrieval_usefulness": {
                    "trace_hits": trace_hits,
                    "success_ratio": round(success_hits / max(1, trace_hits), 4),
                    "fail_ratio": round(failure_hits / max(1, trace_hits), 4),
                    "bridge_usefulness": round(bridge_hits / max(1, trace_hits), 4),
                    "common_question_classes": common_question_classes,
                    "common_retrieval_modes": common_retrieval_modes,
                    "stop_reasons": dict(stats.get("stop_reasons") or {}),
                },
                "updated_at": utc_timestamp(),
            }
            conn.execute(
                """
                INSERT INTO region_summaries (region_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (region_id, _json_dump(summary), str(summary["updated_at"])),
            )
            summaries.append(summary)
        conn.commit()
    return summaries


def fetch_region_summary(region_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload_json, updated_at FROM region_summaries WHERE region_id = ?",
            (str(region_id),),
        ).fetchone()
    if row:
        payload = _json_load(row["payload_json"], {})
        payload.setdefault("updated_at", str(row["updated_at"]))
        return payload
    summaries = rebuild_region_summaries()
    return next((item for item in summaries if str(item.get("region_id")) == str(region_id)), None)


def store_maintenance_run(
    *,
    maintenance_id: str,
    mode: str,
    applied: bool,
    preview_only: bool,
    focus_node_id: str | None,
    report: dict[str, Any],
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO maintenance_runs (maintenance_id, mode, applied, preview_only, focus_node_id, report_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(maintenance_id),
                str(mode),
                1 if bool(applied) else 0,
                1 if bool(preview_only) else 0,
                str(focus_node_id) if focus_node_id else None,
                _json_dump(report),
                utc_timestamp(),
            ),
        )
        conn.commit()


def fetch_recent_maintenance_runs(
    *,
    limit: int = 10,
    applied_only: bool | None = None,
    include_report: bool = True,
) -> list[dict[str, Any]]:
    bootstrap_runtime_store()
    report_select = "report_json" if include_report else "NULL AS report_json"
    with connect() as conn:
        if applied_only is None:
            rows = conn.execute(
                f"""
                SELECT maintenance_id, mode, applied, preview_only, focus_node_id, {report_select}, created_at
                FROM maintenance_runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT maintenance_id, mode, applied, preview_only, focus_node_id, {report_select}, created_at
                FROM maintenance_runs
                WHERE applied = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (1 if applied_only else 0, int(limit)),
            ).fetchall()
    return [
        {
            "maintenance_id": str(row["maintenance_id"]),
            "mode": str(row["mode"]),
            "applied": bool(row["applied"]),
            "preview_only": bool(row["preview_only"]),
            "focus_node_id": str(row["focus_node_id"]) if row["focus_node_id"] is not None else None,
            "report": _json_load(row["report_json"], {}) if row["report_json"] else {},
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def save_heuristic_calibration_payload(scope_key: str, payload: dict[str, Any]) -> None:
    normalized_scope_key = str(scope_key or "global").strip() or "global"
    timestamp = utc_timestamp()
    payload_with_meta = {**dict(payload or {}), "scope_key": normalized_scope_key, "updated_at": timestamp}
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO heuristic_calibration_store (scope_key, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(scope_key) DO UPDATE SET
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (normalized_scope_key, _json_dump(payload_with_meta), timestamp),
        )
        conn.commit()


def store_heuristic_calibration_event(
    *,
    event_id: str,
    event_kind: str,
    source_type: str,
    source_ref: str | None,
    scope_key: str,
    evidence: dict[str, Any],
    delta: dict[str, Any],
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO heuristic_calibration_events (
                event_id, event_kind, source_type, source_ref, scope_key, evidence_json, delta_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event_id),
                str(event_kind or "unknown"),
                str(source_type or "unknown"),
                str(source_ref) if source_ref else None,
                str(scope_key or "global"),
                _json_dump(evidence or {}),
                _json_dump(delta or {}),
                utc_timestamp(),
            ),
        )
        conn.commit()


def fetch_recent_heuristic_calibration_events(*, limit: int = 40) -> list[dict[str, Any]]:
    bootstrap_runtime_store()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT event_id, event_kind, source_type, source_ref, scope_key, evidence_json, delta_json, created_at
            FROM heuristic_calibration_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [
        {
            "event_id": str(row["event_id"]),
            "event_kind": str(row["event_kind"]),
            "source_type": str(row["source_type"]),
            "source_ref": str(row["source_ref"]) if row["source_ref"] is not None else None,
            "scope_key": str(row["scope_key"]),
            "evidence": _json_load(row["evidence_json"], {}),
            "delta": _json_load(row["delta_json"], {}),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _landing_correction_event_id(search_id: str, index: int, event: dict[str, Any]) -> str:
    parts = [
        str(search_id or ""),
        str(event.get("path_id") or ""),
        str(event.get("landing_id") or ""),
        str(event.get("ai_spatial_path_id") or ""),
        str(index),
    ]
    return f"lce_{uuid.uuid5(uuid.NAMESPACE_URL, '::'.join(parts)).hex}"


def _event_json_list(event: dict[str, Any], key: str) -> list[str]:
    return [str(item) for item in list(event.get(key) or []) if str(item).strip()]


def store_landing_correction_events(
    *,
    search_id: str,
    brain_id: str | None,
    query_text: str | None,
    retrieval_mode: str | None,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    bootstrap_runtime_store()
    timestamp = utc_timestamp()
    stored_rows: list[dict[str, Any]] = []
    normalized_search_id = str(search_id or "").strip()
    if not normalized_search_id:
        return []
    with connect() as conn:
        for index, raw_event in enumerate(list(events or []), start=1):
            event = dict(raw_event or {})
            if not event:
                continue
            promoted_hot_node_ids = _event_json_list(event, "promoted_hot_node_ids")
            cold_reservoir_node_ids = _event_json_list(event, "cold_reservoir_node_ids")
            excluded_node_ids = _event_json_list(event, "excluded_node_ids")
            traversed_node_ids = _event_json_list(event, "traversed_node_ids")
            destination_reached = bool(event.get("destination_reached"))
            changed_context_package = bool(event.get("changed_context_package"))
            successful = bool(changed_context_package or promoted_hot_node_ids or destination_reached)
            persisted_event = {
                **event,
                "search_id": normalized_search_id,
                "brain_id": brain_id,
                "query_text": query_text,
                "retrieval_mode": retrieval_mode,
                "persistence_state": "persisted_review_only_h5",
                "successful": successful,
            }
            event_id = str(event.get("event_id") or _landing_correction_event_id(normalized_search_id, index, persisted_event))
            persisted_event["event_id"] = event_id
            conn.execute(
                """
                INSERT INTO landing_correction_events (
                    event_id, search_id, brain_id, query_text, retrieval_mode, query_class, goal,
                    answer_field, guide_area, memory_type, radial_band, brain_revision, scope_key,
                    path_id, landing_id, ai_spatial_path_id, ai_landing_region_ref,
                    ai_landing_coordinate_json, snapped_region_ref, snapped_coordinate_json,
                    bucket_key, snap_delta, snap_status, snap_source, backend_changed_coordinate,
                    heuristic_provisional, destination_reached, changed_context_package, successful,
                    traversed_node_ids_json, promoted_hot_node_ids_json, cold_reservoir_node_ids_json,
                    excluded_node_ids_json, traversed_edge_count, persistence_state, raw_event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    brain_id=excluded.brain_id,
                    query_text=excluded.query_text,
                    retrieval_mode=excluded.retrieval_mode,
                    query_class=excluded.query_class,
                    goal=excluded.goal,
                    answer_field=excluded.answer_field,
                    guide_area=excluded.guide_area,
                    memory_type=excluded.memory_type,
                    radial_band=excluded.radial_band,
                    brain_revision=excluded.brain_revision,
                    scope_key=excluded.scope_key,
                    raw_event_json=excluded.raw_event_json,
                    created_at=excluded.created_at
                """,
                (
                    event_id,
                    normalized_search_id,
                    str(brain_id) if brain_id else None,
                    str(query_text or ""),
                    str(retrieval_mode or ""),
                    str(event.get("query_class") or ""),
                    str(event.get("goal") or ""),
                    str(event.get("answer_field") or ""),
                    str(event.get("guide_area") or ""),
                    str(event.get("memory_type") or ""),
                    str(event.get("radial_band") or ""),
                    str(event.get("brain_revision") or ""),
                    str(event.get("scope_key") or ""),
                    str(event.get("path_id") or ""),
                    str(event.get("landing_id") or ""),
                    str(event.get("ai_spatial_path_id") or ""),
                    str(event.get("ai_landing_region_ref") or ""),
                    _json_dump(event.get("ai_landing_coordinate") or {}),
                    str(event.get("snapped_region_ref") or ""),
                    _json_dump(event.get("snapped_coordinate") or {}),
                    str(event.get("bucket_key") or ""),
                    _safe_float(event.get("snap_delta") or 0.0),
                    str(event.get("snap_status") or ""),
                    str(event.get("snap_source") or ""),
                    1 if bool(event.get("backend_changed_coordinate")) else 0,
                    1 if bool(event.get("heuristic_provisional")) else 0,
                    1 if destination_reached else 0,
                    1 if changed_context_package else 0,
                    1 if successful else 0,
                    _json_dump(traversed_node_ids),
                    _json_dump(promoted_hot_node_ids),
                    _json_dump(cold_reservoir_node_ids),
                    _json_dump(excluded_node_ids),
                    int(event.get("traversed_edge_count") or 0),
                    "persisted_review_only_h5",
                    _json_dump(persisted_event),
                    timestamp,
                ),
            )
            stored_rows.append(persisted_event)
        conn.commit()
    return stored_rows


def fetch_recent_landing_correction_events(
    *,
    limit: int = 80,
    brain_id: str | None = None,
    scope_key: str | None = None,
) -> list[dict[str, Any]]:
    bootstrap_runtime_store()
    clauses: list[str] = []
    params: list[Any] = []
    if brain_id:
        clauses.append("brain_id = ?")
        params.append(str(brain_id))
    if scope_key:
        clauses.append("scope_key = ?")
        params.append(str(scope_key))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT event_id, search_id, brain_id, query_text, retrieval_mode, query_class, goal,
                   answer_field, guide_area, memory_type, radial_band, brain_revision, scope_key,
                   path_id, landing_id, ai_spatial_path_id, ai_landing_region_ref,
                   ai_landing_coordinate_json, snapped_region_ref, snapped_coordinate_json,
                   bucket_key, snap_delta, snap_status, snap_source, backend_changed_coordinate,
                   heuristic_provisional, destination_reached, changed_context_package, successful,
                   traversed_node_ids_json, promoted_hot_node_ids_json, cold_reservoir_node_ids_json,
                   excluded_node_ids_json, traversed_edge_count, persistence_state, raw_event_json, created_at
            FROM landing_correction_events
            {where}
            ORDER BY created_at DESC, event_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        raw_event = _json_load(row["raw_event_json"], {})
        events.append(
            {
                **(raw_event if isinstance(raw_event, dict) else {}),
                "event_id": str(row["event_id"]),
                "search_id": str(row["search_id"]),
                "brain_id": str(row["brain_id"] or ""),
                "query_text": str(row["query_text"] or ""),
                "retrieval_mode": str(row["retrieval_mode"] or ""),
                "query_class": str(row["query_class"] or ""),
                "goal": str(row["goal"] or ""),
                "answer_field": str(row["answer_field"] or ""),
                "guide_area": str(row["guide_area"] or ""),
                "memory_type": str(row["memory_type"] or ""),
                "radial_band": str(row["radial_band"] or ""),
                "brain_revision": str(row["brain_revision"] or ""),
                "scope_key": str(row["scope_key"] or ""),
                "path_id": str(row["path_id"] or ""),
                "landing_id": str(row["landing_id"] or ""),
                "ai_spatial_path_id": str(row["ai_spatial_path_id"] or ""),
                "ai_landing_region_ref": str(row["ai_landing_region_ref"] or ""),
                "ai_landing_coordinate": _json_load(row["ai_landing_coordinate_json"], {}),
                "snapped_region_ref": str(row["snapped_region_ref"] or ""),
                "snapped_coordinate": _json_load(row["snapped_coordinate_json"], {}),
                "bucket_key": str(row["bucket_key"] or ""),
                "snap_delta": _safe_float(row["snap_delta"] or 0.0),
                "snap_status": str(row["snap_status"] or ""),
                "snap_source": str(row["snap_source"] or ""),
                "backend_changed_coordinate": bool(row["backend_changed_coordinate"]),
                "heuristic_provisional": bool(row["heuristic_provisional"]),
                "destination_reached": bool(row["destination_reached"]),
                "changed_context_package": bool(row["changed_context_package"]),
                "successful": bool(row["successful"]),
                "traversed_node_ids": _json_load(row["traversed_node_ids_json"], []),
                "promoted_hot_node_ids": _json_load(row["promoted_hot_node_ids_json"], []),
                "cold_reservoir_node_ids": _json_load(row["cold_reservoir_node_ids_json"], []),
                "excluded_node_ids": _json_load(row["excluded_node_ids_json"], []),
                "traversed_edge_count": int(row["traversed_edge_count"] or 0),
                "persistence_state": str(row["persistence_state"] or ""),
                "created_at": str(row["created_at"]),
            }
        )
    return events


def fetch_heuristic_calibration_snapshot(*, include_recent: bool = True) -> dict[str, Any]:
    bootstrap_runtime_store()
    with connect() as conn:
        store_rows = conn.execute(
            """
            SELECT scope_key, payload_json, updated_at
            FROM heuristic_calibration_store
            ORDER BY scope_key ASC
            """
        ).fetchall()
        event_count = int(conn.execute("SELECT COUNT(*) FROM heuristic_calibration_events").fetchone()[0] or 0)
    snapshot: dict[str, Any] = {
        "global": {},
        "query_classes": {},
        "goals": {},
        "compiled_priors": {},
        "failure_signatures": {},
        "spatial_correction_priors": {},
        "event_count": event_count,
        "recent_events": [],
        "recent_landing_correction_events": [],
        "updated_at": None,
    }
    if include_recent:
        snapshot["recent_events"] = fetch_recent_heuristic_calibration_events(limit=12)
        snapshot["recent_landing_correction_events"] = fetch_recent_landing_correction_events(limit=12)
    updated_values: list[str] = []
    for row in store_rows:
        scope_key = str(row["scope_key"] or "")
        payload = _json_load(row["payload_json"], {})
        updated_at = str(row["updated_at"] or "")
        if updated_at:
            updated_values.append(updated_at)
        if scope_key == "global":
            snapshot["global"] = payload
        elif scope_key.startswith("query_class::"):
            snapshot["query_classes"][scope_key.split("::", 1)[1]] = payload
        elif scope_key.startswith("goal::"):
            snapshot["goals"][scope_key.split("::", 1)[1]] = payload
        elif scope_key.startswith("compiled_prior::"):
            snapshot["compiled_priors"][scope_key.split("::", 1)[1]] = payload
        elif scope_key.startswith("failure_signature::"):
            snapshot["failure_signatures"][scope_key.split("::", 1)[1]] = payload
        elif scope_key.startswith("spatial_correction_prior::"):
            snapshot["spatial_correction_priors"][scope_key.split("::", 1)[1]] = payload
    snapshot["updated_at"] = max(updated_values) if updated_values else None
    return snapshot


def store_benchmark_run(*, benchmark_id: str, phase: str, report: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO benchmark_runs (benchmark_id, phase, report_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (str(benchmark_id), str(phase), _json_dump(report), utc_timestamp()),
        )
        conn.commit()


def fetch_latest_benchmark_run(*, phase: str | None = None) -> dict[str, Any] | None:
    with connect() as conn:
        if phase:
            row = conn.execute(
                """
                SELECT benchmark_id, phase, report_json, created_at
                FROM benchmark_runs
                WHERE phase = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(phase),),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT benchmark_id, phase, report_json, created_at
                FROM benchmark_runs
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
    if not row:
        return None
    return {
        "benchmark_id": str(row["benchmark_id"]),
        "phase": str(row["phase"]),
        "report": _json_load(row["report_json"], {}),
        "created_at": str(row["created_at"]),
    }


def _benchmark_runtime_metrics(benchmark: dict[str, Any] | None) -> dict[str, Any]:
    report = dict((benchmark or {}).get("report") or {})
    return dict(report.get("runtime_audit_metrics") or {})


def _truth_metric(
    runtime_audit: dict[str, Any],
    key: str,
    *,
    benchmark_key: str | None = None,
    benchmark_phase_key: str = "latest_route_richness_benchmark",
) -> float:
    observed = _safe_float(runtime_audit.get(key))
    benchmark_metrics = _benchmark_runtime_metrics(dict(runtime_audit.get(benchmark_phase_key) or {}))
    benchmark_value = _safe_float(benchmark_metrics.get(benchmark_key or key))
    return round(max(observed, benchmark_value), 6)


def _timing_observed(timing_percentiles: dict[str, Any], key: str) -> bool:
    metric = timing_percentiles.get(key)
    if not isinstance(metric, dict):
        return False
    return int(metric.get("count") or 0) > 0 or _safe_float(metric.get("p50")) > 0.0


def build_audit_truth_fields(runtime_audit: dict[str, Any]) -> dict[str, Any]:
    """Build the stable telemetry contract consumed by audit/UI/benchmarks."""
    ai_histogram = dict(runtime_audit.get("ai_contribution_reason_histogram") or {})
    ai_ratio = round(_safe_float(runtime_audit.get("ai_material_contribution_ratio")), 6)
    timing_percentiles = dict(runtime_audit.get("timing_percentiles") or {})
    mode_timing_percentiles = dict(runtime_audit.get("mode_timing_percentiles") or {})
    route_richness_score = _truth_metric(runtime_audit, "route_richness_score")
    highway_effective_use_ratio = _truth_metric(runtime_audit, "highway_effective_use_ratio")
    link_effective_use_ratio = _truth_metric(runtime_audit, "link_effective_use_ratio")
    destination_reached_ratio = _truth_metric(runtime_audit, "destination_reached_ratio")
    route_trace_session_ratio = round(_safe_float(runtime_audit.get("route_trace_session_ratio")), 6)
    route_travel_session_ratio = round(_safe_float(runtime_audit.get("route_travel_session_ratio")), 6)
    warm_hit_ratio = round(_safe_float(runtime_audit.get("warm_hit_ratio")), 6)
    warm_context_reuse_quality = round(_safe_float(runtime_audit.get("warm_context_reuse_quality")), 6)
    route_evidence_seen = (
        route_trace_session_ratio > 0.0
        or route_travel_session_ratio > 0.0
        or _safe_float(_benchmark_runtime_metrics(dict(runtime_audit.get("latest_route_richness_benchmark") or {})).get("route_richness_score")) > 0.0
    )
    warm_evidence_seen = warm_hit_ratio > 0.0 or warm_context_reuse_quality > 0.0
    first_answer_observed = _timing_observed(timing_percentiles, "answer_first_ms") or any(
        _timing_observed(dict(mode_metrics or {}), "answer_first_ms")
        for mode_metrics in mode_timing_percentiles.values()
        if isinstance(mode_metrics, dict)
    )
    final_answer_observed = _timing_observed(timing_percentiles, "answer_final_ms") or any(
        _timing_observed(dict(mode_metrics or {}), "answer_final_ms")
        for mode_metrics in mode_timing_percentiles.values()
        if isinstance(mode_metrics, dict)
    )
    consistency = {
        "ai_material_metric_reflects_evidence": not ai_histogram or ai_ratio > 0.0,
        "route_metric_reflects_evidence": not route_evidence_seen or route_richness_score > 0.0,
        "warm_metric_reflects_evidence": not warm_evidence_seen or warm_hit_ratio > 0.0 or warm_context_reuse_quality > 0.0,
        "first_answer_timing_present": first_answer_observed,
        "final_answer_timing_present": final_answer_observed,
    }
    canonical_telemetry = {
        "schema_version": "agvm.audit.truth.v1",
        "source": "fetch_runtime_audit",
        "ai": {
            "material_contribution_ratio": ai_ratio,
            "contribution_reason_histogram": ai_histogram,
            "material_contribution_seen": ai_ratio > 0.0 or bool(ai_histogram),
        },
        "route": {
            "route_richness_score": route_richness_score,
            "route_trace_session_ratio": route_trace_session_ratio,
            "route_travel_session_ratio": route_travel_session_ratio,
            "highway_route_use_ratio": round(_safe_float(runtime_audit.get("highway_route_use_ratio")), 6),
            "link_route_use_ratio": round(_safe_float(runtime_audit.get("link_route_use_ratio")), 6),
            "local_route_use_ratio": round(_safe_float(runtime_audit.get("local_route_use_ratio")), 6),
            "highway_effective_use_ratio": highway_effective_use_ratio,
            "link_effective_use_ratio": link_effective_use_ratio,
            "destination_reached_ratio": destination_reached_ratio,
        },
        "warm": {
            "warm_hit_ratio": warm_hit_ratio,
            "warm_partial_reuse_ratio": round(_safe_float(runtime_audit.get("warm_partial_reuse_ratio")), 6),
            "warm_context_reuse_quality": warm_context_reuse_quality,
            "warm_state_saved_ratio": round(_safe_float(runtime_audit.get("warm_state_saved_ratio")), 6),
        },
        "timing": {
            "timing_percentiles": timing_percentiles,
            "mode_timing_percentiles": mode_timing_percentiles,
            "first_answer_observed": first_answer_observed,
            "final_answer_observed": final_answer_observed,
        },
        "answer": {
            "answer_now_before_final_ratio": round(_safe_float(runtime_audit.get("answer_now_before_final_ratio")), 6),
            "answer_now_before_exploration_complete_ratio": round(_safe_float(runtime_audit.get("answer_now_before_exploration_complete_ratio")), 6),
            "final_closure_after_destination_resolution_ratio": round(_safe_float(runtime_audit.get("final_closure_after_destination_resolution_ratio")), 6),
            "context_level_1_before_final_ratio": round(_safe_float(runtime_audit.get("context_level_1_before_final_ratio")), 6),
        },
        "context": {
            "raw_text_coverage_ratio": round(_safe_float(runtime_audit.get("raw_text_coverage_ratio")), 6),
            "document_chunk_coverage_ratio": round(_safe_float(runtime_audit.get("document_chunk_coverage_ratio")), 6),
            "support_density": round(_safe_float(runtime_audit.get("support_density")), 6),
            "contradiction_exposure_ratio": round(_safe_float(runtime_audit.get("contradiction_exposure_ratio")), 6),
        },
        "geometry_audit": {
            "landing_fit_score": round(_safe_float(runtime_audit.get("geometry_landing_fit_score")), 6),
            "destination_alignment_score": round(_safe_float(runtime_audit.get("geometry_destination_alignment_score")), 6),
            "projection_error_ratio": round(_safe_float(runtime_audit.get("geometry_projection_error_ratio")), 6),
            "route_efficiency_score": round(_safe_float(runtime_audit.get("geometry_route_efficiency_score")), 6),
            "matrix_a_problem_likelihood": round(_safe_float(runtime_audit.get("matrix_a_problem_likelihood")), 6),
            "matrix_a_adjustment_gain": round(_safe_float(runtime_audit.get("matrix_a_adjustment_gain")), 6),
        },
        "benchmarks": {
            "latest_geometry": dict(runtime_audit.get("latest_geometry_benchmark") or {}),
            "latest_route_richness": dict(runtime_audit.get("latest_route_richness_benchmark") or {}),
            "latest_planner_merge": dict(runtime_audit.get("latest_planner_merge_benchmark") or {}),
            "latest_evaluation": dict(runtime_audit.get("latest_evaluation_benchmark") or {}),
        },
        "consistency": consistency,
    }
    return {
        "ai_material_contribution_ratio": ai_ratio,
        "ai_contribution_reason_histogram": ai_histogram,
        "canonical_telemetry": canonical_telemetry,
        "audit_truth_checks": consistency,
    }


def fetch_runtime_audit() -> dict[str, Any]:
    bootstrap_runtime_store()
    sqlite_path = current_sqlite_path()
    with connect() as conn:
        counts = {
            "nodes_nav": conn.execute("SELECT COUNT(*) FROM nodes_nav").fetchone()[0],
            "node_text": conn.execute("SELECT COUNT(*) FROM node_text").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM links").fetchone()[0],
            "highways": conn.execute("SELECT COUNT(*) FROM highways").fetchone()[0],
            "graph_edges": conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0],
            "atlas_buckets": conn.execute("SELECT COUNT(*) FROM atlas_cache").fetchone()[0],
            "search_sessions": conn.execute("SELECT COUNT(*) FROM search_sessions").fetchone()[0],
            "search_events": conn.execute("SELECT COUNT(*) FROM search_events").fetchone()[0],
            "correction_history": conn.execute("SELECT COUNT(*) FROM correction_history").fetchone()[0],
            "region_summaries": conn.execute("SELECT COUNT(*) FROM region_summaries").fetchone()[0],
            "benchmark_runs": conn.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0],
            "maintenance_runs": conn.execute("SELECT COUNT(*) FROM maintenance_runs").fetchone()[0],
            "warm_thread_state": conn.execute("SELECT COUNT(*) FROM warm_thread_state").fetchone()[0],
            "heuristic_calibration_store": conn.execute("SELECT COUNT(*) FROM heuristic_calibration_store").fetchone()[0],
            "heuristic_calibration_events": conn.execute("SELECT COUNT(*) FROM heuristic_calibration_events").fetchone()[0],
            "landing_correction_events": conn.execute("SELECT COUNT(*) FROM landing_correction_events").fetchone()[0],
        }
        node_type_rows = conn.execute(
            """
            SELECT memory_type, COALESCE(guide_area, '') AS guide_area, COUNT(*) AS count
            FROM nodes_nav
            GROUP BY memory_type, COALESCE(guide_area, '')
            """
        ).fetchall()
        session_rows = conn.execute(
            """
            SELECT status, stop_reason, thread_id, plan_json, result_json
            FROM search_sessions
            ORDER BY created_at DESC
            """
        ).fetchall()
        search_event_rows = conn.execute(
            """
            SELECT search_id, seq, event_type
            FROM search_events
            WHERE event_type IN ('answer_partial', 'context_update', 'step_complete', 'answer_final')
            ORDER BY search_id ASC, seq ASC
            """
        ).fetchall()
        nav_rows = conn.execute(
            """
            SELECT id, x, y, z, fine_bucket_key, coarse_bucket_key
            FROM nodes_nav
            """
        ).fetchall()
        maintenance_rows = conn.execute(
            """
            SELECT mode, applied, preview_only, report_json, created_at
            FROM maintenance_runs
            ORDER BY created_at DESC
            """
        ).fetchall()
    calibration_snapshot = fetch_heuristic_calibration_snapshot()
    calibration_compiled_priors = dict(calibration_snapshot.get("compiled_priors") or {})
    calibration_failure_signatures = dict(calibration_snapshot.get("failure_signatures") or {})
    calibration_compiled_summary = {
        str(key): {
            "scope_key": str(dict(payload).get("scope_key") or f"compiled_prior::{key}"),
            "version": int(dict(payload).get("version") or 0),
            "status": str(dict(payload).get("status") or "active"),
            "sample_count": float(dict(payload).get("sample_count") or 0.0),
            "success_count": float(dict(payload).get("success_count") or 0.0),
            "template_count": len(list(dict(dict(payload).get("priors") or {}).get("answer_strand_templates") or [])),
            "updated_at": dict(payload).get("updated_at"),
        }
        for key, payload in calibration_compiled_priors.items()
    }
    calibration_failure_summary = {
        str(key): {
            "scope_key": str(dict(payload).get("scope_key") or f"failure_signature::{key}"),
            "version": int(dict(payload).get("version") or 0),
            "status": str(dict(payload).get("status") or "review_candidate"),
            "sample_count": float(dict(payload).get("sample_count") or 0.0),
            "failure_count": float(dict(payload).get("failure_count") or 0.0),
            "review_required": bool(dict(dict(payload).get("review") or {}).get("required", True)),
            "updated_at": dict(payload).get("updated_at"),
        }
        for key, payload in calibration_failure_signatures.items()
    }
    calibration_summary = {
        "scope_count": 1
        + len(dict(calibration_snapshot.get("query_classes") or {}))
        + len(dict(calibration_snapshot.get("goals") or {}))
        + len(calibration_compiled_priors)
        + len(calibration_failure_signatures),
        "event_count": int(calibration_snapshot.get("event_count") or 0),
        "updated_at": calibration_snapshot.get("updated_at"),
        "compiled_prior_count": len(calibration_compiled_priors),
        "active_compiled_prior_count": sum(1 for payload in calibration_compiled_priors.values() if str(dict(payload).get("status") or "active") == "active"),
        "failure_signature_count": len(calibration_failure_signatures),
        "review_candidate_count": sum(1 for payload in calibration_failure_signatures.values() if bool(dict(dict(payload).get("review") or {}).get("required", True))),
        "compiled_priors": calibration_compiled_summary,
        "failure_signatures": calibration_failure_summary,
        "recent_events": [dict(item) for item in list(calibration_snapshot.get("recent_events") or [])[:6]],
    }
    file_sizes = {
        "sqlite_size_bytes": sqlite_path.stat().st_size if sqlite_path.exists() else 0,
    }
    positions = [
        {
            "id": str(row["id"]),
            "x": float(row["x"]),
            "y": float(row["y"]),
            "z": float(row["z"]),
            "fine_bucket_key": str(row["fine_bucket_key"]),
            "coarse_bucket_key": str(row["coarse_bucket_key"]),
        }
        for row in nav_rows
    ]
    radius_values = [math.sqrt(item["x"] ** 2 + item["y"] ** 2 + item["z"] ** 2) for item in positions]
    radius_histogram = {"core": 0, "inner": 0, "mid": 0, "outer": 0}
    octant_histogram: dict[str, int] = defaultdict(int)
    for radius in radius_values:
        if radius < 0.24:
            radius_histogram["core"] += 1
        elif radius < 0.42:
            radius_histogram["inner"] += 1
        elif radius < 0.68:
            radius_histogram["mid"] += 1
        else:
            radius_histogram["outer"] += 1
    for item in positions:
        octant_histogram[_octant_key(item["x"], item["y"], item["z"])] += 1

    nearest_distances: list[float] = []
    neighborhood_counts: list[int] = []
    for index, left in enumerate(positions):
        nearest = None
        neighborhood = 0
        for other_index, right in enumerate(positions):
            if other_index == index:
                continue
            dx = left["x"] - right["x"]
            dy = left["y"] - right["y"]
            dz = left["z"] - right["z"]
            fit = math.sqrt(dx * dx + dy * dy + dz * dz)
            if fit <= 0.24:
                neighborhood += 1
            if nearest is None or fit < nearest:
                nearest = fit
            if fit == 0:
                break
        if nearest is not None:
            nearest_distances.append(nearest)
        neighborhood_counts.append(neighborhood)

    fine_bucket_counts: dict[str, int] = defaultdict(int)
    coarse_bucket_counts: dict[str, int] = defaultdict(int)
    for item in positions:
        fine_bucket_counts[item["fine_bucket_key"]] += 1
        coarse_bucket_counts[item["coarse_bucket_key"]] += 1
    sorted_fine_counts = sorted(fine_bucket_counts.values())
    memory_type_histogram: dict[str, int] = defaultdict(int)
    guide_area_histogram: dict[str, int] = defaultdict(int)
    blank_guide_area = 0
    identity_count = 0
    for row in node_type_rows:
        memory_type = str(row["memory_type"] or "")
        guide_area = str(row["guide_area"] or "")
        count = int(row["count"] or 0)
        memory_type_histogram[memory_type] += count
        if guide_area:
            guide_area_histogram[guide_area] += count
        else:
            blank_guide_area += count
        if memory_type == "identity":
            identity_count += count

    timing_samples: dict[str, list[float]] = defaultdict(list)
    stop_reason_histogram: dict[str, int] = defaultdict(int)
    planner_mode_histogram: dict[str, int] = defaultdict(int)
    expected_guide_area_none = 0
    expected_guide_area_total = 0
    budget_exhausted_count = 0
    llm_scout_enabled_count = 0
    hybrid_merge_count = 0
    warm_hit_count = 0
    warm_partial_reuse_count = 0
    divergence_reset_count = 0
    answer_now_before_final_count = 0
    threaded_session_count = 0
    warm_state_saved_count = 0
    continuity_state_histogram: dict[str, int] = defaultdict(int)
    mode_timing_samples: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    document_mode_session_count = 0
    document_mode_detected_count = 0
    document_anchor_top_match_count = 0
    document_chunk_used_before_final_count = 0
    document_fact_support_count = 0
    document_answer_first_by_mode: dict[str, list[float]] = defaultdict(list)
    cold_document_answer_first_by_thread: dict[str, float] = {}
    warm_document_followup_deltas: list[float] = []
    route_trace_session_count = 0
    route_travel_session_count = 0
    route_decision_total = 0
    route_highway_count = 0
    route_link_count = 0
    route_local_count = 0
    destination_reached_count = 0
    merge_trigger_count = 0
    branch_controller_usage_count = 0
    branch_controller_override_count = 0
    master_llm_success_count = 0
    master_fallback_timeout_count = 0
    planner_influence_count = 0
    planner_family_dual_active_count = 0
    planner_family_comparable_count = 0
    planner_family_ai_win_count = 0
    planner_family_tie_count = 0
    planner_family_attribution_count = 0
    planner_arrival_samples: list[float] = []
    planner_seed_ms_samples: list[float] = []
    planner_seed_success_count = 0
    ai_material_contribution_count = 0
    ai_contribution_reason_histogram: dict[str, int] = defaultdict(int)
    answer_strand_counts: list[float] = []
    seed_goal_coverage_values: list[float] = []
    seed_destination_presence_values: list[float] = []
    seed_used_by_bootstrap_count = 0
    answer_now_before_exploration_complete_count = 0
    final_closure_after_destination_resolution_count = 0
    context_level_1_before_final_count = 0
    master_surface_state_histogram: dict[str, int] = defaultdict(int)
    master_fallback_reason_histogram: dict[str, int] = defaultdict(int)
    closure_blocker_reason_histogram: dict[str, int] = defaultdict(int)
    branch_reuse_count = 0
    branch_enrich_count = 0
    branch_fork_count = 0
    dual_origin_branch_count = 0
    merge_resolution_histogram: dict[str, int] = defaultdict(int)
    planner_family_overlap_values: list[float] = []
    planner_family_divergence_values: list[float] = []
    raw_text_coverage_values: list[float] = []
    document_chunk_coverage_values: list[float] = []
    support_density_values: list[float] = []
    contradiction_exposure_values: list[float] = []
    highway_route_yield_values: list[float] = []
    route_richness_scores: list[float] = []
    highway_effective_use_values: list[float] = []
    link_effective_use_values: list[float] = []
    heuristic_family_route_step_values: list[float] = []
    ai_family_route_step_values: list[float] = []
    dual_origin_family_route_step_values: list[float] = []
    execution_reorder_counts: list[int] = []
    execution_reorder_reasons: dict[str, int] = defaultdict(int)
    branch_duplication_values: list[float] = []
    branch_merge_values: list[float] = []
    warm_context_reuse_quality_values: list[float] = []
    maintenance_modes_histogram: dict[str, int] = defaultdict(int)
    maintenance_improvement_count = 0
    maintenance_geometry_improvement_count = 0
    maintenance_identity_improvement_count = 0
    maintenance_proactive_suggestion_count = 0
    applied_maintenance_run_count = 0
    maintenance_quality_scores: list[float] = []
    sleep_run_count = 0
    evolve_run_count = 0
    sleep_review_change_count = 0
    sleep_bridge_adjustment_count = 0
    evolve_structural_change_count = 0
    evolve_new_highway_count = 0
    sleep_quality_scores: list[float] = []
    evolve_quality_scores: list[float] = []
    sleep_identity_deltas: list[float] = []
    evolve_identity_deltas: list[float] = []
    sleep_geometry_deltas: list[float] = []
    evolve_geometry_deltas: list[float] = []
    sleep_changed_nodes: set[str] = set()
    evolve_changed_nodes: set[str] = set()
    maintenance_retrieval_gap_ratios: list[float] = []
    maintenance_retrieval_gap_run_count = 0
    post_retrieval_calibration_gain_values: list[float] = []
    working_memory_depromotion_ratios: list[float] = []
    working_memory_depromotion_review_count = 0
    calibrated_session_count = 0
    calibrated_success_count = 0
    calibrated_branch_count_deltas: list[float] = []
    calibrated_highway_use_rates: list[float] = []
    uncalibrated_highway_use_rates: list[float] = []

    def _families_for_branch(branch: dict[str, Any]) -> set[str]:
        origin_families = {
            str(item).strip().lower()
            for item in list(branch.get("origin_families") or [])
            if str(item).strip().lower() in {"heuristic", "ai"}
        }
        if origin_families:
            return origin_families
        planner_family = str(branch.get("planner_family") or "").strip().lower()
        return {planner_family if planner_family in {"heuristic", "ai"} else "heuristic"}
    for row in maintenance_rows:
        mode = str(row["mode"] or "unknown")
        maintenance_modes_histogram[mode] += 1
        applied = bool(row["applied"])
        if applied:
            applied_maintenance_run_count += 1
        report = _json_load(row["report_json"], {})
        score = _safe_float(report.get("overall_quality_delta_score"))
        maintenance_quality_scores.append(score)
        quality_delta = dict(report.get("quality_delta") or {})
        if score > 0:
            maintenance_improvement_count += 1
        if bool(quality_delta.get("geometry_improved")):
            maintenance_geometry_improvement_count += 1
        if bool(quality_delta.get("identity_improved")):
            maintenance_identity_improvement_count += 1
        if list(report.get("follow_up_candidates") or []) or list(report.get("proactive_opportunities") or []):
            maintenance_proactive_suggestion_count += 1
        retrieval_gap_review = dict(report.get("retrieval_gap_review") or {})
        if retrieval_gap_review:
            maintenance_retrieval_gap_ratios.append(
                _safe_float(retrieval_gap_review.get("maintenance_retrieval_gap_detection_ratio"))
            )
            if int(retrieval_gap_review.get("gap_session_count") or 0) > 0:
                maintenance_retrieval_gap_run_count += 1
            calibration_after_retrieval = dict(retrieval_gap_review.get("post_retrieval_calibration") or {})
            if calibration_after_retrieval:
                post_retrieval_calibration_gain_values.append(
                    _safe_float(calibration_after_retrieval.get("post_retrieval_calibration_gain"))
                )
        depromotion_policy = dict(report.get("working_memory_depromotion_policy") or {})
        if depromotion_policy:
            working_memory_depromotion_ratios.append(_safe_float(depromotion_policy.get("depromotion_candidate_ratio")))
            working_memory_depromotion_review_count += int(depromotion_policy.get("depromote_candidate_count") or 0)
        explicit_mode = mode.strip().lower()
        if explicit_mode == "sleep":
            sleep_run_count += 1
            sleep_quality_scores.append(score)
            sleep_identity_deltas.append(_safe_float(quality_delta.get("identity_memory_ratio")))
            sleep_geometry_deltas.append(_safe_float(quality_delta.get("crowded_bucket_ratio")))
            if (
                list(report.get("confidence_updates") or [])
                or list(report.get("alias_attachments") or [])
                or list(report.get("duplicate_candidates") or [])
                or list(report.get("archived_node_ids") or [])
                or list(report.get("superseded_node_ids") or [])
            ):
                sleep_review_change_count += 1
            if list(report.get("bridge_promotions") or []) or list(report.get("bridge_demotions") or []):
                sleep_bridge_adjustment_count += 1
            if applied:
                sleep_profile = dict(report.get("sleep_profile") or {})
                sleep_changed_nodes.update(str(node_id) for node_id in list(sleep_profile.get("changed_node_ids") or report.get("reviewed_node_ids") or []) if str(node_id).strip())
        elif explicit_mode == "evolve":
            evolve_run_count += 1
            evolve_quality_scores.append(score)
            evolve_identity_deltas.append(_safe_float(quality_delta.get("identity_memory_ratio")))
            evolve_geometry_deltas.append(_safe_float(quality_delta.get("crowded_bucket_ratio")))
            structural_changes = (
                list(report.get("retyped_nodes") or [])
                or list(report.get("repositioned_nodes") or [])
                or list(report.get("region_actions") or [])
                or list(report.get("created_nodes") or [])
                or list(report.get("archived_node_ids") or [])
                or list(report.get("superseded_node_ids") or [])
                or list(report.get("new_highways") or [])
            )
            if structural_changes:
                evolve_structural_change_count += 1
            if list(report.get("new_highways") or []):
                evolve_new_highway_count += 1
            if applied:
                evolve_profile = dict(report.get("evolve_profile") or {})
                evolve_changed_nodes.update(str(node_id) for node_id in list(evolve_profile.get("changed_node_ids") or report.get("reviewed_node_ids") or []) if str(node_id).strip())
    for row in session_rows:
        stop_reason = str(row["stop_reason"] or "").strip() or "unknown"
        stop_reason_histogram[stop_reason] += 1
        if stop_reason == "budget_exhausted":
            budget_exhausted_count += 1
        plan = _json_load(row["plan_json"], {}) if row["plan_json"] else {}
        plan_runtime = dict(plan.get("planner_runtime") or {})
        plan_mode = str(plan.get("planner_mode") or plan_runtime.get("planner_mode") or "unknown").strip() or "unknown"
        planner_mode_histogram[plan_mode] += 1
        if bool(plan_runtime.get("llm_scout_enabled")):
            llm_scout_enabled_count += 1
        for probe in list(plan.get("probes") or []):
            expected_guide_area_total += 1
            if not str(probe.get("expected_guide_area") or "").strip():
                expected_guide_area_none += 1
        result = _json_load(row["result_json"], {}) if row["result_json"] else {}
        timing = dict(result.get("timing") or {})
        planner_runtime = dict(result.get("planner_runtime") or plan_runtime)
        blackboard = dict(((result.get("shared_evidence") or {}).get("blackboard") or (result.get("shared_evidence") or {})))
        planner_seed_runtime = dict(result.get("planner_seed_runtime") or plan.get("planner_seed_runtime") or {})
        planner_seed_source = str(
            planner_runtime.get("planner_seed_source")
            or planner_seed_runtime.get("planner_seed_source")
            or ""
        ).strip()
        planner_seed_ms = planner_runtime.get("planner_seed_ms")
        if planner_seed_ms is None:
            planner_seed_ms = planner_seed_runtime.get("planner_seed_ms")
        if planner_seed_ms is not None:
            planner_seed_ms_samples.append(_safe_float(planner_seed_ms))
        if planner_seed_source == "llm":
            planner_seed_success_count += 1
        if bool(result.get("ai_material_contribution")):
            ai_material_contribution_count += 1
        ai_contribution_reason = str(result.get("ai_contribution_reason") or "").strip()
        if ai_contribution_reason:
            ai_contribution_reason_histogram[ai_contribution_reason] += 1
        strand_rows = list(result.get("answer_strands") or planner_runtime.get("answer_strands") or planner_seed_runtime.get("answer_strands") or [])
        if strand_rows:
            answer_strand_counts.append(float(len(strand_rows)))
        seed_goal_coverage = dict(result.get("seed_goal_coverage") or planner_runtime.get("seed_goal_coverage") or planner_seed_runtime.get("seed_goal_coverage") or {})
        if "coverage_ratio" in seed_goal_coverage:
            seed_goal_coverage_values.append(_safe_float(seed_goal_coverage.get("coverage_ratio")))
        seed_destination_presence = dict(result.get("seed_destination_presence") or planner_runtime.get("seed_destination_presence") or planner_seed_runtime.get("seed_destination_presence") or {})
        if "ratio" in seed_destination_presence:
            seed_destination_presence_values.append(_safe_float(seed_destination_presence.get("ratio")))
        if bool(planner_runtime.get("seed_used_by_bootstrap") or planner_seed_runtime.get("seed_used_by_bootstrap")):
            seed_used_by_bootstrap_count += 1
        if bool(
            result.get("answer_now_before_exploration_complete")
            or planner_runtime.get("answer_now_before_exploration_complete")
        ):
            answer_now_before_exploration_complete_count += 1
        if bool(
            result.get("final_closure_after_destination_resolution")
            or planner_runtime.get("final_closure_after_destination_resolution")
        ):
            final_closure_after_destination_resolution_count += 1
        if bool(
            result.get("context_level_1_before_final")
            or planner_runtime.get("context_level_1_before_final")
        ):
            context_level_1_before_final_count += 1
        route_richness_scores.append(_safe_float(planner_runtime.get("route_richness_score") or blackboard.get("route_richness_score")))
        highway_effective_use_values.append(_safe_float(planner_runtime.get("highway_effective_use_ratio") or blackboard.get("highway_effective_use_ratio")))
        link_effective_use_values.append(_safe_float(planner_runtime.get("link_effective_use_ratio") or blackboard.get("link_effective_use_ratio")))
        heuristic_family_route_step_values.append(_safe_float(planner_runtime.get("heuristic_family_route_step_ratio") or blackboard.get("heuristic_family_route_step_ratio")))
        ai_family_route_step_values.append(_safe_float(planner_runtime.get("ai_family_route_step_ratio") or blackboard.get("ai_family_route_step_ratio")))
        dual_origin_family_route_step_values.append(_safe_float(planner_runtime.get("dual_origin_family_route_step_ratio") or blackboard.get("dual_origin_family_route_step_ratio")))
        execution_reorder_counts.append(int(planner_runtime.get("execution_reorder_count") or blackboard.get("execution_reorder_count") or 0))
        for reason, count in dict(planner_runtime.get("execution_reorder_reasons") or blackboard.get("execution_reorder_reasons") or {}).items():
            execution_reorder_reasons[str(reason).strip() or "unspecified"] += int(count or 0)
        heuristic_calibration = dict(planner_runtime.get("heuristic_calibration") or {})
        continuity_summary = dict(result.get("continuity_summary") or {})
        shared_evidence = dict(result.get("shared_evidence") or {})
        evidence_reservoir = dict(result.get("evidence_reservoir") or {})
        reservoir_quality = dict(evidence_reservoir.get("quality_metrics") or result.get("context_quality_metrics") or {})
        thread_id = str(row["thread_id"] or "").strip()
        retrieval_mode = str(result.get("retrieval_mode") or planner_runtime.get("retrieval_mode") or "balanced").strip() or "balanced"
        document_mode = str(result.get("document_mode") or "none").strip() or "none"
        document_packets = list(result.get("document_packets") or [])
        matches = list(result.get("matches") or [])
        branches = [dict(branch) for branch in list(result.get("branches") or [])]
        master_state = dict(shared_evidence.get("master_state") or {})
        master_decision_history = [dict(item) for item in list(master_state.get("decision_history") or [])]
        master_decision_sources = {
            str(item.get("decision_source") or "").strip()
            for item in master_decision_history
            if str(item.get("decision_source") or "").strip()
        }
        master_surface_state = str(
            master_state.get("answer_surface_state")
            or result.get("answer_surface_state")
            or planner_runtime.get("answer_surface_state")
            or ""
        ).strip()
        if master_surface_state:
            master_surface_state_histogram[master_surface_state] += 1
        master_fallback_reason = str(
            master_state.get("last_fallback_reason")
            or next(
                (
                    item.get("fallback_reason")
                    for item in reversed(master_decision_history)
                    if str(item.get("fallback_reason") or "").strip()
                ),
                "",
            )
            or ""
        ).strip()
        if master_fallback_reason:
            master_fallback_reason_histogram[master_fallback_reason] += 1
        closure_blockers = list(
            master_state.get("final_closure_blockers")
            or result.get("final_closure_blockers")
            or planner_runtime.get("final_closure_blockers")
            or []
        )
        for blocker in closure_blockers:
            if not isinstance(blocker, dict):
                continue
            blocker_reason = str(
                blocker.get("reason")
                or blocker.get("state_reason")
                or blocker.get("state")
                or blocker.get("label")
                or "unspecified"
            ).strip() or "unspecified"
            closure_blocker_reason_histogram[blocker_reason] += 1
        if "llm" in master_decision_sources:
            master_llm_success_count += 1
        if "fallback_timeout" in master_decision_sources:
            master_fallback_timeout_count += 1
        controller_recommendations = [
            dict(branch.get("controller_recommendation") or {})
            for branch in branches
            if dict(branch.get("controller_recommendation") or {})
        ]
        branch_count_adjustment = float(heuristic_calibration.get("branch_count_adjustment") or 0.0)
        calibration_scope_keys = [str(item) for item in list(heuristic_calibration.get("scope_keys_used") or []) if str(item).strip()]
        calibrated = bool(heuristic_calibration.get("applied")) or bool(calibration_scope_keys)
        total_route_hops = sum(
            float(branch.get("highway_hops_taken") or 0.0) + float(branch.get("link_hops_taken") or 0.0) + float(branch.get("local_hops_taken") or 0.0)
            for branch in branches
        )
        highway_use_rate = (
            sum(float(branch.get("highway_hops_taken") or 0.0) for branch in branches) / total_route_hops
            if total_route_hops > 0.0
            else 0.0
        )
        if calibrated:
            calibrated_session_count += 1
            if str(result.get("answerability_state") or "").strip() in {"grounded", "partial"}:
                calibrated_success_count += 1
            calibrated_branch_count_deltas.append(abs(branch_count_adjustment))
            calibrated_highway_use_rates.append(highway_use_rate)
        else:
            uncalibrated_highway_use_rates.append(highway_use_rate)
        controller_sources = {
            str(
                recommendation.get("decision_source")
                or branch.get("controller_decision_source")
                or ""
            ).strip()
            for branch, recommendation in zip(
                branches,
                [dict(branch.get("controller_recommendation") or {}) for branch in branches],
                strict=False,
            )
            if str(
                recommendation.get("decision_source")
                or branch.get("controller_decision_source")
                or ""
            ).strip()
        }
        if "llm" in controller_sources:
            branch_controller_usage_count += 1
        if any(bool(recommendation.get("override_applied")) for recommendation in controller_recommendations):
            branch_controller_override_count += 1
        family_plans = dict(planner_runtime.get("family_plans") or plan_runtime.get("family_plans") or {})
        heuristic_family_plan = dict(family_plans.get("heuristic") or plan.get("heuristic_family_plan") or {})
        ai_family_plan = dict(family_plans.get("ai") or plan.get("ai_family_plan") or {})
        family_overlap = dict(shared_evidence.get("family_overlap") or {})
        family_divergence = dict(shared_evidence.get("family_divergence") or {})
        family_yield = dict(shared_evidence.get("family_yield") or {})
        if planner_runtime.get("llm_completed_ms") is not None:
            planner_arrival_samples.append(_safe_float(planner_runtime.get("llm_completed_ms")))
        if family_overlap:
            planner_family_overlap_values.append(_safe_float(family_overlap.get("overlap_ratio")))
        if family_divergence:
            planner_family_divergence_values.append(_safe_float(family_divergence.get("divergence_ratio")))
        heuristic_branch_rows = [branch for branch in branches if "heuristic" in _families_for_branch(branch)]
        ai_branch_rows = [branch for branch in branches if "ai" in _families_for_branch(branch)]
        physical_heuristic_branch_rows = [branch for branch in branches if str(branch.get("planner_family") or "").strip().lower() == "heuristic"]
        physical_ai_branch_rows = [branch for branch in branches if str(branch.get("planner_family") or "").strip().lower() == "ai"]
        heuristic_branch_count = len(heuristic_branch_rows)
        ai_branch_count = len(ai_branch_rows)
        if heuristic_branch_count and ai_branch_count:
            planner_family_dual_active_count += 1
        ai_probe_count = int(ai_family_plan.get("probe_count") or 0)
        heuristic_probe_count = int(heuristic_family_plan.get("probe_count") or 0)
        if ai_probe_count > 0 or ai_branch_count > 0 or int(planner_runtime.get("llm_added_probe_count") or 0) > 0:
            planner_influence_count += 1
        if heuristic_probe_count > 0 and ai_probe_count > 0:
            planner_family_comparable_count += 1
            heuristic_route_yield = _safe_float(family_yield.get("heuristic"))
            ai_route_yield = _safe_float(family_yield.get("ai"))
            heuristic_evidence_count = sum(len(list(branch.get("evidence_node_ids") or [])) for branch in heuristic_branch_rows)
            ai_evidence_count = sum(len(list(branch.get("evidence_node_ids") or [])) for branch in ai_branch_rows)
            if ai_route_yield > heuristic_route_yield or (ai_route_yield == heuristic_route_yield and ai_evidence_count > heuristic_evidence_count):
                planner_family_ai_win_count += 1
            elif ai_route_yield == heuristic_route_yield and ai_evidence_count == heuristic_evidence_count:
                planner_family_tie_count += 1
        reservoir_entries = [dict(entry) for entry in list(evidence_reservoir.get("entries") or [])]
        if branches or reservoir_entries:
            branch_attribution_ready = all(
                list(_families_for_branch(branch))
                and str(branch.get("family_branch_id") or "").strip()
                and str(branch.get("family_plan_id") or "").strip()
                for branch in branches
            ) if branches else True
            reservoir_attribution_ready = all(list(entry.get("planner_families") or []) for entry in reservoir_entries) if reservoir_entries else True
            if branch_attribution_ready and reservoir_attribution_ready:
                planner_family_attribution_count += 1
        session_branch_reuse_count = sum(1 for branch in branches if str(branch.get("merge_outcome") or "") == "reuse_branch")
        session_branch_enrich_count = sum(1 for branch in branches if str(branch.get("merge_outcome") or "") == "enrich_branch")
        session_branch_fork_count = sum(1 for branch in branches if str(branch.get("merge_outcome") or "") == "fork_new_branch")
        branch_reuse_count += session_branch_reuse_count
        branch_enrich_count += session_branch_enrich_count
        branch_fork_count += session_branch_fork_count
        dual_origin_branch_count += sum(1 for branch in branches if bool(branch.get("dual_origin")))
        merge_resolution_histogram["reuse_branch"] += session_branch_reuse_count
        merge_resolution_histogram["enrich_branch"] += session_branch_enrich_count
        merge_resolution_histogram["fork_new_branch"] += session_branch_fork_count
        route_entries = [dict(entry) for branch in branches for entry in list(branch.get("route_trace") or [])]
        if route_entries:
            route_trace_session_count += 1
        travel_entries = [entry for entry in route_entries if bool(entry.get("travel_performed"))]
        if travel_entries:
            route_travel_session_count += 1
        route_decision_total += len(travel_entries)
        route_highway_count += sum(1 for entry in travel_entries if str(entry.get("edge_type") or "") == "highway")
        route_link_count += sum(1 for entry in travel_entries if str(entry.get("edge_type") or "") == "link")
        route_local_count += sum(1 for entry in travel_entries if str(entry.get("edge_type") or "") == "local")
        if any(bool(entry.get("destination_reached")) for entry in route_entries):
            destination_reached_count += 1
        if any(str(branch.get("stop_reason") or "") in {"merged_duplicate_route", "merged_duplicate_destination"} for branch in branches):
            merge_trigger_count += 1
        if thread_id:
            threaded_session_count += 1
        continuity_state = str(continuity_summary.get("continuity_state") or "").strip()
        if continuity_state:
            continuity_state_histogram[continuity_state] += 1
        if bool(continuity_summary.get("warm_state_used")):
            warm_hit_count += 1
        if bool(continuity_summary.get("warm_state_used")) and continuity_state == "medium_continuity":
            warm_partial_reuse_count += 1
        if continuity_state == "low_continuity" and thread_id:
            divergence_reset_count += 1
        if bool(result.get("warm_state_saved")):
            warm_state_saved_count += 1
        if (
            int(planner_runtime.get("llm_added_probe_count") or 0) > 0
            or session_branch_reuse_count > 0
            or session_branch_enrich_count > 0
            or session_branch_fork_count > 0
        ):
            hybrid_merge_count += 1
        if reservoir_quality:
            raw_text_coverage_values.append(_safe_float(reservoir_quality.get("raw_text_coverage_ratio")))
            document_chunk_coverage_values.append(_safe_float(reservoir_quality.get("document_chunk_coverage_ratio")))
            support_density_values.append(_safe_float(reservoir_quality.get("support_density")))
            contradiction_exposure_values.append(_safe_float(reservoir_quality.get("contradiction_exposure_ratio")))
            highway_route_yield_values.append(_safe_float(reservoir_quality.get("highway_route_yield")))
            branch_duplication_values.append(_safe_float(reservoir_quality.get("branch_duplication_ratio")))
            branch_merge_values.append(_safe_float(reservoir_quality.get("branch_merge_ratio")))
            warm_context_reuse_quality_values.append(_safe_float(reservoir_quality.get("warm_context_reuse_quality")))
        if document_mode != "none":
            document_mode_session_count += 1
            document_mode_detected_count += 1
            if any(bool((match.get("document_hit") or {}).get("document_anchor_id")) or bool((match.get("node") or {}).get("is_document_anchor")) for match in matches[:3]):
                document_anchor_top_match_count += 1
            chunk_matches = [
                match
                for match in matches
                if str(((match.get("document_hit") or {}).get("document_role") or (match.get("node") or {}).get("document_role") or "")).strip() == "chunk"
            ]
            fact_matches = [
                match
                for match in matches
                if str(((match.get("document_hit") or {}).get("document_role") or (match.get("node") or {}).get("document_role") or "")).strip() == "fact"
            ]
            if chunk_matches or any(
                list((packet or {}).get("chunk_node_ids") or [])
                or list((packet or {}).get("ordered_chunk_sequence") or [])
                for packet in document_packets
            ):
                document_chunk_used_before_final_count += 1
            if fact_matches or any(list((packet or {}).get("fact_node_ids") or []) for packet in document_packets):
                document_fact_support_count += 1
            if timing.get("answer_first_ms") is not None:
                answer_first_ms = _safe_float(timing.get("answer_first_ms"))
                document_answer_first_by_mode[retrieval_mode].append(answer_first_ms)
                if thread_id and continuity_state == "high_continuity" and bool(continuity_summary.get("warm_state_used")):
                    baseline = cold_document_answer_first_by_thread.get(thread_id)
                    if baseline is not None:
                        warm_document_followup_deltas.append(max(0.0, baseline - answer_first_ms))
                elif thread_id and not bool(continuity_summary.get("warm_state_used")) and thread_id not in cold_document_answer_first_by_thread:
                    cold_document_answer_first_by_thread[thread_id] = answer_first_ms
        if planner_runtime.get("plan_ms") is not None:
            timing_samples["plan_ms"].append(_safe_float(planner_runtime.get("plan_ms")))
            mode_timing_samples[retrieval_mode]["plan_ms"].append(_safe_float(planner_runtime.get("plan_ms")))
        for key in ("first_landing_ms", "first_context_ms", "answer_first_ms", "answer_final_ms", "total_ms"):
            if timing.get(key) is not None:
                timing_samples[key].append(_safe_float(timing.get(key)))
                mode_timing_samples[retrieval_mode][key].append(_safe_float(timing.get(key)))
        if timing.get("answer_first_ms") is not None and timing.get("answer_final_ms") is not None and _safe_float(timing.get("answer_first_ms")) < _safe_float(timing.get("answer_final_ms")):
            answer_now_before_final_count += 1
    event_sequences: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in search_event_rows:
        event_sequences[str(row["search_id"] or "")].append((int(row["seq"] or 0), str(row["event_type"] or "")))
    background_expansion_after_partial_count = 0
    answer_partial_session_count = 0
    for events in event_sequences.values():
        partial_seq = next((seq for seq, event_type in events if event_type == "answer_partial"), None)
        if partial_seq is None:
            continue
        answer_partial_session_count += 1
        if any(seq > partial_seq and event_type in {"context_update", "step_complete"} for seq, event_type in events):
            background_expansion_after_partial_count += 1
    geometry = {
        "radius_histogram": radius_histogram,
        "octant_histogram": dict(octant_histogram),
        "collision_counts": {
            "under_0_01": sum(1 for value in nearest_distances if value < 0.01),
            "under_0_03": sum(1 for value in nearest_distances if value < 0.03),
            "under_0_05": sum(1 for value in nearest_distances if value < 0.05),
        },
        "nearest_neighbor": {
            "count": len(nearest_distances),
            "median": round(_median(nearest_distances), 6) if nearest_distances else 0.0,
            "min": round(min(nearest_distances), 6) if nearest_distances else 0.0,
            "max": round(max(nearest_distances), 6) if nearest_distances else 0.0,
        },
        "neighborhood_median_r_0_24": round(_median([float(value) for value in neighborhood_counts]), 3) if neighborhood_counts else 0.0,
        "bucket_density": {
            "fine_bucket_count": len(fine_bucket_counts),
            "coarse_bucket_count": len(coarse_bucket_counts),
            "fine_bucket_max": max(sorted_fine_counts) if sorted_fine_counts else 0,
            "fine_bucket_median": sorted_fine_counts[len(sorted_fine_counts) // 2] if sorted_fine_counts else 0,
        },
    }
    total_nodes = max(1, int(counts["nodes_nav"]))
    timing_percentiles = {
        key: {
            "p50": round(_percentile(values, 0.5), 3),
            "p95": round(_percentile(values, 0.95), 3),
            "count": len(values),
        }
        for key, values in timing_samples.items()
        if values
    }
    latest_benchmark = fetch_latest_benchmark_run()
    latest_stream_benchmark = fetch_latest_benchmark_run(phase="stream")
    latest_documents_benchmark = fetch_latest_benchmark_run(phase="documents")
    latest_maintenance_benchmark = fetch_latest_benchmark_run(phase="maintenance")
    latest_calibration_benchmark = fetch_latest_benchmark_run(phase="calibration")
    latest_planner_merge_benchmark = fetch_latest_benchmark_run(phase="planner_merge")
    latest_geometry_benchmark = fetch_latest_benchmark_run(phase="geometry_audit")
    latest_route_richness_benchmark = fetch_latest_benchmark_run(phase="route_richness")
    latest_master_closure_benchmark = fetch_latest_benchmark_run(phase="master_closure")
    latest_evaluation_benchmark = fetch_latest_benchmark_run(phase="evaluation")
    final_evaluation_matrix = dict((latest_evaluation_benchmark or {}).get("report", {}).get("final_evaluation_matrix") or {})
    if not final_evaluation_matrix:
        final_evaluation_matrix = dict(((latest_benchmark or {}).get("report") or {}).get("final_evaluation_matrix") or {})
    if not final_evaluation_matrix:
        final_evaluation_matrix = dict((((latest_benchmark or {}).get("report") or {}).get("suites") or {}).get("evaluation", {}).get("final_evaluation_matrix") or {})
    sleep_vs_evolve_overlap_ratio = round(
        len(sleep_changed_nodes & evolve_changed_nodes) / max(1, len(sleep_changed_nodes | evolve_changed_nodes)),
        6,
    )
    maintenance_mode_specific_quality_delta = {
        "sleep": {
            "run_count": sleep_run_count,
            "avg_quality_delta_score": round(sum(sleep_quality_scores) / max(1, len(sleep_quality_scores)), 6) if sleep_quality_scores else 0.0,
            "avg_identity_delta": round(sum(sleep_identity_deltas) / max(1, len(sleep_identity_deltas)), 6) if sleep_identity_deltas else 0.0,
            "avg_geometry_delta": round(sum(sleep_geometry_deltas) / max(1, len(sleep_geometry_deltas)), 6) if sleep_geometry_deltas else 0.0,
        },
        "evolve": {
            "run_count": evolve_run_count,
            "avg_quality_delta_score": round(sum(evolve_quality_scores) / max(1, len(evolve_quality_scores)), 6) if evolve_quality_scores else 0.0,
            "avg_identity_delta": round(sum(evolve_identity_deltas) / max(1, len(evolve_identity_deltas)), 6) if evolve_identity_deltas else 0.0,
            "avg_geometry_delta": round(sum(evolve_geometry_deltas) / max(1, len(evolve_geometry_deltas)), 6) if evolve_geometry_deltas else 0.0,
        },
    }
    mode_timing_percentiles = {
        mode: {
            key: {
                "p50": round(_percentile(values, 0.5), 3),
                "p95": round(_percentile(values, 0.95), 3),
                "count": len(values),
            }
            for key, values in timing_by_key.items()
            if values
        }
        for mode, timing_by_key in mode_timing_samples.items()
    }
    geometry_benchmark_metrics = _benchmark_runtime_metrics(latest_geometry_benchmark)
    route_benchmark_metrics = _benchmark_runtime_metrics(latest_route_richness_benchmark)

    def _route_truth_from_sessions(values: list[float], metric_key: str) -> float:
        session_value = round(_nonzero_median(values), 6) if values else 0.0
        benchmark_value = _safe_float(route_benchmark_metrics.get(metric_key))
        return round(max(session_value, benchmark_value), 6)

    destination_reached_ratio = round(destination_reached_count / max(1, len(session_rows)), 6)
    destination_reached_ratio = round(
        max(destination_reached_ratio, _safe_float(route_benchmark_metrics.get("destination_reached_ratio"))),
        6,
    )
    return {
        "service": APP_NAME,
        "version": APP_VERSION,
        "storage_backend": "sqlite",
        "counts": counts,
        "files": file_sizes,
        "geometry": geometry,
        "memory_type_histogram": dict(memory_type_histogram),
        "guide_area_histogram": dict(guide_area_histogram),
        "guide_area_blank_ratio": round(blank_guide_area / total_nodes, 6),
        "identity_memory_ratio": round(identity_count / total_nodes, 6),
        "timing_percentiles": timing_percentiles,
        "stop_reason_histogram": dict(stop_reason_histogram),
        "planner_mode_histogram": dict(planner_mode_histogram),
        "budget_exhausted_ratio": round(budget_exhausted_count / max(1, len(session_rows)), 6),
        "expected_guide_area_none_ratio": round(expected_guide_area_none / max(1, expected_guide_area_total), 6),
        "llm_scout_enabled_ratio": round(llm_scout_enabled_count / max(1, len(session_rows)), 6),
        "hybrid_merge_ratio": round(hybrid_merge_count / max(1, len(session_rows)), 6),
        "warm_hit_ratio": round(warm_hit_count / max(1, threaded_session_count), 6),
        "warm_partial_reuse_ratio": round(warm_partial_reuse_count / max(1, warm_hit_count), 6),
        "divergence_reset_ratio": round(divergence_reset_count / max(1, threaded_session_count), 6),
        "answer_now_before_final_ratio": round(answer_now_before_final_count / max(1, len(session_rows)), 6),
        "background_expansion_after_partial_ratio": round(background_expansion_after_partial_count / max(1, answer_partial_session_count), 6),
        "warm_state_saved_ratio": round(warm_state_saved_count / max(1, threaded_session_count), 6),
        "continuity_state_histogram": dict(continuity_state_histogram),
        "mode_timing_percentiles": mode_timing_percentiles,
        "document_mode_detected_ratio": round(document_mode_detected_count / max(1, document_mode_session_count), 6),
        "document_anchor_top_match_ratio": round(document_anchor_top_match_count / max(1, document_mode_session_count), 6),
        "document_chunk_used_before_final_ratio": round(document_chunk_used_before_final_count / max(1, document_mode_session_count), 6),
        "document_fact_support_ratio": round(document_fact_support_count / max(1, document_mode_session_count), 6),
        "raw_text_coverage_ratio": round(_median(raw_text_coverage_values), 6) if raw_text_coverage_values else 0.0,
        "document_chunk_coverage_ratio": round(_median(document_chunk_coverage_values), 6) if document_chunk_coverage_values else 0.0,
        "support_density": round(_median(support_density_values), 6) if support_density_values else 0.0,
        "contradiction_exposure_ratio": round(_median(contradiction_exposure_values), 6) if contradiction_exposure_values else 0.0,
        "highway_route_yield": round(_median(highway_route_yield_values), 6) if highway_route_yield_values else 0.0,
        "branch_duplication_ratio": round(_median(branch_duplication_values), 6) if branch_duplication_values else 0.0,
        "branch_merge_ratio": round(_median(branch_merge_values), 6) if branch_merge_values else 0.0,
        "geometry_landing_fit_score": round(_safe_float(geometry_benchmark_metrics.get("geometry_landing_fit_score")), 6),
        "geometry_destination_alignment_score": round(_safe_float(geometry_benchmark_metrics.get("geometry_destination_alignment_score")), 6),
        "geometry_projection_error_ratio": round(_safe_float(geometry_benchmark_metrics.get("geometry_projection_error_ratio")), 6),
        "geometry_route_efficiency_score": round(_safe_float(geometry_benchmark_metrics.get("geometry_route_efficiency_score")), 6),
        "matrix_a_problem_likelihood": round(_safe_float(geometry_benchmark_metrics.get("matrix_a_problem_likelihood")), 6),
        "matrix_a_adjustment_gain": round(_safe_float(geometry_benchmark_metrics.get("matrix_a_adjustment_gain")), 6),
        "warm_context_reuse_quality": round(_nonzero_median(warm_context_reuse_quality_values), 6) if warm_context_reuse_quality_values else 0.0,
        "document_answer_first_ms_by_mode": {
            mode: {
                "p50": round(_percentile(values, 0.5), 3),
                "p95": round(_percentile(values, 0.95), 3),
                "count": len(values),
            }
            for mode, values in document_answer_first_by_mode.items()
            if values
        },
        "document_warm_followup_delta_ms": {
            "p50": round(_percentile(warm_document_followup_deltas, 0.5), 3) if warm_document_followup_deltas else 0.0,
            "p95": round(_percentile(warm_document_followup_deltas, 0.95), 3) if warm_document_followup_deltas else 0.0,
            "count": len(warm_document_followup_deltas),
        },
        "route_trace_session_ratio": round(route_trace_session_count / max(1, len(session_rows)), 6),
        "route_travel_session_ratio": round(route_travel_session_count / max(1, len(session_rows)), 6),
        "highway_route_use_ratio": round(route_highway_count / max(1, route_decision_total), 6),
        "link_route_use_ratio": round(route_link_count / max(1, route_decision_total), 6),
        "local_route_use_ratio": round(route_local_count / max(1, route_decision_total), 6),
        "route_richness_score": _route_truth_from_sessions(route_richness_scores, "route_richness_score"),
        "highway_effective_use_ratio": _route_truth_from_sessions(highway_effective_use_values, "highway_effective_use_ratio"),
        "link_effective_use_ratio": _route_truth_from_sessions(link_effective_use_values, "link_effective_use_ratio"),
        "heuristic_family_route_step_ratio": _route_truth_from_sessions(heuristic_family_route_step_values, "heuristic_family_route_step_ratio"),
        "ai_family_route_step_ratio": _route_truth_from_sessions(ai_family_route_step_values, "ai_family_route_step_ratio"),
        "dual_origin_family_route_step_ratio": _route_truth_from_sessions(dual_origin_family_route_step_values, "dual_origin_family_route_step_ratio"),
        "destination_reached_ratio": destination_reached_ratio,
        "execution_reorder_count": sum(execution_reorder_counts),
        "execution_reorder_reasons": dict(execution_reorder_reasons),
        "merge_trigger_ratio": round(merge_trigger_count / max(1, len(session_rows)), 6),
        "branch_controller_usage_ratio": round(branch_controller_usage_count / max(1, len(session_rows)), 6),
        "branch_controller_override_ratio": round(branch_controller_override_count / max(1, len(session_rows)), 6),
        "master_llm_success_ratio": round(master_llm_success_count / max(1, len(session_rows)), 6),
        "master_fallback_timeout_ratio": round(master_fallback_timeout_count / max(1, len(session_rows)), 6),
        "planner_influence_ratio": round(planner_influence_count / max(1, len(session_rows)), 6),
        "planner_family_dual_active_ratio": round(planner_family_dual_active_count / max(1, len(session_rows)), 6),
        "planner_family_win_ratio": round(planner_family_ai_win_count / max(1, planner_family_comparable_count), 6),
        "planner_family_tie_ratio": round(planner_family_tie_count / max(1, planner_family_comparable_count), 6),
        "planner_family_attribution_ratio": round(planner_family_attribution_count / max(1, len(session_rows)), 6),
        "planner_arrival_ms": {
            "p50": round(_percentile(planner_arrival_samples, 0.5), 3) if planner_arrival_samples else 0.0,
            "p95": round(_percentile(planner_arrival_samples, 0.95), 3) if planner_arrival_samples else 0.0,
            "count": len(planner_arrival_samples),
        },
        "planner_seed_ms": {
            "p50": round(_percentile(planner_seed_ms_samples, 0.5), 3) if planner_seed_ms_samples else 0.0,
            "p95": round(_percentile(planner_seed_ms_samples, 0.95), 3) if planner_seed_ms_samples else 0.0,
            "count": len(planner_seed_ms_samples),
        },
        "planner_seed_success_ratio": round(planner_seed_success_count / max(1, len(session_rows)), 6),
        "ai_material_contribution_ratio": round(ai_material_contribution_count / max(1, len(session_rows)), 6),
        "ai_contribution_reason_histogram": dict(ai_contribution_reason_histogram),
        "answer_strand_count": round(_median(answer_strand_counts), 6) if answer_strand_counts else 0.0,
        "seed_goal_coverage_ratio": round(_median(seed_goal_coverage_values), 6) if seed_goal_coverage_values else 0.0,
        "seed_destination_presence_ratio": round(_median(seed_destination_presence_values), 6) if seed_destination_presence_values else 0.0,
        "seed_used_by_bootstrap_ratio": round(seed_used_by_bootstrap_count / max(1, len(session_rows)), 6),
        "answer_now_before_exploration_complete_ratio": round(answer_now_before_exploration_complete_count / max(1, len(session_rows)), 6),
        "final_closure_after_destination_resolution_ratio": round(final_closure_after_destination_resolution_count / max(1, len(session_rows)), 6),
        "context_level_1_before_final_ratio": round(context_level_1_before_final_count / max(1, len(session_rows)), 6),
        "master_surface_state_histogram": dict(master_surface_state_histogram),
        "master_fallback_reason_histogram": dict(master_fallback_reason_histogram),
        "closure_blocker_reason_histogram": dict(closure_blocker_reason_histogram),
        "branch_reuse_ratio": round(branch_reuse_count / max(1, len(session_rows)), 6),
        "branch_enrich_ratio": round(branch_enrich_count / max(1, len(session_rows)), 6),
        "branch_fork_ratio": round(branch_fork_count / max(1, len(session_rows)), 6),
        "dual_origin_branch_ratio": round(dual_origin_branch_count / max(1, len(session_rows)), 6),
        "merge_resolution_histogram": dict(merge_resolution_histogram),
        "planner_family_overlap_ratio": round(_median(planner_family_overlap_values), 6) if planner_family_overlap_values else 0.0,
        "planner_family_divergence_ratio": round(_median(planner_family_divergence_values), 6) if planner_family_divergence_values else 0.0,
        "heuristic_calibration_scope_count": int(calibration_summary["scope_count"]),
        "heuristic_calibration_event_count": int(calibration_summary["event_count"]),
        "heuristic_compiled_prior_count": int(calibration_summary["compiled_prior_count"]),
        "heuristic_failure_signature_count": int(calibration_summary["failure_signature_count"]),
        "heuristic_review_candidate_count": int(calibration_summary["review_candidate_count"]),
        "heuristic_calibration_gain": round(min(int(calibration_summary["event_count"]), 40) / 40.0, 6),
        "post_retrieval_calibration_gain": round(
            _median(post_retrieval_calibration_gain_values)
            if post_retrieval_calibration_gain_values
            else min(int(calibration_summary["event_count"]), 40) / 40.0,
            6,
        ),
        "calibrated_bootstrap_success_ratio": round(calibrated_success_count / max(1, calibrated_session_count), 6),
        "calibrated_branch_count_delta": round(_median(calibrated_branch_count_deltas), 6) if calibrated_branch_count_deltas else 0.0,
        "calibrated_highway_use_delta": round(
            (sum(calibrated_highway_use_rates) / max(1, len(calibrated_highway_use_rates)))
            - (sum(uncalibrated_highway_use_rates) / max(1, len(uncalibrated_highway_use_rates))),
            6,
        ),
        "heuristic_calibration_summary": calibration_summary,
        "maintenance_run_count": int(counts["maintenance_runs"]),
        "applied_maintenance_run_count": applied_maintenance_run_count,
        "maintenance_modes_histogram": dict(maintenance_modes_histogram),
        "maintenance_improvement_ratio": round(maintenance_improvement_count / max(1, len(maintenance_rows)), 6),
        "maintenance_geometry_improvement_ratio": round(maintenance_geometry_improvement_count / max(1, len(maintenance_rows)), 6),
        "maintenance_identity_improvement_ratio": round(maintenance_identity_improvement_count / max(1, len(maintenance_rows)), 6),
        "maintenance_proactive_suggestion_ratio": round(maintenance_proactive_suggestion_count / max(1, len(maintenance_rows)), 6),
        "maintenance_repeated_evidence_ratio": round(min(applied_maintenance_run_count / 2.0, 1.0), 6),
        "sleep_review_change_ratio": round(sleep_review_change_count / max(1, sleep_run_count), 6) if sleep_run_count else 0.0,
        "sleep_bridge_adjustment_ratio": round(sleep_bridge_adjustment_count / max(1, sleep_run_count), 6) if sleep_run_count else 0.0,
        "evolve_structural_change_ratio": round(evolve_structural_change_count / max(1, evolve_run_count), 6) if evolve_run_count else 0.0,
        "evolve_new_highway_ratio": round(evolve_new_highway_count / max(1, evolve_run_count), 6) if evolve_run_count else 0.0,
        "sleep_vs_evolve_overlap_ratio": sleep_vs_evolve_overlap_ratio,
        "maintenance_retrieval_gap_detection_ratio": round(_median(maintenance_retrieval_gap_ratios), 6) if maintenance_retrieval_gap_ratios else 0.0,
        "maintenance_retrieval_gap_run_ratio": round(maintenance_retrieval_gap_run_count / max(1, len(maintenance_rows)), 6),
        "working_memory_depromotion_candidate_ratio": round(_median(working_memory_depromotion_ratios), 6) if working_memory_depromotion_ratios else 0.0,
        "working_memory_depromotion_review_count": working_memory_depromotion_review_count,
        "maintenance_mode_specific_quality_delta": maintenance_mode_specific_quality_delta,
        "last_benchmark": latest_benchmark or {},
        "latest_stream_benchmark": latest_stream_benchmark or {},
        "latest_documents_benchmark": latest_documents_benchmark or {},
        "latest_maintenance_benchmark": latest_maintenance_benchmark or {},
        "latest_calibration_benchmark": latest_calibration_benchmark or {},
        "latest_planner_merge_benchmark": latest_planner_merge_benchmark or {},
        "latest_geometry_benchmark": latest_geometry_benchmark or {},
        "latest_route_richness_benchmark": latest_route_richness_benchmark or {},
        "latest_master_closure_benchmark": latest_master_closure_benchmark or {},
        "latest_evaluation_benchmark": latest_evaluation_benchmark or {},
        "final_evaluation_matrix": final_evaluation_matrix,
        "maintenance_quality_scores": [round(value, 6) for value in maintenance_quality_scores[:8]],
    }
