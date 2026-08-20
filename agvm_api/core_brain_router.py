from __future__ import annotations

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
    refresh_local_brain_registry,
    rename_local_brain,
    resolve_brain_scope,
    set_active_brain,
)
from runtime_scope import use_runtime_brain
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


def create_core_brain_router() -> APIRouter:
    router = APIRouter()

    @router.get("/memory/brains", response_model=BrainRegistryResponse)
    def list_brains() -> BrainRegistryResponse:
        registry = refresh_local_brain_registry()
        return BrainRegistryResponse(**registry)

    @router.get("/mcp/brains", response_model=BrainRegistryResponse)
    def mcp_list_brains() -> BrainRegistryResponse:
        registry = refresh_local_brain_registry()
        return BrainRegistryResponse(**registry)

    @router.get("/memory/brains/active")
    def active_brain() -> dict[str, Any]:
        return active_brain_summary()

    @router.get("/mcp/brains/active")
    def mcp_active_brain() -> dict[str, Any]:
        return active_brain()

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
