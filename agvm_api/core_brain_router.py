# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from brain_registry import (
    BrainRegistryError,
    active_brain_summary,
    bootstrap_local_brain_registry,
    create_local_brain,
    delete_local_brain,
    export_local_brain,
    import_local_brain_archive,
    load_local_brain_registry,
    refresh_local_brain_registry,
    rename_local_brain,
    resolve_brain_scope,
    set_active_brain,
)
from runtime_scope import use_runtime_brain
from storage import load_graph, load_graph_view
from schemas import (
    BrainAdminOperationResponse,
    BrainCreateRequest,
    BrainDeleteRequest,
    BrainEnsureRequest,
    BrainEnsureResponse,
    BrainExportRequest,
    BrainImportRequest,
    BrainRegistryBootstrapRequest,
    BrainRegistryResponse,
    BrainRenameRequest,
    BrainSelectionRequest,
)


_BRAIN_ENSURE_LOCK = threading.Lock()
_BRAIN_SYNC_SNAPSHOT_LOCK = threading.Lock()


def create_core_brain_router() -> APIRouter:
    router = APIRouter()

    @router.get("/memory/brains", response_model=BrainRegistryResponse)
    def list_brains() -> BrainRegistryResponse:
        # Registry reads are on the hot path for every product shell. A GET must
        # not rescan every brain storage (some contain large vector databases);
        # mutations already refresh and persist the affected records.
        registry = load_local_brain_registry()
        return BrainRegistryResponse(**registry)

    @router.get("/mcp/brains", response_model=BrainRegistryResponse)
    def mcp_list_brains() -> BrainRegistryResponse:
        registry = load_local_brain_registry()
        return BrainRegistryResponse(**registry)

    @router.get("/memory/brains/active")
    def active_brain() -> dict[str, Any]:
        return active_brain_summary()

    @router.get("/mcp/brains/active")
    def mcp_active_brain() -> dict[str, Any]:
        return active_brain()

    @router.get("/memory/brains/{brain_id}/sync-snapshot")
    def brain_sync_snapshot(brain_id: str, max_nodes: int = 250000) -> dict[str, Any]:
        """Project one real local brain into the bounded Cloud sync contract."""

        if max_nodes < 1 or max_nodes > 250000:
            raise HTTPException(status_code=422, detail="brain_sync_snapshot_max_nodes_out_of_bounds")
        try:
            brain = resolve_brain_scope(brain_id=brain_id, require_explicit=True)
        except BrainRegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        with use_runtime_brain(brain):
            graph = load_graph()
            if not list(graph.get("nodes") or []):
                graph = load_graph_view()
        return _brain_sync_snapshot_payload(brain, graph, max_nodes=max_nodes)

    @router.post("/memory/brains/bootstrap", response_model=BrainRegistryResponse)
    def bootstrap_brain_registry(payload: BrainRegistryBootstrapRequest) -> BrainRegistryResponse:
        legacy_dirs = [Path(value).expanduser().resolve() for value in payload.legacy_data_dirs]
        registry = bootstrap_local_brain_registry(
            legacy_data_dirs=legacy_dirs or None,
            preferred_default_brain_id=payload.default_brain_id,
        )
        return BrainRegistryResponse(**registry)

    @router.post("/memory/brains/select", response_model=BrainRegistryResponse)
    def select_brain(payload: BrainSelectionRequest) -> BrainRegistryResponse:
        try:
            registry = set_active_brain(payload.brain_id, make_default=payload.make_default)
        except BrainRegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return BrainRegistryResponse(**registry)

    @router.post("/mcp/select-brain", response_model=BrainRegistryResponse)
    def mcp_select_brain(payload: BrainSelectionRequest) -> BrainRegistryResponse:
        return select_brain(payload)

    @router.post("/mcp/brains/ensure", response_model=BrainEnsureResponse)
    def mcp_ensure_brain(payload: BrainEnsureRequest) -> BrainEnsureResponse:
        try:
            with _BRAIN_ENSURE_LOCK:
                registry = refresh_local_brain_registry()
                requested_brain_id = str(payload.brain_id or "").strip() or None
                requested_display_name = str(payload.display_name or "").strip()
                brain = _brain_record_by_id(registry, requested_brain_id) or _brain_record_by_display_name(registry, requested_display_name)
                created = False
                selected = False
                warnings: list[str] = []
                if requested_brain_id and brain and str(brain.get("brain_id") or "") != requested_brain_id:
                    warnings.append("requested_brain_id_unmatched_existing_display_name_used")
                if not brain:
                    if not payload.create_if_missing:
                        return _ensure_brain_response(
                            status="blocked",
                            registry=registry,
                            activation_policy=payload.activation_policy,
                            warnings=["brain_not_found_create_if_missing_false"],
                        )
                    registry = create_local_brain(
                        brain_id=requested_brain_id,
                        display_name=requested_display_name,
                        description=payload.description,
                        make_active=False,
                        make_default=False,
                    )
                    registry = refresh_local_brain_registry()
                    brain = _brain_record_by_id(registry, requested_brain_id) or _brain_record_by_display_name(registry, requested_display_name)
                    if not brain:
                        raise BrainRegistryError("ensure_brain_created_record_not_found")
                    _bootstrap_brain_runtime_files(str(brain.get("brain_id") or ""))
                    registry = refresh_local_brain_registry()
                    brain = _brain_record_by_id(registry, str(brain.get("brain_id") or ""))
                    created = True

                brain_id = str(brain.get("brain_id") or "").strip()
                if payload.activation_policy != "return_only" and brain_id:
                    previous_active = str(registry.get("active_brain_id") or "")
                    previous_default = str(registry.get("default_brain_id") or "")
                    make_default = payload.activation_policy == "make_default"
                    if previous_active != brain_id or (make_default and previous_default != brain_id):
                        registry = set_active_brain(brain_id, make_default=make_default)
                        registry = refresh_local_brain_registry()
                        brain = _brain_record_by_id(registry, brain_id)
                        selected = True

                status = "created" if created else "selected" if selected else "existing"
                return _ensure_brain_response(
                    status=status,
                    registry=registry,
                    brain=brain,
                    created=created,
                    selected=selected,
                    activation_policy=payload.activation_policy,
                    warnings=warnings,
                )
        except BrainRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/memory/brains/create", response_model=BrainAdminOperationResponse)
    @router.post("/mcp/brains/create", response_model=BrainAdminOperationResponse)
    def create_brain_endpoint(payload: BrainCreateRequest) -> BrainAdminOperationResponse:
        try:
            registry = create_local_brain(
                brain_id=payload.brain_id,
                display_name=payload.display_name,
                description=payload.description,
                make_active=payload.make_active,
                make_default=payload.make_default,
            )
            brain_id = str(registry.get("active_brain_id") or payload.brain_id or "")
            if not payload.make_active:
                created = next((dict(item) for item in list(registry.get("brains") or []) if str(item.get("display_name") or "") == payload.display_name), {})
                brain_id = str(created.get("brain_id") or brain_id)
            _bootstrap_brain_runtime_files(brain_id)
            registry = refresh_local_brain_registry()
            brain = next((dict(item) for item in list(registry.get("brains") or []) if str(item.get("brain_id") or "") == brain_id), {})
            return BrainAdminOperationResponse(
                schema_version="agvm.local_brain_admin_operation.v1",
                action="create",
                status="created",
                brain_id=brain_id,
                brain=brain,
                registry=registry,
                next_slice="PR-12N-A Hosted Brain Registry And Tenant Isolation",
            )
        except BrainRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/memory/brains/rename", response_model=BrainAdminOperationResponse)
    @router.post("/mcp/brains/rename", response_model=BrainAdminOperationResponse)
    def rename_brain_endpoint(payload: BrainRenameRequest) -> BrainAdminOperationResponse:
        try:
            registry = rename_local_brain(payload.brain_id, display_name=payload.display_name, description=payload.description)
            brain = next((dict(item) for item in list(registry.get("brains") or []) if str(item.get("brain_id") or "") == payload.brain_id), {})
            return BrainAdminOperationResponse(
                schema_version="agvm.local_brain_admin_operation.v1",
                action="rename",
                status="renamed",
                brain_id=payload.brain_id,
                brain=brain,
                registry=registry,
                next_slice="PR-12N-A Hosted Brain Registry And Tenant Isolation",
            )
        except BrainRegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/memory/brains/export", response_model=BrainAdminOperationResponse)
    @router.post("/mcp/brains/export", response_model=BrainAdminOperationResponse)
    def export_brain_endpoint(payload: BrainExportRequest) -> BrainAdminOperationResponse:
        try:
            result = export_local_brain(
                payload.brain_id,
                export_dir=Path(payload.export_dir).expanduser().resolve() if payload.export_dir else None,
                export_kind="export",
            )
            return BrainAdminOperationResponse(**result)
        except BrainRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/memory/brains/backup", response_model=BrainAdminOperationResponse)
    def backup_brain_endpoint(payload: BrainExportRequest) -> BrainAdminOperationResponse:
        try:
            result = export_local_brain(
                payload.brain_id,
                export_dir=Path(payload.export_dir).expanduser().resolve() if payload.export_dir else None,
                export_kind="backup",
            )
            return BrainAdminOperationResponse(**result)
        except BrainRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/memory/brains/import", response_model=BrainAdminOperationResponse)
    @router.post("/memory/brains/restore", response_model=BrainAdminOperationResponse)
    @router.post("/mcp/brains/import", response_model=BrainAdminOperationResponse)
    def import_brain_endpoint(payload: BrainImportRequest) -> BrainAdminOperationResponse:
        try:
            result = import_local_brain_archive(
                Path(payload.archive_path).expanduser().resolve(),
                brain_id=payload.brain_id,
                display_name=payload.display_name,
                make_active=payload.make_active,
                make_default=payload.make_default,
                overwrite_existing=payload.overwrite_existing,
            )
            _bootstrap_brain_runtime_files(str(result.get("brain_id") or ""))
            result["registry"] = refresh_local_brain_registry()
            return BrainAdminOperationResponse(**result)
        except (BrainRegistryError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/memory/brains/import-upload", response_model=BrainAdminOperationResponse)
    def import_brain_upload_endpoint(
        archive: UploadFile = File(...),
        brain_id: str | None = Form(default=None),
        display_name: str | None = Form(default=None),
        make_active: bool = Form(default=False),
        make_default: bool = Form(default=False),
        overwrite_existing: bool = Form(default=False),
    ) -> BrainAdminOperationResponse:
        suffix = Path(archive.filename or "brain.agvm-brain.zip").suffix or ".zip"
        with tempfile.NamedTemporaryFile(prefix="agvm-brain-upload-", suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            while chunk := archive.file.read(1024 * 1024):
                tmp.write(chunk)
        try:
            result = import_local_brain_archive(
                tmp_path,
                brain_id=brain_id,
                display_name=display_name,
                make_active=make_active,
                make_default=make_default,
                overwrite_existing=overwrite_existing,
            )
            _bootstrap_brain_runtime_files(str(result.get("brain_id") or ""))
            result["registry"] = refresh_local_brain_registry()
            return BrainAdminOperationResponse(**result)
        except (BrainRegistryError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @router.post("/memory/brains/delete", response_model=BrainAdminOperationResponse)
    @router.post("/mcp/brains/delete", response_model=BrainAdminOperationResponse)
    def delete_brain_endpoint(payload: BrainDeleteRequest) -> BrainAdminOperationResponse:
        try:
            result = delete_local_brain(
                payload.brain_id,
                confirm_brain_id=payload.confirm_brain_id,
                delete_storage=payload.delete_storage,
            )
            return BrainAdminOperationResponse(**result)
        except BrainRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router


def _bootstrap_brain_runtime_files(brain_id: str) -> dict[str, Any]:
    record = _resolve_brain_record(brain_id=brain_id, require_explicit=True)
    with use_runtime_brain(record):
        from sqlite_store import bootstrap_runtime_store

        bootstrap_runtime_store()
        return {}


def _resolve_brain_record(brain_id: str | None = None, *, require_explicit: bool = False) -> dict[str, Any]:
    try:
        return resolve_brain_scope(brain_id=brain_id, require_explicit=require_explicit)
    except BrainRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _brain_record_by_id(registry: dict[str, Any], brain_id: str | None) -> dict[str, Any]:
    target = str(brain_id or "").strip()
    if not target:
        return {}
    return next((dict(item) for item in list(registry.get("brains") or []) if str((item or {}).get("brain_id") or "") == target), {})


def _brain_sync_snapshot_payload(
    brain: dict[str, Any],
    graph: dict[str, Any],
    *,
    max_nodes: int,
) -> dict[str, Any]:
    graph_nodes = [
        dict(item)
        for item in list(graph.get("nodes") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    selected_nodes = graph_nodes[:max_nodes]
    selected_ids = {str(item.get("id") or "").strip() for item in selected_nodes}
    selected_ids.discard("")
    selected_edges: list[dict[str, Any]] = []
    for item in list(graph.get("edges") or []):
        if not isinstance(item, dict):
            continue
        source = str(
            item.get("source")
            or item.get("source_id")
            or item.get("source_node_id")
            or ""
        ).strip()
        target = str(
            item.get("target")
            or item.get("target_id")
            or item.get("target_node_id")
            or ""
        ).strip()
        if source not in selected_ids or target not in selected_ids:
            continue
        selected_edges.append({**dict(item), "source": source, "target": target})

    sources: dict[str, dict[str, Any]] = {}
    for node in selected_nodes:
        source_id = str(node.get("source_unit_id") or node.get("source_id") or "").strip()
        if not source_id:
            continue
        sources.setdefault(
            source_id,
            {
                "id": source_id,
                "kind": str(node.get("source_type") or "memory_source"),
                "label": str(node.get("source_label") or source_id),
                "uri": str(node.get("source_uri") or "") or None,
                "trust": str(node.get("source_trust") or "") or None,
            },
        )

    storage_path = Path(str(brain.get("storage_path") or "")).expanduser().resolve()
    materialized_sources = _read_json_value(storage_path / "brain_sync_sources.json")
    materialized_profile = _read_json_value(storage_path / "brain_sync_profile.json")
    materialized_revisions = _read_json_value(storage_path / "brain_sync_revisions.json")
    profile_envelope = _read_json_object(storage_path / "brain_profile_v1.json")
    profile = dict(profile_envelope.get("profile") or {}) if profile_envelope else {}
    if profile:
        profile = {
            "schema_version": "agvm.brain_profile_sync.v1",
            "dimensions": len(list(profile.get("routing_fields") or [])) or 12,
            "basis": list(profile.get("routing_fields") or []),
            "state": str(profile_envelope.get("state") or "shadow"),
            "profile": profile,
            "benchmark": dict(profile_envelope.get("benchmark") or {}),
        }

    lifecycle = dict(brain.get("lifecycle") or {})
    profile_revision = int(lifecycle.get("profile_revision") or 0)
    bootstrap_session_id = str(lifecycle.get("bootstrap_session_id") or "").strip()
    exposed_sources = (
        list(materialized_sources)
        if isinstance(materialized_sources, list)
        else list(sources.values())
    )
    exposed_profile = (
        dict(materialized_profile)
        if isinstance(materialized_profile, dict)
        else profile
    )
    materialized_revision = (
        int(materialized_revisions.get("current_revision") or 0)
        if isinstance(materialized_revisions, dict)
        else 0
    )
    revision = _brain_sync_source_revision(
        storage_path=storage_path,
        content={
            "nodes": sorted(graph_nodes, key=lambda item: str(item.get("id") or "")),
            "edges": sorted(
                [dict(item) for item in list(graph.get("edges") or []) if isinstance(item, dict)],
                key=lambda item: (
                    str(item.get("source") or item.get("source_id") or item.get("source_node_id") or ""),
                    str(item.get("target") or item.get("target_id") or item.get("target_node_id") or ""),
                    str(item.get("id") or ""),
                ),
            ),
            "sources": exposed_sources,
            "profile": exposed_profile,
        },
        minimum_revision=max(1, profile_revision, materialized_revision),
    )
    history = [
        {
            "revision": revision,
            "profile_state": str(lifecycle.get("profile_state") or "not_reported"),
            "benchmark_state": str(lifecycle.get("benchmark_state") or "not_reported"),
            "bootstrap_state": str(lifecycle.get("bootstrap_state") or "not_started"),
            "bootstrap_session_id": bootstrap_session_id or None,
        }
    ]
    exposed_revisions = (
        {**dict(materialized_revisions), "current_revision": revision}
        if isinstance(materialized_revisions, dict)
        else {"current_revision": revision, "history": history}
    )
    return {
        "schema_version": "agvm.local_brain_sync_snapshot.v1",
        "brain_id": str(brain.get("brain_id") or ""),
        "display_name": str(brain.get("display_name") or brain.get("brain_id") or "Local brain"),
        "node_count": len(selected_nodes),
        "edge_count": len(selected_edges),
        "source_count": len(exposed_sources),
        "truncated": max(
            int(brain.get("node_count") or 0),
            int(_read_json_object_value(graph.get("meta"), "total_node_count") or 0),
            len(graph_nodes),
        ) > len(selected_nodes),
        "source_revision": revision,
        "snapshot": {
            "nodes": selected_nodes,
            "edges": selected_edges,
            "sources": exposed_sources,
            "profile": exposed_profile,
            "revisions": exposed_revisions,
        },
    }


def _brain_sync_source_revision(
    *,
    storage_path: Path,
    content: dict[str, Any],
    minimum_revision: int,
) -> int:
    content_sha256 = hashlib.sha256(
        json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    state_path = storage_path / "brain_sync_source_revision.json"
    with _BRAIN_SYNC_SNAPSHOT_LOCK:
        previous = _read_json_object(state_path)
        previous_revision = int(previous.get("source_revision") or 0)
        if (
            previous_revision >= minimum_revision
            and str(previous.get("content_sha256") or "") == content_sha256
        ):
            return previous_revision
        revision = max(minimum_revision, previous_revision + 1)
        state = {
            "schema_version": "agvm.local_brain_sync_source_revision.v1",
            "content_sha256": content_sha256,
            "source_revision": revision,
        }
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=state_path.parent,
            prefix=f".{state_path.name}.",
            suffix=".tmp",
        ) as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(state_path)
        return revision


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_json_value(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _read_json_object_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def _brain_record_by_display_name(registry: dict[str, Any], display_name: str | None) -> dict[str, Any]:
    target = str(display_name or "").strip().casefold()
    if not target:
        return {}
    return next((dict(item) for item in list(registry.get("brains") or []) if str((item or {}).get("display_name") or "").strip().casefold() == target), {})


def _ensure_brain_response(
    *,
    status: str,
    registry: dict[str, Any],
    brain: dict[str, Any] | None = None,
    created: bool = False,
    selected: bool = False,
    activation_policy: str = "return_only",
    warnings: list[str] | None = None,
) -> BrainEnsureResponse:
    resolved_brain = dict(brain or {})
    next_tools = ["retrieve_context", "grow_source_preview", "write_memory_preview"] if resolved_brain else ["list_brains", "create_brain"]
    return BrainEnsureResponse(
        status=status,
        brain_id=str(resolved_brain.get("brain_id") or "") or None,
        brain=resolved_brain,
        registry=registry,
        created=created,
        selected=selected,
        activation_policy=activation_policy,
        next_recommended_tools=next_tools,
        warnings=warnings or [],
    )
